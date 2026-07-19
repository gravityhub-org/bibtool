from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import Sequence, TextIO

from .bibtex import (
    BibEntry,
    assign_generated_key,
    dedupe_tokens,
    entries_equivalent,
    find_matching_entry_index,
    merge_entry_fields,
    normalize_for_match,
    parse_bibtex,
    sort_key,
    write_bibtex,
)
from .inspire import (
    InspireClient,
    InspireError,
    SearchResult,
    parse_literature_id_tokens,
)


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
        completion_result = _maybe_handle_completion(argv, stdout=stdout)
        if completion_result is not None:
            return completion_result
        if argv and argv[0] == "search":
            return _run_search(argv[1:], stdout=stdout, provider=provider)
        if argv and argv[0] == "download":
            return _run_download(argv[1:], stdin=stdin, stdout=stdout, provider=provider)
        if argv and argv[0] == "update":
            return _run_update(argv[1:], stdin=stdin, stdout=stdout, provider=provider)
        return _run_default(argv, stdin=stdin, stdout=stdout, provider=provider)
    except (CliError, InspireError, ValueError) as error:
        stderr.write(f"bibtool: {error}\n")
        return 1


def _maybe_handle_completion(argv: Sequence[str], *, stdout: TextIO) -> int | None:
    if not argv:
        return None

    if argv[0] == "--print-completion":
        if len(argv) != 2:
            raise CliError("Usage: bibtool --print-completion bash")
        shell = argv[1]
        if shell != "bash":
            raise CliError("Only bash completion is supported.")
        stdout.write(_bash_completion_script())
        return 0

    if argv[0] == "--install-completion":
        shell = argv[1] if len(argv) > 1 else "bash"
        if len(argv) > 2:
            raise CliError("Usage: bibtool --install-completion [bash]")
        if shell != "bash":
            raise CliError("Only bash completion is supported.")
        destination = _install_bash_completion()
        stdout.write(f"Installed bash completion to {destination}\n")
        return 0

    return None


def _run_default(
    argv: Sequence[str],
    *,
    stdin: TextIO,
    stdout: TextIO,
    provider: InspireClient,
) -> int:
    parser = argparse.ArgumentParser(prog="bibtool")
    parser.add_argument("target", nargs="?")
    parser.add_argument("--bib", dest="bib_path")
    parser.add_argument("--y", action="store_true", dest="yes")
    parser.add_argument("--query", nargs="+")
    parser.add_argument("--name", nargs="+")
    parser.add_argument("--title", nargs="+")
    args = parser.parse_args(list(argv))

    if args.query or args.name or args.title:
        if args.target:
            raise CliError("Use --bib to choose the output file when importing by query.")
        target_path = _default_import_target_path(args.bib_path)
        if args.query and (args.name or args.title):
            raise CliError("Use --query by itself, or combine --name and --title.")

        query = " ".join(args.query) if args.query else None
        name = " ".join(args.name) if args.name else None
        title = " ".join(args.title) if args.title else None
        incoming = list(
            provider.lookup(
                query=query,
                name=name,
                title=title,
                limit=None,
                as_entries=True,
            )
        )
        origin = _lookup_origin(query=query, name=name, title=title)
        return _add_entries(
            target_path=target_path,
            incoming=incoming,
            stdin=stdin,
            stdout=stdout,
            origin=origin,
            auto_confirm=args.yes,
        )

    if not args.target:
        raise CliError("Provide a target BibTeX path or use --query/--name/--title.")
    return _merge_template(target_path=Path(args.target), stdin=stdin, stdout=stdout, auto_confirm=args.yes)


def _run_update(
    argv: Sequence[str],
    *,
    stdin: TextIO,
    stdout: TextIO,
    provider: InspireClient,
) -> int:
    parser = argparse.ArgumentParser(prog="bibtool update")
    parser.add_argument("target", nargs="?")
    parser.add_argument("--bib", dest="bib_path")
    parser.add_argument("--workers", type=int, default=8, help="parallel INSPIRE requests (default: 8)")
    args = parser.parse_args(list(argv))

    if args.workers < 1:
        raise CliError("--workers must be at least 1.")

    if args.bib_path and args.target:
        raise CliError("Use either a positional bibliography path or --bib, not both.")

    if args.bib_path:
        target_path = Path(args.bib_path)
    elif args.target:
        target_path = Path(args.target)
    else:
        target_path = _default_import_target_path(None)

    if not target_path.exists():
        raise CliError(f"Bibliography not found at {target_path}.")

    existing = _read_bib_entries(target_path)
    if not existing:
        stdout.write(f"No entries to update in {target_path}.\n")
        return 0

    merged, updated, skipped = _refresh_entries(existing, provider, workers=args.workers)

    if not updated:
        stdout.write(f"No entries updated in {target_path}.\n")
        if skipped:
            stdout.write(f"Skipped {len(skipped)} entries without an INSPIRE match.\n")
        return 0

    _double_confirm_large_additions(
        len(updated),
        stdin=stdin,
        stdout=stdout,
        auto_confirm=True,
        action="update",
    )
    target_path.write_text(write_bibtex(merged), encoding="utf-8")
    stdout.write(f"Updated {len(updated)} entries in {target_path}.\n")
    if skipped:
        stdout.write(f"Skipped {len(skipped)} entries without an INSPIRE match.\n")
    return 0


def _refresh_entries(
    existing: list[BibEntry],
    provider: InspireClient,
    *,
    workers: int = 8,
) -> tuple[list[BibEntry], list[BibEntry], list[str]]:
    merged: list[BibEntry] = []
    updated: list[BibEntry] = []
    skipped: list[str] = []

    refresh = getattr(provider, "refresh_entries", None)
    if callable(refresh):
        refreshed_list = refresh(existing, workers=workers)
    else:
        refreshed_list = [provider.refresh_entry(entry) for entry in existing]

    for entry, refreshed in zip(existing, refreshed_list, strict=True):
        if refreshed is None:
            skipped.append(entry.key)
            merged.append(entry.clone())
            continue
        if entries_equivalent(entry, refreshed):
            merged.append(entry.clone())
            continue
        merged.append(refreshed)
        updated.append(refreshed)

    merged.sort(key=sort_key)
    return merged, updated, skipped


def _run_search(argv: Sequence[str], *, stdout: TextIO, provider: InspireClient) -> int:
    parser = argparse.ArgumentParser(prog="bibtool search")
    parser.add_argument("query", nargs="*")
    parser.add_argument("--name", nargs="+")
    parser.add_argument("--title", nargs="+")
    parser.add_argument("--arxiv", nargs="+", dest="arxiv_ids")
    parser.add_argument("--inspire", nargs="+", dest="inspire_ids")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(list(argv))

    results = _resolve_search_results(args, provider=provider)

    if not results:
        stdout.write("No matching records found.\n")
        return 0

    for result in results:
        author = result.first_author or "Unknown"
        year = result.year or "????"
        url = f"https://inspirehep.net/literature/{result.recid}"
        linked_title = _terminal_link(result.title, url)
        arxiv = f" arXiv:{result.arxiv_id}" if result.arxiv_id else ""
        stdout.write(f"[{result.recid}] {author} ({year}) {linked_title}{arxiv}\n")
    return 0


def _resolve_search_results(args: argparse.Namespace, *, provider: InspireClient) -> list[SearchResult]:
    has_flags = bool(args.arxiv_ids or args.inspire_ids)
    has_name_title = bool(args.name or args.title)
    query_tokens = list(args.query or [])

    if has_flags and (query_tokens or has_name_title):
        raise CliError("Use --arxiv/--inspire by themselves, or a search query / --name/--title.")
    if has_flags:
        return _results_from_explicit_ids(
            arxiv_ids=args.arxiv_ids or [],
            inspire_ids=args.inspire_ids or [],
            provider=provider,
        )

    query, name, title = _normalize_lookup_terms(args)
    if not query and not name and not title:
        raise CliError("Search query cannot be empty.")

    return list(
        provider.lookup(
            query=query,
            name=name,
            title=title,
            limit=args.limit,
            as_entries=False,
        )
    )


def _run_download(
    argv: Sequence[str],
    *,
    stdin: TextIO,
    stdout: TextIO,
    provider: InspireClient,
) -> int:
    parser = argparse.ArgumentParser(prog="bibtool download")
    parser.add_argument("query", nargs="*")
    parser.add_argument("--name", nargs="+")
    parser.add_argument("--title", nargs="+")
    parser.add_argument("--arxiv", nargs="+", dest="arxiv_ids")
    parser.add_argument("--inspire", nargs="+", dest="inspire_ids")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--dir", dest="directory", default=".")
    parser.add_argument("--y", action="store_true", dest="yes")
    args = parser.parse_args(list(argv))

    results = _resolve_download_results(args, provider=provider)

    if not results:
        stdout.write("No matching records found.\n")
        return 0

    downloadable = [result for result in results if result.arxiv_id]
    skipped_no_arxiv = [result for result in results if not result.arxiv_id]

    if not downloadable:
        stdout.write("No matching records with an arXiv eprint to download.\n")
        return 0

    destination = Path(args.directory)
    destination.mkdir(parents=True, exist_ok=True)

    to_fetch = [result for result in downloadable if not (destination / _pdf_filename(result.arxiv_id)).exists()]
    already_present = len(downloadable) - len(to_fetch)

    if not to_fetch:
        stdout.write(f"All {len(downloadable)} matching PDFs already exist in {destination}.\n")
        if skipped_no_arxiv:
            stdout.write(f"Skipped {len(skipped_no_arxiv)} records without an arXiv eprint.\n")
        return 0

    _double_confirm_large_additions(
        len(to_fetch),
        stdin=stdin,
        stdout=stdout,
        auto_confirm=args.yes,
        action="download",
    )

    downloaded = 0
    failed: list[str] = []
    fetch_pdf = getattr(provider, "fetch_pdf", None)
    if not callable(fetch_pdf):
        raise CliError("Provider does not support PDF downloads.")

    for result in to_fetch:
        path = destination / _pdf_filename(result.arxiv_id)
        try:
            path.write_bytes(fetch_pdf(result.arxiv_id))
        except InspireError as error:
            failed.append(f"{result.arxiv_id} ({error})")
            continue
        downloaded += 1
        stdout.write(f"Downloaded {path}\n")

    if downloaded:
        stdout.write(f"Downloaded {downloaded} PDFs to {destination}.\n")
    if already_present:
        stdout.write(f"Skipped {already_present} existing PDFs.\n")
    if skipped_no_arxiv:
        stdout.write(f"Skipped {len(skipped_no_arxiv)} records without an arXiv eprint.\n")
    if failed:
        stdout.write(f"Failed to download {len(failed)} PDFs.\n")
        for item in failed:
            stdout.write(f"  {item}\n")
        return 1
    return 0


def _resolve_download_results(args: argparse.Namespace, *, provider: InspireClient) -> list[SearchResult]:
    has_flags = bool(args.arxiv_ids or args.inspire_ids)
    has_name_title = bool(args.name or args.title)
    query_tokens = list(args.query or [])

    if has_flags and (query_tokens or has_name_title):
        raise CliError("Use --arxiv/--inspire by themselves, or a search query / --name/--title.")
    if has_flags:
        return _results_from_explicit_ids(
            arxiv_ids=args.arxiv_ids or [],
            inspire_ids=args.inspire_ids or [],
            provider=provider,
        )

    query, name, title = _normalize_lookup_terms(args)
    if not query and not name and not title:
        raise CliError("Download query cannot be empty.")

    if query is not None and name is None and title is None:
        id_results = _maybe_results_from_query_ids(query_tokens, provider=provider)
        if id_results is not None:
            return id_results

    return list(
        provider.lookup(
            query=query,
            name=name,
            title=title,
            limit=args.limit,
            as_entries=False,
        )
    )


def _maybe_results_from_query_ids(
    tokens: Sequence[str],
    *,
    provider: InspireClient,
) -> list[SearchResult] | None:
    parsed_items = parse_literature_id_tokens(tokens)
    if parsed_items is None:
        return None
    return _results_from_parsed_ids(parsed_items, provider=provider)


def _results_from_explicit_ids(
    *,
    arxiv_ids: Sequence[str],
    inspire_ids: Sequence[str],
    provider: InspireClient,
) -> list[SearchResult]:
    parsed_items: list[tuple[str, str]] = []
    for raw in arxiv_ids:
        parsed_items.append(("arxiv", str(raw).strip()))
    for raw in inspire_ids:
        parsed_items.append(("inspire", str(raw).strip()))
    return _results_from_parsed_ids(parsed_items, provider=provider)


def _results_from_parsed_ids(
    parsed_ids: Sequence[tuple[str, str]],
    *,
    provider: InspireClient,
) -> list[SearchResult]:
    resolve = getattr(provider, "results_from_literature_ids", None)
    if callable(resolve):
        return list(resolve(parsed_ids))

    # Fallback for simple test stubs that only expose per-id fetch helpers.
    results: list[SearchResult] = []
    seen_keys: set[str] = set()
    fetch_by_arxiv = getattr(provider, "fetch_result_by_arxiv", None)
    fetch_by_recid = getattr(provider, "fetch_result_by_recid", None)

    for kind, value in parsed_ids:
        if kind == "arxiv":
            if not callable(fetch_by_arxiv):
                raise CliError("Provider does not support arXiv id lookup.")
            result = fetch_by_arxiv(value)
            key = result.arxiv_id or f"arxiv:{value}"
        elif kind == "inspire":
            if not callable(fetch_by_recid):
                raise CliError("Provider does not support INSPIRE id lookup.")
            try:
                recid = int(str(value).strip())
            except ValueError as error:
                raise CliError(f"Invalid INSPIRE id: {value}") from error
            result = fetch_by_recid(recid)
            key = result.arxiv_id or f"inspire:{result.recid}"
        else:
            raise CliError(f"Unknown literature id kind: {kind}")
        if key in seen_keys:
            continue
        seen_keys.add(key)
        results.append(result)
    return results


def _normalize_lookup_terms(args: argparse.Namespace) -> tuple[str | None, str | None, str | None]:
    if args.query and (args.name or args.title):
        raise CliError("Use either positional search terms or --name/--title, not both.")
    query = " ".join(args.query) if args.query else None
    name = " ".join(args.name) if args.name else None
    title = " ".join(args.title) if args.title else None
    return query, name, title


def _pdf_filename(arxiv_id: str) -> str:
    safe = arxiv_id.strip().replace("/", "_")
    if not safe.lower().endswith(".pdf"):
        safe = f"{safe}.pdf"
    return safe


def _terminal_link(text: str, url: str) -> str:
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"


def _lookup_origin(*, query: str | None, name: str | None, title: str | None) -> str:
    if query is not None:
        return f'INSPIRE query "{query}"'
    if name is not None and title is not None:
        return f'INSPIRE author "{name}" and title "{title}"'
    if name is not None:
        return f'INSPIRE author "{name}"'
    return f'INSPIRE title "{title}"'


def _default_import_target_path(bib_path: str | None) -> Path:
    if bib_path:
        return Path(bib_path)

    template_dir = os.environ.get("LATEX_TEMPLATE_DIR")
    if not template_dir:
        raise CliError("LATEX_TEMPLATE_DIR is not set.")
    return Path(template_dir) / "references.bib"


def _merge_template(*, target_path: Path, stdin: TextIO, stdout: TextIO, auto_confirm: bool) -> int:
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
        auto_confirm=auto_confirm,
    )


def _add_entries(
    *,
    target_path: Path,
    incoming: list[BibEntry],
    stdin: TextIO,
    stdout: TextIO,
    origin: str,
    auto_confirm: bool,
) -> int:
    existing = _read_bib_entries(target_path) if target_path.exists() else []
    merged_entries, added, updated = _merge_entries(existing, incoming)

    if not added and not updated:
        stdout.write(f"No new or updated entries from {origin}.\n")
        return 0

    _double_confirm_large_additions(
        len(added) + len(updated),
        stdin=stdin,
        stdout=stdout,
        auto_confirm=auto_confirm,
    )
    target_path.write_text(write_bibtex(merged_entries), encoding="utf-8")
    if added:
        stdout.write(f"Added {len(added)} entries to {target_path}.\n")
    if updated:
        stdout.write(f"Updated {len(updated)} entries in {target_path}.\n")
    return 0


def _read_bib_entries(path: Path) -> list[BibEntry]:
    if not path.exists():
        return []
    return parse_bibtex(path.read_text(encoding="utf-8"))


def _merge_entries(
    existing: list[BibEntry],
    incoming: list[BibEntry],
) -> tuple[list[BibEntry], list[BibEntry], list[BibEntry]]:
    used_keys = {entry.key.lower() for entry in existing}
    seen = set().union(*(dedupe_tokens(entry) for entry in existing))
    merged = [entry.clone() for entry in existing]
    added: list[BibEntry] = []
    updated: list[BibEntry] = []

    for entry in incoming:
        if not dedupe_tokens(entry) and _entry_looks_empty(entry):
            continue

        match_index = find_matching_entry_index(merged, entry)
        if match_index is not None:
            refreshed = merge_entry_fields(merged[match_index], entry)
            if entries_equivalent(merged[match_index], refreshed):
                continue
            merged[match_index] = refreshed
            updated.append(refreshed)
            continue

        tokens = dedupe_tokens(entry)
        if tokens and tokens & seen:
            continue
        prepared = assign_generated_key(entry, used_keys)
        merged.append(prepared)
        added.append(prepared)
        seen.update(dedupe_tokens(prepared))

    merged.sort(key=sort_key)
    return merged, added, updated


def _entry_looks_empty(entry: BibEntry) -> bool:
    return not normalize_for_match(entry.title) and not normalize_for_match(entry.author)


def _double_confirm_large_additions(
    count: int,
    *,
    stdin: TextIO,
    stdout: TextIO,
    auto_confirm: bool,
    action: str = "add",
) -> None:
    if count <= 10:
        return

    if auto_confirm:
        return

    if not getattr(stdin, "isatty", lambda: False)():
        raise CliError(f"Refusing to {action} {count} entries non-interactively; rerun in a terminal to confirm.")

    past = {"add": "added", "update": "updated", "download": "downloaded"}.get(action, f"{action}d")
    stdout.write(f"{count} entries are about to be {past}.\n")
    stdout.write("Continue? [y/N]: ")
    _flush_output(stdout)
    first = stdin.readline().strip().lower()
    if first not in {"y", "yes"}:
        raise CliError("Aborted before writing changes.")

    stdout.write(f"Final confirmation to {action} {count} entries? [y/N]: ")
    _flush_output(stdout)
    second = stdin.readline().strip().lower()
    if second not in {"y", "yes"}:
        raise CliError("Aborted before writing changes.")


def _flush_output(stream: TextIO) -> None:
    flush = getattr(stream, "flush", None)
    if callable(flush):
        flush()


def _install_bash_completion() -> Path:
    destination = Path.home() / ".local" / "share" / "bash-completion" / "completions" / "bibtool"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(_bash_completion_script(), encoding="utf-8")
    return destination


def _bash_completion_script() -> str:
    return """# bash completion for bibtool
_bibtool_completion() {
    local cur prev cword
    local root_opts search_opts download_opts update_opts

    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev=""
    if [[ ${COMP_CWORD} -gt 0 ]]; then
        prev="${COMP_WORDS[COMP_CWORD-1]}"
    fi
    cword=${COMP_CWORD}
    root_opts="search download update --bib --query --name --title --y --print-completion --install-completion -h --help"
    search_opts="--name --title --arxiv --inspire --limit -h --help"
    download_opts="--name --title --arxiv --inspire --limit --dir --y -h --help"
    update_opts="--bib --workers -h --help"

    case "${prev}" in
        --bib)
            COMPREPLY=( $(compgen -f -X '!*.bib' -- "${cur}") )
            return
            ;;
        --dir)
            COMPREPLY=( $(compgen -d -- "${cur}") )
            return
            ;;
        --print-completion|--install-completion)
            COMPREPLY=( $(compgen -W "bash" -- "${cur}") )
            return
            ;;
        --limit|--query|--name|--title|--arxiv|--inspire)
            return
            ;;
    esac

    if [[ ${cword} -eq 1 ]]; then
        COMPREPLY=( $(compgen -W "${root_opts}" -- "${cur}") $(compgen -f -X '!*.bib' -- "${cur}") )
        return
    fi

    if [[ "${COMP_WORDS[1]}" == "search" ]]; then
        if [[ "${cur}" == -* ]]; then
            COMPREPLY=( $(compgen -W "${search_opts}" -- "${cur}") )
        fi
        return
    fi

    if [[ "${COMP_WORDS[1]}" == "download" ]]; then
        if [[ "${cur}" == -* ]]; then
            COMPREPLY=( $(compgen -W "${download_opts}" -- "${cur}") )
        fi
        return
    fi

    if [[ "${COMP_WORDS[1]}" == "update" ]]; then
        if [[ "${cur}" == -* ]]; then
            COMPREPLY=( $(compgen -W "${update_opts}" -- "${cur}") )
            return
        fi
        COMPREPLY=( $(compgen -f -X '!*.bib' -- "${cur}") )
        return
    fi

    if [[ "${cur}" == -* ]]; then
        COMPREPLY=( $(compgen -W "${root_opts}" -- "${cur}") )
        return
    fi

    COMPREPLY=( $(compgen -f -X '!*.bib' -- "${cur}") )
}

complete -F _bibtool_completion bibtool
"""
