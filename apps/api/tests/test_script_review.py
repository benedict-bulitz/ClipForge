from dataclasses import replace
from types import SimpleNamespace

from clipforge.config import Settings
from clipforge.pipeline import build_initial_state
from clipforge.research import ResearchResult
from clipforge.review import ReviewDecision, ReviewFinding, run_ai_review
from clipforge.schemas import AdvancedOptions
from clipforge.script_review import (
    SCRIPT_REVIEW_V2_INSTRUCTIONS,
    OpenAIScriptReviewProvider,
    ScriptReviewRequest,
    ScriptReviewResponse,
    ScriptReviewResult,
    compact_script_draft,
    review_script_v2,
)
from clipforge.script_writer import (
    ScriptBlockV2,
    ScriptDraftV2,
    ScriptWriterFact,
    ScriptWriterResult,
)


def _fact(fact_id: str = "fact_01", verification: str = "supported") -> ScriptWriterFact:
    return ScriptWriterFact(
        id=fact_id,
        claim="Das Loch gleicht den Druck zwischen den Scheiben aus.",
        verification=verification,
    )


def _draft() -> ScriptDraftV2:
    return ScriptDraftV2(
        language="de",
        blocks=[
            ScriptBlockV2(
                role="answer",
                text="Das Loch hilft beim Druckausgleich.",
                fact_ids=["fact_01"],
            ),
            ScriptBlockV2(
                role="explanation",
                text="Flugzeugfenster bestehen aus drei Scheiben.",
                fact_ids=["fact_01"],
            ),
        ],
    )


def _request(draft: ScriptDraftV2 | None = None) -> ScriptReviewRequest:
    return ScriptReviewRequest(
        prompt="Warum haben Flugzeugfenster unten ein kleines Loch?",
        language="de",
        tone="fast_documentary",
        audience="general",
        content_type="factual_explainer",
        draft=draft or _draft(),
        facts=[_fact()],
    )


class FakeReviewProvider:
    name = "fixture-review"

    def __init__(self, result: ScriptReviewResult):
        self.result = result
        self.requests: list[ScriptReviewRequest] = []

    def review(self, request: ScriptReviewRequest) -> ScriptReviewResult:
        self.requests.append(request)
        return self.result


def test_review_approve_keeps_text_and_fact_ids() -> None:
    original = _draft()
    provider = FakeReviewProvider(
        ScriptReviewResult(ScriptReviewResponse(status="approve"), "approved")
    )
    result = review_script_v2(_request(original), provider)
    assert result.status == "approved"
    assert compact_script_draft(_request(original).draft) == compact_script_draft(original)


def test_review_instructions_cover_quality_without_forcing_brevity() -> None:
    instructions = SCRIPT_REVIEW_V2_INSTRUCTIONS.casefold()
    for phrase in (
        "unnecessary reassurance",
        "repeated causal explanations",
        "artificial payoffs",
        "shortest complete answer",
        "do not shorten merely to shorten",
        "useful causal context",
        "payoff is not required",
    ):
        assert phrase in instructions


def test_review_revise_accepts_valid_body_and_ids() -> None:
    revised = ScriptDraftV2(
        language="de",
        blocks=[
            ScriptBlockV2(
                role="answer",
                text="Das Loch gleicht den Druck im Fenster aus.",
                fact_ids=["fact_01"],
            ),
            ScriptBlockV2(
                role="payoff",
                text="So bleibt der Aufbau stabil.",
                fact_ids=["fact_01"],
            ),
        ],
    )
    provider = FakeReviewProvider(
        ScriptReviewResult(
            ScriptReviewResponse(status="revise", draft=revised),
            "revised",
        )
    )
    result = review_script_v2(_request(), provider)
    assert result.status == "revised"
    assert result.response is not None
    assert result.response.draft == revised


def test_invalid_review_keeps_writer_draft() -> None:
    provider = FakeReviewProvider(
        ScriptReviewResult(None, "validation_error", "unknown fact ID")
    )
    result = review_script_v2(_request(), provider)
    assert result.status == "validation_error"
    assert result.response is None


def test_unknown_fact_review_draft_is_rejected(monkeypatch) -> None:
    invalid = ScriptDraftV2(
        language="de",
        blocks=[
            ScriptBlockV2(role="answer", text="Neue Aussage.", fact_ids=["missing"]),
            ScriptBlockV2(role="payoff", text="Ende."),
        ],
    )
    monkeypatch.setattr(
        "clipforge.script_review.OpenAI",
        lambda **_: SimpleNamespace(
            responses=SimpleNamespace(
                parse=lambda **_: SimpleNamespace(
                    output_parsed=ScriptReviewResponse(status="revise", draft=invalid)
                )
            )
        ),
    )
    result = OpenAIScriptReviewProvider(Settings(openai_api_key="test-key")).review(_request())
    assert result.status == "validation_error"
    assert "unknown fact ID" in (result.error or "")


def test_unsupported_fact_review_draft_is_rejected(monkeypatch) -> None:
    invalid = ScriptDraftV2(
        language="de",
        blocks=[
            ScriptBlockV2(role="answer", text="Nicht belegte Aussage.", fact_ids=["fact_01"]),
            ScriptBlockV2(role="payoff", text="Ende."),
        ],
    )
    monkeypatch.setattr(
        "clipforge.script_review.OpenAI",
        lambda **_: SimpleNamespace(
            responses=SimpleNamespace(
                parse=lambda **_: SimpleNamespace(
                    output_parsed=ScriptReviewResponse(status="revise", draft=invalid)
                )
            )
        ),
    )
    result = OpenAIScriptReviewProvider(Settings(openai_api_key="test-key")).review(
        replace(_request(), facts=[_fact(verification="unsupported")])
    )
    assert result.status == "validation_error"
    assert "unsupported fact ID" in (result.error or "")


def test_malformed_review_result_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(
        "clipforge.script_review.OpenAI",
        lambda **_: SimpleNamespace(
            responses=SimpleNamespace(
                parse=lambda **_: SimpleNamespace(output_parsed=None)
            )
        ),
    )
    result = OpenAIScriptReviewProvider(Settings(openai_api_key="test-key")).review(_request())
    assert result.status == "provider_error"


def test_provider_failure_is_explicit() -> None:
    provider = FakeReviewProvider(ScriptReviewResult(None, "provider_error", "offline"))
    result = review_script_v2(_request(), provider)
    assert result.status == "provider_error"
    assert result.error == "offline"


def test_openai_review_uses_worker_model_and_structured_output(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Responses:
        def parse(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(output_parsed=ScriptReviewResponse(status="approve"))

    settings = Settings(openai_api_key="test-key", openai_worker_model="worker-model")
    monkeypatch.setattr(
        "clipforge.script_review.OpenAI",
        lambda **_: SimpleNamespace(responses=Responses()),
    )
    result = OpenAIScriptReviewProvider(settings).review(_request())
    assert result.status == "approved"
    assert captured["model"] == "worker-model"
    assert captured["text_format"] is ScriptReviewResponse


def _plan(prompt: str):
    class Plan:
        def model_dump(self, mode="json"):
            return {
                "intent": {
                    "topic": prompt.rstrip("?"),
                    "intent": "explain",
                    "question": prompt,
                    "language": "de",
                    "content_type": "factual_explainer",
                    "tone": "fast_documentary",
                    "research_required": True,
                    "visual_style": "documentary_graphics",
                    "shortform": True,
                },
                "script_blocks": [{"role": "hook", "text": "Legacy hook."}],
                "hook_candidates": [],
                "visual_intents": [],
            }

    return Plan()


def test_v2_diagnostics_persist_writer_and_reviewed_drafts(monkeypatch) -> None:
    prompt = "Warum haben Flugzeugfenster unten ein kleines Loch?"
    facts = [{
        "claim": "Das Loch gleicht den Druck zwischen den Scheiben aus.",
        "verification": "source_snippet",
        "sources": [{"label": "source", "url": "https://source.test"}],
    }]
    writer = type(
        "Writer",
        (),
        {
            "name": "fixture-writer",
            "generate": lambda self, request: ScriptWriterResult(_draft(), "connected"),
        },
    )()
    reviewer = FakeReviewProvider(
        ScriptReviewResult(ScriptReviewResponse(status="approve"), "approved")
    )
    monkeypatch.setattr(
        "clipforge.pipeline.research_topic",
        lambda *_args, **_kwargs: ResearchResult(facts, [], "verified_sources", "fixture"),
    )
    monkeypatch.setattr(
        "clipforge.pipeline.plan_with_openai",
        lambda *_args, **_kwargs: SimpleNamespace(
            plan=_plan(prompt), status="connected", error=None
        ),
    )
    state = build_initial_state(
        prompt,
        AdvancedOptions(language="de", research="on"),
        Settings(clipforge_ai_mode="local", openai_api_key="test-key"),
        script_writer_provider=writer,
        script_review_provider=reviewer,
    )
    diagnostics = state["script"]["script_writer_v2"]
    assert diagnostics["writer_draft"]
    assert diagnostics["reviewed_draft"]
    assert diagnostics["review"]["status"] == "approved"
    assert state["script"]["narration_owned_by_v2"] is True
    assert state["script"]["blocks"][1]["fact_ids"] == ["fact_01"]


def test_v2_owned_narration_cannot_be_rewritten_by_legacy_review() -> None:
    original = _draft()
    state = {
        "prompt": "Warum haben Flugzeugfenster unten ein kleines Loch?",
        "intent": {"language": "de", "content_type": "factual_explainer"},
        "research": {"required": False, "sources": []},
        "facts": [],
        "script": {
            "text": " ".join(block.text for block in original.blocks),
            "blocks": [
                {"role": block.role, "text": block.text, "fact_ids": block.fact_ids}
                for block in original.blocks
            ],
            "narration_owned_by_v2": True,
        },
        "scenes": [{"start": 0, "end": 1, "narration": "x"}],
        "duration": {"max_seconds": 60, "speaking_rate_wpm": 165, "estimated_seconds": 2},
        "captions": {"enabled": False, "items": [], "style": "clean"},
        "voice": {},
    }

    class LegacyReview:
        name = "legacy-fixture"

        def review(self, _context):
            return ReviewDecision(
                status="needs_fix",
                items=[ReviewFinding(check="script", severity="warning", message="rewrite")],
                corrected_script_blocks=[
                    {"role": "answer", "text": "Es ist kein Schaden, sondern ein Teil des Fensters."}
                ],
            )

    before = state["script"]["blocks"].copy()
    run_ai_review(state, Settings(), provider=LegacyReview())
    assert state["script"]["blocks"] == before
    assert state["script"]["blocks"][0]["fact_ids"] == ["fact_01"]
