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


class UpdateProvider:
    def __init__(self, *, fresh_by_eprint: dict[str, BibEntry] | None = None) -> None:
        self.fresh_by_eprint = fresh_by_eprint or {}

    def refresh_entry(self, entry: BibEntry) -> BibEntry | None:
        eprint = entry.fields.get("eprint")
        if eprint and eprint in self.fresh_by_eprint:
            fresh = self.fresh_by_eprint[eprint].clone()
            fresh.key = entry.key
            return fresh
        return None

    def refresh_entries(self, entries: list[BibEntry], *, workers: int = 8) -> list[BibEntry | None]:
        return [self.refresh_entry(entry) for entry in entries]

    def lookup(self, *, query=None, name=None, title=None, limit=20, as_entries=False):
        if not as_entries:
            return []
        title_text = title if isinstance(title, str) else " ".join(title or [])
        if title_text and "GW231123" in title_text:
            return [
                BibEntry(
                    entry_type="article",
                    key="InspireKeyIgnored",
                    fields={
                        "author": "Ray, Anarya and Banagiri, Sharan",
                        "title": "GW231123: extreme spins or microglitches?",
                        "eprint": "2510.07228",
                        "archiveprefix": "arXiv",
                        "primaryclass": "gr-qc",
                        "journal": "arXiv",
                        "year": "2025",
                    },
                )
            ]
        return []


class CustomBibliographyTests(unittest.TestCase):
    def test_merge_into_custom_bib_updates_matching_entries_without_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template_dir = root / "template"
            template_dir.mkdir()
            custom = root / "project" / "references.bib"
            custom.parent.mkdir()
            custom.write_text(
                """@article{Alice2025MyCustomKey,
  author = {Ray, Anarya},
  title = {GW231123: extreme spins or microglitches?},
  eprint = {2510.07228},
  archiveprefix = {arXiv},
  year = {2025}
}
""",
                encoding="utf-8",
            )
            (template_dir / "references.bib").write_text(
                """@article{MasterGeneratedKey,
  author = {Ray, Anarya and Banagiri, Sharan},
  title = {GW231123: extreme spins or microglitches?},
  eprint = {2510.07228},
  archiveprefix = {arXiv},
  primaryclass = {gr-qc},
  journal = {arXiv},
  year = {2025},
  month = {10}
}

@article{OnlyInMaster2024,
  author = {Hannuksela, Otto},
  title = {Only in master bibliography},
  eprint = {2401.00099},
  year = {2024}
}
""",
                encoding="utf-8",
            )

            original = os.environ.get("LATEX_TEMPLATE_DIR")
            os.environ["LATEX_TEMPLATE_DIR"] = str(template_dir)
            try:
                exit_code = run(
                    [str(custom), "--y"],
                    stdin=io.StringIO(),
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            finally:
                if original is None:
                    os.environ.pop("LATEX_TEMPLATE_DIR", None)
                else:
                    os.environ["LATEX_TEMPLATE_DIR"] = original

            self.assertEqual(exit_code, 0)
            content = custom.read_text(encoding="utf-8")
            self.assertEqual(content.count("@article{"), 2)
            self.assertIn("@article{Alice2025MyCustomKey,", content)
            self.assertNotIn("MasterGeneratedKey", content)
            self.assertIn("primaryclass = {gr-qc}", content)
            self.assertIn("@article{Hannuksela2024OnlyIn,", content)
            self.assertEqual(content.count("2510.07228"), 1)

    def test_import_into_custom_bib_preserves_existing_custom_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            custom = Path(tmp) / "references.bib"
            custom.write_text(
                """@article{Bob2025PreferredKey,
  author = {Ray, Anarya},
  title = {GW231123: extreme spins or microglitches?},
  eprint = {2510.07228},
  year = {2025}
}
""",
                encoding="utf-8",
            )

            exit_code = run(
                ["--title", "GW231123", "extreme", "--bib", str(custom), "--y"],
                stdin=io.StringIO(),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                provider=UpdateProvider(),
            )

            self.assertEqual(exit_code, 0)
            content = custom.read_text(encoding="utf-8")
            self.assertEqual(content.count("@article{"), 1)
            self.assertIn("@article{Bob2025PreferredKey,", content)
            self.assertNotIn("InspireKeyIgnored", content)
            self.assertIn("primaryclass = {gr-qc}", content)

    def test_update_custom_bib_preserves_collaborator_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            custom = Path(tmp) / "references.bib"
            custom.write_text(
                """@article{Carol2024CustomKey,
  author = {Littenberg, Tyson B. and Cornish, Neil J.},
  title = {Bayesian inference for spectral estimation of gravitational wave detector noise},
  eprint = {1410.3852},
  archiveprefix = {arXiv},
  journal = {arXiv},
  year = {2014}
}

@article{Dave2025AnotherCustomKey,
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
                    "1410.3852": BibEntry(
                        entry_type="article",
                        key="ignored",
                        fields={
                            "author": "Littenberg, Tyson B. and Cornish, Neil J.",
                            "title": "Bayesian inference for spectral estimation of gravitational wave detector noise",
                            "eprint": "1410.3852",
                            "archiveprefix": "arXiv",
                            "doi": "10.1103/PhysRevD.91.084034",
                            "journal": "Phys. Rev. D",
                            "volume": "91",
                            "pages": "084034",
                            "year": "2015",
                        },
                    ),
                }
            )

            exit_code = run(
                ["update", str(custom)],
                stdin=io.StringIO(),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                provider=provider,
            )

            self.assertEqual(exit_code, 0)
            content = custom.read_text(encoding="utf-8")
            self.assertIn("@article{Carol2024CustomKey,", content)
            self.assertIn("journal = {Phys. Rev. D}", content)
            self.assertIn("@article{Dave2025AnotherCustomKey,", content)
            self.assertEqual(content.count("@article{"), 2)

    def test_custom_bib_workflow_merge_then_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template_dir = root / "template"
            template_dir.mkdir()
            custom = root / "paper" / "references.bib"
            custom.parent.mkdir()

            custom.write_text(
                """@article{Eve2025ProjectKey,
  author = {Littenberg, Tyson B. and Cornish, Neil J.},
  title = {Bayesian inference for spectral estimation of gravitational wave detector noise},
  eprint = {1410.3852},
  journal = {arXiv},
  year = {2014}
}
""",
                encoding="utf-8",
            )
            (template_dir / "references.bib").write_text(
                """@article{TemplateKey,
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
                            "pages": "084034",
                            "year": "2015",
                        },
                    ),
                }
            )

            original = os.environ.get("LATEX_TEMPLATE_DIR")
            os.environ["LATEX_TEMPLATE_DIR"] = str(template_dir)
            try:
                merge_code = run(
                    [str(custom), "--y"],
                    stdin=io.StringIO(),
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                    provider=provider,
                )
                update_code = run(
                    ["update", str(custom)],
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

            self.assertEqual(merge_code, 0)
            self.assertEqual(update_code, 0)
            content = custom.read_text(encoding="utf-8")
            self.assertEqual(content.count("@article{"), 1)
            self.assertIn("@article{Eve2025ProjectKey,", content)
            self.assertIn("primaryclass = {gr-qc}", content)
            self.assertIn("journal = {Phys. Rev. D}", content)
            self.assertIn("doi = {10.1103/PhysRevD.91.084034}", content)


if __name__ == "__main__":
    unittest.main()
