from io import BytesIO

import pytest


def test_background_preview_fits_and_persists_alpha(tmp_path):
    pytest.importorskip("tkinter")
    image = pytest.importorskip("PIL.Image")
    from localnet.image_previews import decode_preview

    original = tmp_path / "transparent.png"
    cached = tmp_path / "thumbnails" / "cached.png"
    image.new("RGBA", (1800, 900), (20, 60, 90, 127)).save(original)
    preview = decode_preview(original, 240, disk_cache=cached)
    assert len(preview.frames) == 1 and cached.is_file()
    with image.open(BytesIO(preview.frames[0][0])) as scaled:
        assert scaled.size == (240, 120) and scaled.getpixel((0, 0))[3] == 127
    assert decode_preview(original, 240, disk_cache=cached).frames[0][0] == preview.frames[0][0]


def test_animated_gif_preview_retains_frames_and_timing(tmp_path):
    pytest.importorskip("tkinter")
    image = pytest.importorskip("PIL.Image")
    from localnet.image_previews import decode_preview

    original = tmp_path / "motion.gif"
    frames = [image.new("RGB", (60, 25), color) for color in ("blue", "green", "red")]
    frames[0].save(original, save_all=True, append_images=frames[1:], loop=0, duration=[90, 180, 270])
    static = decode_preview(original, 32)
    motion = decode_preview(original, 64, animate=True)
    assert len(static.frames) == 1
    assert [delay for _, delay in motion.frames] == [90, 180, 270]
    assert len({data for data, _ in motion.frames}) == 3
