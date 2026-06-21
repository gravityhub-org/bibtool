from __future__ import annotations

from dataclasses import dataclass, field
import copy
import re
import unicodedata


PREFERRED_FIELD_ORDER = (
    "author",
    "title",
    "journal",
    "booktitle",
    "archiveprefix",
    "eprint",
    "primaryclass",
    "doi",
    "url",
    "year",
    "month",
)


@dataclass(slots=True)
class BibEntry:
    entry_type: str
    key: str
    fields: dict[str, str] = field(default_factory=dict)

    def clone(self) -> "BibEntry":
        return copy.deepcopy(self)

    @property
    def title(self) -> str:
        return self.fields.get("title", "")

    @property
    def author(self) -> str:
        return self.fields.get("author", "")

    @property
    def year(self) -> str:
        return self.fields.get("year", "")


def parse_bibtex(text: str) -> list[BibEntry]:
    entries: list[BibEntry] = []
    index = 0
    while index < len(text):
        at_index = text.find("@", index)
        if at_index == -1:
            break

        entry_type_match = re.match(r"@([A-Za-z]+)\s*([({])", text[at_index:])
        if not entry_type_match:
            index = at_index + 1
            continue

        entry_type = entry_type_match.group(1).lower()
        opener = entry_type_match.group(2)
        closer = "}" if opener == "{" else ")"
        body_start = at_index + entry_type_match.end()
        body, body_end = _read_enclosed(text, body_start - 1, opener, closer)
        index = body_end

        if entry_type in {"comment", "preamble", "string"}:
            continue

        entry = _parse_entry_body(entry_type, body)
        if entry is not None:
            entries.append(entry)
    return entries


def write_bibtex(entries: list[BibEntry]) -> str:
    chunks: list[str] = []
    for entry in entries:
        ordered_items = _ordered_field_items(entry.fields)
        field_lines = [f"  {name} = {{{value}}}" for name, value in ordered_items]
        body = ",\n".join(field_lines)
        chunks.append(f"@{entry.entry_type}{{{entry.key},\n{body}\n}}")
    return "\n\n".join(chunks) + ("\n" if chunks else "")


def normalize_for_match(value: str) -> str:
    ascii_value = _ascii_fold(strip_outer_wrappers(value))
    words = re.findall(r"[a-z0-9]+", ascii_value.lower())
    return " ".join(words)


def dedupe_tokens(entry: BibEntry) -> set[str]:
    tokens: set[str] = set()
    title = normalize_for_match(entry.title)
    if title:
        tokens.add(f"title:{title}")

    doi = normalize_for_match(entry.fields.get("doi", ""))
    if doi:
        tokens.add(f"doi:{doi}")

    eprint = normalize_for_match(entry.fields.get("eprint", ""))
    if eprint:
        tokens.add(f"eprint:{eprint}")

    return tokens


def sort_key(entry: BibEntry) -> tuple[int, str, str, str]:
    year_value = _extract_year(entry.year)
    sortable_year = year_value if year_value is not None else 9999
    return (
        sortable_year,
        normalize_for_match(_first_author(entry.author)),
        normalize_for_match(entry.title),
        entry.key.lower(),
    )


def assign_generated_key(entry: BibEntry, used_keys: set[str]) -> BibEntry:
    updated = entry.clone()
    base_key = _generate_base_key(updated)
    key = base_key
    suffix_index = 0
    while key.lower() in used_keys:
        suffix_index += 1
        key = f"{base_key}{_alpha_suffix(suffix_index)}"
    updated.key = key
    used_keys.add(key.lower())
    return updated


def strip_outer_wrappers(value: str) -> str:
    stripped = value.strip()
    while True:
        if len(stripped) >= 2 and stripped[0] == "{" and stripped[-1] == "}" and _wrapping_pair(stripped, "{", "}"):
            stripped = stripped[1:-1].strip()
            continue
        if len(stripped) >= 2 and stripped[0] == '"' and stripped[-1] == '"':
            stripped = stripped[1:-1].strip()
            continue
        return stripped


def _parse_entry_body(entry_type: str, body: str) -> BibEntry | None:
    key, separator, remainder = body.partition(",")
    if not separator:
        return None

    fields: dict[str, str] = {}
    index = 0
    while index < len(remainder):
        index = _skip_separators(remainder, index)
        if index >= len(remainder):
            break

        name_match = re.match(r"([A-Za-z][A-Za-z0-9_-]*)\s*=", remainder[index:])
        if not name_match:
            break

        field_name = name_match.group(1).lower()
        index += name_match.end()
        value, index = _read_value(remainder, index)
        fields[field_name] = strip_outer_wrappers(value)
        index = _skip_separators(remainder, index)

    return BibEntry(entry_type=entry_type, key=key.strip(), fields=fields)


def _read_enclosed(text: str, start: int, opener: str, closer: str) -> tuple[str, int]:
    depth = 0
    index = start
    in_quote = False
    while index < len(text):
        char = text[index]
        if char == '"' and (index == 0 or text[index - 1] != "\\"):
            in_quote = not in_quote
        elif not in_quote:
            if char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    return text[start + 1:index], index + 1
        index += 1
    raise ValueError("Unterminated BibTeX entry")


def _read_value(text: str, index: int) -> tuple[str, int]:
    index = _skip_whitespace(text, index)
    if index >= len(text):
        return "", index

    parts: list[str] = []
    while index < len(text):
        char = text[index]
        if char == "{":
            value, index = _read_enclosed(text, index, "{", "}")
            parts.append("{" + value + "}")
        elif char == '"':
            index += 1
            start = index
            escaped = False
            while index < len(text):
                if text[index] == '"' and not escaped:
                    break
                escaped = text[index] == "\\" and not escaped
                if text[index] != "\\":
                    escaped = False
                index += 1
            parts.append('"' + text[start:index] + '"')
            index += 1
        else:
            start = index
            while index < len(text) and text[index] not in ",#}":
                index += 1
            parts.append(text[start:index].strip())

        index = _skip_whitespace(text, index)
        if index < len(text) and text[index] == "#":
            index += 1
            index = _skip_whitespace(text, index)
            continue
        break
    return "".join(parts).strip(), index


def _skip_whitespace(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _skip_separators(text: str, index: int) -> int:
    while index < len(text) and (text[index].isspace() or text[index] == ","):
        index += 1
    return index


def _ordered_field_items(fields: dict[str, str]) -> list[tuple[str, str]]:
    remaining = dict(fields)
    ordered: list[tuple[str, str]] = []
    for name in PREFERRED_FIELD_ORDER:
        if name in remaining:
            ordered.append((name, remaining.pop(name)))
    ordered.extend(sorted(remaining.items()))
    return ordered


def _wrapping_pair(value: str, opener: str, closer: str) -> bool:
    depth = 0
    for index, char in enumerate(value):
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0 and index != len(value) - 1:
                return False
    return depth == 0


def _first_author(author_field: str) -> str:
    authors = [part.strip() for part in author_field.split(" and ") if part.strip()]
    if not authors:
        return ""
    first = strip_outer_wrappers(authors[0])
    if "," in first:
        return first.split(",", 1)[0].strip()
    parts = first.split()
    return parts[-1] if parts else ""


def _extract_year(year_field: str) -> int | None:
    match = re.search(r"\d{4}", year_field)
    if match:
        return int(match.group(0))
    return None


def _generate_base_key(entry: BibEntry) -> str:
    author_part = _to_identifier(_first_author(entry.author)) or "Unknown"
    year_part = str(_extract_year(entry.year) or "0000")
    title_words = [_to_identifier(word) for word in _ascii_fold(strip_outer_wrappers(entry.title)).split()]
    title_words = [word for word in title_words if word]
    title_part = "".join(title_words[:2]) or "Untitled"
    return f"{author_part}{year_part}{title_part}"


def _to_identifier(value: str) -> str:
    folded = _ascii_fold(value)
    words = re.findall(r"[A-Za-z0-9]+", folded)
    return "".join(word[:1].upper() + word[1:] for word in words)


def _ascii_fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return normalized.encode("ascii", "ignore").decode("ascii")


def _alpha_suffix(index: int) -> str:
    letters: list[str] = []
    value = index
    while value > 0:
        value -= 1
        letters.append(chr(ord("a") + (value % 26)))
        value //= 26
    return "".join(reversed(letters))
