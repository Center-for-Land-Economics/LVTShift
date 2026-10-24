# Scranton vacant lots and surface parking

An interactive parcel map of the City of Scranton's vacant lots, undeveloped tracts, surface
parking lots and other paved lots, built from public data only. It is the first piece of the Scranton split-rate
repeal analysis (`../`): the parcels it flags are the land whose owners gained most when the
land/building split ended.

```bash
python analysis/scranton_repeal/vacant_parking/build_vacant_parking_map.py            # uses cached downloads
python analysis/scranton_repeal/vacant_parking/build_vacant_parking_map.py --refresh  # re-download everything
```

Outputs, all regenerated and gitignored, in `analysis/reports/scranton/`:

- `vacant_parking_map.html`: self-contained Leaflet map (road/satellite toggle, category and
  tax-exempt filters, OSM parking outlines, click a parcel for owner, class code, size, and
  building, paving and parking shares). Opens by double-click; needs internet only for base tiles.
- `vacant_parking_parcels.csv`: every flagged parcel with its category and inputs.
- `vacant_parking_summary.csv`: parcels and acres by category and tax status. Counts belong
  here, not in prose.
- `vacant_parking_gathered.png` (+ `.json` of its numbers), from `build_gathered_square.py`
  (run after the map script): the vacant, paved and surface-parking lots gathered into one
  square on the city outline, its area equal to their summed ground area and split by
  category. Undeveloped tracts and parking with a building are left out, as its footnote
  says. The outline and the land-area denominator are the Census TIGERweb place boundary.
  The Scranton sibling of `philly_land_tax_research/scripts/build_philly_gathered_square.py`.

## Sources

| Source | What it supplies |
|---|---|
| Lackawanna County GIS, `GISViewer/LandRecords/MapServer/85` | Parcel polygons joined to the roll: PIN, owner, location, class code (`DwellingType`), use code (`DwellingCode`), land and improvement value, and the assessor's free-text `Dimensions` description |
| Chesapeake Bay Land Use/Land Cover Database, 2024 Edition: Lackawanna County 2021/22 raster ([ScienceBase item](https://www.sciencebase.gov/catalog/item/68011bfad4be0263cab10002), 1 m, Chesapeake Conservancy / USGS) | What is on the ground. Structures (raster values 21, and 24 under tree canopy) are the building footprints; Other Impervious (22, and 25 under canopy) is parking lots, driveways, sidewalks and rail lines. Class values are from the 2024 Edition User Guide on the [parent item](https://www.sciencebase.gov/catalog/item/66d099c9d34e98e8a924e574) |
| OpenStreetMap `amenity=parking` (Overpass) | Which paving is parking; `parking=underground`, `multi-storey`, `rooftop`, `lane`, `street_side` are dropped |

The `GISViewer/Parcels` layer that `cities/scranton/model.ipynb` reads has the same polygons,
but its assessment fields are null for every parcel; LandRecords layer 85 is the one with values.
Its values are the **pre-reassessment base** (the roll the 2025 levy was billed on), not the
2026 reassessed roll. They are used here only to tell built from unbuilt parcels, never as dollars.
The 2026 values the repeal model needs come from the County IT extract
(see `../rtk_tracker.csv`).

## Classification

Each parcel is burned onto the land cover's 1 m grid, giving three shares of its area:
`structure_share` (buildings), `paved_share` (non-road paving) and `parking_coverage` (pixels
both paved and inside an OSM parking outline, so a parking outline drawn loosely over a
building or a lawn does not count). Rules apply in priority order; thresholds are constants at
the top of the script.

1. **Surface parking lot**: `parking_coverage` at least `PARKING_COVERAGE` and buildings under
   `MAX_LOT_STRUCTURE`; or the assessor's description mentions parking, the improvement value
   is zero and buildings are under `MAX_LOT_STRUCTURE`.
2. **Mostly parking, with a building**: the same parking test, with a building on the parcel
   (a store and its lot).
3. **Paved lot, not mapped as parking**: at least `MIN_PAVED_SHARE` paved, buildings under
   `MAX_LOT_STRUCTURE`, improvements under `MAX_PARKING_IMP_SHARE` of assessed value, and not
   in OSM. Unmapped parking, truck and equipment yards, paved church and school lots. Rail
   corridors (`RRL` parcel numbers, railroad owners) are excluded, because the land cover maps
   ballast as impervious.
4. **Vacant lot**: improvement value zero on the roll, buildings under `MAX_VACANT_STRUCTURE`
   in the imagery, under `LARGE_TRACT_ACRES`.
5. **Undeveloped tract**: the same, `LARGE_TRACT_ACRES` or more: wooded hillside,
   mine-scarred land, rail and utility land. Kept separate so it does not swamp the urban
   vacant lots.

The imagery overrules the roll in one direction only. A parcel the roll calls unbuilt but
with a building in the imagery is not vacant (spot checks found real houses, garages and a
commercial building on such parcels). A parcel with building value on the roll but no
structure in the imagery is not called vacant, because spot checks found those were mostly
houses hidden under tree canopy rather than demolitions.

Zero-improvement slivers under `MIN_VACANT_SQFT` are dropped. Parcels drawn in several pieces
under one PIN are dissolved to one row. Tax status is the second letter of `DwellingType`
(`T` taxable, `E` exempt); the first letter is the class (R residential, C commercial,
I industrial, G/M/Y/S government, E education, D religious, H hospital, and so on). Use code
`001` is the county's vacant-land code in practice (nearly all such parcels carry no
improvement value), but the code table itself arrives only with the County extract.

## Known limits

- **Paved is not always parking.** *Paved lot* mixes unmapped parking with storage yards and
  hardscape; only OSM separates parking from other paving, and OSM misses lots.
- **Canopy.** A building or lot under dense tree canopy can read as tree cover, which mostly
  hides small houses and makes their parcels look unbuilt in the imagery.
- **Alignment.** Parcel lines, OSM outlines and the raster do not line up exactly, so shares
  near a threshold are noisy on small parcels. *Mostly parking, with a building* is the least
  reliable category.
- **Vintage.** The roll is pre-reassessment and the imagery is 2021/22. A building put up or
  torn down since then is misread until the 2026 County extract and newer imagery replace them.
