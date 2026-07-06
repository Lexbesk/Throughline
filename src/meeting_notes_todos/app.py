"""Entrypoint. Subcommands: hello (M0), extract (M1), ingest (M3), list (M2)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from uuid import uuid4

from .config import Config, load_config
from .models import ActionItem
from .pipeline.extract import ExtractionError, extract_action_items
from .pipeline.merge import MergeResult, apply_decisions
from .pipeline.reconcile import reconcile
from .profile import load_profile
from .prompts import load_prompt
from .providers import build_provider, resolve_provider_name
from .store import build_store
from .usage import record_run, summarize

try:  # optional convenience: load ANTHROPIC_API_KEY from a local .env
    from dotenv import load_dotenv

    load_dotenv()
except ModuleNotFoundError:  # pragma: no cover - dotenv is a declared dependency
    pass


def _ensure_api_key(config: Config) -> bool:
    required = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(
        resolve_provider_name(config.llm)
    )
    if required and not os.environ.get(required):
        print(
            f"{required} is not set. Export it, or copy .env.example to .env "
            "and fill it in, then re-run.",
            file=sys.stderr,
        )
        return False
    return True


def cmd_hello(_args: argparse.Namespace) -> int:
    """Round-trip a trivial prompt through the configured provider (M0 acceptance)."""
    config = load_config()
    if not _ensure_api_key(config):
        return 1
    provider = build_provider(config.llm)
    system_prompt = load_prompt(config.prompts.dir, config.prompts.system)
    result = provider.complete(
        system_prompt=system_prompt,
        user_content="Say hello in one short sentence to confirm the pipeline is wired up.",
    )
    print(result.text)
    print(
        f"[tokens] in={result.usage.input_tokens} "
        f"out={result.usage.output_tokens} model={config.llm.model}"
    )
    record_run(config.usage.path, command="hello", provider=resolve_provider_name(config.llm),
               model=config.llm.model, usage=result.usage)
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    """Extract structured action items from notes and print them (M1; read-only)."""
    config = load_config()
    if not _ensure_api_key(config):
        return 1

    notes = _read_notes(args.notes).strip()
    if not notes:
        print("No notes provided (empty file or stdin).", file=sys.stderr)
        return 1

    meeting_date, err = _parse_meeting_date(args.date)
    if err:
        print(err, file=sys.stderr)
        return 2
    meeting_id = args.meeting_id or _meeting_id(meeting_date)

    provider = build_provider(config.llm)
    try:
        run = extract_action_items(
            provider=provider,
            prompts=config.prompts,
            notes=notes,
            meeting_date=meeting_date,
            meeting_id=meeting_id,
            profile=load_profile(config.store.path),
        )
    except ExtractionError as exc:
        print(f"Extraction failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps([item.model_dump(mode="json") for item in run.items], indent=2))
    else:
        _print_items(run.items)
        print(
            f"\n[tokens] in={run.usage.input_tokens} out={run.usage.output_tokens} "
            f"model={config.llm.model} · {len(run.items)} item(s) · meeting={meeting_id}"
        )
    record_run(config.usage.path, command="extract", provider=resolve_provider_name(config.llm),
               model=config.llm.model, usage=run.usage)
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    """Extract → reconcile against the store → preview; --commit applies (M3)."""
    config = load_config()
    if not _ensure_api_key(config):
        return 1

    notes = _read_notes(args.notes).strip()
    if not notes:
        print("No notes provided (empty file or stdin).", file=sys.stderr)
        return 1

    meeting_date, err = _parse_meeting_date(args.date)
    if err:
        print(err, file=sys.stderr)
        return 2
    meeting_id = args.meeting_id or _meeting_id(meeting_date)

    provider = build_provider(config.llm)
    store = build_store(config.store)
    existing = store.load()

    try:
        run = extract_action_items(
            provider=provider,
            prompts=config.prompts,
            notes=notes,
            meeting_date=meeting_date,
            meeting_id=meeting_id,
            profile=load_profile(config.store.path),
        )
    except ExtractionError as exc:
        print(f"Extraction failed: {exc}", file=sys.stderr)
        return 1

    rec = reconcile(
        provider=provider,
        prompts=config.prompts,
        new_items=run.items,
        existing_items=existing,
    )
    result = apply_decisions(existing, rec.decisions)
    _print_merge_preview(result)

    usage = run.usage + rec.usage
    if args.commit:
        store.save(result.final)
        print(
            f"\ncommitted to {config.store.path}: {len(result.added)} added, "
            f"{len(result.updated)} updated, {len(result.skipped)} skipped"
        )
    else:
        print(
            "\n(dry run — nothing written. Re-run with --commit to apply; "
            "per-item review lands in M4.)"
        )
    print(
        f"[tokens] in={usage.input_tokens} out={usage.output_tokens} "
        f"model={config.llm.model} · meeting={meeting_id}"
    )
    record_run(config.usage.path, command="ingest", provider=resolve_provider_name(config.llm),
               model=config.llm.model, usage=usage)
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    """Print the current TODO store (M2; read path, no API key needed)."""
    config = load_config()
    store = build_store(config.store)
    items = store.load()
    _print_items(items)
    print(f"\n{len(items)} item(s) in {config.store.path}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the local web app (review UI) at localhost (M4)."""
    try:
        import uvicorn
    except ModuleNotFoundError:  # pragma: no cover
        print("uvicorn is not installed; run `pip install -e .` first.", file=sys.stderr)
        return 1
    from .web.app import app as web_app

    print(f"Serving the review UI at http://{args.host}:{args.port}  (Ctrl-C to stop)")
    uvicorn.run(web_app, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_usage(_args: argparse.Namespace) -> int:
    """Show token-usage totals from the per-run log (M5)."""
    config = load_config()
    summary = summarize(config.usage.path)
    total = summary["input_tokens"] + summary["output_tokens"]
    print(
        f"{summary['runs']} run(s) · in={summary['input_tokens']} "
        f"out={summary['output_tokens']} total={total} tokens"
    )
    for model, m in sorted(summary["by_model"].items()):
        print(f"  {model}: {m['runs']} run(s) · in={m['input_tokens']} out={m['output_tokens']}")
    print(f"(log: {config.usage.path})")
    return 0


def _read_notes(path: str | None) -> str:
    if path:
        with open(path, encoding="utf-8") as f:
            return f.read()
    return sys.stdin.read()


def _parse_meeting_date(date_arg: str | None) -> tuple[date, str | None]:
    if not date_arg:
        return date.today(), None
    try:
        return date.fromisoformat(date_arg), None
    except ValueError:
        return date.today(), f"Invalid --date {date_arg!r}; expected YYYY-MM-DD."


def _meeting_id(meeting_date: date) -> str:
    return f"meeting-{meeting_date.isoformat()}-{uuid4().hex[:8]}"


def _print_items(items: list[ActionItem]) -> None:
    if not items:
        print("(no action items found)")
        return
    for item in items:
        print(f"- [{item.status}] {item.title}{_meta_suffix(item)}")
        print(f"    ↳ {item.source_snippet}")


def _print_merge_preview(result: MergeResult) -> None:
    if not (result.added or result.updated or result.skipped):
        print("(no action items extracted)")
        return
    for item in result.added:
        print(f"  NEW     + {item.title}{_meta_suffix(item)}")
    for item, changes in result.updated:
        print(f"  UPDATE  ~ {item.title}  (adds {', '.join(changes)})")
    for item, reason in result.skipped:
        print(f"  SKIP    = {item.title}  ({reason})")


def _meta_suffix(item: ActionItem) -> str:
    bits = []
    if item.owner:
        bits.append(f"owner: {item.owner}")
    if item.priority:
        bits.append(f"priority: {item.priority}")
    if item.due_date:
        bits.append(f"due: {item.due_date.isoformat()}")
    elif item.due_date_text:
        bits.append(f"due (unresolved): {item.due_date_text}")
    return ("  · " + "  · ".join(bits)) if bits else ""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meeting-notes-todos")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("hello", help="round-trip a trivial prompt through the provider")

    for name, help_text in (
        ("extract", "extract action items from notes and print them (read-only)"),
        ("ingest", "extract, reconcile against the store, and (with --commit) merge"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("notes", nargs="?", help="path to a notes file; omit to read stdin")
        p.add_argument(
            "--date",
            help="meeting date YYYY-MM-DD for resolving relative dates (default: today)",
        )
        p.add_argument(
            "--meeting-id", help="id for this meeting/session (default: generated)"
        )
        if name == "extract":
            p.add_argument("--json", action="store_true", help="print items as JSON")
        if name == "ingest":
            p.add_argument(
                "--commit",
                action="store_true",
                help="apply the merge to the store (default: dry-run preview)",
            )

    sub.add_parser("list", help="print the current TODO store")

    serve = sub.add_parser("serve", help="run the local web app (review UI) at localhost")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    sub.add_parser("usage", help="show token-usage totals from the per-run log")

    return parser


_HANDLERS = {
    "hello": cmd_hello,
    "extract": cmd_extract,
    "ingest": cmd_ingest,
    "list": cmd_list,
    "serve": cmd_serve,
    "usage": cmd_usage,
}


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    command = args.command or "hello"
    return _HANDLERS[command](args)


if __name__ == "__main__":
    raise SystemExit(main())
