# KUKA RSI Ethernet datatype mapping

Resolve, for every named variable on the KUKA RSI Ethernet channel, **what
datatype actually flows into that channel**. The final result is a JSON mapping
of `{ variable name: datatype }`.

## Pipeline

Two scripts, run in order (standard library only — no pandas/openpyxl):

```bash
python rsi_block_mapping.py   # RSI_Ethernet.rsix      -> rsi_block_mapping.json   (the bib)
                              #                        -> rsi_block_unknown.json   (what's still missing)
python rsi_mapping.py         # nebumind_*.* + the bib -> rsi_datatype_map.json    (the result)
```

```
RSI_Ethernet.rsix ──rsi_block_mapping.py──▶ rsi_block_mapping.json   (object/output datatype bib)
                                          └▶ rsi_block_unknown.json   (fill-in template of unknowns)
nebumind_rsi.rsix  ┐
nebumind_conf.xml  ┴──────rsi_mapping.py───▶ rsi_datatype_map.json    { variable: datatype }
```

## Files

| File | Role |
| --- | --- |
| `nebumind_conf.xml` | Socket configuration. Each `<ELEMENT>` gives a variable name (`TAG`) and a 1-based channel index (`INDX`). The human-readable name of every Ethernet channel. |
| `nebumind_rsi.rsix` | The concrete RSI wiring: `<RsiObjects>` with the `Ethernet` block's inputs and every object feeding it. |
| `RSI_Ethernet.rsix` | The generic KUKA library — `<BlueprintCollections>` for every possible object. Source for the bib. |
| `rsi_block_mapping.py` | Stage 1. Builds the object/output datatype bib + the unknown template. Holds the `FIXED_DATATYPE_BIB` / `ASSUMED_DATATYPE_BIB`. |
| `rsi_block_mapping.json` | Generated bib: per `ObjType`, the datatype(s) of each output. |
| `rsi_block_unknown.json` | Generated template: object types whose datatype is still unknown. |
| `rsi_mapping.py` | Stage 2. Walks the wiring and resolves each variable via the bib. |
| `rsi_datatype_map.json` | Generated result: `{ variable name: datatype }`. |
| `KST_RSI_50_de.pdf` | KUKA RSI 5.0 manual — object↔`$variable` map (p.131-136), `INDX`/wire-type rules. |
| `KUKA System Variables 8.1 8.2 8.3.pdf` | System-variable datatypes (`$SEN_PINT`, `$CURR_ACT`, …). |

## The reference chain (key → key)

`rsi_mapping.py` walks this per Ethernet channel:

```
nebumind_conf.xml         <ELEMENT TAG="Peening" INDX="1">
   │  TAG  = variable name
   │  INDX = 1-based channel number
   ▼  InIdx = INDX − 1                          ◀── off-by-one (see notes)
nebumind_rsi.rsix / RsiObjects / Ethernet
        <Input InIdx="0" OutObjId="DigIn_1" OutIdx="0"/>
   │  OutObjId → which source object   ;   OutIdx → which output of it
   ▼
nebumind_rsi.rsix / RsiObjects
        <RsiObject ObjId="DigIn_1" ObjType="DigIn">
            <Parameter Name="DataType" ParamValue="0"/>   ← selected enum index (if any)
   │  ObjType → key into the bib   ;   ParamValue → which enum value
   ▼
rsi_block_mapping.json
        "DigIn": { "outputs": [ { "index": 0, "enum": true,
                                  "types": ["Bit","Byte", … ,"Float"] } ] }
                                            ▲ ParamValue (0) indexes types → "Bit"
```

### The two branches at the source object

Looked up in the bib by `ObjType`, then by the output's `index` (= `OutIdx`):

- **`enum: true`** → take the *selected* value: the instance's `DataType`
  `ParamValue` indexes the bib's `types` list (e.g. `0 → Bit`).
- **`enum: false`** → take the fixed datatype: the single documented `types`
  entry, or `assume <type>` for an engineering guess, or `unknown`.
- **Multiple outputs** (e.g. `MotorCurrent`): `OutIdx` selects the output by its
  `index` (`0→A1 … 5→A6`). Each output is typed independently, so an object's
  channels can carry **different** datatypes (e.g. `PosAct`).

## Datatypes: where they come from

The `.rsix` `<Output>` tags carry **no** datatype attribute. There are three cases:

1. **Enum** (`DigIn`, `DigOut`, `Input`) — selectable via a `DataType` enumeration;
   the selected value is read from the instance.
2. **Documented fixed** (`FIXED_DATATYPE_BIB`) — the object reads a KUKA system
   variable whose Value type is documented. Page numbers are cited inline in
   `rsi_block_mapping.py`; `SysVars` = the System Variables manual (KSS
   8.1/8.2/8.3), `RSI` = `KST_RSI_50_de.pdf`.

   | ObjType | System variable (Value type) | Datatype | Ref |
   | --- | --- | --- | --- |
   | `Sen_PInt` | `$SEN_PINT` INT | **Int32** | SysVars p.84 / RSI p.132 |
   | `Sen_PRea` | `$SEN_PREA` REAL | **Float32** | SysVars p.84 / RSI p.132 |
   | `MotorCurrent` / `MotorCurrentExt` | `$CURR_ACT` REAL (% of max) | **Float32** | SysVars p.32 / RSI p.134 |
   | `AnIn` / `AnOut` | `$ANIN` / `$ANOUT` REAL (voltage) | **Float32** | SysVars p.19 / RSI p.131 |
   | `OV_PRO` | `$OV_PRO` INT (% 0..100) | **Int32** | SysVars p.65 / RSI p.134 |
   | `AxisAct` / `AxisActExt` | `$AXIS_ACT` E6AXIS, angles/pos REAL | **Float32** | SysVars p.24 / RSI p.134 |
   | `PosAct` (X,Y,Z,A,B,C) | `$POS_ACT` E6POS, mm/deg REAL | **Float32** | SysVars p.68 / RSI p.134 |

3. **Assumed fixed** (`ASSUMED_DATATYPE_BIB`) — emitted as `assume <type>`: the
   RSI signal-processing/generator objects (`Constant`, `Cosine`, `Sine`,
   `Rectangle`, `Sawtooth`, `Triangle`, `Source`, `D`, `Delay`, `GenCtrl`, `I`,
   `IIRFilter`, `P`, `PD`, `PID`, `PT1`, `PT2`; RSI p.135-136) output a continuous
   REAL signal, plus `GearTorque`/`GearTorqueExt` (torque in Nm; RSI p.134), plus
   `PosAct` channels `S`,`T` (E6POS status/turn — INT in KRL but not in these
   manuals → `assume Int32`).

Everything else stays **`unknown`** and is listed in `rsi_block_unknown.json`.

### Per-channel datatypes

An object's outputs are typed **independently**, so a bib value may be either a
single datatype (every output) or a per-channel dict `{output name: datatype}`.
`PosAct` is the example — its `E6POS` outputs mix REAL and INT:

```python
FIXED_DATATYPE_BIB   = { "PosAct": ({"X": "Float32", … , "C": "Float32"}, "$POS_ACT") }
ASSUMED_DATATYPE_BIB = { "PosAct": {"S": "Int32", "T": "Int32"} }
```

A channel not covered by a dict falls through (documented → assumed → unknown),
so the two bibs can split one object's channels between them.

> KRL `REAL` is IEEE-754 **single precision** (Float32); KRL `INT` is **signed**
> 32-bit — KRL has no unsigned/64-bit scalar. (The Ethernet wire `TYPE` widens
> these to `LONG`/`DOUBLE`, but the source value is Int32/Float32.) Assumptions
> are emitted as `assume <type>` so they are never mistaken for documented facts.

## Extending the bib (the unknown template)

`rsi_block_unknown.json` lists the output channels still without a datatype (only
the unresolved channels appear, so a partially-typed object shows just what's
missing):

```json
{
  "ABS": { "datatype": "", "outputs": ["Out1"] },
  "SUM": { "datatype": "", "outputs": ["Sum"] }
}
```

To resolve one, add it to the right bib in `rsi_block_mapping.py`:

- known datatype → `FIXED_DATATYPE_BIB`: `"ABS": ("Float32", "$SOURCE"),`
- educated guess → `ASSUMED_DATATYPE_BIB`: `"ABS": "Float32",`
- channels differ → per-channel dict: `"Obj": ({"X": "Float32", "S": "Int32"}, "$SRC")`

Re-run `python rsi_block_mapping.py`: the bib updates and the resolved channels
**drop out** of `rsi_block_unknown.json` automatically (the template is rebuilt
from the current bibs each run). Currently 45 of 77 output-bearing types still
have at least one unknown channel.

## Notes / gotchas

1. **Off-by-one INDX ↔ InIdx.** Config `INDX` is 1-based (KUKA channel number);
   the Ethernet `InIdx` is 0-based, so `InIdx = INDX − 1`. Validated by the
   result: every enum-resolved `Bit` lands on a `BOOL` config row, `Sen_PInt` on
   `LONG`, `Sen_PRea` on `DOUBLE`. Exposed as `INDX_TO_INIDX_OFFSET`.
2. **XML namespaces.** Each section declares a *default* namespace, so
   ElementTree sees `{RsiDriver}RsiObject` and `{RsiBlueprint}Element`.
3. **Run order.** `rsi_mapping.py` reads `rsi_block_mapping.json`; if it's
   missing it raises a clear error telling you to run `rsi_block_mapping.py`.
4. **File locks.** Each JSON is written independently and skipped with a message
   if it's open (e.g. in an editor), so one locked file never aborts the run.

## File formats

**`rsi_block_mapping.json`** — one entry per `ObjType` (sinks like `Map2DigOut`,
`Monitor`, `Output` are excluded as they have no outputs):

```json
{
  "DigIn": { "id": 29, "outputs": [
      { "index": 0, "name": "Out1", "enum": true,
        "types": ["Bit","Byte","UByte","Int16","UInt16","Int32","UInt32","Float"],
        "assumed": null } ] },
  "MotorCurrent": { "id": 47, "outputs": [
      { "index": 0, "name": "A1", "enum": false, "types": ["Float32"], "assumed": null } ] },
  "Constant": { "id": 94, "outputs": [
      { "index": 0, "name": "Out1", "enum": false, "types": [], "assumed": "Float32" } ] },
  "PosAct": { "id": 24, "outputs": [
      { "index": 0, "name": "X", "enum": false, "types": ["Float32"], "assumed": null },
      { "index": 6, "name": "S", "enum": false, "types": [], "assumed": "Int32" } ] }
}
```

`types` holds all enum values (enum), the single documented fixed type, or `[]`
when unknown; `assumed` carries an engineering guess (else `null`). Channels of
one object can differ — see `PosAct` above (`X` documented REAL, `S` assumed INT).

**`rsi_datatype_map.json`** — the result:

```json
{
  "Peening": "Bit",
  "ActualFlowrate": "assume Float32",
  "Bauteiltyp": "Int32",
  "MotorCurrentA1": "Float32"
}
```

## Code structure

`rsi_block_mapping.py` (stage 1):

- `load_blueprints` — `RSI_Ethernet.rsix → {Id: Blueprint}`
- `_output_typing` — `Blueprint → (is_enum, types, assumed)` (consults the bibs)
- `build_catalog` — `→ {ObjType: {id, outputs}}`
- `build_unknown_template` — `→ {ObjType: {datatype, outputs}}` for unknowns
- `write_json` — serialize any dict

`rsi_mapping.py` (stage 2):

- `parse_ethernet_inputs` — `InIdx → (OutObjId, OutIdx)`
- `index_rsi_objects` — `ObjId → RsiObjectInstance` (with selected enum index)
- `parse_conf_variables` — `INDX → ConfElement`
- `load_block_mapping` / `find_output` / `resolve_output_datatype` — bib lookup
- `build_mapping` — ties it together; `write_json` / `print_mapping` — outputs
