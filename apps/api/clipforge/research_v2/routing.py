from __future__ import annotations

"""Source routing: which kinds of sources should answer this kind of question?

A small generic suitability layer.  The question is assigned one research
domain from grammar/vocabulary cues (no topic lists of answers), and the
domain orders the *source types* of ``quality.py`` - never websites.  The
order is a preference among relevant sources: relevance is decided first,
elsewhere, so an authoritative but irrelevant page never wins.
"""

import re
from dataclasses import dataclass

_CUES: dict[str, re.Pattern[str]] = {
    "current_events": re.compile(
        r"(?i)\b(?:heute|aktuell\w*|gerade|derzeit|momentan|neueste\w*|jüngste\w*|diese[nm]? (?:woche|monat|jahr)|"
        r"today|currently|latest|recent(?:ly)?|this (?:week|month|year)|news|nachrichten|wahl\w*|election\w*|"
        r"streik\w*|strike\w*|preis(?:e|anstieg)?\b|prices?\b|inflation|20[2-3]\d)\b"
    ),
    "health": re.compile(
        r"(?i)\b(?:gesund\w*|krank\w*|körper\w*|schlaf\w*|müde\w*|muskel\w*|herz\w*|blut\w*|gehirn\w*|haut\b|"
        r"zähne?|magen\w*|darm\w*|ernährung|satt\w*|hunger\w*|virus|viren|bakterie\w*|immun\w*|"
        r"medizin\w*|arzt|symptom\w*|schmerz\w*|hormon\w*|health\w*|body|sleep\w*|tired|muscle\w*|heart|"
        r"blood|brain|skin|teeth|stomach|gut\b|diet|disease\w*|immune|medic\w*|pain|hormone\w*|vitamin\w*)\b"
    ),
    "history": re.compile(
        r"(?i)\b(?:geschicht\w*|historisch\w*|jahrhundert\w*|mittelalter\w*|antike|krieg(?:e|s)?\b|weltkrieg\w*|"
        r"mauer\b|kaiser\w*|könig\w*|reich\b|ddr|brd|revolution\w*|gebaut wurde|erfunden|wurde\b.*\b(?:gebaut|"
        r"gegründet|erfunden|eingeführt)|history|historic\w*|century|medieval|ancient|empire|king|queen|"
        r"world war|revolution|was (?:built|invented|founded)|why did)\b"
    ),
    "science": re.compile(
        r"(?i)\b(?:planet\w*|mars|mond\w*|sonne\w*|stern\w*|galaxi\w*|universum|weltall|weltraum|atmosphär\w*|"
        r"licht\w*|farbe\w*|himmel\w*|physik\w*|chemi\w*|atom\w*|molekül\w*|energie\w*|wärme|temperatur\w*|"
        r"elektr\w*|magnet\w*|welle\w*|strahlung|evolution|tier\w*|pflanze\w*|wetter\w*|klima\w*|vulkan\w*|"
        r"erdbeben\w*|ozean\w*|wasser\w*|eis\b|planet|moon|sun|star\w*|galax\w*|universe|space|atmosphere|"
        r"light|colou?r\w*|sky|physic\w*|chemi\w*|atom\w*|molecule\w*|energy|heat|temperature|electr\w*|"
        r"magnet\w*|wave\w*|radiation|animal\w*|plant\w*|weather|climate|volcano\w*|earthquake\w*|ocean\w*|water|ice)\b"
    ),
    "technology": re.compile(
        r"(?i)\b(?:app\w*|handy\w*|smartphone\w*|computer\w*|internet|software|browser\w*|algorithm\w*|"
        r"ki\b|künstliche intelligenz|wlan|wifi|bluetooth|akku\w*|batterie\w*|chip\w*|prozessor\w*|"
        r"mikrowelle\w*|microwave\w*|laptop\w*|bildschirm\w*|display\w*|kamera\w*|camera\w*|phone\w*|"
        r"tiktok|youtube|instagram|google|apple|android|iphone|ai\b|artificial intelligence|battery|batteries)\b"
    ),
}
# Preferred source types per domain (most preferred first).  Types missing
# from a list rank after the listed ones; low/unknown always rank last.
PREFERENCES: dict[str, tuple[str, ...]] = {
    "science": (
        "primary_research", "government", "academic", "institutional", "reference", "specialist_secondary", "journalism",
    ),
    "health": (
        "government", "primary_research", "academic", "institutional", "reference", "journalism", "specialist_secondary",
    ),
    "history": (
        "institutional", "academic", "government", "primary_research", "reference", "journalism", "specialist_secondary",
    ),
    "technology": (
        "first_party", "academic", "institutional", "government", "reference", "specialist_secondary", "journalism",
    ),
    "current_events": (
        "journalism", "government", "first_party", "institutional", "academic", "reference", "specialist_secondary",
    ),
    "everyday": (
        "academic", "institutional", "government", "primary_research", "reference", "specialist_secondary", "journalism",
    ),
}
# Domains whose claims are time-sensitive by default.
TIME_SENSITIVE_DOMAINS = {"current_events"}
# Domains where the asked product/organisation's own site is a first-party source.
FIRST_PARTY_DOMAINS = {"technology", "current_events"}


@dataclass(frozen=True)
class RoutePlan:
    domain: str
    preferred_types: tuple[str, ...]
    time_sensitive: bool
    first_party_allowed: bool
    reasons: tuple[str, ...]

    def preference(self, source_type: str) -> int:
        """Lower is better; unlisted types after listed ones, low-quality last."""
        if source_type in self.preferred_types:
            return self.preferred_types.index(source_type)
        return len(self.preferred_types) + {"generic_secondary": 0, "unknown": 1, "user_generated": 2, "low_quality": 3}.get(
            source_type, 0
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "domain": self.domain,
            "preferred_types": list(self.preferred_types),
            "time_sensitive": self.time_sensitive,
            "first_party_allowed": self.first_party_allowed,
            "reasons": list(self.reasons),
        }


def route_question(question: str, *, content_type: str | None = None, hint: str | None = None) -> RoutePlan:
    """One research domain for ``question`` (an optional planner ``hint`` wins when valid)."""
    reasons: list[str] = []
    if hint in PREFERENCES:
        domain = str(hint)
        reasons.append(f"decomposition hint: {domain}")
    elif content_type == "current_explainer":
        domain = "current_events"
        reasons.append("intent content_type current_explainer")
    else:
        hits = {name: len(pattern.findall(question or "")) for name, pattern in _CUES.items()}
        # Ties: the earlier domain in _CUES wins (current events, health, history, science, technology).
        ranked = sorted(hits.items(), key=lambda item: (-item[1], list(_CUES).index(item[0])))
        domain, count = ranked[0]
        if count == 0:
            domain = "everyday"
            reasons.append("no domain cue: everyday mechanism")
        else:
            reasons.append(f"{count} {domain} cue(s)")
    return RoutePlan(
        domain=domain,
        preferred_types=PREFERENCES[domain],
        time_sensitive=domain in TIME_SENSITIVE_DOMAINS,
        first_party_allowed=domain in FIRST_PARTY_DOMAINS,
        reasons=tuple(reasons),
    )
