from __future__ import annotations

import shutil
import subprocess

import pytest

from clipforge.export_quality import run_export_quality_checks


@pytest.fixture(scope="module")
def synthetic_media(tmp_path_factory):
    """Creates synthetic MP4s for testing export quality checks."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg not available")
        
    fixtures_dir = tmp_path_factory.mktemp("media")
    
    # 1. Normal short video (video + audio, 1080x1920, 2s)
    normal = fixtures_dir / "normal.mp4"
    subprocess.run([
        ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=blue:s=1080x1920:d=2",
        "-f", "lavfi", "-i", "aevalsrc=0:d=2",
        "-c:v", "libx264", "-c:a", "aac", str(normal)
    ], check=True)
    
    # 2. Too long video (182s)
    long_vid = fixtures_dir / "long.mp4"
    subprocess.run([
        ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=red:s=1080x1920:d=182",
        "-f", "lavfi", "-i", "aevalsrc=0:d=182",
        "-c:v", "libx264", "-c:a", "aac", str(long_vid)
    ], check=True)
    
    # 3. Wrong dimensions (1920x1080)
    wrong_dim = fixtures_dir / "wrong_dim.mp4"
    subprocess.run([
        ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=green:s=1920x1080:d=2",
        "-f", "lavfi", "-i", "aevalsrc=0:d=2",
        "-c:v", "libx264", "-c:a", "aac", str(wrong_dim)
    ], check=True)
    
    # 4. No audio
    no_audio = fixtures_dir / "no_audio.mp4"
    subprocess.run([
        ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=yellow:s=1080x1920:d=2",
        "-c:v", "libx264", str(no_audio)
    ], check=True)

    # 5. Black frames (0.6s black) and silence (2s silence inside a 3s video)
    # We'll make it 3 seconds long, with 2s of silence.
    bad_content = fixtures_dir / "bad_content.mp4"
    # To get silence we can just use aevalsrc=0 (absolute silence). 
    # For black frames, color=black for 1 second.
    subprocess.run([
        ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=black:s=1080x1920:d=3",
        "-f", "lavfi", "-i", "aevalsrc=0:d=3",
        "-c:v", "libx264", "-c:a", "aac", str(bad_content)
    ], check=True)
    
    return {
        "normal": normal,
        "long": long_vid,
        "wrong_dim": wrong_dim,
        "no_audio": no_audio,
        "bad_content": bad_content
    }

def test_quality_normal(synthetic_media):
    state = {
        "timeline": {"duration": 2.0, "width": 1080, "height": 1920},
        "voice": {"enabled": True}
    }
    issues = run_export_quality_checks(state, synthetic_media["normal"], max_duration=180)
    assert not any(i["severity"] == "error" for i in issues)

def test_quality_duration_exceeds(synthetic_media):
    state = {
        "timeline": {"duration": 182.0, "width": 1080, "height": 1920},
        "voice": {"enabled": True}
    }
    issues = run_export_quality_checks(state, synthetic_media["long"], max_duration=180)
    assert any(i["code"] == "duration_exceeds_limit" for i in issues)

def test_quality_dimension_mismatch(synthetic_media):
    state = {
        "timeline": {"duration": 2.0, "width": 1080, "height": 1920},
        "voice": {"enabled": True}
    }
    issues = run_export_quality_checks(state, synthetic_media["wrong_dim"], max_duration=180)
    assert any(i["code"] == "dimension_mismatch" for i in issues)

def test_quality_missing_audio(synthetic_media):
    state = {
        "timeline": {"duration": 2.0, "width": 1080, "height": 1920},
        "voice": {"enabled": True}
    }
    issues = run_export_quality_checks(state, synthetic_media["no_audio"], max_duration=180)
    assert any(i["code"] == "missing_audio_stream" for i in issues)

def test_quality_bad_content(synthetic_media):
    state = {
        "timeline": {"duration": 3.0, "width": 1080, "height": 1920},
        "voice": {"enabled": True}
    }
    issues = run_export_quality_checks(state, synthetic_media["bad_content"], max_duration=180)
    # The bad_content video is all black and all silence.
    codes = [i["code"] for i in issues]
    assert "unintended_black_frames" in codes
    assert "unexpected_audio_silence" in codes
    assert "suspicious_frozen_frames" in codes

def test_quality_narration_overflow(synthetic_media):
    state = {
        "timeline": {"duration": 2.0, "width": 1080, "height": 1920},
        "voice": {
            "enabled": True,
            "blocks": [{"start": 0.0, "end": 3.5}] # video is 2s, narration ends at 3.5
        }
    }
    issues = run_export_quality_checks(state, synthetic_media["normal"], max_duration=180)
    assert any(i["code"] == "narration_overflow" for i in issues)

def test_quality_missing_media(synthetic_media):
    state = {
        "timeline": {"duration": 2.0, "width": 1080, "height": 1920},
        "scenes": [
            {"id": "scene_01", "asset_status": "missing_media"}
        ],
        "voice": {"enabled": True}
    }
    issues = run_export_quality_checks(state, synthetic_media["normal"], max_duration=180)
    assert any(i["code"] == "missing_media" for i in issues)
