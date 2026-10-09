"""Small geography helpers shared by the scripts: the project boundary, grid squares, Earth Engine.

A boundary is a list of rings, one per polygon in the KML, each a list of (lon, lat) pairs. A
project with a single polygon has one ring.
"""

import math

import json

from config import (BUFFER_KM, EE_PROJECT, KML_LAYER, PROTECTED_GEOJSON, SEARCH_KM,
                    require_boundary)


def kml_layers(kml_file):
    """The layers (folders) of a KML that contain polygons: {layer name: geometry}. Invalid shapes
    (for example self-intersecting rings, which real boundary files often contain) are repaired."""
    import geopandas as gpd
    import pyogrio
    from shapely.ops import unary_union
    from shapely.validation import make_valid
    layers = {}
    for name in [row[0] for row in pyogrio.list_layers(kml_file)]:
        try:
            frame = gpd.read_file(kml_file, layer=name)
        except Exception:                                    # a layer that cannot be read has no boundary
            continue
        polygons = [p for g in frame.geometry if g is not None and not g.is_empty
                    for p in _polygons(make_valid(g))]
        if polygons:
            layers[name] = polygons[0] if len(polygons) == 1 else unary_union(polygons)
    return layers


def _read_geometry(kml_file, layer=None):
    """The boundary in the KML: the named layer, or every layer with polygons together."""
    from shapely.ops import unary_union
    layers = kml_layers(kml_file)
    if not layers:
        raise SystemExit(f'No polygons found in {kml_file}. The boundary has to be a polygon.')
    if layer is not None:
        if layer not in layers:
            raise SystemExit(f"Layer '{layer}' has no polygons in {kml_file}. Layers with polygons: "
                             + ', '.join(layers))
        return layers[layer]
    geoms = list(layers.values())
    return geoms[0] if len(geoms) == 1 else unary_union(geoms)


def area_ha(geometry):
    """Area in hectares of a lon/lat geometry."""
    import geopandas as gpd
    series = gpd.GeoSeries([geometry], crs=4326)
    return float(series.to_crs(series.estimate_utm_crs()).iloc[0].area / 1e4)


def _polygons(geometry):
    """The polygons inside any geometry (a polygon, a multipolygon or a collection)."""
    if geometry.geom_type == 'Polygon':
        return [geometry]
    return [p for part in getattr(geometry, 'geoms', []) for p in _polygons(part)]


MAX_EE_VERTICES = 20_000       # Earth Engine rejects requests above 10 MB; this stays far below that


def simplified_rings(rings, max_vertices=MAX_EE_VERTICES):
    """The same rings with fewer points when there are more than `max_vertices` in total, so the
    boundary can be sent to Earth Engine. The tolerance starts at about 10 m and doubles until the
    boundary is small enough. Small boundaries are returned unchanged."""
    from shapely.geometry import Polygon
    if sum(len(ring) for ring in rings) <= max_vertices:
        return rings
    tolerance = 0.0001
    while True:
        out = []
        for ring in rings:
            shape = Polygon(ring).simplify(tolerance, preserve_topology=True)
            if shape.geom_type == 'Polygon' and not shape.is_empty:
                out.append([(x, y) for x, y in shape.exterior.coords])
        if out and sum(len(ring) for ring in out) <= max_vertices:
            return out
        tolerance *= 2
        if tolerance > 0.5:                                   # a boundary this detailed is not a project area
            raise SystemExit('The boundary is too detailed to send to Earth Engine even when simplified.')


def _exterior_rings(geometry):
    """Outer rings of the polygons in a geometry, as lists of (lon, lat) pairs. Holes are left
    out; for the boundaries used so far they change the area by well under 0.1%."""
    return [[(c[0], c[1]) for c in p.exterior.coords] for p in _polygons(geometry)]


def load_boundary(kml_file=None, layer=None):
    """The project boundary from the KML: a list of rings of (lon, lat) pairs. Without a file it is
    the current project's boundary (and layer)."""
    if kml_file is None:
        kml_file, layer = require_boundary(), KML_LAYER
    return _exterior_rings(_read_geometry(kml_file, layer))


def grown_boundary(km, kml_file=None, layer=None):
    """Outline of the project boundary grown by `km`: a list of rings of (lon, lat) pairs."""
    import geopandas as gpd
    if kml_file is None:
        kml_file, layer = require_boundary(), KML_LAYER
    gdf = gpd.GeoDataFrame(geometry=[_read_geometry(kml_file, layer)], crs=4326)
    metric = gdf.to_crs(gdf.estimate_utm_crs())
    return _exterior_rings(metric.geometry.buffer(km * 1000).to_crs(4326).iloc[0])


RADD_ASSET = 'projects/radar-wur/raddalert/v1'      # RADD forest disturbance alerts
RADD_TILES = ('sa', 'africa', 'asia', 'ca')              # the regional tile sets it comes in


def detect_radd_geography(rings):
    """Which RADD tile set has alerts at the project. Returns (name or None, alert pixels per
    tile set). None means RADD does not cover the place (for example dry woodland)."""
    ee = init_earth_engine()
    xs = [x for ring in rings for x, _ in ring]
    ys = [y for ring in rings for _, y in ring]
    box = ee.Geometry.Rectangle([min(xs), min(ys), max(xs), max(ys)])
    counts = {}
    for name in RADD_TILES:
        images = (ee.ImageCollection(RADD_ASSET).filter(ee.Filter.eq('layer', 'alerts'))
                  .filter(ee.Filter.eq('geography', name)).sort('system:time_end'))
        found = (images.mosaic().select('Alert').gte(3)
                 .reduceRegion(ee.Reducer.sum(), box, 100, maxPixels=1e10, tileScale=8).getInfo())
        counts[name] = int(found.get('Alert') or 0)
    best = max(counts, key=counts.get)
    return (best if counts[best] > 0 else None), counts


def boundary_report(kml_file, cell_km=2.0, layer=None):
    """Facts about a boundary file: area, parts and how well 2 km grid cells cover it. A project made
    of thin fragments (no cell mostly inside it) cannot be matched by this pipeline."""
    import geopandas as gpd
    import numpy as np
    import shapely
    from shapely import box
    geometry = _read_geometry(kml_file, layer)
    metric = gpd.GeoSeries([geometry], crs=4326)
    metric = metric.to_crs(metric.estimate_utm_crs()).iloc[0]
    polygons = _polygons(metric)
    area_ha = metric.area / 1e4
    report_vertices = sum(len(p.exterior.coords) for p in polygons)
    step = cell_km * 1000.0
    if report_vertices > 50_000:                             # a very detailed shape makes the cell check crawl
        metric = metric.simplify(30, preserve_topology=True)
    prepared = shapely.prepared.prep(metric)
    min_x, min_y, max_x, max_y = metric.bounds
    shares = []
    for x in np.arange(np.floor(min_x / step) * step, max_x, step):
        for y in np.arange(np.floor(min_y / step) * step, max_y, step):
            cell = box(x, y, x + step, y + step)
            if prepared.intersects(cell):
                shares.append(cell.intersection(metric).area / cell.area)
    shares = np.array(shares)
    mostly_inside = shares >= 0.9
    return {'area_ha': area_ha, 'polygons': len(polygons),
            'holes': sum(len(p.interiors) for p in polygons),
            'vertices': report_vertices,
            'cells_mostly_inside': int(mostly_inside.sum()),
            'cover_by_those_cells': float(shares[mostly_inside].sum() * cell_km ** 2 * 100 / area_ha)}


def canopy_share(rings, threshold=30):
    """Share of the project's area with tree cover of at least `threshold` % (Hansen, year 2000).
    Open woodland scores low, and little of it then counts as forest for this pipeline."""
    ee = init_earth_engine()
    from parcel_finder import boundary_geometry
    tree = ee.Image('UMD/hansen/global_forest_change_2025_v1_13').select('treecover2000')
    return float(tree.gte(threshold).reduceRegion(ee.Reducer.mean(), boundary_geometry(rings), 60,
                                                  maxPixels=1e10, tileScale=8).getInfo()['treecover2000'])


def polygons_only(geojson):
    """Replace geometry collections by their polygons (the maps cannot label collections)."""
    from shapely.geometry import mapping, shape
    from shapely.ops import unary_union
    kept = []
    for feature in geojson['features']:
        geom = shape(feature['geometry'])
        polys = [p for part in getattr(geom, 'geoms', [geom]) for p in _polygons(part)]
        if polys:
            kept.append({'type': 'Feature', 'properties': feature['properties'],
                         'geometry': mapping(unary_union(polys))})
    return {'type': 'FeatureCollection', 'features': kept}


def load_protected(download=True):
    """Protected areas between the project's leakage buffer and its search limit, as GeoJSON.
    They are read from the project's cache file; if that does not exist they are downloaded from
    Earth Engine once and cached. Returns None when they are not cached and cannot be downloaded
    (no Earth Engine login); an empty collection means there are none in the area."""
    if PROTECTED_GEOJSON.exists():
        return json.loads(PROTECTED_GEOJSON.read_text(encoding='utf-8'))
    if not download:
        return None
    try:
        ee = init_earth_engine(authenticate=False)
    except Exception:
        return None
    from parcel_finder import PROTECTED, boundary_geometry
    project = boundary_geometry(load_boundary())
    region = project.buffer(SEARCH_KM * 1000).difference(project.buffer(BUFFER_KM * 1000))
    areas = ee.FeatureCollection(PROTECTED).filterBounds(region).map(
        lambda f: ee.Feature(f.geometry().simplify(200), {'name': f.get('NAME')}))
    geojson = polygons_only(areas.getInfo())
    PROTECTED_GEOJSON.write_text(json.dumps(geojson), encoding='utf-8')
    return geojson


def square_bounds(lon, lat, km):
    """West, south, east, north of a square of side `km` centred on a point."""
    dlat = km / 2 / 111.0
    dlon = dlat / math.cos(math.radians(lat))
    return lon - dlon, lat - dlat, lon + dlon, lat + dlat


def init_earth_engine(authenticate=True):
    """Start Earth Engine and return the ee module. With authenticate=False a missing login
    raises instead of opening a browser."""
    import ee
    try:
        ee.Initialize(project=EE_PROJECT)
    except Exception:
        if not authenticate:
            raise
        ee.Authenticate()
        ee.Initialize(project=EE_PROJECT)
    return ee
