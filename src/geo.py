"""Small geography helpers shared by the scripts: the project boundary, grid squares, Earth Engine."""

import math

from config import EE_PROJECT, KML_FILE


def load_boundary(kml_file=KML_FILE):
    """Outer boundary of the first polygon in the KML, as (lon, lat) pairs."""
    import geopandas as gpd
    ring = gpd.read_file(kml_file, driver='KML').geometry.iloc[0].exterior.coords
    return [(c[0], c[1]) for c in ring]


def grown_boundary(km, kml_file=KML_FILE):
    """Outline of the project boundary grown by `km`, as (lon, lat) pairs."""
    import geopandas as gpd
    gdf = gpd.read_file(kml_file, driver='KML').iloc[:1]
    metric = gdf.to_crs(gdf.estimate_utm_crs())
    ring = metric.geometry.buffer(km * 1000).to_crs(4326).iloc[0].exterior.coords
    return [(c[0], c[1]) for c in ring]


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
