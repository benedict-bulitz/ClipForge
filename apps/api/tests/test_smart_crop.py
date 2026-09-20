from types import SimpleNamespace

from PIL import Image

from clipforge.smart_crop import analyze_scene_media, crop_windows
from clipforge.visual_verifier import visual_intent_text


def test_crop_windows_stay_inside_source_and_target_ratio():
    windows = crop_windows(1920, 1080, 9 / 16)
    assert len(windows) == 5
    for left, top, width, height in windows:
        assert 0 <= left <= 1 and 0 <= top <= 1
        assert 0 < width <= 1 and 0 < height <= 1
        assert left + width <= 1.00001
        assert top + height <= 1.00001


def test_visual_prompt_omits_dangling_context_preposition():
    assert visual_intent_text({"visual_intent": {"objects": ["square"]}})[0] == "a photo of square"


def test_photo_smart_crop_uses_fake_semantic_score(tmp_path):
    image = Image.new("RGB", (160, 90), "black")
    for x in range(100, 160):
        for y in range(90):
            image.putpixel((x, y), (255, 0, 0))
    path = tmp_path / "asset.png"
    image.save(path)

    class FakeVerifier:
        status = "available"
        model_identity = "fake-v1"

        def score_image(self, crop, _texts, *, asset_identity):
            return crop.resize((1, 1)).getpixel((0, 0))[0] / 255

    settings = SimpleNamespace(render_root=tmp_path)
    scene = {"media": {"kind": "photo", "cache_path": "asset.png", "identity": "photo:1"}, "visual_goal": "red subject", "visual_intent": {"visual_goal": "red subject", "objects": ["subject"]}}
    state = {"timeline": {"width": 1080, "height": 1920}}
    result = analyze_scene_media(scene, state, settings, verifier=FakeVerifier())
    assert result["mode"] == "smart"
    assert result["center_x"] > 0.5
