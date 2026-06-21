# bibtool

`bibtool` manages a `references.bib` file by merging from a LaTeX template or importing records from INSPIRE HEP without duplicating existing entries by title, DOI, or eprint.

## Usage

Install and add bash completion:

```bash
uv tool install --upgrade git+https://github.com/gravityhub-org/bibtool.git && uv tool install-completion
```

```bash
bibtool references.bib
bibtool --query Otto Hannuksela
bibtool --name Otto Hannuksela
bibtool --title GWTC-5
bibtool search "searching for"
bibtool --install-completion
```

## Behavior

- `bibtool references.bib` merges `$LATEX_TEMPLATE_DIR/references.bib` into the target file.
- `--query` imports records into `references.bib` by default, or another file via `--bib`; `--name` and `--title` remain supported aliases.
- `search` takes plain search terms and queries INSPIRE HEP without modifying files.
- Search/import use a looser cuhkvoting-style keyword query across title and author, then apply a case-insensitive local AND filter on the returned metadata.
- Duplicate detection is case-insensitive and checks title, DOI, and eprint rather than only BibTeX keys.
- Newly added keys use `FirstAuthorYearFirstTwoTitleWords`, while existing keys in the destination file stay unchanged.
- Output is sorted by year, then first author, then title.
- Adding more than 10 entries requires two interactive confirmations.

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
