# bibtool

`bibtool` manages a `references.bib` file by merging from a LaTeX template or importing records from INSPIRE HEP without duplicating existing entries by title, DOI, or eprint.

## Usage

```bash
bibtool references.bib
bibtool --name Otto Hannuksela
bibtool --title GWTC-5
bibtool search --title "searching for"
```

## Behavior

- `bibtool references.bib` merges `$LATEX_TEMPLATE_DIR/references.bib` into the target file.
- `--name` and `--title` import records into `references.bib` by default, or another file via `--bib`.
- `search` queries INSPIRE HEP and prints matching records without modifying files.
- Duplicate detection is case-insensitive and checks title, DOI, and eprint rather than only BibTeX keys.
- Newly added keys use `FirstAuthorYearFirstTwoTitleWords`, while existing keys in the destination file stay unchanged.
- Output is sorted by year, then first author, then title.
- Adding more than 10 entries requires two interactive confirmations.

## Tests

```bash
python -m unittest discover -s tests
```