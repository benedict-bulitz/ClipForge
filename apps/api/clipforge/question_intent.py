"""Question intent: what the user actually asks, before anything is researched.

One canonical contract, stored as ``intent["question_intent"]`` and carried
by the Story Arc's question contract.  It never replaces the user's words
(``original_question``); it adds the interpreted meaning that research,
fact selection, hook, script, review and answer sufficiency must serve.

It is derived before research from grammar alone - who acts (grammatical
agency), whether a contrast clause denies intention ("obwohl wir es gar
nicht wollten") or denies user action ("obwohl ich nichts angeklickt
habe") - because no model call runs before research.  The existing AI
planner call later confirms or corrects it (``merge_planner_intent``); a
correction that changes the meaning makes the bounded research retry search
the corrected interpretation.

"Warum öffnen *wir* TikTok, obwohl wir es nicht *wollten*?" asks about
human behaviour; "Warum öffnet *sich* TikTok, obwohl ich nichts angeklickt
habe?" asks about the app.  They never resolve to the same intent.
"""
from __future__ import annotations

import re
from typing import Any

from .story_arc import comparison_sides, is_explanatory_question

INTENT_VERSION = 1
QUESTION_TYPES = ("behavioral_why", "technical_why", "causal_why", "how", "comparison", "factual")

# Grammar, not topic vocabulary: who acts.
_PERSON_SUBJECT = re.compile(
    r"(?i)\b(?:wir|ich|du|man|ihr|menschen|leute|viele|kinder|we|i|you|people|humans|kids)\b"
)
_REFLEXIVE_SELF = re.compile(r"(?i)\b(?:\w+(?:t|en))\s+sich\b|\b(?:by itself|on its own|itself)\b|\bvon (?:selbst|allein)\b")
_POSSESSIVE_THING = re.compile(r"(?i)\b(?:mein|meine|dein|deine|unser|unsere|my|your|our)\s+(\w+)")
_CONTRAST = re.compile(r"(?i)(?:,\s*|\b)(?:obwohl|obgleich|trotzdem|although|even though|though)\b(.*)$")
# The contrast denies intention, desire or need (a mental state)...
_MENTAL_STATE = re.compile(
    r"(?i)\b(?:woll\w*|will|möcht\w*|vorhatt\w*|vorhab\w*|beabsichtig\w*|absicht\w*|lust|hunger|hungrig|satt|"
    r"müde|bedürfnis|brauch\w*|eigentlich|bewusst|want\w*|wanted|intend\w*|mean\w*|hungry|full|need\w*|plan\w*|"
    r"decid\w*|entschied\w*|entschloss\w*)\b"
)
# ...or denies / minimises the user's own action (then something else acts).
_USER_ACTION = re.compile(
    r"(?i)\b(?:nichts|nicht|nur|kein\w*|nothing|only|just|never|didn't|did not|haven't)\b[^.?!]*?"
    r"\b(?:angeklickt|geklickt|klick\w*|getippt|tipp\w*|gedrückt|drück\w*|berührt|geöffnet|eingeschaltet|"
    r"click\w*|tap\w*|touch\w*|press\w*|open\w*)\b"
)
_HOW = re.compile(r"(?i)^\s*(?:wie|how)\b")

# What each interpretation is about (research direction and the evidence
# domain an answer must come from).  Small and domain-general on purpose.
DOMAINS = {
    "behavioral_why": {
        "de": "menschliches Verhalten: Gewohnheiten, automatische Handlungen, Auslöser und Belohnungen",
        "en": "human behaviour: habits, automatic actions, cues and rewards",
    },
    "technical_why": {
        "de": "technisches Verhalten von App, Gerät, Browser oder Betriebssystem",
        "en": "technical behaviour of the app, device, browser or operating system",
    },
}
RESEARCH_TERMS = {
    "behavioral_why": {"de": "Psychologie Gewohnheit automatisches Verhalten", "en": "psychology habit automatic behavior"},
    "technical_why": {"de": "technisch Funktionsweise Einstellung", "en": "technical how it works setting"},
}
EXCLUDED = {
    "behavioral_why": {
        "de": "technische Erklärung: App, Gerät, Link oder System lösen das Öffnen aus",
        "en": "technical explanation: the app, device, link or system triggers it",
    },
    "technical_why": {
        "de": "Verhaltenserklärung: die Person tut es aus Gewohnheit",
        "en": "behavioural explanation: the person does it out of habit",
    },
}
# Evidence-domain cues (backstop for the AI judgement, never the answer).
DOMAIN_CUES = {
    "behavioral_why": re.compile(
        r"(?i)\b(?:gewohnheit\w*|gewöhn\w*|routine\w*|automatisch\w*|unbewusst\w*|reflex\w*|impuls\w*|belohn\w*|"
        r"dopamin\w*|gehirn\w*|langeweile|gelangweilt|reiz\w*|auslöser\w*|verhalten\w*|griff\w*|zwang\w*|sucht\w*|"
        r"emotion\w*|gefühl\w*|stress\w*|habit\w*|automatic\w*|unconscious\w*|impulse\w*|reward\w*|brain\w*|"
        r"boredom|bored|cue\w*|behaviou?r\w*|craving\w*|compuls\w*)\b"
    ),
    "technical_why": re.compile(
        r"(?i)\b(?:link\w*|browser\w*|url\w*|installi\w*|installation|betriebssystem\w*|einstellung\w*|software|"
        r"programm\w*|server\w*|code|app-store|deep[- ]?link\w*|universal[- ]?link\w*|weiterleit\w*|anmeld\w*|"
        r"login|redirect\w*|operating system|settings?|install\w*)\b"
    ),
}


def _clauses(question: str) -> tuple[str, str]:
    """(main clause, contrast clause)."""
    match = _CONTRAST.search(question)
    if not match:
        return question, ""
    return question[: match.start()], match.group(1)


def _main_actor(main: str) -> str:
    body = re.sub(r"(?i)^\s*(?:warum|wieso|weshalb|weswegen|why|how come|wie)\s+", "", main.strip())
    words = body.split()
    head = " ".join(words[:3])
    if _REFLEXIVE_SELF.search(main):
        return "object"
    if _PERSON_SUBJECT.search(head):
        return "person"
    if _POSSESSIVE_THING.search(head):
        return "object"
    return "unknown"


def interpret_question(question: str, language: str = "de") -> dict[str, Any]:
    """The deterministic Question Intent Contract (grammar only; never raises)."""
    text = " ".join(str(question or "").split())
    lang = "de" if str(language or "").startswith("de") else "en"
    main, contrast = _clauses(text)
    actor = _main_actor(main)
    denies_action = bool(contrast and _USER_ACTION.search(contrast))
    denies_intention = bool(contrast and _MENTAL_STATE.search(contrast)) and not denies_action
    candidates: list[dict[str, Any]] = []

    def add(kind: str, score: float, why: str) -> None:
        candidates.append({"question_type": kind, "score": round(score, 2), "signal": why})

    if len(comparison_sides(text)) == 2:
        add("comparison", 0.9, "two alternatives")
    elif not is_explanatory_question(text):
        add("how" if _HOW.search(text) else "factual", 0.8, "no why/how question")
    else:
        if actor == "person" and denies_intention:
            add("behavioral_why", 0.9, "a person acts although the contrast denies intention")
            add("technical_why", 0.2, "the object could act on its own")
        elif actor == "object" and (denies_action or not contrast):
            add("technical_why", 0.85 if denies_action else 0.7, "the object acts (reflexive or non-person subject)")
            add("behavioral_why", 0.2, "the person could be acting")
        elif actor == "object" and denies_intention:
            add("technical_why", 0.6, "the object acts")
            add("behavioral_why", 0.45, "the contrast denies intention")
        elif actor == "person" and denies_action:
            add("technical_why", 0.55, "the person denies acting: something else acts")
            add("behavioral_why", 0.45, "the person is the subject")
        else:
            add("causal_why", 0.6, "a why question without agency signals")
    candidates.sort(key=lambda item: -item["score"])
    best = candidates[0]
    runner = candidates[1]["score"] if len(candidates) > 1 else 0.0
    confidence = "high" if best["score"] >= 0.8 and best["score"] - runner >= 0.4 else ("medium" if best["score"] - runner >= 0.15 else "low")
    kind = best["question_type"]
    return {
        "version": INTENT_VERSION,
        "source": "grammar",
        "original_question": text,
        "intended_question": _paraphrase(text, kind, lang),
        "question_type": kind,
        "actor": actor,
        "target_phenomenon": main.strip(" ,?") or text,
        "key_contrast_or_condition": contrast.strip(" ?.") or None,
        "expected_explanation_domain": (DOMAINS.get(kind) or {}).get(lang),
        "explicitly_excluded_interpretations": [EXCLUDED[kind][lang]] if kind in EXCLUDED else [],
        "confidence": confidence,
        "candidates": candidates,
        "ambiguous": confidence == "low" and len(candidates) > 1,
    }


def _paraphrase(question: str, kind: str, lang: str) -> str:
    stem = question.rstrip(" ?")
    if kind == "behavioral_why":
        return (f"{stem} – warum tun Menschen das, ohne es vorher bewusst zu beabsichtigen?" if lang == "de"
                else f"{stem} - why do people do this without consciously intending to?")
    if kind == "technical_why":
        return (f"{stem} – welcher technische Ablauf in App, Gerät oder System löst das aus?" if lang == "de"
                else f"{stem} - which technical process in the app, device or system causes it?")
    return question


def research_query(intent: dict[str, Any] | None, language: str = "de") -> str | None:
    """The research query for the interpreted question (None: use the raw prompt)."""
    if not isinstance(intent, dict):
        return None
    kind = str(intent.get("question_type") or "")
    terms = (RESEARCH_TERMS.get(kind) or {}).get("de" if str(language).startswith("de") else "en")
    if not terms:
        return None
    return f"{intent.get('target_phenomenon') or intent.get('original_question')} {terms}".strip()


def merge_planner_intent(intent: dict[str, Any], planner: dict[str, Any] | None) -> dict[str, Any]:
    """The existing AI planner's semantic judgement confirms or corrects the grammar.

    The original question is never replaced.  A different question type is a
    reinterpretation: the research done so far followed the old one.
    """
    if not isinstance(planner, dict) or planner.get("question_type") not in QUESTION_TYPES:
        return intent
    merged = {**intent}
    for key in ("intended_question", "question_type", "target_phenomenon", "key_contrast_or_condition", "expected_explanation_domain"):
        value = planner.get(key)
        if isinstance(value, str) and value.strip():
            merged[key] = value.strip()[:400]
    excluded = [str(item).strip()[:240] for item in planner.get("explicitly_excluded_interpretations") or [] if str(item).strip()]
    if excluded:
        merged["explicitly_excluded_interpretations"] = excluded[:4]
    if isinstance(planner.get("actor"), str) and planner["actor"].strip():
        merged["actor"] = planner["actor"].strip()[:80]
    merged["source"] = "planner"
    merged["confidence"] = planner.get("confidence") if planner.get("confidence") in {"high", "medium", "low"} else merged.get("confidence")
    merged["reinterpreted"] = merged["question_type"] != intent.get("question_type")
    merged["grammar_question_type"] = intent.get("question_type")
    merged["ambiguous"] = merged.get("confidence") == "low"
    return merged


def domain_alignment(intent: dict[str, Any] | None, texts: list[str]) -> dict[str, Any]:
    """Which interpretation an explanation serves (a backstop for the AI's semantic verdict).

    ``mismatch`` only when the explanation draws on the excluded
    interpretation's domain and not at all on the expected one.
    """
    kind = str((intent or {}).get("question_type") or "")
    other = {"behavioral_why": "technical_why", "technical_why": "behavioral_why"}.get(kind)
    if not other:
        return {"status": "not_applicable", "expected_hits": 0, "excluded_hits": 0}
    text = " ".join(texts)
    expected = len(DOMAIN_CUES[kind].findall(text))
    excluded = len(DOMAIN_CUES[other].findall(text))
    status = "mismatch" if excluded >= 2 and expected == 0 else ("aligned" if expected else "unclear")
    return {"status": status, "expected_hits": expected, "excluded_hits": excluded, "expected_domain": kind, "excluded_domain": other}
