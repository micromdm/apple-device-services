#!/usr/bin/env python3
"""Apply the JSON Patches (RFC 6902) that re-apply curated schema adjustments.

    apply_schema_patches.py PATCHES_DIR SCHEMAS_DIR [--dry-run]

A patches directory holds one JSON Patch per schema of the schemas
directory, named the same way — dep/patches for dep/schemas, say. Each
patch turns the schema that the sync generated from Apple's
documentation back into the curated schema. Every op that overwrites or
removes a value is headed by a "test" op pinning the granular value it
replaces: a schema a patch has already applied — or one a newer sync has
changed — fails the test, and nothing is written. (An "add" carries no
test: RFC 6902 cannot test that a key is absent. The tool checks that
itself — an add that would overwrite an existing value fails instead.)

The applied schemas are written as plain two-space JSON. Canonical key
order and formatting are the job of the steps that follow, the way the
sync formats after generating.

Requires the jsonpatch module.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import jsonpatch


class PatchError(Exception):
    """A patch that will not apply to its schema."""


def path_exists(schema: object, path: str) -> bool:
    """Return True if a JSON Pointer resolves in a schema."""
    try:
        jsonpatch.JsonPointer(path).resolve(schema)
    except jsonpatch.JsonPointerException:
        return False
    return True


def apply_ops(ops: list, schema: object) -> object:
    """Apply a patch's operations one by one, naming the one that fails."""
    working = schema
    for index, op in enumerate(ops, start=1):
        where = f"op {index} ({op.get('op')} {op.get('path')!r})"
        if op.get("op") == "add" and path_exists(working, op.get("path", "")):
            # RFC 6902 "add" overwrites the value of an existing key, so
            # refuse rather than clobber what is already in the schema.
            raise PatchError(f"{where}: add would overwrite an existing value")
        try:
            working = jsonpatch.JsonPatch([op]).apply(working)
        except (jsonpatch.JsonPatchException, jsonpatch.JsonPointerException) as e:
            if op.get("op") == "test":
                raise PatchError(
                    f"{where}: value does not match "
                    "(already patched, or changed by a newer sync)"
                ) from e
            raise PatchError(f"{where}: {e}") from e
    return working


def command_apply(args: argparse.Namespace) -> int:
    """Apply every patch of the patches directory to its schema."""
    patches = Path(args.patches_dir)
    schemas = Path(args.schemas_dir)
    if not patches.is_dir():
        print(f"error: not a directory: {patches}", file=sys.stderr)
        return 1

    writes: list[tuple[Path, object]] = []
    for patch_path in sorted(patches.rglob("*.json")):
        schema_path = schemas / patch_path.relative_to(patches)
        try:
            ops = json.loads(patch_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"error: {patch_path}: {e}", file=sys.stderr)
            return 1
        if not isinstance(ops, list):
            print(f"error: {patch_path}: not a JSON Patch document", file=sys.stderr)
            return 1
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"error: {schema_path}: {e}", file=sys.stderr)
            return 1
        try:
            # Everything is applied in memory first: one failing patch
            # leaves every schema untouched.
            patched = apply_ops(ops, schema)
        except PatchError as e:
            print(f"error: {schema_path}: {e}", file=sys.stderr)
            return 1
        writes.append((schema_path, patched))

    if not args.dry_run:
        for schema_path, schema in writes:
            text = json.dumps(schema, indent=2, ensure_ascii=False) + "\n"
            schema_path.write_text(text, encoding="utf-8")
    verb = "would apply" if args.dry_run else "applied"
    print(f"{verb} {len(writes)} patches to {schemas}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser."""
    parser = argparse.ArgumentParser(
        prog="apply_schema_patches.py",
        description=(
            "Apply the JSON Patches that re-apply curated adjustments over "
            "generated schemas."
        ),
    )
    parser.add_argument(
        "patches_dir",
        metavar="PATCHES_DIR",
        help='directory of the JSON Patches (e.g. "dep/patches")',
    )
    parser.add_argument(
        "schemas_dir",
        metavar="SCHEMAS_DIR",
        help='directory of the schemas to patch (e.g. "dep/schemas")',
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="apply the patches in memory, but write nothing",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse the command line and apply the patches."""
    return command_apply(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
