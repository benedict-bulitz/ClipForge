"""Main-content extraction: the useful part of a page, never the whole page.

Metadata (title, dates, author/organisation, schema.org types, scholarly
citation tags, language) comes from one light stdlib scan of the document.
The main text comes from Scrapling's parser when it is installed (DOM-aware:
main-container choice, boilerplate ancestry), otherwise from the stdlib
fallback below - Scrapling is an improvement, never a single point of
failure.  Navigation, cookie banners, related links, comments, footers and
image credits are dropped before anything reaches downstream reasoning.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from typing import Any

try:  # Optional: the parser-only Scrapling install (lxml + cssselect).
    from scrapling.parser import Selector as _ScraplingSelector
except Exception:  # noqa: BLE001 - any import problem means "use the fallback"
    _ScraplingSelector = None

MAX_PARAGRAPHS = 60
MAX_PARAGRAPH_CHARS = 1_200
MAX_HEADINGS = 24
MAX_TABLES = 3
MAX_TABLE_ROWS = 12
MIN_PARAGRAPH_WORDS = 8

_SKIP_TAGS = {
    "script", "style", "noscript", "nav", "header", "footer", "aside", "form", "button", "svg", "iframe",
    "figcaption", "template", "select", "option", "label", "dialog",
}
_BOILERPLATE = re.compile(
    r"(?i)(?:cookie|consent|gdpr|banner|newsletter|related|recommend|teaser|share|social|comment|subscribe|"
    r"advert|\bads?\b|ad-|promo|sponsor|breadcrumb|sidebar|menu|navbar|nav-|footer|header|masthead|popup|modal|"
    r"paywall|login|signup|toolbar|pagination|tags?-list|author-box|byline-box|caption|credit|copyright|infobox|"
    r"toc\b|mw-editsection|reference|reflist|navbox)"
)
_MAIN_SELECTORS = (
    "article", "main", "[role=main]", "[itemprop=articleBody]", "#content", "#main-content", ".article-body",
    ".post-content", ".entry-content", "#bodyContent",
)
_CHROME_SENTENCE = re.compile(
    r"(?i)(?:cookies?|javascript (?:is )?(?:disabled|required)|aktiviere javascript|enable javascript|"
    r"alle rechte vorbehalten|all rights reserved|zum newsletter|subscribe to|melde dich an|sign up|"
    r"mehr zum thema|read more|weiterlesen|lesen sie auch|related articles|teilen auf|share on|"
    r"foto:|bild:|image:|photo:|©)"
)
_JS_ONLY = re.compile(
    r"(?i)(?:enable javascript|javascript (?:is )?(?:required|disabled)|aktivieren sie javascript|"
    r"you need to enable javascript|this app works best with javascript)"
)
_DATE_META = {
    "published": (
        "article:published_time", "datepublished", "citation_publication_date", "citation_date", "dc.date",
        "dc.date.issued", "dcterms.created", "date", "pubdate", "publishdate", "og:published_time",
    ),
    "updated": ("article:modified_time", "datemodified", "og:updated_time", "dcterms.modified", "last-modified", "lastmod"),
}


@dataclass
class ExtractedPage:
    url: str
    title: str = ""
    site_name: str = ""
    author: str = ""
    published_at: str | None = None
    updated_at: str | None = None
    language: str = ""
    description: str = ""
    headings: list[str] = field(default_factory=list)
    paragraphs: list[str] = field(default_factory=list)
    tables: list[list[str]] = field(default_factory=list)
    schema_types: list[str] = field(default_factory=list)
    og_type: str = ""
    doi: str = ""
    citation_journal: str = ""
    canonical: str = ""
    extractor: str = "builtin"
    js_only: bool = False

    @property
    def word_count(self) -> int:
        return sum(len(paragraph.split()) for paragraph in self.paragraphs)

    def quality_meta(self) -> dict[str, Any]:
        return {
            "schema_types": self.schema_types,
            "og_type": self.og_type,
            "doi": self.doi,
            "citation_journal": self.citation_journal,
            "word_count": self.word_count,
            "retrieved": True,
        }


def _clean(text: object) -> str:
    return re.sub(r"\s+", " ", unescape(str(text or ""))).strip()


def _date(value: object) -> str | None:
    """ISO date (YYYY-MM-DD) from common date strings, or None."""
    text = _clean(value)
    match = re.search(r"((?:19|20)\d{2})-(\d{2})-(\d{2})", text)
    if match:
        return "-".join(match.groups())
    match = re.search(r"\b(\d{1,2})\.(\d{1,2})\.((?:19|20)\d{2})\b", text)
    if match:
        day, month, year = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    match = re.search(r"\b((?:19|20)\d{2})/(\d{2})/(\d{2})\b", text)
    if match:
        return "-".join(match.groups())
    return None


class _MetaScan(HTMLParser):
    """Head metadata, JSON-LD and (for the fallback) main-content text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self.lang = ""
        self.canonical = ""
        self.jsonld: list[str] = []
        self.times: list[str] = []
        self._in_title = False
        self._in_jsonld = False
        self._buffer: list[str] = []
        # Fallback body extraction state.
        self._stack: list[tuple[str, bool, bool]] = []  # (tag, skipped, main)
        self._text_tag: str | None = None
        self._text: list[str] = []
        self.blocks: list[tuple[str, str, bool]] = []  # (kind, text, inside main container)
        self.script_count = 0
        self._row: list[str] | None = None
        self.rows: list[list[str]] = []

    @property
    def _skipped(self) -> bool:
        return bool(self._stack) and self._stack[-1][1]

    @property
    def _main(self) -> bool:
        return bool(self._stack) and self._stack[-1][2]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        if tag == "html":
            self.lang = values.get("lang", "")
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (values.get("property") or values.get("name") or values.get("itemprop") or "").casefold()
            if key and values.get("content") and key not in self.meta:
                self.meta[key] = values["content"]
        elif tag == "link" and "canonical" in values.get("rel", "").casefold():
            self.canonical = values.get("href", "")
        elif tag == "script":
            self.script_count += 1
            if "ld+json" in values.get("type", ""):
                self._in_jsonld = True
                self._buffer = []
        elif tag == "time" and values.get("datetime"):
            self.times.append(values["datetime"])
        if tag in {"meta", "link", "br", "img", "input", "hr", "source", "wbr"}:
            return
        marker = " ".join((values.get("class", ""), values.get("id", ""), values.get("role", "")))
        skipped = self._skipped or tag in _SKIP_TAGS or bool(_BOILERPLATE.search(marker)) and tag not in {"html", "body", "main", "article"}
        main = self._main or tag in {"article", "main"} or values.get("role") == "main" or values.get("itemprop") == "articleBody"
        self._stack.append((tag, skipped, main))
        if self._text_tag is not None and tag in {"p", "li", "h1", "h2", "h3"} and self._text_tag in {"p", "li"}:
            self._flush()  # an unclosed <p>/<li> ends where the next block starts
        if not skipped and tag in {"p", "li", "h1", "h2", "h3", "dd", "blockquote", "td", "th"} and self._text_tag is None:
            self._text_tag = tag
            self._text = []
        if not skipped and tag == "tr":
            self._row = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag == "script" and self._in_jsonld:
            self._in_jsonld = False
            self.jsonld.append("".join(self._buffer))
        if self._text_tag == tag:
            self._flush()
        if tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
        # Pop to the matching tag (tolerates unclosed children).
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                break

    def _flush(self) -> None:
        tag = self._text_tag
        text = _clean("".join(self._text))
        kind = "heading" if tag in {"h1", "h2", "h3"} else ("cell" if tag in {"td", "th"} else "paragraph")
        if text:
            if kind == "cell" and self._row is not None:
                self._row.append(text)
            elif kind != "cell":
                self.blocks.append((kind, text, self._main))
        self._text_tag = None
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._in_jsonld:
            self._buffer.append(data)
        if self._text_tag is not None and not self._skipped:
            self._text.append(data)


def _jsonld_items(chunks: list[str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for chunk in chunks[:8]:
        try:
            data = json.loads(chunk)
        except (ValueError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack and len(items) < 40:
            item = stack.pop(0)
            if isinstance(item, dict):
                items.append(item)
                graph = item.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)
    return items


def _name(value: object) -> str:
    if isinstance(value, dict):
        return _clean(value.get("name"))
    if isinstance(value, list) and value:
        return _name(value[0])
    return _clean(value) if isinstance(value, str) else ""


def _metadata(scan: _MetaScan, page: ExtractedPage) -> None:
    meta = scan.meta
    items = _jsonld_items(scan.jsonld)
    types: list[str] = []
    for item in items:
        kind = item.get("@type")
        for value in kind if isinstance(kind, list) else [kind]:
            if isinstance(value, str) and value not in types:
                types.append(value)
    article = next((item for item in items if item.get("datePublished") or item.get("headline")), {})
    page.schema_types = types[:12]
    page.title = _clean(meta.get("og:title") or meta.get("citation_title") or article.get("headline") or scan.title)[:300]
    page.site_name = _clean(meta.get("og:site_name") or _name(article.get("publisher")) or meta.get("citation_publisher"))[:120]
    page.author = _clean(meta.get("author") or meta.get("citation_author") or _name(article.get("author")))[:120]
    page.description = _clean(meta.get("description") or meta.get("og:description"))[:400]
    page.og_type = _clean(meta.get("og:type")).casefold()
    doi = _clean(meta.get("citation_doi") or meta.get("dc.identifier"))
    page.doi = doi[:120] if re.search(r"\b10\.\d{4,}/", doi) else ""
    page.citation_journal = _clean(meta.get("citation_journal_title"))[:160]
    page.language = _clean(scan.lang)[:12]
    page.canonical = _clean(scan.canonical)[:500]
    published = article.get("datePublished") or next((meta[key] for key in _DATE_META["published"] if meta.get(key)), None)
    updated = article.get("dateModified") or next((meta[key] for key in _DATE_META["updated"] if meta.get(key)), None)
    page.published_at = _date(published) or (_date(scan.times[0]) if scan.times else None)
    page.updated_at = _date(updated)


def _keep_paragraph(text: str) -> bool:
    return len(text.split()) >= MIN_PARAGRAPH_WORDS and not _CHROME_SENTENCE.search(text[:160])


def _scrapling_body(html: str, url: str) -> tuple[list[str], list[str]] | None:
    """(headings, paragraphs) from the best main container, or None."""
    if _ScraplingSelector is None:
        return None
    try:
        root = _ScraplingSelector(html, url=url)
        containers = [element for selector in _MAIN_SELECTORS for element in root.css(selector)]
        if not containers:
            containers = list(root.css("body"))

        def boilerplate(element: Any) -> bool:
            for ancestor in [element, *element.iterancestors()]:
                tag = str(ancestor.tag or "").casefold()
                if tag in {"main", "article", "body", "html"}:
                    return False
                marker = " ".join(str(ancestor.attrib.get(key) or "") for key in ("class", "id", "role"))
                if tag in _SKIP_TAGS or _BOILERPLATE.search(marker):
                    return True
            return False

        best: tuple[int, list[str], list[str]] = (0, [], [])
        for container in containers[:12]:
            headings = [
                _clean(element.get_all_text(separator=" "))
                for element in container.css("h1, h2, h3")
                if not boilerplate(element)
            ]
            paragraphs = [
                _clean(element.get_all_text(separator=" "))
                for element in container.css("p, li, dd, blockquote")
                if not boilerplate(element)
            ]
            paragraphs = [text for text in paragraphs if _keep_paragraph(text)]
            score = sum(len(text) for text in paragraphs)
            if score > best[0]:
                best = (score, headings, paragraphs)
        return best[1], best[2]
    except Exception:  # noqa: BLE001 - malformed markup: the stdlib fallback takes over
        return None


def _dedupe(values: list[str], limit: int, max_chars: int) -> list[str]:
    seen: set[str] = set()
    kept: list[str] = []
    for value in values:
        key = value.casefold()[:200]
        if not value or key in seen:
            continue
        seen.add(key)
        kept.append(value[:max_chars])
        if len(kept) >= limit:
            break
    return kept


def extract_page(html: str, url: str) -> ExtractedPage:
    """Extract one HTML page (never raises for malformed markup)."""
    page = ExtractedPage(url=url)
    scan = _MetaScan()
    try:
        scan.feed(html)
        scan.close()
    except Exception:  # noqa: BLE001 - partial scans are still useful
        pass
    _metadata(scan, page)
    body = _scrapling_body(html, url)
    if body is not None and body[1]:
        headings, paragraphs = body
        page.extractor = "scrapling"
    else:
        in_main = any(main for _kind, _text, main in scan.blocks)
        blocks = [(kind, text) for kind, text, main in scan.blocks if main or not in_main]
        headings = [text for kind, text in blocks if kind == "heading"]
        paragraphs = [text for kind, text in blocks if kind == "paragraph" and _keep_paragraph(text)]
    page.headings = _dedupe(headings, MAX_HEADINGS, 200)
    page.paragraphs = _dedupe(paragraphs, MAX_PARAGRAPHS, MAX_PARAGRAPH_CHARS)
    rows = [row for row in scan.rows if 2 <= len(row) <= 8]
    if rows:
        page.tables = [[" | ".join(cell[:80] for cell in row) for row in rows[:MAX_TABLE_ROWS]]][:MAX_TABLES]
    if not page.title and page.headings:
        page.title = page.headings[0]
    visible_words = page.word_count
    page.js_only = visible_words < 60 and (bool(_JS_ONLY.search(html[:200_000])) or (scan.script_count >= 5 and visible_words < 20))
    return page


def extract_plaintext(text: str, url: str, *, title: str = "", language: str = "", site_name: str = "") -> ExtractedPage:
    """A plain-text source (e.g. an encyclopedia API extract) as an ExtractedPage.

    ``== Heading ==`` lines become headings; reference/literature sections end the text.
    """
    page = ExtractedPage(url=url, title=title, language=language, site_name=site_name, extractor="plaintext")
    paragraphs: list[str] = []
    for raw in str(text or "").splitlines():
        line = _clean(raw)
        heading = re.fullmatch(r"=+\s*(.+?)\s*=+", line)
        if heading:
            name = heading.group(1)
            if re.search(r"(?i)^(?:literatur|weblinks|einzelnachweise|anmerkungen|siehe auch|references|further reading|"
                         r"external links|see also|notes|bibliography|quellen)$", name):
                break
            page.headings.append(name)
            continue
        if _keep_paragraph(line):
            paragraphs.append(line)
    page.headings = _dedupe(page.headings, MAX_HEADINGS, 200)
    page.paragraphs = _dedupe(paragraphs, MAX_PARAGRAPHS, MAX_PARAGRAPH_CHARS)
    return page


def scrapling_available() -> bool:
    return _ScraplingSelector is not None
