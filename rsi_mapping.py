"""
Map KUKA RSI Ethernet variable names to the datatype pushed into each channel.

Inputs (provided):
  * nebumind_conf.xml  - socket configuration. Each <ELEMENT> gives a variable
                         name (TAG) and a 1-based channel index (INDX).
  * nebumind_rsi.rsix  - the concrete RSI wiring: the Ethernet block's inputs and
                         every RsiObject feeding it.
  * rsi_block_mapping.json - the object/output datatype "bib" produced by
                         rsi_block_mapping.py (run that first).

What this does:
  1. Read every Ethernet block input  ->  (OutObjId, OutIdx).
  2. Look the source object up in the bib by its ObjType:
       - if that output is an enum type, take the *selected* enum value (the
         instance's DataType ParamValue indexes the bib's `types` list);
       - otherwise take the fixed datatype from the bib.
  3. Resolve the variable name for each channel from nebumind_conf.xml
     (config INDX is 1-based, Ethernet InIdx is 0-based: InIdx = INDX - 1).

Result -> rsi_datatype_map.json : { variable name: datatype }.
"""

import json
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional


# The <RsiObjects> section declares a *default* XML namespace, so ElementTree
# prefixes every tag with it (e.g. "{RsiDriver}RsiObject").
NS_DRIVER = "RsiDriver"

# Config INDX is 1-based (KUKA RSI channel number); the Ethernet InIdx is
# 0-based. So the i-th config channel feeds Ethernet input InIdx = INDX - 1.
INDX_TO_INIDX_OFFSET = 1

UNKNOWN_DATATYPE = "unknown"

_HERE = os.path.dirname(os.path.abspath(__file__))


def _tag(namespace: str, name: str) -> str:
    """Return a namespaced ElementTree tag, e.g. ('RsiDriver', 'RsiObject')."""
    return f"{{{namespace}}}{name}"


# --------------------------------------------------------------------------- #
# Data containers
# --------------------------------------------------------------------------- #

@dataclass
class RsiObjectInstance:
    """A concrete object from <RsiObjects> (e.g. DigIn_1, MotorCurrent_1)."""
    obj_id: str                       # ObjId, e.g. "DigIn_1"
    obj_type: str                     # ObjType, e.g. "DigIn" (key into the bib)
    datatype_value: Optional[int]     # selected enum index (DataType ParamValue)


@dataclass
class ConfElement:
    """A <ELEMENT> from nebumind_conf.xml (one Ethernet channel)."""
    tag: str            # variable name, e.g. "Peening"
    indx: int           # 1-based channel index
    direction: str      # "SEND" or "RECEIVE"


# --------------------------------------------------------------------------- #
# Step 1 - parse the Ethernet block: InIdx -> (OutObjId, OutIdx)
# --------------------------------------------------------------------------- #

def parse_ethernet_inputs(rsi_root: ET.Element) -> dict:
    """
    Find every Ethernet RsiObject and return its input wiring.

    Returns {InIdx (int): (OutObjId (str), OutIdx (int))}. Each <Input> says
    which object output (OutObjId / OutIdx) is wired into that Ethernet channel.
    """
    inputs: dict = {}
    for rsi_object in rsi_root.iter(_tag(NS_DRIVER, "RsiObject")):
        if rsi_object.get("ObjType") != "Ethernet":
            continue
        for inp in rsi_object.iter(_tag(NS_DRIVER, "Input")):
            inputs[int(inp.get("InIdx"))] = (
                inp.get("OutObjId"), int(inp.get("OutIdx")),
            )
    return inputs


# --------------------------------------------------------------------------- #
# Step 2 - index every RsiObject instance by its ObjId
# --------------------------------------------------------------------------- #

def index_rsi_objects(rsi_root: ET.Element) -> dict:
    """
    Return {ObjId: RsiObjectInstance} for every object in <RsiObjects>.

    A "DataType" parameter, when present, selects the output datatype from an
    enumeration; its ParamValue is the 0-based index into the bib's enum list.
    """
    objects: dict = {}
    for rsi_object in rsi_root.iter(_tag(NS_DRIVER, "RsiObject")):
        datatype_value = None
        for param in rsi_object.iter(_tag(NS_DRIVER, "Parameter")):
            if param.get("Name") == "DataType":
                datatype_value = int(param.get("ParamValue"))
                break

        obj_id = rsi_object.get("ObjId")
        objects[obj_id] = RsiObjectInstance(
            obj_id=obj_id,
            obj_type=rsi_object.get("ObjType"),
            datatype_value=datatype_value,
        )
    return objects


# --------------------------------------------------------------------------- #
# Step 3 - parse the config: INDX -> variable name
# --------------------------------------------------------------------------- #

def parse_conf_variables(conf_path: str) -> dict:
    """
    Return {INDX (int): ConfElement} for every channel in nebumind_conf.xml.

    Only <ELEMENT>s whose INDX is a real integer are kept; "INTERNAL" channels
    (DEF_RIst, ...) carry no Ethernet index. Commented-out sample blocks are XML
    comments, so the parser never sees them.
    """
    root = ET.parse(conf_path).getroot()
    variables: dict = {}

    for direction in ("SEND", "RECEIVE"):
        section = root.find(direction)
        if section is None:
            continue
        for element in section.iter("ELEMENT"):
            try:
                indx = int(element.get("INDX"))
            except (TypeError, ValueError):
                continue  # "INTERNAL" or missing -> not an Ethernet channel
            variables[indx] = ConfElement(
                tag=element.get("TAG"), indx=indx, direction=direction,
            )
    return variables


# --------------------------------------------------------------------------- #
# Step 4 - the datatype bib and the per-output resolution
# --------------------------------------------------------------------------- #

def load_block_mapping(json_path: str) -> dict:
    """Load the object/output datatype bib produced by rsi_block_mapping.py."""
    if not os.path.exists(json_path):
        raise FileNotFoundError(
            f"{json_path} not found - run rsi_block_mapping.py first."
        )
    with open(json_path, encoding="utf-8") as fh:
        return json.load(fh)


def find_output(entry: Optional[dict], out_idx: int) -> Optional[dict]:
    """Return the output record with the given index from a bib entry."""
    if entry is None:
        return None
    return next((o for o in entry["outputs"] if o["index"] == out_idx), None)


def resolve_output_datatype(output: dict, datatype_value: Optional[int]) -> str:
    """
    Resolve one output's datatype using the bib.

      * enum  : pick the selected value (datatype_value indexes `types`).
      * fixed : the single documented datatype, "assume <type>" for a guess, or
                "unknown".
    """
    if output["enum"]:
        types = output["types"]
        if datatype_value is not None and 0 <= datatype_value < len(types):
            return types[datatype_value]           # the *selected* enum value
        return UNKNOWN_DATATYPE
    if output["types"]:
        return output["types"][0]                  # documented fixed datatype
    if output["assumed"]:
        # assumend type is not documented but is an engineering guess from rsi_block_mapping.py
        # it is possible to mark them assumed or unkown here depending on the confidence in the guess, but for now we will return the assumed type for all cases where it is available
        # return f"assume {output['assumed']}"
        return output["assumed"]        # engineering guess
    return UNKNOWN_DATATYPE


# --------------------------------------------------------------------------- #
# Step 5 - tie everything together
# --------------------------------------------------------------------------- #

def build_mapping(rsi_path: str, conf_path: str, block_json_path: str) -> list:
    """
    Resolve every named Ethernet channel to its datatype.

    Returns a list of records (one per config variable, ordered by INDX):
    {variable, datatype, object, output, enum}. `object`/`output` are kept for
    the console; the JSON export keeps only {variable: datatype}.
    """
    rsi_root = ET.parse(rsi_path).getroot()
    ethernet_inputs = parse_ethernet_inputs(rsi_root)   # InIdx -> (obj, outidx)
    instances = index_rsi_objects(rsi_root)             # ObjId -> instance
    conf_variables = parse_conf_variables(conf_path)    # INDX  -> conf element
    catalog = load_block_mapping(block_json_path)       # ObjType -> bib entry

    records = []
    for indx in sorted(conf_variables):
        conf = conf_variables[indx]
        in_idx = indx - INDX_TO_INIDX_OFFSET

        wiring = ethernet_inputs.get(in_idx)
        if wiring is None:
            records.append({"variable": conf.tag, "datatype": UNKNOWN_DATATYPE,
                            "object": None, "output": None, "enum": False})
            continue

        out_obj_id, out_idx = wiring
        instance = instances.get(out_obj_id)
        entry = catalog.get(instance.obj_type) if instance else None
        output = find_output(entry, out_idx)

        if instance is None or output is None:
            records.append({"variable": conf.tag, "datatype": UNKNOWN_DATATYPE,
                            "object": out_obj_id, "output": None, "enum": False})
            continue

        datatype = resolve_output_datatype(output, instance.datatype_value)
        records.append({
            "variable": conf.tag,
            "datatype": datatype,
            "object": out_obj_id,
            "output": output["name"],
            "enum": output["enum"],
        })
    return records


def write_json(records: list, json_path: str) -> None:
    """Write the final { variable name: datatype } mapping as JSON."""
    mapping = {r["variable"]: r["datatype"] for r in records}
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(mapping, fh, indent=2, ensure_ascii=False)


def print_mapping(records: list) -> None:
    """Print the resolved mapping as an aligned table."""
    headers = ["Variable", "Datatype", "Enum", "Via"]

    def row(r: dict) -> list:
        via = f"{r['object']}.{r['output']}" if r["object"] else "-"
        return [r["variable"], r["datatype"], str(r["enum"]), via]

    table = [headers] + [row(r) for r in records]
    widths = [max(len(t[c]) for t in table) for c in range(len(headers))]
    for i, line in enumerate(table):
        print("  ".join(c.ljust(widths[j]) for j, c in enumerate(line)))
        if i == 0:
            print("  ".join("-" * w for w in widths))


_MAPPING_DIR = os.path.join(_HERE, "mapping")
_TEST_CONFIG_DIR = os.path.join(_HERE, "test_config")
_DEFAULT_BLOCK_JSON = os.path.join(_MAPPING_DIR, "rsi_block_mapping.json")


def get_mapping(rsi_path: str, conf_path: str, block_json_path: str = _DEFAULT_BLOCK_JSON) -> dict:
    """Return the variable-to-datatype mapping as a dict, for use in other scripts.

    Args:
        rsi_path:        Path to the .rsix blueprint file (concrete robot wiring).
        conf_path:       Path to the _conf.xml parameter file (variable names).
        block_json_path: Path to rsi_block_mapping.json (datatype bib). Defaults
                         to mapping/rsi_block_mapping.json next to this file.

    Returns:
        {variable_name: datatype} dict for every SEND channel in conf_path.
    """
    records = build_mapping(rsi_path, conf_path, block_json_path)
    return {r["variable"]: r["datatype"] for r in records}


def main() -> None:
    rsi_path = os.path.join(_TEST_CONFIG_DIR, "nebumind_rsi.rsix")
    conf_path = os.path.join(_TEST_CONFIG_DIR, "nebumind_conf.xml")
    out_path = os.path.join(_TEST_CONFIG_DIR, "rsi_datatype_map.json")

    records = build_mapping(rsi_path, conf_path, _DEFAULT_BLOCK_JSON)
    print_mapping(records)

    try:
        write_json(records, out_path)
        print(f"\nJSON written to: {out_path}")
    except PermissionError:
        print(f"\nJSON NOT written - {out_path} is open (close it and re-run).")


if __name__ == "__main__":
    main()
