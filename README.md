# bibtool

`bibtool` manages a `references.bib` file by merging from a LaTeX template or importing records from INSPIRE HEP without duplicating existing entries by title, DOI, or eprint.

## Usage

Install and add bash completion:

```bash
uv tool install --upgrade git+https://github.com/gravityhub-org/bibtool.git && bibtool install-completion
```

```bash
bibtool references.bib
bibtool update
bibtool update references.bib --y
bibtool --y --name Otto Hannuksela
bibtool --query Otto Hannuksela
bibtool --name Otto Hannuksela
bibtool --name Otto Hannuksela --title GWTC-5
bibtool --title GWTC-5
bibtool search "searching for"
bibtool search --name Otto Hannuksela --title GWTC-5
bibtool --install-completion
```

## Behavior

- `bibtool references.bib` merges `$LATEX_TEMPLATE_DIR/references.bib` into the target file.
- `--query`, `--name`, and `--title` import into `$LATEX_TEMPLATE_DIR/references.bib` by default, or another file via `--bib`.
- `--name` and `--title` can be combined in one import command; their results are merged and de-duplicated before writing.
- `search` takes plain search terms and queries INSPIRE HEP without modifying files.
- `update` refreshes every entry in a bibliography from INSPIRE, preserving existing BibTeX keys.
- `update` defaults to `$LATEX_TEMPLATE_DIR/references.bib`, or accepts a path/`--bib`.
- `update` looks up records by eprint, then DOI, then author and title.
- `search --name ... --title ...` can also combine both filters; results are merged and de-duplicated.
- Search/import use a looser cuhkvoting-style keyword query across title and author, then apply a case-insensitive local AND filter on the returned metadata.
- Duplicate detection is case-insensitive and checks title, DOI, and eprint rather than only BibTeX keys.
- Matching entries are updated in place from INSPIRE or template data while preserving their existing BibTeX keys.
- arXiv-only `@article` records from INSPIRE get `journal = {arXiv}` so REVTeX/APS BibTeX styles compile cleanly.
- Newly added keys use `FirstAuthorYearFirstTwoTitleWords`, while existing keys in the destination file stay unchanged.
- Output is sorted by year, then first author, then title.
- Adding more than 10 entries requires two interactive confirmations unless `--y` is passed.

## Bash completion

Install the built-in bash completion script:

```bash
bibtool --install-completion
```

Or print it for manual setup:

```bash
eval "$(bibtool --print-completion bash)"
```

## Tests

```bash
python -m unittest discover -s tests
```
