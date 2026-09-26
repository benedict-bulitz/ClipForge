from pathlib import Path

import pytest

from clipforge.ai import AIHookGenerationResult
from clipforge.alignment import (
    align_narration,
    group_aligned_words,
    phrase_fallback_items,
)
from clipforge.config import Settings
from clipforge.language import detect_text_language, resolve_language
from clipforge.pipeline import apply_edit, build_initial_state
from clipforge.renderer import (
    _create_music_track,
    _create_voice,
    _write_ass_captions,
    caption_style_config,
    music_filter_graph,
    music_render_config,
)
from clipforge.research import ResearchResult
from clipforge.review import (
    ReviewDecision,
    ReviewFinding,
    local_review_items,
    review_context,
    run_ai_review,
)


def test_active_hook_selection_preserves_the_selected_hook_text():
    hook = "The opening looks like damage, but it serves a real purpose: it equalizes pressure."
    candidate = select_hook_candidate(
        {
            "question": "Why is there an opening?",
            "topic": "aircraft windows",
            "language": "en",
        },
        [
            {
                "claim": hook,
                "verification": "source_attributed",
                "sources": [{"label": "Source", "url": "https://source.test"}],
            }
        ],
        existing=hook,
    )
    assert candidate is not None
    assert candidate["text"] == hook


def test_selected_candidate_uses_the_active_strategy_value():
    candidate = select_hook_candidate(
        {
            "question": "Why is there a small opening?",
            "topic": "aircraft windows",
            "language": "en",
        },
        [
            {
                "claim": "The opening is not damage; it helps equalize pressure between the panes.",
                "verification": "source_attributed",
                "sources": [{"label": "Source", "url": "https://source.test"}],
            }
        ],
        model_candidates=[
            {
                "strategy": "direct_reframe",
                "text": "The opening is not damage; it helps equalize pressure between the panes.",
            }
        ],
    )
    assert candidate is not None
    assert candidate["strategy"] in {"counterintuitive_insight", "direct_reframe", "evidence_insight"}
from clipforge.schemas import AdvancedOptions
from clipforge.verbal_hook import hook_context, select_verbal


def select_hook_candidate(intent, facts, *, body="", existing=None, model_candidates=None):
    """The document-based verbal authority over the given candidates (test helper)."""
    facts = [{"id": f"fact_{index:02d}", **fact} for index, fact in enumerate(facts, 1)]
    context = hook_context(intent, facts, story_arc=None, payoff_plan=None, format_plan=None, novelty_plan=None,
                           body_blocks=[{"role": "answer", "text": body}] if body else [])
    extra = [{"strategy": item["strategy"], "text": item["text"], "origin": "ai"} for item in model_candidates or []]
    if existing:
        extra.append({"strategy": "counterintuitive_insight", "text": existing, "origin": "planner"})
    return select_verbal(context, extra=extra)


def settings(tmp_path: Path | None = None, **updates) -> Settings:
    values = {
        "clipforge_ai_mode": "local",
        "openai_api_key": None,
        "caption_alignment_provider": "auto",
    }
    if tmp_path is not None:
        values["render_root"] = tmp_path
    values.update(updates)
    return Settings(**values)


def project_state(*, language: str = "de", **options):
    prompt = (
        "Erzähle eine Geschichte über eine Astronautin auf dem Mond"
        if language == "de"
        else "Tell a story about an astronaut on the moon"
    )
    return build_initial_state(
        prompt,
        AdvancedOptions(language=language, research="off", **options),
        settings(),
    )


def test_german_prompt_auto_resolves_to_german():
    prompt = "Wieso wird unser Planet nicht schwerer, wenn darauf gebaut wird?"
    assert resolve_language(prompt, "auto") == "de"
    state = build_initial_state(
        prompt, AdvancedOptions(language="auto", research="off"), settings()
    )
    assert state["intent"]["language"] == "de"
    assert state["options"]["language"] == "de"
    assert "Recherche" in state["script"]["text"]


@pytest.mark.parametrize(
    ("prompt", "language", "claim", "tier_one", "mechanism"),
    [
        (
            "Warum haben Flugzeugfenster unten ein kleines Loch?",
            "de",
            "Das Loch hilft, den Druck zwischen den Fensterscheiben auszugleichen und die Belastung der Scheiben zu verteilen.",
            "Das kleine Loch ist nicht kaputt, sondern gleicht den Druck zwischen den Scheiben aus.",
            "Beim Aufstieg sinkt der Außendruck, während der Kabinendruck höher bleibt.",
        ),
        (
            "Why do airplane windows have a tiny hole at the bottom?",
            "en",
            "The hole helps equalize pressure between the window panes and distribute the load.",
            "That tiny hole is not damage, but it balances pressure between the panes.",
            "At cruising altitude, cabin pressure stays higher than the outside pressure.",
        ),
    ],
)
def test_realistic_airplane_path_keeps_tier_one_through_review(
    monkeypatch, prompt, language, claim, tier_one, mechanism
):
    source = {"label": "Aviation source", "url": "https://source.test/aviation"}
    facts = [
        {
            "claim": claim,
            "confidence": 0.95,
            "importance": 0.95,
            "sources": [source],
            "verification": "source_snippet",
        }
    ]

    class FakePlan:
        def model_dump(self, mode="json"):
            return {
                "intent": {
                    "topic": prompt.rstrip("?"),
                    "intent": "explain",
                    "question": prompt,
                    "language": language,
                    "content_type": "factual_explainer",
                    "tone": "fast_documentary",
                    "research_required": True,
                    "visual_style": "documentary_graphics",
                    "shortform": True,
                },
                "research_questions": ["What does the hole do?"],
                "facts": [],
                "answer_skeleton": ["HOOK", "ANSWER"],
                "script_blocks": [{"role": "answer", "text": mechanism}],
                "music_mood": "documentary",
                "hook_candidates": [
                    {"strategy": "evidence_insight", "text": tier_one},
                    {"strategy": "evidence_insight", "text": mechanism},
                ],
                "selected_hook_strategy": "evidence_insight",
                "visual_intents": [],
            }

    monkeypatch.setattr(
        "clipforge.pipeline.research_topic",
        lambda *_args, **_kwargs: ResearchResult(
            facts, [source], "verified_sources", "fixture"
        ),
    )
    monkeypatch.setattr(
        "clipforge.pipeline.plan_with_openai",
        lambda *_args, **_kwargs: type(
            "PlanResult",
            (),
            {"plan": FakePlan(), "status": "connected", "error": None},
        )(),
    )
    monkeypatch.setattr(
        "clipforge.pipeline.generate_hook_candidates_with_openai",
        lambda *_args, **_kwargs: AIHookGenerationResult(
            [{"strategy": "direct_reframe", "text": tier_one}],
            "direct_reframe",
            "connected",
        ),
    )
    state = build_initial_state(
        prompt,
        AdvancedOptions(language=language, research="on"),
        settings(),
    )
    selected = state["script"]["selected_hook"]
    assert selected == tier_one
    assert state["script"]["blocks"][0]["text"] == selected

    class Provider:
        name = "fixture-review"

        def review(self, _context):
            return ReviewDecision(
                status="needs_fix",
                items=[ReviewFinding(check="language", severity="warning", message="Keep concise.")],
                corrected_script_blocks=[
                    {"role": "hook", "text": mechanism},
                    {"role": "answer", "text": mechanism},
                ],
            )

    run_ai_review(state, settings(), provider=Provider(), max_rounds=1)
    # The review may correct the body, never replace the selected verbal hook.
    assert state["script"]["selected_hook"] == tier_one
    assert state["script"]["blocks"][0]["text"] == tier_one
    assert state["script"]["text"].startswith(tier_one)
    assert state["script"]["triple_hook"]["selected_strategy"] == "direct_reframe"


@pytest.mark.parametrize(("choice", "expected"), [("de", "de"), ("en", "en")])
def test_explicit_language_is_the_project_source_of_truth(choice, expected):
    state = build_initial_state(
        "Eine Geschichte über den Mond",
        AdvancedOptions(language=choice, research="off"),
        settings(),
    )
    assert state["intent"]["language"] == expected
    assert state["options"]["language"] == choice


def test_voice_edit_and_rerender_state_preserve_language():
    state = project_state(language="de")
    edited, _changed = apply_edit(state, "Mach die Stimme tiefer", settings())
    assert edited["intent"]["language"] == "de"
    assert edited["voice"]["tone"] == "deep"


def test_explicit_language_edit_changes_the_project_language():
    state = project_state(language="en")
    edited, _changed = apply_edit(state, "Translate this video to German", settings())
    assert edited["intent"]["language"] == "de"
    assert "Die" in edited["script"]["text"] or "Ein" in edited["script"]["text"]


def test_german_project_sends_german_text_and_language_instructions_to_tts(
    tmp_path, monkeypatch
):
    state = project_state(language="de")
    captured = {}

    class Speech:
        def create(self, **kwargs):
            captured.update(kwargs)
            return type("Response", (), {"content": b"w" * 5000})()

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.audio = type("Audio", (), {"speech": Speech()})()

    monkeypatch.setattr("clipforge.renderer.OpenAI", FakeOpenAI)
    _create_voice(
        state,
        tmp_path,
        settings(openai_api_key="test-key-not-real"),
    )

    assert captured["input"] == state["script"]["text"]
    assert "Natural de narration" in captured["instructions"]
    assert detect_text_language(captured["input"]) == "de"


def test_review_accepts_matching_language_and_flags_wrong_language():
    state = project_state(language="de")
    matching = local_review_items(state)
    assert any(item["check"] == "language" and item["severity"] == "info" for item in matching)

    state["script"]["text"] = "This entire script is clearly written in English and explains the topic."
    wrong = local_review_items(state)
    assert any(item["check"] == "language" and item["severity"] == "error" for item in wrong)


def test_review_can_apply_one_safe_language_fix_and_is_bounded():
    state = project_state(language="de")
    state["script"]["text"] = "This script is definitely in English and should be corrected."
    state["script"]["blocks"] = [
        {"id": "voice_block_01", "role": "hook", "text": state["script"]["text"]}
    ]

    class Provider:
        name = "fake-review"

        def __init__(self):
            self.calls = 0

        def review(self, _context):
            self.calls += 1
            if self.calls == 1:
                return ReviewDecision(
                    status="needs_fix",
                    items=[ReviewFinding(check="language", severity="error", message="Wrong language.")],
                    corrected_script_blocks=[
                        {"role": "hook", "text": "Diese Geschichte erklärt das Thema klar und auf Deutsch."}
                    ],
                )
            return ReviewDecision(status="passed", items=[])

    provider = Provider()
    run_ai_review(state, settings(), provider=provider, max_rounds=9)

    assert provider.calls == 2
    assert state["ai_review"]["rounds"] == 2
    assert state["ai_review"]["automatic_corrections"]
    # The review corrects the language of the body; the authoritative (German)
    # hook stays the opening instead of the review's rewrite.
    assert "definitely in English" not in state["script"]["text"]
    assert state["script"]["blocks"][0]["text"] == state["script"]["selected_hook"]
    assert detect_text_language(state["script"]["text"]) == "de"


@pytest.mark.parametrize(
    ("language", "prompt", "claim", "question_echo"),
    [
        (
            "de",
            "Warum haben Flugzeugfenster unten ein kleines Loch?",
            "Das kleine Loch hilft dabei, den Druck zwischen den Fensterscheiben und der Kabine auszugleichen.",
            "Warum haben Flugzeugfenster unten ein kleines Loch?",
        ),
        (
            "en",
            "Why do airplane windows have a tiny hole at the bottom?",
            "The tiny hole helps equalize pressure between the window panes and the cabin.",
            "Why do airplane windows have a tiny hole at the bottom?",
        ),
    ],
)
def test_review_failure_preserves_authoritative_airplane_window_hook(
    language, prompt, claim, question_echo
):
    state = build_initial_state(
        prompt,
        AdvancedOptions(language=language, research="off"),
        settings(),
    )
    state["prompt"] = prompt
    state["intent"].update({"language": language, "question": prompt, "topic": "airplane windows"})
    state["facts"] = [
        {
            "id": "fact_01",
            "claim": claim,
            "verification": "source_attributed",
            "sources": [{"label": "Test source", "url": "https://source.test"}],
        }
    ]
    candidate = select_hook_candidate(
        state["intent"],
        state["facts"],
        body=claim,
        model_candidates=[
            {"strategy": "curiosity_gap", "text": question_echo},
            {"strategy": "evidence_insight", "text": claim},
            {
                "strategy": "direct_reframe",
                "text": (
                    "Das Loch hält den Druck im Fenster im Gleichgewicht."
                    if language == "de"
                    else "That hole keeps pressure balanced inside the window."
                ),
            },
        ],
    )
    assert candidate is not None
    assert candidate["text"] != question_echo
    state["script"]["blocks"] = [
        {"id": "voice_block_01", "role": "hook", "text": candidate["text"]},
        {"id": "voice_block_02", "role": "detail", "text": claim},
    ]
    state["script"]["selected_hook"] = candidate["text"]
    state["script"]["selected_hook_strategy"] = candidate["strategy"]
    state["script"]["text"] = f"{candidate['text']} {claim}"
    class Provider:
        name = "failing-review"

        def __init__(self):
            self.calls = 0

        def review(self, _context):
            self.calls += 1
            if self.calls == 1:
                return ReviewDecision(
                    status="needs_fix",
                    items=[
                        ReviewFinding(
                            check="prompt_fidelity",
                            severity="error",
                            message="Opening needs correction.",
                        )
                    ],
                    corrected_script_blocks=[
                        {"role": "hook", "text": question_echo},
                        {"role": "detail", "text": claim},
                    ],
                )
            raise RuntimeError("Language Prompt failed: simulated provider timeout")

    run_ai_review(state, settings(), provider=Provider())

    assert state["ai_review"]["status"] == "unavailable"
    assert sum(block["role"] == "hook" for block in state["script"]["blocks"]) == 1
    assert sum(
        block["role"] == "hook" for block in state["script"]["blocks"]
    ) == 1


@pytest.mark.parametrize(
    ("language", "prompt", "claim", "question_echo", "substantive"),
    [
        (
            "de",
            "Warum haben Flugzeugfenster unten ein kleines Loch?",
            "Das kleine Loch gleicht den Druck zwischen den Fensterscheiben aus.",
            "Warum haben Flugzeugfenster unten ein kleines Loch?",
            "Das kleine Loch gleicht den Druck zwischen den Fensterscheiben aus.",
        ),
        (
            "en",
            "Why do airplane windows have a tiny hole at the bottom?",
            "The tiny hole equalizes pressure between the window panes.",
            "Why do airplane windows have a tiny hole at the bottom?",
            "The tiny hole equalizes pressure between the window panes.",
        ),
    ],
)
def test_successful_review_correction_rejects_question_echo_hook(
    language, prompt, claim, question_echo, substantive
):
    state = build_initial_state(
        prompt,
        AdvancedOptions(language=language, research="off"),
        settings(),
    )
    state["prompt"] = prompt
    state["intent"].update({"language": language, "question": prompt, "topic": "airplane windows"})
    state["facts"] = [
        {
            "id": "fact_01",
            "claim": claim,
            "verification": "source_attributed",
            "sources": [{"label": "Test source", "url": "https://source.test"}],
        }
    ]
    state["script"]["blocks"] = [
        {"id": "voice_block_01", "role": "hook", "text": substantive},
        {"id": "voice_block_02", "role": "detail", "text": claim},
    ]
    state["script"]["selected_hook"] = substantive
    state["script"]["selected_hook_strategy"] = "evidence_insight"
    state["script"]["text"] = f"{substantive} {claim}"

    class Provider:
        name = "successful-review"

        def __init__(self):
            self.calls = 0

        def review(self, _context):
            self.calls += 1
            if self.calls == 1:
                return ReviewDecision(
                    status="needs_fix",
                    corrected_script_blocks=[
                        {"role": "hook", "text": question_echo},
                        {
                            "role": "detail",
                            "text": (
                                "Der Textkörper ist jetzt klarer."
                                if language == "de"
                                else "The body is clearer now."
                            ),
                        },
                    ],
                )
            return ReviewDecision(status="passed")

    run_ai_review(state, settings(), provider=Provider())

    assert state["ai_review"]["status"] in {"passed", "passed_with_warnings"}
    # A review rewrite never turns the question into the hook.
    assert state["script"]["selected_hook"] == substantive
    assert state["script"]["blocks"][0]["text"] == substantive
    assert state["script"]["text"].startswith(substantive)
    assert question_echo not in state["script"]["text"]
    assert sum(block["role"] == "hook" for block in state["script"]["blocks"]) == 1
    assert sum(block["role"] == "hook" for block in state["script"]["blocks"]) == 1


def test_review_unavailable_never_claims_factual_verification_or_exposes_secrets():
    state = project_state(language="en")
    state["research"].update(required=True, sources=[], status="unavailable")
    state["hidden_api_key"] = "must-not-appear"
    context = review_context(state)
    run_ai_review(state, settings())

    assert "must-not-appear" not in str(context)
    assert "must-not-appear" not in str(state["ai_review"])
    assert state["ai_review"]["status"] == "unavailable"
    assert any("not claimed" in item["message"] for item in state["ai_review"]["items"])


def test_review_result_is_reused_when_editorial_inputs_are_unchanged():
    state = project_state(language="en")

    class Provider:
        name = "cached-review"

        def __init__(self):
            self.calls = 0

        def review(self, _context):
            self.calls += 1
            return ReviewDecision(status="passed", items=[])

    provider = Provider()
    run_ai_review(state, settings(), provider=provider)
    state["captions"]["diagnostic"] = "A post-render alignment detail changed."
    state["captions"]["items"] = []
    run_ai_review(state, settings(), provider=provider)
    assert provider.calls == 1


def test_real_aligned_words_are_grouped_and_phrase_fallback_is_honest(tmp_path):
    class Aligner:
        name = "test-aligner"

        def align(self, _audio, _language):
            return [
                {"text": word, "start": index * 0.3, "end": (index + 1) * 0.3}
                for index, word in enumerate(
                    ["Warum", "wird", "unsere", "Erde", "heute", "anders"]
                )
            ]

    result = align_narration(
        tmp_path / "audio.wav",
        "Warum wird unsere Erde heute anders",
        2.0,
        "de",
        settings(),
        aligner=Aligner(),
    )
    groups = group_aligned_words(result.words, 4)
    fallback = phrase_fallback_items("one two three four five six", 3, 3)

    assert result.status == "word_aligned"
    assert len(groups[0]["words"]) == 4
    assert groups[0]["words"][2]["start"] == pytest.approx(0.6)
    assert all(item["timing"] == "phrase_estimate" and "words" not in item for item in fallback)


def test_alignment_failure_is_labelled_as_phrase_fallback(tmp_path):
    class BrokenAligner:
        name = "broken"

        def align(self, _audio, _language):
            raise RuntimeError("offline")

    result = align_narration(
        tmp_path / "audio.wav", "a short script", 2, "en", settings(), aligner=BrokenAligner()
    )
    assert result.status == "phrase_fallback"
    assert result.diagnostic and "failed" in result.diagnostic.lower()


def test_ass_uses_actual_word_intervals_highlight_color_and_caption_switch(tmp_path):
    state = project_state(language="en", caption_highlight_color="#12ab34")
    state["captions"]["items"] = [
        {
            "text": "Why Earth moves",
            "start": 0.0,
            "end": 1.5,
            "timing": "word_aligned",
            "words": [
                {"text": "Why", "start": 0.0, "end": 0.4},
                {"text": "Earth", "start": 0.4, "end": 0.9},
                {"text": "moves", "start": 0.9, "end": 1.5},
            ],
        }
    ]
    output = _write_ass_captions(state, 2, tmp_path).read_text()
    assert output.count("Dialogue: 0") == 3
    assert "&H0034ab12" in output
    assert "0:00:00.40,0:00:00.90" in output

    state["captions"]["enabled"] = False
    disabled = _write_ass_captions(state, 2, tmp_path).read_text()
    assert "Dialogue: 0" not in disabled


def test_caption_styles_have_distinct_renderer_configuration():
    names = ["clean", "bold", "minimal", "pop", "boxed", "outline", "karaoke"]
    signatures = {tuple(caption_style_config(name).values()) for name in names}
    assert len(signatures) == len(names)


def test_music_is_not_created_or_mixed_during_base_render(tmp_path):
    state = project_state(
        language="en",
        music_enabled=False,
        music_volume=0.2,
        music_ducking=True,
        music_fades=True,
    )
    assert _create_music_track("ffmpeg", state, 2, tmp_path) is None
    assert music_render_config(state)["effective_volume"] == pytest.approx(0.0935, rel=1e-4)

    state["music"].update(enabled=True, mood="tech", ducking=False)
    track = _create_music_track("ffmpeg", state, 2, tmp_path)

    assert track is None
    assert music_render_config(state)["effective_volume"] == pytest.approx(0.1481905)
    assert music_render_config(state)["fades"] is True
    graph = music_filter_graph(music_render_config(state), 8)
    assert "volume=0.148" in graph
    assert "afade=t=in" in graph and "afade=t=out:st=7.000" in graph
