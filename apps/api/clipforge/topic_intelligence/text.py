"""German text helpers: normalization, light semantic similarity, niches, question checks.

The similarity is deliberately small and dependency-free (no vector store):
German-aware normalization, stop words, light suffix stemming and a soft token
match that understands compounds ("Autofahren" ~ "Auto") and near spellings
(character trigrams).  It is used for novelty and pool de-duplication only.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from ..language import detect_text_language

_WORD_RE = re.compile(r"[a-z0-9]+")
_FOLD = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})

STOP_WORDS = frozenset({
    "bekommen", "bekommt", "kriegen", "kriegt",
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einer", "eines", "einem", "einen",
    "kein", "keine", "keinen", "keiner", "und", "oder", "aber", "ist", "sind", "war", "waren",
    "wird", "werden", "wurde", "wurden", "sein", "bin", "bist", "seid", "hat", "haben", "hatte",
    "hatten", "gibt", "gab", "kann", "koennen", "konnte", "muss", "muessen", "soll", "sollen",
    "will", "wollen", "darf", "duerfen", "macht", "machen", "mache", "man", "wir", "ich", "du",
    "er", "sie", "es", "uns", "euch", "ihr", "dich", "mich", "sich", "dir", "mir", "ihm", "ihn",
    "ihnen", "im", "in", "am", "an", "auf", "aus", "bei", "beim", "mit", "nach", "von", "vom",
    "zu", "zum", "zur", "fuer", "ueber", "unter", "vor", "hinter", "neben", "als", "wie", "so",
    "sehr", "viel", "viele", "mehr", "weniger", "noch", "schon", "auch", "nur", "immer",
    "manchmal", "oft", "nie", "eigentlich", "genau", "wirklich", "tatsaechlich", "ploetzlich",
    "heute", "gerade", "dann", "wenn", "dass", "ob", "denn", "weil", "doch", "warum", "wieso",
    "weshalb", "was", "wer", "wem", "wen", "wessen", "wo", "woher", "wohin", "wann", "welche",
    "welcher", "welches", "wodurch", "wozu", "wofuer", "womit", "worum", "passiert", "passieren",
    "nicht", "mal", "etwa", "ganz", "a", "and", "are", "as", "at", "be", "by", "can", "do",
    "does", "for", "from", "how", "is", "it", "of", "on", "or", "that", "the", "their", "this",
    "to", "what", "when", "where", "which", "who", "why", "with", "you", "your",
})

_SUFFIXES = ("igkeit", "ungen", "ung", "keit", "heit", "lich", "isch", "en", "er", "em", "es", "e", "n", "s")


def fold(text: str) -> str:
    """Lowercase, fold umlauts/ß and strip accents and punctuation noise."""
    value = unicodedata.normalize("NFKC", str(text or "")).casefold().translate(_FOLD)
    value = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in value if not unicodedata.combining(ch))


def stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


# Small concept map for everyday synonyms the stemmer cannot see (folded stems).
CONCEPT_SYNONYMS = {
    "nickerch": "mittagsschlaf",
    "powernap": "mittagsschlaf",
    "schlaefrig": "mued",
    "erschoepf": "mued",
    "pkw": "auto",
    "wage": "auto",
    "kfz": "auto",
    "handy": "smartphone",
    "suessigkeit": "suess",
    "suessig": "suess",
    "heisshung": "lust",
    "appetit": "lust",
    "geluest": "lust",
    "himmelskoerp": "planet",
}


def content_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for word in _WORD_RE.findall(fold(text)):
        if len(word) < 3 or word in STOP_WORDS or word.isdigit():
            continue
        token = stem(word)
        token = CONCEPT_SYNONYMS.get(token, token)
        if token not in tokens:
            tokens.append(token)
    return tokens


def topic_key(text: str) -> str:
    """Order-independent normalized key used to merge sightings of one topic."""
    return " ".join(sorted(content_tokens(text))) or fold(text).strip()


def _trigrams(token: str) -> set[str]:
    padded = f"_{token}_"
    return {padded[index : index + 3] for index in range(len(padded) - 2)}


def token_match(left: str, right: str) -> float:
    if left == right:
        return 1.0
    shorter, longer = sorted((left, right), key=len)
    if len(shorter) >= 4 and shorter in longer:
        return 0.8  # compound: "auto" in "autofahr", "schlaf" in "mittagsschlaf"
    a, b = _trigrams(left), _trigrams(right)
    jaccard = len(a & b) / max(1, len(a | b))
    return jaccard if jaccard >= 0.6 else 0.0


def similarity(left: str | list[str], right: str | list[str]) -> float:
    """Soft overlap coefficient of content tokens, 0..1 (1 = same concepts)."""
    a = content_tokens(left) if isinstance(left, str) else list(left)
    b = content_tokens(right) if isinstance(right, str) else list(right)
    if not a or not b:
        return 0.0
    small, large = (a, b) if len(a) <= len(b) else (b, a)
    total = sum(max(token_match(token, other) for other in large) for token in small)
    return round(total / len(small), 4)


# ---------------------------------------------------------------------------
# Niches: what a German short-form knowledge channel can explain well.
# Prior = channel fit of the niche (documented, 0..1).  Keywords are folded
# stems matched as token prefixes.
# ---------------------------------------------------------------------------

NICHE_PRIORS: dict[str, float] = {
    "koerper_gesundheit": 0.92,
    "wissenschaft": 0.9,
    "weltraum": 0.9,
    "psychologie": 0.88,
    "natur_tiere": 0.88,
    "alltag_phaenomene": 0.88,
    "technik": 0.85,
    "essen_trinken": 0.82,
    "geschichte": 0.8,
    "geografie": 0.8,
    "wetter_klima": 0.8,
    "wirtschaft_geld": 0.7,
    "sprache_kultur": 0.68,
    "gesellschaft": 0.55,
    "unknown": 0.5,
    "politik_tagesgeschehen": 0.3,
    "sport": 0.3,
    "unterhaltung": 0.22,
    "promi_personen": 0.12,
    "unglueck_tragoedie": 0.08,
}

# Broad-audience appeal of a niche for general German knowledge shorts (documented prior,
# 0..1): how likely an average viewer without special interest is curious about it.
# A prior only - an assessed question or strong evidence can outweigh it.
BROAD_APPEAL_PRIORS: dict[str, float] = {
    "koerper_gesundheit": 0.9,
    "weltraum": 0.88,
    "psychologie": 0.86,
    "alltag_phaenomene": 0.86,
    "natur_tiere": 0.84,
    "wissenschaft": 0.8,
    "wetter_klima": 0.78,
    "essen_trinken": 0.76,
    "technik": 0.7,
    "geschichte": 0.68,
    "geografie": 0.64,
    "wirtschaft_geld": 0.55,
    "gesellschaft": 0.55,
    "sprache_kultur": 0.5,
    "unknown": 0.45,
    "politik_tagesgeschehen": 0.3,
    "sport": 0.3,
    "unterhaltung": 0.3,
    "promi_personen": 0.15,
    "unglueck_tragoedie": 0.1,
}

POOR_FIT_NICHES = frozenset({"politik_tagesgeschehen", "sport", "unterhaltung", "promi_personen", "unglueck_tragoedie"})

_NICHE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "koerper_gesundheit": ("koerp", "schluckauf", "zwerchfell", "reflex", "gaensehaut", "muskelkater", "schlaf", "mued", "gehirn", "herz", "blut", "haut", "muskel", "gesund", "krank", "virus", "bakteri", "immun", "hunger", "durst", "schmerz", "zucker", "vitamin", "husten", "fieber", "grippe", "allergi", "augen", "zaehn", "magen", "darm", "atmen", "gaehn", "schwitz", "niesen", "altern", "mittagsschlaf", "koffein", "ernaehr", "medizin", "medikament", "impf"),
    "psychologie": ("psych", "gefuehl", "angst", "stress", "gedaechtn", "erinner", "traeum", "traum", "gewohnheit", "motivation", "langeweil", "einsam", "lust", "gluecklich", "emotion", "verhalten", "entscheid"),
    "wissenschaft": ("physik", "chemi", "biolog", "forsch", "studie", "experiment", "atom", "molekuel", "energie", "licht", "schall", "gravitation", "schwerkraft", "temperatur", "wasser", "eis", "magnet", "elektr", "quant", "element", "reaktion", "evolution", "dna", "gen"),
    "weltraum": ("weltraum", "planet", "mond", "sonne", "stern", "galaxi", "rakete", "astronaut", "nasa", "esa", "mars", "komet", "asteroid", "schwarz loch", "universum", "satellit", "polarlicht", "sonnenfinsternis", "mondfinsternis", "sternschnupp", "meteor"),
    "natur_tiere": ("tier", "vogel", "voegel", "hund", "katz", "insekt", "biene", "wespe", "spinn", "fisch", "wal", "hai", "pflanz", "baum", "wald", "blum", "pilz", "zeck", "muecke", "schlang", "affe", "igel", "zugvoeg", "herbstlaub", "laub", "natur", "vulkan", "erdbeben"),
    "alltag_phaenomene": ("alltag", "haushalt", "kueche", "auto", "fahr", "zug", "bahn", "handy", "smartphone", "akku", "geld", "einkauf", "supermarkt", "wohnung", "heiz", "strom", "wasserkocher", "kaffee", "zeitumstell", "uhr", "herbst", "winter", "sommer"),
    "technik": ("technik", "computer", "internet", "ki", "kuenstlich intelligenz", "roboter", "chip", "software", "app", "motor", "flugzeug", "elektroauto", "batteri", "solar", "windkraft", "kernkraft", "reaktor", "laser", "wlan", "bluetooth", "gps", "kamera", "bildschirm"),
    "essen_trinken": ("essen", "lebensmittel", "brot", "kaese", "schokolad", "suess", "zucker", "obst", "apfel", "kartoffel", "nudel", "fleisch", "bier", "wein", "kaffee", "tee", "gewuerz", "salz", "backen", "kochen", "kuerbis", "oktoberfest"),
    "geschichte": ("geschicht", "mittelalter", "roemer", "antik", "krieg", "weltkrieg", "kaiser", "koenig", "burg", "pyramid", "mauer", "ddr", "wiedervereinig", "revolution", "erfind", "jahrhundert", "historisch", "archaeolog"),
    "geografie": ("land", "laender", "stadt", "staedte", "insel", "meer", "fluss", "berg", "alpen", "wueste", "kontinent", "grenz", "karte", "rhein", "nordsee", "ostsee", "bodensee", "schweiz", "oesterreich", "deutschland"),
    "wetter_klima": ("wetter", "klima", "regen", "gewitter", "blitz", "sturm", "hitze", "frost", "schnee", "hagel", "nebel", "wolk", "regenbogen", "unwetter", "orkan", "temperatur"),
    "wirtschaft_geld": ("wirtschaft", "inflation", "preis", "euro", "bank", "zins", "aktie", "boerse", "steuer", "rente", "gehalt", "lohn", "firma", "unternehm", "handel", "kosten"),
    "sprache_kultur": ("sprache", "wort", "woert", "dialekt", "redewendung", "brauch", "tradition", "feiertag", "musik", "kunst", "buch", "halloween", "weihnacht", "ostern", "karneval", "fasching"),
    "gesellschaft": ("gesellschaft", "schule", "arbeit", "beruf", "familie", "jugend", "generation", "social media", "trend"),
    "politik_tagesgeschehen": ("politik", "partei", "wahl", "bundestag", "regierung", "minister", "kanzler", "praesident", "gesetz", "koalition", "afd", "cdu", "spd", "gruene", "fdp", "bsw", "linke", "abgeordnet", "diplomat", "sanktion"),
    "sport": ("sport", "fussball", "bundesliga", "champions", "tor", "spieler", "trainer", "tennis", "formel", "rennen", "olymp", "weltmeister", "meisterschaft", "turnier", "liga", "pokal", "dfb", "fc", "handball", "basketball", "boxen", "nfl", "nba"),
    "unterhaltung": ("serie", "staffel", "film", "kino", "netflix", "folge", "sendung", "tatort", "show", "castingshow", "reality", "album", "song", "lied", "konzert", "tour", "streamer", "influencer", "trailer", "episode", "computerspiel", "videospiel", "gameplay"),
    "promi_personen": ("schauspiel", "saenger", "moderator", "rapper", "musiker", "promi", "star", "model", "influencer", "youtuber", "politiker", "fussballspiel"),
    "unglueck_tragoedie": ("tod", "tot", "gestorben", "verstorben", "anschlag", "attentat", "unglueck", "absturz", "amok", "mord", "leiche", "opfer", "unfall", "katastroph", "ermord", "trauer"),
}


def classify_niche(*texts: str) -> tuple[str, float]:
    """(niche, match strength 0..1) from keywords; ``unknown`` when nothing matches."""
    tokens = [word for text in texts for word in _WORD_RE.findall(fold(text))]
    joined = " ".join(tokens)
    scores: dict[str, int] = {}
    for niche, keywords in _NICHE_KEYWORDS.items():
        hits = 0
        for keyword in keywords:
            if " " in keyword:
                hits += keyword in joined
            else:
                hits += any(token.startswith(keyword) for token in tokens)
        if hits:
            scores[niche] = hits
    if not scores:
        return "unknown", 0.0
    # Deterministic: most hits, then the documented niche order.
    order = list(NICHE_PRIORS)
    best = min(scores.items(), key=lambda item: (-item[1], order.index(item[0])))
    return best[0], min(1.0, 0.45 + 0.2 * best[1])


# ---------------------------------------------------------------------------
# Question checks (truth-seeking, natural German, no embedded answer)
# ---------------------------------------------------------------------------

_GERMAN_QUESTION_START = re.compile(
    r"^(?:warum|wieso|weshalb|wie|was|wer|wo|woher|wohin|wann|welche[rsmn]?|wodurch|wozu|wofür|womit|"
    r"kann|können|ist|sind|gibt|hat|haben|stimmt|macht|machen|muss|müssen|würde|wird|werden|darf|sollte)\b",
    re.IGNORECASE,
)
_ANSWER_MARKERS = re.compile(r"(?i)\b(?:weil|denn|deshalb|daher|darum|die antwort|der grund ist|liegt daran)\b")
_TITLE_SEPARATOR = re.compile(r"[:–—|]")
_CLICKBAIT = re.compile(
    r"(?i)\b(?:unglaublich|schockierend|krass|wahnsinn|niemand sagt|du wirst nicht glauben|geheimnis enthüllt|"
    r"geheimer trick|mega|omg|clickbait|must see|sie verschweigen)\b|!{2,}"
)
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF]")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def clean_title(text: str) -> str:
    """Strip YouTube/news decoration: emojis, hashtags, channel suffixes, brackets."""
    value = _EMOJI.sub("", str(text or ""))
    value = re.sub(r"#\w+", "", value)
    value = re.sub(r"\s*[|•]\s*[^|•]*$", "", value)
    value = re.sub(r"[\[(][^\])]*[\])]", "", value)
    value = re.sub(r"\s+", " ", value).strip(" -–—:")
    return value


def question_issues(question: str, *, evidence: str = "") -> list[str]:
    """Reasons a proposed German question is unusable (empty list = usable)."""
    text = " ".join(str(question or "").split())
    issues: list[str] = []
    words = text.split()
    if not text.endswith("?"):
        issues.append("not_a_question")
    if not 4 <= len(words) <= 20 or len(text) > 150:
        issues.append("length_out_of_bounds")
    if not _GERMAN_QUESTION_START.match(text) or detect_text_language(text) == "en":
        issues.append("not_natural_german")
    if _ANSWER_MARKERS.search(text.rstrip("?")):
        issues.append("answer_embedded")
    if _TITLE_SEPARATOR.search(text):
        issues.append("title_format")  # "Thema: Frage?" is a headline, not a spoken question
    if _CLICKBAIT.search(text) or _EMOJI.search(text) or sum(1 for word in words if len(word) > 3 and word.isupper()) >= 2:
        issues.append("clickbait")
    if evidence is not None:
        folded_evidence = fold(evidence)
        for number in _NUMBER.findall(text):
            if number not in folded_evidence:
                issues.append("unsupported_number")
                break
    return issues


def compact(value: Any, limit: int = 280) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Accessibility / obscurity features (observed properties of the text; the
# scoring authority decides what they are worth).
# ---------------------------------------------------------------------------

_MONTHS = "januar|februar|märz|maerz|april|mai|juni|juli|august|september|oktober|november|dezember"
_DATE_TITLE = re.compile(rf"(?i)^(?:\d{{1,2}}\.\s*(?:{_MONTHS})|(?:{_MONTHS})\s+\d{{3,4}}|\d{{3,4}}(?:\s*v\.\s*chr\.)?)$")
_CALENDAR_DESCRIPTION = re.compile(r"(?i)\btag (?:des|im) gregorianischen kalender|\bjahr (?:des|im) \d+\. jahrhundert")
# A name followed by a number ("Flug 1907", "Sommerspiele 2028") or a code ("A320"); a quantity
# ("11 Kilometer") is not an identifier.
_IDENTIFIER = re.compile(r"\b[A-ZÄÖÜ][\wäöüß-]*[- ]\d{2,}\b(?!\s*[A-Za-zäöüÄÖÜ])|\b[A-Z]{1,3}\d+[A-Z0-9]*\b")
_DATE_IN_TEXT = re.compile(rf"(?i)\b\d{{1,2}}\.\s*(?:{_MONTHS})\b")
_ACRONYM = re.compile(r"\b[A-ZÄÖÜ]{3,}\b")
_FOREIGN_LETTERS = re.compile(r"[àáâãåæçèéêëìíîïñòóôõøùúûýÿœ]", re.IGNORECASE)
_EVENT_DESCRIPTION = re.compile(r"(?i)\b(?:flugunfall|flugzeugabsturz|zwischenfall|linienflug|eisenbahnunfall|schiffsunglück|wahl (?:zum|in)|parlamentswahl|fußballspiel|länderspiel|saison \d)")
_GENERIC_WRAPPER = re.compile(r"(?i)^(?:was steckt (?:eigentlich )?hinter|was (?:ist|sind|war|waren) (?:eigentlich )?|was bedeutet|wer (?:ist|war|sind|waren))\b")

MECHANISMS = ("paradox", "what_if", "why", "how", "yes_no", "what_is", "other")


def topic_obscurity_flags(title: str, description: str = "") -> set[str]:
    """Why a raw topic may need prior knowledge (a date page, an identifier, an acronym, ...)."""
    text = " ".join(str(title or "").split())
    flags: set[str] = set()
    if _DATE_TITLE.match(text) or _CALENDAR_DESCRIPTION.search(description or ""):
        flags.add("date_page")
    if _IDENTIFIER.search(text) and "date_page" not in flags:
        flags.add("identifier")
    if _ACRONYM.search(text):
        flags.add("acronym")
    if _FOREIGN_LETTERS.search(text):
        flags.add("foreign_proper_name")
    if text.count("-") >= 2 or (text.count("-") >= 1 and _ACRONYM.search(text)):
        flags.add("compound_proper_name")
    if _EVENT_DESCRIPTION.search(description or ""):
        flags.add("isolated_event")
    return flags


def question_mechanism(question: str) -> str:
    """The curiosity mechanism of a German question (used for quality and diversity)."""
    text = " ".join(str(question or "").split()).casefold()
    if re.match(r"^(?:wie kann es sein|wieso .*obwohl|warum .*obwohl|warum .*trotzdem)", text) or " obwohl " in text:
        return "paradox"
    if re.match(r"^(?:was (?:würde|wuerde) passieren|was wäre|was waere|was passiert,? wenn)", text):
        return "what_if"
    if re.match(r"^(?:warum|wieso|weshalb)\b", text):
        return "why"
    if re.match(r"^(?:wie|wodurch|woher)\b", text):
        return "how"
    if _GENERIC_WRAPPER.match(text):
        return "what_is"
    if re.match(r"^(?:kann|können|ist|sind|gibt|hat|haben|stimmt|macht|muss|müssen|wird|werden|darf|sollte)\b", text):
        return "yes_no"
    return "other"


def question_flags(question: str, topic: str = "") -> set[str]:
    """Obscurity carried INTO the question, plus a generic wrapper around a bare topic."""
    text = " ".join(str(question or "").split())
    flags = {f"question_{flag}" for flag in topic_obscurity_flags(text.rstrip("?")) - {"isolated_event", "date_page"}}
    if _DATE_IN_TEXT.search(text):
        flags.add("question_date")
        flags.discard("question_identifier")
    subject = " ".join(str(topic or "").split())
    bare_topic_wrapper = bool(subject) and re.fullmatch(
        rf"(?i)(?:was steckt (?:eigentlich )?hinter|was (?:ist|sind) (?:eigentlich )?|wie funktioniert (?:eigentlich )?|wie entsteht (?:eigentlich )?)"
        rf"(?:der |die |das |ein |eine )?{re.escape(subject)}\?",
        text,
    )
    if _GENERIC_WRAPPER.match(text) or (bare_topic_wrapper and subject and topic_obscurity_flags(subject)):
        flags.add("generic_wrapper")
    return flags
