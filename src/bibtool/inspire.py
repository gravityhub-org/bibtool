from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import re
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .bibtex import (
    BibEntry,
    best_publication_info,
    enrich_entry_from_metadata,
    entry_needs_metadata_enrichment,
    first_author_name,
    merge_entry_fields,
    normalize_inspire_entry,
    parse_bibtex,
    normalize_for_match,
    strip_outer_wrappers,
)


class InspireError(RuntimeError):
    pass


_JSON_CACHE: dict[str, Any] = {}
_TEXT_CACHE: dict[str, str] = {}


def clear_response_caches() -> None:
    _JSON_CACHE.clear()
    _TEXT_CACHE.clear()


@dataclass(slots=True)
class SearchResult:
    recid: int
    title: str
    authors: list[str]
    year: str
    abstract: str = ""
    has_journal_publication: bool = False
    arxiv_id: str = ""

    @property
    def first_author(self) -> str:
        return self.authors[0] if self.authors else ""


@dataclass(frozen=True, slots=True)
class LookupSpec:
    query: str
    result_matcher: Callable[[SearchResult], bool]
    entry_matcher: Callable[[BibEntry], bool]


class InspireClient:
    def __init__(self, base_url: str = "https://inspirehep.net/api/literature", timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._recid_cache: dict[str, int] = {}

    def clear_lookup_cache(self) -> None:
        self._recid_cache.clear()

    def lookup(
        self,
        *,
        query: str | None = None,
        name: str | None = None,
        title: str | None = None,
        limit: int | None = 20,
        as_entries: bool = False,
    ) -> list[SearchResult] | list[BibEntry]:
        spec = self.resolve_lookup(query=query, name=name, title=title)
        results = self._search_records(spec.query, matcher=spec.result_matcher, limit=limit)
        if not as_entries:
            return results
        return [self.fetch_entry(result.recid) for result in results]

    def resolve_lookup(
        self,
        *,
        query: str | None = None,
        name: str | None = None,
        title: str | None = None,
    ) -> LookupSpec:
        if query is not None:
            words = _normalized_words(query)
            return LookupSpec(
                query=self._keyword_query(query),
                result_matcher=lambda result: _result_contains_all_words(result, words),
                entry_matcher=lambda entry: _entry_contains_all_words(
                    entry,
                    words,
                    include_author=True,
                    include_title=True,
                ),
            )
        if name is not None and title is not None:
            name_words = _normalized_words(name)
            title_words = _normalized_words(title)
            return LookupSpec(
                query=self._name_title_query(name, title),
                result_matcher=lambda result: _matches_name_and_title(result, name_words, title_words),
                entry_matcher=lambda entry: _entry_matches_name_and_title(entry, name_words, title_words),
            )
        if name is not None:
            words = _normalized_words(name)
            return LookupSpec(
                query=self._author_query(name),
                result_matcher=lambda result: any(_text_contains_all_words(author, words) for author in result.authors),
                entry_matcher=lambda entry: _text_contains_all_words(entry.author, words),
            )
        if title is not None:
            words = _normalized_words(title)
            return LookupSpec(
                query=self._title_wildcard_query(title),
                result_matcher=lambda result: _text_contains_all_words(result.title, words),
                entry_matcher=lambda entry: _text_contains_all_words(entry.title, words),
            )
        raise InspireError("Search query cannot be empty.")

    def fetch_query_entries(self, query: str, limit: int | None = None) -> list[BibEntry]:
        return self.lookup(query=query, limit=limit, as_entries=True)

    def fetch_author_entries(self, query: str, limit: int | None = None) -> list[BibEntry]:
        return self.lookup(name=query, limit=limit, as_entries=True)

    def fetch_title_entries(self, query: str, limit: int | None = None) -> list[BibEntry]:
        return self.lookup(title=query, limit=limit, as_entries=True)

    def fetch_name_and_title_entries(self, name: str, title: str, limit: int | None = None) -> list[BibEntry]:
        return self.lookup(name=name, title=title, limit=limit, as_entries=True)

    def search(self, query: str, limit: int | None = 20) -> list[SearchResult]:
        return self.lookup(query=query, limit=limit, as_entries=False)

    def search_title(self, query: str, limit: int | None = 20) -> list[SearchResult]:
        return self.lookup(title=query, limit=limit, as_entries=False)

    def search_author(self, query: str, limit: int | None = 20) -> list[SearchResult]:
        return self.lookup(name=query, limit=limit, as_entries=False)

    def search_name_and_title(self, name: str, title: str, limit: int | None = 20) -> list[SearchResult]:
        return self.lookup(name=name, title=title, limit=limit, as_entries=False)

    def fetch_entry(self, recid: int) -> BibEntry:
        body = self._request_text(f"{self.base_url}/{recid}?format=bibtex")
        entries = parse_bibtex(body)
        if not entries:
            raise InspireError(f"INSPIRE returned no BibTeX for record {recid}.")
        entry = entries[0]
        if entry_needs_metadata_enrichment(entry):
            payload = self._request_json(f"{self.base_url}/{recid}?format=json")
            metadata = payload.get("metadata")
            if isinstance(metadata, dict):
                entry = enrich_entry_from_metadata(entry, metadata)
        return normalize_inspire_entry(entry)

    def fetch_result_by_recid(self, recid: int) -> SearchResult:
        payload = self._request_json(f"{self.base_url}/{recid}?format=json")
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            raise InspireError(f"INSPIRE returned no metadata for record {recid}.")
        results = _search_results_from_hits([{"id": recid, "metadata": metadata}])
        if not results:
            raise InspireError(f"INSPIRE returned an unusable record {recid}.")
        return results[0]

    def fetch_result_by_arxiv(self, arxiv_id: str) -> SearchResult:
        cleaned = normalize_arxiv_id(arxiv_id)
        if not cleaned:
            raise InspireError("arXiv id cannot be empty.")
        results = self._search_records(f"eprint:{cleaned}", matcher=lambda _result: True, limit=5)
        if not results:
            # Still allow downloads when INSPIRE has no hit; arXiv id is enough for the PDF URL.
            return SearchResult(recid=0, title=cleaned, authors=[], year="", arxiv_id=cleaned)
        preferred = _pick_preferred_recid(results)
        chosen = next((result for result in results if result.recid == preferred), results[0])
        return SearchResult(
            recid=chosen.recid,
            title=chosen.title,
            authors=list(chosen.authors),
            year=chosen.year,
            abstract=chosen.abstract,
            has_journal_publication=chosen.has_journal_publication,
            arxiv_id=chosen.arxiv_id or cleaned,
        )

    def fetch_pdf(self, arxiv_id: str) -> bytes:
        """Download a PDF from arXiv using an eprint id from INSPIRE metadata.

        Uses the static arXiv PDF URL, not the arXiv API.
        """
        cleaned = normalize_arxiv_id(arxiv_id)
        if not cleaned:
            raise InspireError("arXiv id cannot be empty.")
        url = f"https://arxiv.org/pdf/{cleaned}"
        return self._request_bytes(url)

    def lookup_recid(self, entry: BibEntry) -> int | None:
        eprint = strip_outer_wrappers(entry.fields.get("eprint", ""))
        if eprint:
            cache_key = f"eprint:{eprint}"
            if cache_key in self._recid_cache:
                return self._recid_cache[cache_key]
            recid = self._lookup_recid_from_query(f"eprint:{eprint}", limit=5)
            if recid is not None:
                self._recid_cache[cache_key] = recid
                return recid

        doi = _normalize_doi(entry.fields.get("doi", ""))
        if doi:
            cache_key = f"doi:{doi}"
            if cache_key in self._recid_cache:
                return self._recid_cache[cache_key]
            recid = self._lookup_recid_from_query(f"doi:{doi}", limit=5)
            if recid is not None:
                self._recid_cache[cache_key] = recid
                return recid

        author = first_author_name(entry.author)
        title = strip_outer_wrappers(entry.title)
        if author and title:
            cache_key = f"name-title:{normalize_for_match(author)}:{normalize_for_match(title)}"
            if cache_key in self._recid_cache:
                return self._recid_cache[cache_key]
            spec = self.resolve_lookup(name=author, title=_title_lookup_terms(title))
            results = self._search_records(spec.query, matcher=spec.result_matcher, limit=10)
            recid = _pick_preferred_recid(results)
            if recid is not None:
                self._recid_cache[cache_key] = recid
            return recid
        if title:
            cache_key = f"title:{normalize_for_match(title)}"
            if cache_key in self._recid_cache:
                return self._recid_cache[cache_key]
            spec = self.resolve_lookup(title=_title_lookup_terms(title))
            results = self._search_records(spec.query, matcher=spec.result_matcher, limit=10)
            recid = _pick_preferred_recid(results)
            if recid is not None:
                self._recid_cache[cache_key] = recid
            return recid
        return None

    def _lookup_recid_from_query(self, query: str, *, limit: int = 5) -> int | None:
        results = self._search_records(query, matcher=lambda _result: True, limit=limit)
        return _pick_preferred_recid(results)

    def refresh_entries(self, entries: list[BibEntry], *, workers: int = 8) -> list[BibEntry | None]:
        if not entries:
            return []
        if workers <= 1:
            return [self.refresh_entry(entry) for entry in entries]

        refreshed: list[BibEntry | None] = [None] * len(entries)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(self.refresh_entry, entry): index
                for index, entry in enumerate(entries)
            }
            for future in futures:
                index = futures[future]
                refreshed[index] = future.result()
        return refreshed

    def refresh_entry(self, entry: BibEntry) -> BibEntry | None:
        recid = self.lookup_recid(entry)
        if recid is None:
            return None
        fresh = self.fetch_entry(recid)
        refreshed = merge_entry_fields(entry, fresh)
        refreshed.key = entry.key
        refreshed.entry_type = entry.entry_type
        return refreshed

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
                    "fields": "titles,authors,abstracts,imprints,preprint_date,publication_info,dois,arxiv_eprints",
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
        parts: list[str] = []
        for token in searchable:
            if _is_year_token(token):
                parts.append(f"date:{token}")
                continue
            escaped = _escape_query_token(token)
            parts.append(f'(title:"{escaped}" or author:"{escaped}")')
        return " and ".join(parts)

    def _author_query(self, query: str) -> str:
        tokens = _query_tokens(query)
        if not tokens:
            raise InspireError("Search query cannot be empty.")
        return " and ".join(f'author:"{_escape_query_token(token)}"' for token in tokens)

    def _title_wildcard_query(self, query: str) -> str:
        tokens = _query_tokens(query)
        if not tokens:
            raise InspireError("Search query cannot be empty.")
        return " and ".join(f"title:{_escape_query_token(token)}*" for token in tokens)

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

    def _request_bytes(self, url: str) -> bytes:
        request = Request(url, headers={"User-Agent": "bibtool/0.1"})
        try:
            with urlopen(request, timeout=max(self.timeout, 30.0)) as response:
                data = response.read()
        except (HTTPError, URLError) as error:
            raise InspireError(f"Unable to download PDF: {error}") from error
        if not data.startswith(b"%PDF"):
            raise InspireError(f"Download from {url} did not return a PDF.")
        return data


def _normalize_doi(value: str) -> str:
    doi = strip_outer_wrappers(value)
    return re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)


def _title_lookup_terms(title: str) -> str:
    words = [word for word in normalize_for_match(title).split() if word and word not in _STOP_WORDS]
    if not words:
        words = [word for word in normalize_for_match(title).split() if word]
    return " ".join(words[:4])


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
        arxiv_id = _arxiv_id_from_metadata(metadata)
        recid = hit.get("id") or hit.get("metadata", {}).get("control_number")
        if not recid or not title:
            continue
        results.append(
            SearchResult(
                recid=int(recid),
                title=title,
                authors=authors,
                year=year,
                abstract=abstract,
                has_journal_publication=best_publication_info(metadata) is not None,
                arxiv_id=arxiv_id,
            )
        )
    return results


def _arxiv_id_from_metadata(metadata: dict[str, Any]) -> str:
    for item in metadata.get("arxiv_eprints", []):
        value = item.get("value")
        if value:
            return str(value)
    return ""


_ARXIV_NEW_RE = re.compile(r"(?i)^(?:arXiv:)?(\d{4}\.\d{4,5})(?:v\d+)?$")
_ARXIV_OLD_RE = re.compile(r"(?i)^(?:arXiv:)?([a-z-]+/\d{7})(?:v\d+)?$")
_ARXIV_URL_RE = re.compile(
    r"(?i)^https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/([^?\s#]+?)(?:\.pdf)?(?:v\d+)?$"
)
_INSPIRE_URL_RE = re.compile(r"(?i)^https?://inspirehep\.net/(?:api/)?literature/(\d+)/?$")
_INSPIRE_PREFIX_RE = re.compile(r"(?i)^(?:inspire:|recid:)?(\d{5,})$")


def normalize_arxiv_id(value: str) -> str:
    token = value.strip()
    if not token:
        return ""
    url_match = _ARXIV_URL_RE.match(token)
    if url_match:
        token = url_match.group(1)
    token = re.sub(r"(?i)^arXiv:", "", token).strip()
    token = re.sub(r"(?i)\.pdf$", "", token).strip()
    token = re.sub(r"v\d+$", "", token).strip()
    new_match = _ARXIV_NEW_RE.match(token) or _ARXIV_NEW_RE.match(f"arXiv:{token}")
    if new_match:
        return new_match.group(1)
    old_match = _ARXIV_OLD_RE.match(token) or _ARXIV_OLD_RE.match(f"arXiv:{token}")
    if old_match:
        return old_match.group(1)
    return token


def parse_literature_id(token: str) -> tuple[str, str] | None:
    """Parse a literature identifier.

    Returns ``("arxiv", arxiv_id)`` or ``("inspire", recid)``, or ``None`` if the
    token does not look like an id.
    """
    raw = token.strip()
    if not raw:
        return None

    inspire_url = _INSPIRE_URL_RE.match(raw)
    if inspire_url:
        return ("inspire", inspire_url.group(1))

    arxiv_url = _ARXIV_URL_RE.match(raw)
    if arxiv_url:
        return ("arxiv", normalize_arxiv_id(raw))

    new_match = _ARXIV_NEW_RE.match(raw)
    if new_match:
        return ("arxiv", new_match.group(1))

    old_match = _ARXIV_OLD_RE.match(raw)
    if old_match:
        return ("arxiv", old_match.group(1))

    inspire_match = _INSPIRE_PREFIX_RE.match(raw)
    if inspire_match:
        return ("inspire", inspire_match.group(1))

    return None


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


def _matches_name_and_title(result: SearchResult, name_words: list[str], title_words: list[str]) -> bool:
    return any(_text_contains_all_words(author, name_words) for author in result.authors) and _text_contains_all_words(
        result.title,
        title_words,
    )


def _entry_matches_name_and_title(entry: BibEntry, name_words: list[str], title_words: list[str]) -> bool:
    return _text_contains_all_words(entry.author, name_words) and _text_contains_all_words(entry.title, title_words)


def _result_contains_all_words(result: SearchResult, required_words: list[str]) -> bool:
    if not required_words:
        return True
    year_words = [word for word in required_words if _is_year_token(word)]
    text_words = [word for word in required_words if not _is_year_token(word)]
    if year_words and not _year_matches(result.year, year_words):
        return False
    if not text_words:
        return True
    haystack = normalize_for_match(" ".join([result.title, result.abstract, *result.authors]))
    return all(word in haystack for word in text_words)


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
    year_words = [word for word in required_words if _is_year_token(word)]
    text_words = [word for word in required_words if not _is_year_token(word)]
    if year_words and not _year_matches(entry.year, year_words):
        return False
    if not text_words:
        return True
    fields: list[str] = []
    if include_title:
        fields.append(entry.title)
    if include_author:
        fields.append(entry.author)
    haystack = normalize_for_match(" ".join(fields))
    return all(word in haystack for word in text_words)


def _is_year_token(token: str) -> bool:
    return bool(_YEAR_TOKEN_RE.fullmatch(token.strip()))


def _year_matches(year: str, year_words: list[str]) -> bool:
    haystack = normalize_for_match(year)
    if not haystack:
        return False
    return all(word in haystack for word in year_words)


def _pick_preferred_recid(results: list[SearchResult]) -> int | None:
    if not results:
        return None
    for result in results:
        if result.has_journal_publication:
            return result.recid
    return results[0].recid


def _escape_query_token(token: str) -> str:
    return token.replace('"', '\\"')


_STOP_WORDS = {"a", "an", "the", "of", "for", "to", "and", "or", "in", "on", "at", "by", "with"}
_YEAR_TOKEN_RE = re.compile(r"(?:19|20)\d{2}")
