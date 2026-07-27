#!/usr/bin/env python3
"""
trace_map.py — trace all your GPX tracks on a minimalist map.

Recursively scans a folder for .gpx files, draws every track/route as a thin
line on a clean basemap, and writes a single self-contained HTML file.

Usage:
    python trace_map.py ~/gpx
    python trace_map.py ~/gpx -o carte.html --color "#1f3b57" --weight 2 --opacity 0.5
    python trace_map.py ~/gpx --basemap voyager --every 3

Deps: pip install gpxpy folium
"""

from __future__ import annotations

import argparse
import colorsys
import math
import sys
from pathlib import Path

import folium
import gpxpy

# Basemaps. Each is {tiles, attr}; attr=None means folium supplies it (built-ins).
# Topographic ones (opentopo, esri-topo, esri-relief) show relief + rivers.
BASEMAPS = {
    "positron": {"tiles": "CartoDB positron", "attr": None},   # light grey minimalist
    "voyager": {"tiles": "CartoDB voyager", "attr": None},     # light with more labels
    "dark": {"tiles": "CartoDB dark_matter", "attr": None},    # dark
    "osm": {"tiles": "OpenStreetMap", "attr": None},           # standard OSM
    "opentopo": {                                              # topo: contours, hillshade, rivers
        "tiles": "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
        "attr": "© OpenStreetMap contributors, SRTM | style: © OpenTopoMap (CC-BY-SA)",
    },
    "esri-topo": {                                             # classic Esri topographic
        "tiles": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
        "attr": "Tiles © Esri — Esri, DeLorme, NAVTEQ, and contributors",
    },
    "esri-relief": {                                           # shaded relief only (low detail, great overview)
        "tiles": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Shaded_Relief/MapServer/tile/{z}/{y}/{x}",
        "attr": "Tiles © Esri — Source: Esri",
    },
    "cyclosm": {                                               # cycling-oriented OSM with relief
        "tiles": "https://{s}.tile-cyclosm.openstreetmap.fr/cyclosm/{z}/{x}/{y}.png",
        "attr": "© OpenStreetMap contributors | style: CyclOSM (CC-BY-SA)",
    },
    # --- label-free variants (no city/country names) ---
    "positron-nolabels": {
        "tiles": "https://{s}.basemaps.cartocdn.com/light_nolabels/{z}/{x}/{y}.png",
        "attr": "© OpenStreetMap contributors © CARTO",
    },
    "voyager-nolabels": {
        "tiles": "https://{s}.basemaps.cartocdn.com/rastertiles/voyager_nolabels/{z}/{x}/{y}.png",
        "attr": "© OpenStreetMap contributors © CARTO",
    },
    "dark-nolabels": {
        "tiles": "https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}.png",
        "attr": "© OpenStreetMap contributors © CARTO",
    },
    "terrain-bg": {                                            # Stamen terrain, hillshade only, no labels
        "tiles": "https://tiles.stadiamaps.com/tiles/stamen_terrain_background/{z}/{x}/{y}.png",
        "attr": "© Stadia Maps © Stamen Design © OpenMapTiles © OpenStreetMap contributors",
        "stadia": True,
        "max_zoom": 18,
    },
    "watercolor": {                                            # parchment / painted (Stamen, open CC-BY)
        "tiles": "https://tiles.stadiamaps.com/tiles/stamen_watercolor/{z}/{x}/{y}.jpg",
        "attr": "© Stadia Maps © Stamen Design © OpenStreetMap contributors",
        "stadia": True,
        "max_zoom": 16,   # watercolor tileset thins out above z16
    },
    "terrain": {                                               # classic warm hillshaded terrain (Stamen, open)
        "tiles": "https://tiles.stadiamaps.com/tiles/stamen_terrain/{z}/{x}/{y}.png",
        "attr": "© Stadia Maps © Stamen Design © OpenMapTiles © OpenStreetMap contributors",
        "stadia": True,
        "max_zoom": 18,
    },
    "modern-antique": {                                        # Esri Modern Antique (proprietary, needs key)
        "tiles": "https://static-map-tiles-api.arcgis.com/arcgis/rest/services/"
                 "static-basemap-tiles-service/v1/arcgis/modern-antique/static/tile/{z}/{y}/{x}",
        "attr": "Esri, TomTom, Garmin, FAO, NOAA, USGS, © OpenStreetMap contributors",
        "arcgis": True,
        "max_zoom": 18,
    },
    "midcentury": {                                            # Esri Midcentury (proprietary, needs key)
        "tiles": "https://static-map-tiles-api.arcgis.com/arcgis/rest/services/"
                 "static-basemap-tiles-service/v1/arcgis/midcentury/static/tile/{z}/{y}/{x}",
        "attr": "Esri, TomTom, Garmin, FAO, NOAA, USGS, © OpenStreetMap contributors",
        "arcgis": True,
        "max_zoom": 18,
    },
}


def distinct_color(i: int) -> str:
    """A well-spread, pleasant color for index i, via golden-angle hue rotation.

    Consecutive indices land far apart on the hue wheel, so neighbouring tracks
    rarely share a similar tint even across hundreds of files.
    """
    hue = (i * 137.508 % 360) / 360.0
    r, g, b = colorsys.hls_to_rgb(hue, 0.5, 0.65)  # fixed lightness/saturation for harmony
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


def find_gpx(root: Path) -> list[Path]:
    """Recursively collect .gpx files (case-insensitive), sorted, deduped."""
    files = {p for p in root.rglob("*") if p.suffix.lower() == ".gpx"}
    return sorted(files)


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in km between two (lat, lon) points."""
    r = 6371.0
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def split_on_gaps(
    pts: list[tuple[float, float]], max_gap_km: float
) -> tuple[list[list[tuple[float, float]]], int]:
    """Break a point list wherever the jump to the next point exceeds max_gap_km.

    Returns (sub_lines, n_cuts). A cut drops the aberrant straight connector
    (train ride, GPS drop-out, forgotten pause) instead of drawing it.
    max_gap_km <= 0 disables splitting.
    """
    if max_gap_km <= 0 or len(pts) < 2:
        return ([pts] if len(pts) >= 2 else []), 0

    sub: list[list[tuple[float, float]]] = []
    current = [pts[0]]
    cuts = 0
    for prev, p in zip(pts, pts[1:]):
        if haversine_km(prev, p) > max_gap_km:
            if len(current) >= 2:
                sub.append(current)
            current = [p]
            cuts += 1
        else:
            current.append(p)
    if len(current) >= 2:
        sub.append(current)
    return sub, cuts


def extract_lines(
    gpx_path: Path, every: int = 1, max_gap_km: float = 0.0
) -> tuple[list[list[tuple[float, float]]], int]:
    """Return (polylines, n_cuts) from a GPX file.

    Handles track segments and routes. Points can be decimated with `every`
    (keep 1 point out of N). Segments are split wherever consecutive points are
    more than max_gap_km apart, so straight-line artifacts get dropped.
    """
    try:
        with gpx_path.open("r", encoding="utf-8", errors="replace") as fh:
            gpx = gpxpy.parse(fh)
    except Exception as exc:  # noqa: BLE001 — skip anything unparseable
        print(f"  ! skipped {gpx_path}: {exc}", file=sys.stderr)
        return [], 0

    lines: list[list[tuple[float, float]]] = []
    total_cuts = 0

    point_lists: list[list[tuple[float, float]]] = []
    for track in gpx.tracks:
        for seg in track.segments:
            point_lists.append([(p.latitude, p.longitude) for p in seg.points[::every]])
    for route in gpx.routes:
        point_lists.append([(p.latitude, p.longitude) for p in route.points[::every]])

    for pts in point_lists:
        subs, cuts = split_on_gaps(pts, max_gap_km)
        lines.extend(subs)
        total_cuts += cuts

    return lines, total_cuts


def build_map(
    root: Path,
    output: Path,
    color: str,
    weight: float,
    opacity: float,
    basemap: str,
    every: int,
    max_gap_km: float,
    stadia_key: str | None = None,
    arcgis_key: str | None = None,
    multicolor: bool = False,
) -> None:
    files = find_gpx(root)
    if not files:
        sys.exit(f"No .gpx files found under {root}")

    print(f"Found {len(files)} GPX file(s) under {root}")

    # Each entry is (label, line, color); label is the file's path relative to
    # root, shown on hover, e.g. "./rides/2024/tour.gpx". In multicolor mode
    # each file gets its own hue; otherwise every track uses `color`.
    tracks: list[tuple[str, list[tuple[float, float]], str]] = []
    total_cuts = 0
    for i, f in enumerate(files):
        label = "./" + f.relative_to(root).as_posix()
        line_color = distinct_color(i) if multicolor else color
        lines, cuts = extract_lines(f, every=every, max_gap_km=max_gap_km)
        total_cuts += cuts
        for line in lines:
            tracks.append((label, line, line_color))

    if not tracks:
        sys.exit("No usable tracks found (files had no track/route points).")

    n_pts = sum(len(line) for _, line, _ in tracks)
    print(f"Drawing {len(tracks)} tracks, {n_pts} points total")
    if max_gap_km > 0:
        print(f"Split {total_cuts} gap(s) > {max_gap_km} km (train/GPS-drop artifacts removed)")

    bm = BASEMAPS[basemap]
    tiles = bm["tiles"]
    if bm.get("stadia") and stadia_key:
        tiles += f"?api_key={stadia_key}"

    if bm.get("arcgis"):
        # Esri Static Basemap Tiles: 512px PNG, token as query param, standard XYZ.
        if not arcgis_key:
            sys.exit("The '" + basemap + "' basemap needs an ArcGIS Location Platform key: "
                     "pass --arcgis-key YOUR_KEY (free non-commercial account at "
                     "location.arcgis.com).")
        tiles += f"?token={arcgis_key}"
        fmap = folium.Map(tiles=None, control_scale=True)
        folium.TileLayer(
            tiles=tiles,
            attr=bm["attr"],
            name=basemap,
            max_zoom=bm.get("max_zoom", 19),
            tile_size=512,   # Esri static tiles are 512x512...
            zoom_offset=-1,  # ...so shift zoom by one to line up with Leaflet
        ).add_to(fmap)
    elif bm["attr"]:
        fmap = folium.Map(tiles=tiles, attr=bm["attr"],
                          max_zoom=bm.get("max_zoom", 19), control_scale=True)
    else:
        fmap = folium.Map(tiles=tiles, control_scale=True)

    # Low opacity means overlapping routes naturally darken — a free density hint.
    for label, line, line_color in tracks:
        folium.PolyLine(
            line,
            color=line_color,
            weight=weight,
            opacity=opacity,
            smooth_factor=1.5,
            tooltip=folium.Tooltip(label, sticky=True),
        ).add_to(fmap)

    # Fit the view to everything we drew.
    lats = [lat for _, line, _ in tracks for lat, _ in line]
    lons = [lon for _, line, _ in tracks for _, lon in line]
    fmap.fit_bounds([[min(lats), min(lons)], [max(lats), max(lons)]])

    fmap.save(str(output))
    print(f"Wrote {output}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", type=Path, help="folder to scan recursively for .gpx files")
    ap.add_argument("-o", "--output", type=Path, default=Path("carte.html"), help="output HTML file (default: carte.html)")
    ap.add_argument("--color", default="#1f3b57", help="line color (default: #1f3b57, a dark slate blue)")
    ap.add_argument("--weight", type=float, default=2.0, help="line thickness (default: 2)")
    ap.add_argument("--opacity", type=float, default=0.55, help="line opacity 0-1 (default: 0.55)")
    ap.add_argument("--basemap", choices=BASEMAPS.keys(), default="positron", help="basemap style (default: positron)")
    ap.add_argument("--every", type=int, default=1, help="keep 1 point out of N to lighten output (default: 1)")
    ap.add_argument("--max-gap", type=float, default=1.0,
                    help="cut a track wherever two consecutive points are more than this many km apart, "
                         "dropping straight-line artifacts (train, GPS loss). 0 disables. (default: 1.0)")
    ap.add_argument("--stadia-key", default=None,
                    help="Stadia Maps API key, only needed for the 'watercolor'/'terrain' basemaps when "
                         "opening the HTML as a local file. Not needed if served from localhost.")
    ap.add_argument("--multicolor", action="store_true",
                    help="give each GPX file its own color (spread across the hue wheel) "
                         "instead of a single color; overrides --color")
    ap.add_argument("--arcgis-key", default=None,
                    help="ArcGIS Location Platform API key, required for the 'modern-antique'/'midcentury' "
                         "basemaps (free non-commercial account at location.arcgis.com).")
    args = ap.parse_args()

    if not args.folder.is_dir():
        sys.exit(f"Not a directory: {args.folder}")

    build_map(
        root=args.folder,
        output=args.output,
        color=args.color,
        weight=args.weight,
        opacity=args.opacity,
        basemap=args.basemap,
        every=max(1, args.every),
        max_gap_km=args.max_gap,
        stadia_key=args.stadia_key,
        arcgis_key=args.arcgis_key,
        multicolor=args.multicolor,
    )


if __name__ == "__main__":
    main()
