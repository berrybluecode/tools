# `strip_dtsx_layout.py` — Logic-Preserving SSIS DTSX Stripper

A conservative pre-processor that removes **only cosmetic, binary, and boilerplate** content
from SSIS `.dtsx` files. The output is still valid XML and contains every piece of
control-flow / data-flow logic from the original — just without the visual layout, compiled
binaries, MSBuild scaffolding, and pure-provenance metadata.

The goal: feed an LLM-based SSIS-to-Spark / SSIS-to-Lakehouse converter a much leaner
`.dtsx` with **zero risk** of dropping translation-relevant information.

---

## Why this exists

Real-world `.dtsx` packages are typically 30–80% bloat:

- Designer layout XML (CDATA blob describing icon coordinates and connector positions).
- Compiled Script Task DLLs base64-encoded inside `<BinaryItem>`.
- MSBuild scaffolding (`*.vbproj`, `*.csproj`, `Project`, `AssemblyInfo.cs`, `Resources.Designer.cs`, …) that ships alongside the actual `ScriptMain.cs` source.
- Microsoft attribution attributes (`DTS:TaskContact="Execute SQL Task; Microsoft Corporation; SQL Server 2019; © …"`).
- Per-variable debug-dump flags (`DTS:IncludeInDebugDump`).
- Package-level provenance (`CreationDate`, `CreatorName`, `VersionGUID`, …).
- For wide-table data flows, `<externalMetadataColumn>` validation snapshots that duplicate the column metadata already carried on `<inputColumn>` / `<outputColumn>`.

None of that is needed by an LLM converter. Removing it makes the input fit a smaller
context window and lets the model focus on the actual ETL logic.

---

## Quick start

```bash
# Default mode — fully safe, no opt-in flags needed
python3 strip_dtsx_layout.py path/to/dtsx_input \
    --output-dir path/to/dtsx_stripped \
    --suffix _stripped

# Maximum safe reduction — adds opt-in cleanups (still logic-preserving)
python3 strip_dtsx_layout.py path/to/dtsx_input \
    --output-dir path/to/dtsx_stripped \
    --suffix _stripped \
    --strip-empty-placeholders \
    --strip-external-metadata
```

`input` can be a single `.dtsx` file or a directory; the directory tree is mirrored under
`--output-dir` so files with duplicate stems (very common in SSIS estates) never collide.

---

## CLI reference

```
python3 strip_dtsx_layout.py <input> --output-dir <dir> [options]
```

**Required**

| Argument | Description |
| --- | --- |
| `input` | A `.dtsx` file or a directory; subdirectories are walked recursively. |
| `--output-dir` / `-o` | Directory to write stripped files into. The input directory tree is mirrored beneath it. |

**General options**

| Flag | Default | Effect |
| --- | --- | --- |
| `--suffix <str>` | empty | Suffix appended to each output file's stem, e.g. `_stripped`. |
| `--quiet` / `-q` | off | Print only failures and the final summary. |

**Opt-out flags (every default removal can be turned off individually)**

| Flag | Disables removal of |
| --- | --- |
| `--keep-build-files` | MSBuild / VS scaffolding `<ProjectItem>` files |
| `--keep-task-contact-and-debug-dump` | `DTS:TaskContact` and `DTS:IncludeInDebugDump` attributes |
| `--keep-metadata` | Package-level provenance attrs (CreationDate, CreatorName, VersionGUID, LastModifiedProductVersion, …) and `<DTS:Property DTS:Name="PackageFormatVersion">` |

**Opt-in flags (off by default — they change the data flow XML shape, but no logic)**

| Flag | What it removes | Best for |
| --- | --- | --- |
| `--strip-empty-placeholders` | Empty `<DTS:Variables />` and default-only `<DTS:LoggingOptions DTS:FilterKind="0" />` placeholders nested *inside* per-task executables | Packages with many small Script / Execute SQL tasks |
| `--strip-external-metadata` | `<externalMetadataColumn>` design-time validation snapshots; the column info is preserved on `<inputColumn>` / `<outputColumn>` | Wide-table data flow packages (column metadata can dominate the file) |

---

## What gets removed (and why it's safe)

### Default removals (always on)

1. **Layout & binaries**

    | Element | Why safe |
    | --- | --- |
    | `<DTS:DesignTimeProperties>` | CDATA blob with designer coordinates. SSIS runtime ignores it. |
    | `<GraphLayout>` | Data-flow visual layout. Runtime ignores it. |
    | `<BinaryItem>` | Compiled Script Task DLL/PDB base64. The actual source file `<ProjectItem Name="ScriptMain.*">` is kept. |
    | `<DTS:Property DTS:Name="LayoutInfo">` | Layout-only property variant. |
    | `<property name="LayoutInfo">` | Pipeline-component layout byte array. |

2. **Script-task build scaffolding** (auto-generated VS files; the real source is `ScriptMain.cs/.vb`)

    | Pattern | What it is |
    | --- | --- |
    | `*.vbproj`, `*.csproj`, `*.proj` | MSBuild project files |
    | `Project` (exact) | CodeProjectML build descriptor |
    | `*AssemblyInfo.cs`, `*AssemblyInfo.vb` | Auto-generated assembly metadata |
    | `*Settings.Designer.cs/.vb` | VS-generated settings accessor |
    | `*Resources.Designer.cs/.vb` | VS-generated resource accessor |
    | `Resources.resx`, `Settings.settings` | VS scaffolding |
    | `app.config` | .NET assembly-binding config |

3. **Pure-metadata attributes** (no runtime effect)

    - `DTS:TaskContact` — Microsoft attribution text.
    - `DTS:IncludeInDebugDump` — debug-dump flag on variables.

4. **Package-level provenance** (only on the root `<DTS:Executable>`)

    - `DTS:CreationDate`, `DTS:CreatorName`, `DTS:CreatorComputerName`
    - `DTS:VersionBuild`, `DTS:VersionGUID`, `DTS:VersionComments`, `DTS:VersionMajor`, `DTS:VersionMinor`
    - `DTS:LastModifiedProductVersion`, `DTS:ProductName`
    - `<DTS:Property DTS:Name="PackageFormatVersion">`

### Opt-in removals

- **`--strip-empty-placeholders`** — Empty `<DTS:Variables/>` and default `<DTS:LoggingOptions DTS:FilterKind="0"/>` nested inside *per-task* executables (the package-level blocks are always kept). These are designer-emitted shells with no runtime effect.

- **`--strip-external-metadata`** — `<externalMetadataColumn>` elements (and their wrapper `<externalMetadataColumns>` once empty). For each removed element, the column `name` / `dataType` / `length` is already carried on the corresponding `<inputColumn>` (via `cachedName` / `cachedDataType` / `cachedLength`) and `<outputColumn>` (via `name` / `dataType` / `length`). The runtime data flow graph (`outputColumn → path → inputColumn`) is fully intact. SSIS Designer would mark the package as "needs re-validation" because validation is what `externalMetadataColumn` exists for, but the runtime logic and translation-relevant info are unchanged.

---

## What is **always** preserved

| Category | Elements / attributes |
| --- | --- |
| Control flow | Every `<DTS:Executable>` (incl. nested), `<DTS:EventHandler>`, `<DTS:PrecedenceConstraint>` with full attribute set (`EvalOp`, `LogicalAnd`, `Expression`, `Value`, `From`, `To`) |
| Variables & expressions | All `<DTS:Variable>`, `<DTS:PropertyExpression>`, expression text |
| Connections | All `<DTS:ConnectionManager>` (incl. nested `<DTS:ObjectData><DTS:ConnectionManager …>` with full connection strings) |
| Configuration & logging | `<DTS:Configurations>`, `<DTS:Configuration>`, `<DTS:LogProviders>`, `<DTS:LogProvider>`, `<DTS:LoggingOptions>` (root-level), `<DTS:SelectedLogProvider>` |
| Data flow | `<pipeline>`, `<components>`, `<component>`, `<inputs>`, `<outputs>`, `<inputColumn>`, `<outputColumn>`, `<paths>`, `<path>`, `<connections>` |
| Flat files | `<FlatFileColumns>`, `<FlatFileColumn>` |
| SQL tasks | `<SQLTask:SqlTaskData>`, `<SQLTask:ParameterBinding>` |
| Execute Process / Execute Package | `<ExecuteProcessData>`, `<ExecutePackageTask>` |
| Script Task source | All `<ProjectItem>` files **not matching the build-scaffolding patterns** (i.e. `ScriptMain.cs` / `ScriptMain.vb` / any user-named code file) |
| References | `DTS:DTSID`, `DTS:refId`, `lineageId`, `externalMetadataColumnId`, `connectionManagerID`, `SQLTask:Connection` |
| Runtime attrs | `DTS:LocaleID`, `DTS:DelayValidation`, `DTS:ProtectionLevel`, `DTS:EnableConfig`, `DTS:Disabled` |
| Annotations & comments | `<DTS:Annotation>`, all XML `<!-- … -->` comments |

---

## How to verify nothing was lost

Run a quick sanity diff against any stripped file:

```bash
python3 - <<'PY'
import xml.etree.ElementTree as ET
NS, SQL = 'www.microsoft.com/SqlServer/Dts', 'www.microsoft.com/sqlserver/dts/tasks/sqltask'

def stats(path):
    r = ET.parse(path).getroot()
    return dict(
        executable           = len(list(r.iter(f'{{{NS}}}Executable'))),
        precedence           = len(list(r.iter(f'{{{NS}}}PrecedenceConstraint'))),
        variable             = len(list(r.iter(f'{{{NS}}}Variable'))),
        connection_manager   = len(list(r.iter(f'{{{NS}}}ConnectionManager'))),
        property_expression  = len(list(r.iter(f'{{{NS}}}PropertyExpression'))),
        sql_task_data        = len(list(r.iter(f'{{{SQL}}}SqlTaskData'))),
        pipeline             = len(list(r.iter('pipeline'))),
        component            = len(list(r.iter('component'))),
        input_column         = len(list(r.iter('inputColumn'))),
        output_column        = len(list(r.iter('outputColumn'))),
        path_                = len(list(r.iter('path'))),
        flat_file_column     = len(list(r.iter('FlatFileColumn'))),
    )

orig     = stats('path/to/original.dtsx')
stripped = stats('path/to/stripped.dtsx')

for k in orig:
    flag = '' if orig[k] == stripped[k] else '  <- WARN'
    print(f'{k:<22} {orig[k]:>6} -> {stripped[k]:>6}{flag}')
PY
```

Every count should match.

For precedence-constraint *attributes* specifically:

```python
for pc in root.iter(f'{{{NS}}}PrecedenceConstraint'):
    print({k.split('}')[-1]: v for k, v in pc.attrib.items()})
```

`EvalOp`, `LogicalAnd`, `Value`, `Expression`, `From`, `To` should all still be present where they were in the original.

---

## Design notes

### Whitelist, not blacklist

The script removes a **closed set of explicit elements / attributes / `<ProjectItem>` patterns**.
Anything not on those lists is kept verbatim. New SSIS schema constructs (e.g. from a future
SSIS version) will pass through untouched rather than be silently dropped.

### Conservative defaults; aggressive removals are opt-in

The defaults are everything we have **strong, structural guarantees** about being non-runtime
content:

- Layout / binary blobs — defined by Microsoft as visual-only or compiled outputs.
- Build scaffolding — defined by Visual Studio as auto-generated.
- Provenance metadata — defined by SSIS schema as informational.

Two more things are **opt-in** because they change the *shape* of the data flow XML (even
though they don't change runtime behaviour):

- `--strip-empty-placeholders`
- `--strip-external-metadata`

You can run with both opt-ins on by default if you trust them for your use case; the README
example above shows that pattern.

### Single-pass tree walk with deferred removal

`strip_dtsx()` builds a `parent_map` once (`{child: parent}` for every node), then iterates
the tree, queueing `(parent, child, key)` tuples for any element matching a removal rule.
The actual removals happen after the iteration is complete to avoid mutating the tree
during traversal. The `parent_map` is also used to safely sweep up empty
`<externalMetadataColumns>` wrappers after their children have been removed.

### Output safety

- The input directory tree is **mirrored** under `--output-dir`. SSIS estates routinely
  reuse package names across solutions (`Master.dtsx`, `Load_Dim.dtsx` …); a flat output
  directory would silently overwrite collisions.
- `*.dtsx` and `*.DTSX` are both collected (case-sensitive on Linux otherwise).
- Per-file errors are caught broadly and logged; the batch never aborts because of one bad
  file. A summary at the end shows succeeded vs. failed file counts and the failure reasons.
- If any file fails the script exits with code `2`; otherwise `0`.

### XML round-trip

Uses `xml.etree.ElementTree` (stdlib only, no extra dependencies):

- Comments are preserved via `XMLParser(target=TreeBuilder(insert_comments=True))`.
- The XML declaration is kept (`tree.write(..., xml_declaration=True)`).
- CDATA sections that survive (rare — the cosmetic ones are stripped) are written back
  as escaped text. **This is XML-semantically identical**: any compliant parser, including
  SSIS, treats `<![CDATA[a < b]]>` and `a &lt; b` as the same content.
- The output encoding is `utf-8`; if the input was UTF-16 the file is converted.
  SSIS handles UTF-8 transparently.

### Memory & performance

`ET.parse()` loads the whole DTSX into memory. Typical real-world packages run from tens
of KB to ~30 MB; processing is sub-second per file even for the larger ones. There is no
parallelism — the bottleneck is XML parsing, which is CPU-bound and small. For very large
estates you can wrap this in `xargs -P` or a `ProcessPoolExecutor`.

### Security note

`xml.etree.ElementTree` is documented as not safe against maliciously constructed XML
(XXE / billion-laughs). For untrusted DTSX inputs, swap the import for `defusedxml.ElementTree`.
For trusted internal SSIS estates (the typical case here), the stdlib is fine.

---

## Real-world results

Measured on representative SSIS packages:

| Package shape | Original | Default | + opt-ins |
| --- | ---: | ---: | ---: |
| 1 large wide-table data flow package (53 data flows, 10 K+ columns each side) | 28.85 MB | 20.97 MB (-27.3%) | **16.46 MB (-43.0%)** |
| 10 small Control + EXEC pairs (mostly Script Tasks and Execute SQL Tasks) | 0.85 MB | 0.37 MB (-56.6%) | 0.36 MB (-57.6%) |

In every case the per-element diff (Executable / PrecedenceConstraint / Variable /
ConnectionManager / pipeline / component / inputColumn / outputColumn / path / FlatFileColumn /
SQL task / source `ProjectItem` / precedence-constraint attributes) was identical between
the original and the stripped output.

---

## Integration with `preprocess_dtsx.py`

`strip_dtsx_layout.py` produces **valid `.dtsx`** as output, so you can chain it directly
into the existing extractor:

```bash
# Step 1: strip layout / binary / boilerplate
python3 strip_dtsx_layout.py raw_dtsx \
    --output-dir stripped_dtsx \
    --strip-empty-placeholders --strip-external-metadata

# Step 2: extract LLM-friendly summary from the leaner .dtsx
python3 preprocess_dtsx.py stripped_dtsx \
    --output-dir extracted --format both --compact
```

You get a smaller, fidelity-preserving DTSX before the lossy summary stage.

---

## Known caveats

- The output is a **logically-equivalent** `.dtsx` for runtime/translation purposes, but is
  not bit-for-bit identical to the input. Whitespace, attribute order, namespace prefix
  declarations, and CDATA-vs-escaped-text encoding may differ. SSIS designer may mark a
  reopened package as "modified". This is expected.
- `--strip-external-metadata` leaves dangling `externalMetadataColumnId` references on
  `<inputColumn>` / `<outputColumn>`. LLM converters don't follow them, but the package
  will fail SSIS Designer validation until you regenerate metadata. Do **not** use this
  flag if you intend to round-trip the file back into SSIS Designer.
- The script does **not** redact secrets in connection strings or variable values. If you
  intend to ship the output to an external service, run a redaction pass first.
