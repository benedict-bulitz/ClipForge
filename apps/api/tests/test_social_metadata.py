import copy

import pytest

from clipforge import services
from clipforge.config import Settings
from clipforge.models import Project, ProjectRevision
from clipforge.schemas import SocialMetadataGenerate, SocialMetadataUpdate
from clipforge.services import regenerate_project_social_metadata, update_project_social_metadata
from clipforge.social_metadata import (
    PlatformHashtags,
    SocialHashtagOutput,
    SocialMetadataError,
    count_emojis,
    emoji_clusters,
    generate_social_metadata,
    normalize_hashtags,
)


class StubProvider:
    def generate(self, _content):
        return SocialHashtagOutput(
            tiktok=PlatformHashtags(hashtags=["#Glühwürmchen", "#Naturwissen"]),
            instagram=PlatformHashtags(hashtags=["#Biolumineszenz", "#Naturwissen"]),
            youtube=PlatformHashtags(hashtags=["#Biolumineszenz", "#WissensShorts"]),
        )


class FailingProvider:
    def generate(self, _content):
        raise SocialMetadataError("provider unavailable")


def state():
    return {
        "version": 1,
        "intent": {"topic": "Warum leuchten Glühwürmchen?", "language": "de", "content_type": "explanation"},
        "script": {"text": "Glühwürmchen erzeugen Licht durch Biolumineszenz."},
        "render": {"status": "complete", "url": "/media/clip.mp4"},
    }


def project(db):
    item = Project(id="social-project", original_prompt="Warum leuchten Glühwürmchen?", title="Glühwürmchen", status="rendered", current_revision=1, active_tip_revision=1)
    item.revisions.append(ProjectRevision(number=1, parent_revision=None, instruction="Original", kind="initial", state=state(), changed_components=[]))
    db.add(item)
    db.commit()
    return item


def test_platform_lists_are_structured_independent_and_content_relevant():
    result = generate_social_metadata(state(), Settings(clipforge_ai_mode="local", openai_api_key=None), provider=StubProvider())

    platforms = result["platforms"]
    assert platforms["tiktok"]["hashtags"] == ["#Glühwürmchen", "#Naturwissen"]
    assert platforms["instagram"]["hashtags"] != platforms["tiktok"]["hashtags"]
    assert platforms["youtube"]["hashtags"] != platforms["instagram"]["hashtags"]


def test_normalization_deduplicates_rejects_malformed_and_generator_blocks_spam():
    assert normalize_hashtags(["Glow", "#glow", "#bad tag", "", "#fyp"], reject_spam=True) == ["#Glow"]


def test_manual_edits_persist_and_regeneration_is_explicit(db):
    item = project(db)
    update_project_social_metadata(db, item, SocialMetadataUpdate(base_revision=1, hashtags={"tiktok": ["#EigeneTags"], "instagram": ["#EigeneTags"], "youtube": ["#EigeneTags"]}, metadata={"tiktok": {"title": "Mein TikTok Titel", "description": "Meine Beschreibung"}}))
    db.refresh(item)
    assert item.current_revision == 2
    assert item.revisions[-1].state["social_metadata"]["platforms"]["tiktok"]["manual"] is True
    assert item.revisions[-1].state["social_metadata"]["platforms"]["tiktok"]["title"] == "Mein TikTok Titel"

    # Reopening reads the persisted revision; no provider call happens until this explicit action.
    regenerate_project_social_metadata(db, item, SocialMetadataGenerate(base_revision=2, platform="youtube"), Settings(clipforge_ai_mode="local", openai_api_key=None))
    db.refresh(item)
    metadata = item.revisions[-1].state["social_metadata"]["platforms"]
    assert metadata["tiktok"]["hashtags"] == ["#EigeneTags"]
    assert metadata["youtube"]["manual"] is False


def test_local_language_output_has_no_viral_spam():
    result = generate_social_metadata(state(), Settings(clipforge_ai_mode="local", openai_api_key=None))
    tags = [tag.casefold() for data in result["platforms"].values() for tag in data["hashtags"]]
    assert "#fyp" not in tags and "#viral" not in tags
    assert any("glühwürmchen" in tag for tag in tags)
    assert result["platforms"]["tiktok"]["title"]
    assert result["platforms"]["instagram"]["description"]
    assert result["platforms"]["tiktok"]["title"] != result["platforms"]["youtube"]["title"]


def test_local_metadata_uses_dynamic_platform_lists_and_persists_copy_fields():
    result = generate_social_metadata(state(), Settings(clipforge_ai_mode="local", openai_api_key=None))
    counts = [len(data["hashtags"]) for data in result["platforms"].values()]
    assert all(count >= 2 for count in counts)
    assert any(count != 5 for count in counts)


def test_generation_failure_is_isolated_as_unavailable_metadata():
    result = generate_social_metadata(state(), Settings(clipforge_ai_mode="local", openai_api_key=None), provider=FailingProvider())
    assert result["status"] == "unavailable"
    assert result["platforms"] == {}


# ---------------------------------------------------------------------------
# Emoji policy: a few relevant emojis, no clickbait chains
# ---------------------------------------------------------------------------

LOCAL = Settings(clipforge_ai_mode="local", openai_api_key=None)
ALARM_AND_HYPE = {"🚨", "‼", "❗", "⚠", "😱", "🤯", "💯", "🔥"}


def topic_state(topic: str, script: str, language: str = "de") -> dict:
    return {"version": 1, "intent": {"topic": topic, "language": language}, "script": {"text": script}}


def assert_policy(platforms: dict) -> None:
    for data in platforms.values():
        titles, descriptions = emoji_clusters(data["title"]), emoji_clusters(data["description"])
        assert 1 <= len(titles) <= 2 and 1 <= len(descriptions) <= 3
        for found in (titles, descriptions):
            assert len({item.replace("️", "") for item in found}) == len(found)  # no repeats
            assert not {item.replace("️", "") for item in found} & ALARM_AND_HYPE
        assert not any(count_emojis(tag) for tag in data["hashtags"])


@pytest.mark.parametrize(("topic", "script", "language", "expected"), [
    ("Warum leuchten Glühwürmchen?", "Glühwürmchen erzeugen Licht durch Biolumineszenz.", "de", "✨"),
    ("Why is Mars red?", "Mars is covered in iron-rich dust that rusts.", "en", "🪐"),
    ("Wie funktioniert ein Kredit?", "Eine Bank leiht dir Geld und verlangt dafür Zinsen.", "de", "💰"),
    ("Warum haben Haie keine Knochen?", "Das Skelett von Haien besteht aus Knorpel.", "de", "🦈"),
])
def test_local_titles_and_descriptions_carry_a_few_topic_emojis(topic, script, language, expected):
    platforms = generate_social_metadata(topic_state(topic, script, language), LOCAL)["platforms"]
    assert_policy(platforms)
    for data in platforms.values():
        assert expected in data["title"] and expected in data["description"]
    # YouTube stays a little more restrained.
    assert count_emojis(platforms["youtube"]["title"]) == 1


def test_topic_without_a_known_concept_gets_a_neutral_emoji_not_a_hardcoded_one():
    platforms = generate_social_metadata(topic_state("Was ist Photosynthese?", ""), LOCAL)["platforms"]
    assert_policy(platforms)
    assert set(emoji_clusters(platforms["tiktok"]["title"])) <= {"🤔", "💡"}


def test_description_emojis_are_placed_naturally():
    platforms = generate_social_metadata(topic_state("Warum ist das Meer salzig?", "Flüsse spülen Mineralien ins Meer. Das Wasser verdunstet, das Salz bleibt."), LOCAL)["platforms"]
    description = platforms["instagram"]["description"]
    # One after the first sentence, the rest at the end; never at the start.
    assert not emoji_clusters(description[:3])
    first = description.index(emoji_clusters(description)[0])
    assert description[:first].rstrip().endswith(".")
    assert emoji_clusters(description.rstrip()[-2:])


class SpamProvider:
    def generate(self, _content):
        return SocialHashtagOutput(
            tiktok=PlatformHashtags(title="🔥🔥🔥😱😱 YOU WON'T BELIEVE THIS 🚨🚨", description="Krass!!! 🦈🦈🦈🌊🐟🚨🤯", hashtags=["#Haie", "#Meer"]),
            instagram=PlatformHashtags(title="Schlafen Haie? 🦈🤔", description="Haie ruhen anders. 🦈 Viele müssen schwimmen, um zu atmen. 🌊", hashtags=["#Haie", "#Meeresbiologie"]),
            youtube=PlatformHashtags(title="Wie Haie ruhen", description="Kurz erklärt, wie Haie ruhen.", hashtags=["#Haie", "#Shorts"]),
        )


def test_spam_guard_limits_dedupes_and_replaces_clickbait():
    result = generate_social_metadata(topic_state("Warum schlafen Haie nie?", "Viele Haie müssen schwimmen, um zu atmen."), LOCAL, provider=SpamProvider())
    platforms = result["platforms"]
    assert_policy(platforms)
    tiktok = platforms["tiktok"]
    assert "BELIEVE" not in tiktok["title"] and "🔥" not in tiktok["title"]
    assert "Haie" in tiktok["title"]  # neutral, topic-based replacement
    assert tiktok["description"].startswith("Krass!") and "!!" not in tiktok["description"]
    assert emoji_clusters(tiktok["description"]) == ["🦈", "🌊", "🐟"]
    # Good provider copy keeps its own, naturally placed emojis untouched.
    assert platforms["instagram"]["title"] == "Schlafen Haie? 🦈🤔"
    assert platforms["instagram"]["description"] == "Haie ruhen anders. 🦈 Viele müssen schwimmen, um zu atmen. 🌊"
    # Copy without emojis gets topic emojis.
    assert "🦈" in platforms["youtube"]["title"] and "🦈" in platforms["youtube"]["description"]


def test_fire_emoji_is_kept_only_when_the_subject_is_fire():
    class FireProvider:
        def generate(self, _content):
            entry = PlatformHashtags(title="Wie Feuer entsteht 🔥", description="Feuer braucht Sauerstoff. 🔥", hashtags=["#Feuer", "#Chemie"])
            return SocialHashtagOutput(tiktok=entry, instagram=entry, youtube=entry)

    fire = generate_social_metadata(topic_state("Wie entsteht Feuer?", "Feuer braucht Brennstoff, Hitze und Sauerstoff."), LOCAL, provider=FireProvider())
    assert fire["platforms"]["tiktok"]["title"] == "Wie Feuer entsteht 🔥"
    hype = generate_social_metadata(topic_state("Warum schlafen Haie nie?", "Haie ruhen anders."), LOCAL, provider=FireProvider())
    assert "🔥" not in hype["platforms"]["tiktok"]["title"]


def test_regeneration_uses_the_emoji_rules_and_keeps_manual_edits(db):
    item = project(db)
    update_project_social_metadata(db, item, SocialMetadataUpdate(base_revision=1, hashtags={"tiktok": ["#EigeneTags"]}, metadata={"tiktok": {"title": "Mein Titel ohne Emoji", "description": "Selbst geschrieben"}}))
    db.refresh(item)
    regenerate_project_social_metadata(db, item, SocialMetadataGenerate(base_revision=2, platform="youtube"), LOCAL)
    db.refresh(item)
    platforms = item.revisions[-1].state["social_metadata"]["platforms"]
    assert platforms["tiktok"]["title"] == "Mein Titel ohne Emoji" and platforms["tiktok"]["manual"] is True
    assert 1 <= count_emojis(platforms["youtube"]["title"]) <= 2
    assert 1 <= count_emojis(platforms["youtube"]["description"]) <= 3

    regenerate_project_social_metadata(db, item, SocialMetadataGenerate(base_revision=3), LOCAL)
    db.refresh(item)
    assert_policy(item.revisions[-1].state["social_metadata"]["platforms"])


def test_render_keeps_saved_metadata_until_regeneration_is_requested(monkeypatch):
    saved = {"status": "available", "source": "generated", "platforms": {"tiktok": {"title": "Alt", "description": "Alt", "hashtags": ["#A", "#B"], "manual": True}}}
    rendered = topic_state("Warum leuchten Glühwürmchen?", "Licht.")
    rendered["social_metadata"] = saved
    calls: list[dict] = []
    monkeypatch.setattr(services, "_render_state", lambda state, *_args, **_kwargs: copy.deepcopy(rendered))
    monkeypatch.setattr(services, "generate_social_metadata", lambda state, _settings: calls.append(state) or {"status": "available", "platforms": {"tiktok": {}}})
    monkeypatch.setattr(services, "build_project_thumbnails", lambda *_args: {"status": "unavailable", "variants": []})
    captured: dict = {}
    monkeypatch.setattr(services, "_append_revision", lambda _db, _project, **kwargs: captured.update(kwargs))
    item = Project(id="p", original_prompt="x", title="x", status="rendered", current_revision=1, active_tip_revision=1)
    item.revisions.append(ProjectRevision(number=1, parent_revision=None, instruction="Original", kind="initial", state=rendered, changed_components=[]))
    monkeypatch.setattr(services, "_next_revision_number", lambda *_args: 2)

    services.render_project(None, item, LOCAL)
    assert calls == [] and captured["state"]["social_metadata"] == saved

    rendered.pop("social_metadata")
    services.render_project(None, item, LOCAL)
    assert len(calls) == 1  # a render without saved metadata still generates it
