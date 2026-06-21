from __future__ import annotations

import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bibtool.cli import run
from bibtool.inspire import InspireClient, SearchResult


class TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


class FlushTrackingStringIO(TtyStringIO):
    def __init__(self, value: str = "") -> None:
        super().__init__(value)
        self.flush_count = 0

    def flush(self) -> None:
        self.flush_count += 1
        super().flush()


class StubProvider:
    def __init__(
        self,
        *,
        query_entries=None,
        query_results=None,
        author_entries=None,
        title_entries=None,
        author_results=None,
        title_results=None,
    ) -> None:
        self.query_entries = query_entries or []
        self.author_entries = author_entries if author_entries is not None else list(self.query_entries)
        self.title_entries = title_entries if title_entries is not None else list(self.query_entries)
        self.query_results = query_results or []
        self.author_results = author_results if author_results is not None else list(self.query_results)
        self.title_results = title_results if title_results is not None else list(self.query_results)
        self.seen_queries: list[tuple[str, int | None]] = []
        self.fetch_calls: list[tuple[str, str]] = []

    def fetch_query_entries(self, query: str):
        self.fetch_calls.append(("query", query))
        self.seen_queries.append((query, None))
        return list(self.query_entries)

    def search(self, query: str, limit: int | None = 20):
        self.seen_queries.append((query, limit))
        results = list(self.query_results)
        return results if limit is None else results[:limit]

    def fetch_author_entries(self, query: str):
        self.fetch_calls.append(("author", query))
        return list(self.author_entries)

    def fetch_title_entries(self, query: str):
        self.fetch_calls.append(("title", query))
        return list(self.title_entries)

    def search_author(self, query: str, limit: int | None = 20):
        self.seen_queries.append((query, limit))
        results = list(self.author_results)
        return results if limit is None else results[:limit]

    def search_title(self, query: str, limit: int | None = 20):
        self.seen_queries.append((query, limit))
        results = list(self.title_results)
        return results if limit is None else results[:limit]


class BibtoolCliTests(unittest.TestCase):
    def test_template_merge_dedupes_by_title_and_preserves_existing_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template_dir = root / "template"
            template_dir.mkdir()
            target = root / "references.bib"
            template = template_dir / "references.bib"

            target.write_text(
                """@article{ExistingKey,
  author = {Doe, Jane},
  title = {Searching For Signals},
  year = {2023}
}
""",
                encoding="utf-8",
            )
            template.write_text(
                """@article{SomeOtherKey,
  author = {Doe, Jane},
  title = {Searching For Signals},
  year = {2023}
}

@article{TemplateKey,
  author = {Hannuksela, Otto},
  title = {A Different Search},
  year = {2024}
}
""",
                encoding="utf-8",
            )

            original = os.environ.get("LATEX_TEMPLATE_DIR")
            os.environ["LATEX_TEMPLATE_DIR"] = str(template_dir)
            try:
                stdout = io.StringIO()
                exit_code = run([str(target)], stdout=stdout, stderr=io.StringIO())
            finally:
                if original is None:
                    os.environ.pop("LATEX_TEMPLATE_DIR", None)
                else:
                    os.environ["LATEX_TEMPLATE_DIR"] = original

            self.assertEqual(exit_code, 0)
            content = target.read_text(encoding="utf-8")
            self.assertIn("@article{ExistingKey,", content)
            self.assertIn("@article{Hannuksela2024ADifferent,", content)
            self.assertEqual(content.count("Searching For Signals"), 1)
            self.assertLess(content.index("ExistingKey"), content.index("Hannuksela2024ADifferent"))

    def test_import_by_title_adds_only_new_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "references.bib"
            target.write_text(
                """@article{KeepThisKey,
  author = {Doe, Jane},
  title = {GWTC-5 Overview},
  year = {2023}
}
""",
                encoding="utf-8",
            )

            provider = StubProvider(
                query_entries=[
                    _entry(
                        "RemoteKeyA",
                        author="Doe, Jane",
                        title="GWTC-5 Overview",
                        year="2023",
                    ),
                    _entry(
                        "RemoteKeyB",
                        author="Hannuksela, Otto",
                        title="GWTC-5 Methods",
                        year="2024",
                    ),
                ]
            )

            exit_code = run(
                ["--query", "GWTC-5", "--bib", str(target)],
                stdin=io.StringIO(),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                provider=provider,
            )

            self.assertEqual(exit_code, 0)
            content = target.read_text(encoding="utf-8")
            self.assertIn("@article{KeepThisKey,", content)
            self.assertIn("@article{Hannuksela2024GWTC5Methods,", content)
            self.assertEqual(content.count("GWTC-5 Overview"), 1)
            self.assertIn(("query", "GWTC-5"), provider.fetch_calls)

    def test_import_defaults_to_template_references_bib(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template_dir = root / "template"
            template_dir.mkdir()
            target = template_dir / "references.bib"
            target.write_text(
                """@article{KeepThisKey,
  author = {Doe, Jane},
  title = {GWTC-5 Overview},
  year = {2023}
}
""",
                encoding="utf-8",
            )

            provider = StubProvider(
                query_entries=[
                    _entry(
                        "RemoteKeyB",
                        author="Hannuksela, Otto",
                        title="GWTC-5 Methods",
                        year="2024",
                    ),
                ]
            )

            original = os.environ.get("LATEX_TEMPLATE_DIR")
            os.environ["LATEX_TEMPLATE_DIR"] = str(template_dir)
            try:
                exit_code = run(
                    ["--query", "GWTC-5"],
                    stdin=io.StringIO(),
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                    provider=provider,
                )
            finally:
                if original is None:
                    os.environ.pop("LATEX_TEMPLATE_DIR", None)
                else:
                    os.environ["LATEX_TEMPLATE_DIR"] = original

            self.assertEqual(exit_code, 0)
            content = target.read_text(encoding="utf-8")
            self.assertIn("@article{KeepThisKey,", content)
            self.assertIn("@article{Hannuksela2024GWTC5Methods,", content)

    def test_import_allows_name_and_title_together(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "references.bib"
            provider = StubProvider(
                author_entries=[
                    _entry("AuthorKey", author="Hannuksela, Otto", title="Author Result", year="2024"),
                ],
                title_entries=[
                    _entry("TitleKey", author="Cornish, Neil", title="GWTC-5 Result", year="2025"),
                ],
            )

            exit_code = run(
                ["--name", "Otto", "Hannuksela", "--title", "GWTC-5", "--bib", str(target), "--y"],
                stdin=io.StringIO(),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                provider=provider,
            )

            self.assertEqual(exit_code, 0)
            content = target.read_text(encoding="utf-8")
            self.assertIn("@article{Hannuksela2024AuthorResult,", content)
            self.assertIn("@article{Cornish2025GWTC5Result,", content)
            self.assertIn(("author", "Otto Hannuksela"), provider.fetch_calls)
            self.assertIn(("title", "GWTC-5"), provider.fetch_calls)

    def test_import_requires_template_dir_when_bib_not_given(self) -> None:
        provider = StubProvider(query_entries=[_entry("RemoteKey", author="Hannuksela, Otto", title="GWTC-5 Methods", year="2024")])
        original = os.environ.pop("LATEX_TEMPLATE_DIR", None)
        try:
            stderr = io.StringIO()
            exit_code = run(
                ["--query", "GWTC-5"],
                stdin=io.StringIO(),
                stdout=io.StringIO(),
                stderr=stderr,
                provider=provider,
            )
        finally:
            if original is not None:
                os.environ["LATEX_TEMPLATE_DIR"] = original

        self.assertEqual(exit_code, 1)
        self.assertIn("LATEX_TEMPLATE_DIR is not set", stderr.getvalue())

    def test_import_rejects_query_combined_with_name_or_title(self) -> None:
        stderr = io.StringIO()
        exit_code = run(
            ["--query", "Neil Cornish", "--name", "Otto Hannuksela"],
            stdin=io.StringIO(),
            stdout=io.StringIO(),
            stderr=stderr,
            provider=StubProvider(),
        )

        self.assertEqual(exit_code, 1)
        self.assertIn("Use --query by itself, or combine --name and --title.", stderr.getvalue())

    def test_large_import_requires_two_confirmations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "references.bib"
            provider = StubProvider(
                query_entries=[
                    _entry(
                        f"RemoteKey{index}",
                        author="Hannuksela, Otto",
                        title=f"Paper {index}",
                        year="2024",
                    )
                    for index in range(11)
                ]
            )

            stdout = io.StringIO()
            stderr = io.StringIO()
            exit_code = run(
                ["--name", "Otto", "Hannuksela", "--bib", str(target)],
                stdin=TtyStringIO("y\ny\n"),
                stdout=stdout,
                stderr=stderr,
                provider=provider,
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue(target.exists())
            self.assertIn("Continue? [y/N]:", stdout.getvalue())
            self.assertEqual(target.read_text(encoding="utf-8").count("@article{"), 11)
            self.assertIn(("author", "Otto Hannuksela"), provider.fetch_calls)

    def test_large_import_skips_confirmation_with_y_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "references.bib"
            provider = StubProvider(
                query_entries=[
                    _entry(
                        f"RemoteKey{index}",
                        author="Hannuksela, Otto",
                        title=f"Paper {index}",
                        year="2024",
                    )
                    for index in range(11)
                ]
            )

            stdout = io.StringIO()
            exit_code = run(
                ["--y", "--name", "Otto", "Hannuksela", "--bib", str(target)],
                stdin=io.StringIO(),
                stdout=stdout,
                stderr=io.StringIO(),
                provider=provider,
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue(target.exists())
            self.assertNotIn("Continue? [y/N]:", stdout.getvalue())

    def test_large_import_flushes_confirmation_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "references.bib"
            provider = StubProvider(
                query_entries=[
                    _entry(
                        f"RemoteKey{index}",
                        author="Hannuksela, Otto",
                        title=f"Paper {index}",
                        year="2024",
                    )
                    for index in range(11)
                ]
            )

            stdout = FlushTrackingStringIO()
            exit_code = run(
                ["--name", "Otto", "Hannuksela", "--bib", str(target)],
                stdin=TtyStringIO("y\ny\n"),
                stdout=stdout,
                stderr=io.StringIO(),
                provider=provider,
            )

            self.assertEqual(exit_code, 0)
            self.assertGreaterEqual(stdout.flush_count, 2)

    def test_search_title_is_case_insensitive(self) -> None:
        provider = StubProvider(
            query_results=[
                SearchResult(
                    recid=101,
                    title="Searching For Gravitational Waves",
                    authors=["Hannuksela, Otto"],
                    year="2025",
                )
            ]
        )

        stdout = io.StringIO()
        exit_code = run(
            ["search", "searching", "for"],
            stdout=stdout,
            stderr=io.StringIO(),
            provider=provider,
        )

        self.assertEqual(exit_code, 0)
        self.assertIn("Searching For Gravitational Waves", stdout.getvalue())
        self.assertIn("\033]8;;https://inspirehep.net/literature/101\033\\Searching For Gravitational Waves\033]8;;\033\\", stdout.getvalue())

    def test_search_positional_query_uses_unified_provider_search(self) -> None:
        provider = StubProvider(
            query_results=[
                SearchResult(
                    recid=202,
                    title="GWTC-5 Methods",
                    authors=["Hannuksela, Otto"],
                    year="2024",
                )
            ]
        )

        stdout = io.StringIO()
        exit_code = run(
            ["search", "GWTC-5", "Hannuksela"],
            stdout=stdout,
            stderr=io.StringIO(),
            provider=provider,
        )

        self.assertEqual(exit_code, 0)
        self.assertIn(("GWTC-5 Hannuksela", 20), provider.seen_queries)
        self.assertIn("GWTC-5 Methods", stdout.getvalue())

    def test_search_allows_name_and_title_together(self) -> None:
        provider = StubProvider(
            author_results=[
                SearchResult(
                    recid=301,
                    title="Author Match",
                    authors=["Hannuksela, Otto"],
                    year="2024",
                ),
                SearchResult(
                    recid=999,
                    title="Wrong Author Only",
                    authors=["Hannuksela, Otto"],
                    year="2022",
                ),
            ],
            title_results=[
                SearchResult(
                    recid=301,
                    title="Author Match",
                    authors=["Hannuksela, Otto"],
                    year="2025",
                ),
            ]
        )

        stdout = io.StringIO()
        exit_code = run(
            ["search", "--name", "Otto", "Hannuksela", "--title", "GWTC-5"],
            stdout=stdout,
            stderr=io.StringIO(),
            provider=provider,
        )

        self.assertEqual(exit_code, 0)
        self.assertIn(("Otto Hannuksela", 20), provider.seen_queries)
        self.assertIn(("GWTC-5", 20), provider.seen_queries)
        self.assertIn("Author Match", stdout.getvalue())
        self.assertNotIn("Wrong Author Only", stdout.getvalue())

    def test_search_dedupes_combined_name_and_title_results(self) -> None:
        shared = SearchResult(
            recid=401,
            title="Shared Match",
            authors=["Hannuksela, Otto"],
            year="2024",
        )
        provider = StubProvider(
            author_results=[shared, shared],
            title_results=[shared],
        )

        stdout = io.StringIO()
        exit_code = run(
            ["search", "--name", "Otto", "Hannuksela", "--title", "Shared"],
            stdout=stdout,
            stderr=io.StringIO(),
            provider=provider,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout.getvalue().count("Shared Match"), 1)

    def test_search_name_and_title_requires_title_match_in_title(self) -> None:
        provider = StubProvider(
            author_results=[
                SearchResult(
                    recid=501,
                    title="A Paper Without Keyword",
                    authors=["Cornish, Neil"],
                    year="2024",
                )
            ],
            title_results=[],
        )

        stdout = io.StringIO()
        exit_code = run(
            ["search", "--name", "Neil", "Cornish", "--title", "Bayes"],
            stdout=stdout,
            stderr=io.StringIO(),
            provider=provider,
        )

        self.assertEqual(exit_code, 0)
        self.assertIn("No matching records found.", stdout.getvalue())

    def test_search_rejects_positional_query_mixed_with_name_or_title(self) -> None:
        stderr = io.StringIO()
        exit_code = run(
            ["search", "Neil", "Cornish", "--name", "Otto", "Hannuksela"],
            stdout=io.StringIO(),
            stderr=stderr,
            provider=StubProvider(),
        )

        self.assertEqual(exit_code, 1)
        self.assertIn("Use either positional search terms or --name/--title, not both.", stderr.getvalue())

    def test_title_alias_uses_title_fetch_path(self) -> None:
        provider = StubProvider(
            query_entries=[_entry("RemoteKey", author="Hannuksela, Otto", title="GWTC-5 Methods", year="2024")]
        )

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "references.bib"
            exit_code = run(
                ["--title", "GWTC-5", "--bib", str(target)],
                stdin=io.StringIO(),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                provider=provider,
            )

        self.assertEqual(exit_code, 0)
        self.assertIn(("title", "GWTC-5"), provider.fetch_calls)

    def test_print_completion_outputs_bash_script(self) -> None:
        stdout = io.StringIO()

        exit_code = run(["--print-completion", "bash"], stdout=stdout, stderr=io.StringIO())

        self.assertEqual(exit_code, 0)
        script = stdout.getvalue()
        self.assertIn("_bibtool_completion()", script)
        self.assertIn("complete -F _bibtool_completion bibtool", script)
        self.assertIn("--install-completion", script)
        self.assertIn("--y", script)

    def test_install_completion_writes_expected_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                exit_code = run(["--install-completion"], stdout=stdout, stderr=stderr)

            self.assertEqual(exit_code, 0)
            completion_file = home / ".local" / "share" / "bash-completion" / "completions" / "bibtool"
            self.assertTrue(completion_file.exists())
            self.assertIn("_bibtool_completion()", completion_file.read_text(encoding="utf-8"))
            self.assertIn(str(completion_file), stdout.getvalue())


class InspireClientTests(unittest.TestCase):
    def test_search_uses_keyword_query_and_stops_at_limit(self) -> None:
        client = FakeInspireClient(
            pages=[
                _search_page(
                    _search_hit(recid=index, title=f"GWTC-5 Result {index}", author="Hannuksela, Otto", year="2025")
                    for index in range(1, 21)
                ),
                _search_page(
                    _search_hit(recid=999, title="GWTC-5 Extra", author="Hannuksela, Otto", year="2025")
                    for _ in range(20)
                ),
            ]
        )

        results = client.search("GWTC-5")

        self.assertEqual(len(results), 20)
        self.assertEqual(len(client.requested_urls), 1)
        self.assertIn("q=%28title%3A%22GWTC-5%22+or+author%3A%22GWTC-5%22%29", client.requested_urls[0])
        self.assertIn("size=20", client.requested_urls[0])
        self.assertIn("fields=titles%2Cauthors%2Cabstracts%2Cimprints%2Cpreprint_date%2Cpublication_info", client.requested_urls[0])

    def test_requests_use_timeout(self) -> None:
        client = InspireClient(timeout=7.0)

        class Response:
            def __enter__(self):
                return io.StringIO('{"hits":{"hits":[]}}')

            def __exit__(self, exc_type, exc, tb):
                return False

        with patch("bibtool.inspire.urlopen", return_value=Response()) as mock_urlopen:
            self.assertEqual(client.search("GWTC-5"), [])

        self.assertEqual(mock_urlopen.call_args.kwargs["timeout"], 7.0)


class FakeInspireClient(InspireClient):
    def __init__(self, *, pages) -> None:
        super().__init__(base_url="https://example.test/api/literature", timeout=1.0)
        self.pages = list(pages)
        self.requested_urls: list[str] = []

    def _request_json(self, url: str):
        self.requested_urls.append(url)
        return self.pages.pop(0) if self.pages else {"hits": {"hits": []}}


def _entry(key: str, *, author: str, title: str, year: str):
    from bibtool.bibtex import BibEntry

    return BibEntry(
        entry_type="article",
        key=key,
        fields={
            "author": author,
            "title": title,
            "year": year,
        },
    )


def _search_hit(*, recid: int, title: str, author: str, year: str) -> dict:
    return {
        "id": recid,
        "metadata": {
            "authors": [{"full_name": author}],
            "titles": [{"title": title}],
            "publication_info": [{"year": int(year)}],
        },
    }


def _search_page(hits) -> dict:
    return {"hits": {"hits": list(hits)}}


if __name__ == "__main__":
    unittest.main()
