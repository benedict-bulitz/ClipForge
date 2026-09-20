from clipforge.media import MediaCandidate, media_relevance, verify_media_shortlist
from clipforge.visual_verifier import UnavailableVisualVerifier, VisualVerification


def candidate(identifier: str, *, title: str = "", query: str = "house foundation") -> MediaCandidate:
    return MediaCandidate(identifier, "video", "https://cdn.test/media", "https://source.test", "Tester", None, 1080, 1920, 8.0, query, 100, title=title, preview_url="https://cdn.test/preview.jpg")


class FakeVerifier:
    status = "available"

    def __init__(self, scores):
        self.scores = scores
        self.calls = []

    def verify_candidate(self, item, texts):
        self.calls.append((item.provider_id, texts))
        return VisualVerification(self.scores[item.provider_id], "verified", "fake")


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
    assert len(verifier.calls[0][1]) == 3
    assert "pouring house foundation concrete" in verifier.calls[0][1][0]


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
    assert any("airplane window breather hole" in prompt for prompt in prompts)
