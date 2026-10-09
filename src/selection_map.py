"""One map of the whole workflow, with a button per stage.

    1 Project              boundary and project cells
    2 Forest data          tree cover and forest loss (needs Earth Engine)
    3 Leakage buffer       ring around the project where no parcel may lie
    4 Protected areas      reserves where no parcel may lie (needs Earth Engine once)
    5 Search region        the ring candidates are drawn from, minus the exclusions
    6 Candidates           every candidate parcel, coloured by similarity
    7 Pool                 the most similar candidates handed to the selection step
    8 QAOA test problem    a 16-parcel version, solved exactly and with QAOA
    9 Selected parcels     the K parcels picked from the pool
   10 Matches              each picked parcel joined to the project cell it resembles most

Run select_parcels.py first (it writes selected_parcels.csv and qaoa_result.json), then:

    python src/selection_map.py

The map opens in your browser and is saved as outputs/selection_map.html. Most of it comes
from the CSV files. Forest layers and protected areas come from Earth Engine; the protected
areas are cached in outputs/protected_areas.geojson, so Earth Engine is needed only once.
"""

import csv
import json
import webbrowser

import folium
import numpy as np
from matplotlib import colormaps, colors

from config import (BUFFER_KM, CELL_KM, KML_FILE, OUTPUT_DIR, QAOA_JSON, SEARCH_KM,
                    SELECTED_CSV)
from geo import grown_boundary, init_earth_engine, load_boundary, square_bounds
from select_parcels import POOL_SIZE, best_matches, load_pool

MAP_HTML = OUTPUT_DIR / 'selection_map.html'
PROTECTED_GEOJSON = OUTPUT_DIR / 'protected_areas.geojson'
SATELLITE = ('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/'
             'MapServer/tile/{z}/{y}/{x}')

# Filled in with the map's variable names; shows one stage's layers at a time. Runs on
# page load because folium defines the map and layers further down the page.
STAGE_SCRIPT = """
window.addEventListener('load', function () {
  var map = %(map)s;
  var groups = %(groups)s;
  var all = %(all)s;
  var stages = %(stages)s;
  function show(n) {
    all.forEach(function (l) { if (map.hasLayer(l)) map.removeLayer(l); });
    groups[n].forEach(function (l) { l.addTo(map); });
    document.querySelectorAll('.stage-btn').forEach(function (b, i) {
      b.classList.toggle('on', i === n);
    });
    document.getElementById('stage-text').innerHTML = '<b>' + (n + 1) + '. ' +
      stages[n][0] + '</b><br>' + stages[n][1];
  }
  var Panel = L.Control.extend({
    options: {position: 'topleft'},
    onAdd: function () {
      var div = L.DomUtil.create('div', 'stage-panel');
      L.DomEvent.disableClickPropagation(div);
      var row = '';
      stages.forEach(function (s, i) {
        row += '<button class="stage-btn" data-i="' + i + '">' + (i + 1) + '. ' + s[0] + '</button>';
      });
      div.innerHTML = '<div class="stage-row">' + row + '</div><div id="stage-text"></div>';
      div.querySelectorAll('button').forEach(function (b) {
        b.onclick = function () { show(parseInt(b.dataset.i)); };
      });
      return div;
    }
  });
  new Panel().addTo(map);
  show(0);
});
"""

STAGE_CSS = """
<style>
.stage-panel {background:white;padding:8px 10px;border-radius:6px;max-width:560px;
  font:13px sans-serif;box-shadow:0 1px 5px rgba(0,0,0,.4)}
.stage-row {display:flex;flex-wrap:wrap;gap:4px;margin-bottom:6px}
.stage-btn {border:1px solid #888;background:#f4f4f4;border-radius:4px;padding:4px 8px;
  cursor:pointer;font-size:12px}
.stage-btn.on {background:#1f6feb;color:white;border-color:#1f6feb}
#stage-text {line-height:1.35}
</style>
"""


def square(lon, lat, km=CELL_KM):
    """Corners (lat, lon) of a square of side `km` centred on a point."""
    west, south, east, north = square_bounds(lon, lat, km)
    return [(south, west), (south, east), (north, east), (north, west)]


def project_ring():
    """Project boundary as (lat, lon) pairs, or None if the KML cannot be read."""
    try:
        return [(lat, lon) for lon, lat in load_boundary()]
    except Exception as err:                        # the map is still useful without it
        print(f'Project boundary not drawn ({KML_FILE}: {err})')
        return None


def buffer_outline(km):
    """Outline of the project boundary grown by `km`, as (lat, lon) pairs."""
    return [(lat, lon) for lon, lat in grown_boundary(km)]


def start_earth_engine():
    """Return the ee module once logged in, or None if Earth Engine is not available."""
    try:
        return init_earth_engine(authenticate=False)
    except Exception as err:
        print(f'Earth Engine not available ({err})')
        return None


def add_forest_layers(m, ee):
    """Tree cover and forest loss tiles from Earth Engine, as a list of map layers."""
    tiles = []
    from parcel_finder import HANSEN
    hansen = ee.Image(HANSEN)
    for band, vis, name in [
            ('treecover2000', {'min': 0, 'max': 100, 'palette': ['#ffffff', '#1a9850']},
             'Tree cover 2000 (%)'),
            ('lossyear', {'min': 1, 'max': 25, 'palette': ['#ffffb2', '#fd8d3c', '#bd0026']},
             'Forest loss (2001 onward, darker = later)')]:
        img = hansen.select(band)
        if band == 'lossyear':
            img = img.selfMask()
        tiles.append(folium.TileLayer(img.getMapId(vis)['tile_fetcher'].url_format,
                                      attr='Hansen / UMD', name=name, overlay=True).add_to(m))
    return tiles


def polygons_only(geojson):
    """Replace geometry collections by their polygons (the map cannot label collections)."""
    from shapely.geometry import mapping, shape
    from shapely.ops import unary_union
    kept = []
    for feature in geojson['features']:
        geom = shape(feature['geometry'])
        parts = getattr(geom, 'geoms', [geom])
        polys = [g for g in parts if g.geom_type in ('Polygon', 'MultiPolygon')]
        if polys:
            kept.append({'type': 'Feature', 'properties': feature['properties'],
                         'geometry': mapping(unary_union(polys))})
    return {'type': 'FeatureCollection', 'features': kept}


def load_protected(ee):
    """Protected areas around the project as GeoJSON: from the cache, else Earth Engine."""
    if PROTECTED_GEOJSON.exists():
        with open(PROTECTED_GEOJSON) as fh:
            return json.load(fh)
    if ee is None:
        return None
    from parcel_finder import PROTECTED
    project = ee.Geometry.Polygon(load_boundary())
    region = project.buffer(SEARCH_KM * 1000).difference(project.buffer(BUFFER_KM * 1000))
    areas = ee.FeatureCollection(PROTECTED).filterBounds(region).map(
        lambda f: ee.Feature(f.geometry().simplify(200), {'name': f.get('NAME')}))
    geojson = polygons_only(areas.getInfo())
    with open(PROTECTED_GEOJSON, 'w') as fh:
        json.dump(geojson, fh)
    return geojson


def main():
    data = load_pool(pool_size=10 ** 9)             # every candidate in candidates.csv
    if data['project_lonlat'] is None:
        raise SystemExit('project_cells.csv has no positions. Run example.py again '
                         '(it now saves them), then select_parcels.py, then this script.')
    with open(SELECTED_CSV, newline='') as fh:
        picked_ids = [row['id'] for row in csv.DictReader(fh)]
    picked = [data['ids'].index(i) for i in picked_ids]
    in_pool = set(range(min(POOL_SIZE, len(data['rows']))))    # rows are sorted by similarity
    cells, distance = best_matches(data, picked)
    farm = data['project_lonlat']
    palette = [colors.to_hex(colormaps['tab20'](i / 20)) for i in range(20)]

    m = folium.Map(location=[farm[:, 1].mean(), farm[:, 0].mean()], zoom_start=9, tiles=None)
    folium.TileLayer(SATELLITE, attr='Esri World Imagery', name='Satellite').add_to(m)

    layers = {}                                     # name -> FeatureGroup, in stage order

    def group(key, name):
        layers[key] = folium.FeatureGroup(name).add_to(m)
        return layers[key]

    g_project = group('project', 'Project')
    g_buffer = group('buffer', 'Leakage buffer')
    g_protected = group('protected', 'Protected areas')
    g_search = group('search', 'Search region')
    g_all = group('all', 'Candidates (by similarity)')
    g_out = group('out', 'Candidates outside the pool')
    g_pool = group('pool', 'Pool (by similarity)')
    g_qaoa = group('qaoa', 'QAOA test problem')
    g_picked = group('picked', 'Selected parcels')
    g_matched = group('matched', 'Matched project cells')
    g_lines = group('lines', 'Match lines')

    ee = start_earth_engine()

    # ---- project
    boundary = project_ring()
    if boundary:
        folium.PolyLine(boundary, color='red', weight=3,
                        tooltip='Project boundary').add_to(g_project)
    for lon, lat in farm:
        folium.CircleMarker((lat, lon), radius=2, color='white', weight=0, fill=True,
                            fill_opacity=0.6).add_to(g_project)

    # ---- forest data
    forest_tiles = []
    if ee is not None:
        try:
            forest_tiles = add_forest_layers(m, ee)
        except Exception as err:
            print(f'Forest layers not added ({err})')

    # ---- leakage buffer, search region
    buffer_ok = False
    if boundary:
        try:
            ring_buffer = buffer_outline(BUFFER_KM)
            ring_search = buffer_outline(SEARCH_KM)
            for layer in (g_buffer, g_search):
                folium.Polygon(ring_buffer, color='yellow', weight=3, fill=True,
                               fill_color='yellow', fill_opacity=0.25,
                               tooltip=f'Leakage buffer ({BUFFER_KM} km): excluded').add_to(layer)
            folium.Polygon(ring_search, color='orange', weight=3, fill=False,
                           tooltip=f'Search limit ({SEARCH_KM} km)').add_to(g_search)
            buffer_ok = True
        except Exception as err:
            print(f'Buffer and search region not drawn ({err})')

    # ---- protected areas
    protected = None
    try:
        protected = load_protected(ee)
    except Exception as err:
        print(f'Protected areas not loaded ({err})')
    if protected:
        for layer in (g_protected, g_search):
            folium.GeoJson(protected, style_function=lambda f: {
                               'color': '#ff00ff', 'weight': 2, 'fillColor': '#ff00ff',
                               'fillOpacity': 0.25},
                           tooltip=folium.GeoJsonTooltip(['name'], aliases=['Protected area'])
                           ).add_to(layer)

    # ---- candidates, then the pool
    sims = [float(r['similarity']) for r in data['rows']]
    lo, hi = min(sims), max(sims)

    def dot(i, layer, colour=None, radius=4):
        row = data['rows'][i]
        if colour is None:
            t = (float(row['similarity']) - lo) / (hi - lo) if hi > lo else 0.5
            colour = colors.to_hex(colormaps['viridis'](t))
        folium.CircleMarker((float(row['lat']), float(row['lon'])), radius=radius,
                            color='white', weight=1, fill=True, fill_color=colour,
                            fill_opacity=0.9,
                            tooltip=f"Candidate {row['id']}: similarity "
                                    f"{float(row['similarity']):.3f}").add_to(layer)

    for i in range(len(data['rows'])):
        dot(i, g_all)
        if i in in_pool:
            dot(i, g_pool, radius=5)
        else:
            dot(i, g_out, '#888888', radius=3)

    # ---- QAOA test problem: 16 parcels, exact answer, QAOA answer
    qaoa_text = None
    if QAOA_JSON.exists():
        with open(QAOA_JSON) as fh:
            q = json.load(fh)
        run = q['runs'][-1]
        exact, found = set(q['exact']), set(run['best_sampled_set'])
        for n, p in enumerate(q['parcels']):
            pos = (p['lat'], p['lon'])
            tip = f"Parcel {p['id']}" + (' - exact optimum' if n in exact else '') \
                + (' - found by QAOA' if n in found else '')
            if n in found:
                folium.CircleMarker(pos, radius=13, color='#1f6feb', weight=3,
                                    fill=False, tooltip=tip).add_to(g_qaoa)
            folium.CircleMarker(pos, radius=7, color='white', weight=1, fill=True,
                                fill_color='#2ca02c' if n in exact else '#999999',
                                fill_opacity=0.95, tooltip=tip).add_to(g_qaoa)
        qaoa_text = (
            f"A {len(q['parcels'])}-parcel version of the problem (pick {len(exact)}) small "
            "enough for a quantum simulator. Green = the exact best set, blue ring = the "
            f"best set QAOA sampled ({run['layers']} layers). QAOA reaches "
            f"{run['ratio']:.2f} of the way to the optimum (random guessing: "
            f"{run['ratio_random']:.2f}); the circuit needs {run['two_qubit_gates']} "
            "two-qubit gates. The real problem below is solved classically.")

    # ---- selected parcels, matches
    for n, (i, c, d) in enumerate(zip(picked, cells, distance)):
        colour = palette[n % len(palette)]
        row = data['rows'][i]
        p_lon, p_lat = float(row['lon']), float(row['lat'])
        f_lon, f_lat = farm[c]
        info = (f"<b>Parcel {row['id']}</b><br>similarity {float(row['similarity']):.3f}<br>"
                f"matches project cell {data['project_ids'][c]}<br>"
                f"gap to that cell: {d:.2f} (spread units)<br>"
                + '<br>'.join(f"{name}: {float(row[name]):.3f}" for name in data['names']))
        folium.Polygon(square(p_lon, p_lat), color=colour, weight=3, fill=True,
                       fill_color=colour, fill_opacity=0.35,
                       popup=folium.Popup(info, max_width=260)).add_to(g_picked)
        folium.Polygon(square(f_lon, f_lat), color=colour, weight=3, fill=True,
                       fill_color=colour, fill_opacity=0.6,
                       tooltip=f"Project cell {data['project_ids'][c]} "
                               f"(matched by parcel {row['id']})").add_to(g_matched)
        folium.PolyLine([(p_lat, p_lon), (f_lat, f_lon)], color=colour, weight=2,
                        opacity=0.9, tooltip=f"Parcel {row['id']}: gap {d:.2f}").add_to(g_lines)

    everything = [(lat, lon) for lon, lat in farm] + [
        (float(r['lat']), float(r['lon'])) for r in data['rows']]
    m.fit_bounds([(min(p[0] for p in everything), min(p[1] for p in everything)),
                  (max(p[0] for p in everything), max(p[1] for p in everything))])
    folium.LayerControl(collapsed=True).add_to(m)

    # ---- stages: (button, explanation, layers shown)
    stages = [('Project', 'The project boundary (red) and the 2 km grid cells inside it '
                          '(white dots). The comparison parcels must resemble these cells.',
               [g_project])]
    if forest_tiles:
        stages.append(('Forest data', 'Tree cover in 2000 and forest loss since. The finder '
                       'matches parcels on forest, pre-start loss and its trend, terrain and '
                       'distance to roads and water. Use the layer menu (top right) to pick '
                       'tree cover or loss.', [g_project] + forest_tiles))
    if buffer_ok:
        stages.append(('Leakage buffer', f'No parcel may lie within {BUFFER_KM} km of the '
                       'project (yellow), because deforestation displaced from the project '
                       'would contaminate the comparison.', [g_project, g_buffer]))
    if protected:
        stages.append(('Protected areas', 'Protected areas (magenta) are excluded too: their '
                       'deforestation follows different rules.', [g_project, g_protected]))
    if buffer_ok:
        stages.append(('Search region', f'Candidates are searched between the buffer and '
                       f'{SEARCH_KM} km from the project (orange), outside protected areas.',
                       [g_project, g_search]))
    stages += [
        ('Candidates', 'Every candidate parcel the finder kept, coloured by similarity to the '
                       'project (yellow = most similar). They are spread out so they cover '
                       'every kind of terrain in the project.',
         [g_project, g_search, g_all]),
        ('Pool', f'The {POOL_SIZE} most similar candidates (coloured) form the pool the '
                 'selection step chooses from. The rest are greyed out.',
         [g_project, g_out, g_pool])]
    if qaoa_text:
        stages.append(('QAOA test problem', qaoa_text, [g_project, g_pool, g_qaoa]))
    stages += [
        ('Selected parcels', f'The {len(picked)} parcels chosen from the pool so that, '
                             'together, they resemble the project while staying spread out.',
         [g_project, g_pool, g_picked]),
        ('Matches', 'Each selected parcel joined to the project cell it resembles most. '
                    'Same colour = one pair. Click a parcel for its numbers.',
         [g_project, g_picked, g_matched, g_lines])]

    def names(groups):
        return '[' + ', '.join(g.get_name() for g in groups) + ']'

    m.get_root().header.add_child(folium.Element(STAGE_CSS))
    m.get_root().script.add_child(folium.Element(STAGE_SCRIPT % {
        'map': m.get_name(),
        'groups': '[' + ', '.join(names(s[2]) for s in stages) + ']',
        'all': names(list(layers.values()) + forest_tiles),
        'stages': json.dumps([[s[0], s[1]] for s in stages])}))
    m.save(MAP_HTML)
    print(f'Saved {MAP_HTML} with {len(stages)} stages; average gap between a parcel and its '
          f'match: {np.mean(distance):.2f} spread units')
    webbrowser.open(MAP_HTML.resolve().as_uri())


if __name__ == '__main__':
    main()
