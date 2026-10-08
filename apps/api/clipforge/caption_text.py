from __future__ import annotations

"""Canonical caption text: immutable display tokens and their spoken form.

The final narration script is the only source of visible caption text. This
module splits it into display tokens (exact script text, never rewritten) and
derives a separate, language-aware *spoken* representation used only to line
the script up with timing evidence from audio ("20 %" -> "zwanzig prozent").

Timing providers report what they heard in their own spelling. The same
normaliser runs over that evidence, so both sides are compared in one spoken
form while the display tokens stay untouched.
"""


import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

# Bump whenever tokenisation or spoken normalisation changes: cached
# alignments built with another version must not be reused.
NORMALIZATION_VERSION = "canonical-text-v1"

_SENTENCE_END = re.compile(r"[.!?…]+[\"'“”’»«)\]]*$")
_LEADING_WRAPPERS = "\"'„“”‚‘’«»([{"
_TRAILING_WRAPPERS = ",;:!?\"'“”‘’«»)]}"


@dataclass(frozen=True)
class Abbreviation:
    spoken: tuple[str, ...]
    # Only abbreviations that can close a sentence may end a caption group.
    may_end_sentence: bool = False


_ABBREVIATIONS: dict[str, dict[str, Abbreviation]] = {
    "de": {
        "z.b.": Abbreviation(("zum", "beispiel")),
        "z.t.": Abbreviation(("zum", "teil")),
        "d.h.": Abbreviation(("das", "heißt")),
        "u.a.": Abbreviation(("unter", "anderem"), True),
        "v.a.": Abbreviation(("vor", "allem")),
        "o.ä.": Abbreviation(("oder", "ähnliches"), True),
        "u.u.": Abbreviation(("unter", "umständen")),
        "bzw.": Abbreviation(("beziehungsweise",)),
        "ca.": Abbreviation(("circa",)),
        "usw.": Abbreviation(("und", "so", "weiter"), True),
        "etc.": Abbreviation(("et", "cetera"), True),
        "dr.": Abbreviation(("doktor",)),
        "prof.": Abbreviation(("professor",)),
        "nr.": Abbreviation(("nummer",)),
        "mio.": Abbreviation(("millionen",), True),
        "mrd.": Abbreviation(("milliarden",), True),
        "jh.": Abbreviation(("jahrhundert",), True),
        "v.chr.": Abbreviation(("vor", "christus"), True),
        "n.chr.": Abbreviation(("nach", "christus"), True),
        "ggf.": Abbreviation(("gegebenenfalls",)),
        "evtl.": Abbreviation(("eventuell",)),
        "inkl.": Abbreviation(("inklusive",)),
        "bspw.": Abbreviation(("beispielsweise",)),
        "sog.": Abbreviation(("sogenannte",)),
        "st.": Abbreviation(("sankt",)),
    },
    "en": {
        "e.g.": Abbreviation(("for", "example")),
        "i.e.": Abbreviation(("that", "is")),
        "etc.": Abbreviation(("et", "cetera"), True),
        "vs.": Abbreviation(("versus",)),
        "dr.": Abbreviation(("doctor",)),
        "mr.": Abbreviation(("mister",)),
        "mrs.": Abbreviation(("missus",)),
        "ms.": Abbreviation(("miz",)),
        "prof.": Abbreviation(("professor",)),
        "approx.": Abbreviation(("approximately",)),
        "st.": Abbreviation(("saint",)),
        "u.s.": Abbreviation(("u", "s"), True),
        "a.m.": Abbreviation(("a", "m"), True),
        "p.m.": Abbreviation(("p", "m"), True),
    },
}

# Measurement units are only spoken out when they directly follow a number.
_UNITS: dict[str, dict[str, tuple[str, ...]]] = {
    "de": {
        "km": ("kilometer",), "m": ("meter",), "cm": ("zentimeter",), "mm": ("millimeter",),
        "kg": ("kilogramm",), "g": ("gramm",), "t": ("tonnen",), "h": ("stunden",),
        "min": ("minuten",), "s": ("sekunden",), "km/h": ("kilometer", "pro", "stunde"),
        "m/s": ("meter", "pro", "sekunde"), "kmh": ("kilometer", "pro", "stunde"),
    },
    "en": {
        "km": ("kilometers",), "m": ("meters",), "cm": ("centimeters",), "mm": ("millimeters",),
        "kg": ("kilograms",), "g": ("grams",), "h": ("hours",), "min": ("minutes",),
        "s": ("seconds",), "km/h": ("kilometers", "per", "hour"), "m/s": ("meters", "per", "second"),
        "mph": ("miles", "per", "hour"), "mi": ("miles",), "ft": ("feet",), "lbs": ("pounds",),
        "lb": ("pounds",),
    },
}

_SYMBOLS: dict[str, dict[str, tuple[str, ...]]] = {
    "de": {
        "%": ("prozent",), "‰": ("promille",), "€": ("euro",), "$": ("dollar",), "£": ("pfund",),
        "&": ("und",), "+": ("plus",), "=": ("gleich",), "§": ("paragraf",), "°": ("grad",),
        "°c": ("grad", "celsius"), "°f": ("grad", "fahrenheit"),
    },
    "en": {
        "%": ("percent",), "‰": ("per", "mille"), "€": ("euros",), "$": ("dollars",),
        "£": ("pounds",), "&": ("and",), "+": ("plus",), "=": ("equals",), "§": ("section",),
        "°": ("degrees",), "°c": ("degrees", "celsius"), "°f": ("degrees", "fahrenheit"),
    },
}
_CURRENCY_PREFIXES = {"€", "$", "£"}

_PART = re.compile(
    r"(?P<sym>°[CcFf](?![^\W\d_])|[%‰€$£&+=§°])"
    r"|(?P<neg>[-−])?(?P<num>\d+(?:[.,'’]\d+)*)"
    r"|(?P<slash>[^\W\d_]+/[^\W\d_]+)"
    r"|(?P<word>[^\W\d_]+(?:['’][^\W\d_]+)*)"
)


@dataclass(frozen=True)
class CanonicalToken:
    """One visible caption token: exact script text plus its spoken form."""

    index: int
    display: str
    units: tuple[str, ...]
    sentence_end: bool

    @property
    def key(self) -> str:
        return spoken_key(" ".join(self.units))


def language_code(language: str | None) -> str:
    return "de" if str(language or "").casefold().startswith("de") else "en"


def spoken_key(text: str) -> str:
    """Comparison form: case-folded letters and digits only (ß -> ss on both sides)."""
    return "".join(character for character in text.casefold() if character.isalnum())


def _abbreviation_key(text: str) -> str:
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFC", text))
    return compact.lstrip(_LEADING_WRAPPERS).rstrip(_TRAILING_WRAPPERS).casefold()


def canonical_tokens(script: str, language: str) -> list[CanonicalToken]:
    """Split the narration into display tokens without changing a single character.

    Whitespace-delimited pieces become tokens. Multi-piece abbreviations ("z. B.")
    stay one token, and pieces without letters or digits ("%", "–", "!") attach
    to their neighbour, so every visible character belongs to exactly one token.
    """
    lang = language_code(language)
    pieces = [(match.start(), match.end()) for match in re.finditer(r"\S+", script)]
    abbreviations = _ABBREVIATIONS[lang]
    spans: list[list[int]] = []
    index = 0
    while index < len(pieces):
        merged = 1
        for count in (3, 2):
            if index + count > len(pieces):
                continue
            window = pieces[index:index + count]
            fragments = [script[start:end] for start, end in window]
            if all(fragment.rstrip(_TRAILING_WRAPPERS).endswith(".") for fragment in fragments) and (
                _abbreviation_key("".join(fragments)) in abbreviations
            ):
                merged = count
                break
        spans.append([pieces[index][0], pieces[index + merged - 1][1]])
        index += merged

    # Symbol-only pieces carry no spoken word of their own boundary: keep them
    # with the previous token ("20 %"), or the next one at the very start.
    joined: list[list[int]] = []
    leading: int | None = None
    for start, end in spans:
        has_word = any(character.isalnum() for character in script[start:end])
        if not has_word:
            if joined:
                joined[-1][1] = end
            elif leading is None:
                leading = start
            continue
        if leading is not None:
            start, leading = leading, None
        joined.append([start, end])
    if leading is not None and not joined:
        return []

    displays = [script[start:end] for start, end in joined]
    units = spoken_unit_sequence(displays, lang)
    tokens: list[CanonicalToken] = []
    for position, (display, spoken) in enumerate(zip(displays, units, strict=True)):
        if not spoken_key(" ".join(spoken)):
            spoken = [spoken_key(display)] if spoken_key(display) else []
        tokens.append(
            CanonicalToken(
                index=len(tokens),
                display=display,
                units=tuple(spoken),
                sentence_end=_ends_sentence(display, displays[position + 1:position + 2], lang),
            )
        )
    return tokens


def _ends_sentence(display: str, following: list[str], lang: str) -> bool:
    if not following:
        return True
    if not _SENTENCE_END.search(display):
        return False
    abbreviation = _ABBREVIATIONS[lang].get(_abbreviation_key(display))
    if abbreviation is None:
        # A lone initial such as "B." or "J." is an abbreviation, not a full stop.
        core = _abbreviation_key(display).rstrip(".")
        return not (len(core) == 1 and core.isalpha())
    next_text = following[0].lstrip(_LEADING_WRAPPERS)
    return abbreviation.may_end_sentence and bool(next_text) and next_text[0].isupper()


def spoken_units(text: str, language: str) -> list[str]:
    return spoken_unit_sequence([text], language)[0]


def spoken_unit_sequence(texts: Sequence[str], language: str) -> list[list[str]]:
    """Spoken units for consecutive texts (context lets "5 km" read as kilometres)."""
    lang = language_code(language)
    result: list[list[str]] = []
    previous_numeric = False
    for text in texts:
        units, previous_numeric = _units(str(text or ""), lang, previous_numeric)
        result.append(units)
    return result


def _units(text: str, lang: str, previous_numeric: bool) -> tuple[list[str], bool]:
    text = unicodedata.normalize("NFC", text)
    key = _abbreviation_key(text)
    abbreviation = _ABBREVIATIONS[lang].get(key)
    if abbreviation is not None:
        return list(abbreviation.spoken), False
    unit = _UNITS[lang].get(key.rstrip("."))
    if previous_numeric and unit is not None:
        return list(unit), False

    units: list[str] = []
    numeric = previous_numeric
    pending_currency: tuple[str, ...] = ()
    for match in _PART.finditer(text):
        if match.group("num"):
            negative = bool(match.group("neg")) and (
                match.start() == 0 or not text[match.start() - 1].isalnum()
            )
            units.extend((["minus"] if negative else []) + _number_words(match.group("num"), lang))
            units.extend(pending_currency)
            pending_currency = ()
            numeric = True
            continue
        if match.group("sym"):
            symbol = match.group("sym").casefold()
            words = _SYMBOLS[lang].get(symbol, ())
            if symbol in _CURRENCY_PREFIXES and re.match(r"\s*[-−]?\d", text[match.end():]):
                pending_currency = words  # "$5" is spoken "five dollars"
            else:
                units.extend(words)
            numeric = False
            continue
        spelled = (match.group("slash") or match.group("word") or "").lower()
        word = spelled.casefold()
        if numeric and lang == "en" and word == "s" and units:
            units[-1] = units[-1][:-1] + "ies" if units[-1].endswith("y") else units[-1] + "s"
        elif numeric and word in _UNITS[lang]:
            units.extend(_UNITS[lang][word])
        elif numeric and lang == "en" and word in {"st", "nd", "rd", "th"} and units:
            units[-1] = _english_ordinal(units[-1])
        elif "/" in word:
            units.extend(part for part in spelled.split("/") if part)
        else:
            units.append(spelled.replace("’", "'"))
        numeric = False
    units.extend(pending_currency)
    return units, numeric


# ---------------------------------------------------------------------------
# Numbers


_DE_ONES = [
    "null", "eins", "zwei", "drei", "vier", "fünf", "sechs", "sieben", "acht", "neun", "zehn",
    "elf", "zwölf", "dreizehn", "vierzehn", "fünfzehn", "sechzehn", "siebzehn", "achtzehn",
    "neunzehn",
]
_DE_TENS = ["", "", "zwanzig", "dreißig", "vierzig", "fünfzig", "sechzig", "siebzig", "achtzig", "neunzig"]
_EN_ONES = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
    "nineteen",
]
_EN_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_MAX_SPOKEN_NUMBER = 999_999_999_999


def _number_words(raw: str, lang: str) -> list[str]:
    raw = raw.replace("’", "'")
    thousands = r"\." if lang == "de" else ","
    decimal = "," if lang == "de" else "."
    grouped = re.fullmatch(rf"(\d{{1,3}}(?:(?:{thousands}|')\d{{3}})+)(?:{re.escape(decimal)}(\d+))?", raw)
    if grouped:
        whole = re.sub(r"\D", "", grouped.group(1))
        return _integer_words(whole, lang, allow_year=False) + _fraction_words(grouped.group(2), lang)
    if raw.isdigit():
        return _integer_words(raw, lang, allow_year=True)
    parts = re.split(r"[.,']", raw)
    if len(parts) == 2:
        separator = raw[len(parts[0])]
        if separator == decimal or separator in ".,":
            word = _decimal_word(lang, separator)
            return _integer_words(parts[0], lang, allow_year=False) + [word] + _digit_words(parts[1], lang)
    # Dates, times and other digit groups are read group by group.
    words: list[str] = []
    for part in parts:
        words.extend(_integer_words(part, lang, allow_year=False))
    return words


def _decimal_word(lang: str, separator: str) -> str:
    if lang == "de":
        return "komma" if separator == "," else "punkt"
    return "point" if separator == "." else "comma"


def _fraction_words(fraction: str | None, lang: str) -> list[str]:
    if not fraction:
        return []
    return [_decimal_word(lang, "," if lang == "de" else ".")] + _digit_words(fraction, lang)


def _digit_words(digits: str, lang: str) -> list[str]:
    ones = _DE_ONES if lang == "de" else _EN_ONES
    return [ones[int(digit)] for digit in digits if digit.isdigit()]


def _integer_words(digits: str, lang: str, *, allow_year: bool) -> list[str]:
    if not digits:
        return []
    if len(digits) > 1 and digits.startswith("0"):
        return _digit_words(digits, lang)
    value = int(digits)
    if value > _MAX_SPOKEN_NUMBER:
        return _digit_words(digits, lang)
    if allow_year and len(digits) == 4:
        year = _year_words(value, lang)
        if year:
            return year
    return _german_cardinal(value) if lang == "de" else _english_cardinal(value)


def _year_words(value: int, lang: str) -> list[str] | None:
    """Four-digit years are read as years ("1999" -> "neunzehnhundertneunundneunzig")."""
    century, rest = divmod(value, 100)
    if lang == "de":
        if 1100 <= value <= 1999:
            return [_german_below_100(century, final=False) + "hundert" + (_german_below_100(rest) if rest else "")]
        return None
    if 1100 <= value <= 1999 or 2010 <= value <= 2099:
        head = _english_below_100(century)
        if rest == 0:
            return head + ["hundred"]
        if rest < 10:
            return head + ["oh", _EN_ONES[rest]]
        return head + _english_below_100(rest)
    return None


def _german_below_100(value: int, *, final: bool = True) -> str:
    if value < 20:
        return "ein" if value == 1 and not final else _DE_ONES[value]
    tens, ones = divmod(value, 10)
    if ones == 0:
        return _DE_TENS[tens]
    return ("ein" if ones == 1 else _DE_ONES[ones]) + "und" + _DE_TENS[tens]


def _german_below_1000(value: int, *, final: bool = True) -> str:
    hundreds, rest = divmod(value, 100)
    head = (("ein" if hundreds == 1 else _DE_ONES[hundreds]) + "hundert") if hundreds else ""
    return head + (_german_below_100(rest, final=final) if rest else "")


def _german_cardinal(value: int) -> list[str]:
    if value == 0:
        return ["null"]
    words: list[str] = []
    for size, singular, plural in ((10**9, "milliarde", "milliarden"), (10**6, "million", "millionen")):
        count, value = divmod(value, size)
        if count:
            words.extend(["eine", singular] if count == 1 else [_german_below_1000(count), plural])
    thousands, rest = divmod(value, 1000)
    compound = ""
    if thousands:
        compound = ("" if thousands == 1 else _german_below_1000(thousands, final=False)) + "tausend"
    if rest:
        compound += _german_below_1000(rest)
    if compound:
        words.append(compound)
    return words


def _english_below_100(value: int) -> list[str]:
    if value < 20:
        return [_EN_ONES[value]]
    tens, ones = divmod(value, 10)
    return [_EN_TENS[tens]] + ([_EN_ONES[ones]] if ones else [])


def _english_below_1000(value: int) -> list[str]:
    hundreds, rest = divmod(value, 100)
    words = [_EN_ONES[hundreds], "hundred"] if hundreds else []
    return words + (_english_below_100(rest) if rest else [])


def _english_cardinal(value: int) -> list[str]:
    if value == 0:
        return ["zero"]
    words: list[str] = []
    for size, name in ((10**9, "billion"), (10**6, "million"), (10**3, "thousand")):
        count, value = divmod(value, size)
        if count:
            words.extend(_english_below_1000(count) + [name])
    if value:
        words.extend(_english_below_1000(value))
    return words


_EN_IRREGULAR_ORDINALS = {
    "one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth",
    "nine": "ninth", "twelve": "twelfth",
}


def _english_ordinal(word: str) -> str:
    if word in _EN_IRREGULAR_ORDINALS:
        return _EN_IRREGULAR_ORDINALS[word]
    if word.endswith("y"):
        return word[:-1] + "ieth"
    return word + "th"
