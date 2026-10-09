"""Question-relative answer grounding: does a sentence answer *this* question?

Lexical relevance only says a sentence is about the same topic.  A core answer
must also preserve what the question asks:

* the ENTITIES the question is about (the sky *of Mars*, not the night sky;
  *künstliche Intelligenz* = "KI"), with compound words counted only when
  their modifier is part of the question ("Marshimmel" yes, "Nachthimmel" no);
* the RELATION the question type requests: a cause or mechanism for why/how,
  the asked predicate for can/does questions ("empfinden", not "analysiert ...
  Schmerz bei Patienten"), a time for when-questions;
* no substitute shapes: advice for an explanation, a naming/definition for a
  cause, an observer relation (X detects Y in someone else) for X doing Y,
  a restatement of the phenomenon.

Deterministic and topic-free: German nouns are capitalised, so the
question's entities come from grammar; other languages fall back to content
words with a coverage quota.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from ..question_intent import answer_mode
from ..story_arc import is_explanatory_question
from .evidence import KIND_PATTERNS, PROCESS_PATTERN, words

_TOKEN = re.compile(r"[A-Za-zÄÖÜäöüß0-9][\wÄÖÜäöüß-]*")
_LEAD = {
    "warum", "wieso", "weshalb", "weswegen", "wodurch", "wie", "was", "wann", "wo", "wer", "welche", "welcher", "welches",
    "kann", "können", "könnte", "ist", "sind", "gibt", "hat", "haben", "wird", "werden", "why", "how", "what", "when",
    "where", "who", "which", "can", "could", "is", "are", "does", "do", "did", "will",
}
_QTYPE = (
    ("can", re.compile(r"(?i)^\s*(?:kann|können|könnte|könnten|darf|can|could|is it possible|ist es möglich)\b")),
    ("when", re.compile(r"(?i)^\s*(?:wann|seit wann|when|since when)\b")),
    ("where", re.compile(r"(?i)^\s*(?:wo|woher|wohin|where)\b")),
    ("what", re.compile(r"(?i)^\s*(?:was ist|was sind|was bedeutet|what is|what are|what does .* mean)\b")),
    ("which", re.compile(r"(?i)^\s*(?:welche\w*|which|wer|who)\b")),
)
# Function words that never carry a question's relation (adverbs, particles).
_FILLER = {
    "überhaupt", "eigentlich", "immer", "erst", "oft", "völlig", "nicht", "manchmal", "genau", "wirklich", "denn", "ohne",
    "dass", "uns", "man", "eine", "einem", "einen", "einer", "ein", "der", "die", "das", "dem", "den", "des", "auf", "in",
    "nach", "vor", "mit", "bei", "von", "und", "oder", "zu", "so", "noch", "schon", "dabei", "even", "really", "actually",
    "always", "only", "just", "the", "a", "an", "of", "on", "to", "and", "or", "it", "its", "at", "after", "before",
    "wurde", "wurden", "wird", "werden", "war", "waren", "ist", "sind", "hat", "haben", "hatte", "kann", "können", "lässt",
    "vom", "zum", "zur", "auch", "obwohl", "sie", "sein", "seine", "ihre", "durch", "aus",
}
_ADJ_SUFFIX = ("lich", "isch", "ig", "en", "er", "es", "em", "e", "n", "s", "ish")
_ADVICE = re.compile(
    r"(?i)\b(?:hilft|helfen|hilfreich|tipps?|solltest|sollten sie|sollte man|vermeide\w*|versuch(?:e|t)? |"
    r"helps?|helpful|tips?|you should|try to|avoid\w*)\b"
)
# A why-question about something people *did* (passive "wurde ... gebaut", or an action verb) asks
# for the purpose / motive / reason of that action, not for what followed it.
_INTENTIONAL_ACTION = re.compile(
    r"(?i)\b(?:wurde|wurden|worden|wird|werden)\b[^?]*\b(?:ge\w+(?:t|en)|\w+iert)\b|"
    r"\b(?:baute|bauten|errichtete\w*|gründete\w*|führte\w* [^?]*\bein|erfand\w*|entwickelte\w*|beschloss\w*|"
    r"verbot\w*|erließ\w*|schuf\w*|plante\w*|eröffnete\w*|schloss\w*|entschied\w*|unterzeichnete\w*|wählte\w*)\b|"
    r"\b(?:was|were)\b[^?]*\b(?:built|introduced|founded|created|invented|made|developed|banned|closed|opened|"
    r"launched|chosen|signed|declared|erected)\b|\bwhy did\b"
)
# Purpose / motive / reason markers: the cause or aim *of* the action.
_PURPOSE = re.compile(
    r"(?i)\bum\b[^.;:]{1,80}?\bzu\s+\w+|\bdamit\b(?!\s+(?:du|ihr|dein\w*|euer\w*)\b)|\bziel\w*\s+(?:war|ist|sollte|waren)\b|"
    r"\b(?:sollte|sollten|wollte|wollten)\b|\bwegen\b|\baufgrund\b|\bals (?:reaktion|antwort) auf\b|\bweil\b|\bdenn\b|"
    r"\bgrund (?:dafür|war|ist|waren)\b|\bzweck\b|\bin order to\b|\bso as to\b|\bbecause\b|\bdue to\b|\bin response to\b|"
    r"\baim(?:ed)?\b|\bintended\b|\bpurpose\b|\bto (?:prevent|stop|keep|protect|ensure|allow|avoid|reduce|control|end)\b|"
    r"\b(?:built|made|created|introduced|founded|designed|erected|established|developed|passed|signed)\b[^.;]{0,50}?\bto\s+(?!be\b)[a-z]{3,}"
)
# Result markers: what happened *after* / *because of* the action ("..., sodass ...").
_RESULT = re.compile(
    r"(?i)\b(?:sodass|so dass|wodurch|weshalb|infolgedessen|folglich|als folge|was dazu führte|führte dazu|"
    r"resulting in|as a result|which led to|thereby|consequently)\b"
)
# What the reader could do (generic agent + possibility/obligation): advice, not an explanation.
_GENERIC_ADVICE = re.compile(
    r"(?i)\b(?:(?:kann|könnte|sollte|muss|darf) man|man (?:kann|könnte|sollte|muss)|(?:kannst|solltest|musst|könntest) du|"
    r"du (?:kannst|solltest|musst)|am besten|es (?:lohnt|empfiehlt) sich|lässt sich [^.]{0,40}\b(?:vermeiden|verhindern|umgehen)|"
    r"you (?:can|could|should|must)\b|one (?:can|should)\b|the best way to|try to|make sure|it helps to)\b"
)
# The page talking about itself or its reader: its purpose is not the phenomenon's cause.
_AUTHOR_PURPOSE = re.compile(
    r"(?i)\b(?:(?:in|mit|mithilfe|nach) (?:diese[mnrs]?|unsere[mnrs]?|meine[mnrs]?) (?:artikel|beitrag|ratgeber|blog\w*|post|video|"
    r"guide|text|kurs|buch|podcast)|(?:in|with|through) (?:this|our|my) (?:article|post|guide|video|blog|course|book)|"
    r"(?:ist|war) (?:es )?(?:mir|uns) (?:so |besonders |sehr |ganz )?wichtig|it(?:'s| is) (?:so )?important to (?:me|us)|"
    r"damit (?:du|ihr|dein\w*|euer\w*)\b|so (?:that )?you (?:can|will|don't)|"
    r"(?:ich|wir) (?:zeige|zeigen|erkläre|erklären|verrate|verraten|möchte|möchten) (?:dir|euch|ihnen)|"
    r"(?:i|we)(?:'ll| will)? (?:show|tell|explain to) you|(?:hier|unten|im folgenden|jetzt) erfährst du|"
    r"here you(?:'ll| will) (?:learn|find)|dir (?:\w+ ){0,6}zu liefern|für dich (?:zusammengefasst|erklärt))"
)
_NAMING = re.compile(
    r"(?i)\b(?:(?:wird|werden|wurde|wurden)\b[^.]{0,60}\b(?:bezeichnet|genannt)|nennt man|bezeichnet man|heißt|"
    r"spitzname|beiname|is (?:often |also )?(?:called|known as|nicknamed)|are (?:often |also )?(?:called|known as)|nickname)\b"
)
_OBSERVER = re.compile(
    r"(?i)\b(?:merk(?:t|en|te|ten|e)?|erkenn\w*|analysier\w*|analyse\w*|mess\w*|misst|detektier\w*|überwach\w*|auswert\w*|interpretier\w*|"
    r"registrier\w*|identifizier\w*|aufspür\w*|find\w* heraus|detect\w*|analy[sz]\w*|measur\w*|monitor\w*|recogni[sz]\w*|"
    r"identif\w*|track\w*|find\w* out|search\w*|such\w*)\b"
)
_CAUSAL = KIND_PATTERNS["mechanism"]
_DATE = KIND_PATTERNS["date"]
_CONNECTIVES = {
    "weil", "denn", "deshalb", "daher", "darum", "deswegen", "dadurch", "sodass", "damit", "wodurch", "indem", "because",
    "since", "therefore", "thus", "hence", "dort", "there",
}
_CONDITION_CLAUSE = re.compile(
    r"(?i)\b(?:when|while|if|under|during|after|before|wenn|falls|während|unter|bei|nach|vor)\b"
    r"([^?;,:]{1,100})"
)
# Grammatical how-questions, excluding requests for a quantity or attribute.
# This is deliberately local to research; downstream script heuristics are separate.
_HOW = re.compile(r"(?i)^\s*(?:wie(?!\s+(?:viel\w*|oft|alt|lang\w*|groß|weit|hoch|schwer)\b)|how(?!\s+(?:much|many|often|old|long|far|high)\b))\b")
_OPERATION = re.compile(r"(?i)\b(?:funktionier\w*|arbeit\w*|works?|operat\w*)\b")
_DETECTION = re.compile(r"(?i)\b(?:merk(?:t|en|te|ten|e)?|erkenn\w*|bestimm\w*|detektier\w*|mess\w*|misst|detect\w*|determin\w*|measur\w*|recogni[sz]\w*)\b")
# These equivalences describe changes/actions, never particular subject matter.
_PREDICATE_EQUIVALENTS = (
    re.compile(r"(?i)\b(?:verlieren|verliert|nachlass\w*|abnehm\w*|nimmt\b[^.;]{0,30}\bab|verringer\w*|sink\w*|decreas\w*|declin\w*|loses?|lose)\b"),
    _DETECTION,
    re.compile(r"(?i)\b(?:beats?|puls\w*|schlägt|schlagen|takt\w*)\b"),
)
_PERIPHERAL = re.compile(
    r"(?i)\b(?:untersucht\w*|untersuchen|erforscht\w*|erforschen|forschungs\w*|studie\w*|study|studies|researchers?|"
    r"investigat\w*|specifications?|\w*hersteller\w*|manufacturers?)\b"
)
_REPORTED_CAUSE = re.compile(r"(?i)\b(?:heraus|found|discovered|zeigten|zeigen|showed|show)\b[^.;]{0,30}\b(?:dass|that)\b(.*)")
_CAUSE_SUBJECT = re.compile(r"(?i)^\s*(?:(?:die|der|the)\s+)?(?:ursache|grund|cause|reason)\b")
_LOCAL_REFERENCE = re.compile(r"(?i)^\s*(?:(?:die|der|das)\s+(?:dabei|dadurch)|(?:da\s+)?(?:sie|er|es|diese\w*|deren|seine\w*|ihre\w*|dadurch|dabei|damit|it|its|they|their|this|these))\b")
# Opposing grammatical scope modifiers must not be supplied by a page title.
# These pairs apply to any process/device, not to a particular question/topic.
_OPPOSING_MODIFIERS = (
    (r"aktiv\w*|active", r"passiv\w*|passive"),
    (r"automatisch\w*|automatic", r"manuell\w*|manual"),
)


def _fold(word: str) -> str:
    return word.casefold().replace("ä", "a").replace("ö", "o").replace("ü", "u").replace("ß", "ss")


def _verb_stem(word: str) -> str:
    word = _fold(word)
    if word.startswith("ge") and len(word) > 5:
        word = word[2:]
    for suffix in ("ungen", "ation", "ung", "est", "et", "en", "st", "er", "t", "e", "n", "s", "ing", "ed"):
        if len(word) - len(suffix) >= 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def inflects(term: str, word: str) -> bool:
    """Same word in another form or as the head of a longer word ("Mikrowelle" ~ "Mikrowellen", "rot" ~ "rötlich")."""
    a, b = _fold(term), _fold(word)
    if a == b or _verb_stem(a) == _verb_stem(b):
        return True
    # A case ending may follow an already plural/agent form ("-er-n").
    if any(a == b + ending or b == a + ending for ending in ("n", "en", "e", "s", "es")):
        return True
    if len(a) >= 4 and b.startswith(a):
        return True
    return any(b == a + suffix + ending for suffix in _ADJ_SUFFIX for ending in ("", "e", "en", "er", "es", "em"))


@dataclass(frozen=True)
class Entity:
    name: str
    head: str
    forms: frozenset[str]  # extra aliases: acronym, other words of a multiword name

    def matched(self, tokens: list[str], others: set[str], acronyms: set[str]) -> bool:
        if _fold(self.head) in acronyms or self.forms & acronyms:
            return True
        for token in tokens:
            # A multiword name is matched by its head noun ("Mauer"), never by its modifier
            # alone ("Berliner Umland" is not the Berliner Mauer).
            if inflects(self.head, token):
                return True
            # Compound with the entity as its head counts only when the
            # modifier is itself part of the question ("Marshimmel", not "Nachthimmel").
            folded, head = _fold(token), _fold(self.head)
            if len(head) >= 4 and folded.endswith(head) and len(folded) > len(head):
                modifier = folded[: -len(head)].rstrip("s-")
                if any(inflects(other, modifier) for other in others if _fold(other) != head):
                    return True
        # A compound can be expressed as separate words in its local context.
        # Both parts must be present; its unqualified head alone never suffices.
        parts = [part for token in tokens for part in token.split("-") if len(part) >= 3]
        return any(inflects(self.head, first + second) for first in parts if _fold(self.head).startswith(_fold(first))
                   for second in parts if first != second and len(second) >= 4)


@dataclass(frozen=True)
class QuestionFrame:
    question: str
    qtype: str  # why | how | can | when | where | what | which | other
    language: str
    entities: tuple[Entity, ...]
    predicate: frozenset[str]
    terms: frozenset[str]
    explanation_asked: bool
    asks_observation: bool
    asks_naming: bool
    # What the answer must state about the asked event: cause (a phenomenon), purpose (an
    # intentional action: "Warum wurde X gebaut?"), mechanism, capability, time, place, identity.
    relation: str = "other"
    extra: dict = field(default_factory=dict, compare=False)

    @property
    def required_entities(self) -> int:
        count = len(self.entities)
        if self.language != "de":
            return math.ceil(0.6 * count)
        return count if count <= 2 else count - 1

    def as_dict(self) -> dict[str, object]:
        return {
            "question_type": self.qtype,
            "relation": self.relation,
            "entities": [entity.name for entity in self.entities],
            "required_entities": self.required_entities,
            "predicate": sorted(self.predicate),
            "condition_terms": sorted(self.extra.get("condition_terms") or []),
        }


def _acronyms(tokens: list[str]) -> set[str]:
    """Abbreviations a text spells out or uses: upper-case tokens ("KI") and the initials of a
    name that ends in a capitalised noun ("künstliche Intelligenz" -> "ki") - never the
    initials of arbitrary neighbouring words ("gleich anspricht")."""
    found = {_fold(token) for token in tokens if 2 <= len(token) <= 4 and token.isupper()}
    for size in (2, 3):
        for index in range(len(tokens) - size + 1):
            window = tokens[index:index + size]
            if window[-1][:1].isupper() and all(re.match(r"[A-Za-zÄÖÜäöüß]", word) for word in window):
                found.add("".join(_fold(word)[0] for word in window))
    return found


def question_frame(question: str, language: str = "de") -> QuestionFrame:
    text = str(question or "").strip()
    lang = "de" if str(language).startswith("de") else "en"
    explanatory = is_explanatory_question(text) or bool(_HOW.search(text))
    qtype = "other"
    if explanatory:
        qtype = "how" if re.match(r"(?i)^\s*(?:wie|how)\b", text) else "why"
    else:
        qtype = next((name for name, pattern in _QTYPE if pattern.search(text)), "other")
    tokens = _TOKEN.findall(text)
    if tokens and tokens[0].casefold() in _LEAD:
        tokens = tokens[1:]
    entities: list[Entity] = []
    predicate: set[str] = set()
    if lang == "de":
        index = 0
        while index < len(tokens):
            token = tokens[index]
            lower = token.casefold()
            if token[:1].isupper() and lower not in _LEAD and lower not in _FILLER:
                phrase = [token]
                # Only a proper adjective ("Berliner", "Wiener") fuses with the next noun into one name;
                # "KI Bilder" stays subject + object.
                while (index + 1 < len(tokens) and tokens[index + 1][:1].isupper() and phrase[-1].endswith("er")
                       and tokens[index + 1].casefold() not in _FILLER):
                    index += 1
                    phrase.append(tokens[index])
                modifier = tokens[index - len(phrase)] if index - len(phrase) >= 0 else ""
                forms = {_fold(word) for word in phrase[:-1]}
                # The initial -en word in a German question is a finite verb,
                # not an adjective naming the following subject ("verlieren X").
                if modifier and (index - len(phrase) > 0 or not modifier.endswith("en")) \
                        and modifier[:1].islower() and modifier.casefold() not in _FILLER and len(modifier) > 3 \
                        and modifier.casefold() not in _LEAD and re.search(r"(?:e|en|er|es)$", modifier):
                    # "künstliche Intelligenz": the adjective is part of the name (acronym "KI").
                    forms.add(_fold(modifier[0] + phrase[-1][0]))
                    predicate.discard(modifier.casefold())
                if len(phrase) > 1:
                    forms.add("".join(_fold(word)[0] for word in phrase))
                entities.append(Entity(" ".join(phrase), phrase[-1], frozenset(forms)))
            elif lower not in _FILLER and lower not in _LEAD and len(lower) >= 3:
                predicate.add(lower)
            index += 1
        # Adjectives absorbed into a name are not the asked relation.
        absorbed = {form for entity in entities for form in entity.forms}
        predicate = {word for word in predicate if _fold(word) not in absorbed}
    else:
        content = [token for token in tokens if token.casefold() not in _FILLER and token.casefold() not in _LEAD and len(token) >= 3]
        # In a copular property question the final pre-condition word is the
        # asked state, rather than another subject entity.
        property_clause = re.match(r"(?i)^\s*why\s+(?:is|are)\s+(.+?)(?:\s+(?:when|while|if|during)\b|[?]|$)", text)
        if property_clause:
            state = _TOKEN.findall(property_clause.group(1))[-1].casefold()
            predicate.add(state)
            content = [token for token in content if token.casefold() != state]
        elif qtype in {"why", "how"}:
            # Action words are the requested event, not extra entities. Keep
            # their relation in non-copular English questions as in German.
            actions = {token.casefold() for token in content if PROCESS_PATTERN.fullmatch(token)
                       or _OPERATION.fullmatch(token)
                       or any(pattern.fullmatch(token) for pattern in _PREDICATE_EQUIVALENTS)}
            predicate.update(actions)
            content = [token for token in content if token.casefold() not in actions]
        entities = [Entity(token, token, frozenset()) for token in content]
    terms = frozenset(words(text))
    condition_terms: set[str] = set()
    for match in _CONDITION_CLAUSE.finditer(text):
        condition_terms.update(words(match.group(1)))
    # A participial modifier is a compact condition without an explicit
    # clause: "injured cats" / "verletzte Katzen".
    for index, token in enumerate(tokens[:-1]):
        if (
            lang == "en"
            and re.search(r"(?i)(?:ed|en)$", token)
            and token.casefold() not in _FILLER
            and token.casefold() not in _LEAD
        ):
            condition_terms.add(token.casefold())
        if lang == "de" and tokens[index + 1][:1].isupper() and re.search(r"(?i)(?:te|ten|ter|tes)$", token):
            condition_terms.add(token.casefold())
    return QuestionFrame(
        question=text,
        qtype=qtype,
        language=lang,
        entities=tuple(entities),
        predicate=frozenset(predicate),
        terms=terms,
        explanation_asked=explanatory or answer_mode(text) == "explanation" and qtype in {"why", "how"},
        asks_observation=bool(_OBSERVER.search(text) or _DETECTION.search(text)),
        asks_naming=bool(_NAMING.search(text)) or bool(re.search(r"(?i)\b(?:heißt|genannt|called|named)\b", text)),
        relation=_relation(qtype, text),
        extra={"condition_terms": sorted(condition_terms)},
    )


def _relation(qtype: str, question: str) -> str:
    if qtype == "why":
        return "purpose" if _INTENTIONAL_ACTION.search(question) else "cause"
    return {"how": "mechanism", "can": "capability", "when": "time", "where": "place", "what": "identity"}.get(qtype, "other")


# Small equivalence set for the most common distinguishing modifier, a position: "in der Mitte"
# is answered by centre words and by the outer/inner contrast that explains it.
_POSITION_CENTRE = re.compile(
    r"(?i)^(?:mitte|mittler\w*|mittelpunkt|zentrum|zentral\w*|innere\w*|innen\w*|inner\w*|kern\w*|center|centre|"
    r"middle|core|inside|interior)$"
)
_POSITION_EQUIVALENT = re.compile(
    r"(?i)\b(?:mitte|mittler\w*|mittelpunkt|zentrum|zentral\w*|innere\w*|innen\w*|inner\w*|kern\w*|tote\w* zone|kalte\w* stelle\w*|"
    r"rand\w*|außen|äußer\w*|oberfläche\w*|ungleichmäßig\w*|center|centre|middle|core|inside|interior|cold spots?|dead zones?|"
    r"edges?|outer|outside|surface|uneven\w*)\b"
)


def distinguishing_entity(frame: QuestionFrame) -> Entity | None:
    """The question's distinguishing modifier: its last-named entity, closest to the asked state
    ("Essen in der Mikrowelle in der *Mitte* kalt").  Only when the entity quota would otherwise let
    an answer skip it (three or more entities), and not when it is coordinated ("Bilder und Videos")."""
    if len(frame.entities) < 3 or frame.required_entities >= len(frame.entities):
        return None
    last = frame.entities[-1]
    before = re.search(rf"(\w+)\s+{re.escape(last.name.split()[0])}\b", frame.question)
    if before and before.group(1).casefold() in {"und", "oder", "and", "or", "sowie"}:
        return None
    return last


def covers_distinguishing(frame: QuestionFrame, text: str, context: str = "") -> bool:
    entity = distinguishing_entity(frame)
    if entity is None:
        return True
    if entity.name in entity_coverage(frame, f"{text} {context}")[1]:
        return True
    return bool(_POSITION_CENTRE.match(_fold(entity.head)) and _POSITION_EQUIVALENT.search(text))


def covers_conditions(frame: QuestionFrame, text: str) -> bool:
    """Whether the answer itself retains the question's explicit condition.

    Paragraph context may establish topic relevance, but an observation near a
    generic mechanism must not make that mechanism answer a conditional why.
    """
    required = set(frame.extra.get("condition_terms") or [])
    if not required:
        return True
    answer_words = words(text)
    return all(
        any(inflects(term, candidate) or inflects(candidate, term) for candidate in answer_words)
        for term in required
    )


def entity_coverage(frame: QuestionFrame, text: str) -> tuple[int, list[str]]:
    tokens = _TOKEN.findall(str(text or ""))
    others = {entity.head for entity in frame.entities} | set(frame.predicate)
    acronyms = _acronyms(tokens)
    covered = [entity.name for entity in frame.entities if entity.matched(tokens, others, acronyms)]
    return len(covered), covered


def _new_content(frame: QuestionFrame, text: str) -> int:
    """Content words the sentence adds beyond the question's own words (and connectives)."""
    asked = list(frame.terms) + [entity.head for entity in frame.entities]
    return sum(
        1 for word in words(text)
        if word not in _CONNECTIVES and not any(inflects(term, word) or inflects(word, term) for term in asked)
    )


def _shared_shape_issues(frame: QuestionFrame, text: str) -> list[str]:
    issues: list[str] = []
    if frame.explanation_asked and (_ADVICE.search(text) or _GENERIC_ADVICE.search(text)):
        issues.append("advice_not_explanation")
    if _AUTHOR_PURPOSE.search(text):
        issues.append("author_or_article_purpose")  # why the page was written, not why the phenomenon happens
    reported = _REPORTED_CAUSE.search(text)
    explains_finding = bool(reported and PROCESS_PATTERN.search(reported.group(1)))
    if _PERIPHERAL.search(text) and not explains_finding and not _PERIPHERAL.search(frame.question):
        issues.append("announcement_or_specification_not_explanation")
    if _OBSERVER.search(text) and not frame.asks_observation and not explains_finding:
        issues.append("observer_relation")  # X detects / analyses Y - not X doing Y
    for first, second in _OPPOSING_MODIFIERS:
        for asked, opposite in ((first, second), (second, first)):
            if re.search(rf"(?i)\b(?:{asked})\b", frame.question) and re.search(rf"(?i)\b(?:{opposite})\b", text) \
                    and not re.search(rf"(?i)\b(?:{asked})\b", text):
                issues.append("scope_modifier_mismatch")
    return issues


# A pronoun that can stand for an earlier-named thing ("Gebaut wurde sie 1961, ...").
_PRONOUN = re.compile(r"(?i)\b(?:sie|er|es|ihn|ihm|ihre[nmrs]?|seine[nmrs]?|diese[rsmn]?|it|its|they|them|their)\b")
_DETERMINER = {
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "einem", "einer", "im", "am", "beim", "zum", "zur", "the",
    "a", "an", "this", "these", "diese", "dieser", "dieses",
}
# The asked thing appearing only inside a reason clause: the sentence explains a consequence of it.
_CAUSE_CLAUSE = re.compile(r"(?i)\b(?:weil|because|denn)\b[^,.;:]*")


def _subject_phrase(sentence: str) -> list[str]:
    """The first noun phrase of a sentence or title - what it is about (German subject-first order):
    consecutive capitalised words after any determiner ("Die Berliner Mauer trennte" -> Berliner Mauer)."""
    phrase: list[str] = []
    for token in _TOKEN.findall(str(sentence or "")):
        if not phrase and token.casefold() in _DETERMINER:
            continue
        if token[:1].isupper():
            phrase.append(token)
            continue
        break
    return phrase


def resolves_pronoun(frame: QuestionFrame, text: str, antecedent: str) -> bool:
    """May a pronoun in ``text`` stand for the asked entity named in ``antecedent``?

    Only when ``text`` actually contains a pronoun and the antecedent (the
    immediately preceding sentence, or the page title for a paragraph-initial
    sentence) is *about* a missing entity: its first noun is that entity.
    "Die Berliner Mauer trennte ... Gebaut wurde sie 1961, ..." resolves;
    "Die Sowjetunion beobachtete die Mauer genau. Sie wollte ..." does not.
    """
    if not antecedent or not _PRONOUN.search(text):
        return False
    subject = _subject_phrase(antecedent)
    if not subject:
        return False
    _count, covered = entity_coverage(frame, text)
    others = {entity.head for entity in frame.entities} | set(frame.predicate)
    return any(
        entity.name not in covered and entity.matched(subject, others, _acronyms(subject))
        for entity in frame.entities
    )


_PROPERTY_REFERENCE = re.compile(
    r"(?i)\b(?:seine?\w*|ihre?\w*|diese\w*|its|their|this|that)\s+"
    r"(?:eigenschaft|zustand|erscheinung|farbe|färbung|property|state|appearance|colou?r|coloration)\b"
)



def covers_predicate(frame: QuestionFrame, text: str, antecedent: str = "") -> bool:
    """A causal core explains the asked state, rather than any effect on its entity.

    A property reference may inherit the state from its actual preceding
    sentence; entity co-occurrence alone never supplies the missing predicate.
    Existing spatial contrasts retain their distinguishing-condition semantics.
    """
    if not frame.predicate or relation_hits(frame, text):
        return True
    if any(pattern.search(frame.question) and pattern.search(text) for pattern in _PREDICATE_EQUIVALENTS):
        return True
    if frame.qtype == "how" and _OPERATION.search(frame.question) and PROCESS_PATTERN.search(text):
        return True
    if distinguishing_entity(frame) is not None and _POSITION_EQUIVALENT.search(text) and covers_distinguishing(frame, text):
        return True
    return bool(antecedent and _PROPERTY_REFERENCE.search(text) and relation_hits(frame, antecedent)
                and entity_coverage(frame, antecedent)[0] >= frame.required_entities)


def core_issues(frame: QuestionFrame, text: str, antecedent: str = "", *, context: str = "") -> list[str]:
    """Why ``text`` cannot be the core answer to ``frame`` (empty: it can).

    ``antecedent`` is what a pronoun in ``text`` may refer to (see ``resolves_pronoun``).
    """
    issues = _shared_shape_issues(frame, text)
    resolved = antecedent if resolves_pronoun(frame, text, antecedent) else ""
    # Local source context may resolve a reference or a nominal cause of an
    # already-named property. Mere co-occurrence on a page does not suffice.
    if context and (_LOCAL_REFERENCE.search(text) or _CAUSE_SUBJECT.search(text)) \
            and _context_linked(frame, text, context) and covers_predicate(frame, text):
        resolved = f"{resolved} {context}"
    covered, _names = entity_coverage(frame, f"{text} {resolved}")
    if frame.entities and covered < frame.required_entities:
        issues.append("entity_mismatch")
    if "entity_mismatch" not in issues and not covers_distinguishing(frame, text, resolved):
        # "Mikrowellen erwärmen Lebensmittel ..." explains the parent topic, not why the *centre* stays cold.
        issues.append("misses_distinguishing_condition")
    if "entity_mismatch" not in issues and not covers_conditions(frame, f"{text} {resolved}"):
        issues.append("misses_question_condition")
    if frame.qtype in {"why", "how"}:
        if frame.qtype == "how" and _OPERATION.search(frame.question) and not PROCESS_PATTERN.search(text):
            issues.append("no_operational_process")
        if frame.relation != "purpose" and not covers_predicate(frame, text, antecedent):
            issues.append("predicate_mismatch")
        if not _CAUSAL.search(text) and not (frame.relation == "purpose" and _PURPOSE.search(text)):
            issues.append("no_cause_or_mechanism")
        if _NAMING.search(text) and not frame.asks_naming:
            issues.append("naming_not_cause")
        if _new_content(frame, text) < 3:
            issues.append("restates_phenomenon")
        effect = _CAUSE_CLAUSE.sub(" ", text)
        if effect != text and "entity_mismatch" not in issues and frame.entities:
            effect_covered, _ = entity_coverage(frame, f"{effect} {resolved if _PRONOUN.search(effect) else ''}")
            if effect_covered < frame.required_entities:
                issues.append("asked_thing_is_the_cause_not_the_effect")
        issues += relation_issues(frame, text, resolved)
    elif frame.qtype == "can" or (frame.qtype == "other" and frame.predicate):
        if frame.predicate and not any(
            inflects(term, token) or inflects(token, term) for term in frame.predicate for token in _TOKEN.findall(text)
        ):
            issues.append("predicate_mismatch")
    elif frame.qtype == "when" and not _DATE.search(text):
        issues.append("no_time")
    return issues


def answer_fit(frame: QuestionFrame, text: str) -> int:
    """How directly an eligible answer states the asked relation (ranking among answers): an explicit
    purpose ("um ... zu", "damit", "Ziel") for an action outranks a mere reason clause, plus the asked
    predicate.  (The distinguishing modifier is an eligibility rule, not a bonus: it must not
    outrank corroboration among answers that all cover it.)"""
    fit = relation_hits(frame, text)
    if frame.relation == "purpose" and re.search(r"(?i)\bum\b[^.;:]{1,80}?\bzu\s+\w+|\bdamit\b|\bziel\w*|\bin order to\b|\bto (?:prevent|stop)\b", text):
        fit += 2
    return fit


def relation_hits(frame: QuestionFrame, text: str) -> int:
    """How many of the asked predicate words the sentence states (a tie-breaker among answers)."""
    tokens = _TOKEN.findall(text)
    return sum(1 for term in frame.predicate if any(inflects(term, token) or inflects(token, term) for token in tokens))


def _context_linked(frame: QuestionFrame, text: str, context: str) -> bool:
    """A reference or shared process participant, not an unrelated sentence near the topic."""
    if not context or not PROCESS_PATTERN.search(text) and not _CAUSAL.search(text):
        return False
    if _LOCAL_REFERENCE.search(text) or _CAUSE_SUBJECT.search(text):
        return True
    # A nominalized process with a component/result in the local explanation.
    if re.match(r"(?i)^\s*\w+(?:ation|tion|ung)\b", text) and PROCESS_PATTERN.search(text):
        return True
    # A component can explain the named whole without repeating it. It must
    # occur elsewhere in the local evidence too, independently of this sentence.
    outside = context.replace(text, " ")
    participants = [word for word in _TOKEN.findall(text)
                    if word[:1].isupper() and word.casefold() not in _DETERMINER]
    return any(inflects(word, other) for word in participants for other in _TOKEN.findall(outside))


def topical_issues(frame: QuestionFrame, text: str, context: str = "") -> list[str]:
    """Why ``text`` (read with its paragraph) cannot support this question at all."""
    issues = _shared_shape_issues(frame, text)
    covered, _names = entity_coverage(frame, f"{text} {context}")
    if frame.entities and covered < frame.required_entities:
        issues.append("entity_mismatch")
    return issues


def mechanism_issues(frame: QuestionFrame, text: str, context: str = "") -> list[str]:
    """A mechanism step must be causal, on the asked entities (with its paragraph), not advice/naming,
    and state the requested relation (a purpose for an intentional action, not its result)."""
    linked = context if _context_linked(frame, text, context) else ""
    issues = topical_issues(frame, text, linked)
    if frame.relation != "purpose" and not covers_predicate(frame, text) and not (
        PROCESS_PATTERN.search(text) and covers_predicate(frame, context)
    ):
        issues.append("predicate_mismatch")
    if "entity_mismatch" not in issues and (
        not covers_distinguishing(frame, text) or not covers_conditions(frame, text)
    ):
        issues.append("misses_question_condition")
    if not _CAUSAL.search(text):
        issues.append("no_cause_or_mechanism")
    if _NAMING.search(text) and not frame.asks_naming:
        issues.append("naming_not_cause")
    issues += relation_issues(frame, text, context)
    return issues


def relation_issues(frame: QuestionFrame, text: str, context: str = "") -> list[str]:
    """For an intentional action ("Warum wurde X gebaut?") the sentence must state its purpose, motive or
    reason; "X wurde gebaut, sodass Y" states what building X caused.  (For a phenomenon the effect clause
    of "A, deshalb B" usually *is* the explanation, so no direction check applies there.)"""
    if frame.relation == "purpose" and not _PURPOSE.search(text):
        return ["consequence_not_purpose" if _RESULT.search(text) else "no_purpose_or_reason"]
    if frame.relation == "purpose" and frame.entities:
        # "Weil der Mauerbau Familien trennte, versuchten viele zu fliehen": the asked action is the
        # *reason* of something else (action -> consequence), not the thing being explained.
        for clause in _CAUSE_CLAUSE.findall(text):
            if entity_coverage(frame, clause)[0] or relation_hits(frame, clause):
                return ["asked_action_is_the_cause"]
    return []
