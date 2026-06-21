from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from .bibtex import BibEntry, parse_bibtex, normalize_for_match


class InspireError(RuntimeError):
    pass


_JSON_CACHE: dict[str, Any] = {}
_TEXT_CACHE: dict[str, str] = {}


@dataclass(slots=True)
class SearchResult:
    recid: int
    title: str
    authors: list[str]
    year: str
    abstract: str = ""

    @property
    def first_author(self) -> str:
        return self.authors[0] if self.authors else ""


class InspireClient:
    def __init__(self, base_url: str = "https://inspirehep.net/api/literature", timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def fetch_query_entries(self, query: str) -> list[BibEntry]:
        query_words = _normalized_words(query)
        return self._fetch_bibtex_entries(
            self._keyword_query(query),
            matcher=lambda entry: _entry_contains_all_words(entry, query_words, include_author=True, include_title=True),
        )

    def fetch_author_entries(self, query: str) -> list[BibEntry]:
        return self._fetch_bibtex_entries(
            self._author_query(query),
        )

    def fetch_title_entries(self, query: str) -> list[BibEntry]:
        return self._fetch_bibtex_entries(
            self._title_query(query),
        )

    def search(self, query: str, limit: int | None = 20) -> list[SearchResult]:
        normalized_query_words = _normalized_words(query)
        return self._search_records(
            self._keyword_query(query),
            matcher=lambda result: _result_contains_all_words(result, normalized_query_words),
            limit=limit,
        )

    def search_title(self, query: str, limit: int | None = 20) -> list[SearchResult]:
        normalized_query_words = _normalized_words(query)
        return self._search_records(
            self._title_query(query),
            matcher=lambda result: _text_contains_all_words(result.title, normalized_query_words),
            limit=limit,
        )

    def search_author(self, query: str, limit: int | None = 20) -> list[SearchResult]:
        normalized_query_words = _normalized_words(query)
        return self._search_records(
            self._author_query(query),
            matcher=lambda result: any(_text_contains_all_words(author, normalized_query_words) for author in result.authors),
            limit=limit,
        )

    def search_name_and_title(self, name: str, title: str, limit: int | None = 20) -> list[SearchResult]:
        name_words = _normalized_words(name)
        title_words = _normalized_words(title)
        return self._search_records(
            self._name_title_query(name, title),
            matcher=lambda result: (
                any(_text_contains_all_words(author, name_words) for author in result.authors)
                and _text_contains_all_words(result.title, title_words)
            ),
            limit=limit,
        )

    def fetch_entry(self, recid: int) -> BibEntry:
        body = self._request_text(f"{self.base_url}/{recid}?format=bibtex")
        entries = parse_bibtex(body)
        if not entries:
            raise InspireError(f"INSPIRE returned no BibTeX for record {recid}.")
        return entries[0]

    def _fetch_bibtex_entries(
        self,
        query: str,
        *,
        matcher: Callable[[BibEntry], bool] | None = None,
    ) -> list[BibEntry]:
        entries: list[BibEntry] = []
        page = 1
        page_size = 50
        while page <= 10:
            params = urlencode(
                {
                    "q": query,
                    "page": page,
                    "size": page_size,
                    "sort": "mostrecent",
                    "format": "bibtex",
                }
            )
            body = self._request_text(f"{self.base_url}/?{params}")
            parsed_entries = parse_bibtex(body)
            page_entries = [entry for entry in parsed_entries if matcher(entry)] if matcher is not None else parsed_entries
            if not page_entries:
                break
            entries.extend(page_entries)
            if len(parsed_entries) < page_size:
                break
            page += 1
        return entries

    def _search_records(
        self,
        query: str,
        *,
        matcher: Callable[[SearchResult], bool],
        limit: int | None,
    ) -> list[SearchResult]:
        results: list[SearchResult] = []
        seen_recids: set[int] = set()
        page = 1
        page_size = 50
        while page <= 10:
            if limit is not None and len(results) >= limit:
                break
            params = urlencode(
                {
                    "q": query,
                    "page": page,
                    "size": page_size,
                    "sort": "mostrecent",
                    "fields": "titles,authors,abstracts,imprints,preprint_date,publication_info",
                }
            )
            payload = self._request_json(f"{self.base_url}/?{params}")
            hits = payload.get("hits", {}).get("hits", [])
            if not hits:
                break
            for result in _search_results_from_hits(hits):
                if result.recid in seen_recids:
                    continue
                if matcher(result):
                    seen_recids.add(result.recid)
                    results.append(result)
                    if limit is not None and len(results) >= limit:
                        return results
            if len(hits) < page_size:
                break
            page += 1
        return results

    def _keyword_query(self, query: str) -> str:
        tokens = _query_tokens(query)
        searchable = [
            token
            for token in tokens
            if token.lower() not in _STOP_WORDS and len(normalize_for_match(token).replace(" ", "")) > 1
        ]
        if not searchable:
            searchable = tokens
        if not searchable:
            raise InspireError("Search query cannot be empty.")
        return " and ".join(f'(title:"{_escape_query_token(token)}" or author:"{_escape_query_token(token)}")' for token in searchable)

    def _author_query(self, query: str) -> str:
        tokens = _query_tokens(query)
        if not tokens:
            raise InspireError("Search query cannot be empty.")
        return " and ".join(f'author:"{_escape_query_token(token)}"' for token in tokens)

    def _title_query(self, query: str) -> str:
        tokens = _query_tokens(query)
        if not tokens:
            raise InspireError("Search query cannot be empty.")
        return " and ".join(f'title:"{_escape_query_token(token)}"' for token in tokens)

    def _name_title_query(self, name: str, title: str) -> str:
        title_tokens = _query_tokens(title)
        if not title_tokens:
            raise InspireError("Search query cannot be empty.")
        title_part = " and ".join(f"title:{_escape_query_token(token)}*" for token in title_tokens)
        return f"{self._author_query(name)} and {title_part}"

    def _request_json(self, url: str) -> dict[str, Any]:
        cached = _JSON_CACHE.get(url)
        if cached is not None:
            return cached
        try:
            with urlopen(url, timeout=self.timeout) as response:
                payload = json.load(response)
        except (HTTPError, URLError, json.JSONDecodeError) as error:
            raise InspireError(f"Unable to query INSPIRE: {error}") from error
        _JSON_CACHE[url] = payload
        return payload

    def _request_text(self, url: str) -> str:
        cached = _TEXT_CACHE.get(url)
        if cached is not None:
            return cached
        try:
            with urlopen(url, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except (HTTPError, URLError, UnicodeDecodeError) as error:
            raise InspireError(f"Unable to fetch BibTeX from INSPIRE: {error}") from error
        _TEXT_CACHE[url] = body
        return body


def _search_results_from_hits(hits: list[dict[str, Any]]) -> list[SearchResult]:
    results: list[SearchResult] = []
    for hit in hits:
        metadata = hit.get("metadata", {})
        authors = [
            author.get("full_name", "")
            for author in metadata.get("authors", [])
            if author.get("full_name")
        ]
        title = _title_from_metadata(metadata)
        year = _year_from_metadata(metadata)
        abstract = _abstract_from_metadata(metadata)
        recid = hit.get("id") or hit.get("metadata", {}).get("control_number")
        if not recid or not title:
            continue
        results.append(SearchResult(recid=int(recid), title=title, authors=authors, year=year, abstract=abstract))
    return results


def _title_from_metadata(metadata: dict[str, Any]) -> str:
    titles = metadata.get("titles", [])
    if titles:
        title = titles[0].get("title")
        if title:
            return title
    return ""


def _year_from_metadata(metadata: dict[str, Any]) -> str:
    if metadata.get("imprints"):
        year = metadata["imprints"][0].get("date")
        if year:
            return str(year)[:4]
    if metadata.get("preprint_date"):
        return str(metadata["preprint_date"])[:4]
    for item in metadata.get("publication_info", []):
        year = item.get("year")
        if year:
            return str(year)
    return ""


def _abstract_from_metadata(metadata: dict[str, Any]) -> str:
    abstracts = metadata.get("abstracts", [])
    if abstracts:
        abstract = abstracts[0].get("value")
        if abstract:
            return str(abstract)
    return ""


def _normalized_words(value: str) -> list[str]:
    return [word for word in normalize_for_match(value).split() if word]


def _query_tokens(value: str) -> list[str]:
    return [token for token in value.split() if token.strip()]


def _result_contains_all_words(result: SearchResult, required_words: list[str]) -> bool:
    if not required_words:
        return True
    haystack = normalize_for_match(" ".join([result.title, result.abstract, *result.authors]))
    return all(word in haystack for word in required_words)


def _text_contains_all_words(text: str, required_words: list[str]) -> bool:
    if not required_words:
        return True
    haystack = normalize_for_match(text)
    return all(word in haystack for word in required_words)


def _entry_contains_all_words(
    entry: BibEntry,
    required_words: list[str],
    *,
    include_author: bool,
    include_title: bool,
) -> bool:
    if not required_words:
        return True
    fields: list[str] = []
    if include_title:
        fields.append(entry.title)
    if include_author:
        fields.append(entry.author)
    haystack = normalize_for_match(" ".join(fields))
    return all(word in haystack for word in required_words)


def _escape_query_token(token: str) -> str:
    return token.replace('"', '\\"')


_STOP_WORDS = {"a", "an", "the", "of", "for", "to", "and", "or", "in", "on", "at", "by", "with"}
