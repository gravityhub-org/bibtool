from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bibtool import inspire as inspire_module
from bibtool.cli import run
from bibtool.inspire import InspireClient, clear_response_caches


class LookupParityTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_response_caches()

    def test_search_and_fetch_share_resolve_lookup(self) -> None:
        client = InspireClient(base_url="https://example.test/api/literature", timeout=1.0)

        search_spec = client.resolve_lookup(query="GWTC-5 Hannuksela")
        fetch_spec = client.resolve_lookup(query="GWTC-5 Hannuksela")

        self.assertEqual(search_spec.query, fetch_spec.query)
        self.assertEqual(search_spec.entry_matcher.__code__.co_code, fetch_spec.entry_matcher.__code__.co_code)

    def test_name_title_query_is_identical_for_search_and_fetch(self) -> None:
        client = InspireClient(base_url="https://example.test/api/literature", timeout=1.0)

        search_spec = client.resolve_lookup(name="Neil Cornish", title="Bayes")
        fetch_spec = client.resolve_lookup(name="Neil Cornish", title="Bayes")

        self.assertEqual(search_spec.query, fetch_spec.query)
        self.assertIn('author:"Neil"', search_spec.query)
        self.assertIn("title:Bayes*", search_spec.query)

    def test_lookup_recids_match_between_search_and_fetch(self) -> None:
        client = RecordingLookupClient(
            pages=[
                _search_page(
                    _search_hit(recid=3, title="GWTC-5 Methods", author="Hannuksela, Otto", year="2025"),
                    _search_hit(recid=4, title="GWTC-5 Results", author="Hannuksela, Otto", year="2025"),
                    _search_hit(recid=9, title="Unrelated", author="Someone Else", year="2025"),
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

        search_results = client.lookup(query="GWTC-5 Hannuksela", limit=5, as_entries=False)
        fetch_entries = client.lookup(query="GWTC-5 Hannuksela", limit=5, as_entries=True)

        self.assertEqual([result.recid for result in search_results], [3, 4])
        self.assertEqual([entry.key for entry in fetch_entries], ["Fetched3", "Fetched4"])
        self.assertEqual(client.json_requests, 3)
        self.assertEqual(client.text_requests, 2)

    def test_name_and_title_lookup_matches_bayesian_title_for_both_modes(self) -> None:
        client = RecordingLookupClient(
            pages=[
                _search_page(
                    _search_hit(
                        recid=2738695,
                        title="Bayesian power spectral estimation",
                        author="Cornish, Neil J.",
                        year="2024",
                    ),
                    _search_hit(recid=501, title="Unrelated paper", author="Cornish, Neil J.", year="2024"),
                )
            ],
            bibtex_by_recid={
                2738695: """@article{BayesPaper,
  author = {Cornish, Neil J. and Gupta, Toral},
  title = {Bayesian power spectral estimation},
  year = {2024}
}
""",
            },
        )

        search_results = client.lookup(name="Neil Cornish", title="Bayes", limit=20, as_entries=False)
        fetch_entries = client.lookup(name="Neil Cornish", title="Bayes", limit=20, as_entries=True)

        self.assertEqual([result.recid for result in search_results], [2738695])
        self.assertEqual([entry.key for entry in fetch_entries], ["BayesPaper"])
        self.assertIn("title%3ABayes%2A", client.requested_urls[0])

    def test_cli_search_and_import_use_same_lookup_spec(self) -> None:
        provider = TrackingProvider()

        run(
            ["search", "--name", "Neil", "Cornish", "--title", "Bayes"],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            provider=provider,
        )
        run(
            ["--name", "Neil", "Cornish", "--title", "Bayes", "--bib", "/tmp/unused.bib", "--y"],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            provider=provider,
        )

        self.assertEqual(len(provider.lookup_calls), 2)
        search_call = provider.lookup_calls[0]
        import_call = provider.lookup_calls[1]
        self.assertEqual(search_call[:3], import_call[:3])
        self.assertEqual(search_call[3], 20)
        self.assertIsNone(import_call[3])
        self.assertFalse(search_call[4])
        self.assertTrue(import_call[4])


class TrackingProvider:
    def __init__(self) -> None:
        self.lookup_calls: list[tuple] = []

    def lookup(self, *, query=None, name=None, title=None, limit=20, as_entries=False):
        self.lookup_calls.append((query, name, title, limit, as_entries))
        return []


class RecordingLookupClient(InspireClient):
    def __init__(self, *, pages, bibtex_by_recid: dict[int, str]) -> None:
        super().__init__(base_url="https://example.test/api/literature", timeout=1.0)
        self.pages = list(pages)
        self.bibtex_by_recid = bibtex_by_recid
        self.requested_urls: list[str] = []
        self.json_requests = 0
        self.text_requests = 0

    def _request_json(self, url: str):
        self.requested_urls.append(url)
        if url not in inspire_module._JSON_CACHE:
            self.json_requests += 1
            inspire_module._JSON_CACHE[url] = self.pages.pop(0) if self.pages else {"hits": {"hits": []}}
        return inspire_module._JSON_CACHE[url]

    def _request_text(self, url: str) -> str:
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
