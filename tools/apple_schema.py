#!/usr/bin/env python3
"""Convert Apple's tutorial JSON to/against local JSON Schemas.

Subcommands:
    diff      diff Apple's tutorial JSON against local schema file(s)
    merge     overwrite/add schema fields from Apple JSON (additive only)
    generate  generate a fresh JSON Schema from an Apple tutorial JSON page
    difftree  check Apple's nav/index against on-disk schema files

Usage:
    python3 apple_schema.py diff <schema-or-dir>
    python3 apple_schema.py merge <schema-or-dir> [--dry-run] [--with-enums]
    python3 apple_schema.py generate <api-uri | doc-path | data-uri>
    python3 apple_schema.py difftree <root> <schema-dir>

The documentation page URL (``x-apple-developer-api-uri``) maps to a
machine-readable mirror at
``https://developer.apple.com/tutorials/data/documentation/<path>.json`` by
replacing ``developer.apple.com/documentation/`` with
``developer.apple.com/tutorials/data/documentation/`` and appending ``.json``.
"""

from __future__ import annotations

import argparse
import json
import os
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


def derive_data_uri(api_uri: str) -> str | None:
    """Derive the tutorial JSON mirror URL from a documentation page URL."""
    if "developer.apple.com/documentation/" not in api_uri:
        return None
    return (
        api_uri.replace(
            "developer.apple.com/documentation/",
            "developer.apple.com/tutorials/data/documentation/",
        )
        + ".json"
    )


def resolve_data_uri(ref: str) -> str | None:
    """Resolve a data URI from an api-uri, a doc path, or a data URI."""
    if ref.startswith("https://developer.apple.com/tutorials/data/"):
        return ref
    if ref.startswith("https://developer.apple.com/documentation/"):
        return derive_data_uri(ref)
    if ref.startswith("/documentation/"):
        return derive_data_uri("https://developer.apple.com" + ref)
    return derive_data_uri(
        "https://developer.apple.com/documentation/" + ref.lstrip("/")
    )


def cache_file(cache_dir: str, url: str) -> str:
    """Map a data URI to a cache file path (mirrors the doc path)."""
    rest = url.split("/", 3)[3]  # e.g. "tutorials/data/documentation/...json"
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
    re-downloading unchanged content, store the ETag + Last-Modified from the
    response alongside the cached JSON (a sidecar or a small metadata file),
    then issue a conditional GET (If-None-Match / If-Modified-Since) and reuse
    the cached copy on 304 Not Modified.
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

    - ``text``              -> plain text
    - ``codeVoice``/``code`` -> backticked code span
    - ``reference``         -> ``[title](url)`` resolved via ``references``

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
    if ref and inner == ref:
        elem: dict[str, Any] = {"$ref": ref}
    else:
        elem = {"type": SCALAR_MAP.get(inner, inner)}
    if is_array:
        return {"type": "array", "items": elem}, ref
    if ref:
        return {"$ref": ref}, ref
    return elem, None


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
            # NOTE: Apple's JSON has NO structured `enum` field. `codeVoice`
            # (the `codes` list) is a heuristic: every `codeVoice`/`code` token
            # from the description prose, collected regardless of whether it is
            # an enum literal or inline code. Any `enum` derived from it is
            # best-effort and must be reviewed (see `merge --with-enums`).
            props[name] = {
                "type": type_dict,
                "ref": ref,
                "description": desc,
                "codeVoice": codes,
                "datetime_hint": "ISO 8601" in desc,
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


def diff(
    schema: JSON,
    apple_props: dict[str, dict[str, Any]],
) -> tuple[list[tuple[str, dict[str, Any]]], list[str], list[dict[str, Any]]]:
    """Compare a schema's properties against Apple's, returning deltas.

    Returns ``(additions, removals, drift)`` where additions are new property
    names, removals are repo-only property names, and drift is a list of
    ``{kind, property, schema, apple}`` entries.
    """
    additions: list[tuple[str, dict[str, Any]]] = []
    removals: list[str] = []
    drift: list[dict[str, Any]] = []
    schema_props = schema.get("properties", {})
    for name, ap in apple_props.items():
        if name not in schema_props:
            additions.append((name, ap))
            continue
        sp = schema_props[name]
        at = type_key(ap["type"])
        st = sp.get("type")
        if at and st and at != st:
            drift.append(
                {
                    "kind": "type",
                    "property": name,
                    "schema": sp.get("type"),
                    "apple": at,
                }
            )
        if ap["description"] and norm(sp.get("description")) != norm(ap["description"]):
            drift.append(
                {
                    "kind": "description",
                    "property": name,
                    "schema": sp.get("description"),
                    "apple": ap["description"],
                }
            )
        # enum is a heuristic (see extract_apple_properties): only report drift
        # against an existing enum, never propose an enum from scratch.
        if (
            sp.get("enum")
            and ap["codeVoice"]
            and set(sp["enum"]) != set(ap["codeVoice"])
        ):
            drift.append(
                {
                    "kind": "enum",
                    "property": name,
                    "schema": sp.get("enum"),
                    "apple": ap["codeVoice"],
                }
            )
    for name in schema_props:
        if name not in apple_props:
            removals.append(name)
    return additions, removals, drift


def apple_property_schema(
    ap: dict[str, Any], with_enums: bool = False
) -> dict[str, Any]:
    """Build a JSON Schema property object from Apple's extracted property."""
    entry: dict[str, Any] = dict(ap["type"]) if ap["type"] else {}
    if ap["description"]:
        entry["description"] = ap["description"]
    if ap["datetime_hint"]:
        entry["format"] = "date-time"
    if with_enums and ap["codeVoice"]:
        # Heuristic enum from prose `codeVoice` tokens — noisy by design,
        # intended only for review (see extract_apple_properties).
        entry["enum"] = ap["codeVoice"]
    return entry


def apply_type(sp: dict[str, Any], ap_type: dict[str, Any] | None) -> None:
    """Overwrite a schema property's type (and items/$ref) from Apple's type."""
    if ap_type is None:
        return
    if "$ref" in ap_type:
        sp["$ref"] = ap_type["$ref"]
        sp.pop("type", None)
        sp.pop("items", None)
    else:
        sp["type"] = ap_type["type"]
        if "items" in ap_type:
            sp["items"] = ap_type["items"]
        else:
            sp.pop("items", None)


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
                if not name or "." in name:
                    # dotted names are sub-objects ($defs/inlined), not files
                    continue
                ident = t.get("identifier")
                result[name] = refs.get(ident, {}).get("url") if ident else None
    return result


def property_type_refs(doc: JSON) -> dict[str, str | None]:
    """Return ``{type_name: url}`` for nested property types (non-dotted names)."""
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
                if not name or "." in name:
                    # dotted names are sub-objects ($defs/inlined), not files
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
        doc, _ = fetch_json(
            doc_path_data_uri(path), cache_dir=cache_dir, cached_only=cached_only
        )
        refs = doc.get("references", {})
        meta = doc.get("metadata", {})
        kind = meta.get("symbolKind")
        title = meta.get("title")

        # A dictionary is itself a schema type (dotted names are inlined sub-objects).
        if kind == "dictionary" and title and "." not in title:
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


def cmd_diff(args: argparse.Namespace) -> None:
    """Print per-schema deltas between local schemas and Apple's JSON."""
    deltas: dict[str, Any] = {}
    for f in schema_files(args.target):
        name = os.path.basename(f)
        schema = _load_schema(f)
        api_uri = schema.get("x-apple-developer-api-uri")
        schema_data_uri = schema.get("x-apple-developer-data-uri")
        missing_x_apple = []
        if not api_uri:
            missing_x_apple.append("x-apple-developer-api-uri")
        if not schema_data_uri:
            missing_x_apple.append("x-apple-developer-data-uri")

        data_uri = derive_data_uri(api_uri) if api_uri else None

        if not data_uri:
            entry: dict[str, Any] = {"missing_x_apple": missing_x_apple}
            if api_uri:
                entry["skip"] = f"no data URI derivable from: {api_uri}"
            deltas[name] = entry
            continue

        try:
            doc, fetched_at = fetch_json(
                data_uri, cache_dir=args.cache_dir, cached_only=args.cached
            )
        except urllib.error.HTTPError as e:
            deltas[name] = {"error": f"HTTP {e.code}: {data_uri}"}
            continue
        except urllib.error.URLError as e:
            deltas[name] = {"error": f"fetch failed: {e}"}
            continue

        apple_title = doc.get("metadata", {}).get("title")
        apple_props = extract_apple_properties(doc)
        additions, removals, drift = diff(schema, apple_props)

        additions_out: dict[str, Any] = {}
        for pname, ap in additions:
            snippet = dict(ap["type"]) if ap["type"] else {}
            if ap["description"]:
                snippet["description"] = ap["description"]
            if ap["datetime_hint"]:
                snippet["format"] = "date-time"
            if ap["codeVoice"]:
                snippet["x-enum-candidates"] = ap["codeVoice"]
            additions_out[pname] = snippet

        refs = doc.get("references", {})
        apple_description, _ = inline_tokens_to_commonmark(
            doc.get("abstract", []), refs
        )

        title_mismatch = schema.get("title") != apple_title
        description_mismatch = norm(schema.get("description")) != norm(
            apple_description
        )
        data_uri_mismatch = bool(schema_data_uri and schema_data_uri != data_uri)

        entry = {
            "fetched_at": fetched_at,
            "title_mismatch": title_mismatch,
            "description_mismatch": description_mismatch,
            "x_apple_data_uri_mismatch": data_uri_mismatch,
        }

        if missing_x_apple:
            entry["missing_x_apple"] = missing_x_apple
            if "x-apple-developer-data-uri" in missing_x_apple:
                entry["x_apple_data_uri_derived"] = data_uri

        if title_mismatch:
            entry["title"] = schema.get("title")
            entry["apple_title"] = apple_title

        if description_mismatch:
            entry["description"] = schema.get("description")
            entry["apple_description"] = apple_description

        if data_uri_mismatch:
            entry["x_apple_data_uri_schema"] = schema_data_uri
            entry["x_apple_data_uri_derived"] = data_uri

        entry["additions"] = additions_out
        entry["removals"] = removals
        entry["drift"] = drift

        deltas[name] = entry

    print(json.dumps(deltas, indent=2, ensure_ascii=False))


def cmd_merge(args: argparse.Namespace) -> None:
    """Overwrite/add schema fields in place from Apple's JSON (additive only)."""
    results: dict[str, Any] = {}
    for f in schema_files(args.target):
        name = os.path.basename(f)
        schema = _load_schema(f)
        api_uri = schema.get("x-apple-developer-api-uri")
        data_uri = derive_data_uri(api_uri) if api_uri else None

        if not data_uri:
            results[name] = {"skip": f"no data URI derivable from: {api_uri}"}
            continue

        try:
            doc, fetched_at = fetch_json(
                data_uri, cache_dir=args.cache_dir, cached_only=args.cached
            )
        except urllib.error.HTTPError as e:
            results[name] = {"error": f"HTTP {e.code}: {data_uri}"}
            continue
        except urllib.error.URLError as e:
            results[name] = {"error": f"fetch failed: {e}"}
            continue

        apple_props = extract_apple_properties(doc)
        apple_title = doc.get("metadata", {}).get("title")
        apple_description, _ = inline_tokens_to_commonmark(
            doc.get("abstract", []), doc.get("references", {})
        )

        summary: dict[str, Any] = {"dry_run": args.dry_run, "fetched_at": fetched_at}

        if apple_title and schema.get("title") != apple_title:
            schema["title"] = apple_title
            summary["title_changed"] = True

        if apple_description and norm(schema.get("description")) != norm(
            apple_description
        ):
            schema["description"] = apple_description
            summary["description_changed"] = True

        if schema.get("x-apple-developer-data-uri") != data_uri:
            schema["x-apple-developer-data-uri"] = data_uri
            summary["x_apple_data_uri_changed"] = True

        props = schema.setdefault("properties", {})
        added: list[str] = []
        type_changed: list[str] = []
        desc_changed: list[str] = []
        enum_changed: list[str] = []
        for pname, ap in apple_props.items():
            if pname not in props:
                props[pname] = apple_property_schema(ap, with_enums=args.with_enums)
                added.append(pname)
            else:
                sp = props[pname]
                at = type_key(ap["type"])
                st = sp.get("type")
                if at and st and at != st:
                    apply_type(sp, ap["type"])
                    type_changed.append(pname)
                if ap["description"] and norm(sp.get("description")) != norm(
                    ap["description"]
                ):
                    sp["description"] = ap["description"]
                    desc_changed.append(pname)
                # `--with-enums`: mix in the heuristic enum (raw `codeVoice`
                # tokens) for review only — noisy by design.
                if (
                    args.with_enums
                    and ap["codeVoice"]
                    and sp.get("enum") != ap["codeVoice"]
                ):
                    sp["enum"] = ap["codeVoice"]
                    enum_changed.append(pname)

        if added:
            summary["added"] = added
        if type_changed:
            summary["type_changed"] = type_changed
        if desc_changed:
            summary["property_description_changed"] = desc_changed
        if enum_changed:
            summary["enum_changed"] = enum_changed

        if not args.dry_run:
            with open(f, "w") as fh:
                json.dump(schema, fh, indent=2, ensure_ascii=False)
                fh.write("\n")

        results[name] = summary

    print(json.dumps(results, indent=2, ensure_ascii=False))


def generate_schema(doc: JSON, data_uri: str) -> JSON:
    """Generate a fresh JSON Schema from a tutorial JSON document."""
    meta = doc.get("metadata", {})
    refs = doc.get("references", {})
    abstract, _ = inline_tokens_to_commonmark(doc.get("abstract", []), refs)

    properties: dict[str, Any] = {}
    for name, ap in extract_apple_properties(doc).items():
        entry = apple_property_schema(ap)
        properties[name] = entry

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
    schema["type"] = "object"
    schema["properties"] = properties

    return schema


def cmd_generate(args: argparse.Namespace) -> None:
    """Print a fresh JSON Schema generated from an Apple page."""
    data_uri = resolve_data_uri(args.ref)
    if not data_uri:
        print(f"error: could not derive a data URI from: {args.ref}", file=sys.stderr)
        sys.exit(1)
    try:
        doc, _fetched_at = fetch_json(
            data_uri, cache_dir=args.cache_dir, cached_only=args.cached
        )
    except urllib.error.HTTPError as e:
        print(f"error: HTTP {e.code}: {data_uri}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(generate_schema(doc, data_uri), indent=2, ensure_ascii=False))


def cmd_difftree(args: argparse.Namespace) -> None:
    """Print missing/extra schema files vs Apple's nav index under a root."""
    root_path = resolve_doc_path(args.root)
    if not root_path:
        print(f"error: could not resolve root: {args.root}", file=sys.stderr)
        sys.exit(1)
    try:
        items = collect_schema_items(root_path, args.cache_dir, args.cached)
    except urllib.error.HTTPError as e:
        print(f"error: HTTP {e.code}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    on_disk = {os.path.basename(f) for f in schema_files(args.target)}
    missing = {
        name: url
        for name, url in sorted(items.items())
        if name + ".json" not in on_disk
    }
    extra = sorted(on_disk - {name + ".json" for name in items})

    report: JSON = {
        "root": root_path,
        "missing": missing,
        "extra": extra,
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))


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
    """Parse CLI arguments and dispatch to the requested subcommand."""
    parser = argparse.ArgumentParser(prog="apple_schema.py")
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("diff", help="diff Apple JSON against local schema file(s)")
    d.add_argument("target", help="schema file or directory of schema files")
    add_cache_args(d)
    d.set_defaults(func=cmd_diff)

    m = sub.add_parser(
        "merge", help="overwrite/add schema fields from Apple JSON (additive only)"
    )
    m.add_argument("target", help="schema file or directory of schema files")
    m.add_argument(
        "--dry-run", action="store_true", help="preview changes without writing"
    )
    m.add_argument(
        "--with-enums",
        action="store_true",
        help="also mix in enum values from Apple's codeVoice tokens (noisy; review via git diff)",
    )
    add_cache_args(m)
    m.set_defaults(func=cmd_merge)

    g = sub.add_parser(
        "generate", help="generate a fresh JSON Schema from an Apple page"
    )
    g.add_argument("ref", help="api-uri, doc path, or data URI")
    add_cache_args(g)
    g.set_defaults(func=cmd_generate)

    t = sub.add_parser(
        "difftree", help="check Apple's nav/index against on-disk schema files"
    )
    t.add_argument(
        "root",
        help="root doc path/URL (e.g. /documentation/devicemanagement/device-assignment)",
    )
    t.add_argument("target", help="schema directory to check against")
    add_cache_args(t)
    t.set_defaults(func=cmd_difftree)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
