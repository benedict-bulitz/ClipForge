from clipforge.hooks import (
    STRATEGIES,
    generate_hook_candidates,
    hook_issues,
    select_hook,
    select_hook_candidate,
)
from clipforge.narration import clean_research_claim, contamination_issues


def intent(question: str, *, language: str = "en", topic: str | None = None) -> dict:
    return {"question": question, "topic": topic or question, "language": language, "tone": "clear", "content_type": "explanation"}


def fact(claim: str, *, sourced: bool = True) -> dict:
    return {"claim": claim, "verification": "source_attributed" if sourced else "unverified_model_synthesis", "sources": [{"label": "Source", "url": "https://source.test"}] if sourced else []}


def test_science_prefers_relevant_curiosity_without_forced_confrontation():
    evidence = fact("Blue light scatters more strongly than red light in Earth's atmosphere.")
    chosen = select_hook(
        intent("Why is the sky blue?", topic="the sky"), [evidence], body=evidence["claim"],
        model_candidates=[{"strategy": "curiosity_gap", "text": "Why does blue light win in the sky?"}],
    )
    assert chosen != "Why is the sky blue?"
    assert "blue light" in chosen.lower()
    assert not hook_issues(chosen, [])


def test_practical_topic_gets_mistake_strategy_and_no_attack():
    candidates = generate_hook_candidates(intent("How do people struggle to save money?", topic="saving money"), [])
    assert any(candidate.strategy == "common_mistake" for candidate in candidates)
    assert all("lazy" not in candidate.text.lower() for candidate in candidates)


def test_existing_weak_hook_can_lose_but_strong_hook_can_win():
    evidence = [fact("Blue light scatters more strongly than red light in Earth's atmosphere.")]
    weak = "Why is the sky blue?"
    strong = "Why does blue light win in the sky?"
    assert select_hook(intent("Why is the sky blue?", topic="the sky"), evidence, body=evidence[0]["claim"], existing=weak) != weak
    assert select_hook(intent("Why is the sky blue?", topic="the sky"), evidence, body="The atmosphere scatters light.", existing=strong) == strong


def test_exact_and_paraphrased_question_echo_lose_when_evidence_exists():
    current_intent = intent(
        "Warum tränen unsere Augen beim Zwiebelschneiden?",
        language="de",
        topic="Augen tränen beim Zwiebelschneiden",
    )
    evidence = [fact("Beim Schneiden setzt die Zwiebel Stoffe frei, die die Augen reizen.")]
    exact = "Warum tränen unsere Augen beim Zwiebelschneiden?"
    paraphrase = "Aber warum tränen deine Augen eigentlich beim Zwiebelschneiden?"
    assert "question_echo" in hook_issues(exact, evidence, intent=current_intent)
    assert "question_echo" in hook_issues(paraphrase, evidence, intent=current_intent)
    selected = select_hook_candidate(
        current_intent,
        evidence,
        body="Die Reizstoffe lösen einen Schutzreflex aus.",
        model_candidates=[
            {"strategy": "ego_challenge", "text": paraphrase},
            {"strategy": "curiosity_gap", "text": "Was reizt deine Augen beim Zwiebelschneiden wirklich?"},
        ],
    )
    assert selected is not None
    assert selected.text != exact
    assert selected.text != paraphrase


def test_jargon_first_hook_is_rejected_while_clear_viewer_hook_can_win():
    current_intent = intent("Wieso kriegen wir Gänsehaut?", language="de", topic="Gänsehaut")
    evidence = [fact("Kleine Muskeln an den Haarwurzeln ziehen sich zusammen und stellen die Haare auf.")]
    jargon = "Piloerektion ist der Grund für deine Gänsehaut."
    clear = "Deine Haare stellen sich auf – aber warum eigentlich?"
    assert "jargon_first" in hook_issues(jargon, evidence, intent=current_intent)
    selected = select_hook_candidate(
        current_intent, evidence, body="Die kleinen Muskeln reagieren auf Kälte oder starke Gefühle.",
        model_candidates=[
            {"strategy": "evidence_insight", "text": jargon},
            {"strategy": "curiosity_gap", "text": clear},
        ],
    )
    assert selected is not None
    assert selected.text == clear


def test_plain_explanation_loses_to_a_stronger_curiosity_hook():
    current_intent = intent(
        "Warum tränen unsere Augen beim Zwiebelschneiden?",
        language="de",
        topic="Augen tränen beim Zwiebelschneiden",
    )
    evidence = [fact("Beim Schneiden setzt die Zwiebel Stoffe frei, die die Augen reizen.")]
    explanation = "Beim Schneiden setzt die Zwiebel Stoffe frei, die die Augen reizen."
    hook = "Was reizt deine Augen beim Zwiebelschneiden wirklich?"
    selected = select_hook_candidate(
        current_intent,
        evidence,
        body=explanation,
        model_candidates=[
            {"strategy": "evidence_insight", "text": explanation},
            {"strategy": "curiosity_gap", "text": hook},
        ],
    )
    assert selected is not None
    assert selected.text == hook
    assert selected.strategy == "curiosity_gap"


def test_unlabelled_legacy_hook_cannot_bypass_the_hook_mechanism_gate():
    current_intent = intent("Warum tränen unsere Augen beim Zwiebelschneiden?", language="de")
    evidence = [fact("Beim Schneiden setzt die Zwiebel Stoffe frei, die die Augen reizen.")]
    selected = select_hook_candidate(
        current_intent,
        evidence,
        body="Die Reizstoffe lösen einen Schutzreflex aus.",
        existing="Beim Schneiden setzt die Zwiebel reizende Stoffe frei.",
    )
    assert selected is None


def test_authoritative_hook_is_first_and_does_not_change_body():
    from clipforge.pipeline import _authoritative_hook_blocks

    body = [
        {"role": "answer", "text": "Die Zwiebel setzt reizende Stoffe frei."},
        {"role": "explanation", "text": "Deine Augen schützen sich mit Tränen."},
    ]
    result, selected = _authoritative_hook_blocks(
        body,
        intent("Warum tränen unsere Augen beim Zwiebelschneiden?", language="de"),
        [fact("Die Zwiebel setzt reizende Stoffe frei.")],
        [{"strategy": "evidence_insight", "text": "Die Zwiebel reizt deine Augen – und deine Tränen schützen sie."}],
    )
    assert selected is not None
    assert result[0]["role"] == "hook"
    assert result[1:] == body


def test_direct_reframe_ego_and_consequence_are_contextual():
    evidence = fact("The problem is not skipping practice, but practicing without feedback; that can cost weeks of progress.")
    candidates = generate_hook_candidates(intent("How can I learn faster?", topic="learning habits"), [evidence], body="Feedback changes how quickly practice works.")
    strategies = {candidate.strategy for candidate in candidates}
    assert {"direct_reframe", "ego_challenge", "high_stakes_consequence"} <= strategies


def test_generic_counterintuitive_shell_is_penalized_and_prevalence_is_gated():
    evidence = fact("Blue light scatters in the atmosphere.")
    assert select_hook(intent("Why is the sky blue?", topic="the sky"), [evidence], body=evidence["claim"]) != "The surprising part of the sky is what actually causes it."
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


def test_german_hook_is_native_and_short():
    chosen = select_hook(
        intent("Warum ist der Himmel blau?", language="de", topic="der Himmel"),
        [fact("Blaues Licht wird in der Atmosphäre stärker gestreut.")],
        body="Blaues Licht wird in der Atmosphäre stärker gestreut.",
        model_candidates=[{"strategy": "curiosity_gap", "text": "Warum gewinnt blaues Licht am Himmel?"}],
    )
    assert chosen != "Warum ist der Himmel blau?"
    assert "blaues licht" in chosen.lower()
    assert len(chosen.split()) <= 14


def test_compound_hook_consumes_each_core_proposition_and_blocks_reassurance():
    selected = select_hook_candidate(
        intent("Warum ist dort ein Loch?", language="de", topic="Flugzeugfenster"),
        [fact("Das Loch hilft beim Druckausgleich zwischen den Scheiben.")],
        body="Das Loch hilft beim Druckausgleich zwischen den Scheiben.",
        model_candidates=[
            {"strategy": "hot_take", "text": "Keine Sorge: Es ist kein Schaden."},
            {"strategy": "evidence_insight", "text": "Das Loch hilft beim Druckausgleich zwischen den Scheiben."},
        ],
    )
    assert selected is None


def test_hook_quality_allows_a_body_that_adds_new_mechanism():
    hook = "Das Loch dient zum Druckausgleich."
    issues = hook_issues(
        "Beim Steigflug sinkt der Außendruck deutlich.",
        [],
        body=hook,
        intent=intent("Warum ist dort ein Loch?", language="de"),
    )
    assert "repeats_body" not in issues


def test_hook_body_repetition_is_detected_and_meta_labels_rejected():
    assert "repeats_body" in hook_issues("Blue light scatters in the atmosphere.", [], body="Blue light scatters in the atmosphere.")
    assert "meta_language" in hook_issues("In this video, we explain the sky.", [])


def test_plain_grounded_fallback_is_not_promoted_to_hook():
    german = fact("Blaues Licht wird in der Atmosphäre stärker gestreut als rotes Licht.")
    chosen = select_hook(intent("Warum ist der Himmel blau?", language="de", topic="der Himmel"), [german], body=german["claim"])
    assert chosen is None
    english = fact("Blue light scatters more strongly than red light in Earth's atmosphere.")
    chosen_en = select_hook(intent("Why is the sky blue?", topic="the sky"), [english], body=english["claim"])
    assert chosen_en is None


def test_generic_fact_is_not_mislabeled_as_counterintuitive_and_model_metadata_is_safe():
    candidates = generate_hook_candidates(intent("What is photosynthesis?", topic="photosynthesis"), [fact("Plants use sunlight to make sugar.")])
    assert all(candidate.strategy != "counterintuitive_insight" for candidate in candidates)
    selected = select_hook(intent("What is photosynthesis?", topic="photosynthesis"), [], body="That sugar then feeds the plant.", model_candidates=[{"strategy": "invented_strategy", "text": "Plants turn sunlight into usable energy."}])
    assert selected is None


def test_modestly_long_factual_sentence_is_body_not_hook():
    evidence = fact("The atmosphere scatters shorter blue wavelengths more strongly than longer red wavelengths, which changes the light we see.")
    selected = select_hook(intent("Why is the sky blue?", topic="sky"), [evidence])
    assert selected is None


def test_plain_comparative_fallback_is_body_and_meta_suffixes_are_rejected():
    claim = fact("Blue light scatters more strongly than red light in Earth's atmosphere.")
    selected = select_hook(intent("Why is the sky blue?", topic="sky"), [claim])
    assert selected is None
    assert "generic_meta_filler" in hook_issues("Blaues Licht wird stärker gestreut – und genau das beantwortet die Frage.", [])


def test_explicit_reframe_and_consequence_keep_distinct_semantics():
    claim = fact("The problem is not skipping practice, but practicing without feedback; that can cost weeks of progress.")
    candidates = generate_hook_candidates(intent("How should I practice?", topic="learning habits"), [claim])
    by_strategy = {candidate.strategy: candidate.text for candidate in candidates}
    assert "not" in by_strategy["direct_reframe"].lower()
    assert "cost weeks" in by_strategy["high_stakes_consequence"].lower()
    assert by_strategy["direct_reframe"] != by_strategy["high_stakes_consequence"]


def test_model_strategy_metadata_is_downgraded_when_rhetoric_does_not_match():
    selected = select_hook(
        intent("What is photosynthesis?", topic="photosynthesis"),
        [],
        body="Sunlight powers sugar production in plants.",
        model_candidates=[{"strategy": "counterintuitive_insight", "text": "Plants use sunlight to make sugar."}],
    )
    assert selected is None


def test_earth_plain_evidence_is_not_promoted_to_hook():
    current_intent = intent("Wieso wird die Erde nicht schwerer, wenn wir darauf bauen?", language="de", topic="die Erde wird beim Bauen nicht schwerer")
    evidence = fact("Beim Bauen wird Materie, die bereits auf der Erde vorhanden ist, nur umverteilt.")
    candidate = select_hook_candidate(current_intent, [evidence], body=evidence["claim"])
    assert candidate is None


def test_unknown_model_strategy_is_safe_for_final_diagnostics():
    candidate = select_hook_candidate(intent("What is photosynthesis?", topic="photosynthesis"), [], body="Plants use sunlight to make sugar.", model_candidates=[{"strategy": "made_up", "text": "Plants turn sunlight into sugar."}])
    assert candidate is None


def test_model_candidates_are_grounded_deduplicated_and_strategy_checked():
    current_intent = intent("Why is the sky blue?", topic="the sky")
    evidence = fact("Blue light scatters more strongly than red light in Earth's atmosphere.")
    model_candidates = [
        {"strategy": "counterintuitive_insight", "text": "Blue light scatters more strongly than red light."},
        {"strategy": "counterintuitive_insight", "text": "Blue light scatters more strongly than red light."},
        {"strategy": "verified_statistic", "text": "42% of people prefer blue skies."},
        {"strategy": "evidence_insight", "text": "Blue light scatters more strongly than red light in Earth's atmosphere."},
    ]
    selected = select_hook_candidate(
        current_intent,
        [evidence],
        model_candidates=model_candidates,
    )
    assert selected is None
    assert "unsupported_statistic" in hook_issues("42% of people prefer blue skies.", [evidence], intent=current_intent)


def test_unknown_model_strategy_is_safe_for_final_selection():
    current_intent = intent("Why does building not add mass to Earth?", topic="Earth building")
    evidence = fact("Matter already on Earth is only redistributed when we build.")

    candidate = select_hook_candidate(
        current_intent,
        [evidence],
        model_candidates=[
            {"strategy": "mystery", "text": "This is amazing and you should watch."}
        ],
    )
    assert candidate is None


def test_research_publisher_and_editorial_boilerplate_are_not_usable_claims():
    claim = "TRAVELBOOK erklärt, warum die Löcher in doppelter Hinsicht wichtig sind. Das Loch hilft beim Druckausgleich."
    cleaned = clean_research_claim(claim)
    assert "TRAVELBOOK" not in cleaned
    assert "Das Loch hilft beim Druckausgleich." in cleaned
    assert "publisher or editorial boilerplate" in contamination_issues(
        "TRAVELBOOK erklärt, warum die Löcher wichtig sind."
    )


def test_strategy_driven_candidate_beats_bare_answer_and_progresses_to_mechanism():
    evidence = [
        fact("The window is not damaged. The opening helps equalize pressure between the panes.")
    ]
    current_intent = intent(
        "Why do airplane windows have a tiny hole?",
        language="en",
        topic="airplane windows",
    )
    selected = select_hook_candidate(
        current_intent,
        evidence,
        body="At cruising altitude, cabin pressure is higher than outside pressure.",
        model_candidates=[
            {"strategy": "evidence_insight", "text": "It equalizes pressure."},
            {
                "strategy": "direct_reframe",
                "text": "The opening is not damage—it helps equalize pressure between the panes.",
            },
        ],
    )
    assert selected is not None
    assert selected.text != "It equalizes pressure."
    assert selected.strategy in STRATEGIES


def test_profile_exposes_supported_misconception_and_consequence():
    from clipforge.hook_library import analyze_hook_opportunities, eligible_strategy_ids

    evidence = [
        fact(
            "The window is not damaged. The opening helps equalize pressure between the panes."
        )
    ]
    profile = analyze_hook_opportunities(evidence)
    assert profile.misconception
    assert profile.viewer_consequence
    assert "direct_confrontation" in eligible_strategy_ids(profile)
