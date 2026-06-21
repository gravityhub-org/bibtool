from __future__ import annotations

import io
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bibtool.bibtex import BibEntry
from bibtool.cli import _merge_entries, run
from bibtool.inspire import InspireClient, SearchResult


class MergeBehaviorTests(unittest.TestCase):
    def test_merge_dedupes_by_doi(self) -> None:
        merged, added = _merge_entries(
            [
                _entry("Keep", author="Doe, Jane", title="Original", year="2020", doi="10.1/example"),
            ],
            [
                _entry("Remote", author="Hannuksela, Otto", title="Different Title", year="2024", doi="10.1/example"),
            ],
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(added, [])
        self.assertEqual(merged[0].key, "Keep")

    def test_merge_dedupes_by_eprint(self) -> None:
        merged, added = _merge_entries(
            [
                _entry("Keep", author="Doe, Jane", title="Original", year="2020", eprint="2401.00001"),
            ],
            [
                _entry("Remote", author="Hannuksela, Otto", title="Different Title", year="2024", eprint="2401.00001"),
            ],
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(added, [])

    def test_merge_sorts_by_year_author_then_title(self) -> None:
        merged, _added = _merge_entries(
            [
                _entry("ZuluKey", author="Zulu, Zoe", title="Later Title", year="2025"),
            ],
            [
                _entry("RemoteB", author="Alpha, Alice", title="Earlier Title", year="2024"),
                _entry("RemoteC", author="Alpha, Alice", title="Zed Title", year="2024"),
            ],
        )

        self.assertEqual([entry.year for entry in merged], ["2024", "2024", "2025"])
        self.assertEqual([entry.author for entry in merged[:2]], ["Alpha, Alice", "Alpha, Alice"])
        self.assertEqual([entry.title for entry in merged[:2]], ["Earlier Title", "Zed Title"])


class CliFailurePathTests(unittest.TestCase):
    def test_missing_template_dir_returns_error(self) -> None:
        original = os.environ.pop("LATEX_TEMPLATE_DIR", None)
        try:
            stderr = io.StringIO()
            exit_code = run(["references.bib"], stdout=io.StringIO(), stderr=stderr)
        finally:
            if original is not None:
                os.environ["LATEX_TEMPLATE_DIR"] = original

        self.assertEqual(exit_code, 1)
        self.assertIn("LATEX_TEMPLATE_DIR is not set", stderr.getvalue())

    def test_large_addition_noninteractive_is_rejected_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "references.bib"
            provider = StubProvider(
                query_entries=[
                    _entry(f"Remote{index}", author="Hannuksela, Otto", title=f"Paper {index}", year="2024")
                    for index in range(11)
                ]
            )

            stderr = io.StringIO()
            exit_code = run(
                ["--name", "Otto", "Hannuksela", "--bib", str(target)],
                stdin=io.StringIO(),
                stdout=io.StringIO(),
                stderr=stderr,
                provider=provider,
            )

            self.assertEqual(exit_code, 1)
            self.assertFalse(target.exists())
            self.assertIn("Refusing to add 11 entries non-interactively", stderr.getvalue())

    def test_large_addition_noninteractive_with_y_writes_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "references.bib"
            provider = StubProvider(
                query_entries=[
                    _entry(f"Remote{index}", author="Hannuksela, Otto", title=f"Paper {index}", year="2024")
                    for index in range(11)
                ]
            )

            exit_code = run(
                ["--y", "--name", "Otto", "Hannuksela", "--bib", str(target)],
                stdin=io.StringIO(),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                provider=provider,
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue(target.exists())

    def test_large_addition_wrong_second_confirmation_aborts_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "references.bib"
            provider = StubProvider(
                query_entries=[
                    _entry(f"Remote{index}", author="Hannuksela, Otto", title=f"Paper {index}", year="2024")
                    for index in range(11)
                ]
            )

            stderr = io.StringIO()
            exit_code = run(
                ["--name", "Otto", "Hannuksela", "--bib", str(target)],
                stdin=TtyStringIO("y\nn\n"),
                stdout=io.StringIO(),
                stderr=stderr,
                provider=provider,
            )

            self.assertEqual(exit_code, 1)
            self.assertFalse(target.exists())
            self.assertIn("Aborted before writing changes", stderr.getvalue())


class InspireBehaviorTests(unittest.TestCase):
    def test_search_filters_case_insensitively_across_authors(self) -> None:
        client = FakeInspireClient(
            pages=[
                _search_page(
                    _search_hit(recid=1, title="One", author="HANNUKSELA, OTTO AKSELI", year="2025"),
                    _search_hit(recid=2, title="Two", author="Someone Else", year="2025"),
                )
            ]
        )

        results = client.search("otto hannuksela")

        self.assertEqual([result.recid for result in results], [1])

    def test_search_continues_to_next_page_until_limit_matches(self) -> None:
        client = FakeInspireClient(
            pages=[
                _search_page(
                    _search_hit(recid=1, title="Something Else", author="A", year="2025"),
                    _search_hit(recid=2, title="Another Thing", author="B", year="2025"),
                ),
                _search_page(
                    _search_hit(recid=3, title="GWTC-5 Methods", author="A", year="2025"),
                    _search_hit(recid=4, title="GWTC-5 Results", author="B", year="2025"),
                ),
            ]
        )

        results = client.search("GWTC-5", limit=2)

        self.assertEqual([result.recid for result in results], [3, 4])
        self.assertEqual(len(client.requested_urls), 2)

    def test_search_requires_all_query_terms_across_title_and_author(self) -> None:
        client = FakeInspireClient(
            pages=[
                _search_page(
                    _search_hit(recid=1, title="GWTC-5 Methods", author="Someone Else", year="2025"),
                    _search_hit(recid=2, title="Methods", author="Hannuksela, Otto", year="2025"),
                    _search_hit(recid=3, title="GWTC-5 Methods", author="Hannuksela, Otto", year="2025"),
                )
            ]
        )

        results = client.search("GWTC-5 Hannuksela", limit=10)

        self.assertEqual([result.recid for result in results], [3])

    def test_fetch_query_entries_fetches_bibtex_for_each_match(self) -> None:
        client = BibtexPagingFakeInspireClient(
            bodies=[
                """@article{Fetched3,\n  author = {Hannuksela, Otto},\n  title = {GWTC-5 Methods},\n  year = {2025}\n}\n\n@article{Fetched4,\n  author = {Hannuksela, Otto},\n  title = {GWTC-5 Results},\n  year = {2025}\n}\n""",
                "",
            ]
        )

        entries = client.fetch_query_entries("GWTC-5")

        self.assertEqual([entry.key for entry in entries], ["Fetched3", "Fetched4"])
        self.assertEqual(len(client.requested_text_urls), 1)
        self.assertIn("format=bibtex", client.requested_text_urls[0])

    def test_fetch_author_entries_uses_author_only_query(self) -> None:
        client = BibtexPagingFakeInspireClient(
            bodies=[
                """@article{Fetched3,\n  author = {Hannuksela, Otto Akseli},\n  title = {Lensing Methods},\n  year = {2025}\n}\n""",
            ]
        )

        entries = client.fetch_author_entries("Otto Hannuksela")

        self.assertEqual([entry.key for entry in entries], ["Fetched3"])
        self.assertIn("q=author%3A%22Otto%22+and+author%3A%22Hannuksela%22", client.requested_text_urls[0])


class TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


class StubProvider:
    def __init__(self, *, query_entries=None) -> None:
        self.query_entries = query_entries or []

    def fetch_query_entries(self, query: str):
        return list(self.query_entries)

    def fetch_author_entries(self, query: str):
        return self.fetch_query_entries(query)


class FakeInspireClient(InspireClient):
    def __init__(self, *, pages) -> None:
        super().__init__(base_url="https://example.test/api/literature", timeout=1.0)
        self.pages = list(pages)
        self.requested_urls: list[str] = []

    def _request_json(self, url: str):
        self.requested_urls.append(url)
        return self.pages.pop(0) if self.pages else {"hits": {"hits": []}}


class FetchingFakeInspireClient(FakeInspireClient):
    def __init__(self, *, pages) -> None:
        super().__init__(pages=pages)
        self.fetched_recids: list[int] = []

    def fetch_entry(self, recid: int) -> BibEntry:
        self.fetched_recids.append(recid)
        return _entry(f"Fetched{recid}", author="Hannuksela, Otto", title=f"Fetched {recid}", year="2025")


class BibtexPagingFakeInspireClient(InspireClient):
    def __init__(self, *, bodies) -> None:
        super().__init__(base_url="https://example.test/api/literature", timeout=1.0)
        self.bodies = list(bodies)
        self.requested_text_urls: list[str] = []

    def _request_text(self, url: str) -> str:
        self.requested_text_urls.append(url)
        return self.bodies.pop(0) if self.bodies else ""


def _entry(key: str, *, author: str, title: str, year: str, doi: str | None = None, eprint: str | None = None) -> BibEntry:
    fields = {
        "author": author,
        "title": title,
        "year": year,
    }
    if doi is not None:
        fields["doi"] = doi
    if eprint is not None:
        fields["eprint"] = eprint
    return BibEntry(entry_type="article", key=key, fields=fields)


def _search_hit(*, recid: int, title: str, author: str, year: str) -> dict:
    return {
        "id": recid,
        "metadata": {
            "authors": [{"full_name": author}],
            "titles": [{"title": title}],
            "publication_info": [{"year": int(year)}],
        },
    }


def _search_page(*hits) -> dict:
    return {"hits": {"hits": list(hits)}}


if __name__ == "__main__":
    unittest.main()
