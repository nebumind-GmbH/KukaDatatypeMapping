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

Also writes rsi_block_unknown.json: a fill-in template of the output channels
whose datatype is still unknown, so you can see what is missing and extend the
bibs below. Each output is typed independently, so an object with multiple
output channels may carry different datatypes per channel (see PosAct).
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
# system variable; the variable's Value type fixes the datatype. References:
#   SysVars = "KUKA System Variables 8.1 8.2 8.3.pdf"  (object -> $variable type)
#   RSI     = "KST_RSI_50_de.pdf"                      (object -> $variable, p.131-134)
# KRL INT = signed 32-bit -> Int32; KRL REAL = IEEE-754 single precision -> Float32
# (the Ethernet wire TYPE widens these to LONG/DOUBLE, but the source value is as below).
#
# The datatype is a single string when every output shares it, or a per-channel
# dict {output name: datatype} when an object's outputs differ (see PosAct). An
# output not listed in the dict falls through to ASSUMED_DATATYPE_BIB / unknown.
FIXED_DATATYPE_BIB = {
    "Sen_PInt": "Int32",           # $SEN_PINT, Value Type INT (SysVars p.84, RSI p.132)
    "Sen_PRea": "Float32",         # $SEN_PREA, Value Type REAL (SysVars p.84, RSI p.132)
    "MotorCurrent": "Float32",     # $CURR_ACT[1..6], Current Type REAL % of max (SysVars p.32, RSI p.134)
    "MotorCurrentExt": "Float32",  # $CURR_ACT[7..12] ext axes E1..E6, REAL (SysVars p.32, RSI p.134)
    "AnIn": "Float32",             # $ANIN, Voltage Type REAL -1.0..+1.0 (SysVars p.19, RSI p.131)
    "AnOut": "Float32",            # $ANOUT, Voltage Type REAL -1.0..+1.0 (SysVars p.19, RSI p.131)
    "OV_PRO": "Int32",             # $OV_PRO, Override Type INT % 0..100 (SysVars p.65, RSI p.134)
    "AxisAct": "Float32",          # $AXIS_ACT[A1..A6], E6AXIS angles/positions REAL (SysVars p.24, RSI p.134)
    "AxisActExt": "Float32",       # $AXIS_ACT[E1..E6] ext axes, REAL (SysVars p.24, RSI p.134)
    # $POS_ACT = E6POS (SysVars p.68); X,Y,Z in mm and A,B,C in deg are REAL.
    # Per-channel dict because S,T differ (see ASSUMED_DATATYPE_BIB).
    "PosAct": {"X": "Float32", "Y": "Float32", "Z": "Float32",
               "A": "Float32", "B": "Float32", "C": "Float32"},
}

# Assumed datatypes organised by RSI object category (KST RSI 5.0 §12.2).
# Like FIXED_DATATYPE_BIB, a value is a single datatype or a per-channel dict.
# Channels not listed in a per-channel dict fall through to unknown (see below).
ASSUMED_DATATYPE_BIB = {
    # --- Signal processing (§12.2.9) ---
    # Continuous analog signal objects; all inputs/outputs are KRL REAL -> Float32.
    "Constant": "Float32",        # constant value (RSI p.135)
    "Cosine": "Float32",          # cosine signal generator (RSI p.135)
    "Sine": "Float32",            # sine signal generator (RSI p.136)
    "Rectangle": "Float32",       # rectangle signal generator (RSI p.136)
    "Sawtooth": "Float32",        # sawtooth signal generator (RSI p.136)
    "Triangle": "Float32",        # triangle signal generator (RSI p.136)
    "Source": "Float32",          # generic signal generator (RSI p.136)
    "D": "Float32",               # differentiator (RSI p.135)
    "Delay": "Float32",           # signal delay (RSI p.135)
    "GenCtrl": "Float32",         # generic filter, up to 8th order (RSI p.135)
    "I": "Float32",               # integrator, trapezoidal (RSI p.135)
    "IIRFilter": "Float32",       # IIR filter (RSI p.135)
    "P": "Float32",               # proportional gain (RSI p.135)
    "PD": "Float32",              # proportional-differential (RSI p.135)
    "PID": "Float32",             # PID controller (RSI p.135)
    "PT1": "Float32",             # 1st-order lag (RSI p.135)
    "PT2": "Float32",             # 2nd-order lag (RSI p.136)
    # Timer fires a rising-edge pulse after the configured time (RSI p.136).
    # A rising edge is a boolean event (0 -> 1 transition) -> Bool.
    "Timer": "Bool",              # rising-edge pulse after elapsed time (RSI p.136)

    # --- Mathematical operations (§12.2.3) ---
    # All math objects operate on KRL REAL values -> Float32.
    # CEIL/FLOOR return an integer-valued REAL; wire type is still REAL -> Float32.
    "ABS": "Float32",             # absolute value / Betragfunktion (RSI p.132)
    "ACOS": "Float32",            # arc cosine (RSI p.132)
    "ASIN": "Float32",            # arc sine (RSI p.132)
    "ATAN": "Float32",            # arc tangent (RSI p.133)
    "ATAN2": "Float32",           # two-argument arc tangent; quadrant from input signs (RSI p.133)
    "CEIL": "Float32",            # smallest integer >= input, returned as REAL (RSI p.133)
    "COS": "Float32",             # cosine (RSI p.133)
    "Division": "Float32",        # division; output: Quotient (RSI p.133)
    "EXP": "Float32",             # exponential function (RSI p.133)
    "FLOOR": "Float32",           # largest integer <= input, returned as REAL (RSI p.133)
    "Limit": "Float32",           # clamp to [LowerLimit, UpperLimit] (RSI p.133)
    "LOG": "Float32",             # logarithm function (RSI p.133)
    "MinMax": "Float32",          # min and max over all inputs; outputs: Min, Max (RSI p.133)
    "MULTI": "Float32",           # multiplication (RSI p.133)
    "NORM": "Float32",            # Euclidean norm of up to 10 inputs (RSI p.133)
    "POW": "Float32",             # power function (RSI p.133)
    "ROUND": "Float32",           # round to nearest integer, returned as REAL (RSI p.133)
    "SIN": "Float32",             # sine (RSI p.133)
    "SUB": "Float32",             # subtraction; output: Diff (RSI p.133)
    "SUM": "Float32",             # addition; output: Sum (RSI p.133)
    "SumUp": "Float32",           # running accumulator (math op, not in §12.2.3 table)
    "TAN": "Float32",             # tangent (RSI p.133)

    # --- Mathematical comparisons (§12.2.4) ---
    # Comparisons evaluate a condition and produce a boolean result -> Bool.
    "Equal": "Bool",              # equality comparison (RSI p.133)
    "Greater": "Bool",            # greater-than comparison (RSI p.133)
    "Less": "Bool",               # less-than comparison (RSI p.133)

    # --- Logical operations (§12.2.2) ---
    # Boolean logic (AND/OR/NOT/XOR) operates on and produces boolean values -> Bool.
    "AND": "Bool",                # logical AND, up to 10 inputs (RSI p.132)
    "NOT": "Bool",                # logical NOT (RSI p.132)
    "OR": "Bool",                 # logical OR, up to 10 inputs (RSI p.132)
    "XOR": "Bool",                # exclusive OR, up to 10 inputs (RSI p.132)
    # Bitwise operations work on integer bit patterns -> Int32.
    "BAND": "Int32",              # bitwise AND with optional constant operand (RSI p.132)
    "BCOMPL": "Int32",            # bitwise complement (RSI p.132)
    "BOR": "Int32",               # bitwise OR with optional constant operand (RSI p.132)

    # --- Flip-flops (§12.2.2) ---
    # Flip-flop outputs a boolean state (set/reset latch) -> Bool.
    "RS_FlipFlop": "Bool",        # RS flip-flop, reset-dominant (RSI p.132)
    "SR_FlipFlop": "Bool",        # SR flip-flop, set-dominant (RSI p.132)

    # --- Signal routing (§12.2.2) ---
    # SignalSwitch routes one of two analog signal paths to its outputs -> Float32.
    "SignalSwitch": "Float32",    # switch between 2 signal paths via control signal (RSI p.132)

    # --- Coordinate transformation (§12.2.5) ---
    # Transform a 3-component vector (In1..3) between reference frames -> Float32.
    # Out1/Out2/Out3 are the transformed vector components in mm or deg.
    "Trafo_RobFrame": "Float32",  # transform vector between robot reference frames (RSI p.134)
    "Trafo_UserFrame": "Float32", # transform vector into user frame with offset/rotation (RSI p.134)

    # --- Correction monitoring (§12.2.7) ---
    # Outputs the current cumulative correction per axis/direction -> Float32.
    # AxisCorrMon: Begrenzung achsspezifische Gesamtkorrektur; A1..A6 in deg, E1..E6 in mm.
    "AxisCorrMon": "Float32",     # cumulative axis correction A1..A6 + E1..E6 (RSI p.134)
    # PosCorrMon: Begrenzung kartesische Gesamtkorrektur; X/Y/Z in mm, A/B/C in deg.
    "PosCorrMon": "Float32",      # cumulative Cartesian correction X/Y/Z + A/B/C (RSI p.134)

    # --- Motion correction (§12.2.6) ---
    # Correction channels carry the applied correction value -> Float32
    # (axis corrections in deg for rotational / mm for linear axes; Cartesian in mm/deg).
    # The Stat output datatype is not documented; see the note below this dict.
    "AxisCorr": {"A1": "Float32", "A2": "Float32", "A3": "Float32",
                 "A4": "Float32", "A5": "Float32", "A6": "Float32"},
    "AxisCorrExt": {"E1": "Float32", "E2": "Float32", "E3": "Float32",
                    "E4": "Float32", "E5": "Float32", "E6": "Float32"},
    "PosCorr": {"X": "Float32", "Y": "Float32", "Z": "Float32",
                "A": "Float32", "B": "Float32", "C": "Float32"},

    # --- Robot data reader (§12.2.8) ---
    "GearTorque": "Float32",      # gear torque A1..A6, Nm (RSI p.134)
    "GearTorqueExt": "Float32",   # gear torque ext axes E1..E6, Nm (RSI p.134)
    # E6POS status/turn: INT in KRL (not in the provided manuals) -> assume Int32.
    "PosAct": {"S": "Int32", "T": "Int32"},
}

# ---- Channels with unknown datatype (not documented in KST RSI 5.0) ----
#
# AxisCorr / AxisCorrExt (§12.2.6 p.134): "Achsweise Korrekturaufschaltung mit
#   Begrenzung". Applies a sensor-driven correction to robot axes A1..A6 (or
#   external axes E1..E6). Stat output: status of the correction block; likely
#   a limit/error flag, but could be Bool (limit exceeded) or Int32 (error code).
#   Not documented in the RSI manual.
#
# PosCorr (§12.2.6 p.134): "Kartesische Korrekturaufschaltung mit Begrenzung".
#   Applies a Cartesian sensor correction (X/Y/Z in mm, A/B/C in deg).
#   Stat output: same uncertainty as AxisCorr.Stat above.
#
# Status (§12.2.8 p.134): "Liefert Statusinformationen der Robotersteuerung,
#   z. B. aktueller Status von Submit- oder Roboter-Interpreter, aktuelle
#   Betriebsart etc." Single Stat output; content is robot controller status
#   information (interpreter state, operating mode, etc.). Likely Int32
#   (integer-encoded status flags / bitmask), but the exact encoding and wire
#   type are not described in the RSI manual.
#
# Ethernet (§12.2.1 p.131): UDP/XML data exchange with an external sensor
#   system. Up to 64 outputs (Out1..Out64); types are user-defined in the XML
#   configuration file. No static type can be assumed.

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


def _channel_datatype(spec, output_name: str):
    """
    Resolve a bib datatype spec for one output channel.

    `spec` is a single datatype string (applies to every output), a per-channel
    dict {output name: datatype}, or None. Returns the datatype or None.
    """
    if isinstance(spec, dict):
        return spec.get(output_name)
    return spec


def _output_typing(blueprint: Blueprint, output_name: str) -> tuple:
    """
    Return (is_enum, types, assumed) for ONE named output of a blueprint.

      * Enumeration  -> types = all DataType enum values,   assumed = None
      * Fixed (doc)  -> types = [documented datatype],      assumed = None
      * Fixed (guess)-> types = [] (unknown),               assumed = guess
      * Unknown      -> types = [] (unknown),               assumed = None

    Only a "DataType" enum types the output; other enums (Type, Source,
    FilterType, ...) are configuration, not datatype. Outputs are typed one at a
    time so a multi-output object can carry different datatypes per channel.
    """
    if "DataType" in blueprint.enum_param_index_by_name:
        param_index = blueprint.enum_param_index_by_name["DataType"]
        return True, list(blueprint.enum_params[param_index]), None

    fixed = FIXED_DATATYPE_BIB.get(blueprint.name)
    if fixed is not None:
        documented = _channel_datatype(fixed, output_name)
        if documented is not None:
            return False, [documented], None

    assumed = _channel_datatype(ASSUMED_DATATYPE_BIB.get(blueprint.name), output_name)
    return False, [], assumed


def build_catalog(blueprint_path: str) -> dict:
    """
    Return {ObjType: {"id": ObjTypeId, "outputs": [output, ...]}}, sorted by
    ObjType, for every object type that has output channels.

    Each output is {"index", "name", "enum", "types", "assumed"} and is typed
    independently, so an object's channels may differ (e.g. PosAct).
    """
    blueprints = load_blueprints(blueprint_path)

    catalog = {}
    for blueprint in blueprints.values():
        if not blueprint.outputs:
            continue  # pure sinks (Map2DigOut, Monitor, Output, ...) have none

        outputs = []
        for idx in sorted(blueprint.outputs):
            name = blueprint.outputs[idx]
            is_enum, types, assumed = _output_typing(blueprint, name)
            outputs.append({
                "index": idx,
                "name": name,
                "enum": is_enum,
                "types": types,
                "assumed": assumed,
            })
        catalog[blueprint.name] = {"id": blueprint.element_id, "outputs": outputs}

    return dict(sorted(catalog.items(), key=lambda kv: kv[0].lower()))


def build_unknown_template(catalog: dict) -> dict:
    """
    Return a fill-in template of the output channels whose datatype is still
    unknown (not in FIXED_DATATYPE_BIB or ASSUMED_DATATYPE_BIB).

        { ObjType: { "datatype": "", "outputs": [unknown output name, ...] } }

    Only the unresolved channels are listed, so a partially-typed object (some
    channels known, some not) shows just what is missing.

    Workflow: look up the missing ObjType, then add it to one of the bibs above
    (ASSUMED_DATATYPE_BIB: "Name": "Float32"; FIXED_DATATYPE_BIB: "Name": "Float32").
    For an object whose channels differ, use a per-channel
    dict, e.g. {"X": "Float32", "S": "Int32"}. Re-running this script regenerates
    the template, so resolved channels drop out automatically.
    """
    template = {}
    for obj_type, entry in catalog.items():
        unknown = [o["name"] for o in entry["outputs"]
                   if not o["enum"] and not o["types"] and o["assumed"] is None]
        if unknown:
            template[obj_type] = {"datatype": "", "outputs": unknown}
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
