from __future__ import annotations

import io
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bibtool.cli import run
from bibtool.inspire import SearchResult


class TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


class StubProvider:
    def __init__(self, *, author_entries=None, title_entries=None, author_results=None, title_results=None) -> None:
        self.author_entries = author_entries or []
        self.title_entries = title_entries or []
        self.author_results = author_results or []
        self.title_results = title_results or []

    def fetch_author_entries(self, query: str):
        return list(self.author_entries)

    def fetch_title_entries(self, query: str):
        return list(self.title_entries)

    def search_author(self, query: str, limit: int | None = 20):
        results = list(self.author_results)
        return results if limit is None else results[:limit]

    def search_title(self, query: str, limit: int | None = 20):
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
                title_entries=[
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
                ["--title", "GWTC-5", "--bib", str(target)],
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

    def test_large_import_requires_two_confirmations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "references.bib"
            provider = StubProvider(
                author_entries=[
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
                stdin=TtyStringIO("add\n11\n"),
                stdout=stdout,
                stderr=stderr,
                provider=provider,
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue(target.exists())
            self.assertIn('Type "add" to continue:', stdout.getvalue())
            self.assertEqual(target.read_text(encoding="utf-8").count("@article{"), 11)

    def test_search_title_is_case_insensitive(self) -> None:
        provider = StubProvider(
            title_results=[
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
            ["search", "--title", "searching", "for"],
            stdout=stdout,
            stderr=io.StringIO(),
            provider=provider,
        )

        self.assertEqual(exit_code, 0)
        self.assertIn("Searching For Gravitational Waves", stdout.getvalue())


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


if __name__ == "__main__":
    unittest.main()
