from clipforge.config import Settings
from clipforge.models import Project, ProjectRevision
from clipforge.schemas import SocialMetadataGenerate, SocialMetadataUpdate
from clipforge.services import regenerate_project_social_metadata, update_project_social_metadata
from clipforge.social_metadata import (
    PlatformHashtags,
    SocialHashtagOutput,
    SocialMetadataError,
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
