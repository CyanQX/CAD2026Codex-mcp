import math

import pytest
from helpers import FakeBackends, make_config, run

from cad_super_mcp import geometry as G
from cad_super_mcp.errors import GeometryUnavailable, InvalidArgument, NeedsClarification
from cad_super_mcp.geometry import Pt
from cad_super_mcp.precision import (
    EntityGeometry,
    PointResolver,
    RouterGeometry,
    anchor_of,
    load_units,
    parse_properties,
    verify_geometry,
)
from cad_super_mcp.router import Router
from cad_super_mcp.units import units_per_mm

INCH = units_per_mm(1)          # drawing units per mm for an inch drawing


LINE_PROPS = {"handle": "2A3", "object_name": "AcDbLine", "layer": "A-WALL",
              "start_point": [0, 0, 0], "end_point": [3600, 0, 0], "length": 3600}
CIRCLE_PROPS = {"handle": "2A4", "object_name": "AcDbCircle", "layer": "A-FURN", "center": [1500, 1800, 0], "radius": 600.0, "area": math.pi * 600**2}
ARC_PROPS = {"handle": "2A5", "object_name": "AcDbArc", "layer": "0", "center": [0, 0, 0], "radius": 100.0,
             "start_point": [100, 0, 0], "end_point": [-100, 0, 0], "start_angle": 0.0, "end_angle": math.pi}
POLY_PROPS = {"handle": "2A6", "object_name": "AcDbPolyline", "layer": "A-ROOM", "closed": True, "length": 12000,
              "vertices": [[0, 0, 0], [3600, 0, 0], [3600, 2400, 0], [0, 2400, 0]], "area": 3600 * 2400}
TEXT_PROPS = {"handle": "2A7", "object_name": "AcDbText", "layer": "A-TEXT", "text_string": "EXIT", "height": 250.0,
              "insertion_point": [100, 200, 0]}


class FakeProvider:
    def __init__(self, geoms=None, fail=None):
        self.geoms = {g.handle.upper(): g for g in (geoms or [])}
        self.fail = fail
        self.reads = []

    async def entity(self, handle, *, fresh=True):
        self.reads.append(handle)
        if self.fail:
            raise self.fail
        try:
            return self.geoms[str(handle).upper()]
        except KeyError:
            raise GeometryUnavailable(f"no such entity {handle}") from None


def geoms():
    return [parse_properties(p, None) for p in (LINE_PROPS, CIRCLE_PROPS, ARC_PROPS, POLY_PROPS, TEXT_PROPS)]


# ---------------------------------------------------------------- parsing
def test_parse_line_circle_arc_polyline_text_in_millimetres():
    line, circle, arc, poly, text = geoms()
    assert line.kind == "line" and line.points["mid"].as_list() == [1800, 0] and line.length == 3600
    assert circle.radius == 600 and circle.points["center"].as_list() == [1500, 1800]
    assert arc.points["mid"].as_list(4) == [0, 100]                     # mid of a 0..180 degree arc
    assert poly.closed and len(poly.vertices) == 4 and poly.area == 3600 * 2400
    assert text.text == "EXIT" and text.height == 250 and text.points["insertion"].as_list() == [100, 200]


def test_parse_converts_drawing_units_to_millimetres():
    props = {"object_name": "AcDbLine", "handle": "1", "layer": "0", "start_point": [0, 0, 0], "end_point": [36, 0, 0], "length": 36}
    g = parse_properties(props, INCH)                                    # an INCH drawing: 36 in = 914.4 mm
    assert g.points["end"].x == pytest.approx(914.4) and g.length == pytest.approx(914.4)
    circle = parse_properties({"object_name": "AcDbCircle", "handle": "2", "center": [1, 1, 0], "radius": 2.0, "area": math.pi * 4}, INCH)
    assert circle.radius == pytest.approx(50.8) and circle.area == pytest.approx(math.pi * 50.8**2)


def test_parse_reports_backend_failure_text_instead_of_crashing():
    with pytest.raises(GeometryUnavailable) as ei:
        parse_properties({"result": "get properties failed: entity not found: ZZ"}, None, "ZZ")
    assert "entity not found" in ei.value.message
    with pytest.raises(GeometryUnavailable):
        parse_properties("not a dict", None)


# ---------------------------------------------------------------- anchors
def test_anchors_per_entity_kind():
    line, circle, arc, poly, text = geoms()
    assert anchor_of(line, "start").as_list() == [0, 0] and anchor_of(line, "END").as_list() == [3600, 0]
    assert anchor_of(line, "mid").as_list() == [1800, 0]
    assert anchor_of(circle, "center").as_list() == [1500, 1800]
    assert anchor_of(circle, "top").as_list(4) == [1500, 2400] and anchor_of(circle, "left").as_list(4) == [900, 1800]
    assert anchor_of(circle, "quadrant:270").as_list(4) == [1500, 1200]
    assert anchor_of(arc, "start").as_list() == [100, 0] and anchor_of(arc, "center").as_list() == [0, 0]
    assert anchor_of(poly, "vertex:2").as_list() == [3600, 2400] and anchor_of(poly, "vertex:-1").as_list() == [0, 2400]
    assert anchor_of(poly, "segment:0").as_list() == [1800, 0] and anchor_of(poly, "segment:3").as_list() == [0, 1200]   # wraps (closed)
    assert anchor_of(poly, "centroid").as_list() == [1800, 1200]
    assert anchor_of(poly, "end").as_list() == [0, 0]                       # closed: the end is the start
    assert anchor_of(poly, "mid").as_list() == [3600, 2400]                 # half of the 12000 mm perimeter = a vertex
    assert anchor_of(text, "insertion").as_list() == [100, 200]


def test_unsupported_or_out_of_range_snaps_explain_what_is_available():
    line, circle, _arc, poly, text = geoms()
    with pytest.raises(InvalidArgument) as ei:
        anchor_of(line, "center")
    assert "start,end,mid" in ei.value.hint
    with pytest.raises(InvalidArgument):
        anchor_of(poly, "vertex:9")
    with pytest.raises(InvalidArgument):
        anchor_of(circle, "quadrant:abc")
    with pytest.raises(InvalidArgument):
        anchor_of(text, "mid")


# ---------------------------------------------------------------- resolver
def resolver(names=None, provider=None):
    return PointResolver(provider or FakeProvider(geoms()), names)


def test_plain_and_unit_points():
    r = resolver()
    assert run(r.resolve([0, 0])).as_list() == [0, 0]
    assert run(r.resolve(["3.6m", "900mm"])).as_list() == [3600, 900]
    assert run(r.resolve([1, 2, 3])).as_list() == [1, 2, 3]
    with pytest.raises(InvalidArgument):
        run(r.resolve([1]))
    with pytest.raises(InvalidArgument):
        run(r.resolve("nope"))


def test_relative_polar_and_midpoint_specs():
    r = resolver()
    assert run(r.resolve({"from": [100, 100], "dx": "3.6m", "dy": 900})).as_list() == [3700, 1000]
    assert run(r.resolve({"from": [0, 0], "angle_deg": 90, "dist": "0.9m"})).as_list(4) == [0, 900]
    assert run(r.resolve({"mid": [[0, 0], {"from": [0, 0], "dx": 400}]})).as_list() == [200, 0]
    nested = {"from": {"from": [0, 0], "dx": 1000}, "dy": 500}
    assert run(r.resolve(nested)).as_list() == [1000, 500]
    with pytest.raises(InvalidArgument):
        run(r.resolve({"from": [0, 0], "dx": 1, "angle_deg": 0, "dist": 1}))            # ambiguous combination
    with pytest.raises(InvalidArgument):
        run(r.resolve({"from": [0, 0], "angle_deg": 30}))                               # polar needs both


def test_handle_snap_specs_read_real_geometry_and_accept_offsets():
    r = resolver()
    assert run(r.resolve({"handle": "2A3", "snap": "mid"})).as_list() == [1800, 0]
    # "0.9 m above the middle of the wall line", the sentence the model no longer has to compute:
    assert run(r.resolve({"handle": "2A3", "snap": "mid", "dy": 900})).as_list() == [1800, 900]
    assert run(r.resolve({"handle": "2a4", "snap": "top"})).as_list(4) == [1500, 2400]      # handles are case-insensitive


def test_named_entities_resolve_through_the_alias_registry():
    r = resolver(names={"west wall": "2A3"})
    assert run(r.resolve({"name": "west wall", "snap": "end"})).as_list() == [3600, 0]
    assert run(r.resolve({"handle": "west wall", "snap": "start"})).as_list() == [0, 0]          # alias accepted in `handle` too
    with pytest.raises(NeedsClarification) as ei:
        run(r.resolve({"name": "east wall", "snap": "end"}))
    assert "west wall" in ei.value.details["options"]


def test_snap_is_required_and_the_error_lists_the_options():
    r = resolver()
    with pytest.raises(NeedsClarification) as ei:
        run(r.resolve({"handle": "2A3"}))
    assert set(ei.value.details["options"]) == {"start", "end", "mid"} and ei.value.hint


def test_intersection_and_perpendicular_foot():
    horiz = EntityGeometry("H", "line", points={"start": Pt(0, 0), "end": Pt(1000, 0)})
    vert = EntityGeometry("V", "line", points={"start": Pt(400, -500), "end": Pt(400, 500)})
    short = EntityGeometry("S", "line", points={"start": Pt(2000, -500), "end": Pt(2000, -100)})
    r = PointResolver(FakeProvider([horiz, vert, short, *geoms()]))
    assert run(r.resolve({"intersect": ["H", "V"]})).as_list() == [400, 0]
    with pytest.raises(InvalidArgument) as ei:
        run(r.resolve({"intersect": ["H", "S"]}))
    assert "infinite" in ei.value.hint
    assert run(r.resolve({"intersect": ["H", "S"], "infinite": True})).as_list() == [2000, 0]
    assert run(r.resolve({"foot": {"point": [250, 900], "onto": "H"}})).as_list() == [250, 0]
    with pytest.raises(InvalidArgument):
        run(r.resolve({"intersect": ["H", "2A4"]}))                                        # a circle is not a line


def test_typos_and_missing_provider_fail_loudly():
    r = resolver()
    with pytest.raises(InvalidArgument) as ei:
        run(r.resolve({"from": [0, 0], "dist_mm": 5}))                                     # a typo must not be silently ignored
    assert "dist_mm" in ei.value.message
    with pytest.raises(InvalidArgument):
        run(r.resolve({"snap": "end"}))
    with pytest.raises(GeometryUnavailable):
        run(PointResolver(None).resolve({"handle": "2A3", "snap": "end"}))
    assert run(PointResolver(None).resolve([1, 2])).as_list() == [1, 2]                    # plain points never need a provider


# ---------------------------------------------------------------- verification
def test_verify_passes_within_tolerance_and_reports_deviation():
    prov = FakeProvider(geoms())
    ok = run(verify_geometry(prov, "2A3", {"kind": "line", "start": Pt(0, 0), "end": Pt(3600, 0), "layer": "A-WALL"}, 0.01))
    assert ok["verified"] is True and ok["max_deviation_mm"] == 0
    bad = run(verify_geometry(prov, "2A3", {"kind": "line", "start": Pt(0, 0), "end": Pt(3600.5, 0)}, 0.01))
    assert bad["verified"] is False and bad["max_deviation_mm"] == pytest.approx(0.5)
    assert [c["what"] for c in bad["checks"] if not c["ok"]] == ["end"]


def test_verify_each_kind():
    prov = FakeProvider(geoms())
    assert run(verify_geometry(prov, "2A4", {"kind": "circle", "center": Pt(1500, 1800), "radius": 600}, 0.01))["verified"]
    assert not run(verify_geometry(prov, "2A4", {"kind": "circle", "center": Pt(1500, 1800), "radius": 601}, 0.01))["verified"]
    arc = G.arc_points(Pt(0, 0), 100, 0, 180)
    assert run(verify_geometry(prov, "2A5", {"kind": "arc", "center": Pt(0, 0), "radius": 100, "start": arc["start"], "end": arc["end"]}, 0.01))["verified"]
    poly = {"kind": "polyline", "vertices": G.rect_vertices(Pt(0, 0), 3600, 2400), "closed": True}
    assert run(verify_geometry(prov, "2A6", poly, 0.01))["verified"]
    assert not run(verify_geometry(prov, "2A6", {**poly, "closed": False}, 0.01))["verified"]
    assert not run(verify_geometry(prov, "2A6", {**poly, "vertices": poly["vertices"][:3]}, 0.01))["verified"]
    assert run(verify_geometry(prov, "2A7", {"kind": "text", "text": "EXIT", "height": 250, "layer": "a-text"}, 0.01))["verified"]
    assert not run(verify_geometry(prov, "2A7", {"kind": "text", "text": "EXlT", "height": 250}, 0.01))["verified"]
    assert not run(verify_geometry(prov, "2A3", {"kind": "circle", "center": Pt(0, 0), "radius": 1}, 0.01))["verified"]   # wrong kind


def test_verify_never_raises_when_readback_is_unavailable():
    r = run(verify_geometry(FakeProvider(fail=GeometryUnavailable("best is down")), "2A3", {"kind": "line", "start": Pt(0, 0), "end": Pt(1, 0)}, 0.01))
    assert r["verified"] is None and "best is down" in r["reason"]
    assert run(verify_geometry(None, "2A3", {"kind": "line"}, 0.01))["verified"] is None


# ---------------------------------------------------------------- router-backed provider
def test_router_geometry_reads_best_and_converts_units(tmp_path):
    async def go():
        props = {"handle": "2A3", "object_name": "AcDbLine", "layer": "0", "start_point": [0, 0, 0], "end_point": [36, 0, 0], "length": 36}
        fake = FakeBackends(script={"best.get_entity_properties": lambda a: props})
        router = Router(make_config(tmp_path), backends=fake)
        prov = RouterGeometry(router, INCH, {})
        g1 = await prov.entity("2A3")
        assert g1.points["end"].x == pytest.approx(914.4)
        await prov.entity("2A3")
        assert [c[1] for c in fake.calls] == ["get_entity_properties", "get_entity_properties"]       # always read fresh

    run(go())


def test_router_geometry_falls_back_to_the_cache_and_says_so(tmp_path):
    async def go():
        fake = FakeBackends(script={"best.get_entity_properties": {"result": "get properties failed: entity not found"}})
        router = Router(make_config(tmp_path), backends=fake)
        cached = parse_properties(LINE_PROPS, None)
        prov = RouterGeometry(router, None, {"2A3": cached})
        g = await prov.entity("2A3")
        assert g.from_cache and g.points["end"].x == 3600
        report = await verify_geometry(prov, "2A3", {"kind": "line", "start": Pt(0, 0), "end": Pt(3600, 0)}, 0.01)
        assert report["verified"] is None and "cache" in report["note"]                              # a cache hit is not verification
        with pytest.raises(GeometryUnavailable):
            await RouterGeometry(router, None, {}).entity("nope")

    run(go())


def test_load_units_reports_unit_warnings(tmp_path):
    async def go():
        from helpers import FakeAutoCAD

        cad = FakeAutoCAD()
        router = Router(make_config(tmp_path), backends=FakeBackends(cad=cad))
        upm, ident, warnings = await load_units(router)
        assert upm == 1.0 and ident["name"] == "test.dwg" and warnings == []
        cad.drawing["insunits"] = 1
        upm, _, warnings = await load_units(router)
        assert upm == pytest.approx(1 / 25.4) and "not millimetres" in warnings[0]
        cad.drawing["insunits"] = 0
        upm, _, warnings = await load_units(router)
        assert upm is None and "not set" in warnings[0]
        cad.running = False
        with pytest.raises(Exception) as ei:
            await load_units(router)
        assert "AUTOCAD_NOT_RUNNING" in str(ei.value)

    run(go())
