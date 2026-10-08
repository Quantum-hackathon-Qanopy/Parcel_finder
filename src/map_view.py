"""Interactive map of the project, its leakage buffer, protected areas and the chosen
candidate parcels (coloured by similarity)."""

import ee
import geemap
from ipyleaflet import LayersControl, WidgetControl
from ipywidgets import HTML
from matplotlib import colormaps, colors

from parcel_finder import HANSEN


def build_map(finder):
    """Return a geemap Map for a fitted `FindParcels`."""
    hansen = ee.Image(HANSEN)
    cells = finder.candidates
    lon, lat = finder.geometry.centroid(1).coordinates().getInfo()

    # geemap's own toolbar, search box and layer list are widgets that do not work
    # in an exported HTML file, so they are switched off.
    m = geemap.Map(center=[lat, lon], zoom=8,
                   toolbar_ctrl=False, search_ctrl=False, draw_ctrl=False,
                   data_ctrl=False, layer_ctrl=False, measure_ctrl=False)
    m.add_basemap('SATELLITE')

    # Forest data
    m.addLayer(hansen.select('treecover2000'),
               {'min': 0, 'max': 100, 'palette': ['#ffffff', '#1a9850']}, 'Tree cover 2000 (%)')
    m.addLayer(hansen.select('lossyear').selfMask(),
               {'min': 1, 'max': 25, 'palette': ['#ffffb2', '#fd8d3c', '#bd0026']},
               'Forest loss (2001 onward, darker = later)')

    # Project, leakage buffer, protected areas (cells touching these are excluded)
    def outline(geometry, **style):
        return ee.FeatureCollection([ee.Feature(geometry)]).style(fillColor='00000000', **style)

    m.addLayer(outline(finder.exclusion_zone, color='yellow', width=2), {}, 'Leakage buffer')
    m.addLayer(outline(finder.geometry, color='red', width=3), {}, 'Project')
    m.addLayer(finder.protected.style(color='FF00FF', fillColor='FF00FF33', width=2),
               {}, 'Protected areas (excluded)')

    # Candidates, coloured by similarity
    sims = [c['properties']['similarity'] for c in cells]
    lo, hi = min(sims), max(sims)
    cmap = colormaps['viridis']

    def color_of(cell):
        t = (cell['properties']['similarity'] - lo) / (hi - lo) if hi > lo else 0.5
        return colors.to_hex(cmap(t))[1:].upper()

    parcels = [ee.Feature(ee.Geometry(c['geometry']),
                          {'style': {'color': color_of(c) + 'FF', 'fillColor': color_of(c) + '88',
                                     'width': 2}}) for c in cells]
    dots = [ee.Feature(ee.Geometry.Point([c['properties']['lon'], c['properties']['lat']]),
                       {'style': {'color': 'FFFFFF', 'fillColor': color_of(c) + 'FF',
                                  'pointSize': 6, 'pointShape': 'circle', 'width': 1}})
            for c in cells]
    m.addLayer(ee.FeatureCollection(parcels).style(styleProperty='style'), {},
               'Candidate parcels (zoom in)')
    m.addLayer(ee.FeatureCollection(dots).style(styleProperty='style'), {},
               'Candidate parcels (centres)')

    # Legend
    stops = ', '.join(colors.to_hex(cmap(i / 4)) for i in range(5))
    m.add(WidgetControl(position='bottomright', widget=HTML(
        "<div style='background:white;padding:6px 10px;border-radius:4px;font-size:12px'>"
        "<b>Similarity (0 to 1)</b>"
        f"<div style='height:12px;width:180px;background:linear-gradient(to right, {stops});"
        "margin:4px 0'></div>"
        f"<div style='display:flex;justify-content:space-between'>"
        f"<span>{lo:.3f}</span><span>{hi:.3f}</span></div></div>")))
    m.add(LayersControl(position='topright'))
    return m