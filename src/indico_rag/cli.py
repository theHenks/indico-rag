"""Command line entry point."""
from __future__ import annotations

import argparse
import asyncio
import sys

from indico_rag import store
from indico_rag.access.base import AuthRequired
from indico_rag.indico.endpoints import parse_event_ref
from indico_rag.scan import format_agenda, scan_event


def _cmd_scan(args: argparse.Namespace) -> int:
    config = store.load_config()
    try:
        host, event_id = parse_event_ref(args.event, config.default_host or None)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        event = asyncio.run(
            scan_event(host, event_id, token=store.get_token(host),
                       progress=lambda m: print(f"… {m}", file=sys.stderr))
        )
    except AuthRequired as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3

    store.save_scan(event)
    print(format_agenda(event))
    return 0


def _cmd_scans(_: argparse.Namespace) -> int:
    records = store.list_scans()
    if not records:
        print("no scans yet")
        return 0
    for record in records:
        print(f"{record.fetched_at}  {record.event_id:>8}  {record.talk_count:>4} talks  "
              f"{record.first_day}..{record.last_day}  {record.title}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="indico-rag",
                                     description="Download an Indico event's talks as PDFs.")
    sub = parser.add_subparsers(dest="command", required=True)

    scan_parser = sub.add_parser("scan", help="fetch an event agenda and print it")
    scan_parser.add_argument("event", help="event URL or bare event id")
    scan_parser.set_defaults(func=_cmd_scan)

    scans_parser = sub.add_parser("scans", help="list cached scans")
    scans_parser.set_defaults(func=_cmd_scans)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
