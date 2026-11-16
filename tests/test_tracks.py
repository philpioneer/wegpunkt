import gzip

import pytest

from wegpunkt import tracks
from wegpunkt.sports import guess_sport

from .conftest import GPX, fit_bytes


def test_gpx_is_analyzed():
    t = tracks.parse(GPX)
    assert t.name == "Morgenrunde" and t.sport_hint == "run" and t.source == "gpx"
    assert 3800 < t.distance_m < 4000
    assert t.duration_s == 1170 and t.moving_s == 1170
    assert 50 < t.elevation_gain_m < 60 and t.elevation_loss_m == 0
    assert t.extra["version"] == tracks.ANALYSIS_VERSION


def test_splits_and_best_efforts():
    t = tracks.parse(GPX)
    splits = t.extra["splits"]
    assert [s["m"] for s in splits[:3]] == [1000, 1000, 1000] and splits[-1]["m"] < 1000
    assert all(290 < s["s"] < 310 for s in splits[:3])          # gleichmäßig 5:00 min/km
    assert abs(sum(s["s"] for s in splits) - t.moving_s) < 2
    assert 285 < t.extra["efforts"]["1 km"] < 305
    assert "5 km" not in t.extra["efforts"]                      # der Lauf ist nur 3,9 km lang


def test_privacy_zone_hides_start_and_end():
    t = tracks.parse(GPX)
    visible = tracks.visible_points(t, 200)
    first, last = t.samples[0], t.samples[-1]
    assert tracks.haversine_m(visible[0][0], visible[0][1], first.lat, first.lon) >= 200
    assert tracks.haversine_m(visible[-1][0], visible[-1][1], last.lat, last.lon) >= 200
    assert len(tracks.visible_points(t, 0)) == len(t.samples)
    assert tracks.visible_points(t, 50_000) == []                # alles innerhalb der Zone


def test_fit_file_with_heart_rate():
    t = tracks.parse(fit_bytes())
    assert t.source == "fit" and t.sport_hint == "enduro"
    assert 3200 < t.distance_m < 3400 and t.duration_s == 590
    assert 100 < t.elevation_gain_m <= 118
    assert 120 <= t.extra["avg_hr"] <= 150 and t.extra["max_hr"] == 149
    assert tracks.visible_points(t, 0)[5][4] == 125              # Puls je Punkt


def test_gzipped_files_are_unpacked():
    assert tracks.parse(gzip.compress(fit_bytes())).source == "fit"
    assert tracks.parse(gzip.compress(GPX)).source == "gpx"


@pytest.mark.parametrize("data", [b"", b"das ist kein gpx", b"<gpx></gpx>", b"\x1f\x8bkaputt"])
def test_broken_files_are_rejected(data):
    with pytest.raises(tracks.TrackError):
        tracks.parse(data)


def test_gpx_without_time_or_elevation():
    gpx = b"""<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>
      <trkpt lat="52.5" lon="13.4"/><trkpt lat="52.51" lon="13.4"/><trkpt lat="52.52" lon="13.4"/>
    </trkseg></trk></gpx>"""
    t = tracks.parse(gpx)
    assert t.duration_s is None and t.moving_s is None and t.elevation_gain_m is None
    assert 2200 < t.distance_m < 2300 and "splits" not in t.extra


@pytest.mark.parametrize("hint,expected", [
    ("running", "run"), ("trail_running", "run"), ("cycling gravel_cycling", "gravel"),
    ("cycling mountain", "enduro"), ("mountain_biking", "enduro"), ("Radfahren", "gravel"),
    ("9", "run"), ("generic", None), ("", None), ("yoga", None),
])
def test_guess_sport(hint, expected):
    assert guess_sport(hint) == expected


def test_route_image_with_map_background(tmp_path):
    import io

    from PIL import Image

    from wegpunkt import render

    class FakeTiles:
        calls = 0

        def get(self, z, x, y):
            FakeTiles.calls += 1
            buf = io.BytesIO()
            Image.new("RGB", (256, 256), (200, 220, 200)).save(buf, "PNG")
            return buf.getvalue()

    points = tracks.visible_points(tracks.parse(GPX), 200)
    out = tmp_path / "karte.png"
    assert render.route_map_png(points, out, (37, 99, 235), FakeTiles())
    img = Image.open(out)
    assert img.size == (1200, 675) and 6 <= FakeTiles.calls <= render.MAX_TILES
    colors = {c for _, c in img.convert("RGB").getcolors(maxcolors=1_000_000)}
    assert (37, 99, 235) in colors                       # die Route in Sportfarbe
    assert not render.route_map_png(points[:1], out, (0, 0, 0), FakeTiles())


def test_route_image_falls_back_without_map_server(tmp_path):
    from wegpunkt.app import create_app
    from .conftest import make_settings

    ctx = create_app(make_settings(tmp_path, map_previews=True)).state.ctx

    class Broken:
        def get(self, z, x, y):
            raise OSError("kein Netz")

        def close(self):
            pass

    ctx.tiles = Broken()
    a = {"id": "abcd", "sport": "run", "track": tracks.visible_points(tracks.parse(GPX), 200)}
    ctx.draw_route(a)
    assert a["has_image"] and (ctx.media_dir / "abcd.png").stat().st_size > 1000
