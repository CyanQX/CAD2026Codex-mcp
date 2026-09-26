"""Precision helpers: exact geometry read-back, point specs ("anchors") and write verification.

Point specs let the model say *where* without doing arithmetic::

    [3600, 0]                                        plain millimetres (units like "3.6m" are fine)
    {"from": [0, 0], "dx": "3.6m", "dy": 900}       relative offset
    {"from": P, "angle_deg": 45, "dist": 300}       polar
    {"mid": [A, B]}                                  midpoint of two specs
    {"handle": "2A3", "snap": "end"}                 a real point on an existing entity (read back from AutoCAD)
    {"name": "west wall", "snap": "mid", "dy": 900} a named entity, then an offset
    {"intersect": ["2A3", "2A4"]}                    where two lines cross
    {"foot": {"point": P, "onto": "2A3"}}            perpendicular foot

Everything is resolved to exact millimetres by the gateway and echoed back.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import geometry as G
from .errors import GatewayError, GeometryUnavailable, InvalidArgument, NeedsClarification
from .geometry import Pt
from .units import fmt, parse_length_mm, parse_number, to_mm, units_per_mm

GRAMMAR = (
    'Point spec: [x,y] | {"from":point, "dx":length, "dy":length} | {"from":point, "angle_deg":angle, "dist":length} | '
    '{"mid":[point,point]} | {"handle":handle-or-alias, "snap":"start|end|mid|center|vertex:N|top|bottom|left|right|centroid"} '
    '| {"intersect":[handle,handle]} | {"foot":{"point":point,"onto":handle}}'
)

_ALLOWED_KEYS = frozenset(
    {"from", "mid", "handle", "ref", "name", "snap", "dx", "dy", "dz", "angle_deg", "angle", "dist",
     "intersect", "infinite", "foot", "point", "onto"}
)


# ---------------------------------------------------------------- entity geometry
@dataclass
class EntityGeometry:
    handle: str
    kind: str  # line | circle | arc | ellipse | polyline | text | point | other
    object_name: str = ""
    layer: str | None = None
    points: dict[str, Pt] = field(default_factory=dict)
    vertices: list[Pt] = field(default_factory=list)
    closed: bool = False
    radius: float | None = None
    length: float | None = None
    area: float | None = None
    text: str | None = None
    height: float | None = None
    from_cache: bool = False

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {"handle": self.handle, "kind": self.kind, "layer": self.layer}
        for key, p in self.points.items():
            out[f"{key}_mm"] = p.as_list()
        if self.vertices:
            out["vertices_mm"] = [v.as_list() for v in self.vertices]
            out["closed"] = self.closed
        for key in ("radius", "length", "area", "height"):
            v = getattr(self, key)
            if v is not None:
                out[f"{key}_mm" if key != "area" else "area_mm2"] = round(v, 6)
        if self.text is not None:
            out["text"] = self.text
        if self.from_cache:
            out["from_cache"] = True
        return out


_KIND = {
    "AcDbLine": "line", "AcDbCircle": "circle", "AcDbArc": "arc", "AcDbEllipse": "ellipse",
    "AcDbPolyline": "polyline", "AcDb2dPolyline": "polyline", "AcDb3dPolyline": "polyline",
    "AcDbText": "text", "AcDbMText": "text", "AcDbPoint": "point",
}


def _preview(value: Any, n: int = 200) -> str:
    return str(value)[:n]


def parse_properties(props: Any, upm: float | None, handle: str = "") -> EntityGeometry:
    """Convert best-cad-mcp's ``get_entity_properties`` payload (drawing units) into millimetres."""

    if not isinstance(props, dict):
        raise GeometryUnavailable("cannot parse the entity properties", details={"payload": _preview(props)})
    obj = props.get("object_name") or props.get("type")
    if not obj:
        raise GeometryUnavailable(
            f"failed to read properties of entity {handle or props.get('handle', '?')}: {_preview(props.get('result') or props.get('message') or props)}",
            details={"payload": _preview(props)},
        )

    def pt(v: Any) -> Pt | None:
        if isinstance(v, (list, tuple)) and len(v) in (2, 3):
            return Pt(*(to_mm(float(c), upm) for c in v))
        return None

    def ln(v: Any) -> float | None:
        return to_mm(float(v), upm) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    kind = _KIND.get(str(obj), "other")
    g = EntityGeometry(handle=str(props.get("handle") or handle), kind=kind, object_name=str(obj), layer=props.get("layer"))
    if kind == "line":
        g.points = {k: p for k, p in (("start", pt(props.get("start_point"))), ("end", pt(props.get("end_point")))) if p}
        if "start" in g.points and "end" in g.points:
            g.points["mid"] = G.midpoint(g.points["start"], g.points["end"])
        g.length = ln(props.get("length"))
    elif kind == "circle":
        c = pt(props.get("center"))
        if c:
            g.points["center"] = c
        g.radius = ln(props.get("radius"))
        a = props.get("area")
        g.area = float(a) / ((upm or 1.0) ** 2) if isinstance(a, (int, float)) else None
    elif kind == "arc":
        c = pt(props.get("center"))
        g.radius = ln(props.get("radius"))
        for key, src in (("start", "start_point"), ("end", "end_point")):
            p = pt(props.get(src))
            if p:
                g.points[key] = p
        if c:
            g.points["center"] = c
            sa, ea = props.get("start_angle"), props.get("end_angle")
            if g.radius and isinstance(sa, (int, float)) and isinstance(ea, (int, float)):
                # best reports arc angles in radians
                sweep = (float(ea) - float(sa)) % (2 * math.pi) or 2 * math.pi
                mid = float(sa) + sweep / 2
                g.points["mid"] = Pt(c.x + g.radius * math.cos(mid), c.y + g.radius * math.sin(mid), c.z)
    elif kind == "ellipse":
        c = pt(props.get("center"))
        if c:
            g.points["center"] = c
    elif kind == "polyline":
        raw = props.get("vertices")
        if isinstance(raw, list):
            g.vertices = [p for p in (pt(v) for v in raw) if p is not None]
        g.closed = bool(props.get("closed"))
        g.length = ln(props.get("length"))
        a = props.get("area")
        g.area = float(a) / ((upm or 1.0) ** 2) if isinstance(a, (int, float)) else None
    elif kind == "text":
        ins = pt(props.get("insertion_point"))
        if ins:
            g.points["insertion"] = ins
        g.text = props.get("text_string")
        g.height = ln(props.get("height"))
    return g


# ------------------------------------------------------------------ snaps / anchors
_SNAP_ALIASES = {
    "start": "start", "begin": "start",
    "end": "end",
    "mid": "mid", "middle": "mid", "midpoint": "mid",
    "center": "center", "centre": "center",
    "centroid": "centroid",
    "top": "top", "bottom": "bottom", "left": "left",
    "right": "right", "insertion": "insertion",
}
_QUADRANT = {"right": 0.0, "top": 90.0, "left": 180.0, "bottom": 270.0}
SNAPS_BY_KIND = {
    "line": "start,end,mid",
    "circle": "center,top,bottom,left,right,quadrant:<deg>",
    "arc": "start,end,mid,center",
    "polyline": "start,end,mid,vertex:N,segment:N,centroid",
    "ellipse": "center",
    "text": "insertion",
    "point": "insertion",
}


def anchor_of(geom: EntityGeometry, snap: str) -> Pt:
    """The exact point ``snap`` names on an entity."""

    raw = str(snap).strip().lower().replace(" ", "")
    name, _, arg = raw.partition(":")
    name = _SNAP_ALIASES.get(name, name)
    allowed = SNAPS_BY_KIND.get(geom.kind, "")

    def bad() -> InvalidArgument:
        return InvalidArgument(
            f"{geom.kind} {geom.handle} does not support the snap point {snap!r}", hint=f"available for this type: {allowed or '(not supported here; use millimetre coordinates instead)'}"
        )

    if geom.kind == "polyline" and geom.vertices:
        vs = geom.vertices
        if name in ("vertex", "v"):
            try:
                return vs[int(arg)]
            except (ValueError, IndexError) as exc:
                raise InvalidArgument(f"invalid vertex index: {arg!r} (there are {len(vs)} vertices; use 0..{len(vs) - 1} or a negative number)") from exc
        if name == "segment":
            try:
                i = int(arg)
                a, b = vs[i], vs[(i + 1) % len(vs)] if geom.closed else vs[i + 1]
                return G.midpoint(a, b)
            except (ValueError, IndexError) as exc:
                raise InvalidArgument(f"invalid segment index: {arg!r}") from exc
        if name == "start":
            return vs[0]
        if name == "end":
            return vs[0] if geom.closed else vs[-1]
        if name == "mid":
            total = G.path_length(vs, geom.closed)
            return G.point_at_length(vs, total / 2, geom.closed)
        if name == "centroid":
            return G.polygon_centroid(vs)
        raise bad()
    if geom.kind in ("line", "arc") and name in geom.points and name in ("start", "end", "mid", "center"):
        return geom.points[name]
    if geom.kind == "circle":
        c, r = geom.points.get("center"), geom.radius
        if c is not None:
            if name == "center":
                return c
            if r is not None and name in _QUADRANT:
                return G.polar(c, _QUADRANT[name], r)
            if r is not None and name == "quadrant":
                try:
                    return G.polar(c, float(arg), r)
                except ValueError as exc:
                    raise InvalidArgument(f"quadrant needs an angle, e.g. quadrant:90 (got {arg!r})") from exc
    if geom.kind in ("ellipse",) and name == "center" and "center" in geom.points:
        return geom.points["center"]
    if geom.kind in ("text", "point") and name == "insertion" and "insertion" in geom.points:
        return geom.points["insertion"]
    raise bad()


class GeometryProvider(Protocol):
    async def entity(self, handle: str, *, fresh: bool = ...) -> EntityGeometry: ...


class RouterGeometry:
    """Reads entity geometry back from AutoCAD (via best's ``get_entity_properties``).

    Always reads fresh (the user may have moved things by hand); the gateway's own record of what it
    drew is only a fallback when the read-back path is unavailable.
    """

    def __init__(self, router: Any, upm: float | None, cache: dict[str, EntityGeometry]) -> None:
        self.router = router
        self.upm = upm
        self.cache = cache

    async def entity(self, handle: str, *, fresh: bool = True) -> EntityGeometry:
        key = str(handle).upper()
        if not fresh and key in self.cache:
            return self.cache[key]
        try:
            out = await self.router.best("get_entity_properties", {"handle": handle})
            if not out.get("ok"):
                raise GeometryUnavailable(f"failed to read properties of entity {handle}: {(out.get('error') or {}).get('message')}")
            geom = parse_properties(out.get("structured"), self.upm, handle)
        except GeometryUnavailable:
            cached = self.cache.get(key)
            if cached is not None:
                cached.from_cache = True
                return cached
            raise
        except GatewayError as exc:
            cached = self.cache.get(key)
            if cached is not None:
                cached.from_cache = True
                return cached
            raise GeometryUnavailable(f"failed to read geometry of entity {handle}: {exc.message}") from exc
        self.cache[key] = geom
        return geom


# --------------------------------------------------------------------- point specs
class PointResolver:
    def __init__(self, provider: GeometryProvider | None, names: dict[str, str] | None = None) -> None:
        self.provider = provider
        self.names = names if names is not None else {}
        self.used_cache = False

    def handle_of(self, ref: Any) -> str:
        text = str(ref)
        return self.names.get(text, text)

    async def _geom(self, ref: Any) -> EntityGeometry:
        if self.provider is None:
            raise GeometryUnavailable("entity geometry is needed but no read channel (best backend) is available")
        geom = await self.provider.entity(self.handle_of(ref))
        self.used_cache = self.used_cache or geom.from_cache
        return geom

    async def resolve(self, spec: Any, name: str = "point") -> Pt:
        if isinstance(spec, Pt):
            return spec
        if isinstance(spec, (list, tuple)):
            if len(spec) not in (2, 3):
                raise InvalidArgument(f"{name} needs [x, y] or [x, y, z] (got {len(spec)} numbers)", hint=GRAMMAR)
            return Pt.of([parse_length_mm(v, f"{name}[{i}]") for i, v in enumerate(spec)])
        if isinstance(spec, dict):
            return await self._from_dict(spec, name)
        raise InvalidArgument(f"{name}: cannot understand {spec!r}", hint=GRAMMAR)

    async def _from_dict(self, spec: dict[str, Any], name: str) -> Pt:
        unknown = sorted(set(spec) - _ALLOWED_KEYS)
        if unknown:
            raise InvalidArgument(f"{name}: unknown key(s) {unknown}", hint=GRAMMAR)
        base: Pt
        if "from" in spec:
            base = await self.resolve(spec["from"], f"{name}.from")
        elif "mid" in spec:
            pair = spec["mid"]
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise InvalidArgument(f"{name}.mid needs an array of two points", hint=GRAMMAR)
            base = G.midpoint(await self.resolve(pair[0], f"{name}.mid[0]"), await self.resolve(pair[1], f"{name}.mid[1]"))
        elif "intersect" in spec:
            pair = spec["intersect"]
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise InvalidArgument(f"{name}.intersect needs two handles", hint=GRAMMAR)
            base = await self._intersection(pair[0], pair[1], bool(spec.get("infinite")), name)
        elif "foot" in spec:
            f = spec["foot"]
            if not isinstance(f, dict) or "point" not in f or "onto" not in f:
                raise InvalidArgument(f'{name}.foot needs {{"point":point, "onto":handle}}', hint=GRAMMAR)
            geom = await self._geom(f["onto"])
            a, b = self._line_ends(geom)
            base = G.project_on_line(await self.resolve(f["point"], f"{name}.foot.point"), a, b)
        elif any(k in spec for k in ("handle", "ref", "name")):
            ref = spec.get("handle") or spec.get("ref")
            if ref is None:
                alias = spec["name"]
                if alias not in self.names:
                    raise NeedsClarification(
                        f"no entity alias named {alias!r}", details={"options": sorted(self.names), "question": f"Which entity is {alias!r}? Give a handle, or name it with cad_name first."}
                    )
                ref = alias
            snap = spec.get("snap")
            geom = await self._geom(ref)
            if not snap:
                raise NeedsClarification(
                    f"a snap is required when referencing {geom.kind} {geom.handle}",
                    details={"options": SNAPS_BY_KIND.get(geom.kind, "").split(","), "question": "Which point of that entity do you want?"},
                )
            base = anchor_of(geom, snap)
        else:
            raise InvalidArgument(f"{name}: point spec is missing from / mid / handle / intersect / foot", hint=GRAMMAR)
        has_offset = any(k in spec for k in ("dx", "dy", "dz"))
        has_polar = "angle_deg" in spec or "angle" in spec or "dist" in spec
        if has_offset and has_polar:
            raise InvalidArgument(f"{name}: dx/dy/dz and angle_deg/dist cannot be combined (nest them under from)")
        if has_offset:
            base = base + Pt(
                parse_length_mm(spec.get("dx", 0), f"{name}.dx"),
                parse_length_mm(spec.get("dy", 0), f"{name}.dy"),
                parse_length_mm(spec.get("dz", 0), f"{name}.dz"),
            )
        if has_polar:
            if "dist" not in spec or ("angle_deg" not in spec and "angle" not in spec):
                raise InvalidArgument(f"{name}: polar offset needs both angle_deg and dist")
            angle = parse_number(spec.get("angle_deg", spec.get("angle")), f"{name}.angle_deg")
            base = G.polar(base, angle, parse_length_mm(spec["dist"], f"{name}.dist"))
        return base

    @staticmethod
    def _line_ends(geom: EntityGeometry) -> tuple[Pt, Pt]:
        if geom.kind == "line" and "start" in geom.points and "end" in geom.points:
            return geom.points["start"], geom.points["end"]
        raise InvalidArgument(f"{geom.kind} {geom.handle} is not a line; this operation supports lines only")

    async def _intersection(self, h1: Any, h2: Any, infinite: bool, name: str) -> Pt:
        a1, a2 = self._line_ends(await self._geom(h1))
        b1, b2 = self._line_ends(await self._geom(h2))
        x = G.segment_intersection(a1, a2, b1, b2, infinite=infinite)
        if x is None:
            raise InvalidArgument(
                f"{name}: the two lines do not intersect (parallel, or the crossing lies outside the segments)", hint='for the extended-line intersection, add "infinite": true'
            )
        return x


# --------------------------------------------------------------- write verification
def _dev(a: Pt, b: Pt) -> float:
    return a.dist2d(b) if abs(a.z) < 1e-9 and abs(b.z) < 1e-9 else a.dist(b)


async def verify_geometry(provider: GeometryProvider | None, handle: str, expected: dict[str, Any], tol_mm: float) -> dict[str, Any]:
    """Read the entity back and compare with ``expected``.  Never raises: ``verified`` is True/False/None."""

    if provider is None:
        return {"verified": None, "reason": "no read-back channel available (best backend not enabled)"}
    try:
        geom = await provider.entity(handle)
    except GatewayError as exc:
        return {"verified": None, "reason": exc.message}
    checks: list[dict[str, Any]] = []

    def point(label: str, exp: Pt, act: Pt | None) -> None:
        if act is None:
            checks.append({"what": label, "expected": exp.as_list(), "actual": None, "ok": False})
            return
        d = _dev(exp, act)
        checks.append({"what": label, "expected": exp.as_list(), "actual": act.as_list(), "deviation_mm": round(d, 6), "ok": d <= tol_mm})

    def scalar(label: str, exp: float, act: float | None) -> None:
        if act is None:
            checks.append({"what": label, "expected": round(exp, 6), "actual": None, "ok": False})
            return
        d = abs(exp - act)
        checks.append({"what": label, "expected": round(exp, 6), "actual": round(act, 6), "deviation_mm": round(d, 6), "ok": d <= tol_mm})

    kind = expected.get("kind")
    if geom.kind != kind:
        checks.append({"what": "kind", "expected": kind, "actual": geom.kind, "ok": False})
    elif kind == "line":
        point("start", expected["start"], geom.points.get("start"))
        point("end", expected["end"], geom.points.get("end"))
    elif kind == "circle":
        point("center", expected["center"], geom.points.get("center"))
        scalar("radius", expected["radius"], geom.radius)
    elif kind == "arc":
        for key in ("center", "start", "end"):
            point(key, expected[key], geom.points.get(key))
        scalar("radius", expected["radius"], geom.radius)
    elif kind == "polyline":
        exp_vs: list[Pt] = expected["vertices"]
        if len(geom.vertices) != len(exp_vs):
            checks.append({"what": "vertex_count", "expected": len(exp_vs), "actual": len(geom.vertices), "ok": False})
        else:
            for i, (e, a) in enumerate(zip(exp_vs, geom.vertices, strict=True)):  # equal length checked just above
                point(f"vertex[{i}]", e, a)
        if "closed" in expected:
            checks.append({"what": "closed", "expected": expected["closed"], "actual": geom.closed, "ok": expected["closed"] == geom.closed})
    elif kind == "text":
        checks.append({"what": "text", "expected": expected["text"], "actual": geom.text, "ok": expected["text"] == geom.text})
        if expected.get("height") is not None:
            scalar("height", expected["height"], geom.height)
        if expected.get("insertion") is not None:
            point("insertion", expected["insertion"], geom.points.get("insertion"))
    if expected.get("layer") and geom.layer is not None:
        checks.append({"what": "layer", "expected": expected["layer"], "actual": geom.layer, "ok": str(expected["layer"]).lower() == str(geom.layer).lower()})
    devs = [c["deviation_mm"] for c in checks if "deviation_mm" in c]
    result: dict[str, Any] = {
        "verified": all(c["ok"] for c in checks) if checks else None,
        "max_deviation_mm": max(devs) if devs else 0.0,
        "tolerance_mm": tol_mm,
        "checks": checks,
    }
    if geom.from_cache:
        result["note"] = "read-back channel unavailable; used the gateway cache (the entity in AutoCAD was not actually verified)"
        result["verified"] = None
    return result


def describe_check_failures(report: dict[str, Any]) -> str:
    bad = [c for c in report.get("checks", []) if not c.get("ok")]
    return "; ".join(
        f"{c['what']} expected {c['expected']} actual {c['actual']}" + (f" (deviation {fmt(c['deviation_mm'])} mm)" if "deviation_mm" in c else "")
        for c in bad
    )


def unit_warnings(insunits: Any) -> list[str]:
    """Human-readable notes about the drawing's INSUNITS setting (empty for a plain millimetre drawing)."""

    upm = units_per_mm(insunits) if isinstance(insunits, int) else None
    if upm is None:
        return ["drawing units are not set (INSUNITS=0): the gateway treats 1 drawing unit as 1 millimetre - confirm that this is what you want."]
    if abs(upm - 1.0) > 1e-9:
        return [f"drawing units are not millimetres (INSUNITS={insunits}): the gateway will convert millimetres into drawing units automatically."]
    return []


async def load_units(router: Any) -> tuple[float | None, dict[str, Any] | None, list[str]]:
    """(drawing units per mm | None if unitless, drawing identity, warnings) from the active drawing."""

    from .router import drawing_identity  # local import: router imports this module's siblings

    out = await router.slacker("get_active_drawing_info", {})
    if not out.get("ok"):
        err = out.get("error") or {}
        raise GatewayError(err.get("message", "cannot read the active drawing info"), code=err.get("code", "BACKEND_REPORTED_FAILURE"), hint=err.get("hint"))
    ident = drawing_identity(out.get("structured"))
    insunits = ident.get("insunits") if ident else None
    upm = units_per_mm(insunits) if isinstance(insunits, int) else None
    return upm, ident, unit_warnings(insunits)
