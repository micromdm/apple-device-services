#!/usr/bin/env python3
"""Sync local JSON Schemas against Apple's documentation (DocC render JSON).

A single ``sync`` command with these shapes:

    apple_schema.py sync <schema-file|schema-dir> [options]
        Merge existing schema file(s) in place (additive; never removes). For a
        directory, also flag missing/extra types against Apple's nav index,
        auto-deriving the relevant nav root from the on-disk schemas.

    apple_schema.py sync <schema-dir> --root <nav-root> [--generate] [options]
        Compare against an explicit nav root (e.g. an empty/new directory),
        flagging missing/extra, and (with --generate) creating the missing ones.

    apple_schema.py sync <type-url|doc-path> [out-dir] [options]
        Generate a single schema from a dictionary type (stdout if no out-dir).

Options:
    --root ROOT     nav root URL/doc-path to compare a schema directory against
    --generate      with --root, also generate the missing schemas
    --dry-run       report what would change without writing
    --enums         also mix in enum candidates from codeVoice tokens (noisy)
    --force-types   overwrite existing properties' type shapes from Apple
    --fmt           format the synced schemas with the `jsonschema` CLI
    --cached        use cached copies only; do not hit the network
    --cache-dir DIR cache directory (default: ~/.cache/apple_schema)

Notes:
    Enum-like *type aliases* (e.g. ``ActivationLockType``) are rendered by
    Apple as ``typealias`` symbols carrying a ``possibleValues`` section; they
    become string schemas constrained to that value set. A value set with a
    single member is expressed with ``const`` instead of ``enum``.

    Run without ``--cached`` once to populate the cache, then re-run with
    ``--cached`` to re-apply the cached documents without re-fetching them.

    The schemas are written as plain JSON; the repo's canonical layout comes
    from the ``jsonschema`` CLI's formatter, which ``--fmt`` runs for you (and
    which the CI checks with ``jsonschema fmt -c``).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

# A JSON document or sub-document (loosely typed).
JSON = dict[str, Any]

UA = {"User-Agent": "Mozilla/5.0"}

# Apple scalar type names mapped to JSON Schema ``type`` values.
SCALAR_MAP: dict[str, str] = {
    "string": "string",
    "boolean": "boolean",
    "bool": "boolean",
    "int": "integer",
    "int32": "integer",
    "int64": "integer",
    "integer": "integer",
    "number": "number",
    "float": "number",
    "double": "number",
}

# Apple type names that are JSON Schema string formats, not types. They appear
# as bare ``text`` type tokens (e.g. ``uri-reference``) and must map to
# ``type: string`` plus the matching ``format``.
STRING_FORMATS: set[str] = {
    "uri-reference",
    "date",
    "date-time",
}

# Apple type names that JSON Schema does not have. They map to their JSON
# Schema equivalent: ``byte``/``data`` are base64-encoded blobs, and ``*``
# ("any") constrains nothing at all.
SPECIAL_TYPES: dict[str, dict[str, Any]] = {
    "byte": {"type": "string", "contentEncoding": "base64"},
    "data": {"type": "string", "contentEncoding": "base64"},
    "*": {},
}

# The ``type`` values JSON Schema defines.
JSON_SCHEMA_TYPES: set[str] = {
    "array",
    "boolean",
    "integer",
    "null",
    "number",
    "object",
    "string",
}


def derive_data_uri(api_uri: str) -> str | None:
    """Derive the tutorial JSON URL from a documentation page URL."""
    if "developer.apple.com/documentation/" not in api_uri:
        return None
    return (
        api_uri.replace(
            "developer.apple.com/documentation/",
            "developer.apple.com/tutorials/data/documentation/",
        )
        + ".json"
    )


def resolve_doc_path(ref: str) -> str | None:
    """Resolve a root ref (api-uri, doc path, or data URI) to a doc path."""
    if "/documentation/" in ref:
        return ref[ref.index("/documentation/") :]
    if "/tutorials/data/documentation/" in ref:
        rest = ref.split("/tutorials/data/documentation/", 1)[1]
        return "/documentation/" + rest.removesuffix(".json")
    return None


def doc_path_data_uri(doc_path: str) -> str:
    """Convert a ``/documentation/...`` doc path to its tutorial JSON data URI."""
    return "https://developer.apple.com/tutorials/data" + doc_path + ".json"


def cache_file(cache_dir: str, url: str) -> str:
    """Map a data URI to a cache file path (mirrors the doc path)."""
    rest = url.split("/", 3)[3]
    rest = rest.removeprefix("tutorials/data/")
    return os.path.join(cache_dir, rest)


def fetch_json(
    url: str,
    cache_dir: str | None = None,
    cached_only: bool = False,
) -> tuple[Any, str | None]:
    """Fetch JSON with a write-through file cache.

    Returns ``(data, fetched_at)``. ``fetched_at`` is the HTTP ``Date`` header
    on a fresh fetch, the cache file mtime when served from cache, or
    ``"stale:<mtime>"`` when falling back to cache after a network error.

    Raises ``HTTPError`` for definitive server responses (404, etc.) — these are
    never masked by the cache. Raises ``URLError`` only when there is no cached
    copy to fall back on.

    FUTURE (ETag): Apple's CDN likely returns ETag/Last-Modified. To avoid
    re-downloading unchanged content, store the ETag + Last-Modified alongside
    the cached JSON and issue a conditional GET (If-None-Match /
    If-Modified-Since) to reuse the copy on 304 Not Modified.
    """
    path = cache_file(cache_dir, url) if cache_dir else None

    if cached_only:
        if path and os.path.exists(path):
            with open(path) as fh:
                return json.load(fh), _mtime(path)
        raise urllib.error.URLError(f"no cached copy of {url}")

    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req) as r:
            data = json.load(r)
            fetched_at = r.headers.get("Date")
        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                json.dump(data, fh)
        return data, fetched_at
    except urllib.error.HTTPError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError):
        if path and os.path.exists(path):
            with open(path) as fh:
                return json.load(fh), "stale:" + _mtime(path)
        raise


def _mtime(path: str) -> str:
    """Return a cache file's mtime as a UTC ISO timestamp."""
    return datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc).isoformat()


def inline_tokens_to_commonmark(
    inline_content: list[JSON], refs: JSON
) -> tuple[str, list[str]]:
    """Translate one block's ``inlineContent[]`` token list to CommonMark.

    Apple's markup is a structured token stream, not Markdown:

    - ``text``               -> plain text
    - ``codeVoice``/``code`` -> backticked code span
    - ``reference``          -> ``[title](url)`` resolved via ``references``

    Returns ``(markdown, code_voices)``, where ``code_voices`` is the list of raw
    ``code``/``codeVoice`` values (used for enum heuristics).
    """
    parts: list[str] = []
    codes: list[str] = []
    for tok in inline_content:
        t = tok.get("type")
        if t == "text":
            parts.append(tok.get("text", ""))
        elif t in ("codeVoice", "code"):
            # `codeVoice`/`code` are overloaded: they mark BOTH enum literals
            # (e.g. "iPad", "added") and ordinary inline code in prose (e.g.
            # "true", "op_type"). We emit a backticked span for Markdown and
            # also collect the raw value for enum heuristics (see
            # extract_apple_properties).
            code = tok.get("code", "")
            parts.append(f"`{code}`")
            codes.append(code)
        elif t == "reference":
            ident = tok.get("identifier")
            title = None
            url = None
            if ident and refs:
                ref = refs.get(ident, {})
                title = ref.get("title")
                url = ref.get("url")
            title = title or tok.get("title") or tok.get("text") or ""
            if url:
                if not url.startswith("http"):
                    url = "https://developer.apple.com" + url
                parts.append(f"[{title}]({url})")
            else:
                parts.append(title)
        # other token types (image, etc.) are ignored
    return "".join(parts), codes


def content_to_commonmark(
    paragraphs: list[JSON],
    refs: JSON,
    sep: str = "\n\n",
) -> tuple[str, list[str]]:
    """Translate a ``content[]`` block list to CommonMark.

    Blocks are joined with ``sep`` (a blank line by default, matching paragraph
    structure).
    """
    blocks: list[str] = []
    codes: list[str] = []
    for p in paragraphs:
        md, c = inline_tokens_to_commonmark(p.get("inlineContent", []), refs)
        if md:
            blocks.append(md)
        codes.extend(c)
    return sep.join(blocks).strip(), codes


def apple_type_to_schema(
    type_tokens: list[JSON],
) -> tuple[dict[str, Any] | None, str | None]:
    """Map Apple type tokens to a JSON Schema type dict (and optional ``$ref``)."""
    texts: list[str] = []
    ref = None
    for tok in type_tokens:
        k = tok.get("kind")
        if k == "typeIdentifier":
            ref = tok.get("text")
            texts.append(tok.get("text"))
        elif k == "text":
            texts.append(tok.get("text"))
    # Apple encodes a type as a token sequence (e.g. `[`, typeIdentifier, `]`
    # for an array of a referenced type). Join them, then detect array vs
    # scalar vs $ref.
    expr = "".join(texts)
    is_array = expr.startswith("[") and expr.endswith("]")
    inner = expr[1:-1] if is_array else expr
    # `$ref` targets a sibling schema file, so the type name needs its `.json`
    # extension appended (the type name is the filename sans extension).
    ref_file = (ref + ".json") if ref else None
    if ref and inner == ref:
        elem: dict[str, Any] = {"$ref": ref_file}
    elif inner in STRING_FORMATS:
        elem = {"type": "string", "format": inner}
    elif inner in SPECIAL_TYPES:
        elem = dict(SPECIAL_TYPES[inner])
    else:
        elem = {"type": SCALAR_MAP.get(inner, inner)}
    if is_array:
        if not elem:
            # An unconstrained element type: an `items` schema that matches
            # everything adds no constraint, so it is left out.
            return {"type": "array"}, ref
        return {"type": "array", "items": elem}, ref
    if ref:
        return {"$ref": ref_file}, ref
    return elem, None


def map_value_schema(doc: JSON) -> dict[str, Any] | None:
    """Return a map's value schema if ``doc`` is a map placeholder, else ``None``.

    Apple encodes a dictionary with arbitrary string keys as a nested
    ``*-data.dictionary`` whose single property is named ``Any Key``. The value
    schema (which may itself be a ``$ref``) is the map's ``additionalProperties``.
    """
    items: list[JSON] = []
    for sec in doc.get("primaryContentSections", []):
        if sec.get("kind") != "properties":
            continue
        for it in sec.get("items", []):
            items.append(it)
    if len(items) != 1 or items[0].get("name") != "Any Key":
        return None
    type_dict, _ref = apple_type_to_schema(items[0].get("type", []))
    return type_dict


def extract_apple_properties(doc: JSON) -> dict[str, dict[str, Any]]:
    """Extract Apple's property inventory from a tutorial JSON document."""
    refs = doc.get("references", {})
    props: dict[str, dict[str, Any]] = {}
    for sec in doc.get("primaryContentSections", []):
        if sec.get("kind") != "properties":
            continue
        for it in sec.get("items", []):
            name = it.get("name")
            type_dict, ref = apple_type_to_schema(it.get("type", []))
            desc, codes = content_to_commonmark(it.get("content", []), refs)
            # Apple now exposes a structured `required` flag per property, but it
            # is only meaningful in some namespaces (e.g. ANB); others (e.g. DEP)
            # report `false` for every property. Capture it anyway and let callers
            # decide how much to trust it.
            required = bool(it.get("required"))
            # Structured enums live in `attributes[]` as `allowedValues`. This is
            # authoritative, unlike the `codeVoice` prose-token heuristic.
            allowed_values = None
            for attr in it.get("attributes", []):
                if attr.get("kind") == "allowedValues":
                    allowed_values = attr.get("values")
                    break
            props[name] = {
                "type": type_dict,
                "ref": ref,
                "description": desc,
                "codeVoice": codes,
                "datetime_hint": "ISO 8601" in desc,
                "required": required,
                "allowedValues": allowed_values,
            }
    return props


def norm(s: str | None) -> str:
    """Normalize a description for comparison (collapse whitespace, unify quotes)."""
    s = s or ""
    s = s.replace("\u2019", "'")
    return " ".join(s.split())


def type_key(type_dict: dict[str, Any] | None) -> str | None:
    """Return the JSON Schema ``type`` value from a type dict (or ``None``)."""
    if not type_dict:
        return None
    return type_dict.get("type")


def type_ref(type_dict: dict[str, Any] | None) -> str | None:
    """Return a ``$ref`` from a type dict, at top level or inside array ``items``.

    Works on both Apple's computed type dict and a stored schema property.
    """
    if not isinstance(type_dict, dict):
        return None
    if "$ref" in type_dict:
        return type_dict["$ref"]
    items = type_dict.get("items")
    if isinstance(items, dict):
        return items.get("$ref")
    return None


def enum_slot(entry: dict[str, Any]) -> dict[str, Any]:
    """Return the dict an ``enum`` belongs in (array ``items`` or the entry itself)."""
    if entry.get("type") == "array":
        return entry.setdefault("items", {})
    return entry


def constraint_node(entry: dict[str, Any]) -> dict[str, Any]:
    """Return the node holding an ``enum``/``const``, without creating one.

    Unlike :func:`enum_slot`, this never adds an ``items`` object to an array
    that has none.
    """
    if entry.get("type") == "array":
        items = entry.get("items")
        if isinstance(items, dict):
            return items
    return entry


def enum_constraint(values: list[Any]) -> dict[str, Any]:
    """Return the JSON Schema constraint for a set of allowed values.

    A single allowed value is expressed with ``const`` (an ``enum`` with one
    member means the same thing, but ``const`` says it directly); anything
    else uses ``enum``.
    """
    if not values:
        return {}
    if len(values) == 1:
        return {"const": values[0]}
    return {"enum": list(values)}


def drop_redundant_type(node: dict[str, Any]) -> None:
    """Drop a ``type`` keyword that an ``enum``/``const`` already implies.

    The repo lints its schemas with the ``jsonschema`` CLI, which flags a
    ``type`` alongside an ``enum``/``const`` as an anti-pattern.
    """
    if "enum" in node or "const" in node:
        node.pop("type", None)


def has_invalid_type(node: dict[str, Any]) -> bool:
    """Return True if a schema node carries a type JSON Schema does not have.

    Apple emits a few type names of its own (``byte``, ``data``, ``*``); an
    earlier generation wrote them into the schemas verbatim, which makes those
    files invalid JSON Schema.
    """
    type_value = node.get("type")
    if isinstance(type_value, str) and type_value not in JSON_SCHEMA_TYPES:
        return True
    items = node.get("items")
    return isinstance(items, dict) and has_invalid_type(items)


def set_constraint(target: dict[str, Any], values: list[Any]) -> None:
    """Constrain ``target`` to ``values``, replacing any prior constraint."""
    target.pop("enum", None)
    target.pop("const", None)
    target.update(enum_constraint(values))
    drop_redundant_type(target)


def constraint_matches(target: dict[str, Any], values: list[Any]) -> bool:
    """Return True if ``target`` already constrains to exactly ``values``."""
    desired = enum_constraint(values)
    return all(target.get(k) == desired.get(k) for k in ("enum", "const"))


def enum_candidates(codes: list[str]) -> list[str]:
    """Return the code tokens that plausibly form an enumeration.

    A lone code token in prose is a literal mention or an example, not a list
    of allowed values, so the heuristic needs at least two candidates.
    """
    return codes if len(codes) >= 2 else []


def possible_values(doc: JSON) -> list[str] | None:
    """Return the values of a symbol's ``possibleValues`` section, if any.

    Apple documents enum-like string types (e.g. ``ActivationLockType``) as
    ``typealias`` symbols whose render JSON carries a ``possibleValues``
    primary-content section listing the allowed values.
    """
    for sec in doc.get("primaryContentSections", []):
        if sec.get("kind") != "possibleValues":
            continue
        values: list[str] = []
        for v in sec.get("values", []):
            name = v.get("name")
            if name is not None:
                values.append(name)
        return values
    return None


def alias_base_type(doc: JSON) -> str:
    """Return the JSON Schema type underlying a type-alias symbol.

    The alias' declaration (e.g. ``string ActivationLockType``) names the
    aliased type; ``string`` is the only one Apple uses today.
    """
    for sec in doc.get("primaryContentSections", []):
        if sec.get("kind") != "declarations":
            continue
        for decl in sec.get("declarations", []):
            for tok in decl.get("tokens", []):
                text = (tok.get("text") or "").strip().lower()
                if text in SCALAR_MAP:
                    return SCALAR_MAP[text]
                if text in STRING_FORMATS:
                    return "string"
    return "string"


def apple_property_schema(
    ap: dict[str, Any], with_enums: bool = False
) -> dict[str, Any]:
    """Build a JSON Schema property object from Apple's extracted property."""
    entry: dict[str, Any] = dict(ap["type"]) if ap["type"] else {}
    if ap["description"]:
        entry["description"] = ap["description"]
    if ap["datetime_hint"]:
        entry["format"] = "date-time"
    if ap["allowedValues"]:
        # Structured enum from `attributes[].allowedValues` — authoritative.
        # For arrays, the enum constrains the element type, not the array.
        set_constraint(enum_slot(entry), list(ap["allowedValues"]))
    elif with_enums and enum_candidates(ap["codeVoice"]) and not type_ref(ap["type"]):
        # Heuristic enum from prose `codeVoice` tokens — noisy by design,
        # intended only for review (see extract_apple_properties). A `$ref`
        # property is skipped: the referenced schema owns its own constraint,
        # and a sibling enum would have to agree with it.
        set_constraint(enum_slot(entry), enum_candidates(ap["codeVoice"]))
    return entry


def apply_type(
    sp: dict[str, Any], ap_type: dict[str, Any] | None, force: bool = False
) -> None:
    """Overwrite a schema property's type (and items/$ref) from Apple's type.

    In ``force`` mode, also drop hand-curated shape keywords that conflict with
    the Apple type (e.g. an inline ``additionalProperties``/``properties`` object
    being replaced by a ``$ref``, or vice versa).
    """
    if ap_type is None:
        return
    if "$ref" in ap_type:
        sp["$ref"] = ap_type["$ref"]
        sp.pop("type", None)
        sp.pop("items", None)
        if force:
            sp.pop("additionalProperties", None)
            sp.pop("properties", None)
    else:
        if "type" in ap_type:
            sp["type"] = ap_type["type"]
        else:
            # An unconstrained type ("any"): drop the type keywords.
            sp.pop("type", None)
        # Carry any further type keywords (e.g. `contentEncoding`).
        for key, value in ap_type.items():
            if key not in ("type", "items"):
                sp[key] = value
        if "items" in ap_type:
            sp["items"] = ap_type["items"]
        else:
            sp.pop("items", None)
        if force:
            sp.pop("$ref", None)
            sp.pop("additionalProperties", None)
            sp.pop("properties", None)


def schema_files(path: str) -> list[str]:
    """Return the JSON schema files under a file or directory (recursive)."""
    if os.path.isfile(path):
        return [path]
    result: list[str] = []
    for root, _dirs, files in os.walk(path):
        for f in files:
            if f.endswith(".json") and not f.startswith("."):
                result.append(os.path.join(root, f))
    return sorted(result)


def endpoint_types(doc: JSON) -> dict[str, str | None]:
    """Return ``{type_name: url}`` for an endpoint's request/response dictionaries."""
    refs = doc.get("references", {})
    result: dict[str, str | None] = {}
    for sec in doc.get("primaryContentSections", []):
        kind = sec.get("kind")
        if kind == "restBody":
            type_list = sec.get("bodyContentType", [])
        elif kind == "restResponses":
            type_list = []
            for it in sec.get("items", []):
                type_list.extend(it.get("type", []))
        else:
            continue
        for t in type_list:
            if t.get("kind") == "typeIdentifier":
                name = t.get("text")
                if not name:
                    continue
                ident = t.get("identifier")
                result[name] = refs.get(ident, {}).get("url") if ident else None
    return result


def property_type_refs(doc: JSON) -> dict[str, str | None]:
    """Return ``{type_name: url}`` for nested property types (dotted or not)."""
    refs = doc.get("references", {})
    result: dict[str, str | None] = {}
    for sec in doc.get("primaryContentSections", []):
        if sec.get("kind") != "properties":
            continue
        for it in sec.get("items", []):
            for t in it.get("type", []):
                if t.get("kind") != "typeIdentifier":
                    continue
                name = t.get("text")
                if not name:
                    continue
                ident = t.get("identifier")
                result[name] = refs.get(ident, {}).get("url") if ident else None
    return result


def collect_schema_items(
    root_path: str,
    cache_dir: str | None,
    cached_only: bool,
) -> dict[str, str | None]:
    """Walk Apple's nav from a root doc path, returning ``{name: url}``.

    Collects every schema type reachable via the navigation tree, the endpoint
    request/response dictionaries, and nested property types.
    """
    items: dict[str, str | None] = {}
    seen: set[str] = set()
    work: list[str] = [root_path]

    while work:
        path = work.pop()
        if path in seen:
            continue
        seen.add(path)
        url = doc_path_data_uri(path)
        try:
            doc, _ = fetch_json(url, cache_dir=cache_dir, cached_only=cached_only)
        except urllib.error.HTTPError as e:
            print(f"error: HTTP {e.code}: {url}", file=sys.stderr)
            sys.exit(1)
        except urllib.error.URLError as e:
            print(
                f"error: could not fetch {url}: {e}\n"
                "hint: run the sync once without --cached to populate the cache",
                file=sys.stderr,
            )
            sys.exit(1)
        refs = doc.get("references", {})
        meta = doc.get("metadata", {})
        kind = meta.get("symbolKind")
        title = meta.get("title")

        # A dictionary is itself a schema type (dotted names are nested
        # dictionaries generated as their own files). Enum-like type aliases
        # are schema types too.
        if kind in ("dictionary", "typealias") and title:
            items.setdefault(title, path)

        # Nav traversal: recurse into child symbols, collections, and groups.
        for ts in doc.get("topicSections", []):
            for ident in ts.get("identifiers", []):
                r = refs.get(ident, {})
                url = r.get("url")
                role = r.get("role")
                if url and role in ("symbol", "collection", "collectionGroup"):
                    work.append(url)

        # Endpoints: request/response types are schema types not listed in the nav.
        if kind == "httpRequest":
            for name, url in endpoint_types(doc).items():
                items.setdefault(name, url)
                if url:
                    work.append(url)

        # Dictionaries: nested property types (e.g. SeedBuildToken) are types too.
        if kind == "dictionary":
            for name, url in property_type_refs(doc).items():
                items.setdefault(name, url)
                if url:
                    work.append(url)

    return items


def _load_schema(path: str) -> JSON:
    """Load a JSON Schema file from disk."""
    with open(path) as fh:
        return json.load(fh)


def write_schema(path: str, schema: JSON) -> None:
    """Write a JSON Schema to disk (creating parent directories as needed).

    The output is plain JSON with two-space indentation; the repo's canonical
    formatting is applied afterwards with the ``jsonschema fmt`` command (the
    CI checks it with ``jsonschema fmt -c``).
    """
    dirname = os.path.dirname(path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    with open(path, "w") as fh:
        json.dump(schema, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def generate_schema(doc: JSON, data_uri: str) -> JSON:
    """Generate a fresh JSON Schema from a tutorial JSON document."""
    meta = doc.get("metadata", {})
    refs = doc.get("references", {})
    abstract, _ = inline_tokens_to_commonmark(doc.get("abstract", []), refs)

    api_uri = None
    variants = doc.get("variants", [])
    if variants and variants[0].get("paths"):
        api_uri = "https://developer.apple.com" + variants[0]["paths"][0]

    schema: JSON = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": meta.get("title"),
    }
    if abstract:
        schema["description"] = abstract
    if api_uri:
        schema["x-apple-developer-api-uri"] = api_uri
    schema["x-apple-developer-data-uri"] = data_uri

    values = possible_values(doc)
    if values is not None:
        # Enum-like type alias (e.g. `ActivationLockType`): the alias is a
        # string constrained to the values Apple lists as possible.
        if values:
            set_constraint(schema, values)
        else:
            schema["type"] = alias_base_type(doc)
        return schema

    map_value = map_value_schema(doc)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, ap in extract_apple_properties(doc).items():
        properties[name] = apple_property_schema(ap)
        if ap["required"]:
            required.append(name)

    schema["type"] = "object"
    if map_value is not None:
        # Map placeholder ("Any Key"): the schema is a map of arbitrary keys,
        # not a named-properties object.
        schema["additionalProperties"] = map_value
    else:
        if properties:
            schema["properties"] = properties
        if required:
            schema["required"] = required

    return schema


def merge_alias(schema: JSON, doc: JSON, values: list[str]) -> bool:
    """Merge an enum-like type alias into a schema; return True if changed.

    The alias' schema is the aliased type (a string) constrained to Apple's
    possible values. Any leftover object shape from an earlier generation
    (e.g. an empty ``properties`` object) is dropped.
    """
    changed = False
    if values:
        if not constraint_matches(schema, values):
            set_constraint(schema, values)
            changed = True
        # An enum/const implies the type of its values; the repo's linter
        # rejects a `type` keyword alongside them.
        if "type" in schema:
            del schema["type"]
            changed = True
    else:
        base_type = alias_base_type(doc)
        if schema.get("type") != base_type:
            schema["type"] = base_type
            changed = True
    for key in ("properties", "required", "additionalProperties"):
        if key in schema:
            del schema[key]
            changed = True
    return changed


def merge_schema(
    schema: JSON,
    doc: JSON,
    data_uri: str,
    with_enums: bool = False,
    force: bool = False,
) -> JSON:
    """Mutate a loaded schema in place from Apple's doc; return a summary.

    Additive/overwrite only: adds new properties, overwrites title, description,
    type, structured enum, and bare sibling ``$ref`` values (but never JSON
    pointers or absolute URIs); never removes properties or hand-curated
    ``required`` entries (some namespaces report ``required: false`` for every
    property, so the repo's hand-derived lists must be preserved). Apple's
    structured ``required`` flags are added to ``required`` but never dropped.

    Map placeholders (``Any Key`` dictionaries) are merged as
    ``additionalProperties`` maps rather than named-properties objects.

    Enum-like type aliases (e.g. ``ActivationLockType``) are merged as a
    string constrained to Apple's ``possibleValues`` — replacing the empty
    object shape earlier generations produced for them.

    With ``force``, existing properties' type shapes are overwritten from Apple
    unconditionally — converting inline objects to ``$ref``s (and vice versa),
    realigning array ``items``, etc. — so a sync can be used to audit hand-crafted
    overrides against Apple's current types.
    """
    apple_props = extract_apple_properties(doc)
    apple_title = doc.get("metadata", {}).get("title")
    apple_description, _ = inline_tokens_to_commonmark(
        doc.get("abstract", []), doc.get("references", {})
    )

    summary: JSON = {}
    if apple_title and schema.get("title") != apple_title:
        schema["title"] = apple_title
        summary["title_changed"] = True
    if apple_description and norm(schema.get("description")) != norm(apple_description):
        schema["description"] = apple_description
        summary["description_changed"] = True
    if schema.get("x-apple-developer-data-uri") != data_uri:
        schema["x-apple-developer-data-uri"] = data_uri
        summary["x_apple_data_uri_changed"] = True

    alias_values = possible_values(doc)
    if alias_values is not None:
        # Enum-like type alias: the schema is the aliased type constrained to
        # Apple's possible values.
        if merge_alias(schema, doc, alias_values):
            summary["alias_changed"] = True
        return summary

    map_value = map_value_schema(doc)
    if map_value is not None:
        # Map placeholder ("Any Key"): the schema is a map of arbitrary keys,
        # not a named-properties object.
        schema["type"] = "object"
        if schema.get("additionalProperties") != map_value:
            schema["additionalProperties"] = map_value
            summary["map_value_changed"] = True
        # Drop a stale named-properties shape (e.g. a previously generated
        # "Any Key" property).
        for key in ("properties", "required"):
            if key in schema:
                del schema[key]
                summary["map_value_changed"] = True
        return summary

    had_properties = "properties" in schema
    props = schema.setdefault("properties", {})
    added: list[str] = []
    type_changed: list[str] = []
    desc_changed: list[str] = []
    enum_changed: list[str] = []
    ref_changed: list[str] = []
    for pname, ap in apple_props.items():
        if pname not in props:
            props[pname] = apple_property_schema(ap, with_enums=with_enums)
            added.append(pname)
        else:
            sp = props[pname]
            shape_keys = ("type", "items", "$ref", "additionalProperties", "properties")
            before = {k: sp.get(k) for k in shape_keys}
            if force and ap["type"] is not None:
                # Force the type shape to match Apple exactly, dropping
                # hand-crafted inline objects / stale `$ref`s.
                apply_type(sp, ap["type"], force=True)
            else:
                at = type_key(ap["type"])
                st = sp.get("type")
                if (at and st and at != st) or has_invalid_type(sp):
                    # Apple's type differs, or the schema carries a type that
                    # JSON Schema does not have (e.g. Apple's `byte`/`*`).
                    apply_type(sp, ap["type"])
                # `$ref`: sync bare sibling refs from Apple's type (adds `.json`,
                # follows renames). JSON pointers (`#/...`) and absolute URIs are
                # hand-curated conventions (`$defs`, external URLs) — never touch.
                ap_ref = type_ref(ap["type"])
                sp_ref = type_ref(sp)
                if (
                    ap_ref
                    and sp_ref
                    and not sp_ref.startswith("#")
                    and "://" not in sp_ref
                    and sp_ref != ap_ref
                ):
                    apply_type(sp, ap["type"])
            if ap["description"] and norm(sp.get("description")) != norm(
                ap["description"]
            ):
                sp["description"] = ap["description"]
                desc_changed.append(pname)
            # Structured enum (`attributes[].allowedValues`) is authoritative;
            # overwrite like `type`. A single allowed value is written as a
            # `const` rather than a one-member `enum`.
            candidates = enum_candidates(ap["codeVoice"])
            if ap["allowedValues"]:
                slot = enum_slot(sp)
                if not constraint_matches(slot, ap["allowedValues"]):
                    set_constraint(slot, list(ap["allowedValues"]))
                    enum_changed.append(pname)
            # `--enums`: mix in the heuristic enum (raw `codeVoice` tokens) for
            # review only, where no structured enum exists. A `$ref` property
            # is skipped: the referenced schema owns its own constraint.
            elif with_enums and candidates and not type_ref(ap["type"]):
                slot = enum_slot(sp)
                if not constraint_matches(slot, candidates):
                    set_constraint(slot, candidates)
                    enum_changed.append(pname)
            # An enum/const implies the type of its values; the repo's linter
            # rejects a `type` keyword alongside them.
            drop_redundant_type(constraint_node(sp))
            # The shape is compared once everything is applied: a forced type
            # that the enum then implies is not a change.
            after = {k: sp.get(k) for k in shape_keys}
            if before != after:
                if before.get("$ref") != after.get("$ref"):
                    ref_changed.append(pname)
                else:
                    type_changed.append(pname)

    # `required`: additive union. Never drop hand-curated entries (some
    # namespaces, e.g. DEP, report `required: false` for every property), but
    # add Apple-declared required properties.
    existing_required: list[str] = list(schema.get("required") or [])
    existing_set = set(existing_required)
    required_added: list[str] = [
        p for p in apple_props if apple_props[p]["required"] and p not in existing_set
    ]
    if required_added:
        schema["required"] = existing_required + required_added

    # An empty `properties` object adds no constraint (the repo's linter
    # flags it) — drop it once we know the type has no properties.
    if not props and had_properties:
        del schema["properties"]
        summary["empty_properties_removed"] = True

    # Repo-only properties (present in the schema but not Apple's docs) are
    # kept, not removed — surfaced here since `git diff` won't show them.
    kept: list[str] = [p for p in props if p not in apple_props]

    if added:
        summary["added"] = added
    if type_changed:
        summary["type_changed"] = type_changed
    if desc_changed:
        summary["property_description_changed"] = desc_changed
    if enum_changed:
        summary["enum_changed"] = enum_changed
    if ref_changed:
        summary["ref_changed"] = ref_changed
    if required_added:
        summary["required_added"] = required_added
    if kept:
        summary["kept_repo_only"] = kept
    return summary


_CHANGE_KEYS = (
    "title_changed",
    "description_changed",
    "x_apple_data_uri_changed",
    "added",
    "type_changed",
    "property_description_changed",
    "enum_changed",
    "alias_changed",
    "ref_changed",
    "required_added",
    "map_value_changed",
    "empty_properties_removed",
)


def _has_changes(summary: JSON) -> bool:
    """Return True if a merge summary indicates any actual schema change."""
    return any(k in summary for k in _CHANGE_KEYS)


def _is_outlier(value: Any) -> bool:
    """Return True if a sync result is worth reporting (not a clean merge)."""
    if not isinstance(value, dict):
        return True  # e.g. the "_extras" list
    if value.get("action") != "merged":
        return True  # generated / would-generate / error / skip
    return _has_changes(value)


def sync_file(path: str, args: argparse.Namespace) -> JSON:
    """Sync a single schema file in place; return a summary dict."""
    schema = _load_schema(path)
    api_uri = schema.get("x-apple-developer-api-uri")
    data_uri = derive_data_uri(api_uri) if api_uri else None
    if not data_uri:
        return {"skip": f"no data URI derivable from: {api_uri}"}
    try:
        doc, fetched_at = fetch_json(
            data_uri, cache_dir=args.cache_dir, cached_only=args.cached
        )
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {data_uri}"}
    except urllib.error.URLError as e:
        return {"error": f"fetch failed: {e}"}
    changes = merge_schema(
        schema, doc, data_uri, with_enums=args.enums, force=args.force_types
    )
    summary = dict(changes)
    summary["action"] = "merged"
    summary["dry_run"] = args.dry_run
    summary["fetched_at"] = fetched_at
    if not args.dry_run and _has_changes(changes):
        write_schema(path, schema)
    return summary


def index_diff(
    root_path: str, outdir: str, args: argparse.Namespace
) -> tuple[dict[str, str | None], list[str]]:
    """Return (missing, extras) between a nav root's index and a schema dir.

    ``missing`` maps type names (in the index but not on disk) to their doc
    URLs; ``extras`` lists on-disk files not present in the index.
    """
    items = collect_schema_items(root_path, args.cache_dir, args.cached)
    on_disk = {os.path.basename(f) for f in schema_files(outdir)}
    missing = {
        name: url for name, url in items.items() if name + ".json" not in on_disk
    }
    extras = sorted(on_disk - {name + ".json" for name in items})
    return missing, extras


def derive_namespace(doc_paths: list[str]) -> str | None:
    """Derive the common ``/documentation/<namespace>`` prefix from doc paths."""
    prefixes = {
        "/".join(p.split("/")[:3]) for p in doc_paths if p.startswith("/documentation/")
    }
    return prefixes.pop() if len(prefixes) == 1 else None


def namespace_sections(
    namespace_root: str, cache_dir: str | None, cached_only: bool
) -> dict[str, str]:
    """Return ``{type_name: group_path}`` across a documentation namespace.

    Reads each ``collection``/``collectionGroup`` listed by the namespace
    landing page and maps its *direct* symbol titles back to that group. Direct
    symbols are enough to identify the home group; the full nested inventory is
    fetched later by ``index_diff``.
    """
    try:
        doc, _ = fetch_json(
            doc_path_data_uri(namespace_root),
            cache_dir=cache_dir,
            cached_only=cached_only,
        )
    except (urllib.error.HTTPError, urllib.error.URLError):
        return {}
    refs = doc.get("references", {})
    sections: dict[str, str] = {}
    for ts in doc.get("topicSections", []):
        for ident in ts.get("identifiers", []):
            r = refs.get(ident, {})
            if r.get("role") not in ("collection", "collectionGroup") or not r.get(
                "url"
            ):
                continue
            group = r["url"]
            try:
                gdoc, _ = fetch_json(
                    doc_path_data_uri(group),
                    cache_dir=cache_dir,
                    cached_only=cached_only,
                )
            except (urllib.error.HTTPError, urllib.error.URLError):
                continue
            grefs = gdoc.get("references", {})
            for gts in gdoc.get("topicSections", []):
                for gident in gts.get("identifiers", []):
                    gr = grefs.get(gident, {})
                    if gr.get("role") == "symbol" and gr.get("title"):
                        sections.setdefault(gr["title"], group)
    return sections


def derive_home_root(outdir: str, args: argparse.Namespace) -> str | None:
    """Derive the collection group most on-disk schemas belong to, or None."""
    on_disk_names = {
        os.path.basename(f).removesuffix(".json") for f in schema_files(outdir)
    }
    api_uris = []
    for f in schema_files(outdir):
        schema = _load_schema(f)
        if schema.get("x-apple-developer-api-uri"):
            api_uris.append(schema["x-apple-developer-api-uri"])
    doc_paths = [p for p in (resolve_doc_path(u) for u in api_uris) if p]
    namespace = derive_namespace(doc_paths)
    if not namespace:
        return None
    sections = namespace_sections(namespace, args.cache_dir, args.cached)
    counts: dict[str, int] = {}
    for name in on_disk_names:
        if name in sections:
            group = sections[name]
            counts[group] = counts.get(group, 0) + 1
    return max(counts, key=counts.get) if counts else None


def generate_one(
    name: str,
    url: str | None,
    outdir: str,
    args: argparse.Namespace,
    results: JSON,
) -> None:
    """Generate a single missing type into ``outdir``, recording into ``results``."""
    filename = name + ".json"
    if not url:
        results[filename] = {"error": f"no URL for type: {name}"}
        return
    data_uri = doc_path_data_uri(url)
    try:
        doc, fetched_at = fetch_json(
            data_uri, cache_dir=args.cache_dir, cached_only=args.cached
        )
    except urllib.error.HTTPError as e:
        results[filename] = {"error": f"HTTP {e.code}: {data_uri}"}
        return
    except urllib.error.URLError as e:
        results[filename] = {"error": f"fetch failed: {e}"}
        return
    if args.dry_run:
        results[filename] = {
            "action": "would-generate",
            "dry_run": True,
            "fetched_at": fetched_at,
        }
    else:
        write_schema(os.path.join(outdir, filename), generate_schema(doc, data_uri))
        results[filename] = {
            "action": "generated",
            "dry_run": False,
            "fetched_at": fetched_at,
        }


def cmd_sync(args: argparse.Namespace) -> None:
    """Sync schemas from Apple's docs, based on the target's shape."""
    target = args.target
    fmt_target: str | None = None

    if os.path.isfile(target):
        results = {os.path.basename(target): sync_file(target, args)}
        fmt_target = target
    elif os.path.isdir(target):
        results = {}
        fmt_target = target
        for f in schema_files(target):
            results[os.path.basename(f)] = sync_file(f, args)
        # Optional index comparison: flag missing/extra (and, with --generate,
        # create missing) against a nav root — given explicitly, or derived
        # from the on-disk schemas' shared documentation namespace.
        root_path = None
        if args.root:
            root_path = resolve_doc_path(args.root)
            if not root_path:
                print(f"error: could not resolve root: {args.root}", file=sys.stderr)
                sys.exit(1)
        else:
            root_path = derive_home_root(target, args)
        if root_path:
            missing, extras = index_diff(root_path, target, args)
            if missing:
                if args.generate:
                    for name, url in sorted(missing.items()):
                        generate_one(name, url, target, args, results)
                else:
                    results["_missing"] = {
                        name: url for name, url in sorted(missing.items())
                    }
            if extras:
                results["_extras"] = extras
    else:
        # A type URL/doc-path: generate a single schema (stdout unless outdir).
        doc_path = resolve_doc_path(target)
        if not doc_path:
            print(f"error: could not resolve target: {target}", file=sys.stderr)
            sys.exit(1)
        data_uri = doc_path_data_uri(doc_path)
        try:
            doc, fetched_at = fetch_json(
                data_uri, cache_dir=args.cache_dir, cached_only=args.cached
            )
        except urllib.error.HTTPError as e:
            print(f"error: HTTP {e.code}: {data_uri}", file=sys.stderr)
            sys.exit(1)
        except urllib.error.URLError as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)
        kind = doc.get("metadata", {}).get("symbolKind")
        if kind not in ("dictionary", "httpRequest", "typealias"):
            print(
                f"error: {target} is a nav root, not a type; "
                "use --root with a schema directory",
                file=sys.stderr,
            )
            sys.exit(1)
        if not args.outdir:
            print(
                json.dumps(generate_schema(doc, data_uri), indent=2, ensure_ascii=False)
            )
            return
        filename = (doc.get("metadata", {}).get("title") or "Untitled") + ".json"
        out = os.path.join(args.outdir, filename)
        fmt_target = out
        if args.dry_run:
            results = {
                filename: {
                    "action": "would-generate",
                    "dry_run": True,
                    "fetched_at": fetched_at,
                }
            }
        else:
            write_schema(out, generate_schema(doc, data_uri))
            results = {
                filename: {
                    "action": "generated",
                    "dry_run": False,
                    "fetched_at": fetched_at,
                }
            }

    # Only report outliers; clean merges (no changes) are silent.
    results = {k: v for k, v in results.items() if _is_outlier(v)}
    print(json.dumps(results, indent=2, ensure_ascii=False))

    if args.fmt and fmt_target and os.path.exists(fmt_target):
        # Formatting is a convenience step: the repo's canonical layout comes
        # from the `jsonschema` formatter. Never fail the run over it.
        print(f"jsonschema fmt: {format_schemas(fmt_target)}", file=sys.stderr)


def format_schemas(path: str) -> str:
    """Format schema files with the ``jsonschema`` CLI's formatter.

    Returns a short note describing the outcome. Formatting is a convenience
    step: a missing or failing formatter never fails the sync.
    """
    binary = shutil.which("jsonschema")
    if not binary:
        return "skipped (the `jsonschema` formatter is not installed)"
    try:
        proc = subprocess.run(
            [binary, "fmt", path], capture_output=True, text=True, check=False
        )
    except OSError as e:
        return f"skipped ({e})"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        return f"failed ({detail[-1] if detail else 'unknown error'})"
    return "formatted"


def add_cache_args(sub: argparse.ArgumentParser) -> None:
    """Add the shared cache-related arguments to a subcommand parser."""
    sub.add_argument(
        "--cached",
        action="store_true",
        help="use cached copies only; do not hit the network",
    )
    sub.add_argument(
        "--cache-dir",
        default=os.path.expanduser("~/.cache/apple_schema"),
        help="cache directory (default: ~/.cache/apple_schema)",
    )


def main() -> None:
    """Parse CLI arguments and dispatch to the sync command."""
    parser = argparse.ArgumentParser(prog="apple_schema.py")
    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser("sync", help="generate/merge schemas from Apple's docs")
    s.add_argument("target", help="schema file/dir, or a type URL/doc-path")
    s.add_argument("outdir", nargs="?", help="output directory for type generation")
    s.add_argument(
        "--root",
        help="nav root URL/doc-path to compare a schema directory against (flags missing/extra)",
    )
    s.add_argument(
        "--generate",
        action="store_true",
        help="with --root, also generate the missing schemas",
    )
    s.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without writing",
    )
    s.add_argument(
        "--enums",
        action="store_true",
        help="also mix in enum candidates from codeVoice tokens where no structured enum exists (noisy; review via git diff)",
    )
    s.add_argument(
        "--force-types",
        action="store_true",
        help="overwrite existing properties' type shapes from Apple (converts inline objects to $refs and vice versa); useful for auditing hand-crafted overrides",
    )
    s.add_argument(
        "--fmt",
        action="store_true",
        help="format the synced schemas with the `jsonschema` CLI's formatter (skipped if it is not installed)",
    )
    add_cache_args(s)
    s.set_defaults(func=cmd_sync)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
