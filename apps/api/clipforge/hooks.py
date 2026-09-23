"""Contextual hook candidates and deterministic truthfulness checks."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .narration import clean_narration_text

STRATEGIES = {"curiosity_gap", "counterintuitive_insight", "direct_reframe", "ego_challenge", "verified_statistic", "social_proof_or_trend", "high_stakes_consequence", "common_mistake", "evidence_insight", "hot_take", "direct_confrontation"}
_TREND_WORDS = re.compile(r"(?i)\b(?:currently|lately|increasingly|suddenly|trending|viral|adopt(?:ed|ion)|switching|more and more|aktuell|zunehmend|immer mehr|im trend)\b")
_PREVALENCE = re.compile(r"(?i)\b(?:most people|most creators|almost everyone|everyone|the most common mistake|everyone makes|experts usually|top performers|die meisten|fast alle|jeder|der häufigste fehler|die meisten menschen|experten machen meistens|erfolgreiche menschen)\b")
_NUMBER = re.compile(r"(?<!\w)(?:\d+(?:[.,]\d+)?\s*%?|\d+(?:[.,]\d+)?\s*(?:million|billion|milliarde[n]?|millionen?))\b", re.IGNORECASE)
_CLICHE = re.compile(r"(?i)(?:they don't want you to know|you(?:'|’)ve been lied to|nobody talks about this|this will change everything|experts hate this|everything you know is wrong|das wollen sie dir nicht sagen|du wurdest belogen)")
_ATTACK = re.compile(r"(?i)\b(?:lazy|stupid|idiot|loser|du bist faul|dumm|versager)\b")
_META = re.compile(r"(?i)\b(?:in this video|today we(?:'|’)re going to|here(?:'|’)s the answer|in diesem video|heute zeige ich)\b")
_META_FILLER = re.compile(r"(?i)(?:that is the key to the answer|that answers the question|that's the answer|das beantwortet die frage|genau das ist die antwort|hier ist der grund|deshalb ist die antwort|und genau das erklärt es)")
_OPENING_SPECIALIST_TERM = re.compile(r"(?i)^\s*(?:piloerektion)\b")
_STOP = {"the", "and", "why", "what", "how", "are", "is", "was", "were", "for", "from", "with", "that", "this", "your", "you", "der", "die", "das", "und", "warum", "wie", "ist", "sind", "für", "von", "mit", "dass", "dies"}


@dataclass(frozen=True)
class HookCandidate:
    strategy: str
    text: str
    score: float
    reason: str


def _question(intent: dict[str, Any]) -> str:
    question = clean_narration_text(intent.get("question") or "").strip().rstrip(".!?")
    return re.sub(r"(?i)^(?:please\s+)?(?:explain|tell me|show me|erkläre|erklaere|erzähl mir|erzaehl mir)\s+", "", question)


def _words(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-zäöüß]{3,}", text.casefold()) if word not in _STOP}


def _evidence_facts(facts: list[dict[str, Any]]) -> list[str]:
    return [clean_narration_text(fact.get("claim")) for fact in facts if clean_narration_text(fact.get("claim"))]


def _supported_numbers(facts: list[dict[str, Any]]) -> set[str]:
    return {m.group(0).lower().replace(",", ".") for f in facts if f.get("sources") and f.get("verification") == "source_attributed" for m in _NUMBER.finditer(str(f.get("claim") or ""))}


def _number_supported(text: str, facts: list[dict[str, Any]], intent: dict[str, Any] | None) -> bool:
    topic = _words(str((intent or {}).get("topic") or "") + " " + _question(intent or {}))
    for match in _NUMBER.finditer(text):
        number = match.group(0).lower().replace(",", ".")
        matches = [f for f in facts if f.get("sources") and f.get("verification") == "source_attributed" and number in str(f.get("claim") or "").lower().replace(",", ".")]
        if not matches:
            return False
        candidate_words = _words(text)
        if topic and not any(topic & _words(str(f.get("claim") or "")) and _words(str(f.get("claim") or "")) & candidate_words for f in matches):
            return False
    return True


def _topic_evidence(text: str, intent: dict[str, Any]) -> bool:
    topic = _words(str(intent.get("topic") or "") + " " + _question(intent))
    return bool(topic & _words(text)) or not topic


def _grounded_insight(claim: str, intent: dict[str, Any]) -> str:
    value = clean_narration_text(claim).strip()
    if len(value.split()) <= 24:
        return value.rstrip(".!?") + "."
    parts = [part.strip() for part in re.split(r"(?<=[,;—])\s+|\s+(?=(?:but|because|although|while|sondern|weil|obwohl|während)\s)", value, flags=re.IGNORECASE)]
    for part in parts:
        if 5 <= len(part.split()) <= 18 and re.search(r"\b(?:is|are|was|were|scatters|causes|can|does|wird|werden|ist|sind|kann|führt|verursacht|gestreut)\b", part, re.IGNORECASE) and not re.search(r"(?:\b(?:is|are|was|were|will|wird|werden|can|kann|and|but|because|although|sondern|weil|obwohl|dass|und|in|als|than)\s*)$", part, re.IGNORECASE):
            return part.rstrip(".!?,;:") + "."
    return value.rstrip(".!?") + "."


def hook_issues(text: str, facts: list[dict[str, Any]], *, body: str = "", intent: dict[str, Any] | None = None) -> list[str]:
    raw = str(text or "").strip()
    value = clean_narration_text(raw).strip()
    if not value:
        return ["meta_language"] if _META.search(raw) else ["empty"]
    issues: list[str] = []
    if _CLICHE.search(raw) or _CLICHE.search(value): issues.append("generic_clickbait")
    if _ATTACK.search(value): issues.append("personal_attack")
    if _META.search(raw) or _META.search(value): issues.append("meta_language")
    if _META_FILLER.search(value): issues.append("generic_meta_filler")
    if _OPENING_SPECIALIST_TERM.search(value): issues.append("jargon_first")
    if intent and (facts or body) and _is_question_echo(value, intent):
        issues.append("question_echo")
    if _PREVALENCE.search(value) and not _PREVALENCE.search(" ".join(_evidence_facts(facts))): issues.append("unsupported_prevalence")
    if _NUMBER.search(value) and not _number_supported(value, facts, intent): issues.append("unsupported_statistic")
    if _TREND_WORDS.search(value):
        evidence = " ".join(_evidence_facts(facts))
        if not _TREND_WORDS.search(evidence) or (intent and not _topic_evidence(value, intent)): issues.append("unsupported_trend")
    if len(value.split()) > 24: issues.append("too_long")
    if re.search(r"(?i)^\s*(?:hook|answer|detail|support|context)\s*[:—-]", value): issues.append("structural_label")
    if body:
        left, right = _words(value), _words(body)
        if left and right and len(left) / len(right) >= 0.8 and len(left & right) / len(right) >= 0.8: issues.append("repeats_body")
    return issues


def _is_question_echo(text: str, intent: dict[str, Any]) -> bool:
    """Reject empty question restatements while retaining genuinely new questions."""
    question = _question(intent).strip().casefold()
    value = text.strip().rstrip("?!.").casefold()
    if value == question:
        return True
    question_words = set(re.findall(r"[a-zäöüß]{2,}", question))
    text_words = set(re.findall(r"[a-zäöüß]{2,}", value))
    if len(question_words) < 2 or not text_words:
        return False
    overlap = len(question_words & text_words)
    if "?" not in text and overlap != len(question_words):
        return False
    return overlap / len(question_words) >= 0.75 and overlap / len(text_words) >= 0.55


def _plain_evidence_explanation(text: str, facts: list[dict[str, Any]], body: str) -> bool:
    """Identify a near-verbatim causal proposition that belongs in the body.

    This is deliberately structural, not a topic-specific vocabulary list. A
    fresh wording of a surprising relationship can still be an evidence insight;
    simply lifting the explanation from a fact or the body cannot.
    """
    candidate_words = _words(text)
    if len(candidate_words) < 2:
        return False
    references = [str(fact.get("claim") or "") for fact in facts]
    if body:
        references.append(body)
    for reference in references:
        reference_words = _words(reference)
        if reference_words and len(candidate_words & reference_words) / len(candidate_words) >= 0.8:
            return True
    return False


def _evidence_insight_has_attention_value(
    text: str, facts: list[dict[str, Any]], body: str, intent: dict[str, Any]
) -> bool:
    """Evidence insight is a hook only when it is more than the explanation."""
    if _plain_evidence_explanation(text, facts, body):
        return False
    evidence_words = set().union(*(_words(str(fact.get("claim") or "")) for fact in facts)) if facts else set()
    topic_words = _words(str(intent.get("topic") or "") + " " + _question(intent))
    return bool(_words(text) & (evidence_words | topic_words))


def _unlabelled_hook_has_mechanism(text: str, intent: dict[str, Any]) -> bool:
    """Keep legacy hook blocks from bypassing strategy-based eligibility."""
    return (
        ("?" in text and not _is_question_echo(text, intent))
        or _strategy_matches("direct_reframe", text)
        or _strategy_matches("counterintuitive_insight", text)
    )


def _candidate_score(strategy: str, text: str, intent: dict[str, Any], facts: list[dict[str, Any]], body: str) -> float:
    topic_words, text_words, body_words = _words(str(intent.get("topic") or "") + " " + _question(intent)), _words(text), _words(body)
    score = 20 + min(30, len(topic_words & text_words) * 7) + min(24, len(text_words & body_words) * 6)
    if body and text_words and len(text_words & body_words) / len(text_words) >= 0.8: score -= 30
    score += 10 if len(text.split()) <= 14 else -min(12, (len(text.split()) - 14) * 2)
    if _is_question_echo(text, intent) and (body or facts): score -= 70
    if strategy in {"counterintuitive_insight", "direct_reframe", "common_mistake", "high_stakes_consequence", "evidence_insight"} and body_words: score += 8
    if strategy == "verified_statistic": score += 14
    if strategy == "social_proof_or_trend": score += 8
    if strategy == "model_selected" and text_words & body_words:
        score += 18
    return score


def _strategy_matches(strategy: str, text: str) -> bool:
    """Keep model metadata aligned with the rhetoric actually present in the text."""
    value = text.casefold()
    if strategy == "counterintuitive_insight":
        return bool(re.search(r"\b(?:but|although|despite|opposite|rather than|instead of|nicht|sondern|obwohl|statt)\b", value))
    if strategy == "direct_reframe":
        return bool(re.search(r"\b(?:not|nicht)\b[^.!?]{0,80}\b(?:but|sondern|rather|instead|sondern)\b", value)) or bool(re.search(r"\b(?:less about|more about|statt)\b", value))
    if strategy == "ego_challenge":
        return bool(re.search(r"(?:\?|\b(?:can you|do you|could you|kannst du|erkennst du|schaffst du)\b)", value))
    if strategy == "common_mistake":
        return bool(re.search(r"\b(?:mistake|error|fail(?:s|ed|ure)?|misconception|fehler|irrtum|scheitert|scheitern)\b", value))
    if strategy == "high_stakes_consequence":
        return bool(re.search(r"\b(?:risk|cost|lose|prevents?|leads?|consequence|impact|risiko|kostet|verhindert|führt|folge|schaden)\b", value))
    if strategy == "verified_statistic":
        return bool(_NUMBER.search(text))
    if strategy == "social_proof_or_trend":
        return bool(_TREND_WORDS.search(text))
    if strategy in {"hot_take", "direct_confrontation"}:
        return bool(re.search(r"\b(?:not|nicht|wrong|falsch|but|sondern|actually|eigentlich)\b", value))
    return True


def generate_hook_candidates(intent: dict[str, Any], facts: list[dict[str, Any]], *, body: str = "") -> list[HookCandidate]:
    question, topic, german = _question(intent), str(intent.get("topic") or _question(intent)).strip(" .?!"), intent.get("language") == "de"
    practical = any(word in (question + " " + topic).casefold() for word in ("sav", "learn", "habit", "mistake", "sparen", "lernen", "fehler", "routine"))
    evidence = _evidence_facts([fact for fact in facts if fact.get("sources") or fact.get("verification") == "source_attributed"])
    raw: list[tuple[str, str, str]] = []
    if question: raw.append(("curiosity_gap", question + "?", "Question fallback."))
    if evidence:
        insight = _grounded_insight(evidence[0], intent)
        contrast = re.search(r"(?i)\b(?:not|instead|rather|but|although|despite|opposite|nicht|sondern|doch|obwohl|statt)\b", evidence[0])
        if contrast:
            if re.search(r"(?i)\b(?:not|nicht)\b.*\b(?:but|sondern)\b", evidence[0]):
                raw.append(("direct_reframe", insight, "Fact contains an explicit not-X-but-Y reframe."))
            else:
                raw.append(("counterintuitive_insight", insight, "Fact contains a supported contrast or reversal."))
        else:
            raw.append(("evidence_insight", insight, "Complete sourced fact used without overstating its rhetoric."))
        if practical and re.search(r"(?i)\b(?:mistake|error|fail|misconception|fehler|irrtum|scheitern)\b", evidence[0]):
            raw.append(("common_mistake", insight, "Practical insight without prevalence language."))
        if re.search(r"(?i)\b(?:can|leads?|risk|cost|before|verhindert|führt|risiko|kostet|consequence|folge)\b", evidence[0]):
            consequence_source = re.split(r"[;—]\s*", evidence[0], maxsplit=1)[-1]
            if re.match(r"(?i)^(?:that|which)\s+", consequence_source):
                subject = re.search(r"(?i)\b(?:but|sondern)\s+(.+?)(?:;|,|\.)", evidence[0])
                if subject:
                    consequence_source = subject.group(1).strip() + " can " + re.sub(r"(?i)^(?:that|which)\s+can\s+", "", consequence_source).strip()
            consequence = _grounded_insight(consequence_source, intent)
            raw.append(("high_stakes_consequence", consequence, "Names a proportionate evidenced consequence."))
    if practical:
        if "sav" in topic.casefold() or "spar" in topic.casefold():
            mistake = "Ein Sparschritt kann scheitern, bevor das Geld das Konto erreicht." if german else "A saving decision can fail before the money reaches the account."
            raw.append(("common_mistake", mistake, "Practical insight without prevalence language."))
        raw.append(("ego_challenge", f"Kannst du den entscheidenden Schritt bei {topic} erkennen?" if german else f"Can you spot the key step in {topic}?", "Self-reflection without shame."))
    claim = next((item for item in evidence if _NUMBER.search(item) and _topic_evidence(item, intent)), "")
    if claim and _supported_numbers(facts): raw.append(("verified_statistic", claim, "Relevant sourced number."))
    trend = next((item for item in evidence if _TREND_WORDS.search(item) and _topic_evidence(item, intent)), "")
    if trend: raw.append(("social_proof_or_trend", trend, "Relevant sourced trend."))
    result: list[HookCandidate] = []
    seen: dict[str, HookCandidate] = {}
    priority = {"direct_reframe": 8, "direct_confrontation": 8, "hot_take": 7, "verified_statistic": 7, "social_proof_or_trend": 6, "common_mistake": 5, "high_stakes_consequence": 4, "counterintuitive_insight": 3, "evidence_insight": 2, "ego_challenge": 1, "curiosity_gap": 0}
    for strategy, text, reason in raw:
        issues = hook_issues(text, facts, body=body, intent=intent)
        if issues and not (strategy == "evidence_insight" and issues == ["repeats_body"]):
            continue
        if strategy == "evidence_insight" and not _evidence_insight_has_attention_value(
            text, facts, body, intent
        ):
            continue
        candidate = HookCandidate(strategy, text, _candidate_score(strategy, text, intent, facts, body), reason)
        key = " ".join(text.casefold().split())
        previous = seen.get(key)
        if previous is None or priority.get(strategy, 0) > priority.get(previous.strategy, 0):
            seen[key] = candidate
    result.extend(seen.values())
    return result


def select_hook_candidate(intent: dict[str, Any], facts: list[dict[str, Any]], *, body: str = "", existing: str | None = None, model_candidates: list[dict[str, Any]] | None = None) -> HookCandidate | None:
    candidates = generate_hook_candidates(intent, facts, body=body)
    seen_text: set[str] = {" ".join(candidate.text.casefold().split()) for candidate in candidates}
    for item in model_candidates or []:
        text = str(item.get("text") or "").strip()
        if text and not hook_issues(text, facts, body=body, intent=intent):
            normalized = " ".join(text.casefold().split())
            if any(len(set(normalized.split()) & set(other.split())) / max(1, min(len(normalized.split()), len(other.split()))) >= 0.85 for other in seen_text):
                continue
            seen_text.add(normalized)
            declared = str(item.get("strategy") or "")
            aliases = {"shock_number": "verified_statistic", "social_proof": "social_proof_or_trend", "fomo": "social_proof_or_trend", "direct_challenge": "direct_confrontation"}
            strategy = aliases.get(declared, declared) if declared in STRATEGIES or declared in aliases else "evidence_insight"
            if strategy in STRATEGIES and not _strategy_matches(strategy, text): strategy = "evidence_insight"
            if strategy == "curiosity_gap" and "?" not in text:
                strategy = "evidence_insight"
            if strategy == "evidence_insight" and not _evidence_insight_has_attention_value(
                text, facts, body, intent
            ):
                continue
            score = _candidate_score(strategy, text, intent, facts, body)
            # The director received the manifest and can supply an original rhetorical
            # hook; give a modest preference to that valid alternative over a literal
            # deterministic fact sentence, never over an eligibility failure.
            evidence_words = set().union(*(_words(str(fact.get("claim") or "")) for fact in facts)) if facts else set()
            rhetorical = {
                "hot_take", "direct_confrontation", "direct_reframe", "ego_challenge",
                "common_mistake", "counterintuitive_insight", "high_stakes_consequence",
                "curiosity_gap", "verified_statistic", "social_proof_or_trend",
            }
            topic_words = _words(str(intent.get("topic") or "") + " " + _question(intent))
            if strategy in rhetorical and (
                len(_words(text) & evidence_words) >= 2
                or len(_words(text) & topic_words) >= 2
            ):
                score += 16
            candidates.append(HookCandidate(strategy, text, score, "Structured Content Director candidate."))
    if (
        existing
        and " ".join(existing.casefold().split()) not in seen_text
        and not hook_issues(existing, facts, body=body, intent=intent)
        and not _plain_evidence_explanation(existing, facts, body)
        and _unlabelled_hook_has_mechanism(existing, intent)
    ):
        candidates.append(HookCandidate("model_selected", clean_narration_text(existing).strip(), _candidate_score("model_selected", existing, intent, facts, body), "Model candidate competes with deterministic candidates."))
    return max(candidates, key=lambda item: item.score, default=None)


def select_hook(intent: dict[str, Any], facts: list[dict[str, Any]], *, body: str = "", existing: str | None = None, model_candidates: list[dict[str, Any]] | None = None) -> str | None:
    candidate = select_hook_candidate(intent, facts, body=body, existing=existing, model_candidates=model_candidates)
    return candidate.text if candidate else None
