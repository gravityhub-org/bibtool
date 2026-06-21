from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bibtool.bibtex import (
    BibEntry,
    assign_generated_key,
    enrich_entry_from_metadata,
    entry_needs_metadata_enrichment,
    merge_entry_fields,
    normalize_for_match,
    normalize_inspire_entry,
    parse_bibtex,
    write_bibtex,
)


class BibtexTests(unittest.TestCase):
    def test_parse_bibtex_handles_nested_braces(self) -> None:
        entries = parse_bibtex(
            """@article{Key,
  author = {Hannuksela, Otto},
  title = {{A {Nested} Title}},
  year = {2024}
}
"""
        )

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].fields["title"], "A {Nested} Title")

    def test_normalize_for_match_is_case_insensitive_and_ascii_folded(self) -> None:
        self.assertEqual(normalize_for_match("  Hännükselä: GWTC-5  "), "hannuksela gwtc 5")

    def test_assign_generated_key_adds_suffix_for_collisions(self) -> None:
        entry = BibEntry(
            entry_type="article",
            key="Remote",
            fields={
                "author": "Hannuksela, Otto",
                "title": "GWTC-5 Methods",
                "year": "2024",
            },
        )

        first = assign_generated_key(entry, {"hannuksela2024gwtc5methods"})
        second = assign_generated_key(entry, {"hannuksela2024gwtc5methods", "hannuksela2024gwtc5methodsa"})

        self.assertEqual(first.key, "Hannuksela2024GWTC5Methodsa")
        self.assertEqual(second.key, "Hannuksela2024GWTC5Methodsb")

    def test_write_bibtex_uses_preferred_field_order(self) -> None:
        text = write_bibtex(
            [
                BibEntry(
                    entry_type="article",
                    key="Key",
                    fields={
                        "year": "2024",
                        "title": "Title",
                        "doi": "10.1/example",
                        "author": "Hannuksela, Otto",
                    },
                )
            ]
        )

        self.assertLess(text.index("author"), text.index("title"))
        self.assertLess(text.index("title"), text.index("doi"))
        self.assertLess(text.index("doi"), text.index("year"))

    def test_normalize_inspire_entry_adds_arxiv_journal(self) -> None:
        entry = normalize_inspire_entry(
            BibEntry(
                entry_type="article",
                key="Ray2025",
                fields={
                    "author": "Ray, Anarya",
                    "title": "GW231123: extreme spins or microglitches?",
                    "eprint": "2510.07228",
                    "archiveprefix": "arXiv",
                    "primaryclass": "gr-qc",
                    "year": "2025",
                },
            )
        )

        self.assertEqual(entry.fields["journal"], "arXiv")

    def test_normalize_inspire_entry_preserves_existing_journal(self) -> None:
        entry = normalize_inspire_entry(
            BibEntry(
                entry_type="article",
                key="Published",
                fields={
                    "author": "Doe, Jane",
                    "title": "A Paper",
                    "journal": "Phys. Rev. D",
                    "eprint": "2401.00001",
                    "archiveprefix": "arXiv",
                    "year": "2024",
                },
            )
        )

        self.assertEqual(entry.fields["journal"], "Phys. Rev. D")

    def test_enrich_entry_from_metadata_adds_publication_fields(self) -> None:
        entry = enrich_entry_from_metadata(
            BibEntry(
                entry_type="article",
                key="Preprint",
                fields={
                    "author": "Littenberg, Tyson B. and Cornish, Neil J.",
                    "title": "Bayesian inference",
                    "eprint": "1410.3852",
                    "archiveprefix": "arXiv",
                    "journal": "arXiv",
                    "year": "2014",
                },
            ),
            {
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
            },
        )

        self.assertEqual(entry.fields["journal"], "Phys. Rev. D")
        self.assertEqual(entry.fields["volume"], "91")
        self.assertEqual(entry.fields["number"], "8")
        self.assertEqual(entry.fields["pages"], "084034")
        self.assertEqual(entry.fields["year"], "2015")
        self.assertEqual(entry.fields["doi"], "10.1103/PhysRevD.91.084034")

    def test_merge_entry_fields_keeps_published_journal_over_incoming_arxiv(self) -> None:
        merged = merge_entry_fields(
            BibEntry(
                entry_type="article",
                key="Key",
                fields={
                    "journal": "Phys. Rev. D",
                    "volume": "91",
                    "year": "2015",
                },
            ),
            BibEntry(
                entry_type="article",
                key="Remote",
                fields={
                    "journal": "arXiv",
                    "eprint": "1410.3852",
                    "year": "2014",
                },
            ),
        )

        self.assertEqual(merged.fields["journal"], "Phys. Rev. D")
        self.assertEqual(merged.fields["eprint"], "1410.3852")

    def test_entry_needs_metadata_enrichment(self) -> None:
        self.assertTrue(
            entry_needs_metadata_enrichment(
                BibEntry(
                    entry_type="article",
                    key="Preprint",
                    fields={"journal": "arXiv", "eprint": "1234.5678"},
                )
            )
        )
        self.assertFalse(
            entry_needs_metadata_enrichment(
                BibEntry(
                    entry_type="article",
                    key="Published",
                    fields={
                        "journal": "Phys. Rev. D",
                        "doi": "10.1/example",
                        "pages": "123",
                    },
                )
            )
        )


if __name__ == "__main__":
    unittest.main()
