"""Story Intelligence V2 x Visual Director V2 on the real pipeline.

``build_initial_state`` runs with fixture research and a fixture planner (no
network, no model); the real ``story_arc`` annotates scenes and binds the
protected visual target; ``prepare_project_media`` then runs the Visual
Director.  Providers, OpenCLIP and the image API are mocked; no paid calls.
"""
import copy
import inspect

import pytest
from test_staged_media_search import Commons, Provider, Verifier, cand, settings_for
from test_story_visual_integration import (
    ISLANDS,
    ISLANDS_Q,
    _strip_story,
    fact,
    generate,
    media_provider,
    visual,
)
from test_visual_director import POOR, WEAK_PASS, FakeGenerator

import clipforge.services  # noqa: F401 - registers ORM models for the db fixture
from clipforge import visual_director
from clipforge.media import _reuse_safe, build_visual_query_plan, prepare_project_media
from clipforge.story_arc import annotate_story_roles
from clipforge.thumbnails import _scene_payoff_safe

SWEDEN = ("swed", "schwed")


def islands(monkeypatch, tmp_path, planner_target="subject_b"):
    # The planner deliberately confuses the answer with the final payoff.
    return generate(monkeypatch, tmp_path, ISLANDS_Q, ISLANDS, planner_target=planner_target)


def run(state, tmp_path, provider, *, generator=None, verifier=None):
    prepare_project_media(
        state, "project", settings_for(tmp_path), client=provider, fallback_client=Commons(),
        visual_verifier=verifier or Verifier(), image_generator=generator, extra_clients=[],
    )
    return state


def mentions_sweden(value) -> bool:
    return any(term in str(value or "").casefold() for term in SWEDEN)


def media_text(media: dict) -> str:
    generation = media.get("generation") or {}
    graphic = media.get("graphic") or {}
    return " ".join(str(value) for value in (
        media.get("query"), media.get("title"), media.get("description"), generation.get("prompt"), *graphic.values(),
    ))


# ---------------------------------------------------------------------------
# Sweden vs Indonesia
# ---------------------------------------------------------------------------

def test_sweden_is_primary_answer_and_protected_target_is_subject_a_not_indonesia(monkeypatch, tmp_path):
    state = islands(monkeypatch, tmp_path)
    arc = state["story_arc"]
    units = {unit["id"]: unit for unit in arc["units"]}

    assert "Schweden" in units[arc["primary_answer_id"]]["claim"]
    assert "Indonesien" in units[arc["final_payoff_id"]]["claim"]
    assert "größte Inselstaat" in units[arc["final_payoff_id"]]["claim"]
    assert arc["primary_answer_id"] != arc["final_payoff_id"]
    # Regression: the protected visual target is Sweden (subject_a), never
    # Indonesia (subject_b), even though the planner proposed subject_b.
    assert state["payoff_plan"]["protected_visual_target"] == "subject_a"
    assert state["payoff_plan"]["protected_visual_target"] != "subject_b"
    assert arc["visual_protection"]["source"] == "story_arc"
    answer_scene = next(scene for scene in state["scenes"] if scene.get("is_primary_answer"))
    assert set(answer_scene["visual_intent"]["media_query_targets"]) == {"subject_a"}
    assert all(mentions_sweden(query) for query in answer_scene["visual_intent"]["media_queries"])

    run(state, tmp_path, media_provider(), generator=FakeGenerator())

    directors = [scene["visual_director"] for scene in state["scenes"]]
    assert {item["story_source"] for item in directors} == {"story_arc"}  # arc fields used directly
    answer = [item for item in directors if item["is_primary_answer"]]
    final = [item for item in directors if item["is_final_payoff"]]
    assert answer and final and not set(map(id, answer)) & set(map(id, final))
    assert {item["story_role"] for item in answer} == {"primary_answer"}
    # story_role is the arc's own value; the payoff identity is a separate flag.
    assert final[0]["story_role"] == "secondary_insight" and final[0]["visual_role"] == "final_payoff"
    assert all(item["story_stage"] == "reveal" for item in answer)


def test_sweden_hidden_before_reveal_indonesia_evidence_allowed(monkeypatch, tmp_path):
    state = islands(monkeypatch, tmp_path)
    generator = FakeGenerator()

    run(state, tmp_path, media_provider(), generator=generator)

    before = [scene for scene in state["scenes"] if scene["story_stage"] == "before_reveal"]
    assert before
    for scene in before:
        assert not mentions_sweden(" ".join(scene["media_search"]["executed_queries"]))
        assert not mentions_sweden(media_text(scene.get("media") or {}))
        assert scene["media"]["reveal_safe"] is True
    # Indonesia evidence may appear before the reveal.
    evidence = before[0]
    assert evidence["media"]["provider_id"] == "id"
    # Sweden appears at the reveal and is recorded as unsafe for earlier scenes.
    answer = next(scene for scene in state["scenes"] if scene.get("is_primary_answer"))
    assert answer["media"]["provider_id"] == "se" and answer["media"]["reveal_safe"] is False
    # After the reveal, Indonesia/context visuals stay structurally safe.
    final = next(scene for scene in state["scenes"] if scene.get("is_final_payoff"))
    assert final["media"]["provider_id"] == "nation" and final["media"]["reveal_safe"] is True


@pytest.mark.parametrize("kind", ["video", "photo"])
def test_stock_video_and_photo_before_reveal_never_show_sweden(monkeypatch, tmp_path, kind):
    state = islands(monkeypatch, tmp_path)
    # A provider that returns Swedish footage for every query.
    swedish = [cand("se-any", "any", "Swedish archipelago islands from above", kind=kind)]
    provider = Provider(videos={} if kind == "photo" else None, photos=None)
    provider.search_videos = lambda query, **_k: [] if kind == "photo" else [cand("se-any", query, swedish[0].title)]
    provider.search_photos = lambda query, **_k: [cand("se-any", query, swedish[0].title, kind="photo")] if kind == "photo" else []

    run(state, tmp_path, provider)

    for scene in state["scenes"]:
        if scene["story_stage"] == "before_reveal":
            assert (scene.get("media") or {}).get("provider_id") != "se-any"
            assert not mentions_sweden(media_text(scene.get("media") or {}))


def test_generated_graphic_comparison_and_number_visuals_respect_the_reveal(monkeypatch, tmp_path):
    state = islands(monkeypatch, tmp_path)
    generator = FakeGenerator()

    run(state, tmp_path, Provider(), generator=generator)  # no real media at all

    records = {item["scene_id"]: item for item in state["visual_director"]["generations"]}
    for scene in state["scenes"]:
        director = scene["visual_director"]
        prompt = records.get(scene["id"], {}).get("prompt", "")
        if scene["story_stage"] == "before_reveal":
            assert not mentions_sweden(prompt)
            assert not mentions_sweden(director.get("graphic"))  # number/comparison/process text
            assert director["planned_type"] != visual_director.COMPARISON_VISUAL
            assert not mentions_sweden(media_text(scene.get("media") or {}))
    answers = [scene for scene in state["scenes"] if scene.get("is_primary_answer")]
    # At the reveal Sweden may be shown (generated image or its number graphic),
    # and every answer visual is recorded as unsafe for earlier scenes.
    assert any(mentions_sweden(records.get(scene["id"], {}).get("prompt")) for scene in answers)
    assert all(scene["media"]["reveal_safe"] is False for scene in answers if scene.get("media"))

    # A comparison visual naming Sweden is only planned once the reveal is reached.
    comparison_state = copy.deepcopy(state)
    # The first scene before the reveal outside the hook window (the opening's
    # text channel belongs to the triple hook).
    hook_block = next(block["id"] for block in comparison_state["script"]["blocks"] if block["role"] == "hook")
    hook = next(item for item in comparison_state["scenes"] if item["block_id"] != hook_block)
    hook["story_stage"] = "before_reveal"
    hook["narration"] = "Wer gewinnt?"
    hook["visual_intent"] = {"visual_goal": "islands", "media_queries": ["swedish islands", "indonesian islands"], "media_query_targets": ["subject_a", "subject_b"]}
    hook.pop("search_queries", None)
    plan = build_visual_query_plan(hook, comparison_state)
    assert visual_director.plan_scene_strategy(hook, comparison_state, plan)["planned_type"] != visual_director.COMPARISON_VISUAL
    revealed = {**hook, "story_stage": "after_reveal"}
    assert visual_director.plan_scene_strategy(revealed, comparison_state, build_visual_query_plan(revealed, comparison_state))["planned_type"] == visual_director.COMPARISON_VISUAL


def test_reused_visuals_and_thumbnails_obey_the_reveal(monkeypatch, tmp_path):
    state = islands(monkeypatch, tmp_path)
    provider = media_provider()
    provider.videos.pop("indonesian islands aerial")  # the evidence scene finds nothing

    run(state, tmp_path, provider, generator=None)

    evidence = next(scene for scene in state["scenes"] if scene["story_stage"] == "before_reveal")
    answer = next(scene for scene in state["scenes"] if scene.get("is_primary_answer"))
    reused = evidence.get("media") or {}
    # Reuse never brings the Sweden answer visual forward.
    assert reused.get("provider_id") != "se" and not mentions_sweden(media_text(reused))
    assert not _reuse_safe(answer["media"], evidence["visual_director"])
    brief = {"protected_information": state["payoff_plan"]["hook_must_not_reveal"]}
    assert _scene_payoff_safe(answer, brief) is False
    final = next(scene for scene in state["scenes"] if scene.get("is_final_payoff"))
    assert _scene_payoff_safe(final, brief) is True


# ---------------------------------------------------------------------------
# Wet fingers
# ---------------------------------------------------------------------------

FINGERS_Q = "Warum werden Finger im Wasser schrumpelig?"
FINGERS = [
    (fact("Nach langem Baden werden die Fingerkuppen schrumpelig.", 0.95), "answer",
     visual("wrinkled wet fingertips after a bath", ["wrinkled wet fingertips", "pruney fingers bath"], ["shared", "shared"])),
    (fact("Nerven lassen die Blutgefäße in den Fingern enger werden; dadurch legt sich die Haut in Falten."), "explanation",
     visual("wrinkled fingertip skin close-up", ["wrinkled fingertip skin", "wet fingertips close-up"], ["shared", "shared"])),
    (fact("Die Furchen helfen möglicherweise dabei, nasse Dinge besser festzuhalten.", 0.7), "payoff",
     visual("wrinkled fingers gripping a wet stone", ["wrinkled fingers gripping wet stone"], ["shared"])),
]
BOOK = "Old book page with printed text"
BUS = "Bus stop on a city street"
CITY = "Unrelated city skyline at night"
HAND = "Close-up of wrinkled wet fingertips after a bath"


def junk_provider(*, with_hand: bool) -> Provider:
    class Junk(Provider):
        def search_photos(self, query, *, portrait):
            self.calls.append(("photo", query))
            rows = [cand("book", query, BOOK, kind="photo"), cand("bus", query, BUS, kind="photo"), cand("city", query, CITY, kind="photo")]
            if with_hand and query == "wrinkled wet fingertips":
                rows.append(cand("hand", query, HAND, kind="photo"))
            return rows

        def search_videos(self, query, *, portrait, scene_duration):
            self.calls.append(("video", query))
            return []

    return Junk()


@pytest.mark.parametrize("with_hand", [True, False])
def test_wet_fingers_rejects_junk_and_falls_back_to_generation(monkeypatch, tmp_path, with_hand):
    state = generate(monkeypatch, tmp_path, FINGERS_Q, FINGERS, planner_target="")
    generator = FakeGenerator()
    verifier = Verifier({"book": POOR, "bus": WEAK_PASS, "city": POOR})

    run(state, tmp_path, junk_provider(with_hand=with_hand), generator=generator, verifier=verifier)

    roles = {scene["visual_director"]["story_role"] for scene in state["scenes"]}
    assert roles == {scene["story_role"] for scene in state["scenes"]}  # arc roles, directly
    assert "explanation" in roles and "primary_answer" in roles
    assert {scene["visual_director"]["story_source"] for scene in state["scenes"]} == {"story_arc"}
    for scene in state["scenes"]:
        assert (scene.get("media") or {}).get("provider_id") not in {"book", "bus", "city"}
    answer = next(scene for scene in state["scenes"] if scene.get("is_primary_answer"))
    if with_hand:
        assert answer["media"]["provider_id"] == "hand"
        assert answer["visual_director"]["decision"] == visual_director.ACCEPTED_REAL
    generated = [scene for scene in state["scenes"] if (scene.get("media") or {}).get("source") == "generated_openai"]
    assert generated, "generated-image fallback becomes eligible"
    for scene in generated:
        prompt = scene["media"]["generation"]["prompt"].casefold()
        assert "wrinkled" in prompt and ("fingertip" in prompt or "finger" in prompt)
        assert "schrumpelig" not in prompt and "blutgefäße" not in prompt  # no raw narration
        assert scene["media"]["generation"]["model"] == "gpt-image-2"
        assert scene["media"]["generation"]["quality"] == "low"
    explanation = [scene for scene in state["scenes"] if scene["visual_director"]["story_role"] == "explanation"]
    # Explanation scenes may use a simple graphic when neither real media nor
    # a generated image is available.
    assert all(visual_director.SIMPLE_GRAPHIC in scene["visual_director"]["fallback_chain"] for scene in explanation)
    assert len(generator.prompts) <= 3
    source = inspect.getsource(visual_director).casefold()
    assert not any(word in source for word in ("finger", "wrinkl", "sweden", "schweden", "indones"))


def test_explanation_scene_uses_graphic_when_generation_budget_is_spent(monkeypatch, tmp_path):
    state = generate(monkeypatch, tmp_path, FINGERS_Q, FINGERS, planner_target="")
    state["visual_director"] = {"policy": {"max_auto_generated_images_per_project": 0}, "generations": []}

    verifier = Verifier({"book": POOR, "bus": WEAK_PASS, "city": POOR})
    run(state, tmp_path, junk_provider(with_hand=False), generator=FakeGenerator(), verifier=verifier)

    explanation = [scene for scene in state["scenes"] if scene["visual_director"]["story_role"] == "explanation"]
    assert explanation and all(scene["media"]["source"] == "simple_graphic" for scene in explanation)
    assert all(scene["visual_director"]["generation"]["status"] == "project_budget_exhausted" for scene in explanation)


# ---------------------------------------------------------------------------
# Compatibility and Change Media
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("combination", ["no_story_arc", "arc_no_director", "director_no_arc", "combined"])
def test_real_story_and_director_state_combinations_load(monkeypatch, tmp_path, combination):
    state = islands(monkeypatch, tmp_path, planner_target="subject_a")
    if combination in {"no_story_arc", "director_no_arc"}:
        _strip_story(state)
    if combination in {"director_no_arc", "combined"}:
        run(state, tmp_path, media_provider(), generator=FakeGenerator())
        for scene in state["scenes"]:
            scene["asset_status"] = "replacement_required"  # force a fresh decision
    if combination == "combined":
        annotate_story_roles(state)

    run(state, tmp_path, media_provider(), generator=FakeGenerator())

    expected = "story_arc" if combination in {"arc_no_director", "combined"} else "legacy_inference"
    assert {scene["visual_director"]["story_source"] for scene in state["scenes"]} == {expected}
    assert state["visual_director"]["policy"]["generated_image_model"] == "gpt-image-2"
    for scene in state["scenes"]:
        assert scene["media_search"]["logical_queries_executed"] <= 3
        if combination in {"no_story_arc", "director_no_arc"}:
            # Old projects keep project-wide protection (no Sweden anywhere).
            assert not mentions_sweden(" ".join(scene["media_search"]["executed_queries"]))


def test_manual_change_media_generation_uses_real_story_arc(db, monkeypatch, tmp_path):
    from test_export import seed_project

    from clipforge.media_candidates import generate_scene_media, scene_generation_option
    from clipforge.services import get_project, serialize_project

    state = islands(monkeypatch, tmp_path)
    state["render"] = {"status": "complete", "url": None}
    settings = settings_for(tmp_path).model_copy(update={"openai_api_key": "sk-test"})
    project = seed_project(db, "55555555-5555-4555-8555-555555555555", state)
    number = next(index for index, scene in enumerate(state["scenes"], 1) if scene["story_stage"] == "before_reveal")
    generator = FakeGenerator()

    option = scene_generation_option(state, number, settings)
    generate_scene_media(db, project, number, settings, generator=generator, visual_verifier=Verifier())

    assert option["model_label"] == "GPT Image 2" and option["quality_label"] == "Low"
    assert not mentions_sweden(option["prompt"]) and not mentions_sweden(generator.prompts[0])
    db.expire_all()
    scene = serialize_project(get_project(db, project.id))["revision"]["state"]["scenes"][number - 1]
    generation = scene["media_alternatives"][0]["generation"]
    assert generation["story_source"] == "story_arc" and generation["story_stage"] == "before_reveal"
    assert generation["trigger"] == "manual" and generation["ai_generated"] is True and generation["reveal_safe"] is True


def test_change_media_alternatives_obey_the_reveal(monkeypatch, tmp_path):
    from clipforge.media_candidates import clear_candidate_sets, discover_scene_media_candidates

    clear_candidate_sets()
    state = islands(monkeypatch, tmp_path)
    number = next(index for index, scene in enumerate(state["scenes"], 1) if scene["story_stage"] == "before_reveal")
    answer_number = next(index for index, scene in enumerate(state["scenes"], 1) if scene.get("is_primary_answer"))

    class Mixed(Provider):
        def search_videos(self, query, *, portrait, scene_duration):
            return [cand("se-alt", query, "Swedish archipelago islands from above"), cand("id-alt", query, "Indonesian islands aerial drone view")]

    _, before = discover_scene_media_candidates(state, "project", number, 1, settings_for(tmp_path), client=Mixed(), fallback_client=Commons(), visual_verifier=Verifier(), extra_clients=[])
    _, at_reveal = discover_scene_media_candidates(state, "project", answer_number, 1, settings_for(tmp_path), client=Mixed(), fallback_client=Commons(), visual_verifier=Verifier(), extra_clients=[])

    assert [item["provider_id"] for item in before] == ["id-alt"]
    assert "se-alt" in {item["provider_id"] for item in at_reveal}
