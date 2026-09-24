from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_editing_media import FakePexels, FakeWikimedia, candidate, local_settings, sample_state

from clipforge.media import derive_search_queries, is_real_media_allowed, prepare_project_media
from clipforge.renderer import RenderUnavailable, _create_visual_segment, _draw_scene


@pytest.mark.parametrize("marker", ["generated_card", "text_card", "flashcard", "diagram_or_card", "placeholder"])
def test_historical_cards_cannot_be_reused(marker):
    media = vars(candidate("1", title="Real lighthouse")).copy()
    media.update(kind="photo", source_type=marker)
    assert not is_real_media_allowed(media)
    assert not is_real_media_allowed(replace(candidate("1"), provider="generated"))


def test_neither_flashcard_nor_unrelated_real_media_wins(tmp_path):
    settings = local_settings(tmp_path, pexels="test")
    state = sample_state(settings)
    state["scenes"] = state["scenes"][:1]
    scene = state["scenes"][0]
    scene.update(narration="Water condenses into droplets", visual_goal="water droplets", visual_intent={"visual_strategy": "diagram_or_card"})
    card = candidate("1", title="Water droplets flashcard")
    real = candidate("2", title="Dog playing in a park")
    scene["media"] = {**vars(card), "identity": card.identity, "cache_path": "old.jpg"}
    (tmp_path / "old.jpg").write_bytes(b"old card")
    prepare_project_media(state, "project", settings, client=FakePexels([card, real]), fallback_client=FakeWikimedia(), visual_verifier=SimpleNamespace(status="unavailable"))
    # The unrelated dog no longer wins merely because it exists; the scene is
    # better explained, so the deterministic process graphic is used instead.
    assert scene["media"]["provider"] == "simple_graphic"
    assert scene["media"]["provider_id"] not in {"1", "2"}
    assert scene["visual_director"]["decision"] == "DEGRADED"
    assert scene["visual_director"]["resolved_type"] == "simple_graphic"
    assert (tmp_path / "old.jpg").exists()


def test_broad_queries_find_real_stock_after_empty_specific_searches(tmp_path):
    settings = local_settings(tmp_path, pexels="test")
    state = sample_state(settings)
    state["scenes"] = state["scenes"][:1]
    planned = derive_search_queries(state["scenes"][0], state)
    class Stock(FakeWikimedia):
        def search_photos(self, query, **kwargs):
            self.queries.append(query)
            return [candidate("3", kind="photo", provider="wikimedia", title="Lighthouse on the ocean coast")] if query not in planned else []
    stock = Stock()
    prepare_project_media(state, "project", settings, client=FakePexels([]), fallback_client=stock, visual_verifier=SimpleNamespace(status="unavailable"))
    search = state["scenes"][0]["media_search"]
    # Broad relaxed queries only spend logical budget left by the staged search.
    assert search["relaxed_queries"] and search["relaxed_queries"][0] in stock.queries
    assert len(set(stock.queries)) <= 3 and search["logical_queries_executed"] <= 3
    assert state["scenes"][0]["media"]["provider_id"] == "3"


def test_provider_failure_and_renderer_never_make_cards(tmp_path):
    settings = local_settings(tmp_path, pexels="test")
    state = sample_state(settings)
    state["scenes"] = state["scenes"][:1]
    prepare_project_media(state, "project", settings, client=FakePexels([]), fallback_client=FakeWikimedia())
    assert state["scenes"][0]["asset_status"] == "real_media_unavailable"
    assert state["scenes"][0]["fallback_media"] == "real_stock"
    with pytest.raises(RenderUnavailable, match="No real scene media"):
        _create_visual_segment("ffmpeg", state, state["scenes"][0], 0, 3, tmp_path, settings)
    with pytest.raises(RenderUnavailable, match="cards are disabled"):
        _draw_scene(state, state["scenes"][0], 0, tmp_path)
