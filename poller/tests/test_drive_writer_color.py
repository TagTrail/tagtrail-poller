import pytest

pytest.importorskip("googleapiclient")

from poller.drive_writer import _color_for_id  # noqa: E402


def test_color_for_id_is_deterministic():
    assert _color_for_id("abc") == _color_for_id("abc")


def test_color_for_id_returns_hex_from_palette():
    color = _color_for_id("anything")
    assert color.startswith("#")
    assert len(color) == 7
