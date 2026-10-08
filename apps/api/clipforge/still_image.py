from __future__ import annotations

"""The one static-image input authority for FFmpeg.

Still scenes (Pexels / Wikimedia / Pixabay photos, generated images) and the
transparent overlay PNGs are fed to FFmpeg as ``-loop 1 -i <file>``.  ``-loop``
is an option of FFmpeg's image2 demuxer, and FFmpeg picks the demuxer from the
file's *content*, not its name.  Photos are cached as ``photo-<id>.jpg``
whatever the provider served: Wikimedia's ``filetype:bitmap`` search also
returns GIFs, whose thumbnails stay GIFs.  A GIF saved as ``.jpg`` opens with
the ``gif`` demuxer, which has no ``loop`` option:

    Option loop not found.
    Error opening input file .../assets/wikimedia/photo-<id>.jpg
    Error opening input files: Option not found

So a still reaches FFmpeg only as a single-frame image whose content matches
its name (JPEG as .jpg/.jpeg, PNG as .png).  A file Pillow identifies as
anything else - GIF, WebP, TIFF, an animated image, a PNG named .jpg - becomes
a real JPEG of its first frame.  A file Pillow cannot identify is left to
FFmpeg exactly as before (it may read formats Pillow does not); an identified
image that cannot be decoded is refused with a clear error.
"""

import hashlib
from pathlib import Path

from PIL import Image, UnidentifiedImageError

# Pillow format -> file suffixes FFmpeg's image2 demuxer opens as that format.
READY_FORMATS = {"JPEG": {".jpg", ".jpeg"}, "PNG": {".png"}}
JPEG_QUALITY = 92
MAX_PIXELS = 80_000_000  # refuse decompression bombs before decoding


class StillImageError(ValueError):
    """The file is not a usable still image."""


def identify(path: Path) -> tuple[str, int] | None:
    """(Pillow format, frame count), or None when Pillow cannot identify the file."""
    try:
        with Image.open(path) as image:
            return image.format or "", int(getattr(image, "n_frames", 1) or 1)
    except (OSError, UnidentifiedImageError, ValueError):
        return None


def ffmpeg_ready(path: Path) -> bool:
    """A single-frame JPEG/PNG whose name matches its content."""
    found = identify(path)
    return found is not None and path.suffix.casefold() in READY_FORMATS.get(found[0], set()) and found[1] == 1


def needs_conversion(path: Path) -> bool:
    """Only an identified image whose content does not match its name (or is animated)."""
    return identify(path) is not None and not ffmpeg_ready(path)


def write_jpeg(source: Path, destination: Path) -> Path:
    """The first frame of any readable image as an RGB JPEG (written atomically).

    Transparency is flattened onto black, like letterboxing in a video frame.
    Orientation is left as stored, exactly as FFmpeg would have read it.
    """
    try:
        with Image.open(source) as image:
            if image.width * image.height > MAX_PIXELS:
                raise StillImageError(f"{source.name} is too large to use as a still image.")
            image.seek(0)
            frame = image.convert("RGBA") if image.mode in {"P", "PA", "LA", "RGBA"} or "transparency" in image.info else image.convert("RGB")
            if frame.mode == "RGBA":
                flat = Image.new("RGB", frame.size, (0, 0, 0))
                flat.paste(frame, mask=frame.getchannel("A"))
                frame = flat
            destination.parent.mkdir(parents=True, exist_ok=True)
            staging = destination.with_name(destination.name + ".partial")
            frame.save(staging, format="JPEG", quality=JPEG_QUALITY)
    except StillImageError:
        raise
    except (OSError, UnidentifiedImageError, ValueError, Image.DecompressionBombError) as exc:
        raise StillImageError(f"{source.name} is not a readable image.") from exc
    staging.replace(destination)
    return destination


def normalize_download(path: Path) -> Path:
    """Make a freshly cached photo what its name says (``photo-<id>.jpg`` = JPEG).

    Idempotent: a real single-frame JPEG, or a file Pillow cannot identify,
    is left untouched.
    """
    if not needs_conversion(path):
        return path
    if path.suffix.casefold() not in {".jpg", ".jpeg"}:
        raise StillImageError(f"{path.name} is not a JPEG path.")
    return write_jpeg(path, path)


def ffmpeg_input(path: Path, work_dir: Path) -> Path:
    """The file to pass to ``-loop 1 -i`` for this still.

    Ready (or unidentifiable) files are used as they are; a mislabelled image
    is converted into ``work_dir`` (named by content, so repeated renders reuse
    it).  This also repairs files cached before downloads were normalized.
    """
    if not needs_conversion(path):
        return path
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError as exc:
        raise StillImageError(f"{path.name} could not be read.") from exc
    target = work_dir / f"still-{digest}.jpg"
    if target.is_file() and ffmpeg_ready(target):
        return target
    return write_jpeg(path, target)


def ffmpeg_still_args(path: Path) -> list[str]:
    """``-loop 1 -i <path>``: the only way a still enters an FFmpeg command."""
    return ["-loop", "1", "-i", str(path)]
