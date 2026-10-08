"""CLI: python -m longbox.scan --path <comics folder> --db <db path>"""

from __future__ import annotations

import argparse
import os
import sys

from .scanner import scan_library


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m longbox.scan",
        description="Index a comics folder (.cbz/.cbr) into the Longbox SQLite database.",
    )
    parser.add_argument(
        "--path",
        default=os.environ.get("LONGBOX_COMICS"),
        help="comics folder to index (env: LONGBOX_COMICS)",
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("LONGBOX_DB", os.path.join("data", "longbox.db")),
        help="SQLite database file (env: LONGBOX_DB, default data/longbox.db)",
    )
    parser.add_argument("--quiet", action="store_true", help="only print the summary")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.path:
        print("error: --path is required (the comics folder)", file=sys.stderr)
        return 2

    if args.quiet:
        progress = None
    else:
        def progress(count: int, _total: int, path: str) -> None:
            print(f"  [{count}] {os.path.basename(path)}")

    try:
        summary = scan_library(args.path, args.db, progress=progress)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print("")
    print("Longbox scan summary")
    print(f"  comics folder : {summary['path']}")
    print(f"  database      : {summary['db']}")
    print(f"  found         : {summary['found']}")
    print(f"  indexed (new) : {summary['indexed']}")
    print(f"  updated       : {summary['updated']}")
    print(f"  errors        : {summary['errors']}")
    print(f"  missing       : {summary['missing']}")
    print(f"  rows in db    : {summary['total_in_db']}")
    print(f"  elapsed       : {summary['elapsed']}s")
    if summary["error_files"]:
        print("")
        print("Files with problems:")
        for path, message in summary["error_files"]:
            print(f"  {path}")
            print(f"      {message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
