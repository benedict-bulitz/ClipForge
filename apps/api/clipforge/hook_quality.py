"""Hook Quality V3: what the viewer gets in the first one to three seconds.

A compact, deterministic quality model layered on the existing Triple Hook
authority (``verbal_hook`` scores the spoken hook, ``triple_hook`` the three
channels together).  It adds no provider call: the existing candidates are
filtered and ranked harder.

The opening is judged where the swipe decision happens — the words heard in
the first second(s), derived from the narration speaking rate instead of a
fixed length rule:

A. immediate clarity      the topic is recognisable inside the first-second window
B. curiosity gap          a real unanswered question (``verbal_hook`` curiosity)
C. specificity            concrete entities/actions (``verbal_hook`` useful_information)
D. novelty                something beyond what the question already says
E. information density    every word earns its place (no setup, no filler)
F. complementarity        voice, image and text each add something
G. payoff integrity       the story pays the promise (``triple_hook`` payoff checks)
H. scroll-stop potential  a cold viewer gets a concrete reason to stay early

Gates are grammar families (German and English), never topic vocabulary.  A
gate only rejects when it is confident (a setup formula, an empty teaser, a
restated question with nothing added); weaker signals only lower scores.
"""

import re
from typing import Any

from .hooks import _OPEN_QUESTION, _UNEXPLAINED_TURN, _WITHHELD

VERSION = 3
# Narration speaking rate (words per minute) shared with the script timing.
SPEAKING_RATE_WPM = 165
# What is heard before the swipe decision: the topic must be clear inside the
# first-second window, something new inside the opening window.
FIRST_SECOND_SECONDS = 1.5
OPENING_SECONDS = 3.0
# A word this long has to be decoded before it is understood (by ear).
HARD_WORD = 11


def window_words(seconds: float, wpm: int = SPEAKING_RATE_WPM) -> int:
    """How many spoken words fit into ``seconds`` at the narration rate."""
    return max(2, round(wpm / 60 * seconds))


# ---------------------------------------------------------------------------
# Grammar families (any topic)
# ---------------------------------------------------------------------------

# Setup formulas: they announce a fact or a question instead of starting it.
SETUP_OPENER = re.compile(
    r"(?i)^\s*(?:(?:so|okay|ok|also|na|nun|hey)\s*[,:!]?\s+)?(?:"
    r"wusstest\s+du|"
    r"wei(?:ß|ss)t\s+du(?:\s+(?:eigentlich|schon|was))?\s*,?\s*(?:warum|wieso|weshalb|was|wie|dass|ob)\b|"
    r"hast\s+du\s+dich\s+(?:(?:schon|eigentlich)\s+)?(?:jemals|mal|je|einmal)\s+gefragt|"
    r"hast\s+du\s+(?:(?:schon|eigentlich)\s+)?(?:mal|jemals|je)\s+(?:darüber\s+)?nachgedacht|"
    r"du\s+hast\s+dich\s+(?:bestimmt|sicher|sicherlich)\s+(?:schon\s+)?(?:mal\s+)?gefragt|"
    r"hier\s+(?:ist|kommt)\s+ein\s+(?:interessanter|spannender|verrückter|cooler|kurioser|krasser)\s+fakt|"
    r"heute\s+(?:schauen|sehen|gucken|klären|lernen|beantworten|erklären|zeigen|besprechen|reden|sprechen)\s+wir|"
    r"heute\s+(?:erkläre|zeige|kläre|beantworte)\s+ich|"
    r"heute\s+geht\s+es\s+um|"
    r"in\s+diesem\s+(?:video|short|clip|beitrag)|"
    r"lass(?:t)?\s+uns\s+(?:mal\s+)?(?:anschauen|ansehen|klären|herausfinden|schauen|darüber|über|einen\s+blick)|"
    r"schauen\s+wir\s+uns\s+(?:mal\s+)?an|"
    r"viele\s+(?:menschen\s+|leute\s+|von\s+uns\s+)?(?:fragen\s+sich|wundern\s+sich|haben\s+sich\s+(?:schon\s+)?(?:mal\s+)?gefragt)|"
    r"(?:eine|die)\s+frage,?\s+die\s+sich\s+(?:viele|jeder|alle)|"
    r"die\s+frage\s+(?:ist|lautet)|"
    r"fun\s+fact|did\s+you\s+know|"
    r"have\s+you\s+ever\s+(?:wondered|asked\s+yourself|thought\s+about|noticed)|ever\s+wondered|"
    r"you(?:'|’)ve\s+probably\s+(?:wondered|asked\s+yourself)|you\s+might\s+(?:be\s+wondering|wonder|have\s+wondered)|"
    r"here(?:'|’)?s\s+an?\s+(?:interesting|fun|crazy|cool|wild)\s+fact|"
    r"today\s+(?:we(?:'|’)re\s+going\s+to|we(?:'|’)ll|we\s+will|we\s+(?:look|talk|explore|answer|explain)|i(?:'|’)ll|i\s+will|i(?:'|’)m\s+going\s+to)|"
    r"in\s+(?:this|today(?:'|’)s)\s+(?:video|short|clip)|"
    r"let(?:'|’)?s\s+(?:talk|look|dive|explore|find\s+out|take\s+a\s+look|break\s+(?:it|this)\s+down)|"
    r"(?:many|a\s+lot\s+of|lots\s+of)\s+people\s+(?:wonder|ask|have\s+asked|are\s+curious)|people\s+often\s+ask|"
    r"the\s+question\s+is"
    r")"
)
# A promise of surprise without its content ("the answer will surprise you").
EMPTY_TEASER = re.compile(
    r"(?i)(?:die\s+antwort\s+(?:wird\s+dich\s+|könnte\s+dich\s+|ist\s+)?(?:überrasch\w*|verblüff\w*)|"
    r"der\s+grund\s+(?:wird\s+dich\s+|könnte\s+dich\s+|ist\s+)?(?:überrasch\w*|verblüff\w*)|"
    r"du\s+wirst\s+überrascht\s+sein|"
    r"(?:es|das)\s+ist\s+nicht\s+(?:das|so)\s*,?\s+(?:was|wie)\s+du\s+(?:denkst|glaubst)|"
    r"\b(?:anders|komplizierter|verrückter|seltsamer|einfacher|überraschender|spannender)\s+als\s+(?:du\s+)?(?:denkst|glaubst|gedacht)|"
    r"die\s+antwort\s+ist\s+(?:ganz\s+)?einfach|die\s+wahrheit\s+ist|hier\s+ist\s+(?:der\s+grund|warum)|"
    r"the\s+(?:answer|reason)\s+(?:will|might|may|could)\s+surprise|you(?:'|’)ll\s+be\s+surprised|"
    r"(?:it(?:'|’)s|it\s+is)\s+not\s+what\s+you\s+(?:think|expect)|"
    r"\b(?:crazier|weirder|stranger|simpler|more\s+surprising|more\s+complicated)\s+than\s+you\s+(?:think|expect|imagine)|"
    r"the\s+answer\s+is\s+(?:surprisingly\s+)?simple|the\s+truth\s+is|here(?:'|’)?s\s+why)"
)
# Words a teaser uses to point at an answer it does not give.
_TEASER_WORDS = re.compile(
    r"(?i)^(?:antwort\w*|answer\w*|grund|gründe|reason\w*|wahrheit|truth|überrasch\w*|surpris\w*|verblüff\w*|"
    r"denk\w*|think\w*|glaub\w*|believe\w*|gedacht|expect\w*|imagine|simple|simpler|einfach\w*|komplizierter|"
    r"complicated|verrückter|crazier|weirder|stranger|seltsamer|anders|spannender|frage\w*|question\w*)$"
)
# Placeholder subjects: the first thing heard names nothing.
VAGUE_OPENING = re.compile(
    r"(?i)^\s*(?:something|etwas|irgendwas|irgendetwas|"
    r"there(?:'|’)?s\s+(?:something|a\s+thing|one\s+thing|a\s+reason)|there\s+is\s+(?:something|a\s+thing|one\s+thing)|"
    r"es\s+gibt\s+(?:da\s+)?(?:etwas|eine\s+sache|einen\s+grund|ein\s+ding)|"
    r"this\s+(?:thing|one|happens|is\s+why|is\s+what|phenomenon|effect)|these\s+things|that\s+(?:thing|happens)|"
    r"(?:dieses?|diese[rn]?)\s+(?:ding|sache|phänomen|effekt)|eine\s+sache|one\s+thing|"
    r"das\s+(?:passiert|geschieht|hier|kennst|kennt|kann|macht|hat|liegt|klingt|sieht|wirkt|funktioniert|bedeutet|"
    r"ist\s+(?:der|die|das)\s+grund)|"
    r"it\s+(?:happens|turns\s+out|all\s+starts))\b"
)
# Relation verbs that state a link without saying what it is.
WEAK_VERB = re.compile(
    r"(?i)\b(?:spiel(?:t|en)\s+(?:dabei\s+)?(?:eine\s+)?(?:\w+\s+)?rolle|ha(?:t|ben)\s+(?:\w+\s+){0,4}?zu\s+tun|geht\s+es\s+um|"
    r"häng(?:t|en)\s+(?:\w+\s+){0,4}?zusammen|plays?\s+(?:a|an)\s+(?:\w+\s+)?role|has\s+(?:\w+\s+){0,3}?to\s+do\s+with|"
    r"is\s+(?:all\s+)?about|is\s+(?:related|connected|linked)\s+to|involves)\b"
)
_ABSTRACT_NOUN = re.compile(r"(?i)^\w{4,}(?:ung|heit|keit|tion|ität|ismus|ierung|schaft|ance|ence|ment|ity|ness)(?:en|s)?$")
_CONTRAST_MARK = re.compile(
    r"(?i)^(?:not|no|never|but|yet|instead|actually|still|despite|although|nicht|kein\w*|nie|aber|doch|sondern|"
    r"obwohl|statt|trotzdem|dennoch|eigentlich|\w+n[’']t)$"
)
_CONSEQUENCE_MARK = re.compile(
    r"(?i)\b(?:risk\w*|danger\w*|cost\w*|lose|loses|loss|damage\w*|prevent\w*|harm\w*|kill\w*|break\w*|"
    r"risiko|gefahr\w*|kostet|kosten|verlier\w*|verlust\w*|schäd\w*|verhinder\w*|tödlich\w*|zerstör\w*)\b"
)
_SELF_TEST = re.compile(
    r"(?i)\b(?:kannst du|erkennst du|schaffst du|würdest du|rate mal|tippst du|teste dich|can you|could you|would you|guess|test yourself)\b"
)
_VISUAL_REVEAL = re.compile(r"(?i)^\s*(?:schau|sieh|achte|guck|look|watch|notice|see)\b")
_REFRAME = re.compile(
    r"(?i)\b(?:not|nicht|kein\w*|no|isn't|aren't)\b[^.!?]{0,80}\b(?:but|sondern|rather|instead)\b|\b(?:statt|instead of|rather than)\b"
)
_NEGATION = re.compile(r"(?i)\b(?:nicht|kein\w*|nie|niemals|not|no|never)\b|\w+n[’']t\b")
_TOKEN = re.compile(r"[\wÄÖÜäöüß'’-]+")


def setup_opener(text: object) -> bool:
    return bool(SETUP_OPENER.search(str(text or "")))


def _tokens(text: str) -> list[str]:
    return [token for token in _TOKEN.findall(text) if token.strip("'’-")]


# ---------------------------------------------------------------------------
# Opening move (candidate diversity)
# ---------------------------------------------------------------------------

OPENING_MOVES = ("number", "challenge", "visual_reveal", "contrast", "contradiction", "consequence", "mystery", "counterintuitive", "fact")


def opening_move(text: object) -> str:
    """The rhetorical move the opening makes (grammar only).

    Candidates are meant to be different approaches; two candidates with the
    same move and mostly the same words are paraphrases.
    """
    value = str(text or "")
    if re.search(r"\d", value):
        return "number"
    if _SELF_TEST.search(value):
        return "challenge"
    if _VISUAL_REVEAL.search(value):
        return "visual_reveal"
    if _REFRAME.search(value):
        return "contrast"
    if _UNEXPLAINED_TURN.search(value):
        return "contradiction"
    if _CONSEQUENCE_MARK.search(value):
        return "consequence"
    if _OPEN_QUESTION.search(value) or _WITHHELD.search(value):
        return "mystery"
    if any(_CONTRAST_MARK.match(token) for token in _tokens(value)):
        return "counterintuitive"
    return "fact"


# ---------------------------------------------------------------------------
# The verbal opening (A, B, D, E, H plus gates)
# ---------------------------------------------------------------------------

def assess_opening(text: str, context: dict[str, Any], *, emergency: bool = False, specific: bool = True) -> dict[str, Any]:
    """First-second quality of a spoken hook: gates, codes and 0..1 scores.

    ``specific`` is the verbal rubric's own verdict that the hook carries
    concrete research content beyond the question.  Emergency hooks (the
    user's question as a last resort) are scored, never rejected here.
    """
    from .verbal_hook import _numbers, _related, _words, proposition_words

    value = " ".join(str(text or "").split())
    tokens = _tokens(value)
    hard: list[str] = []
    codes: list[str] = []
    if not tokens:
        return {"hard_fail": [], "reason_codes": [], "dimensions": {"first_second_clarity": 0.0, "information_density": 0.0, "scroll_stop": 0.0}, "gain": [], "move": "fact"}
    first_window = window_words(FIRST_SECOND_SECONDS)
    opening_window = window_words(OPENING_SECONDS)
    question_words = set(context.get("question_words") or set())
    claim_words = set(context.get("arc_words") or set()) - question_words

    # Where the first concrete anchor (topic, research word or number) and
    # the first new element (research word, number, contrast, stakes) fall.
    first_topic = first_new = None
    for index, token in enumerate(tokens):
        own = _words(token)
        number = bool(re.search(r"\d", token))
        topical = bool(own and (_related(own, question_words) or _related(own, claim_words)))
        if first_topic is None and (topical or number):
            first_topic = index
        novel = number or bool(own and _related(own, claim_words) and not _related(own, question_words)) or bool(_CONTRAST_MARK.match(token)) or bool(_CONSEQUENCE_MARK.match(token))
        if first_new is None and novel:
            first_new = index
    setup = setup_opener(value)
    vague = bool(VAGUE_OPENING.search(value))
    teaser = bool(EMPTY_TEASER.search(value))
    weak_verb = bool(WEAK_VERB.search(value))
    # Words a cold viewer must decode before the topic lands (long, not the
    # question's own terms): the first seconds are spent on decoding.
    decode = [
        token for token in tokens[:opening_window]
        if len(token) >= HARD_WORD and not re.search(r"\d", token) and not _related(_words(token), question_words)
    ]
    first_content = next((token for token in tokens if _words(token)), "")
    abstract_first = bool(first_content and _ABSTRACT_NOUN.match(first_content) and tokens.index(first_content) < first_window)

    # What the hook says beyond the question (teaser words point, they say nothing).
    question = str(context.get("question") or "")
    if question:
        said = proposition_words(value)
        gain = sorted(word for word in said - _related(said, proposition_words(question)) if not _TEASER_WORDS.match(word.lstrip("+-")))
        gain += sorted(_numbers(value) - _numbers(question))
        if not gain and _NEGATION.search(value) and not _NEGATION.search(question):
            # Denying the obvious explanation is information ("water is not the reason").
            gain.append("negation")
    else:
        gain = ["no_question"]
    spoken = _words(value)
    asked = _words(question) - {word for word in _words(question) if _TEASER_WORDS.match(word)}
    covers_question = bool(asked) and len(_related(asked, spoken)) / len(asked) >= 0.6
    # A question echo opens the first second with what the title already says.
    question_led = covers_question and first_new is not None and first_new >= first_window

    if setup:
        codes.append("setup_opener")
        if not emergency:
            hard.append("setup_opener")
    if teaser:
        codes.append("empty_teaser")
        if not emergency and not gain:
            hard.append("empty_teaser")
    if covers_question and not gain:
        codes.append("question_restatement")
        if not emergency:
            hard.append("question_restatement")
    elif question_led:
        codes.append("question_led_opening")
    if vague:
        codes.append("vague_opening")
        if not emergency and (first_topic is None or first_topic >= opening_window) and not specific:
            hard.append("vague_opening")
    if first_topic is None or first_topic >= opening_window:
        codes.append("delayed_specificity")
    elif first_topic >= first_window:
        codes.append("slow_start")
    if weak_verb:
        codes.append("weak_verb")
    if abstract_first:
        codes.append("abstract_opening")

    # A. immediate clarity: the topic inside the first-second window.
    if first_topic is None:
        clarity = 0.15
    elif first_topic < first_window:
        clarity = 1.0
    else:
        clarity = max(0.2, 1.0 - 0.12 * (first_topic - first_window + 1))
    clarity -= (0.5 if setup else 0) + (0.3 if vague else 0) + (0.15 if abstract_first else 0) + min(0.24, 0.08 * len(decode))
    # E. information density: content per spoken word, minus filler formulas.
    content = len({word for word in spoken if not _TEASER_WORDS.match(word)}) + len(_numbers(value))
    density = min(1.0, content / max(1, len(tokens)) / 0.45)
    density -= 0.2 * sum((setup, teaser, weak_verb)) + (0.1 if vague else 0)
    # H. scroll-stop: something concrete and new before the viewer decides.
    # Graded at the window edge: a twist one word later is not worthless.
    early_new = 0.0 if first_new is None else 1.0 if first_new < opening_window else max(0.0, 1.0 - 0.15 * (first_new - opening_window + 1))
    hook_signal = bool(re.search(r"\d", value)) or "?" in value or bool(_UNEXPLAINED_TURN.search(value)) or any(_CONTRAST_MARK.match(token) for token in tokens) or bool(_SELF_TEST.search(value)) or bool(_CONSEQUENCE_MARK.search(value))
    scroll = 0.2 + 0.35 * early_new + 0.25 * max(0.0, clarity) + (0.2 if hook_signal else 0) - min(0.2, 0.1 * len(decode))
    scroll -= (0.3 if setup else 0) + (0.25 if teaser else 0) + (0.15 if question_led else 0) + (0.2 if not gain else 0)
    dimensions = {
        "first_second_clarity": clarity,
        "information_density": density,
        "scroll_stop": scroll,
    }
    return {
        "hard_fail": list(dict.fromkeys(hard)),
        "reason_codes": list(dict.fromkeys(codes)),
        "dimensions": {key: round(max(0.0, min(1.0, score)), 3) for key, score in dimensions.items()},
        "gain": gain,
        "move": opening_move(value),
        "first_second": " ".join(tokens[:first_window]),
    }


# ---------------------------------------------------------------------------
# The first frame (visual hook)
# ---------------------------------------------------------------------------

# Generic stock clichés: footage that could open any video.
_GENERIC_STOCK = re.compile(
    r"(?i)\b(?:person|people|man|woman|student|teacher|businessman|businesswoman|someone)\s+(?:thinking|smiling|looking|"
    r"wondering|confused|talking|explaining|pointing|shrugging)|\b(?:question marks?|light ?bulb|abstract background|"
    r"stock footage|generic|beautiful scenery|random|thinking emoji|brain illustration|globe spinning|"
    r"laptop|office desk|whiteboard|books? on a (?:shelf|table)|nature background)\b"
)
# Framing that loses its subject in a 9:16 crop.
_CROP_RISK = re.compile(
    r"(?i)\b(?:wide[- ]?(?:shot|angle)|panorama\w*|landscape\s+(?:shot|view)|establishing\s+shot|skyline|crowd|"
    r"group\s+of|split[- ]screen|side[- ]by[- ]side|full\s+scene|several\s+\w+\s+(?:at once|together))\b"
)
_CROP_SAFE = re.compile(r"(?i)\b(?:close[- ]?up|macro|detail|medium(?:[- ]shot)?|portrait|vertical|nahaufnahme|makro|extreme)\b")
_MOTION = re.compile(
    r"(?i)\b\w{3,}ing\b|\b(?:motion|moving|moves?|change\w*|turns?|falls?|rises?|bursts?|splash\w*|time[- ]?lapse|slow[- ]?motion|"
    r"before (?:and|&) after|transform\w*)\b"
)


def assess_first_frame(visual: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Can a cold viewer read the first frame instantly, and does it interrupt?

    Clarity: one clear subject, related to the question or the story,
    surviving a 9:16 crop, not generic stock.  Intrigue: motion or change,
    contrast or tension, a close detail.  The visual does not need to repeat
    the narration; an image that only illustrates the question's words with
    nothing happening is weak.
    """
    from .verbal_hook import _related, _words

    visual = visual if isinstance(visual, dict) else {}
    subject = str(visual.get("subject") or visual.get("visual_goal") or "")
    action = " ".join(str(visual.get(key) or "") for key in ("action_state", "motion", "motion_or_change"))
    detail = " ".join(str(visual.get(key) or "") for key in ("key_detail", "contrast", "tension"))
    framing = str(visual.get("framing") or "")
    queries = " ".join(str(query) for query in visual.get("media_queries") or [])
    described = f"{subject} {action} {detail} {framing} {queries}"
    codes: list[str] = []
    seen = _words(f"{subject} {queries}")
    # Visual semantics are written in English: relation to the (project
    # language) question is only checked where both share a language.
    same_language = str(context.get("language") or "en").casefold().startswith("en")
    related = not same_language or bool(_related(seen, set(context.get("question_words") or set()) | set(context.get("arc_words") or set())))
    parts = [part for part in re.split(r",|\band\b|\bund\b|\+", subject) if part.strip()]
    cluttered = len(parts) >= 3 or len(_words(subject)) > 9
    crop_risk = bool(_CROP_RISK.search(described)) and not _CROP_SAFE.search(framing)
    generic = bool(_GENERIC_STOCK.search(described))
    motion = bool(_MOTION.search(action)) or bool(str(visual.get("motion") or visual.get("motion_or_change") or "").strip())
    tension = bool(detail.strip())
    question_only = same_language and bool(seen) and _related(seen, set(context.get("question_words") or set())) == seen
    illustrates = question_only and not motion and not tension
    if not related:
        codes.append("visual_off_topic")
    if cluttered:
        codes.append("visual_cluttered")
    if crop_risk:
        codes.append("visual_crop_risk")
    if generic:
        codes.append("visual_generic_stock")
    if illustrates:
        codes.append("visual_merely_illustrates")
    clarity = 1.0 - (0.35 if not related else 0) - (0.25 if cluttered else 0) - (0.2 if crop_risk else 0) - (0.3 if generic else 0)
    intrigue = (
        0.35 + (0.25 if motion else 0) + (0.2 if tension else 0) + (0.1 if _CROP_SAFE.search(framing) else 0)
        + (0.1 if str(visual.get("key_detail") or "").strip() else 0) - (0.25 if illustrates else 0) - (0.15 if generic else 0)
    )
    return {
        "reason_codes": codes,
        "visual_clarity": round(max(0.0, min(1.0, clarity)), 3),
        "visual_intrigue": round(max(0.0, min(1.0, intrigue)), 3),
    }


# ---------------------------------------------------------------------------
# On-screen text
# ---------------------------------------------------------------------------

def on_screen_role(text: str, verbal: str, context: dict[str, Any]) -> tuple[str, list[str]]:
    """What a (validated) on-screen hook does: number, contrast, challenge, clue, key phrase.

    Returns the role and penalty codes (restating the question, hard to read).
    """
    from .verbal_hook import _numbers, _related, _words

    value = str(text or "").strip()
    if not value:
        return "none", []
    codes: list[str] = []
    own = _words(value)
    asked = _words(context.get("question") or "")
    if own and asked and len(_related(own, asked)) / len(own) >= 0.8 and not (_numbers(value) - _numbers(context.get("question") or "")):
        codes.append("on_screen_restates_question")
    if any(len(token) >= 15 for token in _tokens(value)):
        codes.append("on_screen_hard_to_read")
    if _numbers(value) - _numbers(verbal):
        role = "number"
    elif re.search(r"(?i)\b(?:vs\.?|versus|gegen|statt|instead|not|nicht|kein\w*|but|aber|sondern)\b|≠|→", value):
        role = "contrast"
    elif _SELF_TEST.search(value) or re.search(r"(?i)^\s*(?:rate|guess|test)\b", value):
        role = "challenge"
    elif "?" in value:
        role = "clue"
    else:
        role = "key_phrase"
    return role, codes


# ---------------------------------------------------------------------------
# Report (persisted with the selected hook)
# ---------------------------------------------------------------------------

def quality_report(dimensions: dict[str, float], *, move: str, codes: list[str], first_second: str = "") -> dict[str, Any]:
    """The eight V3 scores of one opening, read from its rubric dimensions."""
    def get(key: str) -> float:
        return round(float(dimensions.get(key, 0.0)), 3)

    scores = {
        "immediate_clarity": get("first_second_clarity"),
        "curiosity_gap": get("curiosity"),
        "specificity": get("useful_information"),
        "novelty": get("insight"),
        "information_density": get("information_density"),
        "complementarity": get("complementarity"),
        "payoff_integrity": get("payoff_alignment"),
        "scroll_stop": get("scroll_stop"),
    }
    return {
        "version": VERSION,
        **scores,
        "overall": round(100 * sum(scores.values()) / len(scores), 1),
        "opening_move": move,
        "first_second": first_second,
        "codes": [code for code in codes if code in V3_CODES][:8],
    }


V3_CODES = {
    "setup_opener", "empty_teaser", "question_restatement", "question_led_opening", "vague_opening",
    "delayed_specificity", "slow_start", "weak_verb", "abstract_opening", "visual_off_topic", "visual_cluttered",
    "visual_crop_risk", "visual_generic_stock", "visual_merely_illustrates", "on_screen_restates_question",
    "on_screen_hard_to_read", "generic_opener",
}
