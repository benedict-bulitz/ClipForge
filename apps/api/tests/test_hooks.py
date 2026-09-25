"""Canonical verbal-hook taxonomy and deterministic truthfulness checks.

Selection itself lives in ``verbal_hook`` (the one verbal-hook authority);
these tests cover the validators it relies on and port the former V1
selection rules to that authority.
"""
from clipforge.hooks import (
    CANONICAL_STRATEGIES,
    STRATEGIES,
    canonical_strategy,
    hook_issues,
    strategy_matches,
)
from clipforge.narration import clean_research_claim, contamination_issues
from clipforge.verbal_hook import assess_verbal, hook_context, rank_verbal, select_verbal


def intent(question: str, *, language: str = "en", topic: str | None = None) -> dict:
    return {"question": question, "topic": topic or question, "language": language, "tone": "clear", "content_type": "explanation"}


def fact(claim: str, *, sourced: bool = True, index: int = 1) -> dict:
    return {
        "id": f"fact_{index:02d}", "claim": claim,
        "verification": "source_attributed" if sourced else "unverified_model_synthesis",
        "sources": [{"label": "Source", "url": "https://source.test"}] if sourced else [],
    }


def context(current_intent: dict, facts: list[dict], body: str = "") -> dict:
    return hook_context(current_intent, facts, story_arc=None, payoff_plan=None, format_plan=None, novelty_plan=None,
                        body_blocks=[{"role": "answer", "text": body}] if body else [])


def best(current_intent: dict, facts: list[dict], candidates: list[dict], body: str = "") -> dict | None:
    return select_verbal(context(current_intent, facts, body), extra=[{**item, "origin": "ai"} for item in candidates])


# --- canonical taxonomy -------------------------------------------------------

def test_canonical_strategies_are_the_documented_set_and_aliases_normalise():
    assert CANONICAL_STRATEGIES == (
        "curiosity_gap", "counterintuitive_insight", "direct_reframe", "evidence_insight", "common_mistake",
        "verified_statistic", "social_proof_or_trend", "high_stakes_consequence", "ego_challenge",
    )
    assert STRATEGIES == set(CANONICAL_STRATEGIES)
    assert canonical_strategy("shock_number") == "verified_statistic"
    assert canonical_strategy("social_proof") == "social_proof_or_trend"
    assert canonical_strategy("hot_take") == "counterintuitive_insight"
    assert canonical_strategy("direct_confrontation") == "direct_reframe"
    for invented in ("comparison_tension", "mystery", "viral_hook", "attention_hook", "invented_strategy", "", None):
        assert canonical_strategy(invented) is None


def test_strategy_labels_must_be_carried_by_the_wording():
    assert strategy_matches("verified_statistic", "Rund 17.000 Inseln – reicht das?")
    assert not strategy_matches("verified_statistic", "Sehr viele Inseln.")
    assert strategy_matches("direct_reframe", "The opening is not damage but a pressure valve.")
    assert not strategy_matches("direct_reframe", "The opening equalizes pressure.")
    assert strategy_matches("counterintuitive_insight", "Diese Falten sind kein Wasserschaden.")
    assert strategy_matches("ego_challenge", "Errätst du, welches Tier das ist?")
    assert strategy_matches("curiosity_gap", "Why does blue light win in the sky?")
    assert not strategy_matches("made_up", "Anything.")


# --- validators (unchanged rules) ---------------------------------------------

def test_exact_and_paraphrased_question_echo_are_flagged_when_evidence_exists():
    current_intent = intent("Warum tränen unsere Augen beim Zwiebelschneiden?", language="de", topic="Augen tränen beim Zwiebelschneiden")
    evidence = [fact("Beim Schneiden setzt die Zwiebel Stoffe frei, die die Augen reizen.")]
    assert "question_echo" in hook_issues("Warum tränen unsere Augen beim Zwiebelschneiden?", evidence, intent=current_intent)
    assert "question_echo" in hook_issues("Aber warum tränen deine Augen eigentlich beim Zwiebelschneiden?", evidence, intent=current_intent)


def test_generic_prevalence_is_gated():
    assert "unsupported_prevalence" in hook_issues("The most common mistake happens before payday.", [])
    assert "unsupported_prevalence" in hook_issues("Der häufigste Fehler passiert vor dem Zahltag.", [])
    assert "unsupported_prevalence" not in hook_issues("Most people save less than they plan.", [fact("Most people save less than they plan.")])


def test_supported_number_allowed_but_vague_research_cannot_become_statistic():
    supported = fact("42% of surveyed readers changed their habits.")
    assert "unsupported_statistic" not in hook_issues("42% changed their habits.", [supported])
    assert "unsupported_statistic" in hook_issues("78% changed their habits.", [fact("Many readers changed their habits.")])
    unrelated = [fact("42% of cyclists changed routes.")]
    assert "unsupported_statistic" in hook_issues("42% of readers changed habits.", unrelated, intent=intent("How do readers form habits?", topic="readers habits"))


def test_trend_and_cliche_are_gated():
    assert "unsupported_trend" in hook_issues("Everyone is switching to this.", [])
    assert "generic_clickbait" in hook_issues("You've been lied to about the sky.", [])
    assert "unsupported_trend" in hook_issues("Everyone is switching to this.", [fact("42% of cyclists changed routes.")], intent=intent("How do readers form habits?", topic="readers habits"))
    assert "unsupported_trend" not in hook_issues("Readers are increasingly choosing digital books.", [fact("Readers are increasingly choosing digital books.")], intent=intent("Why are readers choosing digital books?", topic="readers digital books"))


def test_hook_quality_allows_a_body_that_adds_new_mechanism():
    issues = hook_issues("Beim Steigflug sinkt der Außendruck deutlich.", [], body="Das Loch dient zum Druckausgleich.", intent=intent("Warum ist dort ein Loch?", language="de"))
    assert "repeats_body" not in issues


def test_hook_body_repetition_is_detected_and_meta_labels_rejected():
    assert "repeats_body" in hook_issues("Blue light scatters in the atmosphere.", [], body="Blue light scatters in the atmosphere.")
    assert "meta_language" in hook_issues("In this video, we explain the sky.", [])
    assert "generic_meta_filler" in hook_issues("Blaues Licht wird stärker gestreut – und genau das beantwortet die Frage.", [])


def test_research_publisher_and_editorial_boilerplate_are_not_usable_claims():
    claim = "TRAVELBOOK erklärt, warum die Löcher in doppelter Hinsicht wichtig sind. Das Loch hilft beim Druckausgleich."
    cleaned = clean_research_claim(claim)
    assert "TRAVELBOOK" not in cleaned
    assert "Das Loch hilft beim Druckausgleich." in cleaned
    assert "publisher or editorial boilerplate" in contamination_issues("TRAVELBOOK erklärt, warum die Löcher wichtig sind.")


def test_profile_exposes_supported_misconception_and_consequence():
    from clipforge.hook_library import analyze_hook_opportunities, eligible_strategy_ids

    profile = analyze_hook_opportunities([fact("The window is not damaged. The opening helps equalize pressure between the panes.")])
    assert profile.misconception
    assert profile.viewer_consequence
    assert "direct_confrontation" in eligible_strategy_ids(profile)


# --- the former V1 selection rules, now enforced by the verbal authority -------

def test_question_echo_loses_to_a_researched_hook():
    current_intent = intent("Warum tränen unsere Augen beim Zwiebelschneiden?", language="de", topic="Augen tränen beim Zwiebelschneiden")
    evidence = [fact("Beim Schneiden setzt die Zwiebel Stoffe frei, die die Augen reizen – nicht der Geruch.")]
    chosen = best(current_intent, evidence, [
        {"strategy": "curiosity_gap", "text": "Warum tränen unsere Augen beim Zwiebelschneiden?"},
        {"strategy": "counterintuitive_insight", "text": "Nicht der Geruch reizt deine Augen – es sind Stoffe aus der Zwiebel."},
    ], body="Die Reizstoffe lösen einen Schutzreflex aus.")
    assert chosen is not None and chosen["strategy"] in STRATEGIES
    assert "warum tränen unsere augen" not in chosen["text"].casefold()


def test_jargon_first_hook_loses_to_a_clear_viewer_hook():
    current_intent = intent("Wieso kriegen wir Gänsehaut?", language="de", topic="Gänsehaut")
    evidence = [fact("Kleine Muskeln an den Haarwurzeln ziehen sich zusammen und stellen die Haare auf.")]
    jargon = "Piloerektion ist der Grund für deine Gänsehaut."
    clear = "Deine Haare stellen sich auf – aber warum eigentlich?"
    assert "jargon_first" in hook_issues(jargon, evidence, intent=current_intent)
    ranked = rank_verbal(context(current_intent, evidence, "Die kleinen Muskeln reagieren auf Kälte."), [
        {"strategy": "evidence_insight", "text": jargon, "origin": "ai"},
        {"strategy": "curiosity_gap", "text": clear, "origin": "ai"},
    ])
    assert ranked[0]["text"] == clear


def test_model_strategy_label_that_the_wording_does_not_carry_is_rejected():
    ctx = context(intent("What is photosynthesis?", topic="photosynthesis"), [fact("Plants use sunlight to make sugar, but they also release oxygen.")], "Sunlight powers sugar production.")
    assert "strategy_not_in_wording" in assess_verbal("Plants use sunlight to make sugar.", "counterintuitive_insight", ctx)["hard_fail"]
    assert "non_document_strategy" in assess_verbal("Plants turn sunlight into sugar.", "invented_strategy", ctx)["hard_fail"]


def test_unsupported_model_statistic_never_wins():
    current_intent = intent("Why is the sky blue?", topic="the sky")
    evidence = [fact("Blue light scatters more strongly than red light in Earth's atmosphere.")]
    ctx = context(current_intent, evidence)
    assert "unsupported_statistic" in assess_verbal("42% of people prefer blue skies.", "verified_statistic", ctx)["hard_fail"]
    chosen = best(current_intent, evidence, [{"strategy": "verified_statistic", "text": "42% of people prefer blue skies."}])
    assert chosen is None or "42%" not in chosen["text"]


def test_supported_direct_reframe_beats_a_bare_answer():
    evidence = [fact("The window is not damaged. The opening helps equalize pressure between the panes.")]
    chosen = best(intent("Why do airplane windows have a tiny hole?", topic="airplane windows"), evidence, [
        {"strategy": "evidence_insight", "text": "It equalizes pressure."},
        {"strategy": "direct_reframe", "text": "The opening is not damage—it helps equalize pressure between the panes."},
    ], body="At cruising altitude, cabin pressure is higher than outside pressure.")
    assert chosen is not None and chosen["strategy"] == "direct_reframe"
