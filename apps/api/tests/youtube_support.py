"""Fakes for the YouTube Learning Loop tests: nothing here reaches Google."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clipforge.config import Settings
from clipforge.exporter import safe_export_filename
from clipforge.models import Project, ProjectRevision
from clipforge.youtube.provider import (
    REQUESTED_SCOPES,
    ChannelIdentity,
    OAuthClient,
    TokenGrant,
    UploadProgress,
    YouTubeApiError,
)

CLIENT_ID = "test-client-1234.apps.googleusercontent.com"
ACCESS_TOKEN = "ya29.fake-access-token-never-logged"
REFRESH_TOKEN = "1//fake-refresh-token-never-in-db"


def youtube_settings(tmp_path: Path, **overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        clipforge_ai_mode="local",
        openai_api_key=None,
        render_root=tmp_path / "projects",
        downloads_root=tmp_path / "Downloads",
        youtube_oauth_client_id=CLIENT_ID,
        youtube_oauth_client_secret="fake-client-secret",
        **overrides,
    )


@dataclass
class FakeYouTube:
    """In-memory Google: OAuth, channel, resumable uploads, videos, analytics."""

    channel: ChannelIdentity = field(default_factory=lambda: ChannelIdentity("UC_fake_channel_01", "Knowledge Lab"))
    scopes: tuple[str, ...] = REQUESTED_SCOPES
    refresh_error: YouTubeApiError | None = None
    start_error: YouTubeApiError | None = None
    chunk_errors: dict[int, YouTubeApiError] = field(default_factory=dict)  # chunk number -> error (once)
    expire_sessions: bool = False
    update_error: YouTubeApiError | None = None
    thumbnail_error: YouTubeApiError | None = None
    list_error: YouTubeApiError | None = None
    playlist_error: YouTubeApiError | None = None
    playlist_page_size: int = 50
    thumbnails_set: list[tuple[str, int, str]] = field(default_factory=list)
    categories: list[dict[str, Any]] = field(default_factory=lambda: [
        {"id": "27", "snippet": {"title": "Education", "assignable": True}},
        {"id": "28", "snippet": {"title": "Science & Technology", "assignable": True}},
        {"id": "18", "snippet": {"title": "Short Movies", "assignable": False}},
    ])
    analytics_handler: Any = None
    sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    videos: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: list[tuple[str, Any]] = field(default_factory=list)
    revoked: list[str] = field(default_factory=list)
    _ids: Any = field(default_factory=lambda: itertools.count(1))
    _chunks: int = 0

    # OAuth -------------------------------------------------------------
    def exchange_code(self, client: OAuthClient, code: str, code_verifier: str) -> TokenGrant:
        self.calls.append(("exchange_code", {"code": code, "verifier_len": len(code_verifier), "redirect_uri": client.redirect_uri}))
        if code == "bad-code":
            raise YouTubeApiError("auth_expired", "YouTube access expired or was revoked. Reconnect YouTube.")
        return TokenGrant(ACCESS_TOKEN, 3600, REFRESH_TOKEN, self.scopes)

    def refresh_access_token(self, client: OAuthClient, refresh_token: str) -> TokenGrant:
        self.calls.append(("refresh", None))
        if self.refresh_error:
            raise self.refresh_error
        return TokenGrant(ACCESS_TOKEN, 3600, None, ())

    def revoke(self, token: str) -> None:
        self.revoked.append(token)

    def get_my_channel(self, access_token: str) -> ChannelIdentity:
        return self.channel

    # Channel schedule (uploads playlist, newest upload first) ------------
    def get_uploads_playlist_id(self, access_token: str) -> str:
        self.calls.append(("uploads_playlist", None))
        if self.playlist_error:
            raise self.playlist_error
        return "UU" + self.channel.channel_id[2:]

    def list_playlist_items(self, access_token: str, playlist_id: str, page_token: str | None = None) -> tuple[list[dict[str, Any]], str | None]:
        self.calls.append(("playlist_items", page_token))
        if self.playlist_error:
            raise self.playlist_error
        assert playlist_id == "UU" + self.channel.channel_id[2:]
        ordered = list(reversed(list(self.videos.values())))
        start = int(page_token or 0)
        page = ordered[start:start + self.playlist_page_size]
        items = [{"contentDetails": {"videoId": video["id"], "videoPublishedAt": video["snippet"].get("uploadedAt") or video["snippet"].get("publishedAt")}} for video in page]
        following = start + self.playlist_page_size
        return items, (str(following) if following < len(ordered) else None)

    def add_studio_video(self, video_id: str, *, privacy: str = "private", publish_at: str | None = None, published_at: str = "2026-09-01T10:00:00Z", upload_status: str = "processed", title: str = "Made in YouTube Studio") -> dict[str, Any]:
        """A video scheduled or published outside ClipForge (YouTube Studio, another tool)."""
        status: dict[str, Any] = {"privacyStatus": privacy, "uploadStatus": upload_status}
        if publish_at:
            status["publishAt"] = publish_at
        self.videos[video_id] = {"id": video_id, "snippet": {"title": title, "publishedAt": published_at}, "status": status}
        return self.videos[video_id]

    # Upload ------------------------------------------------------------
    def start_resumable_upload(self, access_token: str, body: dict[str, Any], size: int, content_type: str, *, notify_subscribers: bool = True) -> str:
        self.calls.append(("start_upload", body))
        self.calls.append(("notify_subscribers", notify_subscribers))
        if self.start_error:
            raise self.start_error
        uri = f"https://www.googleapis.com/upload/youtube/v3/videos?upload_id=session{len(self.sessions) + 1}"
        self.sessions[uri] = {"size": size, "data": bytearray(), "body": body}
        return uri

    def _session(self, uri: str) -> dict[str, Any]:
        if self.expire_sessions or uri not in self.sessions:
            raise YouTubeApiError("upload_session_expired", "The YouTube upload session expired.", status_code=404)
        return self.sessions[uri]

    def _complete(self, session: dict[str, Any]) -> UploadProgress:
        if "video_id" not in session:
            video_id = f"vid{next(self._ids):08d}"
            session["video_id"] = video_id
            self.videos[video_id] = {
                "id": video_id,
                "snippet": {**session["body"]["snippet"], "publishedAt": "2026-09-01T10:00:00Z"},
                # YouTube stores what was sent; unsent fields get its defaults.
                "status": {"license": "youtube", "embeddable": True, "publicStatsViewable": True, **session["body"]["status"], "uploadStatus": "uploaded"},
            }
        return UploadProgress(complete=True, video=self.videos[session["video_id"]])

    def query_upload(self, access_token: str, session_uri: str, size: int) -> UploadProgress:
        session = self._session(session_uri)
        if len(session["data"]) >= size:
            return self._complete(session)
        return UploadProgress(complete=False, offset=len(session["data"]))

    def upload_chunk(self, access_token: str, session_uri: str, data: bytes, offset: int, size: int) -> UploadProgress:
        self._chunks += 1
        if self._chunks in self.chunk_errors:
            raise self.chunk_errors.pop(self._chunks)
        session = self._session(session_uri)
        assert offset == len(session["data"]), "chunks must resume at the confirmed offset"
        session["data"].extend(data)
        if len(session["data"]) >= size:
            return self._complete(session)
        return UploadProgress(complete=False, offset=len(session["data"]))

    # Videos ------------------------------------------------------------
    def list_videos(self, access_token: str, video_ids: list[str], parts: str) -> list[dict[str, Any]]:
        self.calls.append(("list_videos", video_ids))
        assert len(video_ids) <= 50, "videos.list accepts at most 50 ids"
        if self.list_error:
            raise self.list_error
        return [self.videos[item] for item in video_ids if item in self.videos]

    def update_video(self, access_token: str, body: dict[str, Any], parts: str) -> dict[str, Any]:
        self.calls.append(("update_video", body))
        if self.update_error:
            raise self.update_error
        video = self.videos[body["id"]]
        video["status"] = {**body["status"], "uploadStatus": video["status"].get("uploadStatus")}
        return video

    # Analytics ---------------------------------------------------------
    def analytics_report(self, access_token: str, params: dict[str, str]) -> dict[str, Any]:
        self.calls.append(("analytics", dict(params)))
        if self.analytics_handler is None:
            return {"columnHeaders": [], "rows": []}
        return self.analytics_handler(params)

    def set_thumbnail(self, access_token: str, video_id: str, data: bytes, content_type: str) -> dict[str, Any]:
        self.calls.append(("set_thumbnail", video_id))
        if self.thumbnail_error:
            raise self.thumbnail_error
        self.thumbnails_set.append((video_id, len(data), content_type))
        return {"items": [{"default": {"url": "https://i.ytimg.com/x.jpg"}}]}

    def list_categories(self, access_token: str, region_code: str, language: str) -> list[dict[str, Any]]:
        self.calls.append(("list_categories", (region_code, language)))
        return self.categories

    def publish(self, video_id: str, when: str = "2026-09-02T12:00:00Z") -> None:
        self.videos[video_id]["status"].update(privacyStatus="public", uploadStatus="processed")
        self.videos[video_id]["status"].pop("publishAt", None)
        self.videos[video_id]["snippet"]["publishedAt"] = when


def analytics_payload(metrics: dict[str, float] | None = None, *, curve: list[float] | None = None, content_type: str = "SHORTS", rejected: set[str] | None = None, video_id: str = ""):
    """An Analytics API stand-in answering video, creatorContentType and retention queries."""
    rejected = rejected or set()

    def handler(params: dict[str, str]) -> dict[str, Any]:
        names = params["metrics"].split(",")
        dimension = params["dimensions"]
        if dimension == "video":
            if len(names) > 1 and rejected & set(names):
                raise YouTubeApiError("bad_request", "Unknown identifier (engagedViews) given in field parameters.metrics.")
            if rejected & set(names):
                raise YouTubeApiError("bad_request", "Unknown identifier given in field parameters.metrics.")
            if metrics is None:
                return {"columnHeaders": [{"name": "video"}, *({"name": name} for name in names)], "rows": []}
            return {
                "columnHeaders": [{"name": "video"}, *({"name": name} for name in names)],
                "rows": [[params["filters"].split("==")[1], *(metrics.get(name, 0) for name in names)]],
            }
        if dimension == "creatorContentType":
            return {"columnHeaders": [{"name": "creatorContentType"}, {"name": "views"}], "rows": [[content_type, 10]] if metrics else []}
        if dimension == "elapsedVideoTimeRatio":
            if curve is None:
                return {"columnHeaders": [{"name": "elapsedVideoTimeRatio"}, *({"name": name} for name in names)], "rows": []}
            rows = []
            for index, value in enumerate(curve, start=1):
                row = [round(index / len(curve), 2)]
                for name in names:
                    row.append({
                        "audienceWatchRatio": value,
                        "relativeRetentionPerformance": 0.5,
                        "startedWatching": 0.0,
                        "stoppedWatching": max(0.0, (curve[index - 2] if index > 1 else 1.0) - value) * 100,
                        "totalSegmentImpressions": value * 100,
                    }[name])
                rows.append(row)
            return {"columnHeaders": [{"name": "elapsedVideoTimeRatio"}, *({"name": name} for name in names)], "rows": rows}
        raise AssertionError(f"unexpected analytics query {params}")

    return handler


def rendered_state(duration: float = 10.0, *, scenes: int = 4, strategy: str = "counterintuitive_insight") -> dict[str, Any]:
    step = duration / scenes
    scene_rows = []
    for index in range(scenes):
        media: dict[str, Any] = {"identity": f"pexels:video:{index}", "source": "pexels", "provider": "pexels", "kind": "video"}
        overlays: list[dict[str, Any]] = []
        if index == 2:
            media = {"identity": "generated_openai:photo:abc", "source": "generated_openai", "provider": "generated_openai", "kind": "photo"}
        if index == 1:
            overlays = [{"kind": "process", "spec": {"kind": "process", "steps": ["a", "b"]}}]
        scene_rows.append({
            "id": f"scene_{index + 1:02d}",
            "block_id": f"voice_block_{index + 1:02d}",
            "start": round(index * step, 3),
            "end": round((index + 1) * step, 3),
            "narration": "some narrated words here",
            "story_role": ["hook", "context", "primary_answer", "payoff"][index % 4],
            "story_stage": "open",
            "is_primary_answer": index == 2,
            "is_final_payoff": index == 3,
            "media": media,
            "overlays": overlays,
            "visual_intent": {"visual_strategy": "literal"},
            "visual_director": {"visual_strategy": "stock_photo" if index == 0 else "literal", "decision": "real_media", "resolved_type": "real_video", "decision_reason": "accepted"},
        })
    return {
        "version": 2,
        "intent": {"content_type": "factual_explainer", "language": "en", "topic": "Airplane windows"},
        "format_plan": {"selected_format": "explanation"},
        "story_arc": {"format": "explanation", "structure": "answer_first"},
        "script": {
            "text": "Planes once lost windows. Round corners spread stress.",
            "word_count": 8,
            "triple_hook": {
                "hook_id": "hook_03",
                "status": "selected",
                "source": "generated",
                "selected_strategy": strategy,
                "verbal_hook": "Square windows once tore planes apart.",
                "on_screen_text_hook": "Why round?",
                "visual_hook": {"subject": "airplane window", "visual_strategy": "literal"},
                "score": 83.5,
                "reason_codes": ["concrete", "curiosity"],
                "selection": {"candidate_count": 4, "eligible_count": 2, "selected_id": "hook_03", "candidates": [{"id": "hook_03", "strategy": strategy, "score": 83.5}]},
            },
        },
        "duration": {"actual_seconds": duration},
        "timeline": {"duration": duration, "width": 1080, "height": 1920, "fps": 30, "aspect_ratio": "9:16", "cut_pace": "fast"},
        "voice": {"provider": "openai", "voice_id": "marin", "profile": "Warm documentary", "tone": "warm", "speed": 1.0},
        "music": {"enabled": True, "track": {"id": "track_7"}, "volume": 0.14, "ducking": True},
        "captions": {"enabled": True, "style": "karaoke"},
        "scenes": scene_rows,
        "final_quality_review": {
            "status": "issues_remain",
            "overall_score": 71,
            "issues": [{"id": "i1", "scene_id": "scene_02"}, {"id": "i2", "scene_id": "scene_03"}],
            "unresolved": ["i2"],
            "repairs": [{"scene_id": "scene_02", "repair_effective": True}, {"scene_id": "scene_03", "repair_effective": False}],
            "repaired_scenes": ["scene_02"],
            "repair_pass_count": 1,
        },
        "social_metadata": {
            "status": "available",
            "platforms": {"youtube": {"title": "Why are airplane windows <round>?", "description": "The comet disaster explained.", "hashtags": ["#aviation", "#shorts", "#engineering"]}},
        },
        "render": {"status": "complete", "url": "/media/x/renders/v1/clipforge.mp4", "revision": 1, "stale": False},
        "content_hashes": {"render": "abc123"},
        "edit_history": [],
    }


def add_export_revision(db, project: Project, settings: Settings, content: bytes, render_state: dict[str, Any]) -> None:
    """Append a render revision and its export revision; write the canonical MP4."""
    render_number = max((item.number for item in project.revisions), default=0) + 1
    state = {**render_state, "render": {**render_state["render"], "revision": render_number}}
    project.revisions.append(ProjectRevision(number=render_number, parent_revision=project.current_revision if project.revisions else None, instruction="Render video", kind="system" if project.revisions else "initial", state=state, changed_components=["render"]))
    filename = safe_export_filename(project.title, project.id)
    downloads = settings.resolved_downloads_root
    downloads.mkdir(parents=True, exist_ok=True)
    (downloads / filename).write_bytes(content)
    media_url = f"/api/projects/{project.id}/exported-video"
    export_state = {
        **state,
        "export": {"status": "exported", "filename": filename, "media_url": media_url, "source_revision": render_number, "file_size": len(content)},
        "render": {**state["render"], "url": media_url, "exported": True, "stale": False, "status": "complete"},
    }
    project.revisions.append(ProjectRevision(number=render_number + 1, parent_revision=render_number, instruction="Export MP4", kind="system", state=export_state, changed_components=["export"]))
    project.current_revision = render_number + 1
    project.active_tip_revision = render_number + 1
    db.commit()
    db.refresh(project)


def exported_project(db, settings: Settings, content: bytes = b"\x00mp4" * 6000, *, project_id: str = "11111111-1111-4111-8111-111111111111", duration: float = 10.0, strategy: str = "counterintuitive_insight") -> Project:
    project = Project(id=project_id, original_prompt="Why are airplane windows round?", title="Why are airplane windows round?", status="exported", current_revision=1)
    db.add(project)
    db.flush()
    add_export_revision(db, project, settings, content, rendered_state(duration, strategy=strategy))
    return project


def publish_options(**overrides: Any):
    from clipforge.youtube.publishing import PublishOptions

    values: dict[str, Any] = {
        "title": "Why are airplane windows round?",
        "description": "The comet disaster explained.\n\n#aviation #shorts #engineering",
        "tags": ["aviation", "shorts", "engineering"],
        "thumbnail": {"source": "youtube_auto"},
        "made_for_kids": False,
        "contains_synthetic_media": False,
        "visibility": "private",
        "default_language": "en",
        "default_audio_language": "en",
    }
    values.update(overrides)
    return PublishOptions.model_validate(values)
