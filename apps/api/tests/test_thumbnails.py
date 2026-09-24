from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from clipforge.config import Settings
from clipforge.models import Project, ProjectRevision
from clipforge.services import (
    get_project,
    regenerate_project_thumbnails,
    select_project_thumbnail,
    serialize_project,
)
from clipforge.thumbnails import (
    _brief_terms,
    _candidate_score,
    _candidate_score_breakdown,
    _caption_risk,
    _compose,
    _cover_text,
    _cover_text_details,
    _fit_source_with_metadata,
    _fit_text,
    _frame_candidates,
    _quality_gate,
    _region_visual_density,
    _source_record,
    _thumbnail_visual_prompt_groups,
    _verify_thumbnail_shortlist,
    build_project_thumbnails,
    build_thumbnail_brief,
)
from clipforge.visual_verifier import UnavailableVisualVerifier, VisualVerification


@pytest.fixture(autouse=True)
def disable_real_thumbnail_inference(monkeypatch):
    monkeypatch.setattr("clipforge.thumbnails.get_visual_verifier", lambda: UnavailableVisualVerifier())


def _settings(tmp_path: Path) -> Settings:
    return Settings(render_root=tmp_path / "projects", clipforge_ai_mode="local", openai_api_key=None)


def _state(project_id: str) -> dict:
    return {
        "prompt": "Why do fireflies glow?",
        "intent": {"topic": "Why do fireflies glow?"},
        "render": {"status": "complete", "url": f"/media/{project_id}/renders/v1/clipforge.mp4"},
        "scenes": [
            {
                "id": "scene-1",
                "media": {
                    "provider": "wikimedia",
                    "provider_id": "photo-1",
                    "kind": "photo",
                    "cache_path": f"{project_id}/assets/wikimedia/photo-1.jpg",
                },
            }
        ],
    }


def test_thumbnail_generation_uses_project_media_and_builds_variants(tmp_path):
    settings = _settings(tmp_path)
    project_id = "project-one"
    image_path = settings.render_root / project_id / "assets" / "wikimedia" / "photo-1.jpg"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (800, 600), (30, 120, 80)).save(image_path)

    result = build_project_thumbnails(_state(project_id), project_id, settings)

    assert result["status"] == "available"
    assert result["selected_variant_id"] in {variant["id"] for variant in result["variants"]}
    assert len(result["variants"]) == 3
    assert all((settings.render_root / variant["url"].removeprefix("/media/")).is_file() for variant in result["variants"])
    assert all(variant["source_scene_id"] == "scene-1" for variant in result["variants"])
    assert "FIREFLIES" in result["variants"][0]["text"]


def test_thumbnail_generation_fuses_optional_visual_verification(tmp_path):
    settings = _settings(tmp_path)
    project_id = "visual-fusion"
    image_path = settings.render_root / project_id / "assets" / "wikimedia" / "photo-1.jpg"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (800, 600), (30, 120, 80)).save(image_path)

    class FakeVerifier:
        status = "available"
        model_identity = "fake-openclip"

        def verify_thumbnail_image(self, _path, **_kwargs):
            return VisualVerification(
                0.34, "verified", "fake", primary_visual_score=0.34,
                context_visual_score=0.16, visual_margin=0.18, confidence="high"
            )

    result = build_project_thumbnails(_state(project_id), project_id, settings, visual_verifier=FakeVerifier())
    assert result["variants"][0]["verifier_used"] is True
    assert result["variants"][0]["verifier_model"] == "fake-openclip"
    assert result["variants"][0]["visual_subject_match"] == 0.34
    assert result["variants"][0]["score_components"]["visual_subject"] > 0.5


def test_missing_project_media_is_non_fatal(tmp_path):
    result = build_project_thumbnails(_state("missing"), "missing", _settings(tmp_path))

    assert result["status"] == "unavailable"
    assert result["variants"] == []
    assert result["selected_variant_id"] is None


def test_thumbnail_selection_persists_as_a_revision(db, tmp_path):
    settings = _settings(tmp_path)
    project_id = "persisted-project"
    image_path = settings.render_root / project_id / "assets" / "wikimedia" / "photo-1.jpg"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (800, 600), (30, 120, 80)).save(image_path)
    project = Project(id=project_id, original_prompt="Why do fireflies glow?", title="Fireflies", status="rendered")
    project.revisions.append(ProjectRevision(number=1, instruction="Render video", kind="system", state=_state(project_id), changed_components=["render"]))
    db.add(project)
    db.commit()

    regenerate_project_thumbnails(db, project, settings, base_revision=1)
    db.refresh(project)
    selected = serialize_project(get_project(db, project_id))["revision"]["state"]["thumbnails"]["variants"][1]["id"]
    select_project_thumbnail(db, project, selected, base_revision=2)

    reopened = serialize_project(get_project(db, project_id))
    assert reopened["revision"]["state"]["thumbnails"]["selected_variant_id"] == selected
    assert reopened["revision"]["state"]["render"] == _state(project_id).get("render", {})

    regenerated = regenerate_project_thumbnails(db, get_project(db, project_id), settings, base_revision=3)
    assert regenerated.state["thumbnails"]["selected_variant_id"] == selected


def test_thumbnail_brief_is_format_aware_and_payoff_safe():
    state = _state("comparison")
    state["intent"] = {
        "topic": "Which country has more pyramids?",
        "question": "Which country has more pyramids, Egypt or Sudan?",
    }
    state["format_plan"] = {"selected_format": "comparison", "visual_structure": "A vs B"}
    state["payoff_plan"] = {"hook_must_not_reveal": "Sudan"}
    state["script"] = {"triple_hook": {"on_screen_text_hook": "EGYPT VS SUDAN\nWHO HAS MORE?"}}

    brief = build_thumbnail_brief(state)

    assert brief["composition_strategy"] == "SPLIT_COMPARISON"
    assert "SUDAN" not in brief["cover_text"].split("\n")[-1]
    assert "WHO HAS MORE" in _cover_text(state, brief)
    assert brief["format"] == "comparison"
    assert brief["secondary_subject"] == "Sudan"
    assert brief["source_scene_hints"] == ["scene-1"]
    assert brief["safe_zone"]["bottom"] > 0


def test_cover_text_rejects_context_dependent_fragment_and_prefers_standalone_question():
    state = _state("cover-text")
    state["intent"] = {
        "topic": "Why can airplanes not fly over every country?",
        "question": "Why can airplanes not fly over every country?",
    }
    state["format_plan"] = {"selected_format": "explanation"}
    state["script"] = {"triple_hook": {"on_screen_text_hook": "AN OKAY IS MISSING"}}
    text, details = _cover_text_details(state)
    assert "AIRPLANES" in text
    assert details["cover_text_source"] == "intent_format"
    assert any("context_dependent" in reason for reason in details["rejected_text_reasons"])


def test_explanation_brief_has_standalone_premise_and_visual_subjects():
    state = _state("aircraft")
    state["intent"] = {
        "topic": "Warum dürfen Flugzeuge nicht einfach über jedes Land fliegen",
        "question": "Warum dürfen Flugzeuge nicht einfach über jedes Land fliegen?",
    }
    state["format_plan"] = {"selected_format": "explanation", "visual_structure": "airspace and borders"}
    state["script"] = {"triple_hook": {"visual_hook": {"subjects_to_show": ["Passagierflugzeug", "Landesgrenze auf einer Karte"]}}}
    brief = build_thumbnail_brief(state)
    assert brief["cover_text"].startswith("Warum dürfen Flugzeuge")
    assert "airplane" in brief["primary_visual_subjects"]
    assert "border" in brief["primary_visual_subjects"]
    assert "sky" not in brief["primary_visual_subjects"]


def test_subject_coverage_distinguishes_airplane_from_generic_sky():
    brief = {
        "primary_subject": "airplane airspace border",
        "primary_visual_subjects": ["airplane", "airspace", "border"],
        "supporting_visual_context": ["sky", "landscape"],
        "secondary_subject": None,
        "comparison_subject": None,
        "format": "explanation",
        "visual_strategy": "airspace",
        "recommended_angle": "",
    }
    airplane = _source_record(
        scene_id="airplane", path=Path("airplane.jpg"), context="airliner over border", brief=brief,
        width=1200, height=800, provenance={"high": "commercial airplane airspace border", "medium": "", "low": ""},
    )
    sky = _source_record(
        scene_id="sky", path=Path("sky.jpg"), context="sky landscape", brief=brief,
        width=1800, height=1200, provenance={"high": "sky landscape", "medium": "", "low": ""},
    )
    assert airplane["subject_coverage"] == "FULL"
    assert sky["subject_coverage"] == "WEAK"
    assert airplane["relevance"] > sky["relevance"]


def test_thumbnail_visual_prompts_include_comparison_entities_and_context():
    brief = {
        "comparison_subject": "Schweden",
        "secondary_subject": "Indonesien",
        "primary_visual_subjects": ["sweden", "indonesia", "island", "archipelago"],
        "supporting_visual_context": ["sky", "landscape"],
    }
    prompts = _thumbnail_visual_prompt_groups(brief)
    assert any("sweden" in value and "islands" in value for value in prompts["primary"])
    assert any("indonesia" in value for value in prompts["secondary"])
    assert any("tropical road" in value for value in prompts["context"])


def test_thumbnail_visual_shortlist_is_bounded_and_downgrades_context_only(tmp_path):
    class FakeVerifier:
        status = "available"
        model_identity = "fake-openclip"

        def __init__(self):
            self.calls = []

        def verify_thumbnail_image(self, path, **_kwargs):
            self.calls.append(path)
            if path.name == "road.jpg":
                return VisualVerification(
                    0.20, "verified", "fake", primary_visual_score=0.17,
                    context_visual_score=0.31, visual_margin=-0.14, confidence="low"
                )
            return VisualVerification(
                0.34, "verified", "fake", primary_visual_score=0.34,
                context_visual_score=0.20, visual_margin=0.14, confidence="high"
            )

    sources = []
    for index in range(7):
        path = tmp_path / ("road.jpg" if index == 0 else f"island-{index}.jpg")
        Image.new("RGB", (800, 600), (40 + index, 100, 140)).save(path)
        sources.append({
            "path": path, "scene_id": str(index), "payoff_safe": True,
            "relevance": 0.9 if index == 0 else 0.3, "quality_score": 0.5, "pixels": 480_000,
        })
    verifier = FakeVerifier()
    result = _verify_thumbnail_shortlist(sources, {"primary_visual_subjects": ["island"], "supporting_visual_context": ["sky"]}, verifier=verifier)
    assert len(verifier.calls) == 5
    assert result[0]["visual_context_only"] is True
    assert result[0]["metadata_visual_disagreement"] is True
    assert result[0]["relevance"] < 0.9
    assert result[1]["visual_subject_match"] == 0.34
    assert result[1]["relevance"] > 0.3


def test_thumbnail_visual_verifier_failure_preserves_metadata_fallback(tmp_path):
    class BrokenVerifier:
        status = "available"
        model_identity = "broken"

        def verify_thumbnail_image(self, *_args, **_kwargs):
            raise RuntimeError("model unavailable")

    path = tmp_path / "asset.jpg"
    Image.new("RGB", (800, 600), "blue").save(path)
    source = {"path": path, "scene_id": "asset", "payoff_safe": True, "relevance": 0.7, "quality_score": 0.5, "pixels": 480_000}
    result = _verify_thumbnail_shortlist([source], {"primary_visual_subjects": ["island"], "supporting_visual_context": []}, verifier=BrokenVerifier())
    assert result[0]["relevance"] == 0.7
    assert result[0]["visual_verification"]["status"] == "verification_error"


def test_comparison_variants_use_distinct_compositions_and_two_assets(tmp_path):
    settings = _settings(tmp_path)
    project_id = "comparison-project"
    state = _state(project_id)
    state["intent"] = {
        "topic": "Which country has more pyramids?",
        "question": "Which country has more pyramids, Egypt or Sudan?",
    }
    state["format_plan"] = {"selected_format": "comparison"}
    state["scenes"][0]["visual_goal"] = "Sudan pyramids"
    state["scenes"].append(
        {
            "id": "scene-2",
            "visual_goal": "Egypt pyramids",
            "media": {
                "kind": "photo",
                "cache_path": f"{project_id}/assets/wikimedia/photo-2.jpg",
            },
        }
    )
    for name, color in (("photo-1.jpg", (30, 120, 80)), ("photo-2.jpg", (120, 60, 30))):
        image_path = settings.render_root / project_id / "assets" / "wikimedia" / name
        image_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1200, 900), color).save(image_path)

    result = build_project_thumbnails(state, project_id, settings)

    assert {variant["composition_strategy"] for variant in result["variants"]} == {
        "SPLIT_COMPARISON",
        "SUBJECT_FOCUS",
        "TYPOGRAPHY_FOCUS",
    }
    assert result["selected_variant_id"] == max(result["variants"], key=lambda item: item["score"])["id"]
    assert result["brief"]["safe_zone"]["x"] == 72


def test_thumbnail_text_fitting_handles_long_copy():
    image = Image.new("RGB", (1080, 1920))
    from PIL import ImageDraw

    draw = ImageDraw.Draw(image)
    font, lines, _ = _fit_text(draw, "A genuinely useful explanatory comparison for a curious viewer", 936, 430)

    assert len(lines) <= 3
    assert all(draw.textbbox((0, 0), line, font=font)[2] <= 936 for line in lines)


def test_thumbnail_generation_uses_font_fallback(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    project_id = "font-fallback"
    image_path = settings.render_root / project_id / "assets" / "wikimedia" / "photo-1.jpg"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (800, 600), (30, 120, 80)).save(image_path)
    monkeypatch.setattr("clipforge.thumbnails._FONT_CANDIDATES", ())

    result = build_project_thumbnails(_state(project_id), project_id, settings)

    assert result["status"] == "available"
    assert all(variant["width"] == 1080 for variant in result["variants"])


def test_thumbnail_generation_falls_back_when_second_comparison_asset_missing(tmp_path):
    settings = _settings(tmp_path)
    project_id = "one-sided-comparison"
    state = _state(project_id)
    state["intent"] = {"topic": "Egypt or Sudan", "question": "Egypt or Sudan?"}
    state["format_plan"] = {"selected_format": "comparison"}
    state["scenes"][0]["visual_goal"] = "Egypt pyramids"
    image_path = settings.render_root / project_id / "assets" / "wikimedia" / "photo-1.jpg"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (800, 600), (30, 120, 80)).save(image_path)

    result = build_project_thumbnails(state, project_id, settings)

    assert result["status"] == "available"
    assert all(variant["composition_strategy"] != "SPLIT_COMPARISON" for variant in result["variants"])


def test_thumbnail_layout_uses_prominent_type_and_safe_bounds(tmp_path):
    settings = _settings(tmp_path)
    project_id = "layout-prominence"
    image_path = settings.render_root / project_id / "assets" / "wikimedia" / "photo-1.jpg"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (1400, 1000), (30, 120, 80)).save(image_path)

    result = build_project_thumbnails(_state(project_id), project_id, settings)

    assert all(variant["font_size"] >= 120 for variant in result["variants"])
    assert all(variant["text_area_ratio"] >= 0.05 for variant in result["variants"])
    assert all(
        variant["text_bbox"][0] >= 72
        and variant["text_bbox"][2] <= 1008
        and variant["text_bbox"][1] >= 180
        and variant["text_bbox"][3] <= 1660
        for variant in result["variants"]
    )


def test_layout_scoring_penalizes_excessive_dead_space():
    brief = {"format": "explanation"}
    source = {"pixels": 1080 * 1920, "relevance": 2}
    compact_score, compact_reasons = _candidate_score(
        source,
        brief,
        "IMAGE_PLUS_HEADER",
        {"text_fits": True, "line_count": 2, "text_area_ratio": 0.12, "dead_space_ratio": 0.7},
        1,
    )
    sparse_score, sparse_reasons = _candidate_score(
        source,
        brief,
        "IMAGE_PLUS_HEADER",
        {"text_fits": True, "line_count": 2, "text_area_ratio": 0.02, "dead_space_ratio": 0.98},
        1,
    )

    assert compact_score > sparse_score
    assert "canvas_utilization" in compact_reasons
    assert "excessive_dead_space" in sparse_reasons


def test_semantic_asset_ranking_beats_unrelated_high_resolution_asset(tmp_path):
    settings = _settings(tmp_path)
    project_id = "semantic-ranking"
    state = _state(project_id)
    state["intent"] = {"topic": "Egypt and Sudan pyramids", "question": "Which country has more pyramids, Egypt or Sudan?"}
    state["format_plan"] = {"selected_format": "comparison"}
    state["scenes"] = [
        {"id": "pyramids", "visual_goal": "Egyptian pyramids and Sudanese pyramids", "media": {"kind": "photo", "cache_path": f"{project_id}/assets/pyramids.jpg"}},
        {"id": "portrait", "visual_goal": "person portrait", "media": {"kind": "photo", "cache_path": f"{project_id}/assets/portrait.jpg"}},
    ]
    asset_dir = settings.render_root / project_id / "assets"
    asset_dir.mkdir(parents=True)
    Image.new("RGB", (800, 600), (80, 120, 70)).save(asset_dir / "pyramids.jpg")
    Image.new("RGB", (2400, 1800), (80, 80, 80)).save(asset_dir / "portrait.jpg")

    result = build_project_thumbnails(state, project_id, settings)

    assert result["variants"][0]["source_scene_id"] == "pyramids"
    assert {"egypt", "sudan", "pyramid"}.issubset(set(result["variants"][0]["matched_terms"]))
    assert all(variant["source_scene_id"] == "pyramids" for variant in result["variants"])


def test_protected_payoff_scene_is_deprioritized(tmp_path):
    settings = _settings(tmp_path)
    project_id = "payoff-safe-assets"
    state = _state(project_id)
    state["intent"] = {"topic": "Egypt and Sudan pyramids", "question": "Which country has more pyramids, Egypt or Sudan?"}
    state["payoff_plan"] = {"hook_must_not_reveal": "Sudan"}
    state["scenes"] = [
        {"id": "setup", "visual_goal": "Egypt pyramids context", "media": {"kind": "photo", "cache_path": f"{project_id}/assets/setup.jpg"}},
        {"id": "answer", "narration": "Sudan has more pyramids and is the winner", "visual_goal": "Sudan answer reveal", "media": {"kind": "photo", "cache_path": f"{project_id}/assets/answer.jpg"}},
    ]
    asset_dir = settings.render_root / project_id / "assets"
    asset_dir.mkdir(parents=True)
    for name in ("setup.jpg", "answer.jpg"):
        Image.new("RGB", (900, 700), (80, 120, 70)).save(asset_dir / name)

    result = build_project_thumbnails(state, project_id, settings)

    assert result["variants"][0]["source_scene_id"] == "setup"
    assert all(variant["payoff_safe"] for variant in result["variants"])


def test_unrelated_second_asset_prevents_forced_split_comparison(tmp_path):
    settings = _settings(tmp_path)
    project_id = "unrelated-comparison"
    state = _state(project_id)
    state["intent"] = {"topic": "Egypt and Sudan pyramids", "question": "Which country has more pyramids, Egypt or Sudan?"}
    state["format_plan"] = {"selected_format": "comparison"}
    state["scenes"] = [
        {"id": "pyramids", "visual_goal": "Egypt pyramids", "media": {"kind": "photo", "cache_path": f"{project_id}/assets/pyramids.jpg"}},
        {"id": "portrait", "visual_goal": "person portrait", "media": {"kind": "photo", "cache_path": f"{project_id}/assets/portrait.jpg"}},
    ]
    asset_dir = settings.render_root / project_id / "assets"
    asset_dir.mkdir(parents=True)
    for name in ("pyramids.jpg", "portrait.jpg"):
        Image.new("RGB", (900, 700), (80, 120, 70)).save(asset_dir / name)

    result = build_project_thumbnails(state, project_id, settings)

    assert all(variant["composition_strategy"] != "SPLIT_COMPARISON" for variant in result["variants"])


def test_comparison_split_requires_visually_meaningful_pair(tmp_path):
    settings = _settings(tmp_path)
    project_id = "visual-comparison-pair"
    state = _state(project_id)
    state["intent"] = {"topic": "Egypt and Sudan pyramids", "question": "Which country has more pyramids, Egypt or Sudan?"}
    state["format_plan"] = {"selected_format": "comparison"}
    state["scenes"] = [
        {"id": "egypt", "visual_goal": "Egypt pyramids", "media": {"kind": "photo", "query": "Egypt pyramids", "cache_path": f"{project_id}/assets/egypt.jpg"}},
        {"id": "sudan", "visual_goal": "Sudan pyramids", "media": {"kind": "photo", "query": "Sudan pyramids", "cache_path": f"{project_id}/assets/sudan.jpg"}},
    ]
    asset_dir = settings.render_root / project_id / "assets"
    asset_dir.mkdir(parents=True)
    for name in ("egypt.jpg", "sudan.jpg"):
        Image.new("RGB", (900, 700), (80, 120, 70)).save(asset_dir / name)

    class PairVerifier:
        status = "available"
        model_identity = "fake-openclip"

        def verify_thumbnail_image(self, path, **_kwargs):
            egypt = path.name == "egypt.jpg"
            return VisualVerification(
                0.34, "verified", "fake", primary_visual_score=0.34 if egypt else 0.12,
                secondary_visual_score=0.12 if egypt else 0.34,
                context_visual_score=0.10, visual_margin=0.24, confidence="high",
            )

    result = build_project_thumbnails(state, project_id, settings, visual_verifier=PairVerifier())

    assert any(variant["composition_strategy"] == "SPLIT_COMPARISON" for variant in result["variants"])


def test_video_frame_candidates_keep_timestamp_and_reject_black_frames(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    project_id = "frame-candidates"
    project_dir = settings.render_root / project_id
    video = project_dir / "renders" / "v1" / "clipforge.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"fixture")
    state = _state(project_id)
    state["render"] = {"url": f"/media/{project_id}/renders/v1/clipforge.mp4"}
    state["scenes"] = [
        {"id": "black", "start": 0, "end": 1, "visual_goal": "black transition"},
        {"id": "useful", "start": 1, "end": 2, "visual_goal": "fireflies glowing"},
    ]
    brief = build_thumbnail_brief(state)

    def fake_extract(_ffmpeg, _video, timestamp, destination):
        color = (0, 0, 0) if destination.name.startswith("frame-1") else (80, 140, 90)
        Image.new("RGB", (640, 480), color).save(destination)
        return True

    monkeypatch.setattr("clipforge.thumbnails.ffmpeg_path", lambda: "fake-ffmpeg")
    monkeypatch.setattr("clipforge.thumbnails._extract_frame", fake_extract)

    candidates = _frame_candidates(state, project_dir, brief, tmp_path / "frames")

    assert len(candidates) == 1
    assert candidates[0]["source_type"] == "video_frame"
    assert candidates[0]["scene_id"] == "useful"
    assert candidates[0]["timestamp"] > 1


def test_comparison_brief_propagates_both_subjects_and_normalizes_plural():
    state = _state("terms")
    state["intent"] = {
        "topic": "Which country has more pyramids?",
        "question": "Which country has more pyramids, Egypt or Sudan?",
    }
    state["format_plan"] = {"selected_format": "comparison"}

    terms = _brief_terms(build_thumbnail_brief(state))

    assert {"egypt", "sudan", "pyramid"}.issubset(terms)
    assert "comparison" not in terms


def test_generic_format_match_has_negligible_relevance():
    brief = {
        "primary_subject": "Egypt Sudan pyramids",
        "secondary_subject": "Sudan",
        "comparison_subject": "Egypt",
        "format": "comparison",
    }
    candidate = _source_record(
        scene_id="generic",
        path=Path("/tmp/generic.jpg"),
        context="comparison scene video",
        provenance={"high": "comparison", "medium": "", "low": ""},
        brief=brief,
        width=1200,
        height=900,
    )

    assert candidate["relevance"] == 0
    assert candidate["provenance_trust"] == "low"


def test_original_asset_query_outweighs_misleading_scene_label(tmp_path):
    settings = _settings(tmp_path)
    project_id = "provenance-ranking"
    state = _state(project_id)
    state["intent"] = {"topic": "Egypt Sudan pyramids", "question": "Which country has more pyramids, Egypt or Sudan?"}
    state["format_plan"] = {"selected_format": "comparison"}
    state["scenes"] = [
        {"id": "trusted", "visual_goal": "generic scene", "media": {"kind": "photo", "query": "Egyptian pyramids Sudan Nubian pyramids", "title": "Pyramids", "provider": "wikimedia", "cache_path": f"{project_id}/assets/trusted.jpg"}},
        {"id": "misleading", "visual_goal": "Egypt Sudan pyramids", "media": {"kind": "photo", "cache_path": f"{project_id}/assets/misleading.jpg"}},
    ]
    asset_dir = settings.render_root / project_id / "assets"
    asset_dir.mkdir(parents=True)
    for name in ("trusted.jpg", "misleading.jpg"):
        Image.new("RGB", (900, 700), (80, 120, 70)).save(asset_dir / name)

    result = build_project_thumbnails(state, project_id, settings)

    assert result["variants"][0]["source_scene_id"] == "trusted"
    assert result["variants"][0]["provenance_trust"] == "high"
    assert all(variant["source_scene_id"] == "trusted" for variant in result["variants"])


def test_sparse_provenance_reduces_confidence_even_with_scene_overlap():
    brief = {"primary_subject": "Egypt Sudan pyramids", "secondary_subject": "Sudan", "comparison_subject": "Egypt"}
    candidate = _source_record(
        scene_id="label-only",
        path=Path("/tmp/label-only.jpg"),
        context="Egypt Sudan pyramids",
        provenance={"high": "", "medium": "Egypt Sudan pyramids", "low": "asset-1"},
        brief=brief,
        width=1200,
        height=900,
    )

    assert candidate["provenance_trust"] == "medium"
    assert candidate["relevance"] < 0.5


def test_informative_crop_beats_blank_region(tmp_path):
    image = Image.new("RGB", (1200, 800), (4, 4, 4))
    draw = ImageDraw.Draw(image)
    draw.rectangle((80, 80, 560, 720), fill=(230, 160, 55))
    image_path = tmp_path / "focal.jpg"
    image.save(image_path)

    _, metadata = _fit_source_with_metadata(
        image_path,
        (1080, 1920),
        centering=(0.15, 0.5),
        composition="SUBJECT_FOCUS",
        text_placement="bottom",
    )

    assert metadata["focal_score"] > 0.1
    assert metadata["blank_space_ratio"] < 0.9
    assert _region_visual_density(Image.new("RGB", (100, 100), (2, 2, 2))) < metadata["focal_score"]


def test_crop_strategy_is_composition_aware(tmp_path):
    image_path = tmp_path / "crop.jpg"
    Image.new("RGB", (1200, 800), (90, 120, 160)).save(image_path)

    _, subject = _fit_source_with_metadata(
        image_path, (1080, 1920), centering=(0.5, 0.5), composition="SUBJECT_FOCUS", text_placement="bottom"
    )
    _, typography = _fit_source_with_metadata(
        image_path, (1080, 1920), centering=(0.5, 0.5), composition="TYPOGRAPHY_FOCUS", text_placement="center"
    )

    assert subject["crop_strategy"].startswith("subject_focus:")
    assert typography["crop_strategy"].startswith("typography_focus:")


def test_split_composition_crops_each_side_and_reports_balance(tmp_path):
    first = tmp_path / "first.jpg"
    second = tmp_path / "second.jpg"
    Image.new("RGB", (1200, 800), (220, 100, 40)).save(first)
    Image.new("RGB", (700, 1200), (40, 100, 220)).save(second)
    sources = [
        {"path": first},
        {"path": second},
    ]

    metadata = _compose(sources, tmp_path / "split.jpg", {"cover_text": "A VS B\nWHICH ONE?"}, "SPLIT_COMPARISON", 0)

    assert set(metadata["crop_box"]) == {"left", "right"}
    assert metadata["crop_strategy"] == "split_independent"
    assert 0.0 <= metadata["balance_score"] <= 1.0


def test_busy_region_moves_text_to_safer_area(tmp_path):
    image = Image.new("RGB", (1080, 1920), (15, 15, 15))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 1080, 850), fill=(245, 245, 245))
    source = tmp_path / "busy-top.jpg"
    image.save(source)
    metadata = _compose(
        [{"path": source}],
        tmp_path / "busy-top-output.jpg",
        {"cover_text": "WHY DOES THIS HAPPEN?"},
        "IMAGE_PLUS_HEADER",
        0,
    )

    assert metadata["text_placement"] == "bottom"


def test_text_stroke_bounds_are_safe(tmp_path):
    image = Image.new("RGB", (1080, 1920), (90, 100, 120))
    source = tmp_path / "safe.jpg"
    image.save(source)
    metadata = _compose(
        [{"path": source}],
        tmp_path / "safe-output.jpg",
        {"cover_text": "A VERY LONG QUESTION THAT MUST WRAP INSIDE THE SAFE AREA"},
        "TYPOGRAPHY_FOCUS",
        0,
    )

    assert metadata["clipping_safe"] is True
    assert metadata["text_bbox"][0] >= 72
    assert metadata["text_bbox"][2] <= 1080 - 72


def test_quality_gate_rejects_weak_candidate_and_keeps_strong_candidate():
    source = {"payoff_safe": True}
    weak, reasons = _quality_gate(
        {"text_fits": False, "focal_score": 0.04, "blank_space_ratio": 0.96, "balance_score": 0.2},
        source,
    )
    strong, strong_reasons = _quality_gate(
        {"text_fits": True, "focal_score": 0.55, "blank_space_ratio": 0.45, "balance_score": 0.9},
        source,
    )

    assert weak is False
    assert "text_outside_safe_area" in reasons
    assert strong is True
    assert strong_reasons == []


def test_score_calibration_exposes_components_and_avoids_saturation():
    brief = {"format": "explanation"}
    metadata = {
        "text_fits": True,
        "focal_score": 0.42,
        "blank_space_ratio": 0.2,
        "balance_score": 0.7,
        "text_collision": 0.1,
        "line_count": 2,
        "text_area_ratio": 0.18,
        "dead_space_ratio": 0.7,
        "backing_area_ratio": 0.3,
        "text_bbox": [72, 200, 900, 600],
    }
    source = {
        "pixels": 1080 * 1920,
        "relevance": 1.0,
        "subject_matches": ["airplane"],
        "provenance_trust": "high",
        "quality_score": 0.5,
        "quality_reasons": ["usable_contrast"],
        "payoff_safe": True,
        "source_type": "scene_asset",
        "caption_risk": {"risk": 0.0},
    }
    score, _, components = _candidate_score_breakdown(source, brief, "IMAGE_PLUS_HEADER", metadata, 1)

    assert score < 1.0
    assert {"semantic", "provenance", "visual_quality", "crop", "typography", "composition", "safety", "penalties"}.issubset(components)


def test_caption_risk_penalizes_frame_relative_to_clean_asset():
    brief = {"format": "explanation"}
    metadata = {"text_fits": True, "focal_score": 0.5, "blank_space_ratio": 0.2, "balance_score": 0.7, "text_collision": 0.1, "line_count": 2, "text_area_ratio": 0.18, "dead_space_ratio": 0.7, "backing_area_ratio": 0.3, "text_bbox": [72, 200, 900, 600]}
    base = {"pixels": 1080 * 1920, "relevance": 1.0, "subject_matches": ["airplane"], "provenance_trust": "high", "quality_score": 0.5, "quality_reasons": [], "payoff_safe": True}
    clean_score, _, _ = _candidate_score_breakdown(base | {"source_type": "scene_asset", "caption_risk": {"risk": 0.0}}, brief, "IMAGE_PLUS_HEADER", metadata, 1)
    frame_score, reasons, components = _candidate_score_breakdown(base | {"source_type": "video_frame", "caption_risk": {"risk": 0.35, "active": True, "position": "center"}}, brief, "IMAGE_PLUS_HEADER", metadata, 1)

    assert frame_score < clean_score
    assert "caption_overlay_risk" in reasons
    assert components["source_cleanliness"] < 1.0


def test_caption_layout_metadata_identifies_active_and_clean_intervals():
    state = {"captions": {"enabled": True, "position": "center", "items": [{"start": 1.0, "end": 2.0, "text": "caption"}]}}

    active = _caption_risk(state, 1.5)
    clean = _caption_risk(state, 2.5)

    assert active["active"] is True
    assert active["risk"] > 0
    assert clean["active"] is False
    assert clean["risk"] == 0


def test_nearby_frame_search_prefers_clean_caption_interval(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    project_id = "caption-search"
    project_dir = settings.render_root / project_id
    video = project_dir / "renders" / "v1" / "clipforge.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"fixture")
    state = _state(project_id)
    state["render"] = {"url": f"/media/{project_id}/renders/v1/clipforge.mp4"}
    state["captions"] = {"enabled": True, "position": "center", "items": [{"start": 0.0, "end": 0.7, "text": "baked caption"}]}
    state["scenes"] = [{"id": "captioned", "start": 0, "end": 1, "visual_goal": "fireflies glowing"}]

    def fake_extract(_ffmpeg, _video, _timestamp, destination):
        Image.new("RGB", (640, 480), (80, 140, 90)).save(destination)
        return True

    monkeypatch.setattr("clipforge.thumbnails.ffmpeg_path", lambda: "fake-ffmpeg")
    monkeypatch.setattr("clipforge.thumbnails._extract_frame", fake_extract)

    candidates = _frame_candidates(state, project_dir, build_thumbnail_brief(state), tmp_path / "frames")

    assert len(candidates) == 1
    assert candidates[0]["caption_risk"]["active"] is False
    assert candidates[0]["timestamp"] > 0.7
