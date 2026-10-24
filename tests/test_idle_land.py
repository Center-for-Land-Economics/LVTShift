"""
Tests for lvt/idle_land.py: block-by-block land cover shares, and the order of the
idle-land rules.

These pin mechanics, not data. Whether the rules classify real parcels correctly was
checked against aerial imagery for Scranton and Philadelphia (see the READMEs of
analysis/scranton_repeal/vacant_parking/ and philly_land_tax_research's
analysis/idle_land/), which no fixture here can stand in for.
"""
from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from lvt.idle_land import (
    ACRE_SQM, CHESAPEAKE_LULC_CODES, classify_idle, land_cover_shares,
)

CRS = 'EPSG:26918'


@pytest.fixture
def scene(tmp_path):
    """A 60 x 40 m raster: buildings, paving and forest, with three parcels and one lot."""
    lc = np.full((40, 60), 41, dtype='uint8')  # forest
    lc[:, 0:20] = 22                           # paving, west third
    lc[0:10, 20:40] = 21                       # a building, north of the middle third
    path = tmp_path / 'lc.tif'
    with rasterio.open(path, 'w', driver='GTiff', height=40, width=60, count=1, dtype='uint8',
                       crs=CRS, transform=from_origin(0, 40, 1, 1)) as dst:
        dst.write(lc, 1)
    parcels = gpd.GeoDataFrame(geometry=[box(0, 0, 20, 40), box(20, 0, 40, 40), box(40, 0, 60, 40)],
                               crs=CRS)
    parking = gpd.GeoDataFrame(geometry=[box(0, 0, 10, 40)], crs=CRS)
    return path, parcels, parking


def test_block_processing_matches_single_pass(scene):
    path, parcels, parking = scene
    whole = land_cover_shares(parcels, parking, path, CHESAPEAKE_LULC_CODES, block_rows=10_000)
    blocks = land_cover_shares(parcels, parking, path, CHESAPEAKE_LULC_CODES, block_rows=7)
    pd.testing.assert_frame_equal(whole, blocks)
    assert whole['pixels'].tolist() == [800, 800, 800]
    assert whole['paved_share'].tolist() == [1.0, 0.0, 0.0]
    assert whole['parking_coverage'].tolist() == [0.5, 0.0, 0.0]
    assert whole['structure_share'].tolist() == [0.0, 0.25, 0.0]


def _classify(rows, **kw):
    df = pd.DataFrame(rows)
    shares = df[['structure_share', 'paved_share', 'parking_coverage']]
    cat = classify_idle(shares, area_sqm=df['area'], building_value=df['bldg'],
                        imp_share=df['imp'], **kw)
    return [None if pd.isna(c) else c for c in cat]


def test_rule_priority():
    base = dict(structure_share=0.0, paved_share=0.0, parking_coverage=0.0, area=400.0, bldg=0, imp=0.0)
    rows = [
        {**base, 'paved_share': 0.9, 'parking_coverage': 0.8},          # mapped lot, unbuilt
        {**base, 'paved_share': 0.6, 'parking_coverage': 0.5,
         'structure_share': 0.3, 'bldg': 1, 'imp': 0.8},                # store and its lot
        {**base, 'paved_share': 0.9},                                    # paved, not in OSM
        {**base},                                                        # bare lot
        {**base, 'area': 2 * ACRE_SQM},                                  # big bare tract
        {**base, 'structure_share': 0.3},                                # roll unbuilt, imagery built
        {**base, 'bldg': 1, 'imp': 0.8},                                 # roll built, imagery not
        {**base, 'area': 20.0},                                          # sliver
    ]
    assert _classify(rows) == ['parking', 'parking_bldg', 'paved', 'vacant', 'tract', None, None, None]


def test_assessor_parking_and_rail():
    base = dict(structure_share=0.0, paved_share=0.9, parking_coverage=0.0, area=400.0, bldg=0, imp=0.0)
    out = _classify([base, base], assessor_parking=pd.Series([True, False]),
                    rail=pd.Series([False, True]))
    assert out == ['parking', 'vacant']
