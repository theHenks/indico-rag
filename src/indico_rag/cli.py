"""Command line entry point."""
from __future__ import annotations

import argparse
import asyncio
import sys
from urllib.parse import urlparse

from indico_rag import store
from indico_rag.access.base import AuthRequired
from indico_rag.indico.endpoints import parse_event_ref
from indico_rag.scan import format_agenda, scan_event


def _resolve_token(host: str, cli_token: str | None) -> str | None:
    """Use and persist a `--token` argument, else fall back to the stored token."""
    if cli_token:
        store.set_token(host, cli_token)
        return cli_token
    return store.get_token(host)


def _resolve_host_ref(text: str) -> str:
    """Extract a bare host from a host URL or any event URL under it."""
    try:
        host, _event_id = parse_event_ref(text)
        return host
    except ValueError:
        pass
    parsed = urlparse(text.strip())
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    raise ValueError(f"not a host or event URL: {text!r}")


def _print_auth_required(exc: AuthRequired) -> None:
    print(f"error: {exc}", file=sys.stderr)
    if store.get_token(exc.host) is None:
        print(
            f"no token is stored for {exc.host}. Create a personal token "
            f"(it looks like indp_<42 characters>) at {exc.host}/user/tokens/, "
            f"then run: indico-rag login {exc.host} --token <TOKEN>",
            file=sys.stderr,
        )
        print("that token is sent as an 'Authorization: Bearer' header.", file=sys.stderr)


def _cmd_scan(args: argparse.Namespace) -> int:
    config = store.load_config()
    try:
        host, event_id = parse_event_ref(args.event, config.default_host or None)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    token = _resolve_token(host, args.token)

    try:
        event = asyncio.run(
            scan_event(host, event_id, token=token,
                       progress=lambda m: print(f"… {m}", file=sys.stderr))
        )
    except AuthRequired as exc:
        _print_auth_required(exc)
        return 3

    store.save_scan(event)
    print(format_agenda(event))
    return 0


def _cmd_login(args: argparse.Namespace) -> int:
    try:
        host = _resolve_host_ref(args.host)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    store.set_token(host, args.token)
    path = store.state_root() / "credentials.json"
    print(f"token saved for {host} in {path}")
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


def _cmd_fetch(args: argparse.Namespace) -> int:
    from pathlib import Path

    from indico_rag.pipeline.runner import build_jobs, run_jobs

    config = store.load_config()
    try:
        host, event_id = parse_event_ref(args.event, config.default_host or None)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.output:
        config.output_root = Path(args.output).expanduser()
    if args.extract:
        config.extract_text = True
    if args.concurrency:
        config.download_concurrency = args.concurrency
        if args.concurrency > 16:
            print(f"warning: {args.concurrency} parallel downloads is impolite to the host",
                  file=sys.stderr)

    token = _resolve_token(host, args.token)

    try:
        event = asyncio.run(scan_event(host, event_id, token=token,
                                       progress=lambda m: print(f"… {m}", file=sys.stderr)))
    except AuthRequired as exc:
        _print_auth_required(exc)
        return 3

    selected = {t.contribution_id for t in event.all_talks()}
    jobs, no_material = build_jobs(event, selected, config.output_root)
    print(f"{len(jobs)} to download, {len(no_material)} without material", file=sys.stderr)

    summary = asyncio.run(run_jobs(
        event, jobs, no_material, config=config, root=config.output_root,
        token=token,
        on_update=lambda tid, stage, detail: print(f"  [{stage}] {detail}", file=sys.stderr),
    ))

    print(f"\ndone={summary.done} failed={summary.failed} "
          f"skipped={summary.skipped} no-material={summary.no_material}")
    for title, reason in summary.failures:
        print(f"  FAILED  {title}: {reason}")
    if summary.extraction_failures:
        print("\ntext extraction failed for (the PDF itself is fine):")
        for title, reason in summary.extraction_failures:
            print(f"  NO TEXT  {title}: {reason}")
    return 1 if summary.failed or summary.extraction_failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="indico-rag",
                                     description="Download an Indico event's talks as PDFs.")
    sub = parser.add_subparsers(dest="command", required=True)

    scan_parser = sub.add_parser("scan", help="fetch an event agenda and print it")
    scan_parser.add_argument("event", help="event URL or bare event id")
    scan_parser.add_argument("--token", help="personal API token to use and store for this host")
    scan_parser.set_defaults(func=_cmd_scan)

    scans_parser = sub.add_parser("scans", help="list cached scans")
    scans_parser.set_defaults(func=_cmd_scans)

    fetch_parser = sub.add_parser("fetch", help="download every talk in an event")
    fetch_parser.add_argument("event", help="event URL or bare event id")
    fetch_parser.add_argument("-o", "--output", help="output root directory")
    fetch_parser.add_argument("-j", "--concurrency", type=int, help="parallel downloads")
    fetch_parser.add_argument("--extract", action="store_true", help="write text sidecars")
    fetch_parser.add_argument("--token", help="personal API token to use and store for this host")
    fetch_parser.set_defaults(func=_cmd_fetch)

    login_parser = sub.add_parser("login", help="store a personal API token for a host")
    login_parser.add_argument("host", help="host URL or any event URL on that host")
    login_parser.add_argument("--token", required=True,
                              help="personal API token, sent as an Authorization: Bearer header")
    login_parser.set_defaults(func=_cmd_login)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
