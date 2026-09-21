from clipforge.media import MediaCandidate, media_relevance, verify_media_shortlist
from clipforge.visual_verifier import UnavailableVisualVerifier, VisualVerification


def candidate(
    identifier: str, *, title: str = "", query: str = "house foundation", rank: float = 100
) -> MediaCandidate:
    return MediaCandidate(identifier, "video", "https://cdn.test/media", "https://source.test", "Tester", None, 1080, 1920, 8.0, query, rank, title=title, preview_url="https://cdn.test/preview.jpg")


class FakeVerifier:
    status = "available"

    def __init__(self, scores):
        self.scores = scores
        self.calls = []

    def verify_candidate(self, item, texts):
        self.calls.append((item.provider_id, texts))
        value = self.scores[item.provider_id]
        return value if isinstance(value, VisualVerification) else VisualVerification(value, "verified", "fake")


def scene():
    return {"narration": "Workers pouring concrete foundation", "visual_goal": "house foundation construction", "visual_intent": {"visual_goal": "house foundation construction", "objects": ["house", "foundation", "concrete"], "actions": ["pouring"], "context": ["construction site"]}}


def test_unknown_metadata_can_be_promoted_by_strong_visual_match():
    item = candidate("unknown")
    assert media_relevance(item, scene())["confidence"] == "unknown"
    rows = verify_media_shortlist([item], scene(), {"intent": {"topic": "house"}}, FakeVerifier({"unknown": 0.8}))
    assert rows[0][1]["confidence"] == "acceptable"


def test_low_visual_match_rejects_unknown_and_metadata_supported_candidates():
    items = [candidate("unknown"), candidate("relevant", title="Workers pouring concrete foundation")]
    rows = verify_media_shortlist(items, scene(), {"intent": {"topic": "house"}}, FakeVerifier({"unknown": 0.05, "relevant": 0.05}))
    assert all(row[1]["confidence"] == "rejected" for row in rows)


def test_rejected_metadata_is_not_resurrected():
    item = candidate("dog", title="Dog playing in a park")
    rows = verify_media_shortlist([item], scene(), {"intent": {"topic": "house"}}, FakeVerifier({"dog": 0.99}))
    assert rows == []


def test_verifier_unavailable_preserves_metadata_fallback():
    item = candidate("unknown")
    rows = verify_media_shortlist([item], scene(), {"intent": {"topic": "house"}}, UnavailableVisualVerifier())
    assert rows[0][1]["visual"]["status"] == "unavailable_dependency"
    assert rows[0][1]["confidence"] == "unknown"


def test_visual_intent_text_is_bounded_and_structured():
    verifier = FakeVerifier({"known": 0.5})
    verify_media_shortlist([candidate("known", title="Workers pouring concrete foundation")], scene(), {"intent": {"topic": "house"}}, verifier)
    assert len(verifier.calls[0][1]) <= 4
    assert any("pouring house foundation concrete" in prompt for prompt in verifier.calls[0][1])
    assert verifier.calls[0][1].scene[0] == scene()["narration"]


def test_visual_intent_text_adds_missing_global_subject_context():
    verifier = FakeVerifier({"known": 0.5})
    scene_data = {
        "narration": "The hole sits in the middle pane.",
        "visual_goal": "hole in the middle pane",
        "visual_intent": {
            "visual_goal": "hole sits middle pane",
            "objects": ["hole", "middle pane"],
            "actions": [],
            "context": [],
        },
    }
    verify_media_shortlist(
        [candidate("known", title="Airplane window close-up")],
        scene_data,
        {"intent": {"topic": "airplane window breather hole"}},
        verifier,
    )
    prompts = verifier.calls[0][1]
    assert prompts.subject == ["a photo of airplane window"]
    assert all("airplane window" not in prompt for prompt in prompts.scene)


def test_scene_score_dominates_global_subject_score():
    local = candidate("local", title="Workers pouring concrete foundation")
    generic = candidate("generic")
    verifier = FakeVerifier(
        {
            "local": VisualVerification(
                0.28,
                "verified",
                "fake",
                subject_score=0.05,
                scene_score=0.30,
            ),
            "generic": VisualVerification(
                0.30,
                "verified",
                "fake",
                subject_score=0.90,
                scene_score=0.10,
            ),
        }
    )

    rows = verify_media_shortlist(
        [local, generic], scene(), {"intent": {"topic": "house"}}, verifier
    )

    assert rows[0][0].provider_id == "local"
    assert rows[0][1]["confidence"] in {"high", "acceptable"}
    assert rows[1][1]["confidence"] == "rejected"


def test_high_ranked_generic_candidate_is_rejected_below_local_scene_threshold():
    generic = candidate("generic", title="Winter athlete catching breath", rank=500)
    relevant = candidate("relevant", title="Water vapor condensing into fine droplets", rank=10)
    verifier = FakeVerifier(
        {
            "generic": VisualVerification(
                0.70,
                "verified",
                "fake",
                subject_score=0.92,
                scene_score=0.10,
            ),
            "relevant": VisualVerification(
                0.32,
                "verified",
                "fake",
                subject_score=0.10,
                scene_score=0.34,
            ),
        }
    )
    mechanism_scene = {
        "narration": "Water vapor condenses into fine droplets.",
        "visual_goal": "condensation forming water droplets in mist",
    }

    rows = verify_media_shortlist(
        [generic, relevant], mechanism_scene, {"intent": {"topic": "visible breath in winter"}}, verifier
    )
    result = {candidate.provider_id: relevance for candidate, relevance in rows}

    assert result["generic"]["confidence"] == "rejected"
    assert result["relevant"]["confidence"] in {"high", "acceptable"}


def test_visual_flashcard_detection_rejects_otherwise_relevant_candidate():
    item = candidate("card", title="Workers pouring concrete foundation")
    result = VisualVerification(
        0.30,
        "verified",
        "fake",
        subject_score=0.30,
        scene_score=0.30,
        presentation_score=0.36,
        photographic_score=0.20,
        diagram_score=0.22,
        presentation_risk=True,
    )

    rows = verify_media_shortlist(
        [item], scene(), {"intent": {"topic": "house"}}, FakeVerifier({"card": result})
    )

    assert rows[0][1]["confidence"] == "rejected"
    assert rows[0][1]["presentation_risk"]["source"] == "vision"


def test_visual_diagram_signal_does_not_trigger_blanket_rejection():
    item = candidate("diagram", title="Scientific diagram of concrete foundation layers")
    result = VisualVerification(
        0.30,
        "verified",
        "fake",
        subject_score=0.25,
        scene_score=0.31,
        presentation_score=0.27,
        photographic_score=0.18,
        diagram_score=0.35,
        presentation_risk=False,
    )

    rows = verify_media_shortlist(
        [item], scene(), {"intent": {"topic": "house"}}, FakeVerifier({"diagram": result})
    )

    assert rows[0][1]["confidence"] in {"high", "acceptable"}
