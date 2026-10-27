#!/usr/bin/env python3
"""Format JSON files in place: sorted keys, two-space indentation.

    sortkeys.py FILE [FILE ...] [--check]

With --check, nothing is written: the files that are not formatted are
named, and the exit status is 1 if there are any.
"""

import argparse
import json
import sys
from pathlib import Path


def formatted(text: str, indent: int) -> str:
    """Return the formatted text of a JSON document."""
    data = json.loads(text)
    return json.dumps(data, indent=indent, ensure_ascii=False, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Format JSON files in place: sorted keys, indentation."
    )
    parser.add_argument("paths", type=Path, nargs="+", metavar="FILE")
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="number of spaces of indentation (default: 2)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="write nothing; fail if a file is not formatted",
    )
    args = parser.parse_args()

    status = 0
    for path in args.paths:
        try:
            # newline="": compare the bytes as they are, CRLF and all.
            with path.open(encoding="utf-8", newline="") as fh:
                text = fh.read()
            result = formatted(text, args.indent)
            if result == text:
                continue
            if args.check:
                print(f"not formatted: {path}", file=sys.stderr)
                status = 1
            else:
                path.write_text(result, encoding="utf-8")
        except (OSError, ValueError) as e:
            print(f"error: {path}: {e}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
