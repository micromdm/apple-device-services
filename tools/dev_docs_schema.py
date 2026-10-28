#!/usr/bin/env python3
"""Fetch Apple's developer documentation JSON and turn it into JSON Schemas.

    dev_docs_schema.py fetch SOURCE [--single]
    dev_docs_schema.py generate SOURCE [SOURCE ...] SCHEMA_DIR
    dev_docs_schema.py generate-single FILE [-o OUTPUT]
    dev_docs_schema.py index FILE [FILE ...]
    dev_docs_schema.py prune SOURCE SCHEMA_DIR [-n]

SOURCE names the document that starts the walk, relative to Apple's
"/documentation/" path: "applebusinessapi.json" is the navigation page of the
Apple Business API namespace, "devicemanagement/device-assignment.json" the
page of one of its groups.

The documents are cached under $XDG_CACHE_HOME/dev_docs_schema, mirroring
Apple's URLs: the navigation page of the namespace is cached as
"applebusinessapi.json", the documents it names as
"applebusinessapi/activationlocktype.json". The ETag of a document is kept
in a hidden file beside it — ".applebusinessapi.json.etag".

The commands:

    fetch           download the documents the source names into the cache;
                    with --single, the source document alone
    generate        write a schema for every type the sources name, and an
                    OpenAPI document of their endpoints (OpenAPI.json)
    generate-single convert one document into one schema
    index           list the documents a document names
    prune           remove the schemas the source does not name

A source names a document in the cache, the way ``fetch`` and ``generate``
take one. ``index`` and ``generate-single`` take either: a document in the
cache, or the path of a file.

``fetch`` and ``generate`` walk the same way: from the source document to
the documents it names, to the documents they name, and so on. A type
document names the types it references, and those are only known once the
document itself is at hand. ``fetch`` downloads what the walk finds;
``generate`` reads the same walk from the cache and writes the schemas.

The walk stays inside the namespace of the source. Apple's pages name
symbols of other namespaces too -- the Swift standard library, Foundation --
and following those walks the whole of Apple's documentation: eleven
thousand documents for one of the Device Management groups.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple

# A JSON document or sub-document (loosely typed).
JSON = dict[str, Any]

# The base of Apple's tutorial JSON.
DATA_BASE = "https://developer.apple.com/tutorials/data"

# The prefix of the paths of Apple's documentation.
DOC_PREFIX = "/documentation/"

# The name of the cache directory, below the user's cache directory.
CACHE_NAME = "dev_docs_schema"

# The user agent sent to Apple's servers.
USER_AGENT = "apple-device-services"

# The seconds to wait for Apple's servers.
TIMEOUT = 30


class Conflict(Exception):
    """Raised when two documents disagree about the name of a document."""


# ---------------------------------------------------------------- documents


def parse_document(raw: str, source: str) -> JSON:
    """Parse an Apple documentation JSON document."""
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"{source}: not valid JSON: {e}") from e
    if not isinstance(doc, dict):
        raise TypeError(f"{source}: not a JSON object")
    return doc


def read_document(path: str | Path) -> JSON:
    """Read an Apple documentation JSON document from a file."""
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    return parse_document(raw, str(path))


def is_symbol(reference: JSON) -> bool:
    """Return True if a reference points at a document worth fetching.

    Apple marks the pages of a navigation page with a ``role``: ``symbol`` for
    the types and the endpoints, ``article`` for prose and ``collection`` for
    the navigation pages themselves. The endpoints are worth fetching even
    though they are not types: they name the request and response types.
    """
    return reference.get("role") == "symbol"


def mentioned_identifiers(doc: JSON) -> set[str]:
    """Return the identifiers the content of a document mentions.

    A document holds two kinds of references: the ones its content mentions —
    the pages of its navigation, the types of its properties, the request and
    response types of an endpoint — and the ones it merely relates to (the
    Swift protocols a type conforms to, the sibling types of its namespace).
    Only the first kind belongs to the document: the second leads to the whole
    of Apple's documentation.
    """
    identifiers: set[str] = set()
    for section in doc.get("topicSections") or []:
        identifiers.update(section.get("identifiers") or [])
    collect_identifiers(doc.get("primaryContentSections") or [], identifiers)
    return identifiers


def collect_identifiers(node: Any, identifiers: set[str]) -> None:
    """Collect the type identifiers of a content section."""
    if isinstance(node, dict):
        if node.get("kind") == "typeIdentifier" and node.get("identifier"):
            identifiers.add(node["identifier"])
        for value in node.values():
            collect_identifiers(value, identifiers)
    elif isinstance(node, list):
        for item in node:
            collect_identifiers(item, identifiers)


class Reference(NamedTuple):
    """A document another document names."""

    name: str  # the document's name in the cache
    title: str  # the name of the type it describes
    is_type: bool  # whether it becomes a schema


def references(doc: JSON) -> list[Reference]:
    """Return the documents a document names in its content.

    Only the references the content mentions are returned: the "related
    symbols" of a page -- the sibling types of its namespace, the Swift
    protocols a type conforms to -- are left out.
    """
    entries: list[Reference] = []
    mentioned = mentioned_identifiers(doc)
    for identifier, reference in (doc.get("references") or {}).items():
        title = reference.get("title")
        url = reference.get("url")
        if not title or not url or not is_symbol(reference):
            continue
        if identifier not in mentioned:
            continue
        entries.append(Reference(cache_name(url), title, is_type_reference(reference)))
    return entries


def document_title(doc: JSON) -> str | None:
    """Return the title of a document — the name of the type it describes."""
    return (doc.get("metadata") or {}).get("title")


def is_type(doc: JSON) -> bool:
    """Return True if a document describes a type.

    Apple marks the kinds of its documents with a ``symbolKind``: a
    ``dictionary`` is a data type, a ``typealias`` an enum-like string type,
    and an ``httpRequest`` an endpoint. Only the first two become schemas.
    """
    return (doc.get("metadata") or {}).get("symbolKind") in ("dictionary", "typealias")


def is_type_reference(reference: JSON) -> bool:
    """Return True if a reference points at a type.

    Apple describes the symbol a reference points at with its declaration
    tokens: a type has them ("object Device"), an endpoint does not.
    """
    return bool(reference.get("fragments"))


# -------------------------------------------------------------------- paths


def cache_name(url: str) -> str:
    """Return the name of a cached document for an Apple documentation URL.

    The cache mirrors Apple's URLs: the path below "/documentation/" is the
    name of the file, with ".json" appended.
    """
    path = url
    if path.startswith(DOC_PREFIX):
        path = path[len(DOC_PREFIX) :]
    else:
        path = path.lstrip("/")
    return path + ".json"


def document_path(name: str) -> str:
    """Return the Apple documentation path of a cached document."""
    path = name.removesuffix(".json")
    return DOC_PREFIX + path


def source_name(source: str) -> str:
    """Return the name in the cache of the document a walk starts at."""
    name = source.lstrip("/")
    if not name.endswith(".json"):
        name += ".json"
    return name


def schema_file(title: str) -> str:
    """Return the name of the schema file of a type.

    The file is named after the type, the characters a URI reference cannot
    hold bare replaced by underscores: "ErrorResponse.Errors.Meta.Any Key"
    is "ErrorResponse.Errors.Meta.Any_Key.json", the way Apple's own
    identifiers spell it. The ``$ref`` naming the file must be a valid URI
    reference, and a percent-encoded one ("Any%20Key") is not decoded by
    every resolver — go-jsonschema's is not.
    """
    return re.sub(r"[^A-Za-z0-9._-]", "_", title) + ".json"


def schema_files(directory: Path) -> list[Path]:
    """Return the JSON schema files of a directory."""
    return sorted(
        path for path in directory.rglob("*.json") if not path.name.startswith(".")
    )


def namespace_of(name: str) -> str:
    """Return the documentation namespace a cached document belongs to.

    The namespace is the first segment of the name: the documents of the
    Device Management namespace are "devicemanagement/..." whatever else they
    are named.
    """
    return name.split("/", 1)[0].removesuffix(".json")


def read_source(cache: Cache, source: str) -> JSON:
    """Read a document from a file, from the cache, or from the standard input.

    A source is either the path of a file or the name of a document in the
    cache, the way ``fetch`` and ``generate`` name their source. "-" is the
    standard input.
    """
    if source == "-":
        return parse_document(sys.stdin.read(), "the standard input")
    path = Path(source)
    if path.exists():
        return read_document(path)
    name = source_name(source)
    document = cache.read(name)
    if document is None:
        raise ValueError(
            f"{source}: not a file, and not in the cache ({cache.path(name)})"
        )
    return document


# ------------------------------------------------------------------- schemas

# Apple scalar type names mapped to JSON Schema ``type`` values.
SCALAR_TYPES: dict[str, str] = {
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

# Apple type names that are JSON Schema string formats, not types.
STRING_FORMATS: set[str] = {
    "uri-reference",
    "date",
    "date-time",
}

# Apple type names that are widths of a JSON Schema ``integer`` or
# ``number`` (OpenAPI's formats). They are kept as ``x-format``: the
# ``jsonschema`` linter refuses a ``format`` on anything but a string.
NUMBER_FORMATS: set[str] = {"int32", "int64", "float", "double"}

# Apple type names JSON Schema does not have: ``byte`` and ``data`` are
# base64-encoded blobs, ``*`` means "any value".
SPECIAL_TYPES: dict[str, dict[str, Any]] = {
    "byte": {"type": "string", "contentEncoding": "base64"},
    "data": {"type": "string", "contentEncoding": "base64"},
    "*": {},
}


def reference_link(token: JSON, refs: JSON) -> str:
    """Render a reference token as a Markdown link."""
    ref = refs.get(token.get("identifier"), {})
    title = ref.get("title") or token.get("title") or token.get("text") or ""
    url = ref.get("url")
    if not url:
        return title
    if not url.startswith("http"):
        url = "https://developer.apple.com" + url
    return f"[{title}]({url})"


# Markdown markers of the inline tokens that wrap other inline tokens.
INLINE_MARKERS: dict[str, str] = {
    "strong": "**",
    "emphasis": "*",
    "strikethrough": "~~",
}


def inline_text(tokens: list[JSON], refs: JSON) -> str:
    """Render an inline content token list as Markdown."""
    parts: list[str] = []
    for token in tokens:
        kind = token.get("type")
        if kind == "text":
            parts.append(token.get("text", ""))
        elif kind in ("codeVoice", "code"):
            parts.append(f"`{token.get('code', '')}`")
        elif kind == "reference":
            parts.append(reference_link(token, refs))
        elif "inlineContent" in token:
            # A token that wraps others — "**(Optional)**" — keeps its text.
            marker = INLINE_MARKERS.get(kind, "")
            text = inline_text(token["inlineContent"], refs)
            parts.append(f"{marker}{text}{marker}")
    return "".join(parts)


def block_text(block: JSON, refs: JSON) -> str:
    """Render one content block as Markdown."""
    kind = block.get("type")
    if kind == "paragraph":
        return inline_text(block.get("inlineContent", []), refs)
    if kind == "unorderedList":
        items: list[str] = []
        for item in block.get("items", []):
            text = content_text(item.get("content", []), refs)
            # Indent a multi-line item's continuation beneath its bullet.
            items.append(f"- {text}".replace("\n", "\n  "))
        return "\n".join(items)
    if kind == "aside":
        # A "Note" or "Important" call-out becomes a Markdown blockquote.
        name = block.get("name") or (block.get("style") or "note").capitalize()
        text = content_text(block.get("content", []), refs)
        return f"> **{name}:** {text}".replace("\n", "\n> ")
    # Headings and other blocks carry no body text here.
    return ""


def content_text(blocks: list[JSON], refs: JSON) -> str:
    """Render a content block list as Markdown paragraphs."""
    paragraphs: list[str] = []
    for block in blocks:
        text = block_text(block, refs)
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs).strip()


def scalar_schema(name: str) -> dict[str, Any]:
    """Map an Apple scalar type name to a JSON Schema object."""
    if name in SPECIAL_TYPES:
        return dict(SPECIAL_TYPES[name])
    if name in STRING_FORMATS:
        return {"type": "string", "format": name}
    schema = {"type": SCALAR_TYPES.get(name, name)}
    if name in NUMBER_FORMATS:
        schema["x-format"] = name
    return schema


def type_schema(tokens: list[JSON]) -> dict[str, Any]:
    """Map Apple's type tokens to a JSON Schema object.

    A referenced type becomes a ``$ref`` to the sibling schema file (see
    ``schema_file``), a scalar its JSON Schema
    type, and a union of them — "(uri-reference | ErrorLinks.Associated)" —
    an ``anyOf``. Brackets around the whole make it an array.
    """
    alternatives: list[dict[str, Any]] = []
    for token in tokens:
        if token.get("kind") == "typeIdentifier":
            alternatives.append({"$ref": schema_file(token.get("text", ""))})
        elif token.get("kind") == "text":
            # The punctuation of the declaration: brackets, parentheses, and
            # the bars between the scalar alternatives of a union.
            for name in token.get("text", "").split("|"):
                name = name.strip(" []()")
                if name:
                    alternatives.append(scalar_schema(name))
    if len(alternatives) > 1:
        # A union of types: "one or more of these". `anyOf` rather than
        # `oneOf`, since the alternatives are often permissive enough that a
        # value matches more than one of them.
        elem: dict[str, Any] = {"anyOf": alternatives}
    else:
        # No alternative at all is no constraint at all.
        elem = alternatives[0] if alternatives else {}

    text = "".join(t.get("text", "") for t in tokens if t.get("kind") == "text")
    if text.strip().startswith("["):
        # An unconstrained element type adds no constraint, so `items` is
        # left out.
        return {"type": "array", "items": elem} if elem else {"type": "array"}
    return elem


def typed_value(value: Any, schema: dict[str, Any], name: str) -> Any:
    """Return an attribute's value typed the way its schema is.

    Apple writes the values of the ``default``, ``minimum`` and ``maximum``
    attributes as strings — "100" for an integer — whatever their type.
    """
    kind = schema.get("type")
    try:
        if kind == "integer":
            return int(value)
        if kind == "number":
            return float(value)
        if kind == "boolean" and value in ("true", "false"):
            return value == "true"
        if kind == "string":
            return str(value)
    except (TypeError, ValueError):
        pass
    raise ValueError(f"{name}: the value {value!r} is not a {kind}")


def allowed_values(item: JSON) -> list[Any]:
    """Return an Apple property's structured allowed values, if it has them."""
    for attribute in item.get("attributes", []):
        if attribute.get("kind") == "allowedValues":
            return list(attribute.get("values") or [])
    return []


def constraint_slot(schema: dict[str, Any]) -> dict[str, Any]:
    """Return the node a value constraint belongs in (array items or the node)."""
    if schema.get("type") == "array":
        return schema.setdefault("items", {})
    return schema


def set_constraint(schema: dict[str, Any], values: list[Any]) -> None:
    """Constrain a schema to a set of allowed values.

    A single allowed value is written as a ``const``. The ``type`` keyword is
    dropped: the constraint already implies the type of its values.
    """
    slot = constraint_slot(schema)
    slot.pop("enum", None)
    slot.pop("const", None)
    if len(values) == 1:
        slot["const"] = values[0]
    elif values:
        slot["enum"] = list(values)
    if "enum" in slot or "const" in slot:
        slot.pop("type", None)


def property_schema(item: JSON, refs: JSON) -> dict[str, Any]:
    """Build a JSON Schema property from an Apple property item."""
    schema = type_schema(item.get("type", []))
    description = content_text(item.get("content", []), refs)
    if description:
        schema["description"] = description
    if "iso 8601" in description.lower() and "format" not in schema:
        # Case-insensitive: Apple writes "iSO 8601" too. The hyphenated
        # "ISO-8601" of the ANB docs is left alone: its examples carry a
        # "+0000" offset, which is not an RFC 3339 date-time.
        schema["format"] = "date-time"
    for attribute in item.get("attributes", []):
        # A bound constrains the values (an array's items); a default is
        # the property's own. Typed before an enum drops the type.
        keyword = attribute.get("kind")
        if keyword in ("minimum", "maximum"):
            slot = constraint_slot(schema)
        elif keyword == "default":
            slot = schema
        else:
            continue
        slot[keyword] = typed_value(attribute.get("value"), slot, item.get("name"))
    values = allowed_values(item)
    if values:
        set_constraint(schema, values)
    return schema


def properties(doc: JSON) -> list[tuple[str, JSON]]:
    """Return an Apple document's properties."""
    items: list[tuple[str, JSON]] = []
    for section in doc.get("primaryContentSections", []):
        if section.get("kind") != "properties":
            continue
        for item in section.get("items", []):
            name = item.get("name")
            if name:
                items.append((name, item))
    return items


def possible_values(doc: JSON) -> list[str] | None:
    """Return an Apple document's possible values, if it has that section."""
    for section in doc.get("primaryContentSections", []):
        if section.get("kind") != "possibleValues":
            continue
        return [v["name"] for v in section.get("values", []) if "name" in v]
    return None


def alias_type(doc: JSON) -> str:
    """Return the JSON Schema type a type alias aliases."""
    for section in doc.get("primaryContentSections", []):
        if section.get("kind") != "declarations":
            continue
        for declaration in section.get("declarations", []):
            for token in declaration.get("tokens", []):
                name = (token.get("text") or "").strip().lower()
                if name in SCALAR_TYPES:
                    return SCALAR_TYPES[name]
    return "string"


def map_value(doc: JSON) -> dict[str, Any] | None:
    """Return the value schema of an "Any Key" (map) dictionary, if it is one.

    Apple encodes a dictionary with arbitrary string keys as a nested
    ``*-data.dictionary`` whose single property is named ``Any Key``.
    """
    items = [item for _name, item in properties(doc)]
    if len(items) != 1 or items[0].get("name") != "Any Key":
        return None
    return type_schema(items[0].get("type", []))


def introduced_text(doc: JSON) -> str:
    """Render an Apple document's availability line.

    The availability lives in the metadata: every platform the document is
    available on names the version that introduced it — "Apple Business API
    2.6+" or "Device Assignment Services 5.0+", the way the documentation
    front-end shows it.
    """
    platforms = doc.get("metadata", {}).get("platforms") or []
    names = [
        f"{p.get('name')} {p['introducedAt']}+"
        for p in platforms
        if p.get("introducedAt")
    ]
    if not names:
        return ""
    return ", ".join(names)


def api_version(doc: JSON) -> str | None:
    """Return the version of the API a document was introduced in.

    A document of one platform (the repository's API namespaces are) names
    one version. The OS platforms of the wider Device Management namespace
    name several, so their versions are returned with the platform's own
    name — "iOS 7.0, macOS 10.9" — to stay distinct.
    """
    platforms = doc.get("metadata", {}).get("platforms") or []
    versions = [p for p in platforms if p.get("introducedAt")]
    if not versions:
        return None
    if len(versions) == 1:
        return versions[0]["introducedAt"]
    return ", ".join(f"{p.get('name')} {p['introducedAt']}" for p in versions)


# The headings of the content sections that hold prose about a type.
PROSE_HEADINGS: set[str] = {"Discussion", "Overview", "Possible Values"}


def discussion_text(doc: JSON, refs: JSON) -> str:
    """Render a document's prose sections as Markdown.

    The prose is in the content sections headed "Discussion", "Overview" or
    "Possible Values"; the heading itself carries no prose. Other sections
    ("Example response") hold code, not prose about the type. The prose of
    an enum-like type is an unorderedList of one "NAME: what it means"
    bullet per possible value — a plain-paragraph one is prose about the
    type.
    """
    texts: list[str] = []
    for section in doc.get("primaryContentSections", []):
        if section.get("kind") != "content":
            continue
        blocks = section.get("content") or []
        heading = blocks[0] if blocks else {}
        if heading.get("type") == "heading" and heading.get("text") in PROSE_HEADINGS:
            texts.append(content_text(blocks[1:], refs))
    return "\n\n".join(text for text in texts if text)


def document_uris(
    doc: JSON, data_base: str = DATA_BASE
) -> tuple[str | None, str | None]:
    """Return the (API page, tutorial JSON) URLs of an Apple document."""
    for variant in doc.get("variants") or []:
        for path in variant.get("paths") or []:
            return (
                "https://developer.apple.com" + path,
                data_base + path + ".json",
            )
    return None, None


def document_description(doc: JSON, refs: JSON) -> str:
    """Render a document's description: abstract, availability and prose."""
    parts = (
        inline_text(doc.get("abstract", []), refs),
        introduced_text(doc),
        discussion_text(doc, refs),
    )
    return "\n\n".join(part for part in parts if part)


def document_extensions(doc: JSON, data_base: str = DATA_BASE) -> JSON:
    """Return the ``x-apple-developer-*`` fields that point back at a document."""
    fields: JSON = {}
    version = api_version(doc)
    if version:
        fields["x-apple-developer-api-version"] = version
    api_uri, data_uri = document_uris(doc, data_base)
    if api_uri:
        fields["x-apple-developer-api-uri"] = api_uri
    if data_uri:
        fields["x-apple-developer-data-uri"] = data_uri
    return fields


def document_schema(doc: JSON, data_base: str = DATA_BASE) -> JSON:
    """Build the JSON Schema for an Apple type document."""
    meta = doc.get("metadata", {})
    refs = doc.get("references", {})

    schema: JSON = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": meta.get("title"),
    }
    description = document_description(doc, refs)
    if description:
        schema["description"] = description
    schema.update(document_extensions(doc, data_base))

    values = possible_values(doc)
    if values is not None:
        # An enum-like type alias: the alias is a string constrained to the
        # values Apple lists as possible.
        if values:
            set_constraint(schema, values)
        else:
            schema["type"] = alias_type(doc)
        return schema

    value = map_value(doc)
    if value is not None:
        # A map of arbitrary keys, not a named-properties object.
        schema["type"] = "object"
        schema["additionalProperties"] = value
        return schema

    schema["type"] = "object"
    props: dict[str, Any] = {}
    required: list[str] = []
    for name, item in properties(doc):
        props[name] = property_schema(item, refs)
        if item.get("required"):
            required.append(name)
    if props:
        schema["properties"] = props
    if required:
        schema["required"] = required
    return schema


def write_schema(schema: JSON, path: str | Path) -> None:
    """Write a JSON Schema to a file or the standard output.

    The output is plain JSON with two-space indentation and sorted keys. The
    repo's canonical formatting comes from the ``jsonschema`` CLI's
    formatter, which the CI checks with ``jsonschema fmt -c`` — run it over
    the schemas before committing them.
    """
    text = json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if str(path) == "-":
        sys.stdout.write(text)
        return
    # The name of the file is the name of the type; flag it when the two
    # disagree, so a schema is never filed under the wrong name.
    title = schema.get("title")
    if title and Path(path).name != schema_file(title):
        print(f"warning: {path} holds the type {title!r}", file=sys.stderr)
    Path(path).write_text(text, encoding="utf-8")


# ------------------------------------------------------------------- openapi

# The version of the OpenAPI Specification the documents are written in.
# The schemas of OpenAPI 3.1 are JSON Schema 2020-12, so its documents
# reference the schema files as they are.
OPENAPI_VERSION = "3.1.0"

# The file of the OpenAPI document, beside the schemas it references.
OPENAPI_FILE = "OpenAPI.json"

# Apple's parameter sources that are OpenAPI parameter locations.
PARAMETER_SOURCES: set[str] = {"query", "path", "header", "cookie"}


def is_endpoint(doc: JSON) -> bool:
    """Return True if a document describes an endpoint (an ``httpRequest``)."""
    return (doc.get("metadata") or {}).get("symbolKind") == "httpRequest"


class Endpoint(NamedTuple):
    """The URL of an endpoint."""

    method: str  # "get"
    server: str  # "https://api-business.apple.com"
    path: str  # "/v1/orgDevices/{id}"


def endpoint(doc: JSON) -> Endpoint | None:
    """Return the method, server and path of an endpoint document.

    Apple tokenizes the URL: a ``method`` ("GET"), a ``baseURL``
    ("https://api-business.apple.com/"), then ``path`` ("v1/orgDevices/")
    and ``parameter`` ("{id}") tokens. OpenAPI joins the server and the
    path, which starts with the slash.
    """
    for section in doc.get("primaryContentSections", []):
        if section.get("kind") != "restEndpoint":
            continue
        method = server = ""
        path: list[str] = []
        for token in section.get("tokens", []):
            kind = token.get("kind")
            if kind == "method":
                method = token.get("text", "")
            elif kind == "baseURL":
                server = token.get("text", "")
            elif kind in ("path", "parameter"):
                path.append(token.get("text", ""))
        if method and server:
            return Endpoint(
                method.lower(), server.rstrip("/"), "/" + "".join(path).lstrip("/")
            )
    return None


def operation_id(name: str) -> str:
    """Return the ``operationId`` of the endpoint document with a cache name.

    The id is the last segment of Apple's URL in camel case: "fetchDevices"
    for "devicemanagement/fetch-devices.json" ("Get a List of Devices"). The
    URL is more stable than the title.
    """
    slug = name.removesuffix(".json").rsplit("/", 1)[-1]
    words = [word for word in re.split(r"[^A-Za-z0-9]+", slug) if word]
    return words[0].lower() + "".join(w[:1].upper() + w[1:] for w in words[1:])


def operation_parameters(doc: JSON, refs: JSON) -> list[JSON]:
    """Return the OpenAPI parameters of an endpoint document."""
    parameters: list[JSON] = []
    for section in doc.get("primaryContentSections", []):
        if section.get("kind") != "restParameters":
            continue
        source = section.get("source")
        if source not in PARAMETER_SOURCES:
            raise ValueError(f"{document_title(doc)}: parameters in {source!r}")
        for item in section.get("items", []):
            # A parameter is described the way a property is.
            schema = property_schema(item, refs)
            parameter: JSON = {"name": item.get("name"), "in": source}
            description = schema.pop("description", None)
            if description:
                parameter["description"] = description
            if item.get("required"):
                parameter["required"] = True
            parameter["schema"] = schema
            parameters.append(parameter)
    return parameters


def request_body(doc: JSON, refs: JSON) -> JSON | None:
    """Return the OpenAPI request body of an endpoint document, if it has one."""
    for section in doc.get("primaryContentSections", []):
        if section.get("kind") != "restBody":
            continue
        body: JSON = {}
        description = content_text(section.get("content", []), refs)
        if description:
            body["description"] = description
        mime_type = section.get("mimeType")
        if mime_type:
            schema = type_schema(section.get("bodyContentType", []))
            body["content"] = {mime_type: {"schema": schema}}
        return body
    return None


def operation_responses(doc: JSON, refs: JSON) -> JSON:
    """Return the OpenAPI responses of an endpoint document, by status code.

    A response's description is its prose, or else Apple's reason phrase
    ("Bad Request"): OpenAPI requires one.
    """
    responses: JSON = {}
    for section in doc.get("primaryContentSections", []):
        if section.get("kind") != "restResponses":
            continue
        for item in section.get("items", []):
            status = str(item.get("status"))
            if status in responses:
                raise ValueError(f"{document_title(doc)}: two {status} responses")
            description = content_text(item.get("content", []), refs)
            response: JSON = {"description": description or item.get("reason", "")}
            mime_type = item.get("mimeType")
            if mime_type and item.get("type"):
                schema = type_schema(item["type"])
                response["content"] = {mime_type: {"schema": schema}}
            responses[status] = response
    return responses


def operation(name: str, doc: JSON, data_base: str = DATA_BASE) -> JSON:
    """Build the OpenAPI operation of an endpoint document."""
    refs = doc.get("references", {})
    op: JSON = {"operationId": operation_id(name), "summary": document_title(doc)}
    description = document_description(doc, refs)
    if description:
        op["description"] = description
    op.update(document_extensions(doc, data_base))
    parameters = operation_parameters(doc, refs)
    if parameters:
        op["parameters"] = parameters
    body = request_body(doc, refs)
    if body:
        op["requestBody"] = body
    responses = operation_responses(doc, refs)
    if responses:
        op["responses"] = responses
    return op


def version_key(version: str) -> tuple[int, ...]:
    """Return a sort key that orders versions numerically ("5.10" > "5.9")."""
    return tuple(int(number) for number in re.findall(r"\d+", version))


def external_refs(node: Any) -> set[str]:
    """Return the ``$ref``s of a document that name other files."""
    found: set[str] = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#"):
            found.add(ref.split("#", 1)[0])
        for value in node.values():
            found |= external_refs(value)
    elif isinstance(node, list):
        for item in node:
            found |= external_refs(item)
    return found


def endpoint_tags(roots: list[JSON]) -> tuple[dict[str, str], list[str]]:
    """Return the tag of each document, by cache name, and the tags in order.

    The tags are the topic sections of the source documents — "Devices",
    "Blueprints" — the way Apple groups the endpoints on the page of an API.
    An endpoint the source documents do not list has none.
    """
    tags: dict[str, str] = {}
    order: list[str] = []
    for root in roots:
        refs = root.get("references") or {}
        for section in root.get("topicSections") or []:
            title = section.get("title")
            for identifier in section.get("identifiers") or []:
                url = (refs.get(identifier) or {}).get("url")
                if title and url:
                    tags.setdefault(cache_name(url), title)
                    if title not in order:
                        order.append(title)
    return tags, order


def openapi_document(
    roots: list[JSON], endpoints: dict[str, JSON], data_base: str = DATA_BASE
) -> JSON:
    """Build the OpenAPI document of the endpoints a walk found.

    The roots are the documents the walks started at; the endpoints are the
    endpoint documents, by cache name. The types the operations name are
    referenced as the sibling schema files.
    """
    paths: dict[str, JSON] = {}
    servers: dict[str, list[JSON]] = {}
    ids: dict[str, str] = {}
    versions: list[str] = []
    tags, tag_order = endpoint_tags(roots)
    for name in sorted(endpoints):
        doc = endpoints[name]
        url = endpoint(doc)
        if url is None:
            raise ValueError(f"{name}: an endpoint without a URL")
        op = operation(name, doc, data_base)
        if name in tags:
            op["tags"] = [tags[name]]
        previous = ids.setdefault(op["operationId"], name)
        if previous != name:
            raise Conflict(f"{previous} and {name} are both {op['operationId']}")
        methods = paths.setdefault(url.path, {})
        if url.method in methods:
            raise Conflict(f"{url.method.upper()} {url.path} is documented twice")
        methods[url.method] = op
        servers.setdefault(url.server, []).append(op)
        version = api_version(doc)
        if version:
            versions.append(version)

    if len(servers) > 1:
        # Operations on several hosts: each names its own.
        for server, ops in servers.items():
            for op in ops:
                op["servers"] = [{"url": server}]
    info: JSON = {
        "title": " / ".join(filter(None, map(document_title, roots))),
        # The latest version any operation was introduced in: the version of
        # the API that has all of them.
        "version": max(versions, key=version_key) if versions else "",
    }
    description = "\n\n".join(
        filter(None, (inline_text(r.get("abstract", []), {}) for r in roots))
    )
    if description:
        info["description"] = description
    document: JSON = {"openapi": OPENAPI_VERSION, "info": info, "paths": paths}
    # The sections that list endpoints; "Objects and data types" lists types.
    used = {tags[name] for name in endpoints if name in tags}
    if used:
        document["tags"] = [{"name": tag} for tag in tag_order if tag in used]
    if len(servers) == 1:
        document["servers"] = [{"url": next(iter(servers))}]
    return document


# --------------------------------------------------------------------- cache


class Cache:
    """The cached documents, mirrored from Apple's URLs."""

    def __init__(self, root: Path, data_base: str = DATA_BASE) -> None:
        self.root = root
        self.data_base = data_base
        self.fetched = 0
        self.unchanged = 0

    def path(self, name: str) -> Path:
        """Return the path of a cached document.

        The names come from the URLs of Apple's documents, so a name that
        would leave the cache is refused.
        """
        parts = PurePosixPath(name)
        if parts.is_absolute() or ".." in parts.parts:
            raise ValueError(f"{name}: not a name inside the cache")
        return self.root / name

    def etag_path(self, name: str) -> Path:
        """Return the path of the ETag that belongs to a cached document.

        The ETag sits beside the document, in a hidden file: the name of the
        document, prefixed with a period and suffixed with ".etag".
        """
        document = self.path(name)
        return document.with_name(f".{document.name}.etag")

    def url(self, name: str) -> str:
        """Return the URL of a document."""
        return f"{self.data_base}{document_path(name)}.json"

    def read(self, name: str) -> JSON | None:
        """Read a cached document, or None if it is not cached."""
        path = self.path(name)
        if not path.exists():
            return None
        return read_document(path)

    def fetch(self, name: str) -> JSON:
        """Fetch a document into the cache and return it.

        The download is conditional: the ETag of the cached document is sent
        back, and a document that did not change costs a 304 and no bytes.
        An ETag without its document is not sent: the 304 would leave
        nothing to read.
        """
        url = self.url(name)
        destination = self.path(name)
        etag_file = self.etag_path(name)
        etag = ""
        if etag_file.exists() and destination.exists():
            etag = etag_file.read_text(encoding="utf-8").strip()

        request = urllib.request.Request(url)
        request.add_header("User-Agent", USER_AGENT)
        if etag:
            request.add_header("If-None-Match", etag)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                body = response.read()
                # Parse before storing: a body that is not a document must
                # not be cached under an ETag that would keep it there.
                document = parse_document(body.decode("utf-8"), url)
                self._store(destination, etag_file, body, response.headers)
        except urllib.error.HTTPError as e:
            if e.code != 304:
                raise ValueError(f"{url}: {e.code} {e.reason}") from e
            # The document did not change: the cached one is current.
            self.unchanged += 1
            document = self.read(name)
            if document is None:
                raise ValueError(f"{name}: the server has it, the cache does not")
            return document
        self.fetched += 1
        return document

    def _store(
        self, destination: Path, etag_file: Path, body: bytes, headers: Any
    ) -> None:
        """Write a fetched document and its ETag to the cache.

        The ETag of the previous document goes first and the document is
        renamed into place whole, so an interrupted write never leaves a
        partial document behind an ETag that would keep it there.
        """
        destination.parent.mkdir(parents=True, exist_ok=True)
        etag_file.unlink(missing_ok=True)
        partial = destination.with_name(f".{destination.name}.partial")
        partial.write_bytes(body)
        # Keep the timestamp the server gave the document.
        timestamp = headers.get("Last-Modified")
        if timestamp:
            moment = parsedate_to_datetime(timestamp).timestamp()
            os.utime(partial, (moment, moment))
        os.replace(partial, destination)
        etag = headers.get("ETag")
        if etag:
            etag_file.write_text(etag, encoding="utf-8")


# --------------------------------------------------------------------- walk


@dataclass
class Document:
    """A document of the walk, and the names its referrers gave it."""

    name: str
    data: JSON
    titles: set[str] = field(default_factory=set)


@dataclass
class Resolver:
    """Resolves the documents a source document names.

    The walk is breadth-first: the source document, then the documents it
    names, then the documents they name. ``load`` returns the document with
    the given name, or None when it is not available.

    The walk stays inside the namespace of the source: Apple's pages name
    symbols of other namespaces too — the Swift standard library, Foundation
    — and following those walks the whole of Apple's documentation.
    """

    source: str
    load: Callable[[str], JSON | None]
    announce: Callable[[int], None] | None = None
    scope: str | None = None
    names: dict[str, set[str]] = field(default_factory=dict)
    titles: dict[str, str] = field(default_factory=dict)
    missing: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.scope is None:
            self.scope = namespace_of(self.source)

    def levels(self) -> Iterator[list[Document]]:
        """Yield the documents of the walk, level by level."""
        self.names = {self.source: set()}
        self.titles = {}
        self.missing = set()
        seen = {self.source}
        level = [self.source]
        while level:
            if self.announce:
                self.announce(len(level))
            documents = []
            for name in level:
                data = self.load(name)
                if data is None:
                    self.missing.add(name)
                    continue
                title = document_title(data)
                if title:
                    self.titles[name] = title
                for other in self.names[name]:
                    self.check(name, other)
                documents.append(Document(name, data, self.names[name]))
            yield documents
            following = []
            for document in documents:
                for reference in references(document.data):
                    name, title = reference.name, reference.title
                    if not self.in_scope(name):
                        continue
                    names = self.names.setdefault(name, set())
                    names.add(title)
                    self.check(name, title)
                    if name not in seen:
                        seen.add(name)
                        following.append(name)
            level = following

    def in_scope(self, name: str) -> bool:
        """Return True if a document belongs to the namespace of the walk."""
        if not self.scope:
            return True
        return name.startswith(f"{self.scope}/")

    def check(self, name: str, title: str) -> None:
        """Check that a document is named the same way by everyone.

        The document's own title is the one everybody has to agree on: a
        document named two ways would become two schemas.
        """
        own = self.titles.get(name)
        if own and own != title:
            raise Conflict(f"{name} is named two ways: {own} and {title}")

    def walk(self) -> int:
        """Walk the documents, and return how many there were."""
        return sum(len(level) for level in self.levels())


# ------------------------------------------------------------------ commands


def announce(count: int) -> None:
    """Report the size of a level of the walk."""
    plural = "" if count == 1 else "s"
    print(f"fetching {count} document{plural}", file=sys.stderr)


def command_fetch(args: argparse.Namespace) -> int:
    """Download the documents a source document names into the cache."""
    cache = Cache(args.cache_dir, args.data_base)
    source = source_name(args.source)
    print(f"fetching {cache.url(source)}", file=sys.stderr)

    if args.single:
        # Only the source document: the walk stops here.
        cache.fetch(source)
    else:
        resolver = Resolver(source, cache.fetch, announce)
        resolver.walk()
    print(
        f"fetched {cache.fetched} documents, {cache.unchanged} unchanged",
        file=sys.stderr,
    )
    return 0


def command_generate(args: argparse.Namespace) -> int:
    """Write a schema for every type the sources name, and an OpenAPI document.

    The endpoints the sources name become the operations of an OpenAPI
    document, written beside the schemas as ``OpenAPI.json``; the types they
    reference are the schemas.
    """
    cache = Cache(args.cache_dir, args.data_base)
    schema_dir = Path(args.schema_dir)
    if schema_dir.exists() and not schema_dir.is_dir():
        # The last argument is the schema directory, the way ``cp`` takes
        # its destination: never write into a file.
        print(f"error: not a directory: {schema_dir}", file=sys.stderr)
        return 1

    writes: dict[Path, JSON] = {}
    roots: list[JSON] = []
    endpoints: dict[str, JSON] = {}
    missing: set[str] = set()
    skipped = 0
    shared: dict[str, str] = {}
    for source in (source_name(s) for s in args.sources):
        resolver = Resolver(source, cache.read)
        for level in resolver.levels():
            for document in level:
                if document.name == source:
                    roots.append(document.data)
                if is_endpoint(document.data):
                    endpoints.setdefault(document.name, document.data)
                    continue
                if not is_type(document.data):
                    # Articles and navigation pages are not types.
                    skipped += 1
                    continue
                title = document_title(document.data)
                if not title:
                    skipped += 1
                    continue
                name = schema_file(title)
                if name == OPENAPI_FILE:
                    raise Conflict(f"{document.name} would overwrite {OPENAPI_FILE}")
                previous = shared.setdefault(name, document.name)
                if previous != document.name:
                    # Several documents may share a name, but only one of them
                    # can become a schema.
                    print(
                        f"note: {name} is shared by {previous} and {document.name}",
                        file=sys.stderr,
                    )
                writes[schema_dir / name] = document_schema(
                    document.data, args.data_base
                )
        missing |= resolver.missing

    if missing:
        # The walk stops at the documents that are not cached, so the types
        # behind them are not known: nothing is written rather than a part.
        print(
            f"error: {len(missing)} documents are not in the cache; "
            "fetch them before generating:",
            file=sys.stderr,
        )
        for name in sorted(missing):
            print(f"  {cache.path(name)}", file=sys.stderr)
        return 1

    if endpoints:
        document = openapi_document(roots, endpoints, args.data_base)
        # Every type an operation references has to be a schema.
        unresolved = sorted(
            ref
            for ref in external_refs(document)
            if schema_dir / ref not in writes and not (schema_dir / ref).exists()
        )
        if unresolved:
            print(f"error: {OPENAPI_FILE} references missing files:", file=sys.stderr)
            for ref in unresolved:
                print(f"  {ref}", file=sys.stderr)
            return 1
        writes[schema_dir / OPENAPI_FILE] = document

    schema_dir.mkdir(parents=True, exist_ok=True)
    for path, schema in writes.items():
        write_schema(schema, path)
    if skipped:
        print(f"note: {skipped} documents are not types", file=sys.stderr)
    print(
        f"wrote {len(writes) - bool(endpoints)} schemas and "
        f"{len(endpoints)} operations to {schema_dir}",
        file=sys.stderr,
    )
    return 0


def command_generate_single(args: argparse.Namespace) -> int:
    """Convert one document into one schema.

    The document is either a file or the name of a document in the cache, the
    way ``index`` takes either.
    """
    cache = Cache(args.cache_dir, args.data_base)
    doc = read_source(cache, args.input)
    if not is_type(doc):
        # Endpoints and articles are not types.
        print(f"skipping {args.input}: not a type", file=sys.stderr)
        return 0
    write_schema(document_schema(doc, args.data_base), args.output)
    return 0


def command_index(args: argparse.Namespace) -> int:
    """List the documents a document names.

    A document is either a file or the name of a document in the cache, the
    way ``fetch`` and ``generate`` name their source. The types are listed
    with the name of the schema file they become; the endpoints are listed
    on their own — they have no schema of their own.
    """
    cache = Cache(args.cache_dir, args.data_base)
    entries: set[tuple[str, str]] = set()
    for source in args.files:
        doc = read_source(cache, source)
        for reference in references(doc):
            name = reference.name
            if reference.is_type:
                entries.add((name, schema_file(reference.title)))
            else:
                entries.add((name, ""))
    for name, file in sorted(entries):
        print(f"{name} {file}".rstrip())
    return 0


def command_prune(args: argparse.Namespace) -> int:
    """Remove the schemas a source document does not name.

    The walk is the one ``generate`` uses, so the two agree on the name of
    every type: the schemas that are kept are the ones ``generate`` writes.
    The files that are removed are the ones the source does not name -- the
    schemas of types Apple no longer documents, and the hand-written ones.

    A schema directory belongs to one root, the way the repository's
    directories do. A directory that holds the schemas of several roots is
    pruned by hand: the dry run names the files that would go.
    """
    cache = Cache(args.cache_dir, args.data_base)
    source = source_name(args.source)
    schema_dir = Path(args.schema_dir)
    if not schema_dir.is_dir():
        print(f"error: not a directory: {schema_dir}", file=sys.stderr)
        return 1

    resolver = Resolver(source, cache.read)
    wanted: set[str] = set()
    for level in resolver.levels():
        for document in level:
            if is_endpoint(document.data):
                wanted.add(OPENAPI_FILE)
            if not is_type(document.data):
                continue
            title = document_title(document.data)
            if title:
                wanted.add(schema_file(title))

    if resolver.missing:
        # The walk stops at the documents that are not cached, so the types
        # behind them are not known and their schemas would be removed.
        print(
            f"error: {len(resolver.missing)} documents are not in the cache; "
            "fetch them before pruning",
            file=sys.stderr,
        )
        return 1

    extras = [path for path in schema_files(schema_dir) if path.name not in wanted]
    if not extras:
        print(f"nothing to prune in {schema_dir}", file=sys.stderr)
        return 0
    if args.dry_run:
        print(
            f"would remove {len(extras)} schemas from {schema_dir}:",
            file=sys.stderr,
        )
        for path in extras:
            print(f"  {path}", file=sys.stderr)
        return 0
    for path in extras:
        path.unlink()
    print(f"removed {len(extras)} schemas from {schema_dir}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------- main


def default_cache_dir() -> Path:
    """Return the cache directory: $XDG_CACHE_HOME/dev_docs_schema."""
    base = os.environ.get("XDG_CACHE_HOME")
    if not base:
        base = str(Path.home() / ".cache")
    return Path(base) / CACHE_NAME


def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--cache-dir",
        metavar="DIR",
        default=argparse.SUPPRESS,
        help=f"cache directory (default: $XDG_CACHE_HOME/{CACHE_NAME})",
    )
    common.add_argument(
        "--data-base",
        metavar="URL",
        default=argparse.SUPPRESS,
        help=f"base of Apple's tutorial JSON (default: {DATA_BASE})",
    )

    parser = argparse.ArgumentParser(
        prog="dev_docs_schema.py",
        description=(
            "Fetch Apple's developer documentation JSON and turn it into JSON Schemas."
        ),
        parents=[common],
    )
    commands = parser.add_subparsers(dest="command", required=True)

    fetch = commands.add_parser(
        "fetch",
        parents=[common],
        help="download the documents a source names into the cache",
    )
    fetch.add_argument("source", help='e.g. "applebusinessapi.json"')
    fetch.add_argument(
        "--single",
        "--no-recurse",
        action="store_true",
        help="fetch the source document only; do not follow its references",
    )
    fetch.set_defaults(func=command_fetch)

    generate = commands.add_parser(
        "generate",
        parents=[common],
        help="write a schema for every type the sources name, and an OpenAPI document",
    )
    generate.add_argument(
        "sources", nargs="+", metavar="SOURCE", help='e.g. "applebusinessapi.json"'
    )
    generate.add_argument("schema_dir", help='e.g. "abm/schemas"')
    generate.set_defaults(func=command_generate)

    single = commands.add_parser(
        "generate-single",
        parents=[common],
        help="convert one document into one schema",
    )
    single.add_argument(
        "input",
        metavar="FILE",
        help=(
            "Apple documentation JSON document: a file, or a document in the "
            'cache ("-" for the standard input)'
        ),
    )
    single.add_argument(
        "-o",
        "--output",
        default="-",
        metavar="FILE",
        help="schema file to write (default: stdout)",
    )
    single.set_defaults(func=command_generate_single)

    index = commands.add_parser(
        "index",
        parents=[common],
        help="list the documents a document names",
    )
    index.add_argument(
        "files",
        nargs="+",
        metavar="FILE",
        help="documents to list: files, or documents in the cache",
    )
    index.set_defaults(func=command_index)

    prune = commands.add_parser(
        "prune",
        parents=[common],
        help="remove the schemas a source does not name",
    )
    prune.add_argument("source", help='e.g. "applebusinessapi.json"')
    prune.add_argument("schema_dir", help='e.g. "abm/schemas"')
    prune.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="list the schemas that would be removed",
    )
    prune.set_defaults(func=command_prune)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse the command line and run the command."""
    # The output is meant to be piped into another command, so exit quietly
    # when the reader goes away (e.g. `| head`).
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    parser = build_parser()
    args = parser.parse_args(argv)

    args.cache_dir = Path(getattr(args, "cache_dir", default_cache_dir()))
    args.data_base = getattr(args, "data_base", DATA_BASE)

    try:
        return args.func(args)
    except (Conflict, HTTPException, OSError, TypeError, ValueError) as e:
        # urllib's errors are OSErrors; a connection that drops mid-response
        # is an HTTPException (IncompleteRead).
        print(f"error: {e}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
