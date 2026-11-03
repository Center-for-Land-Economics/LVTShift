"""Offline tests for lvt.waterfront_utils (no network / TIGER download)."""

import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point, Polygon

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lvt.waterfront_utils import (
    add_waterfront_flags,
    split_single_family_by_waterfront,
    waterfront_share,
)


def _synthetic_parcels():
    # Parcels laid out along the x-axis (meters, in a projected CRS) so the
    # distance to a water body at x=0 is easy to reason about.
    pts = [Point(x, 0) for x in (5, 50, 500)]
    return gpd.GeoDataFrame(
        {"property_category": ["Single Family Residential"] * 3},
        geometry=pts,
        crs="EPSG:6933",
    )


def _synthetic_water():
    # A lake occupying x in [-100, 0].
    poly = Polygon([(-100, -100), (0, -100), (0, 100), (-100, 100)])
    return gpd.GeoDataFrame({"MTFCC": ["H2030"]}, geometry=[poly], crs="EPSG:6933")


def test_flags_use_thresholds():
    parcels = _synthetic_parcels()
    out = add_waterfront_flags(
        parcels,
        water_gdf=_synthetic_water(),
        thresholds_m=(30, 100),
        projected_crs="EPSG:6933",
    )
    # Distances to the lake edge at x=0 are 5, 50, 500 m.
    assert list(out["waterfront_30m"]) == [True, False, False]
    assert list(out["waterfront_100m"]) == [True, True, False]
    assert abs(out["water_dist_m"].iloc[1] - 50) < 1e-6


def test_no_water_flags_all_false():
    parcels = _synthetic_parcels()
    empty = gpd.GeoDataFrame({"MTFCC": []}, geometry=[], crs="EPSG:6933")
    out = add_waterfront_flags(parcels, water_gdf=empty, thresholds_m=(30, 100))
    assert not out["waterfront_30m"].any()
    assert not out["waterfront_100m"].any()


def test_split_only_touches_flagged_sfh():
    df = pd.DataFrame(
        {
            "property_category": [
                "Single Family Residential",
                "Single Family Residential",
                "Condominium",
            ],
            "waterfront_100m": [True, False, True],
        }
    )
    out = split_single_family_by_waterfront(df)
    assert list(out["property_category"]) == [
        "Single Family Residential — Waterfront",
        "Single Family Residential",
        "Condominium",  # condos are not split even when on the water
    ]


def test_waterfront_share():
    df = pd.DataFrame(
        {
            "property_category": ["Single Family Residential"] * 4 + ["Condominium"],
            "waterfront_100m": [True, True, False, False, True],
        }
    )
    stats = waterfront_share(df)
    assert stats["sfh_total"] == 4
    assert stats["waterfront_count"] == 2
    assert abs(stats["waterfront_pct"] - 50.0) < 1e-9
