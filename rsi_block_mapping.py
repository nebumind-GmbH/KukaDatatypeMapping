"""
Generate the RSI object/output datatype "bib" as JSON.

Reads the generic KUKA library RSI_Ethernet.rsix (which lists every possible RSI
object) and, for each blueprint <Element> that has output channels, records - per
output - whether its datatype is selectable from an enumeration and what
datatype(s) it can be. The result is written to rsi_block_mapping.json and is
consumed by rsi_mapping.py.

Output (rsi_block_mapping.json), one entry per ObjType:

    { ObjType: { "id": <ObjTypeId>, "outputs": [
        { "index", "name", "enum", "types": [...], "assumed": <type|null> } ] } }

  * enum    : true  -> datatype is chosen from a DataType enumeration
              false -> datatype is fixed
  * types   : enum  -> all possible datatypes, in enum order (a selector picks
                       one of these by index)
              fixed -> the single documented datatype, or [] when unknown
  * assumed : engineering guess used when `types` is empty (else null)

Also writes rsi_block_unknown.json: a fill-in template of every object type whose
datatype is still unknown, so you can see what is missing and extend the bibs
below. All outputs of an object are assumed to share the same datatype.
"""

import json
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass

# The .rsix BlueprintCollections section declares a *default* XML namespace, so
# ElementTree prefixes every tag with it (e.g. "{RsiBlueprint}Element").
NS_BLUEPRINT = "RsiBlueprint"

# Marker for an object whose fixed output datatype is not documented anywhere.
UNKNOWN_DATATYPE = "unknown"

# Fixed output datatypes that ARE documented. Each RSI source object reads a KUKA
# system variable; the variable's Value type fixes the datatype. ObjType ->
# (datatype, system variable). References (page numbers are the printed pages):
#   SysVars = "KUKA System Variables 8.1 8.2 8.3.pdf"  (object -> $variable type)
#   RSI     = "KST_RSI_50_de.pdf"                      (object -> $variable, p.131-134)
# KRL INT = signed 32-bit -> Int32; KRL REAL = IEEE-754 single precision -> Float32
# (the Ethernet wire TYPE widens these to LONG/DOUBLE, but the source value is as below).
FIXED_DATATYPE_BIB = {
    # Value Type INT (SysVars p.84); Sen_PInt reads $SEN_PINT (RSI p.132).
    "Sen_PInt": ("Int32", "$SEN_PINT"),
    # Value Type REAL (SysVars p.84); Sen_PRea reads $SEN_PREA (RSI p.132).
    "Sen_PRea": ("Float32", "$SEN_PREA"),
    # Current Type REAL, % of max (SysVars p.32); MotorCurrent = $CURR_ACT[1..6] (RSI p.134).
    "MotorCurrent": ("Float32", "$CURR_ACT"),
    # $CURR_ACT axes 7..12 = ext axes E1..E6, REAL (SysVars p.32); RSI p.134.
    "MotorCurrentExt": ("Float32", "$CURR_ACT"),
    # Voltage Type REAL, -1.0..+1.0 (SysVars p.19); AnIn reads $ANIN (RSI p.131).
    "AnIn": ("Float32", "$ANIN"),
    # Voltage Type REAL, -1.0..+1.0 (SysVars p.19); AnOut reads $ANOUT (RSI p.131).
    "AnOut": ("Float32", "$ANOUT"),
    # Override Type INT, % 0..100 (SysVars p.65); OV_PRO reads $OV_PRO (RSI p.134).
    "OV_PRO": ("Int32", "$OV_PRO"),
    # $AXIS_ACT = E6AXIS, axis angles/positions in deg/mm = REAL (SysVars p.24);
    # AxisAct = robot axes A1..A6 (RSI p.134).
    "AxisAct": ("Float32", "$AXIS_ACT"),
    # $AXIS_ACT external axes E1..E6, REAL (SysVars p.24); AxisActExt (RSI p.134).
    "AxisActExt": ("Float32", "$AXIS_ACT"),
}
# Not added: PosAct -> $POS_ACT is E6POS, a MIXED struct (X,Y,Z,A,B,C = REAL but
# S,T = INT, SysVars p.68), which breaks the "one datatype per object" assumption.

# Fixed objects with no documented datatype. RSI signal-processing/generator
# objects (KST_RSI p.135-136) output a continuous analog signal, and RSI internal
# signals are REAL -> assume Float32. GearTorque is a real physical quantity (Nm)
# with no matching variable in this manual ($TORQUE_AXIS_ACT is *motor* torque).
ASSUMED_DATATYPE_BIB = {
    "Constant": "Float32",       # constant value (RSI p.135)
    "Cosine": "Float32",         # cosine generator (RSI p.135)
    "Sine": "Float32",           # sine generator (RSI p.136)
    "Rectangle": "Float32",      # rectangle generator (RSI p.136)
    "Sawtooth": "Float32",       # sawtooth generator (RSI p.136)
    "Triangle": "Float32",       # triangle generator (RSI p.136)
    "Source": "Float32",         # signal generator (RSI p.136)
    "D": "Float32",              # differentiator (RSI p.135)
    "Delay": "Float32",          # signal delay (RSI p.135)
    "GenCtrl": "Float32",        # generic filter, up to 8th order (RSI p.135)
    "I": "Float32",              # integrator (RSI p.135)
    "IIRFilter": "Float32",      # IIR filter (RSI p.135)
    "P": "Float32",              # gain (RSI p.135)
    "PD": "Float32",             # proportional-differential (RSI p.135)
    "PID": "Float32",            # PID (RSI p.135)
    "PT1": "Float32",            # 1st-order lag (RSI p.135)
    "PT2": "Float32",            # 2nd-order lag (RSI p.136)
    "GearTorque": "Float32",     # gear torque A1..A6, Nm (RSI p.134)
    "GearTorqueExt": "Float32",  # gear torque ext axes E1..E6, Nm (RSI p.134)
}

_HERE = os.path.dirname(os.path.abspath(__file__))


def _tag(namespace: str, name: str) -> str:
    """Return a namespaced ElementTree tag, e.g. ('RsiBlueprint', 'Element')."""
    return f"{{{namespace}}}{name}"


@dataclass
class Blueprint:
    """An object *definition* from <BlueprintCollections> (an <Element>)."""
    element_id: int                    # Id, links to an instance's ObjTypeId
    name: str                          # Name, e.g. "DigIn"
    outputs: dict                      # {output index (int): output name (str)}
    enum_params: dict                  # {param Index (int): [enum values]}
    enum_param_index_by_name: dict     # {param Name: param Index}


def load_blueprints(rsix_path: str) -> dict:
    """Parse <BlueprintCollections> from a .rsix file and return {Id: Blueprint}."""
    root = ET.parse(rsix_path).getroot()
    blueprints: dict = {}

    for element in root.iter(_tag(NS_BLUEPRINT, "Element")):
        element_id = element.get("Id")
        if element_id is None:
            # Editor-only helper elements (Comment, ConnectorIn, ...) have no Id.
            continue

        # --- outputs: {index -> name} ------------------------------------- #
        outputs: dict = {}
        outputs_node = element.find(_tag(NS_BLUEPRINT, "Outputs"))
        if outputs_node is not None:
            for out in outputs_node.findall(_tag(NS_BLUEPRINT, "Output")):
                outputs[int(out.get("Index"))] = out.get("Name")

        # --- enum parameters: {param index -> [values]} ------------------- #
        enum_params: dict = {}
        enum_param_index_by_name: dict = {}
        params_node = element.find(_tag(NS_BLUEPRINT, "Parameters"))
        if params_node is not None:
            for enum_param in params_node.findall(_tag(NS_BLUEPRINT, "EnumParameter")):
                param_index = int(enum_param.get("Index"))
                values_node = enum_param.find(_tag(NS_BLUEPRINT, "EnumValues"))
                values = [
                    v.text for v in values_node.findall(_tag(NS_BLUEPRINT, "Value"))
                ] if values_node is not None else []
                enum_params[param_index] = values
                enum_param_index_by_name[enum_param.get("Name")] = param_index

        blueprints[int(element_id)] = Blueprint(
            element_id=int(element_id),
            name=element.get("Name"),
            outputs=outputs,
            enum_params=enum_params,
            enum_param_index_by_name=enum_param_index_by_name,
        )
    return blueprints


def _output_typing(blueprint: Blueprint) -> tuple:
    """
    Return (is_enum, types, assumed) for a blueprint's outputs.

      * Enumeration  -> types = all DataType enum values,   assumed = None
      * Fixed (doc)  -> types = [documented datatype],      assumed = None
      * Fixed (guess)-> types = [] (unknown),               assumed = guess
      * Fixed (n/a)  -> types = [] (unknown),               assumed = None

    Only a "DataType" enum types the output; other enums (Type, Source,
    FilterType, ...) are configuration, not datatype.
    """
    if "DataType" in blueprint.enum_param_index_by_name:
        param_index = blueprint.enum_param_index_by_name["DataType"]
        return True, list(blueprint.enum_params[param_index]), None

    if blueprint.name in FIXED_DATATYPE_BIB:
        return False, [FIXED_DATATYPE_BIB[blueprint.name][0]], None  # documented

    return False, [], ASSUMED_DATATYPE_BIB.get(blueprint.name)       # guess / n/a


def build_catalog(blueprint_path: str) -> dict:
    """
    Return {ObjType: {"id": ObjTypeId, "outputs": [output, ...]}}, sorted by
    ObjType, for every object type that has output channels.

    Each output is {"index", "name", "enum", "types", "assumed"}. All outputs of
    an object share the same typing (agreed assumption).
    """
    blueprints = load_blueprints(blueprint_path)

    catalog = {}
    for blueprint in blueprints.values():
        if not blueprint.outputs:
            continue  # pure sinks (Map2DigOut, Monitor, Output, ...) have none

        is_enum, types, assumed = _output_typing(blueprint)
        outputs = [
            {
                "index": idx,
                "name": blueprint.outputs[idx],
                "enum": is_enum,
                "types": types,
                "assumed": assumed,
            }
            for idx in sorted(blueprint.outputs)
        ]
        catalog[blueprint.name] = {"id": blueprint.element_id, "outputs": outputs}

    return dict(sorted(catalog.items(), key=lambda kv: kv[0].lower()))


def build_unknown_template(catalog: dict) -> dict:
    """
    Return a fill-in template of every object type whose fixed output datatype is
    still unknown (i.e. not in FIXED_DATATYPE_BIB or ASSUMED_DATATYPE_BIB).

        { ObjType: { "datatype": "", "outputs": [name, ...] } }

    Workflow: look up the missing ObjType, then add it to one of the bibs above
    (ASSUMED_DATATYPE_BIB: "Name": "Float32"; FIXED_DATATYPE_BIB: "Name":
    ("Float32", "$VAR")). Re-running this script regenerates the template, so a
    resolved type drops out automatically.
    """
    template = {}
    for obj_type, entry in catalog.items():
        first = entry["outputs"][0]   # all outputs of an object share typing
        if not first["enum"] and not first["types"] and first["assumed"] is None:
            template[obj_type] = {
                "datatype": "",
                "outputs": [o["name"] for o in entry["outputs"]],
            }
    return template


def write_json(data: dict, json_path: str) -> None:
    """Write a dict as indented JSON."""
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def main() -> None:
    blueprint_path = os.path.join(_HERE, "RSI_Ethernet.rsix")
    catalog = build_catalog(blueprint_path)
    template = build_unknown_template(catalog)

    max_outputs = max(len(v["outputs"]) for v in catalog.values())
    print(f"{len(catalog)} object types with outputs, up to {max_outputs} each; "
          f"{len(template)} still have an unknown datatype.")

    for data, filename, label in (
        (catalog, "rsi_block_mapping.json", "bib"),
        (template, "rsi_block_unknown.json", "unknown template"),
    ):
        path = os.path.join(_HERE, filename)
        try:
            write_json(data, path)
            print(f"{label} written to: {path}")
        except PermissionError:
            print(f"{label} NOT written - {path} is open (close it and re-run).")


if __name__ == "__main__":
    main()
