from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from .bibtex import BibEntry, parse_bibtex, normalize_for_match


class InspireError(RuntimeError):
    pass


@dataclass(slots=True)
class SearchResult:
    recid: int
    title: str
    authors: list[str]
    year: str

    @property
    def first_author(self) -> str:
        return self.authors[0] if self.authors else ""


class InspireClient:
    def __init__(self, base_url: str = "https://inspirehep.net/api/literature") -> None:
        self.base_url = base_url.rstrip("/")

    def fetch_author_entries(self, query: str) -> list[BibEntry]:
        matches = self._search_records(query)
        normalized_query = normalize_for_match(query)
        filtered = [
            result
            for result in matches
            if any(normalized_query in normalize_for_match(author) for author in result.authors)
        ]
        return [self.fetch_entry(result.recid) for result in filtered]

    def fetch_title_entries(self, query: str) -> list[BibEntry]:
        matches = self.search_title(query, limit=None)
        return [self.fetch_entry(result.recid) for result in matches]

    def search_title(self, query: str, limit: int | None = 20) -> list[SearchResult]:
        normalized_query = normalize_for_match(query)
        filtered = [
            result
            for result in self._search_records(query)
            if normalized_query in normalize_for_match(result.title)
        ]
        return filtered if limit is None else filtered[:limit]

    def search_author(self, query: str, limit: int | None = 20) -> list[SearchResult]:
        normalized_query = normalize_for_match(query)
        filtered = [
            result
            for result in self._search_records(query)
            if any(normalized_query in normalize_for_match(author) for author in result.authors)
        ]
        return filtered if limit is None else filtered[:limit]

    def fetch_entry(self, recid: int) -> BibEntry:
        body = self._request_text(f"{self.base_url}/{recid}?format=bibtex")
        entries = parse_bibtex(body)
        if not entries:
            raise InspireError(f"INSPIRE returned no BibTeX for record {recid}.")
        return entries[0]

    def _search_records(self, query: str) -> list[SearchResult]:
        results: list[SearchResult] = []
        page = 1
        size = 50
        while page <= 10:
            params = urlencode({"q": query, "page": page, "size": size})
            payload = self._request_json(f"{self.base_url}/?{params}")
            hits = payload.get("hits", {}).get("hits", [])
            if not hits:
                break
            results.extend(_search_results_from_hits(hits))
            if len(hits) < size:
                break
            page += 1
        return results

    def _request_json(self, url: str) -> dict[str, Any]:
        try:
            with urlopen(url) as response:
                return json.load(response)
        except (HTTPError, URLError, json.JSONDecodeError) as error:
            raise InspireError(f"Unable to query INSPIRE: {error}") from error

    def _request_text(self, url: str) -> str:
        try:
            with urlopen(url) as response:
                return response.read().decode("utf-8")
        except (HTTPError, URLError, UnicodeDecodeError) as error:
            raise InspireError(f"Unable to fetch BibTeX from INSPIRE: {error}") from error


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
        recid = hit.get("id") or hit.get("metadata", {}).get("control_number")
        if not recid or not title:
            continue
        results.append(SearchResult(recid=int(recid), title=title, authors=authors, year=year))
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
