import ee
import geopandas as gpd
from parcel_finder import FindParcels


ee.Authenticate()
ee.Initialize()

gdf = gpd.read_file("ALGAR_limite_fazenda_pacaja_v2_MS_17012019.shp.kml", driver='KML')

coords = list(gdf.geometry.iloc[0].exterior.coords)
coords = [(coord[0], coord[1]) for coord in coords]

hansen = ee.Image('UMD/hansen/global_forest_change_2025_v1_13')
elevation = ee.Image('USGS/SRTMGL1_003')
forest_cover = hansen.select('treecover2000')
deforestation = hansen.select('lossyear').gt(0).unmask(0)


max_diffs = {
    'biomass': 10.0,   
    'history': 0.1,    
    'elevation': 500.0  
}

feature_stack = ee.Image.cat([
    forest_cover.rename('biomass'),
    deforestation.rename('history'),
    elevation.rename("elevation")
])

parcel_finder = FindParcels(coords)

parcel_finder.load_features(feature_stack, max_diffs)

parcel_finder.fit(3)

parcel_finder.parcels






