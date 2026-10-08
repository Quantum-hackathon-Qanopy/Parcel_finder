"""Map of the picked comparison parcels and the project cells they match.

For every picked parcel, a line leads to the project cell it resembles most. Each pair
has its own colour. Run select_parcels.py first (it writes selected_parcels.csv), then:

    python src/selection_map.py

The map opens in your browser and is saved as selection_map.html. It is the one combined
map: all candidates (coloured by similarity), the picked parcels and their matched project
cells. Forest layers (tree cover, forest loss) are added if Earth Engine is logged in;
without it they are skipped and everything else comes from the CSV files.
"""

import csv
import math
import sys
import webbrowser
from pathlib import Path

import folium
import numpy as np
from matplotlib import colormaps, colors

sys.path.insert(0, str(Path(__file__).resolve().parent))   # find select_parcels.py
from select_parcels import DATA_DIR, OUTPUT_DIR, SELECTED_CSV, best_matches, load_pool

KML_FILE = DATA_DIR / 'ALGAR_limite_fazenda_pacaja_v2_MS_17012019.shp.kml'   # same file as example.py
MAP_HTML = OUTPUT_DIR / 'selection_map.html'
CELL_KM = 2.0                    # side length of a grid cell, as in example.py (CELL_SIZE_M)
SATELLITE = ('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/'
             'MapServer/tile/{z}/{y}/{x}')


def square(lon, lat, km=CELL_KM):
    """Corners (lat, lon) of a square of side `km` centred on a point."""
    dlat = km / 2 / 111.0
    dlon = dlat / math.cos(math.radians(lat))
    return [(lat - dlat, lon - dlon), (lat - dlat, lon + dlon),
            (lat + dlat, lon + dlon), (lat + dlat, lon - dlon)]


def add_outline(m):
    """Draw the project boundary from the KML file, if it can be read."""
    try:
        import geopandas as gpd
        ring = gpd.read_file(KML_FILE, driver='KML').geometry.iloc[0].exterior.coords
        folium.PolyLine([(c[1], c[0]) for c in ring], color='red', weight=3,
                        tooltip='Project boundary').add_to(m)
    except Exception as err:                        # the map is still useful without it
        print(f'Project boundary not drawn ({KML_FILE}: {err})')


def add_forest_layers(m):
    """Tree cover and forest loss from Earth Engine, if it is available (off by default)."""
    try:
        import ee
        from example import EE_PROJECT
        from parcel_finder import HANSEN
        ee.Initialize(project=EE_PROJECT)
        hansen = ee.Image(HANSEN)
        for band, vis, name in [
                ('treecover2000', {'min': 0, 'max': 100, 'palette': ['#ffffff', '#1a9850']},
                 'Tree cover 2000 (%)'),
                ('lossyear', {'min': 1, 'max': 25, 'palette': ['#ffffb2', '#fd8d3c', '#bd0026']},
                 'Forest loss (2001 onward, darker = later)')]:
            img = hansen.select(band)
            if band == 'lossyear':
                img = img.selfMask()
            folium.TileLayer(img.getMapId(vis)['tile_fetcher'].url_format, attr='Hansen / UMD',
                             name=name, overlay=True, show=False).add_to(m)
    except Exception as err:                        # the map is still useful without them
        print(f'Forest layers not added ({err})')


def main():
    data = load_pool(pool_size=10 ** 9)             # every candidate in candidates.csv
    if data['project_lonlat'] is None:
        raise SystemExit('project_cells.csv has no positions. Run example.py again '
                         '(it now saves them), then select_parcels.py, then this script.')
    with open(SELECTED_CSV, newline='') as fh:
        picked_ids = [row['id'] for row in csv.DictReader(fh)]
    picked = [data['ids'].index(i) for i in picked_ids]
    cells, distance = best_matches(data, picked)
    farm = data['project_lonlat']
    palette = [colors.to_hex(colormaps['tab20'](i / 20)) for i in range(20)]

    m = folium.Map(location=[farm[:, 1].mean(), farm[:, 0].mean()], zoom_start=9, tiles=None)
    folium.TileLayer(SATELLITE, attr='Esri World Imagery', name='Satellite').add_to(m)
    add_forest_layers(m)
    add_outline(m)

    farm_layer = folium.FeatureGroup('Project cells').add_to(m)
    other_layer = folium.FeatureGroup('Other candidates (not picked, by similarity)').add_to(m)
    matched_layer = folium.FeatureGroup('Matched project cells').add_to(m)
    parcel_layer = folium.FeatureGroup('Picked parcels').add_to(m)
    line_layer = folium.FeatureGroup('Match lines').add_to(m)

    for lon, lat in farm:
        folium.CircleMarker((lat, lon), radius=2, color='white', weight=0, fill=True,
                            fill_opacity=0.5).add_to(farm_layer)
    sims = [float(r['similarity']) for r in data['rows']]
    lo, hi = min(sims), max(sims)
    for i, row in enumerate(data['rows']):
        if i not in picked:
            t = (float(row['similarity']) - lo) / (hi - lo) if hi > lo else 0.5
            folium.CircleMarker((float(row['lat']), float(row['lon'])), radius=4,
                                color='white', weight=1, fill=True,
                                fill_color=colors.to_hex(colormaps['viridis'](t)),
                                fill_opacity=0.9,
                                tooltip=f"Candidate {row['id']}: similarity "
                                        f"{float(row['similarity']):.3f}").add_to(other_layer)

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
                       popup=folium.Popup(info, max_width=260)).add_to(parcel_layer)
        folium.Polygon(square(f_lon, f_lat), color=colour, weight=3, fill=True,
                       fill_color=colour, fill_opacity=0.6,
                       tooltip=f"Project cell {data['project_ids'][c]} "
                               f"(matched by parcel {row['id']})").add_to(matched_layer)
        folium.PolyLine([(p_lat, p_lon), (f_lat, f_lon)], color=colour, weight=2,
                        opacity=0.9, tooltip=f"Parcel {row['id']}: gap {d:.2f}").add_to(line_layer)

    everything = [(lat, lon) for lon, lat in farm] + [
        (float(data['rows'][i]['lat']), float(data['rows'][i]['lon'])) for i in picked]
    m.fit_bounds([(min(p[0] for p in everything), min(p[1] for p in everything)),
                  (max(p[0] for p in everything), max(p[1] for p in everything))])
    folium.LayerControl(collapsed=False).add_to(m)
    m.get_root().html.add_child(folium.Element(
        "<div style='position:fixed;bottom:20px;left:20px;z-index:9999;background:white;"
        "padding:8px 12px;border-radius:4px;font-size:12px;max-width:260px'>"
        f"<b>{len(picked)} picked parcels</b> (large squares outside the project) joined by a "
        "line to the project cell each resembles most. Same colour = one pair. "
        "Click a parcel for its numbers. A small gap means a close match.</div>"))
    m.save(MAP_HTML)
    print(f'Saved {MAP_HTML}; average gap between a parcel and its match: '
          f'{np.mean(distance):.2f} spread units')
    webbrowser.open(MAP_HTML.resolve().as_uri())


if __name__ == '__main__':
    main()