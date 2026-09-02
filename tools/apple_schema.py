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
    --cached        use cached copies only; do not hit the network
    --cache-dir DIR cache directory (default: ~/.cache/apple_schema)
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

# Apple type names that are JSON Schema string formats, not types. They appear
# as bare ``text`` type tokens (e.g. ``uri-reference``) and must map to
# ``type: string`` plus the matching ``format``.
STRING_FORMATS: set[str] = {
    "uri-reference",
    "date",
    "date-time",
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
    if ref and inner == ref:
        elem: dict[str, Any] = {"$ref": ref}
    elif inner in STRING_FORMATS:
        elem = {"type": "string", "format": inner}
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
            # best-effort and must be reviewed (see `sync --enums`).
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


def write_schema(path: str, schema: JSON) -> None:
    """Write a JSON Schema to disk (creating parent directories as needed)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(schema, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def generate_schema(doc: JSON, data_uri: str) -> JSON:
    """Generate a fresh JSON Schema from a tutorial JSON document."""
    meta = doc.get("metadata", {})
    refs = doc.get("references", {})
    abstract, _ = inline_tokens_to_commonmark(doc.get("abstract", []), refs)

    properties: dict[str, Any] = {}
    for name, ap in extract_apple_properties(doc).items():
        properties[name] = apple_property_schema(ap)

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


def merge_schema(
    schema: JSON, doc: JSON, data_uri: str, with_enums: bool = False
) -> JSON:
    """Mutate a loaded schema in place from Apple's doc; return a summary.

    Additive/overwrite only: adds new properties, overwrites title, description,
    type, and (optionally) enum; never removes properties, ``required``, or
    other curated fields.
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

    props = schema.setdefault("properties", {})
    added: list[str] = []
    type_changed: list[str] = []
    desc_changed: list[str] = []
    enum_changed: list[str] = []
    for pname, ap in apple_props.items():
        if pname not in props:
            props[pname] = apple_property_schema(ap, with_enums=with_enums)
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
            # `--enums`: mix in the heuristic enum (raw `codeVoice` tokens) for
            # review only — noisy by design.
            if with_enums and ap["codeVoice"] and sp.get("enum") != ap["codeVoice"]:
                sp["enum"] = ap["codeVoice"]
                enum_changed.append(pname)

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
    changes = merge_schema(schema, doc, data_uri, with_enums=args.enums)
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

    if os.path.isfile(target):
        results = {os.path.basename(target): sync_file(target, args)}
    elif os.path.isdir(target):
        results = {}
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
        if kind not in ("dictionary", "httpRequest"):
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
        help="also mix in enum candidates from codeVoice tokens (noisy; review via git diff)",
    )
    add_cache_args(s)
    s.set_defaults(func=cmd_sync)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
