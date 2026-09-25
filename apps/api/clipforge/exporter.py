from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .music import resolve_track_path
from .renderer import ffmpeg_path, music_filter_graph, music_input_args, music_render_config

MIN_EXPORT_BYTES = 10_000
GENERATED_PROJECT_DIRECTORIES = (
    "renders",
    "assets",
    "audio",
    "media",
    "generated",
    "tmp",
    "temp",
    "segment-cache",
    "critic",
)


class ExportUnavailable(RuntimeError):
    pass


class UnsafeExportPath(ExportUnavailable):
    pass


@dataclass(frozen=True)
class CleanupResult:
    status: str
    removed: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ExportResult:
    filename: str
    display_path: str
    media_url: str
    file_size: int
    exported_at: str
    cleanup: CleanupResult
    already_exported: bool = False


@dataclass(frozen=True)
class FinalizedExport:
    filename: str
    display_path: str
    media_url: str
    file_size: int
    exported_at: str
    destination: Path
    backup: Path | None
    already_exported: bool = False


Verifier = Callable[[Path], None]
logger = logging.getLogger(__name__)


def safe_export_filename(title: str, project_id: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(title or ""))
    ascii_title = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title).strip("-")
    slug = slug[:72].rstrip("-") or "clipforge-video"
    identity = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:10]
    return f"{slug}-{identity}.mp4"


def _project_directory(project_id: str, settings: Settings) -> Path:
    storage_root = settings.render_root.resolve()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,127}", project_id):
        raise UnsafeExportPath("The project storage identity is invalid.")
    project_dir = (storage_root / project_id).resolve()
    if project_dir.parent != storage_root:
        raise UnsafeExportPath("The project directory is outside ClipForge project storage.")
    return project_dir


def _downloads_directory(settings: Settings) -> Path:
    downloads = settings.resolved_downloads_root
    storage_root = settings.render_root.resolve()
    if downloads == storage_root or downloads.is_relative_to(storage_root):
        raise UnsafeExportPath("Downloads must be outside generated project storage.")
    return downloads


def _source_render(state: dict[str, Any], project_dir: Path, settings: Settings) -> Path:
    url = str(state.get("render", {}).get("url") or "")
    if not url.startswith("/media/"):
        raise ExportUnavailable("This project has no finished working render to export.")
    relative = url.removeprefix("/media/")
    source = (settings.render_root.resolve() / relative).resolve()
    renders = (project_dir / "renders").resolve()
    if (
        not source.is_relative_to(renders)
        or source.suffix.casefold() != ".mp4"
        or not source.is_file()
    ):
        raise ExportUnavailable("The finished project render is missing or outside project storage.")
    return source


def verify_mp4(path: Path) -> None:
    candidate = path.resolve()
    if not candidate.is_file() or candidate.stat().st_size < MIN_EXPORT_BYTES:
        raise ExportUnavailable("The exported MP4 is missing or too small to be valid.")

    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        command = [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=format_name:stream=codec_type",
            "-of",
            "json",
            str(candidate),
        ]
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=45, check=False
            )
            payload = json.loads(completed.stdout or "{}")
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise ExportUnavailable("FFprobe could not verify the exported MP4.") from exc
        formats = str(payload.get("format", {}).get("format_name") or "").split(",")
        streams = {
            str(stream.get("codec_type"))
            for stream in payload.get("streams", [])
            if isinstance(stream, dict)
        }
        if completed.returncode != 0 or "mp4" not in formats or not {"video", "audio"}.issubset(streams):
            raise ExportUnavailable("The export is not a valid narrated MP4 with video and audio.")
        return

    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise ExportUnavailable("FFmpeg is unavailable, so the exported MP4 cannot be verified.")
    try:
        completed = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-i",
                str(candidate),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExportUnavailable("FFmpeg could not verify the exported MP4.") from exc
    probe_text = completed.stderr.casefold()
    if completed.returncode != 0 or "input #0, mov,mp4" not in probe_text:
        raise ExportUnavailable("The export is not a valid narrated MP4 with video and audio.")


def cleanup_project_files(project_id: str, settings: Settings) -> CleanupResult:
    project_dir = _project_directory(project_id, settings)
    storage_root = settings.render_root.resolve()
    removed: list[str] = []
    warnings: list[str] = []
    for name in GENERATED_PROJECT_DIRECTORIES:
        target = project_dir / name
        if not target.exists() and not target.is_symlink():
            continue
        try:
            if target.parent.resolve() != project_dir or not project_dir.is_relative_to(storage_root):
                raise UnsafeExportPath(f"Refused to clean unsafe project path: {name}")
            if target.is_symlink():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            removed.append(name)
        except (OSError, UnsafeExportPath) as exc:
            warnings.append(f"Could not remove project {name}: {exc}")
    return CleanupResult(
        status="warning" if warnings else "complete",
        removed=tuple(removed),
        warnings=tuple(warnings),
    )


def exported_video_path(
    project_id: str,
    title: str,
    export_metadata: dict[str, Any],
    settings: Settings,
) -> Path:
    expected = safe_export_filename(title, project_id)
    if export_metadata.get("filename") != expected:
        raise ExportUnavailable("The project's canonical export metadata is invalid.")
    downloads = _downloads_directory(settings)
    candidate = (downloads / expected).resolve()
    if candidate.parent != downloads or not candidate.is_file():
        raise ExportUnavailable("The project's canonical exported video is unavailable.")
    return candidate


def finalize_export(
    project_id: str,
    title: str,
    state: dict[str, Any],
    settings: Settings,
    *,
    verifier: Verifier | None = None,
) -> FinalizedExport:
    verifier = verifier or verify_mp4
    project_dir = _project_directory(project_id, settings)
    downloads = _downloads_directory(settings)
    filename = safe_export_filename(title, project_id)
    media_url = f"/api/projects/{project_id}/exported-video"
    previous = state.get("export") if isinstance(state.get("export"), dict) else {}

    try:
        source = _source_render(state, project_dir, settings)
    except ExportUnavailable:
        if previous.get("status") != "exported":
            raise
        existing = exported_video_path(project_id, title, previous, settings)
        verifier(existing)
        return FinalizedExport(
            filename=filename,
            display_path=f"Downloads / {filename}",
            media_url=media_url,
            file_size=existing.stat().st_size,
            exported_at=str(previous.get("exported_at") or datetime.now(UTC).isoformat()),
            destination=existing,
            backup=None,
            already_exported=True,
        )

    try:
        downloads.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ExportUnavailable("ClipForge could not create its Downloads directory.") from exc
    destination_entry = downloads / filename
    if destination_entry.is_symlink():
        raise UnsafeExportPath("The canonical export destination cannot be a symbolic link.")
    destination = destination_entry.resolve()
    if destination.parent != downloads:
        raise UnsafeExportPath("The export destination escaped ClipForge Downloads.")
    staging = downloads / f".{filename}.{uuid.uuid4().hex}.staging.mp4"
    backup = downloads / f".{filename}.{uuid.uuid4().hex}.previous.mp4"
    had_previous = destination.is_file()
    try:
        music = state.get("music") if isinstance(state.get("music"), dict) else {}
        track = resolve_track_path(music) if music.get("enabled") else None
        if track is None:
            if music.get("enabled"):
                selected = music.get("track") if isinstance(music.get("track"), dict) else {}
                logger.warning(
                    "Music export source unavailable track_id=%s file=%s",
                    selected.get("id"), selected.get("file"),
                )
            shutil.copy2(source, staging)
        else:
            selected = music.get("track") if isinstance(music.get("track"), dict) else {}
            logger.info(
                "Mixing music track_id=%s source=%s exists=%s size=%s",
                selected.get("id"), track, track.is_file(), track.stat().st_size if track.is_file() else 0,
            )
            ffmpeg = ffmpeg_path()
            if not ffmpeg:
                raise ExportUnavailable("FFmpeg is unavailable, so ClipForge cannot mix the selected music.")
            timeline = state.get("timeline") if isinstance(state.get("timeline"), dict) else {}
            duration = max(1.0, float(timeline.get("duration") or 60))
            command = [ffmpeg, "-y", "-v", "error", "-i", str(source), *music_input_args(track, duration)]
            # The base render already contains the immutable narration. ffprobe is
            # deliberately avoided here; -shortest safely follows the video source.
            command.extend([
                "-filter_complex", music_filter_graph(music_render_config(state), duration),
                "-map", "0:v:0", "-map", "[mixed]", "-c:v", "copy", "-c:a", "aac",
                "-shortest", "-movflags", "+faststart", str(staging),
            ])
            try:
                completed = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)
            except (OSError, subprocess.SubprocessError) as exc:
                logger.warning("Music export FFmpeg invocation failed track_id=%s error=%s", selected.get("id"), exc)
                raise ExportUnavailable("The selected music could not be mixed into the export.") from exc
            if completed.returncode != 0 or not staging.is_file():
                logger.warning(
                    "Music export mix failed track_id=%s returncode=%s output_exists=%s stderr=%s",
                    selected.get("id"), completed.returncode, staging.is_file(),
                    " ".join((completed.stderr or "").split())[-1000:],
                )
                raise ExportUnavailable("The selected music could not be mixed into the export.")
            logger.info("Music export mix complete track_id=%s output=%s size=%s", selected.get("id"), staging, staging.stat().st_size)
        verifier(staging)
        if had_previous:
            os.replace(destination, backup)
        os.replace(staging, destination)
        try:
            verifier(destination)
        except Exception:
            destination.unlink(missing_ok=True)
            if backup.exists():
                os.replace(backup, destination)
            raise
    except Exception as exc:
        staging.unlink(missing_ok=True)
        if backup.exists() and not destination.exists():
            os.replace(backup, destination)
        if isinstance(exc, ExportUnavailable):
            raise
        raise ExportUnavailable("ClipForge could not finalize the exported MP4 safely.") from exc

    exported_at = datetime.now(UTC).isoformat()
    return FinalizedExport(
        filename=filename,
        display_path=f"Downloads / {filename}",
        media_url=media_url,
        file_size=destination.stat().st_size,
        exported_at=exported_at,
        destination=destination,
        backup=backup if had_previous else None,
    )


def commit_finalized_export(finalized: FinalizedExport) -> tuple[str, ...]:
    """Commit a verified export after database persistence; cleanup failure is non-fatal."""
    if finalized.backup is None:
        return ()
    try:
        finalized.backup.unlink(missing_ok=True)
    except OSError as exc:
        return (f"Could not remove the previous export backup: {exc}",)
    return ()


def rollback_finalized_export(finalized: FinalizedExport) -> None:
    """Restore the previous canonical export when database persistence fails."""
    if finalized.already_exported:
        return
    try:
        finalized.destination.unlink(missing_ok=True)
        if finalized.backup is not None and finalized.backup.exists():
            os.replace(finalized.backup, finalized.destination)
    except OSError as exc:
        raise ExportUnavailable(
            "Export persistence failed and ClipForge could not restore the previous canonical file."
        ) from exc
