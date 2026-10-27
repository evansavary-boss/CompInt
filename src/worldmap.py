"""World map: TopoJSON country shapes (Natural Earth 1:110m, via world-atlas) to SVG paths.

Shapes are projected once at build time with the Equal Earth projection and keyed by the
ISO 3166 alpha-2 code from config/geo.yaml (`num` links the numeric TopoJSON ids).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

# Equal Earth projection constants (Šavrič, Patterson & Jenny, 2018).
A1, A2, A3, A4 = 1.340264, -0.081106, 0.000893, 0.003796
M = math.sqrt(3) / 2
X_MAX = 2.7064   # projected half-width for lon = ±180°
Y_MAX = 1.3173   # projected half-height for lat = ±90°
SKIP = {"010"}   # Antarctica: large, empty for this use


def equal_earth(lon: float, lat: float) -> tuple[float, float]:
    lam, phi = math.radians(lon), math.radians(lat)
    theta = math.asin(M * math.sin(phi))
    t2 = theta * theta
    t6 = t2 * t2 * t2
    x = 2 * math.sqrt(3) * lam * math.cos(theta) / (3 * (A1 + 3 * A2 * t2 + t6 * (7 * A3 + 9 * A4 * t2)))
    y = theta * (A1 + A2 * t2 + t6 * (A3 + A4 * t2))
    return x, y


def decode_arcs(topo: dict[str, Any]) -> list[list[tuple[float, float]]]:
    """Quantized, delta-encoded arcs to lon/lat coordinates."""
    (sx, sy), (tx, ty) = topo["transform"]["scale"], topo["transform"]["translate"]
    arcs = []
    for arc in topo["arcs"]:
        x = y = 0
        points = []
        for dx, dy in arc:
            x += dx
            y += dy
            points.append((x * sx + tx, y * sy + ty))
        arcs.append(points)
    return arcs


def ring_points(ring: list[int], arcs: list[list[tuple[float, float]]]) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for index in ring:
        arc = arcs[index] if index >= 0 else list(reversed(arcs[~index]))
        points.extend(arc if not points else arc[1:])
    return points


def build_paths(topo_path: Path, countries: dict[str, dict[str, Any]], width: float = 960.0) -> dict[str, Any]:
    """SVG path data per ISO alpha-2 code, plus the viewBox size."""
    topo = json.loads(topo_path.read_text(encoding="utf-8"))
    arcs = decode_arcs(topo)
    num_to_code = {str(c.get("num")).zfill(3): code for code, c in countries.items() if c.get("num")}
    scale = width / (2 * X_MAX)
    height = round(2 * Y_MAX * scale, 1)

    def project(lon: float, lat: float) -> tuple[int, int]:
        x, y = equal_earth(lon, lat)
        return round((x + X_MAX) * scale), round((Y_MAX - y) * scale)

    def ring_path(pts: list[tuple[float, float]]) -> str:
        # Whole pixels are enough at this scale; drop points that round to the same pixel.
        # Rings crossing the antimeridian (Russia, Fiji) are unwrapped so they do not draw a
        # line across the whole map; the overflow past ±180° is clipped by the viewBox.
        out: list[tuple[int, int]] = []
        prev = None
        for lon, lat in pts:
            if prev is not None:
                while lon - prev > 180:
                    lon -= 360
                while lon - prev < -180:
                    lon += 360
            prev = lon
            p = project(lon, lat)
            if not out or p != out[-1]:
                out.append(p)
        if len(out) < 3:
            return ""
        return "M" + "L".join(f"{x},{y}" for x, y in out) + "Z"

    shapes: dict[str, str] = {}
    others: list[str] = []
    for geom in topo["objects"]["countries"]["geometries"]:
        gid = str(geom.get("id", "")).zfill(3) if geom.get("id") else ""
        if gid in SKIP or geom.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        polygons = [geom["arcs"]] if geom["type"] == "Polygon" else geom["arcs"]
        parts = []
        for polygon in polygons:
            for ring in polygon:
                parts.append(ring_path(ring_points(ring, arcs)))
        path = "".join(parts)
        code = num_to_code.get(gid)
        if code:
            shapes[code] = shapes.get(code, "") + path
        else:
            others.append(path)       # territories outside the reference (drawn, never coloured)
    return {"width": width, "height": height, "shapes": shapes, "others": "".join(others)}
