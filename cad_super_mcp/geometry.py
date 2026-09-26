"""Pure 2D/3D geometry kernel (no I/O, no AutoCAD).

The point of this module: the *model never does coordinate arithmetic*.  Everything that is
"3600 to the right of that wall", "the middle of that line", "a 240 mm thick wall around this
centre line" is computed here, deterministically, and echoed back so a human can check it.
All lengths are millimetres, all angles degrees, counter-clockwise from +X.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

EPS = 1e-9


@dataclass(frozen=True, slots=True)
class Pt:
    x: float
    y: float
    z: float = 0.0

    @staticmethod
    def of(seq: Sequence[float]) -> Pt:
        if len(seq) not in (2, 3):
            raise ValueError("a point needs 2 or 3 numbers")
        return Pt(float(seq[0]), float(seq[1]), float(seq[2]) if len(seq) == 3 else 0.0)

    def __add__(self, o: Pt) -> Pt:
        return Pt(self.x + o.x, self.y + o.y, self.z + o.z)

    def __sub__(self, o: Pt) -> Pt:
        return Pt(self.x - o.x, self.y - o.y, self.z - o.z)

    def scaled(self, k: float) -> Pt:
        return Pt(self.x * k, self.y * k, self.z * k)

    def dist(self, o: Pt) -> float:
        return math.dist((self.x, self.y, self.z), (o.x, o.y, o.z))

    def dist2d(self, o: Pt) -> float:
        return math.hypot(self.x - o.x, self.y - o.y)

    def rounded(self, digits: int = 6) -> Pt:
        def r(v: float) -> float:
            v = round(v, digits)
            return 0.0 if v == 0 else v

        return Pt(r(self.x), r(self.y), r(self.z))

    def as_list(self, digits: int = 6, dims: int | None = None) -> list[float]:
        """[x, y] (or [x, y, z] when z is non-zero or dims=3), rounded."""

        p = self.rounded(digits)
        if dims == 3 or (dims is None and abs(p.z) > EPS):
            return [p.x, p.y, p.z]
        return [p.x, p.y]

    def __iter__(self):  # convenient unpacking: x, y, z = pt
        yield self.x
        yield self.y
        yield self.z


def distance(a: Pt, b: Pt) -> float:
    return a.dist(b)


def midpoint(a: Pt, b: Pt) -> Pt:
    return Pt((a.x + b.x) / 2, (a.y + b.y) / 2, (a.z + b.z) / 2)


def lerp(a: Pt, b: Pt, t: float) -> Pt:
    return Pt(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t, a.z + (b.z - a.z) * t)


def normalize_deg(angle: float) -> float:
    """Wrap to [0, 360)."""

    a = math.fmod(angle, 360.0)
    return a + 360.0 if a < 0 else a


def angle_deg(a: Pt, b: Pt) -> float:
    """Direction of a->b in degrees, (-180, 180]."""

    return math.degrees(math.atan2(b.y - a.y, b.x - a.x))


def polar(origin: Pt, angle: float, dist: float) -> Pt:
    r = math.radians(angle)
    return Pt(origin.x + dist * math.cos(r), origin.y + dist * math.sin(r), origin.z)


def rotate_about(p: Pt, center: Pt, angle: float) -> Pt:
    r = math.radians(angle)
    c, s = math.cos(r), math.sin(r)
    dx, dy = p.x - center.x, p.y - center.y
    return Pt(center.x + dx * c - dy * s, center.y + dx * s + dy * c, p.z)


def project_on_line(p: Pt, a: Pt, b: Pt) -> Pt:
    """Foot of the perpendicular from p onto the infinite line ab."""

    dx, dy = b.x - a.x, b.y - a.y
    d2 = dx * dx + dy * dy
    if d2 < EPS:
        raise ValueError("cannot project onto a zero-length line")
    t = ((p.x - a.x) * dx + (p.y - a.y) * dy) / d2
    return Pt(a.x + t * dx, a.y + t * dy, a.z)


def segment_intersection(p1: Pt, p2: Pt, p3: Pt, p4: Pt, *, infinite: bool = False) -> Pt | None:
    """Intersection of p1p2 with p3p4 (segments, or infinite lines); None if parallel/outside."""

    d1x, d1y = p2.x - p1.x, p2.y - p1.y
    d2x, d2y = p4.x - p3.x, p4.y - p3.y
    denom = d1x * d2y - d1y * d2x
    if abs(denom) < EPS:
        return None
    t = ((p3.x - p1.x) * d2y - (p3.y - p1.y) * d2x) / denom
    u = ((p3.x - p1.x) * d1y - (p3.y - p1.y) * d1x) / denom
    if not infinite and (t < -EPS or t > 1 + EPS or u < -EPS or u > 1 + EPS):
        return None
    return Pt(p1.x + t * d1x, p1.y + t * d1y, p1.z)


def divide_segment(a: Pt, b: Pt, n: int) -> list[Pt]:
    """The n-1 interior points that split a->b into n equal parts."""

    if n < 2:
        raise ValueError("n must be >= 2")
    return [lerp(a, b, i / n) for i in range(1, n)]


# ------------------------------------------------------------------------ shapes
def rect_vertices(anchor: Pt, width: float, height: float, *, anchor_kind: str = "corner", rotation: float = 0.0) -> list[Pt]:
    """Four vertices, counter-clockwise, starting at the lower-left corner of the (rotated) rectangle.

    anchor_kind='corner': ``anchor`` is the lower-left corner; 'center': the centre.
    The rectangle is rotated about ``anchor``.
    """

    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")
    if anchor_kind == "center":
        ox, oy = -width / 2, -height / 2
    elif anchor_kind == "corner":
        ox, oy = 0.0, 0.0
    else:
        raise ValueError("anchor_kind must be 'corner' or 'center'")
    local = [(ox, oy), (ox + width, oy), (ox + width, oy + height), (ox, oy + height)]
    out = []
    for lx, ly in local:
        out.append(rotate_about(Pt(anchor.x + lx, anchor.y + ly, anchor.z), anchor, rotation) if rotation else Pt(anchor.x + lx, anchor.y + ly, anchor.z))
    return out


def polygon_signed_area(vs: Sequence[Pt]) -> float:
    """Shoelace area; positive for counter-clockwise."""

    s = 0.0
    n = len(vs)
    for i in range(n):
        a, b = vs[i], vs[(i + 1) % n]
        s += a.x * b.y - b.x * a.y
    return s / 2


def polygon_area(vs: Sequence[Pt]) -> float:
    return abs(polygon_signed_area(vs))


def path_length(vs: Sequence[Pt], closed: bool = False) -> float:
    n = len(vs)
    total = sum(vs[i].dist(vs[i + 1]) for i in range(n - 1))
    if closed and n > 2:
        total += vs[-1].dist(vs[0])
    return total


def polygon_centroid(vs: Sequence[Pt]) -> Pt:
    area2 = polygon_signed_area(vs) * 2
    n = len(vs)
    if abs(area2) < EPS:
        return Pt(sum(v.x for v in vs) / n, sum(v.y for v in vs) / n, sum(v.z for v in vs) / n)
    cx = cy = 0.0
    for i in range(n):
        a, b = vs[i], vs[(i + 1) % n]
        cross = a.x * b.y - b.x * a.y
        cx += (a.x + b.x) * cross
        cy += (a.y + b.y) * cross
    return Pt(cx / (3 * area2), cy / (3 * area2), vs[0].z)


def bbox(vs: Iterable[Pt]) -> tuple[Pt, Pt]:
    pts = list(vs)
    return (
        Pt(min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)),
        Pt(max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)),
    )


def point_at_length(vs: Sequence[Pt], target: float, closed: bool = False) -> Pt:
    """The point ``target`` mm along the path (clamped to its ends)."""

    pts = list(vs) + ([vs[0]] if closed and len(vs) > 2 else [])
    if target <= 0:
        return pts[0]
    walked = 0.0
    for a, b in zip(pts, pts[1:], strict=False):  # consecutive pairs: the second list is intentionally one shorter
        seg = a.dist(b)
        if walked + seg >= target - EPS and seg > EPS:
            return lerp(a, b, (target - walked) / seg)
        walked += seg
    return pts[-1]


def arc_points(center: Pt, radius: float, start_deg: float, end_deg: float) -> dict[str, Pt | float]:
    """Start/end/mid points and sweep of a counter-clockwise arc from start_deg to end_deg."""

    sweep = normalize_deg(end_deg - start_deg)
    if sweep < EPS:
        sweep = 360.0
    return {
        "start": polar(center, start_deg, radius),
        "end": polar(center, start_deg + sweep, radius),
        "mid": polar(center, start_deg + sweep / 2, radius),
        "sweep_deg": sweep,
    }


# ----------------------------------------------------------------------- offsets
def _dedupe(vs: Sequence[Pt], closed: bool) -> list[Pt]:
    out: list[Pt] = []
    for p in vs:
        if not out or p.dist(out[-1]) > EPS:
            out.append(p)
    if closed and len(out) > 1 and out[0].dist(out[-1]) <= EPS:
        out.pop()
    return out


def offset_polyline(vs: Sequence[Pt], distance: float, *, closed: bool = False, miter_limit: float = 4.0) -> list[Pt]:
    """Parallel copy of a polyline at ``distance`` to its LEFT (negative = right), with mitred joints.

    For a counter-clockwise closed polygon a positive distance moves inward.  Corners sharper
    than ``miter_limit`` * |distance| are bevelled instead of producing a long spike.
    """

    pts = _dedupe(vs, closed)
    n = len(pts)
    if n < 2 or (closed and n < 3):
        raise ValueError("not enough distinct vertices to offset")
    segs = n if closed else n - 1
    normals: list[tuple[float, float]] = []
    for i in range(segs):
        a, b = pts[i], pts[(i + 1) % n]
        dx, dy = b.x - a.x, b.y - a.y
        length = math.hypot(dx, dy)
        normals.append((-dy / length, dx / length))
    d = distance

    def joint(v: Pt, n1: tuple[float, float], n2: tuple[float, float]) -> list[Pt]:
        dot = n1[0] * n2[0] + n1[1] * n2[1]
        denom = 1 + dot
        bevel = [Pt(v.x + d * n1[0], v.y + d * n1[1], v.z), Pt(v.x + d * n2[0], v.y + d * n2[1], v.z)]
        if denom < 1e-9:
            return bevel
        mx, my = d * (n1[0] + n2[0]) / denom, d * (n1[1] + n2[1]) / denom
        if math.hypot(mx, my) > miter_limit * abs(d):
            return bevel
        return [Pt(v.x + mx, v.y + my, v.z)]

    out: list[Pt] = []
    if closed:
        for i in range(n):
            out.extend(joint(pts[i], normals[(i - 1) % n], normals[i]))
    else:
        out.append(Pt(pts[0].x + d * normals[0][0], pts[0].y + d * normals[0][1], pts[0].z))
        for i in range(1, n - 1):
            out.extend(joint(pts[i], normals[i - 1], normals[i]))
        out.append(Pt(pts[-1].x + d * normals[-1][0], pts[-1].y + d * normals[-1][1], pts[-1].z))
    return out


def wall_outline(centerline: Sequence[Pt], thickness: float, *, closed: bool = False, justify: str = "center") -> dict:
    """A wall of ``thickness`` around/beside ``centerline``.

    justify: 'center' (default), 'left' or 'right' - which side of the drawn line the wall body
    extends to.  Returns the two edge polylines and, for an open centre line, the closed outline.
    """

    if thickness <= 0:
        raise ValueError("thickness must be positive")
    if justify == "center":
        left_d, right_d = thickness / 2, -thickness / 2
    elif justify == "left":
        left_d, right_d = thickness, 0.0
    elif justify == "right":
        left_d, right_d = 0.0, -thickness
    else:
        raise ValueError("justify must be 'center', 'left' or 'right'")
    base = _dedupe(centerline, closed)
    left = offset_polyline(base, left_d, closed=closed) if left_d else list(base)
    right = offset_polyline(base, right_d, closed=closed) if right_d else list(base)
    outline = None if closed else left + list(reversed(right))
    return {"left": left, "right": right, "closed": closed, "outline": outline}
