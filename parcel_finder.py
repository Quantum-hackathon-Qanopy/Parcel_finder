class FindParcels:
    def __init__(self, coordinates):
        self.coordinates = coordinates
        self.geometry = ee.Geometry.Polygon(self.coordinates)
        self.search_region = self.geometry.buffer(50000).difference(self.geometry)

    def load_features(self, features, max_diffs):
      self.feature_stack = features
      self.bandNames = self.feature_stack.bandNames().getInfo()
      self.max_diffs = max_diffs


    def fit(self, parcel_amount):
      self.project_profile = self.feature_stack.reduceRegion(
          reducer=ee.Reducer.mean(),
          geometry=self.geometry,
          scale=30,
          maxPixels=1e9
      ).getInfo()


      grid = search_region.coveringGrid(proj=ee.Projection('EPSG:4326'), scale=400)
      grid_with_data = feature_stack.reduceRegions(
                                      collection=grid,
                                      reducer=ee.Reducer.mean(),
                                      scale=30
                                      )
      

      
      
      
      analyzed_grid = grid_with_data.map(match_areas)
      best_references = analyzed_grid.sort('similarity_score', False).limit(parcel_amount)
      self.parcels = best_references

     
     
    def match_areas(self,cell):
          total_difference = 0
          for band in self.bandNames():
            cell_band = ee.Number(cell.get(band))
            cell_band = ee.Number(ee.Algorithms.If(cell_band, cell_band, -1))
            target_band = ee.Number(self.project_profile.get(band))
            target_band = ee.Number(ee.Algorithms.If(target_band, target_band, -1))
            diff_band = ee.Number(cell_band).subtract(target_band).abs()
            norm_band = diff_band.divide(max_diffs[band]).clamp(0, 1)
            total_difference = total_difference.add(norm_band) 

          
          similarity_score = ee.Number(100).subtract(total_difference.divide(len(self.bandNames)).multiply(100))
          return cell.set('similarity_score', similarity_score)  
