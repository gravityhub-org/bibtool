from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bibtool.bibtex import BibEntry, assign_generated_key, normalize_for_match, parse_bibtex, write_bibtex


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


if __name__ == "__main__":
    unittest.main()
