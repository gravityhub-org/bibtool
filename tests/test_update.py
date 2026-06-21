from __future__ import annotations

import io
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bibtool.bibtex import BibEntry
from bibtool.cli import run
from bibtool.inspire import InspireClient


class UpdateProvider:
    def __init__(self, *, fresh_by_eprint: dict[str, BibEntry] | None = None) -> None:
        self.fresh_by_eprint = fresh_by_eprint or {}
        self.refresh_calls: list[str] = []

    def refresh_entry(self, entry: BibEntry) -> BibEntry | None:
        self.refresh_calls.append(entry.key)
        eprint = entry.fields.get("eprint")
        if eprint and eprint in self.fresh_by_eprint:
            fresh = self.fresh_by_eprint[eprint].clone()
            fresh.key = entry.key
            return fresh
        return None


class UpdateCommandTests(unittest.TestCase):
    def test_update_refreshes_all_entries_and_preserves_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "references.bib"
            target.write_text(
                """@article{Ray2025GW231123Extreme,
  author = {Ray, Anarya},
  title = {GW231123: extreme spins or microglitches?},
  eprint = {2510.07228},
  archiveprefix = {arXiv},
  year = {2025}
}

@article{KeepUnchanged,
  author = {Doe, Jane},
  title = {Already Complete},
  journal = {Phys. Rev. D},
  doi = {10.1/complete},
  year = {2020}
}
""",
                encoding="utf-8",
            )

            provider = UpdateProvider(
                fresh_by_eprint={
                    "2510.07228": BibEntry(
                        entry_type="article",
                        key="ignored",
                        fields={
                            "author": "Ray, Anarya and Banagiri, Sharan",
                            "title": "GW231123: extreme spins or microglitches?",
                            "eprint": "2510.07228",
                            "archiveprefix": "arXiv",
                            "primaryclass": "gr-qc",
                            "journal": "arXiv",
                            "year": "2025",
                            "month": "10",
                        },
                    ),
                    "10.1/complete": BibEntry(
                        entry_type="article",
                        key="ignored",
                        fields={
                            "author": "Doe, Jane",
                            "title": "Already Complete",
                            "journal": "Phys. Rev. D",
                            "doi": "10.1/complete",
                            "year": "2020",
                        },
                    ),
                }
            )

            stdout = io.StringIO()
            exit_code = run(
                ["update", str(target), "--y"],
                stdin=io.StringIO(),
                stdout=stdout,
                stderr=io.StringIO(),
                provider=provider,
            )

            self.assertEqual(exit_code, 0)
            content = target.read_text(encoding="utf-8")
            self.assertIn("@article{Ray2025GW231123Extreme,", content)
            self.assertIn("journal = {arXiv}", content)
            self.assertIn("primaryclass = {gr-qc}", content)
            self.assertIn("@article{KeepUnchanged,", content)
            self.assertEqual(provider.refresh_calls, ["Ray2025GW231123Extreme", "KeepUnchanged"])
            self.assertIn("Updated 1 entries", stdout.getvalue())
            self.assertIn("Skipped 1 entries", stdout.getvalue())

    def test_update_defaults_to_template_references_bib(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            template_dir = Path(tmp) / "template"
            template_dir.mkdir()
            target = template_dir / "references.bib"
            target.write_text(
                """@article{Example2024,
  author = {Hannuksela, Otto},
  title = {Example Paper},
  eprint = {2401.00001},
  year = {2024}
}
""",
                encoding="utf-8",
            )

            provider = UpdateProvider(
                fresh_by_eprint={
                    "2401.00001": BibEntry(
                        entry_type="article",
                        key="ignored",
                        fields={
                            "author": "Hannuksela, Otto",
                            "title": "Example Paper",
                            "eprint": "2401.00001",
                            "journal": "arXiv",
                            "year": "2024",
                        },
                    ),
                }
            )

            original = os.environ.get("LATEX_TEMPLATE_DIR")
            os.environ["LATEX_TEMPLATE_DIR"] = str(template_dir)
            try:
                exit_code = run(
                    ["update", "--y"],
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
            self.assertIn("journal = {arXiv}", target.read_text(encoding="utf-8"))


class LookupRecidTests(unittest.TestCase):
    def test_lookup_recid_prefers_eprint(self) -> None:
        client = RecordingUpdateClient(
            search_results={
                "eprint:2510.07228": [301],
            },
            bibtex_by_recid={},
        )
        entry = BibEntry(
            entry_type="article",
            key="Key",
            fields={"eprint": "2510.07228", "title": "Title", "author": "Ray, Anarya"},
        )

        self.assertEqual(client.lookup_recid(entry), 301)
        self.assertEqual(client.search_queries, ["eprint:2510.07228"])


class RecordingUpdateClient(InspireClient):
    def __init__(self, *, search_results: dict[str, list[int]], bibtex_by_recid: dict[int, str]) -> None:
        super().__init__(base_url="https://example.test/api/literature", timeout=1.0)
        self.search_results = search_results
        self.bibtex_by_recid = bibtex_by_recid
        self.requested_urls: list[str] = []
        self.search_queries: list[str] = []

    def _search_records(self, query, *, matcher, limit):
        from bibtool.inspire import SearchResult

        self.search_queries.append(query)
        recids = self.search_results.get(query, [])[: limit or None]
        return [
            SearchResult(recid=recid, title="Title", authors=["Author"], year="2025")
            for recid in recids
        ]

    def _request_text(self, url: str) -> str:
        self.requested_urls.append(url)
        if "?format=bibtex" in url:
            recid = int(url.rsplit("/", 1)[-1].split("?", 1)[0])
            return self.bibtex_by_recid.get(recid, "")
        return ""


if __name__ == "__main__":
    unittest.main()
