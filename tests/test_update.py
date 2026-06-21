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

    def test_update_upgrades_arxiv_preprint_to_published_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "references.bib"
            target.write_text(
                """@article{Littenberg2015BayesianInference,
  author = {Littenberg, Tyson B. and Cornish, Neil J.},
  title = {Bayesian inference for spectral estimation of gravitational wave detector noise},
  eprint = {1410.3852},
  archiveprefix = {arXiv},
  primaryclass = {gr-qc},
  journal = {arXiv},
  year = {2014}
}
""",
                encoding="utf-8",
            )

            provider = UpdateProvider(
                fresh_by_eprint={
                    "1410.3852": BibEntry(
                        entry_type="article",
                        key="ignored",
                        fields={
                            "author": "Littenberg, Tyson B. and Cornish, Neil J.",
                            "title": "Bayesian inference for spectral estimation of gravitational wave detector noise",
                            "eprint": "1410.3852",
                            "archiveprefix": "arXiv",
                            "primaryclass": "gr-qc",
                            "doi": "10.1103/PhysRevD.91.084034",
                            "journal": "Phys. Rev. D",
                            "volume": "91",
                            "number": "8",
                            "pages": "084034",
                            "year": "2015",
                        },
                    ),
                }
            )

            exit_code = run(
                ["update", str(target), "--y"],
                stdin=io.StringIO(),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                provider=provider,
            )

            self.assertEqual(exit_code, 0)
            content = target.read_text(encoding="utf-8")
            self.assertIn("journal = {Phys. Rev. D}", content)
            self.assertIn("doi = {10.1103/PhysRevD.91.084034}", content)
            self.assertIn("pages = {084034}", content)

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

    def test_lookup_recid_prefers_published_record(self) -> None:
        client = RecordingUpdateClient(
            search_results={
                "eprint:1410.3852": [100, 200],
            },
            search_metadata={
                100: False,
                200: True,
            },
            bibtex_by_recid={},
        )
        entry = BibEntry(
            entry_type="article",
            key="Key",
            fields={"eprint": "1410.3852", "title": "Bayesian inference", "author": "Littenberg, Tyson B."},
        )

        self.assertEqual(client.lookup_recid(entry), 200)


    def test_fetch_entry_enriches_publication_from_metadata(self) -> None:
        client = RecordingUpdateClient(
            search_results={},
            bibtex_by_recid={
                1322348: """@article{Littenberg:2014oda,
  author = {Littenberg, Tyson B. and Cornish, Neil J.},
  title = {Bayesian inference for spectral estimation of gravitational wave detector noise},
  eprint = {1410.3852},
  archivePrefix = {arXiv},
  year = {2014}
}
""",
            },
            metadata_by_recid={
                1322348: {
                    "publication_info": [
                        {
                            "journal_title": "Phys.Rev.D",
                            "journal_volume": "91",
                            "journal_issue": "8",
                            "artid": "084034",
                            "year": 2015,
                        }
                    ],
                    "dois": [{"value": "10.1103/PhysRevD.91.084034"}],
                }
            },
        )

        entry = client.fetch_entry(1322348)

        self.assertEqual(entry.fields["journal"], "Phys. Rev. D")
        self.assertEqual(entry.fields["doi"], "10.1103/PhysRevD.91.084034")
        self.assertEqual(entry.fields["pages"], "084034")


class RecordingUpdateClient(InspireClient):
    def __init__(
        self,
        *,
        search_results: dict[str, list[int]],
        bibtex_by_recid: dict[int, str],
        search_metadata: dict[int, bool] | None = None,
        metadata_by_recid: dict[int, dict] | None = None,
    ) -> None:
        super().__init__(base_url="https://example.test/api/literature", timeout=1.0)
        self.search_results = search_results
        self.bibtex_by_recid = bibtex_by_recid
        self.search_metadata = search_metadata or {}
        self.metadata_by_recid = metadata_by_recid or {}
        self.requested_urls: list[str] = []
        self.search_queries: list[str] = []

    def _search_records(self, query, *, matcher, limit):
        from bibtool.inspire import SearchResult

        self.search_queries.append(query)
        recids = self.search_results.get(query, [])[: limit or None]
        return [
            SearchResult(
                recid=recid,
                title="Title",
                authors=["Author"],
                year="2025",
                has_journal_publication=self.search_metadata.get(recid, False),
            )
            for recid in recids
        ]

    def _fetch_metadata(self, recid: int) -> dict:
        return self.metadata_by_recid.get(recid, {})

    def _request_text(self, url: str) -> str:
        self.requested_urls.append(url)
        if "?format=bibtex" in url:
            recid = int(url.rsplit("/", 1)[-1].split("?", 1)[0])
            return self.bibtex_by_recid.get(recid, "")
        return ""


if __name__ == "__main__":
    unittest.main()
