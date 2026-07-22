#!/usr/bin/env python3
# /// script
# requires-python = ">=3.8"
# dependencies = ["dnfile"]
# ///
"""
inspect-nuget-package.py — List the .NET API symbols inside a NuGet package, or
check whether a specific one exists.

A .nupkg is a ZIP archive containing one or more managed assemblies (.dll),
typically under lib/<TFM>/ (and sometimes ref/<TFM>/ for reference-only
facade assemblies). This reads those assemblies' CLR metadata directly out of
the ZIP — no C#/.NET toolchain, no ildasm, no extraction to disk — and reports
every namespace/type/member as a "symbol":

    type      a class, interface, struct, enum, or delegate
    method    including constructors and property/event accessors
    field
    property
    event

Symbols are named the way .NET reflection names them: "Namespace.Type" for a
type ("Outer+Inner" for a nested one), "Namespace.Type.Member" for a member.

WHAT THIS IS NOT
----------------
This reads type/method/field/property/event *declarations* (ECMA-335 metadata
tables), not method bodies. It cannot tell you what a method does, and it does
not decode full parameter/return types (that needs a much larger signature
decoder) — only the parameter *count*, which is enough to tell most overloads
apart in a listing; two overloads sharing the same arity still collapse into
one entry. Compiler-generated noise (backing fields, closures, anything whose
name contains '<' or '>' — never a legal source identifier) is always
skipped, since it's never what someone means by "a symbol in this package".

Only symbols actually defined in a scanned assembly are reported. A facade
assembly that merely *forwards* a type elsewhere (an ECMA-335 ExportedType,
common in netstandard reference assemblies) is not followed or reported.

Usage:
    inspect-nuget-package PACKAGE.nupkg                     # list public symbols
    inspect-nuget-package PACKAGE.nupkg --all                # include non-public too
    inspect-nuget-package PACKAGE.nupkg --json
    inspect-nuget-package PACKAGE.nupkg --check JsonConvert
    inspect-nuget-package PACKAGE.nupkg --check Newtonsoft.Json.JsonConvert.SerializeObject
    inspect-nuget-package PACKAGE.nupkg --list-tfms
"""

import argparse
import json
import logging
import sys
import zipfile

try:
    import dnfile
    from dnfile.errors import dnFormatError
except ImportError:  # pragma: no cover - surfaced to the user with guidance
    sys.stderr.write(
        "inspect-nuget-package: the 'dnfile' module is required (.NET metadata "
        "parsing).\nRun via the provided wrapper (uv installs it), or: pip "
        "install dnfile\n"
    )
    sys.exit(1)

# dnfile logs a WARNING (straight to stderr, via Python's logging "handler of
# last resort") for oddities in a handful of real-world assemblies with
# slightly non-conforming metadata -- e.g. rewritten/instrumented assemblies.
# It recovers and keeps parsing regardless, so this is noise for our purposes,
# not something actionable through this tool's own error handling.
logging.getLogger("dnfile").setLevel(logging.ERROR)

try:
    from pefile import PEFormatError
except ImportError:  # pragma: no cover - pefile is a dnfile dependency
    PEFormatError = dnFormatError

__version__ = "1.0"

# ---------------------------------------------------------------------------
# Visibility (ECMA-335 II.23.1.15 TypeAttributes, II.23.1.10 FieldAttributes,
# II.23.1.10 MethodAttributes visibility sub-fields all share this shape)
# ---------------------------------------------------------------------------
# rank: how accessible, low to high. Used to pick the most-accessible accessor
# for a property/event, which has no visibility of its own in the metadata.
_VISIBILITY_RANK = {
    "private": 0,
    "private protected": 1,
    "internal": 2,
    "protected": 3,
    "protected internal": 4,
    "public": 5,
}

# A symbol is part of the "public API" (visible to some consumer outside the
# declaring assembly, directly or via inheritance) at these levels.
_PUBLIC_FACING = {"public", "protected", "protected internal"}

_TYPE_VISIBILITY = {
    "tdNotPublic": "internal",
    "tdPublic": "public",
    "tdNestedPublic": "public",
    "tdNestedPrivate": "private",
    "tdNestedFamily": "protected",
    "tdNestedAssembly": "internal",
    "tdNestedFamANDAssem": "private protected",
    "tdNestedFamORAssem": "protected internal",
}


def type_visibility(flags):
    """Return the visibility label for a TypeDef's Flags (CLASS_ATTRS bitfield)."""
    for name, label in _TYPE_VISIBILITY.items():
        if getattr(flags, name, False):
            return label
    return "internal"


# MethodAttributes spells the "internal" bit 'Assem'; FieldAttributes spells
# the identical concept 'Assembly' (ECMA-335 II.23.1.10 vs II.23.1.15) -- an
# inconsistency in the spec itself, so each needs its own attribute name.
_METHOD_VISIBILITY = [
    ("mdPublic", "public"),
    ("mdFamORAssem", "protected internal"),
    ("mdFamily", "protected"),
    ("mdAssem", "internal"),
    ("mdFamANDAssem", "private protected"),
    ("mdPrivate", "private"),
    ("mdPrivateScope", "private"),
]
_FIELD_VISIBILITY = [
    ("fdPublic", "public"),
    ("fdFamORAssem", "protected internal"),
    ("fdFamily", "protected"),
    ("fdAssembly", "internal"),
    ("fdFamANDAssem", "private protected"),
    ("fdPrivate", "private"),
    ("fdPrivateScope", "private"),
]


def member_visibility(flags, prefix):
    """Return the visibility label for a method/field Flags bitfield.

    ``prefix`` is 'md' for MethodAttributes or 'fd' for FieldAttributes.
    """
    checks = _METHOD_VISIBILITY if prefix == "md" else _FIELD_VISIBILITY
    for attr, label in checks:
        if getattr(flags, attr, False):
            return label
    return "private"


def is_public_facing(label):
    return label in _PUBLIC_FACING


# ---------------------------------------------------------------------------
# Compressed-integer parsing (ECMA-335 II.23.2), just enough of the method
# signature blob to recover the parameter count without decoding full types.
# ---------------------------------------------------------------------------
def _read_compressed_uint(data, pos):
    """Return (value, next_pos) for the compressed unsigned int at data[pos:]."""
    first = data[pos]
    if first & 0x80 == 0:
        return first, pos + 1
    if first & 0xC0 == 0x80:
        value = ((first & 0x3F) << 8) | data[pos + 1]
        return value, pos + 2
    value = ((first & 0x1F) << 24) | (data[pos + 1] << 16) | \
        (data[pos + 2] << 8) | data[pos + 3]
    return value, pos + 4


def decode_param_count(sig_bytes):
    """Parse a MethodDefSig's header (II.23.2.1) to recover its parameter count.

    Layout: [flags byte] [generic param count, if GENERIC flag set] [param
    count] RetType Param*. We stop right after ParamCount -- the return type
    and parameter types themselves are never decoded.
    """
    if not sig_bytes:
        return None
    flags = sig_bytes[0]
    pos = 1
    GENERIC = 0x10
    try:
        if flags & GENERIC:
            _, pos = _read_compressed_uint(sig_bytes, pos)
        param_count, pos = _read_compressed_uint(sig_bytes, pos)
        return param_count
    except IndexError:
        return None


# ---------------------------------------------------------------------------
# Type identity / naming
# ---------------------------------------------------------------------------
def build_nested_map(md):
    """Return {id(nested_TypeDefRow): enclosing_TypeDefRow} from NestedClass."""
    nested = {}
    table = md.NestedClass
    if table is None:
        return nested
    for row in table.rows:
        nested[id(row.NestedClass.row)] = row.EnclosingClass.row
    return nested


def type_full_name(row, nested_map):
    """'Namespace.Type', or 'Namespace.Outer+Inner' for a nested type."""
    chain = [str(row.TypeName)]
    enclosing = nested_map.get(id(row))
    while enclosing is not None:
        chain.append(str(enclosing.TypeName))
        enclosing = nested_map.get(id(enclosing))
    namespace = enclosing_namespace(row, nested_map)
    name = "+".join(reversed(chain))
    return f"{namespace}.{name}" if namespace else name


def enclosing_namespace(row, nested_map):
    """The namespace of the outermost enclosing type (namespaces are only
    recorded on the outermost TypeDef; nested types have an empty one)."""
    top = row
    while True:
        parent = nested_map.get(id(top))
        if parent is None:
            break
        top = parent
    return str(top.TypeNamespace)


def is_type_public_facing(row, nested_map):
    """A nested type is only really reachable from outside if every enclosing
    type is also public-facing -- an internal outer type hides a public inner
    one just as effectively as marking the inner type internal would."""
    current = row
    while current is not None:
        if not is_public_facing(type_visibility(current.Flags)):
            return False
        current = nested_map.get(id(current))
    return True


_BASE_TYPE_KIND = {
    "Enum": "enum",
    "ValueType": "struct",
    "MulticastDelegate": "delegate",
}


def type_kind(row):
    if getattr(row.Flags, "tdInterface", False):
        return "interface"
    extends = row.Extends
    base_row = getattr(extends, "row", None) if extends is not None else None
    if base_row is not None:
        try:
            base_name = str(base_row.TypeName)
        except AttributeError:
            base_name = None  # e.g. a TypeSpec (generic base) -- treat as class
        if base_name in _BASE_TYPE_KIND:
            return _BASE_TYPE_KIND[base_name]
    return "class"


def is_compiler_generated_name(name):
    """Never a legal source identifier -- backing fields ('<Foo>k__BackingField'),
    closures, iterator/async state machines, etc. Always noise for this tool."""
    return "<" in name or ">" in name


# ---------------------------------------------------------------------------
# Symbol extraction from one loaded assembly
# ---------------------------------------------------------------------------
def build_property_event_visibility(md):
    """Map id(PropertyRow|EventRow) -> most-accessible linked accessor's label,
    via MethodSemantics (II.22.28), since properties/events carry no
    visibility of their own -- only their get/set/add/remove methods do."""
    best = {}
    table = md.MethodSemantics
    if table is None:
        return best
    for row in table.rows:
        assoc_row = row.Association.row
        method_row = row.Method.row
        label = member_visibility(method_row.Flags, "md")
        key = id(assoc_row)
        current = best.get(key)
        if current is None or _VISIBILITY_RANK[label] > _VISIBILITY_RANK[current]:
            best[key] = label
    return best


def extract_symbols(pe, source):
    """Yield symbol dicts for every type/method/field/property/event defined
    in this assembly. ``source`` (e.g. a target-framework moniker) is recorded
    on each symbol so multiple assemblies' results can be merged and traced
    back to where they came from."""
    md = pe.net.mdtables
    nested_map = build_nested_map(md)
    prop_event_vis = build_property_event_visibility(md)

    for row in md.TypeDef.rows:
        if str(row.TypeName) == "<Module>":
            continue  # the pseudo-type holding module-level fields/methods
        if is_compiler_generated_name(str(row.TypeName)):
            continue  # closures, iterator/async state machines, etc.

        full_name = type_full_name(row, nested_map)
        public_facing = is_type_public_facing(row, nested_map)
        own_visibility = type_visibility(row.Flags)

        yield {
            "kind": "type",
            "type_kind": type_kind(row),
            "name": full_name,
            "visibility": own_visibility,
            "is_public": public_facing,
            "static": bool(getattr(row.Flags, "tdAbstract", False)
                           and getattr(row.Flags, "tdSealed", False)),
            "source": source,
        }

        for member_index in row.MethodList:
            m = member_index.row
            name = str(m.Name)
            if is_compiler_generated_name(name):
                continue
            visibility = member_visibility(m.Flags, "md")
            yield {
                "kind": "method",
                "name": f"{full_name}.{name}",
                "visibility": visibility,
                "is_public": public_facing and is_public_facing(visibility),
                "static": bool(getattr(m.Flags, "mdStatic", False)),
                "params": decode_param_count(m.Signature.value),
                "source": source,
            }

        for member_index in row.FieldList:
            f = member_index.row
            name = str(f.Name)
            if is_compiler_generated_name(name):
                continue
            visibility = member_visibility(f.Flags, "fd")
            yield {
                "kind": "field",
                "name": f"{full_name}.{name}",
                "visibility": visibility,
                "is_public": public_facing and is_public_facing(visibility),
                "static": bool(getattr(f.Flags, "fdStatic", False)),
                "source": source,
            }

    for row in (md.Property.rows if md.Property else []):
        name = str(row.Name)
        if is_compiler_generated_name(name):
            continue
        owner = _find_owner(md.PropertyMap, row, "PropertyList")
        if owner is None:
            continue
        owner_name = type_full_name(owner, nested_map)
        owner_public = is_type_public_facing(owner, nested_map)
        visibility = prop_event_vis.get(id(row), type_visibility(owner.Flags))
        yield {
            "kind": "property",
            "name": f"{owner_name}.{name}",
            "visibility": visibility,
            "is_public": owner_public and is_public_facing(visibility),
            "static": False,
            "source": source,
        }

    for row in (md.Event.rows if md.Event else []):
        name = str(row.Name)
        if is_compiler_generated_name(name):
            continue
        owner = _find_owner(md.EventMap, row, "EventList")
        if owner is None:
            continue
        owner_name = type_full_name(owner, nested_map)
        owner_public = is_type_public_facing(owner, nested_map)
        visibility = prop_event_vis.get(id(row), type_visibility(owner.Flags))
        yield {
            "kind": "event",
            "name": f"{owner_name}.{name}",
            "visibility": visibility,
            "is_public": owner_public and is_public_facing(visibility),
            "static": False,
            "source": source,
        }


def _find_owner(map_table, member_row, list_attr):
    """Look up the owning TypeDef of a Property/Event row via its *Map table
    (the tables link a type to a *range* of rows; dnfile resolves that range
    into a concrete list, so a linear scan is all that's needed here)."""
    if map_table is None:
        return None
    for map_row in map_table.rows:
        for idx in getattr(map_row, list_attr):
            if idx.row is member_row:
                return map_row.Parent.row
    return None


# ---------------------------------------------------------------------------
# Walking the .nupkg archive
# ---------------------------------------------------------------------------
def _tfm_of(arcname):
    """Extract the target-framework folder name from 'lib/<tfm>/x.dll' etc."""
    parts = arcname.split("/")
    if len(parts) >= 3 and parts[0] in ("lib", "ref"):
        return parts[1]
    return None


def find_assemblies(zf, tfm=None):
    """Yield (arcname, tfm_or_None) for every .dll under lib/ or ref/."""
    for info in zf.infolist():
        name = info.filename
        if not name.lower().endswith(".dll"):
            continue
        parts = name.split("/")
        if not parts or parts[0] not in ("lib", "ref"):
            continue
        this_tfm = _tfm_of(name)
        if tfm is not None and this_tfm != tfm:
            continue
        yield name, this_tfm


def list_tfms(zf):
    return sorted({t for _, t in find_assemblies(zf) if t})


def load_assembly(zf, arcname):
    """Return a dnfile.dnPE for the assembly at arcname, or None (with a
    warning on stderr) if it isn't a .NET assembly / can't be parsed."""
    data = zf.read(arcname)
    try:
        pe = dnfile.dnPE(data=data)
    except (PEFormatError, dnFormatError) as exc:
        sys.stderr.write(f"inspect-nuget-package: skipping {arcname}: {exc}\n")
        return None
    if pe.net is None:
        sys.stderr.write(
            f"inspect-nuget-package: skipping {arcname}: not a .NET assembly\n"
        )
        return None
    return pe


def collect_symbols(package_path, tfm=None):
    """Return (symbols, assemblies_scanned). Symbols with the same (kind,
    name) from different assemblies are merged, recording every source they
    were seen in -- this is what keeps a package with 6 near-identical
    lib/<tfm>/ builds from reporting the same symbol 6 times."""
    merged = {}
    scanned = []
    with zipfile.ZipFile(package_path) as zf:
        assemblies = list(find_assemblies(zf, tfm))
        for arcname, this_tfm in assemblies:
            pe = load_assembly(zf, arcname)
            if pe is None:
                continue
            scanned.append(arcname)
            label = this_tfm or arcname
            for sym in extract_symbols(pe, label):
                # params disambiguates overloads sharing a name (methods only;
                # None for every other kind, so those still dedupe on name
                # alone). Two overloads with the *same* arity still collapse
                # into one entry -- see the module docstring's caveat.
                key = (sym["kind"], sym["name"], sym.get("params"))
                existing = merged.get(key)
                if existing is None:
                    sym["sources"] = [sym.pop("source")]
                    merged[key] = sym
                else:
                    src = sym["source"]
                    if src not in existing["sources"]:
                        existing["sources"].append(src)
    return list(merged.values()), scanned


# ---------------------------------------------------------------------------
# Symbol matching (--check)
# ---------------------------------------------------------------------------
def matches_query(symbol_name, query, contains, ignore_case):
    name, q = symbol_name, query
    if ignore_case:
        name, q = name.lower(), q.lower()
    if contains:
        return q in name
    if "." in q or "+" in q:
        return name == q
    # A bare identifier (no dots): match the last segment -- the type or
    # member's own name, ignoring its enclosing namespace/type.
    tail = name.replace("+", ".").rsplit(".", 1)[-1]
    return tail == q


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def format_symbol(sym):
    extra = ""
    if sym["kind"] == "type":
        extra = f" ({sym['type_kind']})"
    elif sym["kind"] == "method" and sym.get("params") is not None:
        extra = f" ({sym['params']} param{'s' if sym['params'] != 1 else ''})"
    static = " static" if sym.get("static") else ""
    return f"{sym['kind']:<8} {sym['visibility']:<17}{static:<7} {sym['name']}{extra}"


def build_arg_parser():
    p = argparse.ArgumentParser(
        prog="inspect-nuget-package",
        description="List the .NET API symbols in a NuGet package, or check "
                    "whether one exists.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("package", help="path to a .nupkg file")
    p.add_argument("--check", metavar="SYMBOL",
                   help="check whether SYMBOL exists instead of listing "
                        "every symbol; exit status reflects the result")
    p.add_argument("--contains", action="store_true",
                   help="match --check as a substring instead of requiring "
                        "an exact match")
    p.add_argument("-i", "--ignore-case", action="store_true",
                   help="case-insensitive matching for --check")
    p.add_argument("--all", action="store_true",
                   help="include non-public symbols too (default: public "
                        "API surface only -- public and protected)")
    p.add_argument("--kind", action="append", choices=[
        "type", "method", "field", "property", "event",
    ], help="restrict to this symbol kind (repeatable); default: all kinds")
    p.add_argument("--tfm", metavar="NAME",
                   help="only scan assemblies under lib/<NAME>/ or "
                        "ref/<NAME>/ (default: scan every assembly found)")
    p.add_argument("--list-tfms", action="store_true",
                   help="list the target-framework folders in the package "
                        "and exit")
    p.add_argument("--json", action="store_true",
                   help="emit JSON instead of a human-readable listing")
    return p


def main(argv=None):
    args = build_arg_parser().parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    try:
        if args.list_tfms:
            with zipfile.ZipFile(args.package) as zf:
                tfms = list_tfms(zf)
            if args.json:
                print(json.dumps(tfms))
            else:
                for t in tfms:
                    print(t)
            return 0

        symbols, scanned = collect_symbols(args.package, tfm=args.tfm)
    except (OSError, zipfile.BadZipFile) as exc:
        sys.stderr.write(f"inspect-nuget-package: cannot read package: {exc}\n")
        return 1

    if not scanned:
        sys.stderr.write(
            "inspect-nuget-package: no .NET assemblies found under lib/ or "
            "ref/" + (f" for --tfm {args.tfm}" if args.tfm else "") + "\n"
        )
        return 1

    if not args.all:
        symbols = [s for s in symbols if s["is_public"]]
    if args.kind:
        wanted = set(args.kind)
        symbols = [s for s in symbols if s["kind"] in wanted]
    symbols.sort(key=lambda s: (s["kind"], s["name"]))

    if args.check is not None:
        matches = [s for s in symbols
                   if matches_query(s["name"], args.check, args.contains,
                                     args.ignore_case)]
        if args.json:
            print(json.dumps(matches, ensure_ascii=False, indent=2))
        elif matches:
            for s in matches:
                print(format_symbol(s))
            print(f"\nFound {len(matches)} match(es) for {args.check!r}.")
        else:
            print(f"No match for {args.check!r}.")
        return 0 if matches else 1

    if args.json:
        print(json.dumps(symbols, ensure_ascii=False, indent=2))
    else:
        for s in symbols:
            print(format_symbol(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
