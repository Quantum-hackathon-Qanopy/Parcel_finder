"""Small geography helpers shared by the scripts: the project boundary, grid squares, Earth Engine.

A boundary is a list of rings, one per polygon in the KML, each a list of (lon, lat) pairs. A
project with a single polygon has one ring.
"""

import math

from config import EE_PROJECT, require_boundary


def _read_geometry(kml_file):
    """All features of the KML as one geometry. Invalid shapes (for example self-intersecting
    rings, which real boundary files often contain) are repaired first."""
    import geopandas as gpd
    from shapely.ops import unary_union
    from shapely.validation import make_valid
    geoms = [make_valid(g) for g in gpd.read_file(kml_file, driver='KML').geometry
             if g is not None and not g.is_empty]
    return geoms[0] if len(geoms) == 1 else unary_union(geoms)


def _polygons(geometry):
    """The polygons inside any geometry (a polygon, a multipolygon or a collection)."""
    if geometry.geom_type == 'Polygon':
        return [geometry]
    return [p for part in getattr(geometry, 'geoms', []) for p in _polygons(part)]


def _exterior_rings(geometry):
    """Outer rings of the polygons in a geometry, as lists of (lon, lat) pairs. Holes are left
    out; for the boundaries used so far they change the area by well under 0.1%."""
    return [[(c[0], c[1]) for c in p.exterior.coords] for p in _polygons(geometry)]


def load_boundary(kml_file=None):
    """The project boundary from the KML: a list of rings of (lon, lat) pairs."""
    return _exterior_rings(_read_geometry(kml_file or require_boundary()))


def grown_boundary(km, kml_file=None):
    """Outline of the project boundary grown by `km`: a list of rings of (lon, lat) pairs."""
    import geopandas as gpd
    gdf = gpd.GeoDataFrame(geometry=[_read_geometry(kml_file or require_boundary())], crs=4326)
    metric = gdf.to_crs(gdf.estimate_utm_crs())
    return _exterior_rings(metric.geometry.buffer(km * 1000).to_crs(4326).iloc[0])


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
