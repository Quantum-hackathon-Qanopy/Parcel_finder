import ee


class FindParcels:
    def __init__(self, coordinates):
        self.coordinates = coordinates
        self.geometry = ee.Geometry.Polygon(self.coordinates)
        self.search_region = self.geometry.buffer(50000).difference(self.geometry)

    def load_features(self, features, max_diffs):
        self.feature_stack = features
        self.bandNames = self.feature_stack.bandNames().getInfo()
        self.max_diffs = max_diffs

    def fit(self, parcel_amount=200):
        """Find the `parcel_amount` candidate parcels most similar to the project area."""
        # Mean value of every feature band over the project area (client-side dict).
        self.project_profile = self.feature_stack.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=self.geometry,
            scale=30,
            maxPixels=1e9
        ).getInfo()

        grid = self.search_region.coveringGrid(proj=ee.Projection('EPSG:4326'), scale=400)
        grid_with_data = self.feature_stack.reduceRegions(
            collection=grid,
            reducer=ee.Reducer.mean(),
            scale=30
        )

        analyzed_grid = grid_with_data.map(self.match_areas)
        best_references = analyzed_grid.sort('similarity_score', False).limit(parcel_amount)
        self.parcels = best_references

    def match_areas(self, cell):
        total_difference = ee.Number(0)
        for band in self.bandNames:
            # Missing data is treated as -1. A plain truthiness check in ee.Algorithms.If
            # would also treat a legitimate 0 as missing, so test for null explicitly.
            cell_value = cell.get(band)
            cell_band = ee.Number(ee.Algorithms.If(ee.Algorithms.IsEqual(cell_value, None), -1, cell_value))

            target_value = self.project_profile.get(band)
            target_band = ee.Number(-1 if target_value is None else target_value)

            diff_band = cell_band.subtract(target_band).abs()
            norm_band = diff_band.divide(self.max_diffs[band]).clamp(0, 1)
            total_difference = total_difference.add(norm_band)

        similarity_score = ee.Number(100).subtract(
            total_difference.divide(len(self.bandNames)).multiply(100)
        )
        return cell.set('similarity_score', similarity_score)
