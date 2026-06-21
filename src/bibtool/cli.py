from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import Sequence, TextIO

from .bibtex import BibEntry, assign_generated_key, dedupe_tokens, normalize_for_match, parse_bibtex, sort_key, write_bibtex
from .inspire import InspireClient, InspireError


class CliError(RuntimeError):
    pass


def main() -> None:
    raise SystemExit(run())


def run(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    provider: InspireClient | None = None,
) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    provider = provider or InspireClient()

    try:
        if argv and argv[0] == "search":
            return _run_search(argv[1:], stdout=stdout, provider=provider)
        return _run_default(argv, stdin=stdin, stdout=stdout, provider=provider)
    except (CliError, InspireError, ValueError) as error:
        stderr.write(f"bibtool: {error}\n")
        return 1


def _run_default(
    argv: Sequence[str],
    *,
    stdin: TextIO,
    stdout: TextIO,
    provider: InspireClient,
) -> int:
    parser = argparse.ArgumentParser(prog="bibtool")
    parser.add_argument("target", nargs="?")
    parser.add_argument("--bib", dest="bib_path", default="references.bib")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--name", nargs="+")
    mode.add_argument("--title", nargs="+")
    args = parser.parse_args(list(argv))

    if args.name or args.title:
        if args.target:
            raise CliError("Use --bib to choose the output file when importing by --name or --title.")
        query = " ".join(args.name or args.title)
        target_path = Path(args.bib_path)
        incoming = provider.fetch_author_entries(query) if args.name else provider.fetch_title_entries(query)
        return _add_entries(
            target_path=target_path,
            incoming=incoming,
            stdin=stdin,
            stdout=stdout,
            origin=f'INSPIRE query "{query}"',
        )

    if not args.target:
        raise CliError("Provide a target BibTeX path or use --name/--title.")
    return _merge_template(target_path=Path(args.target), stdin=stdin, stdout=stdout)


def _run_search(argv: Sequence[str], *, stdout: TextIO, provider: InspireClient) -> int:
    parser = argparse.ArgumentParser(prog="bibtool search")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--name", nargs="+")
    mode.add_argument("--title", nargs="+")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(list(argv))

    query = " ".join(args.name or args.title)
    results = provider.search_author(query, limit=args.limit) if args.name else provider.search_title(query, limit=args.limit)
    if not results:
        stdout.write("No matching records found.\n")
        return 0

    for result in results:
        author = result.first_author or "Unknown"
        year = result.year or "????"
        stdout.write(f"[{result.recid}] {author} ({year}) {result.title}\n")
    return 0


def _merge_template(*, target_path: Path, stdin: TextIO, stdout: TextIO) -> int:
    template_dir = os.environ.get("LATEX_TEMPLATE_DIR")
    if not template_dir:
        raise CliError("LATEX_TEMPLATE_DIR is not set.")

    source_path = Path(template_dir) / "references.bib"
    if not source_path.exists():
        raise CliError(f"Template bibliography not found at {source_path}.")

    incoming = _read_bib_entries(source_path)
    return _add_entries(
        target_path=target_path,
        incoming=incoming,
        stdin=stdin,
        stdout=stdout,
        origin=str(source_path),
    )


def _add_entries(
    *,
    target_path: Path,
    incoming: list[BibEntry],
    stdin: TextIO,
    stdout: TextIO,
    origin: str,
) -> int:
    existing = _read_bib_entries(target_path) if target_path.exists() else []
    merged_entries, added = _merge_entries(existing, incoming)

    if not added:
        stdout.write(f"No new entries added from {origin}.\n")
        return 0

    _double_confirm_large_additions(len(added), stdin=stdin, stdout=stdout)
    target_path.write_text(write_bibtex(merged_entries), encoding="utf-8")
    stdout.write(f"Added {len(added)} entries to {target_path}.\n")
    return 0


def _read_bib_entries(path: Path) -> list[BibEntry]:
    if not path.exists():
        return []
    return parse_bibtex(path.read_text(encoding="utf-8"))


def _merge_entries(existing: list[BibEntry], incoming: list[BibEntry]) -> tuple[list[BibEntry], list[BibEntry]]:
    used_keys = {entry.key.lower() for entry in existing}
    seen = set().union(*(dedupe_tokens(entry) for entry in existing))
    merged = [entry.clone() for entry in existing]
    added: list[BibEntry] = []

    for entry in incoming:
        tokens = dedupe_tokens(entry)
        if tokens and tokens & seen:
            continue
        if not tokens and _entry_looks_empty(entry):
            continue
        prepared = assign_generated_key(entry, used_keys)
        merged.append(prepared)
        added.append(prepared)
        seen.update(dedupe_tokens(prepared))

    merged.sort(key=sort_key)
    return merged, added


def _entry_looks_empty(entry: BibEntry) -> bool:
    return not normalize_for_match(entry.title) and not normalize_for_match(entry.author)


def _double_confirm_large_additions(count: int, *, stdin: TextIO, stdout: TextIO) -> None:
    if count <= 10:
        return

    if not getattr(stdin, "isatty", lambda: False)():
        raise CliError(f"Refusing to add {count} entries non-interactively; rerun in a terminal to confirm.")

    stdout.write(f"{count} entries are about to be added.\n")
    stdout.write('Type "add" to continue: ')
    first = stdin.readline().strip().lower()
    if first != "add":
        raise CliError("Aborted before writing changes.")

    stdout.write(f'Type "{count}" to confirm the final add: ')
    second = stdin.readline().strip()
    if second != str(count):
        raise CliError("Aborted before writing changes.")
