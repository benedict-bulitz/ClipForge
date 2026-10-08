from __future__ import annotations

"""Transparent source classification: what kind of source is this?

There is no opaque trust score.  A source gets one ``source_type`` from a
small fixed vocabulary, an ``authority`` tier derived from that type, and the
``reasons`` that led there (host shape, page metadata, content signals).
No individual website is a universal winner: hosts are recognised only by
generic shape (``.gov``, ``.edu``, ``uni-``, ``museum``), never by name, with
two documented exceptions - Wikipedia (the existing free research provider)
and a short list of user-generated platforms that must never be mistaken for
authority.
"""

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

SOURCE_TYPES = (
    "primary_research",
    "government",
    "academic",
    "institutional",
    "first_party",
    "reference",
    "journalism",
    "specialist_secondary",
    "generic_secondary",
    "user_generated",
    "low_quality",
    "unknown",
)
AUTHORITY = {
    "primary_research": "high",
    "government": "high",
    "academic": "high",
    "institutional": "high",
    "first_party": "high",
    "reference": "medium",
    "journalism": "medium",
    "specialist_secondary": "medium",
    "generic_secondary": "low",
    "user_generated": "low",
    "low_quality": "low",
    "unknown": "unknown",
}
TIER_RANK = {"high": 0, "medium": 1, "unknown": 2, "low": 3}

# Two-label public suffixes common in ClipForge's languages (registrable-domain heuristic).
_SECOND_LEVEL = {
    "co.uk", "ac.uk", "gov.uk", "org.uk", "nhs.uk", "com.au", "edu.au", "gov.au", "org.au", "ac.at", "gv.at",
    "or.at", "co.at", "ac.jp", "co.jp", "go.jp", "ac.nz", "govt.nz", "co.nz", "gc.ca", "edu.cn", "gov.cn",
    "ac.in", "gov.in", "edu.tr", "gov.br", "edu.br", "com.br", "ac.za", "gov.za", "co.za", "admin.ch",
}
_GOVERNMENT_SUFFIX = (
    ".gov", ".mil", ".int", ".europa.eu", ".bund.de", ".gv.at", ".admin.ch", ".gouv.fr", ".gob.es", ".gc.ca",
    ".govt.nz", ".gov.uk", ".gov.au", ".go.jp", ".gov.in", ".gov.br", ".gov.za", ".nhs.uk",
)
_ACADEMIC_HOST = re.compile(
    r"(?:^|\.)(?:uni-[\w-]+|[\w-]*universit[\w-]*|[\w-]*hochschule[\w-]*|tu-[\w-]+|[\w-]*college[\w-]*)\.|"
    r"\.edu(?:\.[a-z]{2})?$|\.ac\.[a-z]{2}$"
)
_INSTITUTION_HOST = re.compile(
    r"(?i)(?:museum|archiv|archive|bibliothek|library|akademie|academy|institut|observator|sternwarte|"
    r"planetarium|gedenkst|stiftung|foundation|society)"
)
_REFERENCE_HOST = re.compile(r"(?i)(?:^|\.)wikipedia\.org$|pedia\b|pedia\.|lexikon|encyclop")
_USER_GENERATED_HOST = re.compile(
    r"(?i)(?:^|\.)(?:reddit|quora|gutefrage|wer-weiss-was|stackexchange|stackoverflow|answers|forum|forums|"
    r"community|youtube|tiktok|facebook|instagram|pinterest|twitter|x|medium|blogspot|wordpress|tumblr)\.|"
    r"(?:^|\.)forum[\w-]*\."
)
_JOURNALISM_HOST = re.compile(r"(?i)(?:news|nachrichten|zeitung|presse|tagesschau|rundschau|kurier|journal)")
_SPECIALIST_HOST = re.compile(
    r"(?i)(?:scien|wissen|spektrum|physik|physics|chemi|astro|space|weltraum|geschicht|histor|biolog|"
    r"medizin|medic|klima|climate|geo|tech|technik)"
)
# German federal bodies ("bundesarchiv.de", "bundesregierung.de"), not leagues or shops.
_GOVERNMENT_LABEL = re.compile(r"(?i)(?:^|\.)(?:bundes(?![\w-]*liga)[\w-]*|bmbf|bmuv)\.de\.")
# Content signals of search-engine-optimised filler (title / text).
_LISTICLE = re.compile(
    r"(?i)^\s*(?:top\s*\d+|\d+\s+(?:gründe|dinge|fakten|tipps|tricks|reasons|things|facts|tips|ways)\b)|"
    r"you won'?t believe|du wirst nicht glauben|unglaublich!|schockierend|shocking|krass\b"
)
_AFFILIATE = re.compile(
    r"(?i)\b(?:affiliate|provisionslink|\*?werbung\b|anzeige\b|sponsored|gesponsert|partnerlink|"
    r"jetzt kaufen|buy now|bestseller|preisvergleich|testsieger|vergleichssieger)"
)
_FARM = re.compile(
    r"(?i)(?:in diesem (?:artikel|beitrag|ratgeber) (?:erfährst|erfahren|zeigen)|in this (?:article|post|guide),? "
    r"(?:we|you)|das solltest du wissen|everything you need to know|alles,? was du wissen musst|ratgeber\b)"
)
_SCHEMA = {
    "government": {"governmentorganization", "governmentoffice", "governmentservice"},
    "academic": {"collegeoruniversity", "educationalorganization", "scholarlyarticle"},
    "institutional": {"museum", "archiveorganization", "library", "researchorganization", "researchproject"},
    "journalism": {"newsarticle", "reportagenewsarticle", "analysisnewsarticle", "newsmediaorganization"},
}


@dataclass(frozen=True)
class SourceClass:
    source_type: str
    authority: str
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        return {"source_type": self.source_type, "authority": self.authority, "reasons": list(self.reasons)}


def host_of(url: str) -> str:
    host = (urlsplit(str(url or "")).hostname or "").casefold()
    return host.removeprefix("www.")


def registrable_domain(url_or_host: str) -> str:
    """``news.example.co.uk`` -> ``example.co.uk`` (small public-suffix heuristic)."""
    host = host_of(url_or_host) if "/" in str(url_or_host) else str(url_or_host or "").casefold()
    labels = [label for label in host.split(".") if label]
    if len(labels) <= 2:
        return ".".join(labels)
    if ".".join(labels[-2:]) in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _entity_tokens(question_terms: set[str] | frozenset[str] | None) -> set[str]:
    return {term.casefold() for term in question_terms or () if len(term) >= 4}


def classify_source(
    url: str,
    *,
    title: str = "",
    text: str = "",
    meta: dict[str, object] | None = None,
    question_terms: set[str] | frozenset[str] | None = None,
    first_party_allowed: bool = False,
) -> SourceClass:
    """Classify one source by generic host shape and page metadata.

    ``meta`` (from extraction) may carry ``schema_types``, ``og_type``,
    ``doi``, ``citation_journal`` and ``word_count``.  Without a retrieved
    page only the host is known; an unrecognised host stays ``unknown``.
    """
    meta = meta or {}
    host = host_of(url)
    domain = registrable_domain(host)
    path = urlsplit(str(url or "")).path.casefold()
    schema = {str(item).casefold() for item in meta.get("schema_types") or ()}
    reasons: list[str] = []
    word_count = int(meta.get("word_count") or 0)
    retrieved = bool(meta.get("retrieved"))

    def result(kind: str, *why: str) -> SourceClass:
        return SourceClass(kind, AUTHORITY[kind], tuple([*why, *reasons]))

    if meta.get("doi") or meta.get("citation_journal") or host in {"doi.org", "dx.doi.org"} or "/doi/" in path:
        return result("primary_research", "scholarly citation metadata (DOI / journal)")
    if host.endswith(_GOVERNMENT_SUFFIX) or _GOVERNMENT_LABEL.search("." + host + ".") or schema & _SCHEMA["government"]:
        return result("government", "government host or organisation")
    # The asked product's / organisation's own domain ("tiktok.com" for a
    # TikTok question).  Only where routing expects first-party sources: the
    # same word as a planet or food would otherwise make a brand authoritative.
    label = domain.split(".")[0]
    if first_party_allowed and label in _entity_tokens(question_terms):
        return result("first_party", f"domain belongs to the asked subject ({label})")
    if _ACADEMIC_HOST.search(host + ".") or _ACADEMIC_HOST.search(host) or schema & _SCHEMA["academic"]:
        return result("academic", "university / academic host")
    if host.endswith(".museum") or schema & _SCHEMA["institutional"] or (
        _INSTITUTION_HOST.search(domain) and not _USER_GENERATED_HOST.search(host)
    ):
        return result("institutional", "museum / archive / institute host or organisation")
    if _USER_GENERATED_HOST.search(host):
        return result("user_generated", "user-generated platform")
    if _REFERENCE_HOST.search(host):
        return result("reference", "reference work")
    low: list[str] = []
    if _LISTICLE.search(title or ""):
        low.append("listicle / clickbait title")
    if _AFFILIATE.search(text[:4000]) or _AFFILIATE.search(title or ""):
        low.append("affiliate / advertising markers")
    if _FARM.search(text[:2000]):
        low.append("content-farm phrasing")
    if retrieved and 0 < word_count < 120:
        low.append(f"thin main content ({word_count} words)")
    if len(low) >= 2:
        return result("low_quality", *low)
    reasons.extend(f"caution: {item}" for item in low)
    if schema & _SCHEMA["journalism"] or _JOURNALISM_HOST.search(domain):
        return result("journalism", "news organisation / news article")
    if _SPECIALIST_HOST.search(domain):
        return result("specialist_secondary", "specialist publication host")
    if retrieved:
        return result("generic_secondary", "retrieved page without authority signals")
    return result("unknown", "host not recognised; page not retrieved")
