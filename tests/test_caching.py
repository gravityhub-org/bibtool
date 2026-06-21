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


class CachingTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_response_caches()

    def test_json_responses_are_cached_by_url(self) -> None:
        payload = _search_page(
            _search_hit(recid=1, title="Bayesian paper", author="Cornish, Neil J.", year="2024"),
        )
        handler = _MockUrlHandler(json_body=json.dumps(payload))

        client = InspireClient(base_url="https://example.test/api/literature", timeout=1.0)
        with patch("bibtool.inspire.urlopen", side_effect=handler):
            first = client.lookup(name="Neil Cornish", title="Bayes", limit=1, as_entries=False)
            second = client.lookup(name="Neil Cornish", title="Bayes", limit=1, as_entries=False)

        self.assertEqual([result.recid for result in first], [1])
        self.assertEqual([result.recid for result in second], [1])
        self.assertEqual(handler.json_calls, 1)
        self.assertEqual(handler.text_calls, 0)

    def test_bibtex_responses_are_cached_by_url(self) -> None:
        payload = _search_page(
            _search_hit(recid=3, title="GWTC-5 Methods", author="Hannuksela, Otto", year="2025"),
        )
        bibtex = """@article{Fetched3,
  author = {Hannuksela, Otto},
  title = {GWTC-5 Methods},
  year = {2025}
}
"""
        handler = _MockUrlHandler(json_body=json.dumps(payload), text_by_suffix={"/3?format=bibtex": bibtex})

        client = InspireClient(base_url="https://example.test/api/literature", timeout=1.0)
        with patch("bibtool.inspire.urlopen", side_effect=handler):
            first = client.lookup(query="GWTC-5", limit=1, as_entries=True)
            second = client.lookup(query="GWTC-5", limit=1, as_entries=True)

        self.assertEqual([entry.key for entry in first], ["Fetched3"])
        self.assertEqual([entry.key for entry in second], ["Fetched3"])
        self.assertEqual(handler.json_calls, 2)
        self.assertEqual(handler.text_calls, 1)
        payload = _search_page(
            _search_hit(recid=1, title="Bayesian paper", author="Cornish, Neil J.", year="2024"),
        )
        handler = _MockUrlHandler(json_body=json.dumps(payload))

        first_client = InspireClient(base_url="https://example.test/api/literature", timeout=1.0)
        second_client = InspireClient(base_url="https://example.test/api/literature", timeout=1.0)
        with patch("bibtool.inspire.urlopen", side_effect=handler):
            first_client.lookup(name="Neil Cornish", title="Bayes", limit=1, as_entries=False)
            second_client.lookup(name="Neil Cornish", title="Bayes", limit=1, as_entries=False)

        self.assertEqual(handler.json_calls, 1)

    def test_fetch_after_search_reuses_json_cache(self) -> None:
        payload = _search_page(
            _search_hit(recid=3, title="GWTC-5 Methods", author="Hannuksela, Otto", year="2025"),
        )
        bibtex = """@article{Fetched3,
  author = {Hannuksela, Otto},
  title = {GWTC-5 Methods},
  year = {2025}
}
"""
        handler = _MockUrlHandler(json_body=json.dumps(payload), text_by_suffix={"/3?format=bibtex": bibtex})

        client = InspireClient(base_url="https://example.test/api/literature", timeout=1.0)
        with patch("bibtool.inspire.urlopen", side_effect=handler):
            client.lookup(query="GWTC-5", limit=1, as_entries=False)
            client.lookup(query="GWTC-5", limit=1, as_entries=True)

        self.assertEqual(handler.json_calls, 2)
        self.assertEqual(handler.text_calls, 1)

    def test_clear_response_caches_forces_fresh_network_calls(self) -> None:
        payload = _search_page(
            _search_hit(recid=1, title="Bayesian paper", author="Cornish, Neil J.", year="2024"),
        )
        handler = _MockUrlHandler(json_body=json.dumps(payload))

        client = InspireClient(base_url="https://example.test/api/literature", timeout=1.0)
        with patch("bibtool.inspire.urlopen", side_effect=handler):
            client.lookup(name="Neil Cornish", title="Bayes", limit=1, as_entries=False)
            clear_response_caches()
            client.lookup(name="Neil Cornish", title="Bayes", limit=1, as_entries=False)

        self.assertEqual(handler.json_calls, 2)

    def test_different_queries_use_separate_cache_entries(self) -> None:
        payload_a = _search_page(
            _search_hit(recid=1, title="Bayesian paper", author="Cornish, Neil J.", year="2024"),
        )
        payload_b = _search_page(
            _search_hit(recid=2, title="GWTC-5 Methods", author="Hannuksela, Otto", year="2025"),
        )
        handler = _MockUrlHandler(
            json_by_query={
                "Bayes": json.dumps(payload_a),
                "GWTC-5": json.dumps(payload_b),
            }
        )

        client = InspireClient(base_url="https://example.test/api/literature", timeout=1.0)
        with patch("bibtool.inspire.urlopen", side_effect=handler):
            client.lookup(name="Neil Cornish", title="Bayes", limit=1, as_entries=False)
            client.lookup(query="GWTC-5", limit=1, as_entries=False)
            client.lookup(name="Neil Cornish", title="Bayes", limit=1, as_entries=False)
            client.lookup(query="GWTC-5", limit=1, as_entries=False)

        self.assertEqual(handler.json_calls, 2)

    def test_cli_search_reuses_module_cache_between_run_calls(self) -> None:
        payload = _search_page(
            _search_hit(recid=1, title="Bayesian paper", author="Cornish, Neil J.", year="2024"),
        )
        handler = _MockUrlHandler(json_body=json.dumps(payload))

        with patch("bibtool.inspire.urlopen", side_effect=handler):
            run(
                ["search", "--name", "Neil", "Cornish", "--title", "Bayes"],
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
            run(
                ["search", "--name", "Neil", "Cornish", "--title", "Bayes"],
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

        self.assertEqual(handler.json_calls, 1)
        self.assertGreater(len(inspire_module._JSON_CACHE), 0)

    def test_module_caches_populated_after_requests(self) -> None:
        payload = _search_page(
            _search_hit(recid=3, title="GWTC-5 Methods", author="Hannuksela, Otto", year="2025"),
        )
        bibtex = """@article{Fetched3,
  author = {Hannuksela, Otto},
  title = {GWTC-5 Methods},
  year = {2025}
}
"""
        handler = _MockUrlHandler(json_body=json.dumps(payload), text_by_suffix={"/3?format=bibtex": bibtex})

        client = InspireClient(base_url="https://example.test/api/literature", timeout=1.0)
        with patch("bibtool.inspire.urlopen", side_effect=handler):
            client.lookup(query="GWTC-5", limit=1, as_entries=True)

        self.assertEqual(len(inspire_module._JSON_CACHE), 2)
        self.assertEqual(len(inspire_module._TEXT_CACHE), 1)


class _MockUrlHandler:
    def __init__(
        self,
        *,
        json_body: str | None = None,
        json_by_query: dict[str, str] | None = None,
        text_by_suffix: dict[str, str] | None = None,
    ) -> None:
        self.json_body = json_body or json.dumps({"hits": {"hits": []}})
        self.json_by_query = json_by_query or {}
        self.text_by_suffix = text_by_suffix or {}
        self.json_calls = 0
        self.text_calls = 0

    def __call__(self, url: str, timeout: float | None = None):
        if "?format=bibtex" in url:
            self.text_calls += 1
            body = ""
            for suffix, candidate in self.text_by_suffix.items():
                if url.endswith(suffix) or suffix in url:
                    body = candidate
                    break
            return _BytesResponse(body)

        self.json_calls += 1
        body = self.json_body
        for token, candidate in self.json_by_query.items():
            if token in url:
                body = candidate
                break
        return _BytesResponse(body)


class _BytesResponse:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def __enter__(self):
        return io.BytesIO(self._body)

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._body


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
