from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bibtool.bibtex import BibEntry
from bibtool.cli import _merge_entries, run
from bibtool.inspire import InspireClient, SearchResult, clear_response_caches


class MergeBehaviorTests(unittest.TestCase):
    def test_merge_dedupes_by_doi(self) -> None:
        merged, added, updated = _merge_entries(
            [
                _entry("Keep", author="Doe, Jane", title="Original", year="2020", doi="10.1/example"),
            ],
            [
                _entry("Remote", author="Hannuksela, Otto", title="Different Title", year="2024", doi="10.1/example"),
            ],
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(added, [])
        self.assertEqual(len(updated), 1)
        self.assertEqual(merged[0].key, "Keep")
        self.assertEqual(merged[0].author, "Hannuksela, Otto")
        self.assertEqual(merged[0].title, "Different Title")
        self.assertEqual(merged[0].year, "2024")

    def test_merge_dedupes_by_eprint(self) -> None:
        merged, added, updated = _merge_entries(
            [
                _entry("Keep", author="Doe, Jane", title="Original", year="2020", eprint="2401.00001"),
            ],
            [
                _entry(
                    "Remote",
                    author="Hannuksela, Otto",
                    title="Different Title",
                    year="2024",
                    eprint="2401.00001",
                    journal="Phys. Rev. D",
                ),
            ],
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(added, [])
        self.assertEqual(len(updated), 1)
        self.assertEqual(merged[0].fields["journal"], "Phys. Rev. D")

    def test_merge_sorts_by_year_author_then_title(self) -> None:
        merged, _added, _updated = _merge_entries(
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
    def setUp(self) -> None:
        clear_response_caches()

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
        filler = [
            _search_hit(recid=index, title="Something Else", author="A", year="2025")
            for index in range(1, 51)
        ]
        client = FakeInspireClient(
            pages=[
                _search_page(*filler),
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

    def test_search_name_and_title_uses_single_author_query(self) -> None:
        client = FakeInspireClient(
            pages=[
                _search_page(
                    _search_hit(recid=2738695, title="Bayesian power spectral estimation", author="Cornish, Neil J.", year="2024"),
                    _search_hit(recid=501, title="Unrelated paper", author="Cornish, Neil J.", year="2024"),
                )
            ]
        )

        results = client.search_name_and_title("Neil Cornish", "Bayes", limit=20)

        self.assertEqual([result.recid for result in results], [2738695])
        self.assertEqual(len(client.requested_urls), 1)
        self.assertIn("author%3A%22Neil%22+and+author%3A%22Cornish%22", client.requested_urls[0])
        self.assertIn("title%3ABayes%2A", client.requested_urls[0])

    def test_search_name_and_title_pages_until_limit(self) -> None:
        filler = [
            _search_hit(recid=index, title=f"Paper {index}", author="Cornish, Neil J.", year="2024")
            for index in range(1, 51)
        ]
        bayes_hits = [
            _search_hit(recid=1000 + index, title=f"Bayesian study {index}", author="Cornish, Neil J.", year="2020")
            for index in range(25)
        ]
        client = FakeInspireClient(
            pages=[
                _search_page(*filler),
                _search_page(*bayes_hits),
            ]
        )

        results = client.search_name_and_title("Neil Cornish", "Bayes", limit=20)

        self.assertEqual(len(results), 20)
        self.assertTrue(all("Bayesian" in result.title for result in results))
        self.assertEqual(len(client.requested_urls), 2)

    def test_fetch_entry_adds_arxiv_journal_for_preprints(self) -> None:
        client = RecordingLookupClient(
            pages=[],
            bibtex_by_recid={
                3064813: """@article{Ray:2025rtt,
  author = {Ray, Anarya},
  title = {GW231123: extreme spins or microglitches?},
  eprint = {2510.07228},
  archivePrefix = {arXiv},
  primaryClass = {gr-qc},
  year = {2025}
}
""",
            },
        )

        entry = client.fetch_entry(3064813)

        self.assertEqual(entry.fields["journal"], "arXiv")
        self.assertEqual(entry.fields["archiveprefix"], "arXiv")

    def test_fetch_query_entries_fetches_bibtex_for_each_match(self) -> None:
        client = RecordingLookupClient(
            pages=[
                _search_page(
                    _search_hit(recid=3, title="GWTC-5 Methods", author="Hannuksela, Otto", year="2025"),
                    _search_hit(recid=4, title="GWTC-5 Results", author="Hannuksela, Otto", year="2025"),
                )
            ],
            bibtex_by_recid={
                3: """@article{Fetched3,
  author = {Hannuksela, Otto},
  title = {GWTC-5 Methods},
  year = {2025}
}
""",
                4: """@article{Fetched4,
  author = {Hannuksela, Otto},
  title = {GWTC-5 Results},
  year = {2025}
}
""",
            },
        )

        entries = client.fetch_query_entries("GWTC-5")

        self.assertEqual([entry.key for entry in entries], ["Fetched3", "Fetched4"])
        self.assertEqual(client.json_requests, 1)
        self.assertEqual(client.text_requests, 2)
        self.assertIn("format=bibtex", client.requested_urls[-1])

    def test_fetch_author_entries_uses_author_only_query(self) -> None:
        client = RecordingLookupClient(
            pages=[
                _search_page(
                    _search_hit(recid=3, title="Lensing Methods", author="Hannuksela, Otto Akseli", year="2025"),
                )
            ],
            bibtex_by_recid={
                3: """@article{Fetched3,
  author = {Hannuksela, Otto Akseli},
  title = {Lensing Methods},
  year = {2025}
}
""",
            },
        )

        entries = client.fetch_author_entries("Otto Hannuksela")

        self.assertEqual([entry.key for entry in entries], ["Fetched3"])
        self.assertIn("q=author%3A%22Otto%22+and+author%3A%22Hannuksela%22", client.requested_urls[0])


class TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


class StubProvider:
    def __init__(self, *, query_entries=None) -> None:
        self.catalog = [
            (index + 1, entry)
            for index, entry in enumerate(query_entries or [])
        ]

    def lookup(self, *, query=None, name=None, title=None, limit=20, as_entries=False):
        spec = InspireClient().resolve_lookup(query=query, name=name, title=title)
        matched: list[tuple[int, BibEntry]] = []
        seen_recids: set[int] = set()
        for recid, entry in self.catalog:
            if recid in seen_recids:
                continue
            if spec.entry_matcher(entry):
                seen_recids.add(recid)
                matched.append((recid, entry))
        if limit is not None:
            matched = matched[:limit]
        if as_entries:
            return [entry for _recid, entry in matched]
        return [
            SearchResult(
                recid=recid,
                title=entry.title,
                authors=[part.strip() for part in entry.author.split(" and ") if part.strip()],
                year=entry.year,
            )
            for recid, entry in matched
        ]


class RecordingLookupClient(InspireClient):
    def __init__(self, *, pages, bibtex_by_recid: dict[int, str]) -> None:
        super().__init__(base_url="https://example.test/api/literature", timeout=1.0)
        self.pages = list(pages)
        self.bibtex_by_recid = bibtex_by_recid
        self.requested_urls: list[str] = []
        self.json_requests = 0
        self.text_requests = 0

    def _request_json(self, url: str):
        from bibtool import inspire as inspire_module

        self.requested_urls.append(url)
        if url not in inspire_module._JSON_CACHE:
            self.json_requests += 1
            inspire_module._JSON_CACHE[url] = self.pages.pop(0) if self.pages else {"hits": {"hits": []}}
        return inspire_module._JSON_CACHE[url]

    def _request_text(self, url: str) -> str:
        from bibtool import inspire as inspire_module

        self.requested_urls.append(url)
        if url in inspire_module._TEXT_CACHE:
            return inspire_module._TEXT_CACHE[url]
        self.text_requests += 1
        if "?format=bibtex" in url:
            recid = int(url.rsplit("/", 1)[-1].split("?", 1)[0])
            body = self.bibtex_by_recid.get(recid, "")
            inspire_module._TEXT_CACHE[url] = body
            return body
        return ""


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


def _entry(key: str, *, author: str, title: str, year: str, doi: str | None = None, eprint: str | None = None, journal: str | None = None) -> BibEntry:
    fields = {
        "author": author,
        "title": title,
        "year": year,
    }
    if doi is not None:
        fields["doi"] = doi
    if eprint is not None:
        fields["eprint"] = eprint
    if journal is not None:
        fields["journal"] = journal
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
