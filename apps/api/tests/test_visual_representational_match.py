"""Representational match: a picture OF the subject beats a page ABOUT it.

Regression for the real "Warum ist der Mars rot?" run, generalised: a
newspaper/magazine/book page, catalogue card or stamp that names the subject
must not outrank (or, for physical/scientific scenes, even pass as) a visual
that depicts the object, place, event or mechanism. A document stays valid
when the scene itself is about a document. Deterministic and offline; topics
are fixtures only.
"""

from test_visual_sources_retrieval_v2 import photo, verified, wall_state

from clipforge import visual_judge
from clipforge.media import build_visual_query_plan
from clipforge.visual_judge import apply_vision_verdict, judge_and_rank, judge_candidate
from clipforge.visual_search_planner import DOCUMENT_TOLERANT_DOMAINS
from clipforge.visual_verifier import OpenClipVisualVerifier


def science_state():
    """A planet-colour explainer scene (Mars-like fixture; no topic rules exist)."""
    scene = {
        "id": "s2", "block_id": "b2", "start": 3, "end": 7,
        "narration": "Der Boden des Mars enthält viel Eisenoxid – im Grunde Rost.",
        "visual_intent": {
            "visual_goal": "red rusty soil on the Martian surface", "objects": ["red Martian soil"],
            "context": ["rocky red plain"], "media_queries": ["mars red soil surface", "iron oxide dust"],
            "entities": ["Mars"], "alternate_terms": ["red planet"], "factual_sensitivity": "scientific_specific",
        },
    }
    return {"timeline": {"width": 1080, "height": 1920}, "scenes": [scene], "assets": {},
            "intent": {"topic": "Warum ist der Mars rot?"}}


def place_state():
    scene = {
        "id": "p1", "narration": "The Matterhorn rises almost 4,500 metres above the valley.",
        "visual_intent": {"visual_goal": "the Matterhorn peak above the valley", "objects": ["Matterhorn peak"],
                          "entities": ["Matterhorn"], "factual_sensitivity": "real_place_or_object"},
    }
    return {"timeline": {"width": 1080, "height": 1920}, "scenes": [scene], "assets": {}}


def object_state():
    scene = {"id": "o1", "narration": "Each key of a typewriter swings a metal typebar against the ribbon.",
             "visual_intent": {"visual_goal": "typewriter typebars striking the ribbon", "objects": ["typewriter typebars"]}}
    return {"timeline": {"width": 1080, "height": 1920}, "scenes": [scene], "assets": {}}


def headline_state():
    scene = {
        "id": "h1", "narration": "Am nächsten Morgen meldete die Zeitung die Schließung der Grenze.",
        "visual_intent": {"visual_goal": "newspaper front page headline about the border closure",
                          "objects": ["newspaper front page"], "entities": ["Berlin Wall"], "time_period": "1961",
                          "factual_sensitivity": "historical_event"},
    }
    return {"timeline": {"width": 1080, "height": 1920}, "scenes": [scene], "assets": {},
            "intent": {"topic": "Warum wurde die Berliner Mauer gebaut?"}}


def ranked_ids(rows, state):
    return [candidate.provider_id for candidate, _ in judge_and_rank(rows, state["scenes"][0], state)]


def judged(rows, state):
    return {candidate.provider_id: relevance["judge"] for candidate, relevance in judge_and_rank(rows, state["scenes"][0], state)}


# ---------------------------------------------------------------------------
# Physical / scientific scene: real scientific imagery beats an article
# ---------------------------------------------------------------------------


def test_real_scientific_image_beats_newspaper_article_about_the_same_subject():
    state = science_state()
    nasa = photo("surface", "Mars surface panorama from the Curiosity rover, Gale Crater", provider="nasa",
                 width=1600, height=1200)
    article = photo("article", "Mars, the red planet - newspaper article page, 1910", width=1700, height=2200)
    cover = photo("cover", "Popular Astronomy magazine cover: Mars and its red iron oxide soil",
                  provider="openverse", width=1200, height=1600)
    rows = [
        (article, verified(confidence="high", scene_score=0.29, tier=3)),
        (cover, verified(confidence="high", scene_score=0.30, tier=3)),
        (nasa, verified(confidence="acceptable", scene_score=0.27)),
    ]
    verdicts = judged(rows, state)
    assert ranked_ids(rows, state)[0] == "surface"
    for meta in ("article", "cover"):
        assert verdicts[meta]["reject"] and "meta_visual_not_depiction" in verdicts[meta]["reasons"]
        assert verdicts[meta]["scores"]["representational_match"] < 0.5
        assert verdicts[meta]["representation"]["direct_expected"] is True
    assert verdicts["surface"]["scores"]["representational_match"] == 1.0
    assert not verdicts["surface"]["reject"]


def test_place_scene_prefers_the_actual_place_over_an_article_about_it():
    state = place_state()
    peak = photo("peak", "Matterhorn north face at sunrise above Zermatt valley", provider="pexels", width=1200, height=1800)
    article = photo("travel", "Travel magazine article about the Matterhorn", provider="openverse", width=1200, height=1700)
    rows = [(article, verified(confidence="high", tier=3, scene_score=0.30)), (peak, verified(scene_score=0.28))]
    verdicts = judged(rows, state)
    assert ranked_ids(rows, state)[0] == "peak"
    assert verdicts["travel"]["reject"]


def test_object_scene_prefers_the_object_over_a_magazine_page_mentioning_it():
    state = object_state()
    machine = photo("machine", "Close-up of typewriter typebars striking the ribbon", provider="pixabay")
    page = photo("ad", "1920s magazine advertisement page for typewriters", provider="wikimedia")
    rows = [(page, verified(confidence="high", tier=3, scene_score=0.31)), (machine, verified(scene_score=0.28))]
    assert ranked_ids(rows, state)[0] == "machine"
    assert judged(rows, state)["ad"]["reject"]


def test_vision_evidence_detects_a_text_page_without_metadata_markers():
    state = science_state()
    scan = photo("scan", "Mars red planet 1907")  # nothing in the caption says "page"
    relevance = verified(confidence="high", tier=3, scene_score=0.30)
    relevance["visual"].update(document_score=0.31, photographic_score=0.22, diagram_score=0.20)
    verdict = judge_candidate(scan, relevance, state["scenes"][0], state)
    assert verdict["representation"]["source"] == "vision"
    assert verdict["reject"] and "meta_visual_not_depiction" in verdict["reasons"]
    relevance["visual"].update(document_score=0.19)
    assert judge_candidate(scan, relevance, state["scenes"][0], state)["representation"]["meta_visual"] is False


def test_commons_categories_mark_a_scan_as_a_document():
    state = science_state()
    scan = photo("cat", "Mars 1910.jpg", origin={"categories": ["Newspapers of the United States", "Mars in culture"]})
    verdict = judge_candidate(scan, verified(confidence="high", tier=3), state["scenes"][0], state)
    assert verdict["representation"]["meta_visual"] and verdict["reject"]


def test_ordinary_photo_mentioning_a_paper_in_its_long_description_is_not_a_document():
    state = science_state()
    nasa = photo("pia", "Gale Crater layered rocks on Mars", provider="nasa",
                  description="Curiosity imaged these rocks; the findings were published in an article in the journal Science.")
    verdict = judge_candidate(nasa, verified(), state["scenes"][0], state)
    assert verdict["representation"]["meta_visual"] is False and not verdict["reject"]


# ---------------------------------------------------------------------------
# Documents remain valid when the scene is about a document
# ---------------------------------------------------------------------------


def test_historical_document_wins_when_the_document_is_the_scene_subject():
    state = headline_state()
    front_page = photo("front", "Newspaper front page, 13 August 1961: Berlin Wall border closed", provider="loc",
                       width=1700, height=2200)
    street = photo("street", "Berlin street 1961", provider="pexels", width=1080, height=1920)
    rows = [(street, verified(scene_score=0.26, temporal_evidence={"required": True, "matched_years": [1961]})),
            (front_page, verified(confidence="high", scene_score=0.31, temporal_evidence={"required": True, "matched_years": [1961]}))]
    verdicts = judged(rows, state)
    assert ranked_ids(rows, state)[0] == "front"
    assert verdicts["front"]["scores"]["representational_match"] == 1.0
    assert verdicts["front"]["representation"]["scene_wants_document"] is True
    assert not verdicts["front"]["reject"]


def test_archival_event_photo_beats_a_catalogue_card_without_rejecting_documents_globally():
    state = wall_state()
    event = photo("event", "Berlin Wall 1961, soldiers unrolling barbed wire", provider="loc")
    card = photo("card", "Catalog card: Berlin Wall 1961 photographs, index", provider="loc", width=1700, height=2200)
    years = {"required": True, "matched_years": [1961], "established": True}
    rows = [(card, verified(confidence="high", tier=3, scene_score=0.30, temporal_evidence=years)),
            (event, verified(scene_score=0.27, temporal_evidence=years))]
    verdicts = judged(rows, state)
    assert ranked_ids(rows, state) == ["event", "card"]
    # History tolerates records: penalised far below the event photo, not rejected.
    assert "historical" in DOCUMENT_TOLERANT_DOMAINS
    assert not verdicts["card"]["reject"]
    assert verdicts["card"]["final_score"] < verdicts["event"]["final_score"] - 0.1


def test_metadata_only_relevance_cannot_overpower_weak_representation():
    state = science_state()
    stamp = photo("stamp", "Postage stamp: Mars, the red planet with iron oxide soil")
    rock = photo("rock", "Reddish rocky ground on Mars", provider="nasa")
    stamp_rel = verified(confidence="high", tier=3, scene_score=0.34)
    verdict = judge_candidate(stamp, stamp_rel, state["scenes"][0], state)
    assert verdict["scores"]["semantic_match"] <= visual_judge.META_SEMANTIC_CAP
    rows = [(stamp, stamp_rel), (rock, verified(confidence="unknown", tier=1, scene_score=0.27))]
    assert ranked_ids(rows, state)[0] == "rock"


def test_authoritative_direct_source_is_preferred_when_equally_relevant():
    state = science_state()
    stock = photo("stock", "Red rocky Martian landscape", provider="pexels")
    agency = photo("agency", "Red rocky Martian landscape", provider="nasa")
    rows = [(stock, verified()), (agency, verified())]
    verdicts = judged(rows, state)
    assert ranked_ids(rows, state)[0] == "agency"
    assert verdicts["agency"]["scores"]["source_authority"] > verdicts["stock"]["scores"]["source_authority"]
    # Outside authority-ranked domains providers are neutral: ties keep order.
    everyday = object_state()
    tie = [(photo("a", "Typewriter typebars", provider="pexels"), verified()),
           (photo("b", "Typewriter typebars", provider="wikimedia"), verified())]
    assert ranked_ids(tie, everyday) == ["a", "b"]


# ---------------------------------------------------------------------------
# Optional VLM evidence on the bounded shortlist
# ---------------------------------------------------------------------------


def _vlm(**overrides):
    values = {"semantic_match": 9, "factual_match": 9, "representational_match": 9, "visual_impact": 7,
              "vertical_fit": 8, "document_page": False, "watermark_or_text": False, "wrong_subject": False,
              "reject": False, "rationale": "ok"}
    return {**values, **overrides}


def test_vlm_document_page_vetoes_a_physical_scene_but_not_a_document_scene():
    science = science_state()
    scan = photo("scan", "Mars 1907 red planet")
    base = judge_candidate(scan, verified(), science["scenes"][0], science)
    assert not base["reject"]
    vetoed = apply_vision_verdict(base, _vlm(document_page=True, representational_match=2, watermark_or_text=True))
    assert vetoed["reject"] and "vlm_meta_visual_not_depiction" in vetoed["reasons"]

    headline = headline_state()
    page = photo("page", "Newspaper front page 1961 Berlin Wall", provider="loc")
    base = judge_candidate(page, verified(temporal_evidence={"required": True, "matched_years": [1961]}), headline["scenes"][0], headline)
    kept = apply_vision_verdict(base, _vlm(document_page=True, representational_match=3, watermark_or_text=True))
    assert not kept["reject"] and kept["scores"]["representational_match"] == 1.0


def test_vlm_low_representational_score_lowers_a_direct_candidate():
    state = science_state()
    base = judge_candidate(photo("x", "Martian red soil"), verified(), state["scenes"][0], state)
    lowered = apply_vision_verdict(base, _vlm(representational_match=3))
    assert lowered["scores"]["representational_match"] == 0.3
    assert lowered["reject"]


# ---------------------------------------------------------------------------
# Query routing: physical scenes never send a bare name to catalogue engines
# ---------------------------------------------------------------------------


def test_physical_science_plan_never_sends_a_bare_name_to_catalogue_engines():
    state = science_state()
    scene = state["scenes"][0]
    plan = build_visual_query_plan(scene, state)["search_plan"]
    assert plan["domain"] == "space"
    bare = {"mars", "red planet"}
    for family in ("space", "commons", "archive"):
        queries = plan["source_queries"].get(family) or []
        assert queries and not bare & set(queries), (family, queries)
        assert all(len(query.split()) >= 2 for query in queries)
    assert plan["source_queries"]["space"][0] == "mars red martian soil"


def test_historical_catalogue_phrasing_is_unchanged():
    state = wall_state()
    plan = build_visual_query_plan(state["scenes"][0], state)["search_plan"]
    assert plan["source_queries"]["archive"][0] == "berlin wall 1961"
    assert "alternate_subject" not in {item["facet"] for item in plan["facets"]}


# ---------------------------------------------------------------------------
# Local CLIP evidence plumbing
# ---------------------------------------------------------------------------


def test_verifier_reports_a_document_score_from_the_page_prompts(tmp_path, monkeypatch):
    from PIL import Image

    path = tmp_path / "page.jpg"
    Image.new("RGB", (64, 64), "white").save(path)
    verifier = OpenClipVisualVerifier()

    def fake_score(_image, texts, *, asset_identity):
        joined = " ".join(texts)
        return 0.33 if "newspaper or magazine page" in joined else 0.2

    monkeypatch.setattr(verifier, "score_image", fake_score)
    result = verifier.verify_local_image(path, ["red soil"])
    assert result.document_score == 0.33
    assert result.photographic_score == 0.2


def test_an_incidental_prop_tag_does_not_make_a_photo_a_document():
    state = object_state()
    desk = photo("desk", "Typewriter typebars on a writer's desk", provider="pixabay", tags=("typewriter", "book", "newspaper"))
    verdict = judge_candidate(desk, verified(), state["scenes"][0], state)
    assert verdict["representation"]["meta_visual"] is False and not verdict["reject"]
