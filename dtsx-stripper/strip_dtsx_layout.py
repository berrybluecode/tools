#!/usr/bin/env python3
"""
SSIS DTSX Layout / Binary / Boilerplate Stripper (lossless for runtime logic)

Removes ONLY content that is cosmetic, binary, debug-only, or pure provenance.
All control-flow and data-flow logic is preserved verbatim.

Default removals (always on, all logic-safe):

  Layout & binary
    * <DTS:DesignTimeProperties>           designer layout XML (CDATA blob)
    * <GraphLayout>                        data-flow visual layout XML
    * <BinaryItem>                         compiled Script Task DLL/PDB (base64)
    * <DTS:Property DTS:Name="LayoutInfo">  layout-only property variant
    * <property name="LayoutInfo">         pipeline component layout byte array

  Script-task build scaffolding (the runnable source - ScriptMain.cs/.vb - is kept):
    * <ProjectItem Name="*.vbproj"> / "*.csproj" / "*.proj"   MSBuild project file
    * <ProjectItem Name="Project">                            CodeProjectML descriptor
    * <ProjectItem Name="*AssemblyInfo.cs/.vb">               auto-generated assembly metadata
    * <ProjectItem Name="*Settings.Designer.cs/.vb">          auto-generated VS settings
    * <ProjectItem Name="*Resources.Designer.cs/.vb">         auto-generated VS resource accessor
    * <ProjectItem Name="Resources.resx" / "Settings.settings"> generated VS scaffolding
    * <ProjectItem Name="app.config">                         generated config

  Pure-noise attributes (no runtime effect, no translation value):
    * DTS:IncludeInDebugDump               per-variable debug-dump bitmask

  Package-level provenance (only on the root <DTS:Executable>):
    * DTS:CreatorName, DTS:CreatorComputerName       (PII-flavoured: usernames / hostnames)
    * DTS:VersionBuild, DTS:VersionGUID              (internal counters / stamps)
    * DTS:VersionMajor, DTS:VersionMinor             (internal counters)
    * DTS:ProductName                                (Microsoft product stamp)
    * <DTS:Property DTS:Name="PackageFormatVersion"> (SSIS XML schema version)

Kept by default because they may carry useful translation context:
    * DTS:TaskContact          (last field hints at original SSIS version per task)
    * DTS:CreationDate         (package age - useful for legacy detection)
    * DTS:VersionComments      (developer release notes, often empty but possible)
    * DTS:LastModifiedProductVersion (target SSIS version stamp)

  Strip these too with --strip-signal-metadata if you don't need the signal.

Optional removals (off by default):

  --strip-empty-placeholders
    * Empty <DTS:Variables /> nested inside per-task <DTS:Executable>
    * Empty <DTS:LoggingOptions DTS:FilterKind="0" /> nested inside per-task <DTS:Executable>
      (these inherit from package-level defaults and are designer-emitted shells)

  --strip-external-metadata   (large win on wide-table data flow packages)
    * <externalMetadataColumn>     design-time validation metadata
    * <externalMetadataColumns>    (the surrounding wrapper, if it ends up empty)
      The same column name / dataType / length is already on every <inputColumn>
      via cachedName/cachedDataType/cachedLength and on every <outputColumn>
      via name/dataType/length, so no information needed for translation is lost.
      Note: this does change the data flow XML shape - SSIS designer would mark
      the package as needing re-validation, but the runtime logic is intact.

  --strip-signal-metadata     (only if you don't need the legacy / version hints)
    * DTS:TaskContact, DTS:CreationDate, DTS:VersionComments,
      DTS:LastModifiedProductVersion
      These can carry useful translation context (see "Kept by default" above);
      strip them only if you've confirmed your converter doesn't need them.

Use --keep-* flags to opt out of any default removal.

Everything else is preserved verbatim, including:
  * Every <DTS:Executable>, <DTS:EventHandler>, <DTS:PrecedenceConstraint>
    with full attribute set (EvalOp, LogicalAnd, Expression, Value)
  * All <DTS:ConnectionManager>, <DTS:Variable>, <DTS:PropertyExpression>
  * All <pipeline> components, paths, inputs/outputs, inputColumn/outputColumn,
    derived-column expressions, lookups, sorts, aggregates
  * All Script Task source files (ScriptMain.cs / ScriptMain.vb)
  * All <SqlTaskData> SQL statements + ParameterBindings
  * All XML comments and DTS:Annotations
  * All DTSIDs, refIds, ConfigurationStrings, ProtectionLevels (referenced elsewhere)

Usage:
    python strip_dtsx_layout.py <input.dtsx>   --output-dir <dir>
    python strip_dtsx_layout.py <directory>    --output-dir <dir>
    python strip_dtsx_layout.py <directory>    -o out --suffix _stripped
    python strip_dtsx_layout.py <directory>    -o out --strip-empty-placeholders
    python strip_dtsx_layout.py <directory>    -o out --keep-task-contact

Notes:
    * The directory tree under <input> is mirrored under <output-dir> so files
      with duplicate stems (very common in SSIS estates) never collide.
    * Uses xml.etree.ElementTree for a stdlib-only round-trip. CDATA sections
      that survive are written back as escaped text - XML-semantically identical.
    * For untrusted DTSX, prefer running this through `defusedxml` first.
"""

import argparse
import fnmatch
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

DTS_NS = "www.microsoft.com/SqlServer/Dts"
SQL_NS = "www.microsoft.com/sqlserver/dts/tasks/sqltask"

ET.register_namespace("DTS", DTS_NS)
ET.register_namespace("SQLTask", SQL_NS)

DTS_TAG = lambda local: f"{{{DTS_NS}}}{local}"  # noqa: E731

LAYOUT_ELEMENT_TAGS = {
    DTS_TAG("DesignTimeProperties"),
    "DesignTimeProperties",
    "GraphLayout",
    "BinaryItem",
}
LAYOUT_PROPERTY_NAMES = {"LayoutInfo"}

BUILD_PROJECT_ITEM_NAMES_EXACT = {"Project", "app.config"}
# Patterns are matched against both the full Name and its basename (split on '\' and '/')
BUILD_PROJECT_ITEM_PATTERNS = (
    "*.vbproj",
    "*.csproj",
    "*.proj",
    "AssemblyInfo.cs",
    "AssemblyInfo.vb",
    "Settings.Designer.cs",
    "Settings.Designer.vb",
    "Resources.Designer.cs",
    "Resources.Designer.vb",
    "Resources.resx",
    "Settings.settings",
)

# Attributes stripped everywhere they appear.
# DTS:IncludeInDebugDump is a pure debug-only bitmask with no runtime effect.
DEBUG_ATTRS = {DTS_TAG("IncludeInDebugDump")}

# DTS:TaskContact is mostly Microsoft attribution boilerplate, BUT the third
# field of its semicolon-separated value reveals the SSIS product version each
# task type was originally authored against (e.g. "Microsoft SQL Server v9" =
# SSIS 2005). That can be useful legacy-detection context for an LLM converter,
# so we keep it by default and let users opt in to stripping it.
SIGNAL_BEARING_ATTRS = {DTS_TAG("TaskContact")}

# Stripped only from the root <DTS:Executable>. These are pure provenance /
# internal stamps with no translation value (and DTS:CreatorName /
# DTS:CreatorComputerName are PII-flavoured).
ROOT_PROVENANCE_ATTRS = {
    DTS_TAG("CreatorName"),
    DTS_TAG("CreatorComputerName"),
    DTS_TAG("VersionBuild"),
    DTS_TAG("VersionGUID"),
    DTS_TAG("VersionMajor"),
    DTS_TAG("VersionMinor"),
    DTS_TAG("ProductName"),
}

# Kept by default - they carry real signal:
#   DTS:CreationDate                  - reveals package age (legacy detection)
#   DTS:VersionComments               - may contain user-authored release notes
#   DTS:LastModifiedProductVersion    - target SSIS version (translation hint)
# Add the corresponding tag here only if --strip-signal-metadata is set.
SIGNAL_BEARING_ROOT_ATTRS = {
    DTS_TAG("CreationDate"),
    DTS_TAG("VersionComments"),
    DTS_TAG("LastModifiedProductVersion"),
}

ROOT_PROVENANCE_PROPERTY_NAMES = {"PackageFormatVersion"}


def _is_build_project_item(name: str) -> bool:
    if name in BUILD_PROJECT_ITEM_NAMES_EXACT:
        return True
    basename = name.replace("\\", "/").rsplit("/", 1)[-1]
    candidates = {name, basename}
    return any(
        fnmatch.fnmatchcase(c, pat)
        for c in candidates
        for pat in BUILD_PROJECT_ITEM_PATTERNS
    )


def _is_empty_placeholder(elem: ET.Element) -> bool:
    """True if elem is an empty <DTS:Variables /> or default <DTS:LoggingOptions />.

    Both are designer-emitted shells with no runtime effect.
    """
    if list(elem):
        return False
    if (elem.text or "").strip():
        return False

    if elem.tag == DTS_TAG("Variables"):
        return not elem.attrib

    if elem.tag == DTS_TAG("LoggingOptions"):
        attrs = {k: v for k, v in elem.attrib.items()}
        # Default-only LoggingOptions: just DTS:FilterKind="0" (no other config)
        return attrs == {DTS_TAG("FilterKind"): "0"}

    return False


def strip_dtsx(root: ET.Element, opts: argparse.Namespace) -> dict:
    """Walk the tree and remove cosmetic / binary / boilerplate content.

    Returns a count of how many removals of each kind happened.
    """
    counts: dict[str, int] = {}

    parent_map = {c: p for p in root.iter() for c in p}
    to_remove: list[tuple[ET.Element, ET.Element, str]] = []

    for elem in list(root.iter()):
        tag = elem.tag

        if tag in LAYOUT_ELEMENT_TAGS:
            parent = parent_map.get(elem)
            if parent is not None:
                key = "DesignTimeProperties" if tag.endswith("DesignTimeProperties") else tag
                to_remove.append((parent, elem, key))
            continue

        if tag == DTS_TAG("Property"):
            name = elem.get(DTS_TAG("Name"), "")
            if name in LAYOUT_PROPERTY_NAMES:
                parent = parent_map.get(elem)
                if parent is not None:
                    to_remove.append((parent, elem, "DTS:Property[LayoutInfo]"))
                continue
            if (
                opts.strip_metadata
                and name in ROOT_PROVENANCE_PROPERTY_NAMES
                and parent_map.get(elem) is root
            ):
                to_remove.append((root, elem, f"DTS:Property[{name}]"))
            continue

        if tag == "property":
            if elem.get("name", "") in LAYOUT_PROPERTY_NAMES:
                parent = parent_map.get(elem)
                if parent is not None:
                    to_remove.append((parent, elem, "property[LayoutInfo]"))
            continue

        if (
            opts.strip_build_files
            and tag == "ProjectItem"
            and _is_build_project_item(elem.get("Name", ""))
        ):
            parent = parent_map.get(elem)
            if parent is not None:
                to_remove.append(
                    (parent, elem, f"ProjectItem[{elem.get('Name', '')}]")
                )
            continue

        if opts.strip_empty_placeholders and _is_empty_placeholder(elem):
            parent = parent_map.get(elem)
            if parent is not None and parent is not root:
                to_remove.append((parent, elem, f"empty {tag.split('}')[-1]}"))
            continue

        if opts.strip_external_metadata and tag == "externalMetadataColumn":
            parent = parent_map.get(elem)
            if parent is not None:
                to_remove.append((parent, elem, "externalMetadataColumn"))
            continue

    for parent, child, key in to_remove:
        try:
            parent.remove(child)
            counts[key] = counts.get(key, 0) + 1
        except ValueError:
            pass

    if opts.strip_external_metadata:
        for wrap in list(root.iter("externalMetadataColumns")):
            if len(wrap) == 0 and not (wrap.text or "").strip():
                parent = parent_map.get(wrap)
                if parent is not None:
                    try:
                        parent.remove(wrap)
                        counts["externalMetadataColumns (empty)"] = (
                            counts.get("externalMetadataColumns (empty)", 0) + 1
                        )
                    except ValueError:
                        pass

    if opts.strip_metadata:
        attrs_to_strip = set(ROOT_PROVENANCE_ATTRS)
        if opts.strip_signal_metadata:
            attrs_to_strip |= SIGNAL_BEARING_ROOT_ATTRS
        for attr in list(root.attrib):
            if attr in attrs_to_strip:
                del root.attrib[attr]
                counts[f"@{attr.split('}')[-1]}"] = counts.get(
                    f"@{attr.split('}')[-1]}", 0
                ) + 1

    if opts.strip_metadata_attrs:
        attrs_to_strip = set(DEBUG_ATTRS)
        if opts.strip_signal_metadata:
            attrs_to_strip |= SIGNAL_BEARING_ATTRS
        for elem in root.iter():
            for attr in list(elem.attrib):
                if attr in attrs_to_strip:
                    del elem.attrib[attr]
                    key = f"@{attr.split('}')[-1]}"
                    counts[key] = counts.get(key, 0) + 1

    return counts


def process_file(input_path: Path, output_path: Path, opts: argparse.Namespace) -> dict:
    """Parse, strip, and write a single .dtsx file."""
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    tree = ET.parse(str(input_path), parser=parser)
    root = tree.getroot()

    removed = strip_dtsx(root, opts)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(str(output_path), encoding="utf-8", xml_declaration=True)

    orig = input_path.stat().st_size
    new = output_path.stat().st_size
    return {
        "input": str(input_path),
        "output": str(output_path),
        "original_size": orig,
        "stripped_size": new,
        "reduction_pct": (1 - new / orig) * 100 if orig else 0.0,
        "removed": removed,
    }


def _resolve_inputs(in_path: Path) -> tuple[list[Path], Path]:
    if in_path.is_file():
        return [in_path], in_path.parent
    if in_path.is_dir():
        files = list(in_path.rglob("*.dtsx")) + list(in_path.rglob("*.DTSX"))
        files = sorted(set(files))
        return files, in_path
    raise FileNotFoundError(in_path)


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Strip cosmetic, binary, and boilerplate content from SSIS .dtsx files. "
            "All control-flow and data-flow logic is preserved."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("input", help="Path to a .dtsx file or a directory of .dtsx files")
    p.add_argument(
        "--output-dir", "-o", required=True,
        help="Directory to write stripped .dtsx files into. "
             "The input directory structure is mirrored.",
    )
    p.add_argument(
        "--suffix", default="",
        help="Optional suffix appended to the file stem (e.g. _stripped).",
    )
    p.add_argument(
        "--quiet", "-q", action="store_true",
        help="Print only failures and the final summary.",
    )

    g = p.add_argument_group("opt-out flags (all on by default)")
    g.add_argument(
        "--keep-build-files", dest="strip_build_files", action="store_false",
        help="Keep <ProjectItem> MSBuild/codeprojectml scaffolding files.",
    )
    g.add_argument(
        "--keep-debug-dump", dest="strip_metadata_attrs",
        action="store_false",
        help="Keep DTS:IncludeInDebugDump attributes.",
    )
    g.add_argument(
        "--keep-metadata", dest="strip_metadata", action="store_false",
        help="Keep package-level provenance attrs (CreatorName, VersionGUID, "
             "VersionBuild, ProductName, ...) and PackageFormatVersion.",
    )

    g2 = p.add_argument_group("opt-in flags (off by default)")
    g2.add_argument(
        "--strip-empty-placeholders", action="store_true",
        help="Also remove empty <DTS:Variables /> and default-only "
             "<DTS:LoggingOptions DTS:FilterKind=\"0\" /> placeholders nested "
             "inside per-task executables.",
    )
    g2.add_argument(
        "--strip-external-metadata", action="store_true",
        help="Also remove <externalMetadataColumn> design-time validation "
             "metadata. Column name / dataType / length is preserved on "
             "<inputColumn>/<outputColumn> via cachedName/name etc., so no "
             "information needed for translation is lost. Big win on wide-table "
             "data flow packages.",
    )
    g2.add_argument(
        "--strip-signal-metadata", action="store_true",
        help="Also remove attributes that *may* carry useful signal but are "
             "often noise: DTS:TaskContact (legacy SSIS-version hint per task), "
             "DTS:CreationDate (package age), DTS:VersionComments (release notes "
             "field, often empty), DTS:LastModifiedProductVersion (target SSIS "
             "version). Use only if you are sure your packages don't rely on "
             "these as translation context.",
    )

    p.set_defaults(
        strip_build_files=True,
        strip_metadata_attrs=True,
        strip_metadata=True,
        strip_empty_placeholders=False,
        strip_external_metadata=False,
        strip_signal_metadata=False,
    )
    return p


def main() -> None:
    args = _build_argparser().parse_args()

    in_path = Path(args.input)
    out_dir = Path(args.output_dir).resolve()

    try:
        files, in_root = _resolve_inputs(in_path)
    except FileNotFoundError:
        print(f"Error: {in_path} not found", file=sys.stderr)
        sys.exit(1)

    if not files:
        print(f"No .dtsx files found under {in_path}", file=sys.stderr)
        sys.exit(1)

    total_orig = 0
    total_new = 0
    total_removed: dict[str, int] = {}
    failed: list[tuple[Path, str]] = []

    for f in files:
        try:
            rel = f.relative_to(in_root)
        except ValueError:
            rel = Path(f.name)
        out_name = f"{rel.stem}{args.suffix}{f.suffix}"
        out_path = out_dir / rel.parent / out_name

        try:
            r = process_file(f, out_path, args)
        except ET.ParseError as e:
            failed.append((f, f"XML parse error: {e}"))
            print(f"ERR  {f}: XML parse error: {e}", file=sys.stderr)
            continue
        except Exception as e:
            failed.append((f, repr(e)))
            print(f"ERR  {f}: {e!r}", file=sys.stderr)
            continue

        total_orig += r["original_size"]
        total_new += r["stripped_size"]
        for k, v in r["removed"].items():
            total_removed[k] = total_removed.get(k, 0) + v

        if not args.quiet:
            removed_summary = ", ".join(
                f"{k}={v}" for k, v in sorted(r["removed"].items()) if v
            ) or "nothing"
            print(
                f"OK   {f}  ->  {out_path}  "
                f"({r['original_size']/1024:.1f} KB -> {r['stripped_size']/1024:.1f} KB, "
                f"-{r['reduction_pct']:.1f}%; removed: {removed_summary})"
            )

    print()
    print(f"Processed: {len(files) - len(failed)}/{len(files)}")
    if total_orig:
        print(
            f"Total: {total_orig/1024/1024:.2f} MB -> {total_new/1024/1024:.2f} MB "
            f"({(1 - total_new/total_orig) * 100:.1f}% smaller)"
        )
    if total_removed:
        print("Total removed across all files:")
        for k, v in sorted(total_removed.items()):
            print(f"  {k}: {v}")

    if failed:
        print(f"\nFailed files: {len(failed)}", file=sys.stderr)
        for f, why in failed:
            print(f"  - {f}: {why}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
