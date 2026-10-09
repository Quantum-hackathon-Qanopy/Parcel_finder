"""Find comparison parcels for a project.

Everything used for matching is measured BEFORE the project start year, so what
happens to the project afterwards can never influence which parcels are chosen.

How it works
------------
1. Cut the project and the land around it into square grid cells (2 km by default).
2. Describe every cell with the features below.
3. Score each candidate cell by how closely it resembles the project's own cells:
   the average distance to its `n_nearest` closest project cells, so the river-side
   and inland parts of a large project can both find analogues. Each feature gap is
   divided by that feature's spread across the candidates, so no hand-picked
   tolerances are needed. similarity = 1 / (1 + distance).
4. Keep a well-spaced set that gives every part of the project a close analogue
   (greedy "coverage"), then fill any places left over with the best-scoring cells.

Candidates never include the project, a leakage buffer around it, or protected areas.

Features (averaged per grid cell):
    forest_start  share of the cell that was forest at the project start
    loss_old      share lost earlier than `trend_years` before the start
    loss_recent   share lost in the `trend_years` just before the start
    elevation     metres
    slope         degrees
    dist_edge     distance to the nearest non-forest pixel at the start (m)
    dist_water    distance to the nearest permanent water (m)
    dist_road     distance to the nearest road (m)
"""

import ee
import numpy as np

HANSEN = 'UMD/hansen/global_forest_change_2025_v1_13'
LAST_LOSS_CODE = 25         # newest year in the Hansen loss layer (25 means 2025)
SRTM = 'USGS/SRTMGL1_003'
WATER = 'JRC/GSW1_4/GlobalSurfaceWater'
PROTECTED = 'WCMC/WDPA/current/polygons'
ROADS = 'projects/sat-io/open-datasets/GRIP4/Central-South-America'

MAX_DIST_M = 50000          # distance features are capped here
DT_NEIGHBORHOOD_PX = 256    # neighbourhood of the distance transform, in pixels
MAX_FETCH = 5000            # most cells Earth Engine lets us download in one call
MIN_INSIDE_SHARE = 0.9      # a project cell must be at least this much inside the project


# ------------------------------------------------------------ pure numpy helpers
def feature_spread(matrix):
    """Standard deviation of every feature (never zero)."""
    sd = matrix.std(axis=0)
    sd[sd <= 0] = 1.0
    return sd


def feature_distances(candidates, project_cells, spread):
    """Distance between every candidate and every project cell: the root mean square
    of the feature gaps, each gap measured in units of the feature's spread.
    Returns an array of shape (candidates, project cells)."""
    a, b = candidates / spread, project_cells / spread
    squared = (a ** 2).sum(1)[:, None] + (b ** 2).sum(1)[None, :] - 2 * a @ b.T
    return np.sqrt(np.maximum(squared, 0) / a.shape[1])


def nearest_distance(distances, n):
    """Average distance from each candidate to its `n` closest project cells."""
    n = min(n, distances.shape[1])
    return np.partition(distances, n - 1, axis=1)[:, :n].mean(axis=1)


def km_to_all(lon, lat, lons, lats):
    """Great-circle distance in km from one point to arrays of points."""
    p1, p2 = np.radians(lat), np.radians(lats)
    a = (np.sin((p2 - p1) / 2) ** 2
         + np.cos(p1) * np.cos(p2) * np.sin(np.radians(lons - lon) / 2) ** 2)
    return 6371.0 * 2 * np.arcsin(np.sqrt(a))


def select_by_coverage(similarity, lons, lats, n, min_spacing_km, fallback):
    """Pick up to `n` candidates, no two closer than `min_spacing_km`.

    Greedy coverage: each pick is the candidate that most improves the best
    available analogue of the project's cells (`similarity` has one row per
    candidate and one column per project cell). Once no candidate improves any
    project cell, the remaining places go to the best-scoring allowed candidates
    (`fallback` holds one score per candidate).
    Returns (row numbers of the picks, how many were picked for coverage)."""
    best = np.zeros(similarity.shape[1])
    allowed = np.ones(len(similarity), bool)
    chosen, n_coverage = [], 0
    for _ in range(n):
        gain = np.maximum(similarity - best, 0).sum(axis=1)
        gain[~allowed] = -1.0
        i = int(np.argmax(gain))
        if gain[i] > 1e-9:
            n_coverage += 1
        else:
            score = np.where(allowed, fallback, -1.0)
            i = int(np.argmax(score))
            if score[i] < 0:
                break           # nothing allowed is left
        chosen.append(i)
        best = np.maximum(best, similarity[i])
        allowed &= km_to_all(lons[i], lats[i], lons, lats) >= min_spacing_km
        allowed[i] = False
    return chosen, n_coverage


def yearly_loss_rate(loss, forest, years):
    """Forest loss in percent of forest per year (nan if there is no forest)."""
    return (loss or 0) / forest / years * 100 if forest else float('nan')


def boundary_geometry(coordinates):
    """Earth Engine geometry for one ring of (lon, lat) pairs or for a list of rings. A very
    detailed boundary is simplified first (Earth Engine limits the size of a request)."""
    if isinstance(coordinates[0][0], (int, float)):                # a single ring
        return ee.Geometry.Polygon(coordinates)
    from geo import simplified_rings
    coordinates = simplified_rings(coordinates)
    if len(coordinates) == 1:
        return ee.Geometry.Polygon(coordinates[0])
    return ee.Geometry.MultiPolygon([[ring] for ring in coordinates])


# ------------------------------------------------------------------ main class
class FindParcels:
    def __init__(self, coordinates, start_year, buffer_m=10000, search_radius_m=50000,
                 cell_size_m=2000, forest_threshold=30, min_forest_share=0.5,
                 trend_years=5, min_spacing_km=6.0, n_nearest=3):
        """
        coordinates       project boundary: one ring of (lon, lat) pairs, or a list of rings
                          (one per polygon) for a boundary made of several parts
        start_year        project start year; only earlier information is used
        buffer_m          leakage buffer: no candidate within this distance of the project
        search_radius_m   candidates are searched up to this distance from the project
        cell_size_m       side length of a grid cell (2000 m = 400 ha)
        forest_threshold  canopy cover (%) that counts as forest
        min_forest_share  cells with less forest than this are dropped
        trend_years       length of the "recent loss" window before the start
        min_spacing_km    minimum distance between candidate centres (0 turns it off)
        n_nearest         number of closest project cells a candidate is compared with
        """
        if start_year - trend_years < 2002:
            raise ValueError('start_year is too early for the pre-start trend window.')
        self.start_year = start_year
        self.cell_size_m = cell_size_m
        self.forest_threshold = forest_threshold
        self.min_forest_share = min_forest_share
        self.trend_years = trend_years
        self.min_spacing_km = min_spacing_km
        self.n_nearest = n_nearest

        self.geometry = boundary_geometry(coordinates)
        self.exclusion_zone = self.geometry.buffer(buffer_m)
        self.search_region = self.geometry.buffer(search_radius_m).difference(self.exclusion_zone)

    # ----------------------------------------------------------------- features
    def _build_features(self):
        """Stack the matching features into one image (pre-start information only)."""
        hansen = ee.Image(HANSEN)
        loss_year = hansen.select('lossyear')            # 1 means 2001, 2 means 2002, ...
        last_pre = self.start_year - 2001                # last loss year before the start
        recent_from = last_pre - self.trend_years + 1

        lost_before = loss_year.gt(0).And(loss_year.lte(last_pre))
        lost_recent = loss_year.gte(recent_from).And(loss_year.lte(last_pre))
        lost_old = lost_before.And(lost_recent.Not())

        canopy = hansen.select('treecover2000').where(lost_before, 0)
        forest = canopy.gte(self.forest_threshold)
        pixel_m = ee.Image.pixelArea().sqrt()

        def distance_to(mask):
            """Distance in metres to the nearest pixel where `mask` is 1."""
            d = mask.fastDistanceTransform(DT_NEIGHBORHOOD_PX).sqrt().multiply(pixel_m)
            return d.min(MAX_DIST_M)

        water = ee.Image(WATER).select('occurrence').gt(50).unmask(0)
        roads = ee.FeatureCollection(ROADS).filterBounds(self.search_region.buffer(60000))
        elevation = ee.Image(SRTM).select('elevation')

        return ee.Image.cat([
            forest.unmask(0).rename('forest_start'),
            lost_old.unmask(0).rename('loss_old'),
            lost_recent.unmask(0).rename('loss_recent'),
            elevation.rename('elevation'),
            ee.Terrain.slope(elevation).rename('slope'),
            distance_to(forest.Not().unmask(1)).rename('dist_edge'),
            distance_to(water).rename('dist_water'),
            roads.distance(MAX_DIST_M, 100).unmask(MAX_DIST_M).rename('dist_road'),
        ])

    def _cell_features(self, grid):
        """Average every feature inside each grid cell; keep complete, forested cells."""
        cells = self.features.reduceRegions(
            collection=grid, reducer=ee.Reducer.mean(), scale=30, tileScale=4)
        return (cells
                .filter(ee.Filter.notNull(self.band_names))
                .filter(ee.Filter.gte('forest_start', self.min_forest_share)))

    def _download_cells(self):
        """Download the candidate cells and the project's own cells."""
        proj = ee.Projection('EPSG:4326')

        # Candidates: cells in the search ring that touch neither the leakage buffer
        # nor a protected area.
        self.protected = ee.FeatureCollection(PROTECTED).filterBounds(self.search_region)
        grid = (self.search_region.coveringGrid(proj=proj, scale=self.cell_size_m)
                .filter(ee.Filter.bounds(self.exclusion_zone).Not())
                .filter(ee.Filter.bounds(self.protected).Not()))

        def add_center(cell):
            c = cell.geometry().centroid(1).coordinates()
            return cell.set({'lon': c.get(0), 'lat': c.get(1)})

        candidates = self._cell_features(grid).map(add_center).limit(MAX_FETCH)

        # Project cells: same grid, only cells (almost) fully inside the project.
        def inside_share(cell):
            part = cell.geometry().intersection(self.geometry, 1)
            return cell.set('inside', part.area(1).divide(cell.geometry().area(1)))

        project = self._cell_features(
            self.geometry.coveringGrid(proj=proj, scale=self.cell_size_m)
            .map(inside_share)
            .filter(ee.Filter.gte('inside', MIN_INSIDE_SHARE))).map(add_center)

        print('  downloading candidate cells...')
        candidates = candidates.getInfo()['features']
        print('  downloading project cells...')
        project = project.getInfo()['features']

        if len(candidates) >= MAX_FETCH:
            raise RuntimeError(
                f'More than {MAX_FETCH} eligible cells. Use a larger cell_size_m or a '
                'smaller search radius so all of them can be downloaded.')
        if not project:
            raise RuntimeError('No forested project cells found. Check the boundary and cell size.')
        return candidates, project

    # --------------------------------------------------------------------- fit
    def fit(self, n_candidates=200):
        """Choose up to `n_candidates` comparison cells. Results:
        self.candidates     chosen cells (GeoJSON features), best similarity first
        self.project_cells  feature matrix of the project's own cells
        self.project_lonlat centre (lon, lat) of each of the project's own cells
        self.band_names     feature names (columns of the matrices)
        self.n_eligible     number of eligible candidate cells before selection
        """
        self.features = self._build_features()
        self.band_names = self.features.bandNames().getInfo()
        candidates, project = self._download_cells()
        self.n_eligible = len(candidates)
        self.project_lonlat = np.array(
            [[c['properties']['lon'], c['properties']['lat']] for c in project])

        def matrix(cells):
            return np.array([[c['properties'][b] for b in self.band_names] for c in cells], float)

        cand_matrix = matrix(candidates)
        self.project_cells = matrix(project)
        self.spread = feature_spread(cand_matrix)

        distances = feature_distances(cand_matrix, self.project_cells, self.spread)
        similarity = 1.0 / (1.0 + nearest_distance(distances, self.n_nearest))
        for cell, s in zip(candidates, similarity):
            cell['properties']['similarity'] = float(s)

        lons = np.array([c['properties']['lon'] for c in candidates])
        lats = np.array([c['properties']['lat'] for c in candidates])
        rows, self.n_coverage_picks = select_by_coverage(
            1.0 / (1.0 + distances), lons, lats, n_candidates, self.min_spacing_km, similarity)
        self.candidates = sorted((candidates[i] for i in rows),
                                 key=lambda c: -c['properties']['similarity'])

    # ------------------------------------------------------------- diagnostics
    def project_means(self):
        """Average of every feature over the project's own cells."""
        return dict(zip(self.band_names, self.project_cells.mean(axis=0).tolist()))

    def spreads(self):
        """Spread (standard deviation) of every feature across the candidate cells."""
        return dict(zip(self.band_names, self.spread.tolist()))

    def coverage(self, close=0.5):
        """Share of the project's cells with a close analogue (within `close` spread
        units) among the chosen candidates."""
        chosen = np.array([[c['properties'][b] for b in self.band_names]
                           for c in self.candidates], float)
        nearest = feature_distances(chosen, self.project_cells, self.spread).min(axis=0)
        return float((nearest <= close).mean())

    def ring_loss_report(self, edges_km=(0, 2.5, 5, 10, 15, 20, 30, 50)):
        """Forest loss per year in rings around the project, before and after the
        start year (protected areas left out). Loss that jumps right next to the
        project after the start would point to leakage. Meant for choosing the
        buffer; the post-start years are never used for matching."""
        start_code = self.start_year - 2000
        if start_code > LAST_LOSS_CODE:
            raise ValueError('No post-start years in the forest-loss data.')
        n_pre, n_post = self.trend_years, LAST_LOSS_CODE - start_code + 1
        pre_from = start_code - n_pre

        hansen = ee.Image(HANSEN)
        # Unmask: pixels without loss have no value (not 0) and would silently
        # drop out of the And() calls below.
        ly = hansen.select('lossyear').unmask(0)
        tree = hansen.select('treecover2000').unmask(0).gte(self.forest_threshold)
        forest_pre = tree.And(ly.gt(0).And(ly.lt(pre_from)).Not())
        forest_post = tree.And(ly.gt(0).And(ly.lt(start_code)).Not())
        loss_pre = forest_pre.And(ly.gte(pre_from)).And(ly.lt(start_code))
        loss_post = forest_post.And(ly.gte(start_code)).And(ly.lte(LAST_LOSS_CODE))

        outer = self.geometry.buffer(max(edges_km) * 1000)
        unprotected = ee.Image(0).paint(
            ee.FeatureCollection(PROTECTED).filterBounds(outer), 1).Not()
        counts = ee.Image.cat([
            forest_pre.rename('pre_forest'), loss_pre.rename('pre_loss'),
            forest_post.rename('post_forest'), loss_post.rename('post_loss'),
        ]).updateMask(unprotected)

        rings = []
        for a, b in zip(edges_km[:-1], edges_km[1:]):
            inner = self.geometry if a == 0 else self.geometry.buffer(a * 1000)
            rings.append(ee.Feature(self.geometry.buffer(b * 1000).difference(inner),
                                    {'inner_km': a, 'outer_km': b}))
        sums = counts.reduceRegions(
            collection=ee.FeatureCollection(rings), reducer=ee.Reducer.sum(),
            scale=60, tileScale=4).getInfo()['features']

        rows = []
        for f in sums:
            p = f['properties']
            pre = yearly_loss_rate(p.get('pre_loss'), p.get('pre_forest'), n_pre)
            post = yearly_loss_rate(p.get('post_loss'), p.get('post_forest'), n_post)
            rows.append({'inner_km': p['inner_km'], 'outer_km': p['outer_km'],
                         'before': pre, 'after': post,
                         'ratio': post / pre if pre > 0 else float('nan')})
        return rows