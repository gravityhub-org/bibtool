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
from bibtool.bibtex import BibEntry
from bibtool.inspire import (
    InspireClient,
    InspireError,
    SearchResult,
    clear_response_caches,
    parse_literature_id,
)


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
        catalog=None,
    ) -> None:
        self.query_entries = query_entries or []
        self.author_entries = author_entries if author_entries is not None else list(self.query_entries)
        self.title_entries = title_entries if title_entries is not None else list(self.query_entries)
        self.query_results = query_results or []
        self.author_results = author_results if author_results is not None else list(self.query_results)
        self.title_results = title_results if title_results is not None else list(self.query_results)
        if catalog is not None:
            self.catalog = catalog
        else:
            self.catalog = self._build_catalog()
        self.lookup_calls: list[tuple[str | None, str | None, str | None, int | None, bool]] = []
        self.seen_queries: list[tuple[str, int | None]] = []
        self.seen_name_title_queries: list[tuple[str, str, int | None]] = []
        self.fetch_calls: list[tuple[str, str]] = []
        self.pdf_calls: list[str] = []
        self.pdf_data: dict[str, bytes] = {}
        self.pdf_errors: dict[str, Exception] = {}
        self.recid_calls: list[int] = []
        self.arxiv_id_calls: list[str] = []

    def _build_catalog(self) -> list[tuple[int, BibEntry]]:
        catalog: list[tuple[int, BibEntry]] = []
        seen_recids: set[int] = set()
        seen_keys: set[str] = set()
        recid = 1
        for entry in [*self.query_entries, *self.author_entries, *self.title_entries]:
            if entry.key in seen_keys:
                continue
            seen_keys.add(entry.key)
            catalog.append((recid, entry))
            seen_recids.add(recid)
            recid += 1
        for result in [*self.query_results, *self.author_results, *self.title_results]:
            if result.recid in seen_recids:
                continue
            seen_recids.add(result.recid)
            catalog.append((result.recid, _result_to_entry(result)))
        return catalog

    def lookup(
        self,
        *,
        query: str | None = None,
        name: str | None = None,
        title: str | None = None,
        limit: int | None = 20,
        as_entries: bool = False,
    ):
        self.lookup_calls.append((query, name, title, limit, as_entries))
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
                arxiv_id=entry.fields.get("eprint", ""),
            )
            for recid, entry in matched
        ]

    def fetch_pdf(self, arxiv_id: str) -> bytes:
        self.pdf_calls.append(arxiv_id)
        if arxiv_id in self.pdf_errors:
            raise self.pdf_errors[arxiv_id]
        return self.pdf_data.get(arxiv_id, b"%PDF-1.4 stub")

    def fetch_result_by_recid(self, recid: int) -> SearchResult:
        self.recid_calls.append(recid)
        for catalog_recid, entry in self.catalog:
            if catalog_recid == recid:
                return SearchResult(
                    recid=catalog_recid,
                    title=entry.title,
                    authors=[part.strip() for part in entry.author.split(" and ") if part.strip()],
                    year=entry.year,
                    arxiv_id=entry.fields.get("eprint", ""),
                )
        raise InspireError(f"INSPIRE returned no metadata for record {recid}.")

    def fetch_result_by_arxiv(self, arxiv_id: str) -> SearchResult:
        from bibtool.inspire import normalize_arxiv_id

        cleaned = normalize_arxiv_id(arxiv_id)
        self.arxiv_id_calls.append(cleaned)
        for catalog_recid, entry in self.catalog:
            if entry.fields.get("eprint", "") == cleaned:
                return SearchResult(
                    recid=catalog_recid,
                    title=entry.title,
                    authors=[part.strip() for part in entry.author.split(" and ") if part.strip()],
                    year=entry.year,
                    arxiv_id=cleaned,
                )
        return SearchResult(recid=0, title=cleaned, authors=[], year="", arxiv_id=cleaned)

    def fetch_query_entries(self, query: str, limit: int | None = None):
        self.fetch_calls.append(("query", query))
        return self.lookup(query=query, limit=limit, as_entries=True)

    def search(self, query: str, limit: int | None = 20):
        self.seen_queries.append((query, limit))
        return self.lookup(query=query, limit=limit, as_entries=False)

    def fetch_author_entries(self, query: str, limit: int | None = None):
        self.fetch_calls.append(("author", query))
        return self.lookup(name=query, limit=limit, as_entries=True)

    def fetch_title_entries(self, query: str, limit: int | None = None):
        self.fetch_calls.append(("title", query))
        return self.lookup(title=query, limit=limit, as_entries=True)

    def search_author(self, query: str, limit: int | None = 20):
        self.seen_queries.append((query, limit))
        return self.lookup(name=query, limit=limit, as_entries=False)

    def search_title(self, query: str, limit: int | None = 20):
        self.seen_queries.append((query, limit))
        return self.lookup(title=query, limit=limit, as_entries=False)

    def search_name_and_title(self, name: str, title: str, limit: int | None = 20):
        self.seen_name_title_queries.append((name, title, limit))
        return self.lookup(name=name, title=title, limit=limit, as_entries=False)


class LiteratureIdParsingTests(unittest.TestCase):
    def test_parse_arxiv_and_inspire_ids(self) -> None:
        self.assertEqual(parse_literature_id("2501.12345"), ("arxiv", "2501.12345"))
        self.assertEqual(parse_literature_id("arXiv:2501.12345v2"), ("arxiv", "2501.12345"))
        self.assertEqual(parse_literature_id("https://arxiv.org/pdf/2501.12345.pdf"), ("arxiv", "2501.12345"))
        self.assertEqual(parse_literature_id("hep-ph/9901001"), ("arxiv", "hep-ph/9901001"))
        self.assertEqual(parse_literature_id("2738695"), ("inspire", "2738695"))
        self.assertEqual(parse_literature_id("inspire:2738695"), ("inspire", "2738695"))
        self.assertEqual(
            parse_literature_id("https://inspirehep.net/literature/2738695"),
            ("inspire", "2738695"),
        )
        self.assertIsNone(parse_literature_id("GWTC-5"))
        self.assertIsNone(parse_literature_id("2024"))


class BibtoolCliTests(unittest.TestCase):
    def test_import_updates_existing_entry_and_preserves_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "references.bib"
            target.write_text(
                """@article{Ray2025GW231123Extreme,
  author = {Ray, Anarya and Banagiri, Sharan and Thrane, Eric and Lasky, Paul D.},
  title = {GW231123: extreme spins or microglitches?},
  archiveprefix = {arXiv},
  eprint = {2510.07228},
  primaryclass = {gr-qc},
  year = {2025},
  month = {10},
  reportnumber = {LIGO-P2500613}
}
""",
                encoding="utf-8",
            )

            provider = StubProvider(
                query_entries=[
                    BibEntry(
                        entry_type="article",
                        key="Ray:2025rtt",
                        fields={
                            "author": "Ray, Anarya and Banagiri, Sharan and Thrane, Eric and Lasky, Paul D.",
                            "title": "GW231123: extreme spins or microglitches?",
                            "eprint": "2510.07228",
                            "archiveprefix": "arXiv",
                            "primaryclass": "gr-qc",
                            "journal": "arXiv",
                            "year": "2025",
                            "month": "10",
                            "reportnumber": "LIGO-P2500613",
                        },
                    ),
                ]
            )

            stdout = io.StringIO()
            exit_code = run(
                ["--title", "GW231123", "extreme", "spins", "--bib", str(target), "--y"],
                stdin=io.StringIO(),
                stdout=stdout,
                stderr=io.StringIO(),
                provider=provider,
            )

            self.assertEqual(exit_code, 0)
            content = target.read_text(encoding="utf-8")
            self.assertIn("@article{Ray2025GW231123Extreme,", content)
            self.assertIn("journal = {arXiv}", content)
            self.assertIn("Updated 1 entries", stdout.getvalue())

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
            self.assertEqual(provider.lookup_calls[0], ("GWTC-5", None, None, None, True))

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
                    _entry("AuthorKey", author="Hannuksela, Otto", title="GWTC-5 Result", year="2024"),
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
            self.assertIn("@article{Hannuksela2024GWTC5Result,", content)
            self.assertNotIn("Cornish2025GWTC5Result", content)
            self.assertEqual(provider.lookup_calls[0][:4], (None, "Otto Hannuksela", "GWTC-5", None))

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
            self.assertEqual(provider.lookup_calls[0][:4], (None, "Otto Hannuksela", None, None))

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
                    arxiv_id="2501.12345",
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
        self.assertIn("arXiv:2501.12345", stdout.getvalue())

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
        self.assertEqual(provider.lookup_calls[0], ("GWTC-5 Hannuksela", None, None, 20, False))
        self.assertIn("GWTC-5 Methods", stdout.getvalue())

    def test_search_allows_name_and_title_together(self) -> None:
        provider = StubProvider(
            catalog=[
                (
                    301,
                    _entry("AuthorKey", author="Hannuksela, Otto", title="GWTC-5 Author Match", year="2024"),
                ),
                (
                    999,
                    _entry("WrongKey", author="Hannuksela, Otto", title="Wrong Title", year="2022"),
                ),
            ],
        )

        stdout = io.StringIO()
        exit_code = run(
            ["search", "--name", "Otto", "Hannuksela", "--title", "GWTC-5"],
            stdout=stdout,
            stderr=io.StringIO(),
            provider=provider,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(provider.lookup_calls[0][:4], (None, "Otto Hannuksela", "GWTC-5", 20))
        self.assertIn("GWTC-5 Author Match", stdout.getvalue())
        self.assertNotIn("Wrong Title", stdout.getvalue())

    def test_search_dedupes_combined_name_and_title_results(self) -> None:
        shared = _entry("SharedKey", author="Hannuksela, Otto", title="Shared Match", year="2024")
        provider = StubProvider(
            catalog=[(401, shared), (401, shared)],
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

    def test_search_name_and_title_matches_bayesian_substring(self) -> None:
        provider = StubProvider(
            catalog=[
                (
                    2738695,
                    _entry(
                        "BayesKey",
                        author="Gupta, Toral and Cornish, Neil J.",
                        title="Bayesian power spectral estimation of gravitational wave detector noise",
                        year="2024",
                    ),
                ),
                (
                    501,
                    _entry("OtherKey", author="Cornish, Neil", title="A Paper Without Keyword", year="2024"),
                ),
            ],
        )

        stdout = io.StringIO()
        exit_code = run(
            ["search", "--name", "Neil", "Cornish", "--title", "Bayes"],
            stdout=stdout,
            stderr=io.StringIO(),
            provider=provider,
        )

        self.assertEqual(exit_code, 0)
        self.assertIn("Bayesian power spectral estimation", stdout.getvalue())
        self.assertNotIn("A Paper Without Keyword", stdout.getvalue())

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

    def test_download_writes_pdfs_for_matching_arxiv_records(self) -> None:
        provider = StubProvider(
            query_results=[
                SearchResult(
                    recid=101,
                    title="Searching For Gravitational Waves",
                    authors=["Hannuksela, Otto"],
                    year="2025",
                    arxiv_id="2501.12345",
                ),
                SearchResult(
                    recid=102,
                    title="Searching For Something Else",
                    authors=["Someone Else"],
                    year="2024",
                ),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            stdout = io.StringIO()
            exit_code = run(
                ["download", "searching", "for", "--dir", str(dest)],
                stdout=stdout,
                stderr=io.StringIO(),
                provider=provider,
            )

            pdf_path = dest / "2501.12345.pdf"
            self.assertEqual(exit_code, 0)
            self.assertEqual(provider.lookup_calls[0], ("searching for", None, None, 20, False))
            self.assertEqual(provider.pdf_calls, ["2501.12345"])
            self.assertTrue(pdf_path.exists())
            self.assertEqual(pdf_path.read_bytes(), b"%PDF-1.4 stub")
            self.assertIn(f"Downloaded {pdf_path}", stdout.getvalue())
            self.assertIn("Skipped 1 records without an arXiv eprint.", stdout.getvalue())

    def test_download_skips_existing_pdfs(self) -> None:
        provider = StubProvider(
            query_results=[
                SearchResult(
                    recid=101,
                    title="Searching For Gravitational Waves",
                    authors=["Hannuksela, Otto"],
                    year="2025",
                    arxiv_id="2501.12345",
                ),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            existing = dest / "2501.12345.pdf"
            existing.write_bytes(b"%PDF-1.4 existing")
            stdout = io.StringIO()
            exit_code = run(
                ["download", "searching", "--dir", str(dest)],
                stdout=stdout,
                stderr=io.StringIO(),
                provider=provider,
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(provider.pdf_calls, [])
            self.assertEqual(existing.read_bytes(), b"%PDF-1.4 existing")
            self.assertIn("All 1 matching PDFs already exist", stdout.getvalue())

    def test_download_uses_same_name_title_lookup_as_search(self) -> None:
        provider = StubProvider(
            catalog=[
                (
                    301,
                    _entry(
                        "AuthorKey",
                        author="Hannuksela, Otto",
                        title="GWTC-5 Author Match",
                        year="2024",
                        eprint="2401.00001",
                    ),
                ),
            ],
        )

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            exit_code = run(
                ["download", "--name", "Otto", "Hannuksela", "--title", "GWTC-5", "--dir", str(dest)],
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                provider=provider,
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(provider.lookup_calls[0][:4], (None, "Otto Hannuksela", "GWTC-5", 20))
            self.assertEqual(provider.pdf_calls, ["2401.00001"])
            self.assertTrue((dest / "2401.00001.pdf").exists())

    def test_download_sanitizes_legacy_arxiv_filenames(self) -> None:
        provider = StubProvider(
            query_results=[
                SearchResult(
                    recid=55,
                    title="Legacy Paper",
                    authors=["Author, A."],
                    year="1999",
                    arxiv_id="hep-ph/9901001",
                ),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            exit_code = run(
                ["download", "legacy", "--dir", str(dest)],
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                provider=provider,
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue((dest / "hep-ph_9901001.pdf").exists())

    def test_download_by_arxiv_id(self) -> None:
        provider = StubProvider(
            catalog=[
                (
                    101,
                    _entry(
                        "ArxivKey",
                        author="Hannuksela, Otto",
                        title="By Arxiv Id",
                        year="2025",
                        eprint="2501.12345",
                    ),
                ),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            stdout = io.StringIO()
            exit_code = run(
                ["download", "arXiv:2501.12345v2", "--dir", str(dest)],
                stdout=stdout,
                stderr=io.StringIO(),
                provider=provider,
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(provider.lookup_calls, [])
            self.assertEqual(provider.arxiv_id_calls, ["2501.12345"])
            self.assertEqual(provider.pdf_calls, ["2501.12345"])
            self.assertTrue((dest / "2501.12345.pdf").exists())

    def test_download_by_inspire_id(self) -> None:
        provider = StubProvider(
            catalog=[
                (
                    2738695,
                    _entry(
                        "InspireKey",
                        author="Cornish, Neil",
                        title="By Inspire Id",
                        year="2024",
                        eprint="2312.11808",
                    ),
                ),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            exit_code = run(
                ["download", "2738695", "--dir", str(dest)],
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                provider=provider,
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(provider.lookup_calls, [])
            self.assertEqual(provider.recid_calls, [2738695])
            self.assertEqual(provider.pdf_calls, ["2312.11808"])
            self.assertTrue((dest / "2312.11808.pdf").exists())

    def test_download_by_explicit_arxiv_and_inspire_flags(self) -> None:
        provider = StubProvider(
            catalog=[
                (
                    101,
                    _entry("A", author="A", title="A", year="2025", eprint="2501.12345"),
                ),
                (
                    202,
                    _entry("B", author="B", title="B", year="2024", eprint="2401.00001"),
                ),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            exit_code = run(
                ["download", "--arxiv", "2501.12345", "--inspire", "202", "--dir", str(dest)],
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                provider=provider,
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(provider.arxiv_id_calls, ["2501.12345"])
            self.assertEqual(provider.recid_calls, [202])
            self.assertEqual(sorted(provider.pdf_calls), ["2401.00001", "2501.12345"])
            self.assertTrue((dest / "2501.12345.pdf").exists())
            self.assertTrue((dest / "2401.00001.pdf").exists())

    def test_download_rejects_mixed_ids_and_search_terms(self) -> None:
        stderr = io.StringIO()
        exit_code = run(
            ["download", "2501.12345", "Hannuksela"],
            stdout=io.StringIO(),
            stderr=stderr,
            provider=StubProvider(),
        )

        self.assertEqual(exit_code, 1)
        self.assertIn("Mix of literature ids and search terms is not supported", stderr.getvalue())

    def test_download_inspire_id_without_arxiv_is_skipped(self) -> None:
        provider = StubProvider(
            catalog=[
                (
                    404,
                    _entry("NoArxiv", author="Author, A.", title="No Eprint", year="2020"),
                ),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            stdout = io.StringIO()
            exit_code = run(
                ["download", "--inspire", "404", "--dir", tmp],
                stdout=stdout,
                stderr=io.StringIO(),
                provider=provider,
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(provider.pdf_calls, [])
            self.assertIn("No matching records with an arXiv eprint to download.", stdout.getvalue())

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
        self.assertEqual(provider.lookup_calls[0], (None, None, "GWTC-5", None, True))

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
        self.assertIn("size=50", client.requested_urls[0])
        self.assertIn("publication_info", client.requested_urls[0])

    def test_search_author_and_year_uses_date_filter(self) -> None:
        client = FakeInspireClient(
            pages=[
                _search_page(
                    [
                        _search_hit(recid=1, title="Lens Dynamics", author="Koopmans, L.V.E.", year="2009"),
                        _search_hit(recid=2, title="Later Work", author="Koopmans, L.V.E.", year="2015"),
                        _search_hit(recid=3, title="Other 2009", author="Someone Else", year="2009"),
                    ]
                )
            ]
        )

        results = client.search("koopmans 2009")

        self.assertEqual([result.recid for result in results], [1])
        self.assertIn("author%3A%22koopmans%22", client.requested_urls[0])
        self.assertIn("date%3A2009", client.requested_urls[0])
        self.assertNotIn("title%3A%222009%22", client.requested_urls[0])

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


def _entry(key: str, *, author: str, title: str, year: str, eprint: str | None = None):
    from bibtool.bibtex import BibEntry

    fields = {
        "author": author,
        "title": title,
        "year": year,
    }
    if eprint:
        fields["eprint"] = eprint
    return BibEntry(
        entry_type="article",
        key=key,
        fields=fields,
    )


def _result_to_entry(result: SearchResult) -> BibEntry:
    fields = {
        "author": " and ".join(result.authors),
        "title": result.title,
        "year": result.year,
    }
    if result.arxiv_id:
        fields["eprint"] = result.arxiv_id
    return BibEntry(
        entry_type="article",
        key=f"Rec{result.recid}",
        fields=fields,
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
