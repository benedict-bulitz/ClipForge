"""Real macOS render failure: "Option loop not found" for a Wikimedia photo.

Root cause: photos are cached as ``photo-<id>.jpg`` whatever the provider
served.  Wikimedia's ``filetype:bitmap`` search also returns GIFs (their
thumbnails stay GIFs).  FFmpeg picks the demuxer from the *content*: a GIF
named .jpg opens with the ``gif`` demuxer, which has no ``-loop`` option.
"""
from __future__ import annotations

import io
import subprocess
from pathlib import Path

import httpx
import imageio_ffmpeg
import pytest
from PIL import Image
from test_visual_director import finger_project, settings_for

from clipforge import renderer, still_image
from clipforge.media import (
    MediaCandidate,
    MediaProviderError,
    WikimediaMediaClient,
    _cache_candidate,
)
from clipforge.renderer import RenderUnavailable, _create_visual_segment


def ffmpeg() -> str | None:
    try:
        path = imageio_ffmpeg.get_ffmpeg_exe()
    except (OSError, RuntimeError):
        return None
    return path if Path(path).exists() else None


needs_ffmpeg = pytest.mark.skipif(ffmpeg() is None, reason="FFmpeg binary not available")


def image_bytes(fmt: str, *, size=(640, 480), color=(200, 100, 50), mode="RGB", frames: int = 1, **save) -> bytes:
    buffer = io.BytesIO()
    first = Image.new(mode, size, color if mode != "RGBA" else (*color, 0))
    if frames > 1:
        rest = [Image.new(mode, size, (0, 0, 255)) for _ in range(frames - 1)]
        first.save(buffer, format=fmt, save_all=True, append_images=rest, **save)
    else:
        first.save(buffer, format=fmt, **save)
    return buffer.getvalue()


def write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def fmt_of(path: Path) -> tuple[str, int, tuple[int, int]]:
    with Image.open(path) as image:
        return image.format, int(getattr(image, "n_frames", 1) or 1), image.size


# ---------------------------------------------------------------------------
# Root cause, reproduced with the FFmpeg ClipForge runs
# ---------------------------------------------------------------------------


@needs_ffmpeg
def test_root_cause_gif_saved_as_jpg_breaks_the_loop_input(tmp_path):
    gif = write(tmp_path / "assets" / "wikimedia" / "photo-946106.jpg", image_bytes("GIF"))
    real = write(tmp_path / "real.jpg", image_bytes("JPEG"))

    def old_argv(path: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [ffmpeg(), "-y", "-v", "error", "-loop", "1", "-i", str(path), "-t", "0.2", "-vf", "scale=64:64,format=yuv420p", "-f", "null", "-"],
            capture_output=True, text=True, timeout=60, check=False,
        )

    failed = old_argv(gif)
    assert failed.returncode != 0
    assert "Option loop not found" in failed.stderr and "Error opening input file" in failed.stderr
    assert old_argv(real).returncode == 0  # -loop itself is fine for real JPEGs


# ---------------------------------------------------------------------------
# The static-image input authority
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "data"), [
    ("baseline.jpg", image_bytes("JPEG")),
    ("progressive.jpg", image_bytes("JPEG", progressive=True)),
    ("cmyk.jpg", image_bytes("JPEG", mode="CMYK", color=(0, 50, 100, 0))),
    ("generated.png", image_bytes("PNG")),  # OpenAI images are real PNGs
    ("overlay.png", image_bytes("PNG", mode="RGBA")),
])
def test_ready_stills_are_used_unchanged(tmp_path, name, data):
    path = write(tmp_path / name, data)
    assert still_image.ffmpeg_ready(path)
    assert still_image.ffmpeg_input(path, tmp_path / "work") == path
    assert not (tmp_path / "work").exists()


@pytest.mark.parametrize(("label", "data"), [
    ("gif", image_bytes("GIF")),
    ("animated gif", image_bytes("GIF", frames=3)),
    ("png", image_bytes("PNG")),
    ("transparent png", image_bytes("PNG", mode="RGBA")),
    ("webp", image_bytes("WEBP")),
    ("tiff", image_bytes("TIFF")),
])
def test_mislabelled_stills_become_real_single_frame_jpegs(tmp_path, label, data):
    source = write(tmp_path / "assets" / "wikimedia" / "photo-946106.jpg", data)
    converted = still_image.ffmpeg_input(source, tmp_path / "work")
    assert converted != source and converted.parent == tmp_path / "work"
    assert fmt_of(converted) == ("JPEG", 1, (640, 480)), label
    assert source.read_bytes() == data  # the project's cached asset is not rewritten at render time
    assert still_image.ffmpeg_input(source, tmp_path / "work") == converted  # repeated renders reuse it


def test_animated_gif_uses_its_first_frame(tmp_path):
    source = write(tmp_path / "photo-1.jpg", image_bytes("GIF", frames=3, color=(255, 0, 0)))
    converted = still_image.ffmpeg_input(source, tmp_path / "work")
    with Image.open(converted) as image:
        red, _green, blue = image.convert("RGB").getpixel((10, 10))
    assert red > 200 and blue < 60


def truncated_gif() -> bytes:
    """Identified as GIF, but the image data is cut off."""
    return image_bytes("GIF", size=(640, 480))[:40]


def test_an_identified_but_broken_image_is_refused_clearly(tmp_path):
    source = write(tmp_path / "photo-2.jpg", truncated_gif())
    assert still_image.identify(source) == ("GIF", 1)
    with pytest.raises(still_image.StillImageError, match="not a readable image"):
        still_image.ffmpeg_input(source, tmp_path / "work")


def test_unidentifiable_files_are_left_to_ffmpeg_as_before(tmp_path):
    # Pillow does not know every format FFmpeg reads; nothing is guessed or rejected here.
    source = write(tmp_path / "photo-3.jpg", b"not something Pillow can identify")
    assert still_image.identify(source) is None
    assert still_image.ffmpeg_input(source, tmp_path / "work") == source
    assert still_image.normalize_download(source) == source and source.read_bytes() == b"not something Pillow can identify"


def test_every_loop_input_is_built_by_the_authority():
    source = Path(renderer.__file__).read_text(encoding="utf-8")
    assert '"-loop"' not in source  # no hand-built still inputs left in the renderer
    assert source.count("still_image.ffmpeg_still_args(") == 2  # the scene still + its overlay PNGs
    assert still_image.ffmpeg_still_args(Path("/x/a.jpg")) == ["-loop", "1", "-i", "/x/a.jpg"]


# ---------------------------------------------------------------------------
# Downloads: a cached photo-<id>.jpg is a JPEG from now on
# ---------------------------------------------------------------------------


def wikimedia_candidate(page_id: str = "946106") -> MediaCandidate:
    return MediaCandidate(
        provider_id=page_id, kind="photo", download_url=f"https://upload.wikimedia.org/thumb/{page_id}.gif",
        source_url="https://commons.wikimedia.org/wiki/File:x.gif", creator="c", creator_url=None,
        width=640, height=640, duration=None, query="sweets", rank=60, provider="wikimedia", title="File:x.gif",
    )


def wikimedia_serving(body: bytes, content_type: str = "image/gif") -> WikimediaMediaClient:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=body, headers={"content-type": content_type}))
    return WikimediaMediaClient(client=httpx.Client(transport=transport))


def test_wikimedia_gif_is_cached_as_a_real_jpeg(tmp_path):
    render_root = tmp_path
    asset_root = tmp_path / "project" / "assets"
    media = _cache_candidate(wikimedia_candidate(), {}, asset_root=asset_root, render_root=render_root, downloader=wikimedia_serving(image_bytes("GIF", frames=2)))
    cached = render_root / media["cache_path"]
    assert cached.name == "photo-946106.jpg"
    assert fmt_of(cached) == ("JPEG", 1, (640, 480))
    assert still_image.ffmpeg_ready(cached)


def test_a_real_jpeg_download_is_left_untouched(tmp_path):
    data = image_bytes("JPEG")
    media = _cache_candidate(wikimedia_candidate("7"), {}, asset_root=tmp_path / "a", render_root=tmp_path, downloader=wikimedia_serving(data, "image/jpeg"))
    assert (tmp_path / media["cache_path"]).read_bytes() == data


def test_a_broken_image_download_is_rejected_and_not_kept(tmp_path):
    with pytest.raises(MediaProviderError, match="not a usable image"):
        _cache_candidate(wikimedia_candidate("8"), {}, asset_root=tmp_path / "a", render_root=tmp_path, downloader=wikimedia_serving(truncated_gif()))
    assert not (tmp_path / "a" / "wikimedia" / "photo-8.jpg").exists()  # the next render cannot reuse it


# ---------------------------------------------------------------------------
# The real scene render (final render, scene tests and Final Critic share it)
# ---------------------------------------------------------------------------


def still_scene(tmp_path: Path, data: bytes):
    settings = settings_for(tmp_path)
    state = finger_project()
    state["timeline"].update(width=108, height=192, fps=10)
    scene = state["scenes"][0]
    scene.pop("overlays", None)
    scene["media"] = {"identity": "wikimedia:photo:946106", "provider": "wikimedia", "source": "wikimedia", "kind": "photo", "cache_path": "project/assets/wikimedia/photo-946106.jpg"}
    write(tmp_path / "project" / "assets" / "wikimedia" / "photo-946106.jpg", data)
    return settings, state, scene


@needs_ffmpeg
def test_real_render_of_a_gif_saved_as_jpg_scene(tmp_path, monkeypatch):
    """The exact failing input (already cached in an existing project) now renders."""
    settings, state, scene = still_scene(tmp_path, image_bytes("GIF", size=(800, 600)))
    monkeypatch.setattr("clipforge.renderer.analyze_scene_media", lambda *_a, **_k: {"center_x": 0.5, "center_y": 0.5, "confidence": 0.9})
    commands: list[list[str]] = []
    real_run = subprocess.run

    def recording_run(command, **kwargs):
        commands.append(list(command))
        return real_run(command, **kwargs)

    monkeypatch.setattr("clipforge.renderer.subprocess.run", recording_run)
    cache = tmp_path / "segment-cache"
    temp = tmp_path / "render-1"
    temp.mkdir()
    segment = _create_visual_segment(ffmpeg(), state, scene, 0, 0.5, temp, settings, cache_dir=cache)
    assert segment.is_file() and segment.stat().st_size > 0
    argv = commands[-1]
    loop_input = argv[argv.index("-loop") + 3]
    assert argv[argv.index("-loop"):argv.index("-loop") + 3] == ["-loop", "1", "-i"]
    assert Path(loop_input).parent == temp and fmt_of(Path(loop_input))[0] == "JPEG"
    assert scene["render_segment"]["cache"] == "miss"
    # A later render (new temp dir) hits the same segment cache: the key is the scene's own file.
    temp2 = tmp_path / "render-2"
    temp2.mkdir()
    _create_visual_segment(ffmpeg(), state, scene, 0, 0.5, temp2, settings, cache_dir=cache)
    assert scene["render_segment"]["cache"] == "hit"


def test_a_broken_scene_image_fails_with_a_clear_message(tmp_path, monkeypatch):
    settings, state, scene = still_scene(tmp_path, truncated_gif())
    monkeypatch.setattr("clipforge.renderer.analyze_scene_media", lambda *_a, **_k: None)
    temp = tmp_path / "t"
    temp.mkdir()
    with pytest.raises(RenderUnavailable, match=r"Scene 1 media is not a usable image .*Replace this scene's media"):
        _create_visual_segment("ffmpeg", state, scene, 0, 0.5, temp, settings)
