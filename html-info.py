#!/usr/bin/env python3
"""Print useful basic information about an HTML, XML, or XHTML document.

The script reads a file (or stdin), sniffs the opening declarations, and reports
the kind of document plus easy-to-find metadata such as:

    * doctype and XML declaration
    * root tag, namespaces, language, and text direction
    * declared / inferred character encoding
    * <title> text
    * common <meta> values like description, author, viewport, generator
    * canonical URL and a simple mobile-friendly hint
    * SHA-256 of the raw input bytes
    * whether <head> / <body> tags are present, plus a few tag counts

Output can be either human-readable text or JSON.

Examples:
    html-info.py index.html
    html-info.py --format json page.xhtml
    cat feed.xml | html-info.py

Exit status:
    0   success
    1   file could not be read, or input was empty
    2   usage error (bad/missing arguments; handled by argparse)
"""
import argparse
import codecs
import hashlib
import json
import re
import sys
from html.parser import HTMLParser

__version__ = "1.0.0"

XHTML_NAMESPACE = "http://www.w3.org/1999/xhtml"
SNIFF_BYTES = 65536
COMMON_META_NAMES = (
    "description",
    "author",
    "viewport",
    "generator",
    "keywords",
    "robots",
)


def normalize_space(value):
    """Collapse internal whitespace and strip the ends."""
    if value is None:
        return None
    return " ".join(value.split())


def read_input(path):
    """Return (label, bytes) from a file path or stdin."""
    if path:
        with open(path, "rb") as f:
            return path, f.read()
    return "<stdin>", sys.stdin.buffer.read()


def decode_asciiish(value):
    """Decode ASCII-compatible declaration bytes safely."""
    if value is None:
        return None
    return value.decode("ascii", "replace")


def sniff_declarations(data):
    """Sniff the opening declarations that are easy to extract from raw bytes."""
    head = data[:SNIFF_BYTES]

    xml_decl = re.search(br"^\s*<\?xml\b.*?\?>", head, re.I | re.S)
    xml_encoding = re.search(
        br"^\s*<\?xml\b[^>]*\bencoding\s*=\s*['\"]([^'\"]+)['\"][^>]*\?>",
        head,
        re.I | re.S,
    )
    doctype = re.search(
        br"<!DOCTYPE\b.*?(?:\[[\s\S]*?\]\s*)?>",
        head,
        re.I,
    )
    meta_charset = None
    meta_content_type = None
    for meta_tag in re.finditer(br"<meta\b[^>]*>", head, re.I):
        tag = meta_tag.group(0)
        if meta_charset is None:
            meta_charset = re.search(
                br"\bcharset\s*=\s*['\"]?\s*([^'\"/\s>]+)",
                tag,
                re.I,
            )
        if meta_content_type is None:
            http_equiv = re.search(
                br"\bhttp-equiv\s*=\s*['\"]?\s*content-type\b",
                tag,
                re.I,
            )
            content = re.search(
                br"\bcontent\s*=\s*['\"]([^'\"]*)['\"]",
                tag,
                re.I,
            )
            if http_equiv and content:
                charset = re.search(
                    br"\bcharset=([^;'\"/\s>]+)",
                    content.group(1),
                    re.I,
                )
                if charset:
                    meta_content_type = charset
        if meta_charset and meta_content_type:
            break

    bom_encoding = None
    if data.startswith(codecs.BOM_UTF8):
        bom_encoding = "utf-8-sig"
    elif data.startswith(codecs.BOM_UTF32_LE):
        bom_encoding = "utf-32"
    elif data.startswith(codecs.BOM_UTF32_BE):
        bom_encoding = "utf-32"
    elif data.startswith(codecs.BOM_UTF16_LE):
        bom_encoding = "utf-16"
    elif data.startswith(codecs.BOM_UTF16_BE):
        bom_encoding = "utf-16"

    return {
        "bom_encoding": bom_encoding,
        "xml_declaration": decode_asciiish(xml_decl.group(0)) if xml_decl else None,
        "xml_encoding": decode_asciiish(xml_encoding.group(1)) if xml_encoding else None,
        "doctype": decode_asciiish(doctype.group(0)) if doctype else None,
        "meta_charset": decode_asciiish(meta_charset.group(1)) if meta_charset else None,
        "meta_content_type_charset": (
            decode_asciiish(meta_content_type.group(1)) if meta_content_type else None
        ),
    }


def choose_encoding(sniffed):
    """Pick the best decoding encoding and remember where it came from."""
    if sniffed["bom_encoding"]:
        return sniffed["bom_encoding"], "bom"
    if sniffed["xml_encoding"]:
        return sniffed["xml_encoding"], "xml declaration"
    if sniffed["meta_charset"]:
        return sniffed["meta_charset"], "meta charset"
    if sniffed["meta_content_type_charset"]:
        return sniffed["meta_content_type_charset"], "meta content-type"
    return "utf-8", "default"


def decode_document(data, requested_encoding):
    """Decode bytes into text, surfacing any fallback as a warning."""
    tried = []
    candidates = []
    for encoding in (requested_encoding, "utf-8", "cp1252"):
        if encoding and encoding not in candidates:
            candidates.append(encoding)

    for encoding in candidates:
        try:
            return data.decode(encoding), encoding, None
        except (LookupError, UnicodeDecodeError) as exc:
            tried.append("{0}: {1}".format(encoding, exc))

    warning = (
        "Could not decode input using the sniffed/default encodings; "
        "decoded as utf-8 with replacement characters instead. Tried: {0}".format(
            "; ".join(tried)
        )
    )
    return data.decode("utf-8", errors="replace"), "utf-8", warning


def classify_document(root_tag, default_namespace, doctype, xml_declaration):
    """Classify the document as html, xhtml, or xml."""
    root = (root_tag or "").lower()
    doctype_lower = (doctype or "").lower()
    if root == "html":
        if default_namespace == XHTML_NAMESPACE or "xhtml" in doctype_lower:
            return "xhtml"
        return "html"
    if xml_declaration or root:
        return "xml"
    return "unknown"


def parse_content_type_charset(content):
    """Extract charset=... from a Content-Type meta content string."""
    match = re.search(r"\bcharset\s*=\s*([^;\s]+)", content or "", re.I)
    if not match:
        return None
    return match.group(1).strip(" '\"")


def is_mobile_friendly(viewport):
    """Return a simple hint based on the viewport meta tag."""
    if not viewport:
        return False
    viewport_lower = viewport.lower()
    return (
        "width=device-width" in viewport_lower
        or "initial-scale=" in viewport_lower
        or "viewport-fit=" in viewport_lower
    )


class BasicMarkupParser(HTMLParser):
    """Collect basic document metadata from HTML/XML-like markup."""

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.root_tag = None
        self.root_attrs = {}
        self.namespaces = {}
        self.lang = None
        self.xml_lang = None
        self.text_direction = None
        self.head_present = False
        self.body_present = False
        self.title = None
        self._in_title = False
        self._title_parts = []
        self.meta = {}
        self.meta_count = 0
        self.link_count = 0
        self.stylesheet_count = 0
        self.script_count = 0
        self.style_count = 0
        self.canonical_url = None

    def _record_tag(self, tag, attrs):
        attrs_dict = {}
        for key, value in attrs:
            if key and key not in attrs_dict:
                attrs_dict[key] = value

        if self.root_tag is None:
            self.root_tag = tag
            self.root_attrs = attrs_dict
            self.lang = attrs_dict.get("lang")
            self.xml_lang = attrs_dict.get("xml:lang")
            self.text_direction = attrs_dict.get("dir")
            self.namespaces = {
                key: value
                for key, value in attrs_dict.items()
                if key == "xmlns" or key.startswith("xmlns:")
            }

        if tag == "head":
            self.head_present = True
        elif tag == "body":
            self.body_present = True
        elif tag == "title" and self.title is None:
            self._in_title = True
            self._title_parts = []
        elif tag == "meta":
            self.meta_count += 1
            self._record_meta(attrs_dict)
        elif tag == "link":
            self.link_count += 1
            self._record_link(attrs_dict)
        elif tag == "script":
            self.script_count += 1
        elif tag == "style":
            self.style_count += 1

    def _record_meta(self, attrs):
        charset = normalize_space(attrs.get("charset"))
        if charset and "charset" not in self.meta:
            self.meta["charset"] = charset

        http_equiv = normalize_space(attrs.get("http-equiv"))
        content = normalize_space(attrs.get("content"))
        if http_equiv and http_equiv.lower() == "content-type" and content:
            parsed = parse_content_type_charset(content)
            if parsed and "content_type_charset" not in self.meta:
                self.meta["content_type_charset"] = parsed

        name = normalize_space(attrs.get("name"))
        if name:
            key = name.lower()
            if key in COMMON_META_NAMES and content and key not in self.meta:
                self.meta[key] = content

        prop = normalize_space(attrs.get("property"))
        if prop and prop.lower() == "og:locale" and content and "og:locale" not in self.meta:
            self.meta["og:locale"] = content

    def _record_link(self, attrs):
        rel = normalize_space(attrs.get("rel")) or ""
        href = normalize_space(attrs.get("href"))
        rel_tokens = {token.lower() for token in rel.split()}
        if "stylesheet" in rel_tokens:
            self.stylesheet_count += 1
        if "canonical" in rel_tokens and href and self.canonical_url is None:
            self.canonical_url = href

    def handle_starttag(self, tag, attrs):
        self._record_tag(tag, attrs)

    def handle_startendtag(self, tag, attrs):
        self._record_tag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "title" and self._in_title:
            self.title = normalize_space("".join(self._title_parts))
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self._title_parts.append(data)

    def close(self):
        if self._in_title and self.title is None:
            self.title = normalize_space("".join(self._title_parts))
            self._in_title = False
        HTMLParser.close(self)


def build_info(label, data, sniffed, chosen_encoding, decoded_encoding, decode_warning, parser):
    """Build the final structured info object."""
    meta = {
        "description": parser.meta.get("description"),
        "author": parser.meta.get("author"),
        "viewport": parser.meta.get("viewport"),
        "generator": parser.meta.get("generator"),
        "keywords": parser.meta.get("keywords"),
        "robots": parser.meta.get("robots"),
        "og:locale": parser.meta.get("og:locale"),
    }
    meta = {key: value for key, value in meta.items() if value}

    default_namespace = parser.namespaces.get("xmlns")
    kind = classify_document(
        parser.root_tag,
        default_namespace,
        sniffed["doctype"],
        sniffed["xml_declaration"],
    )

    warnings = []
    if decode_warning:
        warnings.append(decode_warning)
    if chosen_encoding != decoded_encoding:
        warnings.append(
            "Used '{0}' for decoding after '{1}' could not be used.".format(
                decoded_encoding,
                chosen_encoding,
            )
        )

    return {
        "source": label,
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "document_kind": kind,
        "root_tag": parser.root_tag,
        "root_attributes": {
            key: value for key, value in parser.root_attrs.items() if value is not None
        },
        "doctype": sniffed["doctype"],
        "xml_declaration": sniffed["xml_declaration"],
        "encoding": {
            "used": decoded_encoding,
            "chosen": chosen_encoding,
            "chosen_from": sniffed_source_label(sniffed, chosen_encoding),
            "bom": sniffed["bom_encoding"],
            "xml_declaration": sniffed["xml_encoding"],
            "meta_charset": sniffed["meta_charset"] or parser.meta.get("charset"),
            "meta_content_type_charset": (
                sniffed["meta_content_type_charset"]
                or parser.meta.get("content_type_charset")
            ),
        },
        "language": parser.lang or parser.xml_lang,
        "xml_language": parser.xml_lang,
        "text_direction": parser.text_direction,
        "title": parser.title,
        "meta": meta,
        "canonical_url": parser.canonical_url,
        "mobile_friendly_hint": is_mobile_friendly(parser.meta.get("viewport")),
        "namespaces": parser.namespaces,
        "head_present": parser.head_present,
        "body_present": parser.body_present,
        "counts": {
            "meta": parser.meta_count,
            "link": parser.link_count,
            "stylesheet_links": parser.stylesheet_count,
            "script": parser.script_count,
            "style": parser.style_count,
        },
        "warnings": warnings,
    }


def sniffed_source_label(sniffed, chosen_encoding):
    """Return a label describing where the chosen encoding came from."""
    if sniffed["bom_encoding"] == chosen_encoding:
        return "bom"
    if sniffed["xml_encoding"] == chosen_encoding:
        return "xml declaration"
    if sniffed["meta_charset"] == chosen_encoding:
        return "meta charset"
    if sniffed["meta_content_type_charset"] == chosen_encoding:
        return "meta content-type"
    return "default"


def format_yes_no(value):
    return "yes" if value else "no"


def print_human(info):
    """Render the info object as human-readable text."""
    encoding = info["encoding"]
    counts = info["counts"]
    lines = [
        "Source: {0}".format(info["source"]),
        "Document kind: {0}".format(info["document_kind"]),
        "Size: {0} bytes".format(info["size_bytes"]),
        "SHA-256: {0}".format(info["sha256"]),
        "Root tag: {0}".format(info["root_tag"] or "(not found)"),
        "Doctype: {0}".format(info["doctype"] or "(none)"),
        "XML declaration: {0}".format(info["xml_declaration"] or "(none)"),
        "Decoding: {0} (from {1})".format(
            encoding["used"],
            encoding["chosen_from"],
        ),
        "Language: {0}".format(info["language"] or "(not declared)"),
        "XML language: {0}".format(info["xml_language"] or "(not declared)"),
        "Text direction: {0}".format(info["text_direction"] or "(not declared)"),
        "Title: {0}".format(info["title"] or "(none)"),
        "Description: {0}".format(info["meta"].get("description") or "(none)"),
        "Author: {0}".format(info["meta"].get("author") or "(none)"),
        "Viewport: {0}".format(info["meta"].get("viewport") or "(none)"),
        "Mobile-friendly hint: {0}".format(format_yes_no(info["mobile_friendly_hint"])),
        "Generator: {0}".format(info["meta"].get("generator") or "(none)"),
        "Keywords: {0}".format(info["meta"].get("keywords") or "(none)"),
        "Robots: {0}".format(info["meta"].get("robots") or "(none)"),
        "Canonical URL: {0}".format(info["canonical_url"] or "(none)"),
        "Head tag present: {0}".format(format_yes_no(info["head_present"])),
        "Body tag present: {0}".format(format_yes_no(info["body_present"])),
        "Counts: meta={0}, link={1}, stylesheet_links={2}, script={3}, style={4}".format(
            counts["meta"],
            counts["link"],
            counts["stylesheet_links"],
            counts["script"],
            counts["style"],
        ),
    ]

    if info["namespaces"]:
        namespace_bits = []
        for key in sorted(info["namespaces"]):
            namespace_bits.append("{0}={1}".format(key, info["namespaces"][key]))
        lines.append("Namespaces: {0}".format(", ".join(namespace_bits)))
    else:
        lines.append("Namespaces: (none)")

    declared = []
    if encoding["bom"]:
        declared.append("bom={0}".format(encoding["bom"]))
    if encoding["xml_declaration"]:
        declared.append("xml={0}".format(encoding["xml_declaration"]))
    if encoding["meta_charset"]:
        declared.append("meta charset={0}".format(encoding["meta_charset"]))
    if encoding["meta_content_type_charset"]:
        declared.append(
            "meta content-type={0}".format(encoding["meta_content_type_charset"])
        )
    lines.append(
        "Declared encodings: {0}".format(", ".join(declared) if declared else "(none)")
    )

    if info["warnings"]:
        lines.append("Warnings: {0}".format(" | ".join(info["warnings"])))

    for line in lines:
        print(line)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="html-info.py",
        description="Print useful basic information about an HTML/XML/XHTML file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "output formats:\n"
            "  human   readable key/value lines (default)\n"
            "  json    machine-friendly JSON\n"
            "\n"
            "examples:\n"
            "  html-info.py index.html\n"
            "  html-info.py --format json page.xhtml\n"
            "  cat feed.xml | html-info.py\n"
        ),
    )
    parser.add_argument(
        "path",
        nargs="?",
        help="file to inspect (default: read bytes from stdin)",
    )
    parser.add_argument(
        "--format",
        choices=("human", "json"),
        default="human",
        help="output format (default: human)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s {0}".format(__version__),
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    try:
        label, data = read_input(args.path)
    except OSError as exc:
        print("error: {0}".format(exc), file=sys.stderr)
        return 1

    if not data.strip():
        print("error: input is empty", file=sys.stderr)
        return 1

    sniffed = sniff_declarations(data)
    chosen_encoding, _source = choose_encoding(sniffed)
    text, decoded_encoding, decode_warning = decode_document(data, chosen_encoding)

    parser = BasicMarkupParser()
    parser.feed(text)
    parser.close()

    info = build_info(
        label,
        data,
        sniffed,
        chosen_encoding,
        decoded_encoding,
        decode_warning,
        parser,
    )

    if args.format == "json":
        json.dump(info, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        print_human(info)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
