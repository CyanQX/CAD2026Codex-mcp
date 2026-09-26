import math

import pytest

from cad_super_mcp import geometry as g
from cad_super_mcp.geometry import Pt


def close(a: Pt, b: Pt, tol=1e-6):
    return a.dist(b) < tol


def test_point_basics_and_rounding():
    p = Pt.of([1.0, 2.0])
    assert (p.x, p.y, p.z) == (1.0, 2.0, 0.0)
    assert Pt(0.1 + 0.2, 0, 0).as_list(6) == [0.3, 0.0]                     # float noise never leaks to the drawing
    assert Pt(1, 2, 3).as_list() == [1, 2, 3]
    assert Pt(1, 2, 0).as_list(dims=3) == [1, 2, 0]
    assert Pt(-0.0000000001, 0).rounded().x == 0.0 and str(Pt(-1e-12, 0).rounded().x) == "0.0"
    with pytest.raises(ValueError):
        Pt.of([1])


def test_midpoint_lerp_polar_angle():
    a, b = Pt(0, 0), Pt(3600, 0)
    assert close(g.midpoint(a, b), Pt(1800, 0))
    assert close(g.lerp(a, b, 0.25), Pt(900, 0))
    assert close(g.polar(a, 90, 900), Pt(0, 900))
    assert close(g.polar(Pt(100, 100), 45, math.sqrt(2) * 10), Pt(110, 110))
    assert g.angle_deg(a, Pt(0, 5)) == pytest.approx(90)
    assert g.normalize_deg(-90) == 270 and g.normalize_deg(720) == 0
    assert g.distance(Pt(0, 0), Pt(3, 4)) == 5


def test_rotate_and_project_and_intersect():
    assert close(g.rotate_about(Pt(10, 0), Pt(0, 0), 90), Pt(0, 10))
    assert close(g.project_on_line(Pt(5, 7), Pt(0, 0), Pt(10, 0)), Pt(5, 0))
    x = g.segment_intersection(Pt(0, 0), Pt(10, 10), Pt(0, 10), Pt(10, 0))
    assert close(x, Pt(5, 5))
    assert g.segment_intersection(Pt(0, 0), Pt(1, 0), Pt(0, 1), Pt(1, 1)) is None            # parallel
    assert g.segment_intersection(Pt(0, 0), Pt(1, 1), Pt(5, 0), Pt(5, 10)) is None            # outside the segments
    assert close(g.segment_intersection(Pt(0, 0), Pt(1, 1), Pt(5, 0), Pt(5, 10), infinite=True), Pt(5, 5))


def test_divide_segment():
    pts = g.divide_segment(Pt(0, 0), Pt(3000, 0), 3)
    assert [p.x for p in pts] == [1000, 2000]


def test_rectangle_by_corner_center_and_rotation():
    r = g.rect_vertices(Pt(0, 0), 3600, 2400)
    assert [p.as_list() for p in r] == [[0, 0], [3600, 0], [3600, 2400], [0, 2400]]
    assert g.polygon_signed_area(r) > 0                                                       # counter-clockwise
    c = g.rect_vertices(Pt(1800, 1200), 3600, 2400, anchor_kind="center")
    assert [p.as_list() for p in c] == [[0, 0], [3600, 0], [3600, 2400], [0, 2400]]
    rot = g.rect_vertices(Pt(0, 0), 100, 50, rotation=90)
    assert [p.as_list(4) for p in rot] == [[0, 0], [0, 100], [-50, 100], [-50, 0]]
    with pytest.raises(ValueError):
        g.rect_vertices(Pt(0, 0), 0, 5)


def test_area_perimeter_centroid_bbox():
    r = g.rect_vertices(Pt(0, 0), 3600, 2400)
    assert g.polygon_area(r) == 3600 * 2400
    assert g.path_length(r, closed=True) == 2 * (3600 + 2400)
    assert close(g.polygon_centroid(r), Pt(1800, 1200))
    lo, hi = g.bbox(r)
    assert (lo.x, lo.y, hi.x, hi.y) == (0, 0, 3600, 2400)
    tri = [Pt(0, 0), Pt(6, 0), Pt(0, 6)]
    assert close(g.polygon_centroid(tri), Pt(2, 2))
    line = [Pt(0, 0), Pt(10, 0), Pt(20, 0)]                                                   # degenerate polygon: falls back
    assert close(g.polygon_centroid(line), Pt(10, 0))


def test_point_at_length_walks_the_path():
    path = [Pt(0, 0), Pt(1000, 0), Pt(1000, 1000)]
    assert close(g.point_at_length(path, 500), Pt(500, 0))
    assert close(g.point_at_length(path, 1500), Pt(1000, 500))
    assert close(g.point_at_length(path, 99999), Pt(1000, 1000))
    assert close(g.point_at_length(path, 0), Pt(0, 0))


def test_arc_points_and_wraparound():
    a = g.arc_points(Pt(0, 0), 100, 0, 180)
    assert close(a["start"], Pt(100, 0)) and close(a["end"], Pt(-100, 0)) and close(a["mid"], Pt(0, 100))
    assert a["sweep_deg"] == 180
    w = g.arc_points(Pt(0, 0), 100, 270, 90)                                                  # crosses 0 degrees
    assert w["sweep_deg"] == 180 and close(w["mid"], Pt(100, 0))
    assert g.arc_points(Pt(0, 0), 10, 30, 30)["sweep_deg"] == 360                             # full circle


# ------------------------------------------------------------------------ offsets
def test_offset_of_a_ccw_rectangle_inward_and_outward():
    r = g.rect_vertices(Pt(0, 0), 10, 6)
    inner = g.offset_polyline(r, 1, closed=True)
    assert [p.as_list(6) for p in inner] == [[1, 1], [9, 1], [9, 5], [1, 5]]
    outer = g.offset_polyline(r, -1, closed=True)
    assert [p.as_list(6) for p in outer] == [[-1, -1], [11, -1], [11, 7], [-1, 7]]


def test_offset_open_polyline_with_a_corner_is_mitred():
    line = [Pt(0, 0), Pt(10, 0), Pt(10, 10)]
    left = g.offset_polyline(line, 1)
    assert [p.as_list(6) for p in left] == [[0, 1], [9, 1], [9, 10]]
    right = g.offset_polyline(line, -1)
    assert [p.as_list(6) for p in right] == [[0, -1], [11, -1], [11, 10]]


def test_offset_very_sharp_corner_is_bevelled_not_a_spike():
    spike = [Pt(0, 0), Pt(100, 0), Pt(0, 5)]                                                  # a needle-sharp turn
    out = g.offset_polyline(spike, -1, miter_limit=2.0)
    assert len(out) == 4                                                                      # the middle joint became 2 points
    assert all(abs(p.x) < 200 for p in out)


def test_offset_collinear_points_are_stable():
    out = g.offset_polyline([Pt(0, 0), Pt(5, 0), Pt(10, 0)], 2)
    assert [p.as_list() for p in out] == [[0, 2], [5, 2], [10, 2]]


def test_offset_ignores_duplicate_vertices_and_rejects_degenerate_input():
    out = g.offset_polyline([Pt(0, 0), Pt(0, 0), Pt(10, 0)], 1)
    assert [p.as_list() for p in out] == [[0, 1], [10, 1]]
    with pytest.raises(ValueError):
        g.offset_polyline([Pt(0, 0), Pt(0, 0)], 1)
    with pytest.raises(ValueError):
        g.offset_polyline([Pt(0, 0), Pt(1, 0)], 1, closed=True)


def test_wall_outline_centered_thickness_240():
    w = g.wall_outline([Pt(0, 0), Pt(3600, 0)], 240)
    assert [p.as_list() for p in w["left"]] == [[0, 120], [3600, 120]]
    assert [p.as_list() for p in w["right"]] == [[0, -120], [3600, -120]]
    assert [p.as_list() for p in w["outline"]] == [[0, 120], [3600, 120], [3600, -120], [0, -120]]
    assert g.polygon_area(w["outline"]) == 3600 * 240


def test_wall_outline_justification_and_closed_ring():
    left = g.wall_outline([Pt(0, 0), Pt(1000, 0)], 200, justify="left")
    assert [p.as_list() for p in left["left"]] == [[0, 200], [1000, 200]] and [p.as_list() for p in left["right"]] == [[0, 0], [1000, 0]]
    ring = g.wall_outline(g.rect_vertices(Pt(0, 0), 4000, 3000), 240, closed=True)
    assert ring["outline"] is None
    inner_area = g.polygon_area(ring["left"])                                                  # CCW centre line: left = inward
    outer_area = g.polygon_area(ring["right"])
    assert inner_area == pytest.approx((4000 - 240) * (3000 - 240))
    assert outer_area == pytest.approx((4000 + 240) * (3000 + 240))
    with pytest.raises(ValueError):
        g.wall_outline([Pt(0, 0), Pt(1, 0)], 0)
    with pytest.raises(ValueError):
        g.wall_outline([Pt(0, 0), Pt(1, 0)], 10, justify="up")
