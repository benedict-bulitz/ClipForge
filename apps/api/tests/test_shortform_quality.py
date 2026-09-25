from pathlib import Path

import pytest
from pydantic import ValidationError

from clipforge.ai import (
    DIRECTOR_INSTRUCTIONS,
    AIHookCandidate,
    AIHookGenerationResult,
    AIIntent,
    AIPlanResult,
    AIProjectPlan,
    AIScriptBlock,
)
from clipforge.alignment import alignment_readiness, phrase_fallback_items
from clipforge.config import Settings
from clipforge.narration import clean_narration_text, contamination_issues
from clipforge.pipeline import (
    _apply_selected_hook,
    _factual_blocks,
    _fit_blocks,
    _normalise_blocks,
    _refresh_script_derivatives,
    apply_edit,
    build_initial_state,
    enforce_selected_hook,
)
from clipforge.renderer import (
    RenderUnavailable,
    _create_voice,
    _write_ass_captions,
    readiness,
    render_video,
)
from clipforge.review import ReviewDecision, local_review_items, run_ai_review
from clipforge.schemas import AdvancedOptions


def settings(tmp_path: Path | None = None, **updates) -> Settings:
    values = {"clipforge_ai_mode": "local", "openai_api_key": None}
    if tmp_path is not None:
        values["render_root"] = tmp_path
    values.update(updates)
    return Settings(**values)


def state(**options):
    return build_initial_state(
        "Tell a clear story about a curious astronaut on the moon",
        AdvancedOptions(language="en", research="off", **options),
        settings(),
    )


YELLOWSTONE = """CONTEXT
Yellowstone National Park covers nearly...
CAUSE
According to the U.S. Geological ... editor. ...
<strong>it would bring about a calamity for most of the United States</strong>
TURN
...
PAYOFF
Although another catastrophic eruption...
<strong>scientists are not convinced that one will ever happen</strong>"""


def test_narration_cleaner_removes_markup_scaffolding_and_snippet_artifacts():
    cleaned = clean_narration_text(YELLOWSTONE)
    lowered = cleaned.lower()
    assert "<strong>" not in cleaned
    assert not {"CONTEXT", "CAUSE", "TURN", "PAYOFF"}.intersection(cleaned.split())
    assert "editor" not in lowered
    assert "..." not in cleaned
    assert cleaned == (
        "it would bring about a calamity for most of the United States. "
        "scientists are not convinced that one will ever happen."
    )
    assert contamination_issues(cleaned) == []


def test_narration_cleaner_preserves_legitimate_editor_and_ellipsis_speech():
    cleaned = clean_narration_text("The editor chose the final cut. Wait... Then the screen changed.")
    assert cleaned == "The editor chose the final cut. Wait. Then the screen changed."


def test_markdown_and_boilerplate_are_removed_from_spoken_text():
    text = (
        "The short answer to 'Why?': **Air scatters blue light.** "
        "[Source](https://invalid.test) [1] (Example Institute, 2024)"
    )
    assert clean_narration_text(text) == "Air scatters blue light."


def test_research_is_cleaned_evidence_not_raw_snippet_narration():
    blocks = _factual_blocks(
        {"language": "en"},
        [
            {"claim": "CONTEXT\nA park covers 2,200,000 acres..."},
            {"claim": "According to Example Institute, magma is melted rock underground."},
        ],
    )
    spoken = " ".join(block["text"] for block in blocks)
    assert "CONTEXT" not in spoken
    assert "According to" not in spoken
    assert "acres" not in spoken
    assert "magma is melted rock underground" in spoken


def test_director_contract_is_answer_first_accessible_and_max_is_a_ceiling():
    lowered = DIRECTOR_INSTRUCTIONS.lower()
    assert "curiosity hook" in lowered
    assert "never saw the user's prompt" in lowered
    assert "next sentence" in lowered
    assert "zero prior knowledge" in lowered
    assert "do not pad" in lowered
    assert "maximum duration" in lowered
    fallback = _factual_blocks(
        {"language": "en", "question": "Why is the sky blue?", "topic": "sky blue"},
        [{"claim": "Blue light scatters more strongly."}],
    )
    assert not fallback[0]["text"].startswith("The short answer to")
    assert fallback[0]["role"] == "answer"
    assert len(fallback) == 1
    assert "According to" not in fallback[0]["text"]


def test_earth_fallback_keeps_the_answer_when_no_real_hook_is_available():
    intent = {
        "language": "de",
        "question": "Warum wird die Erde nicht schwerer, wenn wir auf ihr bauen?",
        "topic": "Warum wird die Erde nicht schwerer wenn wir auf ihr bauen",
    }
    blocks = _factual_blocks(
        intent,
        [
            {
                "claim": (
                    "Die Erde wird durch ein gebautes Haus nicht schwerer, weil das "
                    "Baumaterial bereits zur Erde gehört."
                )
            },
            {
                "claim": (
                    "Beim Bauen wird Materie nur umgeformt und an einen anderen Ort bewegt."
                )
            },
        ],
    )

    assert blocks[0]["role"] == "answer"
    assert "Baumaterial" in blocks[0]["text"]
    assert blocks[1]["role"] == "support"
    # The body fallback writes no hook of its own: Triple Hook V2 owns the opening.
    assert all(block["role"] != "hook" for block in blocks)


def test_editorial_publication_guidance_and_all_structural_labels_are_removed():
    contaminated = """HOOK
Warum wird die Erde nicht schwerer?
ANSWER
Materie wird nur bewegt.
DETAIL
That assessment is based on the provided material; add the original source links before publication.
SUPPORT
Die Gesamtmasse bleibt gleich."""
    cleaned = clean_narration_text(contaminated)

    assert "add the original source links" not in cleaned.casefold()
    assert "provided material" not in cleaned.casefold()
    assert not {"HOOK", "ANSWER", "DETAIL", "SUPPORT"}.intersection(cleaned.split())
    assert contamination_issues(contaminated) == [
        "standalone structural label",
        "editorial instruction",
    ]
    assert contamination_issues(cleaned) == []


def test_duration_defaults_are_dynamic_and_validate_bounds():
    defaults = AdvancedOptions()
    assert defaults.min_duration is None
    assert defaults.max_duration == 60
    assert defaults.pacing == "fast"
    simple = state()
    assert simple["duration"]["estimated_seconds"] < 60
    assert simple["duration"]["max_seconds"] == 60
    assert state(min_duration=30, max_duration=60)["duration"]["estimated_seconds"] == 30
    assert state(max_duration=10)["duration"]["estimated_seconds"] <= 10
    with pytest.raises(ValidationError):
        AdvancedOptions(min_duration=61, max_duration=60)


def test_shortening_drops_complete_low_value_sentences_not_mid_sentence():
    blocks = [{
        "role": "answer",
        "text": (
            "First complete answer. Second useful supporting detail. "
            "Third low value unnecessary filler detail."
        ),
    }]
    fitted = _fit_blocks(blocks, max_duration=5, wpm=120)
    assert fitted[0]["text"].endswith(".")
    assert "Third low value" not in fitted[0]["text"]
    assert "…" not in fitted[0]["text"]


def test_fast_cut_speed_reaches_scenes_without_changing_voice_speed():
    fast = state(pacing="fast")
    relaxed = state(pacing="slow")
    assert fast["timeline"]["cut_pace"] == "fast"
    assert all(scene["motion"] == "fast_cut" for scene in fast["scenes"])
    assert all(scene["motion"] == "slow_push" for scene in relaxed["scenes"])
    assert len(fast["scenes"]) >= len(relaxed["scenes"])
    assert all(len(scene["narration"].split()) >= 2 for scene in fast["scenes"])
    assert fast["voice"]["speed"] == relaxed["voice"]["speed"]


def test_editor_script_change_reapplies_spoken_text_invariant(monkeypatch):
    project = state()
    project["script"]["blocks"][0]["text"] = "CONTEXT\n<strong>Clean this speech</strong>"
    directive = type(
        "Directive",
        (),
        {
            "components": ["script"],
            "clarification": None,
            "script_action": "shorter",
            "caption_action": None,
            "voice_gender": None,
            "voice_tone": None,
            "voice_speed": None,
            "change_speaker": False,
            "visual_action": None,
        },
    )()
    monkeypatch.setattr("clipforge.pipeline.interpret_edit", lambda *_args, **_kwargs: directive)
    edited, _ = apply_edit(project, "Make it shorter", settings())
    assert contamination_issues(edited["script"]["text"]) == []
    assert "CONTEXT" not in edited["script"]["text"]


def test_ai_review_correction_cleans_spoken_text():
    project = state()

    class Provider:
        name = "fake"

        def __init__(self):
            self.calls = 0

        def review(self, _context):
            self.calls += 1
            if self.calls == 1:
                return ReviewDecision(
                    status="needs_fix",
                    corrected_script_blocks=[
                        {"role": "ANSWER", "text": "<b>The answer is simple.</b>"}
                    ],
                )
            return ReviewDecision(status="passed")

    run_ai_review(project, settings(), provider=Provider())
    selected_hook = project["script"]["selected_hook"]
    assert selected_hook is None
    assert project["script"]["text"] == "The answer is simple."
    assert contamination_issues(project["script"]["text"]) == []


def test_ai_review_flags_preamble_contamination_repetition_and_low_value_detail():
    project = state()
    bad = (
        "The short answer to this starts here: <strong>According to Example Institute, "
        "the site covers 2,000 acres.</strong> The site covers 2,000 acres. "
        "The site covers 2,000 acres."
    )
    project["script"]["text"] = bad
    project["script"]["blocks"] = [{"id": "voice_block_01", "role": "hook", "text": bad}]
    checks = {item["check"] for item in local_review_items(project)}
    assert {"directness", "contamination", "relevance", "repetition"}.issubset(checks)


def test_tts_and_captions_receive_only_canonical_spoken_text(tmp_path, monkeypatch):
    project = state()
    project["script"]["blocks"] = [
        {"id": "voice_block_01", "role": "ANSWER", "text": YELLOWSTONE}
    ]
    _refresh_script_derivatives(project)
    captured = {}

    class Speech:
        def create(self, **kwargs):
            captured.update(kwargs)
            return type("Response", (), {"content": b"a" * 5000})()

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.audio = type("Audio", (), {"speech": Speech()})()

    monkeypatch.setattr("clipforge.renderer.OpenAI", FakeOpenAI)
    _create_voice(project, tmp_path, settings(openai_api_key="unit-test-key"))
    caption_text = " ".join(item["text"] for item in project["captions"]["items"])
    assert captured["input"] == project["script"]["text"]
    assert caption_text == project["script"]["text"]
    assert contamination_issues(captured["input"]) == []


def test_ass_highlights_exactly_one_word_and_fallback_is_not_word_perfect(tmp_path):
    project = state()
    project["attention_events"] = []
    project["captions"]["items"] = [
        {
            "text": "Blue light scatters",
            "start": 0,
            "end": 1.5,
            "timing": "word_aligned",
            "words": [
                {"text": "Blue", "start": 0, "end": 0.5},
                {"text": "light", "start": 0.5, "end": 1.0},
                {"text": "scatters", "start": 1.0, "end": 1.5},
            ],
        }
    ]
    ass = _write_ass_captions(project, 2, tmp_path).read_text()
    assert ass.count("Dialogue:") == 3
    for line in [line for line in ass.splitlines() if line.startswith("Dialogue:")]:
        assert line.lower().count("&h003868ff") == 1
    fallback = phrase_fallback_items("Blue light scatters", 2, 4)
    assert fallback[0]["timing"] == "phrase_estimate"
    assert "words" not in fallback[0]
    project["captions"]["items"][0]["timing"] = "phrase_estimate"
    estimated = _write_ass_captions(project, 2, tmp_path).read_text()
    assert estimated.count("Dialogue:") == 1


def test_render_rejects_contamination_before_cached_voice_or_captions(tmp_path):
    project = state()
    project["script"]["text"] = "<strong>Never render this.</strong>"
    with pytest.raises(RenderUnavailable, match="Narration validation"):
        render_video(project, "project", 1, settings(tmp_path))


def test_alignment_readiness_distinguishes_dependency_and_model(monkeypatch, tmp_path):
    monkeypatch.setattr("clipforge.alignment.importlib.util.find_spec", lambda _name: None)
    missing = alignment_readiness(settings())
    assert not missing.ready
    assert missing.status == "Alignment dependency missing"

    monkeypatch.setattr("clipforge.alignment.importlib.util.find_spec", lambda _name: object())
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    preparing = alignment_readiness(settings())
    assert not preparing.ready
    assert preparing.status == "Preparing local alignment"
    snapshot = tmp_path / "models--Systran--faster-whisper-tiny" / "snapshots" / "test"
    snapshot.mkdir(parents=True)
    (snapshot / "model.bin").write_bytes(b"test")
    ready = readiness(settings())["alignment"]
    assert ready["ready"] is True
    assert ready["status"] == "Word alignment ready"


def test_standard_setup_includes_local_alignment_dependency():
    root = Path(__file__).resolve().parents[3]
    pyproject = (root / "apps/api/pyproject.toml").read_text()
    bootstrap = (root / "scripts/bootstrap.sh").read_text()
    assert '"faster-whisper>=1.1,<2"' in pyproject.split("[project.optional-dependencies]")[0]
    assert "import faster_whisper" in bootstrap


def test_authoritative_hook_collapses_zero_one_and_many_input_hooks():
    intent = {
        "language": "de",
        "question": "Wieso wird die Erde nicht schwerer, wenn wir darauf bauen?",
        "topic": "die Erde wird beim Bauen nicht schwerer",
        "content_type": "explanation",
    }
    facts = [
        {
            "claim": "Beim Bauen wird Materie, die bereits auf der Erde vorhanden ist, nur umverteilt.",
            "sources": [{"label": "Source", "url": "https://source.test"}],
            "importance": 0.9,
        }
    ]
    candidates = [
        {
            "strategy": "direct_reframe",
            "text": "Bauen macht die Erde nicht schwerer, sondern verteilt nur vorhandene Materie neu.",
        }
    ]
    body = [{"role": "answer", "text": facts[0]["claim"]}]

    facts = [{"id": "fact_01", "verification": "source_attributed", **facts[0]}]
    selected = candidates[0]["text"]

    def state_with(blocks: list[dict]) -> dict:
        return {
            "intent": intent,
            "facts": facts,
            "script": {
                "blocks": [dict(block) for block in blocks],
                "triple_hook": {
                    "version": 2, "verbal_hook": selected, "selected_strategy": "direct_reframe",
                    "selection": {"candidates": [{"strategy": "direct_reframe", "verbal_hook": selected, "eligible": True}]},
                },
            },
        }

    for blocks in (
        body,
        [{"role": "hook", "text": intent["question"]}, *body],
        [{"role": "hook", "text": "Erste schwache Hook-Frage?"}, {"role": "hook", "text": "Zweite schwache Hook-Frage?"}, *body],
    ):
        state = state_with(blocks)
        enforce_selected_hook(state)
        result = state["script"]["blocks"]
        assert [block["role"] for block in result].count("hook") == 1
        assert result[0]["role"] == "hook" and result[0]["text"] == selected
        assert result[0]["text"] != intent["question"]
        assert state["script"]["selected_hook"] == state["script"]["triple_hook"]["verbal_hook"] == selected


def test_normalisation_preserves_multi_sentence_hook_without_duplication():
    hook = "Beim Bauen wird Materie nur umverteilt. Nichts Neues kommt hinzu."
    blocks = _normalise_blocks(
        [
            {"role": "hook", "text": hook},
            {"role": "answer", "text": "Die Gesamtmasse der Erde bleibt praktisch gleich."},
        ],
        max_duration=60,
    )
    restored = _apply_selected_hook(blocks, hook)
    assert restored[0]["role"] == "hook"
    assert restored[0]["text"] == hook
    assert " ".join(block["text"] for block in restored).count("Nichts Neues kommt hinzu.") == 1

    tight = _fit_blocks(
        [
            {"role": "hook", "text": hook},
            {
                "role": "answer",
                "text": (
                    "Die Gesamtmasse bleibt gleich, weil Baumaterial bereits zur Erde gehört "
                    "und nur von einem Ort zum anderen bewegt wird."
                ),
            },
        ],
        max_duration=8,
    )
    assert tight[0]["role"] == "hook"
    assert tight[0]["text"] == hook


def test_earth_director_without_hook_block_still_opens_with_selected_hook(monkeypatch, tmp_path):
    prompt = "Wieso wird die Erde nicht schwerer, wenn wir darauf bauen?"
    evidence = "Beim Bauen wird Materie, die bereits auf der Erde vorhanden ist, nur umverteilt."

    def fake_research(*_args, **_kwargs):
        return type(
            "ResearchResult",
            (),
            {
                "status": "ok",
                "provider": "mock",
                "error": None,
                "sources": [{"label": "Source", "url": "https://source.test"}],
                "facts": [
                    {
                        "claim": evidence,
                        "confidence": 0.9,
                        "importance": 0.9,
                        "sources": [{"label": "Source", "url": "https://source.test"}],
                    }
                ],
            },
        )()

    plan = AIProjectPlan(
        intent=AIIntent(
            topic="die Erde wird beim Bauen nicht schwerer",
            intent="explain",
            question=prompt,
            language="de",
            content_type="explanation",
            tone="fast_documentary",
            research_required=True,
            visual_style="documentary_graphics",
        ),
        research_questions=["Warum wird die Erde beim Bauen nicht schwerer?"],
        facts=[],
        answer_skeleton=["ANSWER", "SUPPORT"],
        script_blocks=[
            AIScriptBlock(role="answer", text=evidence),
            AIScriptBlock(
                role="support",
                text="Die Gesamtmasse der Erde bleibt deshalb praktisch gleich.",
            ),
        ],
        music_mood="documentary",
        hook_candidates=[
            AIHookCandidate(
                strategy="curiosity_gap",
                text="Warum bleibt die Erde beim Bauen gleich schwer?",
            ),
            AIHookCandidate(
                strategy="direct_reframe",
                text="Bauen macht die Erde nicht schwerer, sondern verteilt nur vorhandene Materie neu.",
            ),
            AIHookCandidate(
                strategy="direct_reframe",
                text="Ein Haus macht die Erde nicht schwerer, sondern verteilt nur Materie neu.",
            ),
        ],
        selected_hook_strategy="evidence_insight",
    )

    monkeypatch.setattr("clipforge.pipeline.research_topic", fake_research)
    monkeypatch.setattr(
        "clipforge.pipeline.plan_with_openai",
        lambda *_args, **_kwargs: AIPlanResult(plan, "connected"),
    )
    monkeypatch.setattr(
        "clipforge.pipeline.generate_hook_candidates_with_openai",
        lambda *_args, **_kwargs: AIHookGenerationResult(
            [{
                "strategy": "direct_reframe",
                "text": "Bauen macht die Erde nicht schwerer, sondern verteilt nur vorhandene Materie neu.",
            }],
            "direct_reframe",
            "connected",
        ),
    )

    state = build_initial_state(
        prompt,
        AdvancedOptions(language="de", research="on"),
        settings(tmp_path, clipforge_ai_mode="openai", openai_api_key="test-key-not-real"),
    )

    selected_hook = state["script"]["selected_hook"]
    assert selected_hook
    assert state["script"]["selected_hook_strategy"]
    assert state["script"]["blocks"][0]["role"] == "hook"
    assert state["script"]["blocks"][0]["text"] == selected_hook
    assert [block["role"] for block in state["script"]["blocks"]].count("hook") == 1
    assert selected_hook != prompt
    assert "Materie" in selected_hook or "schwerer" in selected_hook.casefold()
    assert state["script"]["text"].startswith(selected_hook)
    assert state["captions"]["items"]
    assert state["captions"]["items"][0]["text"].split()[0] == selected_hook.split()[0]

    captured: dict = {}

    class Speech:
        def create(self, **kwargs):
            captured.update(kwargs)
            return type("Response", (), {"content": b"w" * 5000})()

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.audio = type("Audio", (), {"speech": Speech()})()

    monkeypatch.setattr("clipforge.renderer.OpenAI", FakeOpenAI)
    tts_state = {
        **state,
        "voice": {
            **state["voice"],
            "provider": "openai",
            "voice_id": "marin",
            "model": "gpt-4o-mini-tts",
            "speed": 1.0,
        },
    }
    _create_voice(
        tts_state,
        tmp_path,
        settings(tmp_path, openai_api_key="test-key-not-real"),
    )
    assert str(captured["input"]).startswith(selected_hook)
