# Scranton vacant lots and surface parking

An interactive parcel map of the City of Scranton's vacant lots, undeveloped tracts and surface
parking lots, built from public data only. It is the first piece of the Scranton split-rate
repeal analysis (`../`): the parcels it flags are the land whose owners gained most when the
land/building split ended.

```bash
python analysis/scranton_repeal/vacant_parking/build_vacant_parking_map.py            # uses cached downloads
python analysis/scranton_repeal/vacant_parking/build_vacant_parking_map.py --refresh  # re-download everything
```

Outputs, all regenerated and gitignored, in `analysis/reports/scranton/`:

- `vacant_parking_map.html`: self-contained Leaflet map (road/satellite toggle, category and
  tax-exempt filters, OSM parking outlines, click a parcel for owner, class code, size and
  parking coverage). Opens by double-click; needs internet only for base tiles.
- `vacant_parking_parcels.csv`: every flagged parcel with its category and inputs.
- `vacant_parking_summary.csv`: parcels and acres by category and tax status. Counts belong
  here, not in prose.

## Sources

| Source | What it supplies |
|---|---|
| Lackawanna County GIS, `GISViewer/LandRecords/MapServer/85` | Parcel polygons joined to the roll: PIN, owner, location, class code (`DwellingType`), use code (`DwellingCode`), land and improvement value, and the assessor's free-text `Dimensions` description |
| OpenStreetMap `amenity=parking` (Overpass) | Surface parking outlines; `parking=underground`, `multi-storey`, `rooftop`, `lane`, `street_side` are dropped |
| OpenStreetMap `building=*` (Overpass) | Footprints cut out of the parking area, so a parking outline drawn over a building does not make the building a lot. OSM maps only a minority of Scranton's buildings, so this catches few cases; a complete footprint set (Microsoft or Overture) would do the job properly |

The `GISViewer/Parcels` layer that `cities/scranton/model.ipynb` reads has the same polygons,
but its assessment fields are null for every parcel; LandRecords layer 85 is the one with values.
Its values are the **pre-reassessment base** (the roll the 2025 levy was billed on), not the
2026 reassessed roll. They are used here only to tell built from unbuilt parcels, never as dollars.
The 2026 values the repeal model needs come from the County IT extract
(see `../rtk_tracker.csv`).

## Classification

Rules apply in priority order; thresholds are constants at the top of the script.

1. **Surface parking lot**: OSM surface parking (net of building footprints) covers at least
   `PARKING_COVERAGE` of the parcel and improvements are under `MAX_PARKING_IMP_SHARE` of its
   assessed value; or the assessor's description mentions parking and the improvement value is zero.
2. **Mostly parking, with a building**: the same coverage test, but the parcel carries a
   building (a store and its lot).
3. **Vacant lot**: improvement value zero, under `LARGE_TRACT_ACRES`.
4. **Undeveloped tract**: improvement value zero, `LARGE_TRACT_ACRES` or more: wooded
   hillside, mine-scarred land, rail and utility land. Kept separate so it does not swamp the
   urban vacant lots.

Zero-improvement slivers under `MIN_VACANT_SQFT` are dropped. Parcels drawn in several pieces
under one PIN are dissolved to one row. Tax status is the second letter of `DwellingType`
(`T` taxable, `E` exempt); the first letter is the class (R residential, C commercial,
I industrial, G/M/Y/S government, E education, D religious, H hospital, and so on). Use code
`001` is the county's vacant-land code in practice (nearly all such parcels carry no
improvement value), but the code table itself arrives only with the County extract.

## Known limits

- **OSM misses lots.** A paved lot that OSM does not map, and whose roll record shows no
  building, lands in *Vacant lot*. Satellite spot checks found this mainly on industrial
  yards and small commercial lots. A 1 m impervious-surface land cover would separate paved
  from green vacant land. Scranton drains to the Susquehanna, so it should fall inside the
  Chesapeake Conservancy's 1 m Chesapeake Bay watershed land cover (not yet checked).
- **Alignment.** OSM outlines and county parcel lines do not always agree. Coverage near the
  threshold is noisy, so *Mostly parking, with a building* is the least reliable category.
- **Vintage.** The improvement-value test reads the pre-reassessment roll. A building put up
  or torn down since that roll was last updated is misread until the 2026 extract replaces it.
