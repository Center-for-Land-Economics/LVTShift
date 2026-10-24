"""
Idle-land classification from 1 m land cover, OSM parking and the assessment roll.

Shared by the Scranton vacant/parking map (analysis/scranton_repeal/vacant_parking/)
and the Philadelphia comparison in philly_land_tax_research, so both cities are
classified by the same rules and the same thresholds.

Each parcel is burned onto a 1 m land cover grid, giving three shares of its area:
structure_share (buildings), paved_share (non-road paving) and parking_coverage
(pixels both paved and inside an OSM parking outline). classify_idle then sorts
parcels, in priority order, into surface parking, parking with a building, paved
lots, vacant lots and undeveloped tracts.

Both land cover products use the same class definitions (the Chesapeake Bay
Program's), with different raster values:
  - Chesapeake Bay Land Use/Land Cover Database, 2024 Edition (counties in or
    beside the Bay watershed): CHESAPEAKE_LULC_CODES
  - EPA Region 3 high-resolution land cover for the counties outside the watershed
    (Pennsylvania 2022; EnviroAtlas): EPA_R3_LANDCOVER_CODES
"""
from typing import Dict, Optional, Tuple

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import rasterize
from rasterio.windows import Window, from_bounds
from shapely.geometry import box

ACRE_SQM = 4046.8564224
SQFT_PER_SQM = 10.7639

# Raster values per class, from each product's documentation
CHESAPEAKE_LULC_CODES: Dict[str, Tuple[int, ...]] = {
    'structure': (21, 24),  # Structures; Tree Canopy over Structures
    'paved': (22, 25),      # Other Impervious; Tree Canopy over Other Impervious
}
EPA_R3_LANDCOVER_CODES: Dict[str, Tuple[int, ...]] = {
    'structure': (7, 10),   # Impervious Structures; Tree Canopy over Impervious Structures
    'paved': (8, 11),       # Other Impervious; Tree Canopy over Other Impervious
}

# OSM parking= values that are not surface lots; pass as fetch_osm_parking's exclude_types
OSM_NON_SURFACE_PARKING = ('underground', 'multi-storey', 'lane', 'rooftop', 'street_side')

PARKING_COVERAGE = 0.4       # share of parcel both paved and inside OSM parking
MAX_LOT_STRUCTURE = 0.10     # a parking or paved lot carries no more building than this
MAX_VACANT_STRUCTURE = 0.15  # a vacant lot carries no more building than this (sheds, overhang)
MIN_PAVED_SHARE = 0.5
MAX_PARKING_IMP_SHARE = 0.25
LARGE_TRACT_ACRES = 1.0
MIN_VACANT_SQFT = 500

CATEGORY_LABELS = {
    'parking': 'Surface parking lot',
    'parking_bldg': 'Mostly parking, with a building',
    'paved': 'Paved lot, not mapped as parking',
    'vacant': 'Vacant lot (under 1 acre)',
    'tract': 'Undeveloped tract (1 acre+)',
}


def taxable_land_shares(category: pd.Series, acres: pd.Series, taxable: pd.Series) -> dict:
    """
    Each idle category's acres and share of taxable land area, taxable parcels only.

    Parameters
    ----------
    category : Series
        classify_idle output, one row per parcel (all parcels, idle or not).
    acres : Series
        Parcel ground area in acres.
    taxable : Series of bool
        Whether the parcel is on the tax roll.

    Returns
    -------
    dict
        taxable_acres, taxable_parcels, and per category {acres, pct, parcels},
        plus 'urban' (parking + paved + vacant) and 'all_idle' (urban + tract).
    """
    tx = taxable.fillna(False).astype(bool)
    total = float(acres[tx].sum())
    out = {'taxable_acres': total, 'taxable_parcels': int(tx.sum()), 'categories': {}}
    groups = {**{k: [k] for k in CATEGORY_LABELS},
              'urban': ['parking', 'paved', 'vacant'], 'all_idle': ['parking', 'paved', 'vacant', 'tract']}
    for name, keys in groups.items():
        sel = tx & category.isin(keys)
        a = float(acres[sel].sum())
        out['categories'][name] = {'acres': a, 'pct': a / total * 100, 'parcels': int(sel.sum())}
    return out


def land_cover_shares(
    parcels: gpd.GeoDataFrame,
    parking: gpd.GeoDataFrame,
    raster_path,
    codes: Dict[str, Tuple[int, ...]],
    block_rows: int = 4096,
) -> pd.DataFrame:
    """
    Structure, paving and parking shares of each parcel from a 1 m land cover raster.

    Parcels are burned onto the raster grid one block of rows at a time, so a
    county-sized raster never has to sit in memory whole. Where parcel polygons
    overlap, a pixel goes to the later parcel. Shares are of each parcel's own
    pixels.

    Parameters
    ----------
    parcels : GeoDataFrame
        Parcel polygons, any CRS.
    parking : GeoDataFrame
        OSM surface parking polygons, any CRS.
    raster_path : str or Path
        Land cover GeoTIFF.
    codes : dict
        'structure' and 'paved' raster values, e.g. CHESAPEAKE_LULC_CODES.
    block_rows : int
        Raster rows per block.

    Returns
    -------
    DataFrame
        Indexed like parcels, with structure_share, paved_share, parking_coverage
        and pixels (the parcel's pixel count, its area in m²).
    """
    with rasterio.open(raster_path) as src:
        crs = src.crs
        pp = parcels.to_crs(crs).reset_index(drop=True)
        pk = parking.to_crs(crs).reset_index(drop=True)
        full = from_bounds(*pp.total_bounds, src.transform).round_offsets().round_lengths()
        col_off, row_off = int(full.col_off), int(full.row_off)
        width, height = int(full.width), int(full.height)
        n = len(pp) + 1
        px = np.zeros(n)
        struct = np.zeros(n)
        paved = np.zeros(n)
        in_lot = np.zeros(n)
        for r0 in range(0, height, block_rows):
            win = Window(col_off, row_off + r0, width, min(block_rows, height - r0))
            transform = src.window_transform(win)
            bounds = rasterio.windows.bounds(win, src.transform)
            hits = np.sort(pp.sindex.query(box(*bounds)))
            if not len(hits):
                continue
            lc = src.read(1, window=win, boundless=True, fill_value=0)
            ids = rasterize(((pp.geometry.iloc[i], i + 1) for i in hits), out_shape=lc.shape,
                            transform=transform, fill=0, dtype='int32')
            inside = ids > 0
            if not inside.any():
                continue
            idx = ids[inside]
            lots = pk.sindex.query(box(*bounds))
            lot_mask = (rasterize(pk.geometry.iloc[lots], out_shape=lc.shape, transform=transform,
                                  fill=0, dtype='uint8').astype(bool)
                        if len(lots) else np.zeros(lc.shape, bool))
            is_paved = np.isin(lc, codes['paved'])
            px += np.bincount(idx, minlength=n)
            struct += np.bincount(idx, weights=np.isin(lc, codes['structure'])[inside], minlength=n)
            paved += np.bincount(idx, weights=is_paved[inside], minlength=n)
            in_lot += np.bincount(idx, weights=(is_paved & lot_mask)[inside], minlength=n)
    denom = np.maximum(px[1:], 1)
    return pd.DataFrame({'structure_share': struct[1:] / denom, 'paved_share': paved[1:] / denom,
                         'parking_coverage': in_lot[1:] / denom, 'pixels': px[1:]},
                        index=parcels.index)


def classify_idle(
    shares: pd.DataFrame,
    area_sqm: pd.Series,
    building_value: pd.Series,
    imp_share: pd.Series,
    assessor_parking: Optional[pd.Series] = None,
    rail: Optional[pd.Series] = None,
) -> pd.Series:
    """
    Idle-land category per parcel, or None for parcels that are not idle.

    Rules, in priority order (thresholds are module constants):
      parking       parking_coverage >= PARKING_COVERAGE and structure_share <
                    MAX_LOT_STRUCTURE; or the assessor marks the parcel as parking,
                    it has no building value and structure_share < MAX_LOT_STRUCTURE
      parking_bldg  the same parking test with a structure on the parcel
      paved         paved_share >= MIN_PAVED_SHARE, structure_share <
                    MAX_LOT_STRUCTURE, imp_share < MAX_PARKING_IMP_SHARE, not rail
      vacant        no building value, structure_share < MAX_VACANT_STRUCTURE,
                    under LARGE_TRACT_ACRES
      tract         the same, LARGE_TRACT_ACRES or more
    Zero-improvement slivers under MIN_VACANT_SQFT are not vacant. The imagery
    overrules the roll in one direction only: a parcel the roll calls unbuilt but
    with a structure in the imagery is not vacant, while a parcel with building
    value and no structure in the imagery (often a house under tree canopy) is not
    called vacant.

    Parameters
    ----------
    shares : DataFrame
        land_cover_shares output.
    area_sqm : Series
        Parcel ground area in square metres.
    building_value : Series
        Assessed improvement value (any vintage; only zero vs non-zero matters).
    imp_share : Series
        Improvement value as a share of total assessed value.
    assessor_parking : Series of bool, optional
        The assessor's own parking flag (a use code or description).
    rail : Series of bool, optional
        Rail corridors, kept out of 'paved' because land cover maps ballast as
        impervious.

    Returns
    -------
    Series
        Category key per parcel (see CATEGORY_LABELS), None where not idle.
    """
    idx = shares.index
    acres = area_sqm / ACRE_SQM
    no_building_value = building_value.fillna(0).eq(0)
    lot_structure = shares['structure_share'] < MAX_LOT_STRUCTURE
    mapped_parking = shares['parking_coverage'] >= PARKING_COVERAGE
    text_parking = (assessor_parking.fillna(False).astype(bool) & no_building_value
                    if assessor_parking is not None else pd.Series(False, index=idx))
    not_rail = ~rail.fillna(False).astype(bool) if rail is not None else pd.Series(True, index=idx)
    paved_lot = ((shares['paved_share'] >= MIN_PAVED_SHARE) & lot_structure
                 & (imp_share < MAX_PARKING_IMP_SHARE) & not_rail)
    vacant = (no_building_value & (shares['structure_share'] < MAX_VACANT_STRUCTURE)
              & (area_sqm * SQFT_PER_SQM >= MIN_VACANT_SQFT))

    cat = pd.Series(None, index=idx, dtype=object)
    cat[vacant & (acres >= LARGE_TRACT_ACRES)] = 'tract'
    cat[vacant & (acres < LARGE_TRACT_ACRES)] = 'vacant'
    cat[paved_lot] = 'paved'
    cat[mapped_parking & ~lot_structure] = 'parking_bldg'
    cat[(mapped_parking | text_parking) & lot_structure] = 'parking'
    return cat
