"""Unit tests for the shared Philadelphia LYCD construction and exemption carry-forward.

Synthetic data only, no network or cache dependencies.
"""

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import Point

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lvt.philadelphia import (  # noqa: E402
    HOMESTEAD_ORDERS,
    uncap_bare_land,
    relevel_beyond_support,
    large_lot_band_factor,
    LARGE_TRACT_TREATMENTS,
    compute_lycd_land_values,
    carry_forward_exemptions,
    compute_residual_building_value,
    reallocate_land_within_total,
    paint_land_surface,
    condo_unit_areas,
    corrected_lot_areas,
)


def _toy_city(n_per_zone: int = 60) -> tuple[gpd.GeoDataFrame, pd.DataFrame, pd.DataFrame]:
    """Two L3 zones inside one L2/L1; zone A improved parcels at $100/sqft, zone B at $200/sqft."""
    rows = []
    k = 0
    for zone, psf, x0 in (("A", 100.0, 0.0), ("B", 200.0, 10_000.0)):
        for i in range(n_per_zone):
            rows.append(dict(parcel_number=f"{k:09d}", pin=str(1000 + k), category_code="1",
                             total_area=1000.0, market_value=psf * 1000.0, taxable_building=0.0,
                             x=x0 + i, y=0.0, zone=zone))
            k += 1
        # one vacant lot per zone, OPA-valued far below the zone rate
        rows.append(dict(parcel_number=f"{k:09d}", pin=str(1000 + k), category_code="6",
                         total_area=500.0, market_value=5_000.0, taxable_building=0.0,
                         x=x0 + 1, y=1.0, zone=zone))
        k += 1
    # an improved parcel in zone A with no OPA area but a PIN polygon
    rows.append(dict(parcel_number=f"{k:09d}", pin=str(1000 + k), category_code="1",
                     total_area=0.0, market_value=100_000.0, taxable_building=0.0,
                     x=2.0, y=1.0, zone="A"))
    k += 1
    # an improved parcel with no GMA assignment at all, sitting in zone B's cluster
    rows.append(dict(parcel_number=f"{k:09d}", pin=str(1000 + k), category_code="1",
                     total_area=1000.0, market_value=200_000.0, taxable_building=0.0,
                     x=10_003.0, y=1.0, zone=None))
    k += 1
    # a cheap house in the expensive zone: the market-value cap must bind
    rows.append(dict(parcel_number=f"{k:09d}", pin=str(1000 + k), category_code="1",
                     total_area=1000.0, market_value=20_000.0, taxable_building=0.0,
                     x=10_004.0, y=1.0, zone="B"))
    df = pd.DataFrame(rows)
    gdf = gpd.GeoDataFrame(df.drop(columns=["x", "y"]),
                           geometry=[Point(x, y) for x, y in zip(df.x, df.y)], crs="EPSG:3857")
    gma = pd.DataFrame({"key": df.parcel_number, "gma3": df.zone, "gma2": "L2", "gma1": "L1"})
    gma = gma[gma.gma3.notna()]
    pin_areas = pd.DataFrame({"pin": df.pin, "pin_area_sqft": np.where(df.total_area > 0, df.total_area, 800.0)})
    return gdf, gma, pin_areas


def test_lycd_zone_rate_times_area():
    gdf, gma, pin_areas = _toy_city()
    res = compute_lycd_land_values(gdf, gma, pin_areas, min_improved=50, city_area_sqft=1e9)
    out = res.gdf.set_index("parcel_number")
    a_imp = out[(out.gma3 == "A") & (out.category_code == "1") & (out.total_area > 0)]
    # zone A: median 100 $/sqft x 0.20 x 1000 sqft
    assert np.allclose(a_imp.lycd_land_value, 100.0 * 0.20 * 1000.0)
    assert (a_imp.gma_level == "L3").all()
    # vacant in zone A: 100 $/sqft x 1.00 x 500 sqft, uncapped even though OPA says $5k
    vac_a = out[(out.gma3 == "A") & (out.category_code == "6")]
    assert np.allclose(vac_a.lycd_land_value, 100.0 * 500.0)
    assert res.diagnostics["land_pct_vacant"] == 1.0


def test_lycd_area_chain_and_cap():
    gdf, gma, pin_areas = _toy_city()
    res = compute_lycd_land_values(gdf, gma, pin_areas, min_improved=50, city_area_sqft=1e9)
    out = res.gdf.set_index("parcel_number")
    # OPA area missing -> PIN polygon area (800 sqft) at zone A's rate
    no_opa = out[(out.total_area == 0) & (out.category_code == "1")]
    assert (no_opa.area_source == "pin_dor").all()
    assert np.allclose(no_opa.lycd_land_value, 100.0 * 0.20 * 800.0)
    # cheap house in zone B: 200 x 0.2 x 1000 = 40,000 > market 20,000 -> capped
    cheap = out[out.market_value == 20_000.0]
    assert np.allclose(cheap.lycd_land_value, 20_000.0)
    assert res.diagnostics["n_capped"] == 1
    assert res.diagnostics["land_base_pre_cap"] > res.diagnostics["land_base_post_cap"]


def test_lycd_hierarchy_fallback_and_knn():
    gdf, gma, pin_areas = _toy_city(n_per_zone=30)
    # 30 improved per L3 < min_improved=50 -> both zones fall back to the pooled L2 median
    res = compute_lycd_land_values(gdf, gma, pin_areas, min_improved=50, city_area_sqft=1e9)
    out = res.gdf.set_index("parcel_number")
    assigned = out[out.gma3.notna() & (out.category_code == "1") & (out.total_area > 0)]
    assert set(assigned.gma_level) == {"L2"}
    assert assigned.lycd_land_value.nunique() <= 2   # pooled rate x area (cap may bind on one)
    # unassigned parcel gets the median RATE of its neighbours, applied to its own area
    knn = out[out.gma3.isna()]
    assert (knn.gma_level == "knn").all()
    assert knn.lycd_land_value.notna().all()
    assert res.diagnostics["n_land_knn"] == 1


def test_lycd_area_guard_raises():
    gdf, gma, pin_areas = _toy_city()
    with pytest.raises(ValueError, match="inflated"):
        compute_lycd_land_values(gdf, gma, pin_areas, city_area_sqft=1.0)


def test_lycd_land_pct_improved_as_series():
    """A per-parcel share, as model_lycd_refined_prototype.ipynb passes an FHFA tract share."""
    gdf, gma, pin_areas = _toy_city()
    # Zone A's improved parcels get 0.30, zone B's get 0.10; every vacant row gets a
    # deliberately wrong 0.99, to confirm land_pct_vacant overrides it regardless.
    share = pd.Series(
        np.where(gdf["category_code"] == "6", 0.99,
                 np.where(gdf["zone"] == "A", 0.30, 0.10)),
        index=gdf.index,
    )
    res = compute_lycd_land_values(gdf, gma, pin_areas, land_pct_improved=share,
                                   min_improved=50, city_area_sqft=1e9)
    out = res.gdf.set_index("parcel_number")

    a_imp = out[(out.gma3 == "A") & (out.category_code == "1") & (out.total_area > 0)]
    assert np.allclose(a_imp.lycd_land_value, 100.0 * 0.30 * 1000.0)

    b_imp = out[(out.gma3 == "B") & (out.category_code == "1") & (out.total_area > 0)]
    assert np.allclose(b_imp.lycd_land_value, 200.0 * 0.10 * 1000.0)

    # Vacant rows ignore the 0.99 in the Series -- land_pct_vacant (default 1.00) still wins.
    vac_a = out[(out.gma3 == "A") & (out.category_code == "6")]
    assert np.allclose(vac_a.lycd_land_value, 100.0 * 500.0)
    assert res.diagnostics["land_pct_improved"].startswith("per-parcel Series")


def _toy_city_two_categories(n_per_group: int = 30, area: float = 500.0):
    """One GMA zone with two co-located property types at different rates: category '1'
    ("Residential") at $100/sqft, category '4' ("NonResidential") at $300/sqft. Every parcel
    in a group shares the identical psf, so any member can serve as the read-off probe."""
    rows = []
    k = 0
    for code, psf in (("1", 100.0), ("4", 300.0)):
        for i in range(n_per_group):
            rows.append(dict(parcel_number=f"{k:09d}", pin=str(3000 + k), category_code=code,
                             total_area=area, market_value=psf * area, taxable_building=0.0,
                             x=float(i), y=0.0))
            k += 1
    df = pd.DataFrame(rows)
    gdf = gpd.GeoDataFrame(df.drop(columns=["x", "y"]),
                           geometry=[Point(x, y) for x, y in zip(df.x, df.y)], crs="EPSG:3857")
    gma = pd.DataFrame({"key": df.parcel_number, "gma3": "A", "gma2": "L2", "gma1": "L1"})
    pin_areas = pd.DataFrame({"pin": df.pin, "pin_area_sqft": df.total_area})
    return gdf, gma, pin_areas


def test_lycd_zone_group_col_stratifies_zone_median():
    """model_lycd_refined_prototype.ipynb's Q2 refinement: price a rowhouse against other
    residential comps, not whichever commercial parcel happens to share its GMA zone."""
    gdf, gma, pin_areas = _toy_city_two_categories()
    gdf["_zone_group"] = np.where(gdf["category_code"] == "1", "Residential", "NonResidential")

    pooled = compute_lycd_land_values(gdf, gma, pin_areas, min_improved=10, city_area_sqft=1e9)
    stratified = compute_lycd_land_values(gdf, gma, pin_areas, min_improved=10, city_area_sqft=1e9,
                                          zone_group_col="_zone_group")

    pooled_out = pooled.gdf.set_index("parcel_number")
    strat_out = stratified.gdf.set_index("parcel_number")
    resid_pid = gdf.loc[gdf.category_code == "1", "parcel_number"].iloc[0]
    nonresid_pid = gdf.loc[gdf.category_code == "4", "parcel_number"].iloc[0]

    # Pooled: one blended median (100 and 300 in equal counts -> 200) applied to everyone,
    # so a residential and a non-residential parcel with the same area get the same land value.
    assert np.isclose(pooled_out.loc[resid_pid, "lycd_zone_psf"], 200.0)
    assert np.isclose(pooled_out.loc[resid_pid, "lycd_land_value"],
                      pooled_out.loc[nonresid_pid, "lycd_land_value"])
    assert np.isclose(pooled_out.loc[resid_pid, "lycd_land_value"], 200.0 * 0.20 * 500.0)

    # Stratified: each group sees its own pure rate.
    assert np.isclose(strat_out.loc[resid_pid, "lycd_zone_psf"], 100.0)
    assert np.isclose(strat_out.loc[nonresid_pid, "lycd_zone_psf"], 300.0)
    assert np.isclose(strat_out.loc[resid_pid, "lycd_land_value"], 100.0 * 0.20 * 500.0)
    assert np.isclose(strat_out.loc[nonresid_pid, "lycd_land_value"], 300.0 * 0.20 * 500.0)


def test_lycd_zone_group_col_missing_raises():
    gdf, gma, pin_areas = _toy_city()
    with pytest.raises(ValueError, match="not a column"):
        compute_lycd_land_values(gdf, gma, pin_areas, zone_group_col="nonexistent")


def test_lycd_knn_imputes_rate_not_dollar_value():
    """Audit 2026-08-08 finding 5: an unmatched parcel must get a RATE from its neighbours
    and apply it to its own area, not inherit a neighbour's dollar land value outright."""
    rows = []
    k = 0
    for i in range(60):
        rows.append(dict(parcel_number=f"{k:09d}", pin=str(4000 + k), category_code="1",
                         total_area=10_000.0, market_value=100.0 * 10_000.0, taxable_building=0.0,
                         x=float(i), y=0.0, zone="A"))
        k += 1
    # An unmatched SMALL parcel sitting in the middle of a cluster of LARGE (10,000 sqft) ones.
    small_pid = f"{k:09d}"
    rows.append(dict(parcel_number=small_pid, pin=str(4000 + k), category_code="1",
                     total_area=100.0, market_value=100.0 * 100.0, taxable_building=0.0,
                     x=30.0, y=0.1, zone=None))
    df = pd.DataFrame(rows)
    gdf = gpd.GeoDataFrame(df.drop(columns=["x", "y"]),
                          geometry=[Point(x, y) for x, y in zip(df.x, df.y)], crs="EPSG:3857")
    gma = pd.DataFrame({"key": df.parcel_number, "gma3": df.zone, "gma2": "L2", "gma1": "L1"})
    gma = gma[gma.gma3.notna()]
    pin_areas = pd.DataFrame({"pin": df.pin, "pin_area_sqft": df.total_area})

    res = compute_lycd_land_values(gdf, gma, pin_areas, min_improved=10, city_area_sqft=1e9)
    out = res.gdf.set_index("parcel_number")
    small = out.loc[small_pid]
    assert small.gma_level == "knn"
    # Correct: the neighbourhood's $100/sqft rate x this parcel's OWN 100 sqft x 0.20 share.
    assert np.isclose(small.lycd_land_value, 100.0 * 0.20 * 100.0)
    # The old (wrong) behaviour inherited a neighbour's own land value outright (100 x 0.20 x
    # 10,000 = 200,000) -- two orders of magnitude off this parcel's actual size.
    assert not np.isclose(small.lycd_land_value, 100.0 * 0.20 * 10_000.0)


def test_lycd_cap_include_building_off_by_default():
    """Default behavior (`cap_include_building=False`) is the plain land-only cap, even when
    the parcel carries a building value -- see the parameter's docstring for why turning it
    on is a much bigger change than it looks (it collapses land toward OPA's own split for
    any parcel whose building already accounts for most of market_value)."""
    gdf, gma, pin_areas = _toy_city()
    cheap_idx = gdf.index[gdf["market_value"] == 20_000.0][0]
    gdf.loc[cheap_idx, "taxable_building"] = 5_000.0
    cheap_pid = gdf.loc[cheap_idx, "parcel_number"]

    res = compute_lycd_land_values(gdf, gma, pin_areas, min_improved=50, city_area_sqft=1e9)
    out = res.gdf.set_index("parcel_number")
    assert np.isclose(out.loc[cheap_pid, "lycd_land_value"], 20_000.0)   # unchanged by building
    assert res.diagnostics["cap_include_building"] is False


def test_lycd_cap_include_building_opt_in():
    """With cap_include_building=True, land is capped at market_value - taxable_building,
    so land + building never exceeds market_value."""
    gdf, gma, pin_areas = _toy_city()
    cheap_idx = gdf.index[gdf["market_value"] == 20_000.0][0]
    gdf.loc[cheap_idx, "taxable_building"] = 5_000.0
    cheap_pid = gdf.loc[cheap_idx, "parcel_number"]

    res = compute_lycd_land_values(gdf, gma, pin_areas, min_improved=50, city_area_sqft=1e9,
                                   cap_include_building=True)
    out = res.gdf.set_index("parcel_number")
    cheap = out.loc[cheap_pid]
    # land = market_value - building = 20,000 - 5,000 = 15,000, so land + building lands
    # exactly at market_value, never above it.
    assert np.isclose(cheap.lycd_land_value, 15_000.0)
    assert np.isclose(cheap.lycd_land_value + 5_000.0, 20_000.0)


def _exemption_cases() -> pd.DataFrame:
    cap = 100_000.0
    return pd.DataFrame({
        # 1 plain taxable, 2 homestead (single exemption), 3 abated (building fully exempt),
        # 4 homestead-wiped (mv < cap, fully exempt), 5 institutional (fully exempt),
        # 6 homestead + abatement, 7 homestead where exemption spills onto land
        "taxable_land":     [50_000, 60_000, 40_000,      0,       0, 60_000, 20_000],
        "taxable_building": [150_000, 140_000,     0,      0,       0,      0,      0],
        "exempt_land":      [0,       0,      0, 20_000, 500_000,      0, 30_000],
        "exempt_building":  [0, 100_000, 300_000, 60_000, 900_000, 400_000, 70_000],
        "homestead_exemption": [0, cap, 0, 80_000, 0, cap, cap],
    }).assign(market_value=lambda d: d.taxable_land + d.taxable_building + d.exempt_land + d.exempt_building)


def test_carry_forward_reconstructs_opa_base_from_opa_land():
    df = _exemption_cases()
    df["same_land"] = df.taxable_land + df.exempt_land
    res = carry_forward_exemptions(df, new_land_col="same_land", homestead_cap=100_000)
    assert res.diagnostics["reconstruction_match_rate"] == 1.0
    old = (df.taxable_land + df.taxable_building).clip(lower=0)
    assert np.allclose(res.reform_taxable_total, old)


def test_carry_forward_with_doubled_land():
    df = _exemption_cases()
    df["new_land"] = 2 * (df.taxable_land + df.exempt_land)
    res = carry_forward_exemptions(df, new_land_col="new_land", homestead_cap=100_000)
    tot = res.reform_taxable_total.values
    # 1 plain: land doubles, building unchanged
    assert tot[0] == pytest.approx(100_000 + 150_000)
    # 2 homestead: 120k land + 240k building - 100k cap
    assert tot[1] == pytest.approx(120_000 + 240_000 - 100_000)
    # 3 abated: building stays fully exempt, land doubles
    assert tot[2] == pytest.approx(80_000)
    # 4 homestead-wiped: gross 80k -> land 40k + bldg 60k = 100k, minus cap -> 0; still exempt
    assert tot[3] == pytest.approx(0.0)
    assert bool(res.homestead_active.iloc[3]) and not bool(res.institutional_exempt.iloc[3])
    # 5 institutional: stays fully exempt regardless of land
    assert tot[4] == pytest.approx(0.0) and bool(res.institutional_exempt.iloc[4])
    # 6 homestead + abatement: exempt_building 400k = 300k abatement + 100k homestead, so
    #   120k land + 400k bldg - 300k abatement dollars - 100k cap = 120k (building fully shielded)
    assert tot[5] == pytest.approx(120_000 + 400_000 - 300_000 - 100_000)
    assert res.reform_taxable_building.iloc[5] == pytest.approx(0.0)
    # 7 homestead spilling onto land: gross land 50k -> 100k; bldg 70k; minus cap 100k
    assert tot[6] == pytest.approx(100_000 + 70_000 - 100_000)
    assert res.reform_taxable_building.iloc[6] == pytest.approx(0.0)   # building-first


def test_carry_forward_homestead_wiped_reenters_when_land_lifts_it():
    df = _exemption_cases().iloc[[3]].copy()
    df["new_land"] = 90_000.0          # 90k land + 60k building = 150k > cap
    res = carry_forward_exemptions(df, new_land_col="new_land", homestead_cap=100_000)
    assert res.reform_taxable_total.iloc[0] == pytest.approx(50_000.0)
    assert res.diagnostics["n_homestead_wiped_reentering"] == 1


def test_carry_forward_requires_homestead_column():
    df = _exemption_cases().drop(columns=["homestead_exemption"])
    df["new_land"] = 1.0
    with pytest.raises(ValueError, match="homestead_exemption"):
        carry_forward_exemptions(df, new_land_col="new_land", homestead_cap=100_000)


def test_carry_forward_reconstruction_floor_raises():
    df = _exemption_cases()
    df["new_land"] = 1.0
    # a wrong cap breaks the homestead reconstruction on the homestead rows
    with pytest.raises(ValueError, match="reproduces OPA's taxable total"):
        carry_forward_exemptions(df, new_land_col="new_land", homestead_cap=10_000,
                                 reconstruction_floor=0.99)


def test_residual_building_ordinary_parcel():
    df = pd.DataFrame({"market_value": [200_000.0, 100_000.0], "lycd_land": [90_000.0, 150_000.0]})
    out = compute_residual_building_value(df, land_col="lycd_land")
    # 200k - 90k = 110k; 100k - 150k would be negative -> floored at 0
    assert np.allclose(out.values, [110_000.0, 0.0])


def test_carry_forward_override_prevents_double_counting():
    """The actual point of this mechanism: land + building after exemptions never exceeds
    market_value on ordinary (non-abated) rows, once building is the exemption-aware residual
    -- unlike holding building at OPA's own recorded value, which overshoots on several."""
    df = _exemption_cases()
    # Row 2 (abated) is excluded from this claim entirely -- see
    # test_carry_forward_override_cannot_preserve_abated_building for why routing it through
    # this same call, even with an override, is wrong regardless of the override's value.
    non_abated = df.drop(index=2)
    # Big enough to force overshoot on most rows if unaddressed, but clipped to each row's own
    # market_value -- mirroring the land-only cap compute_lycd_land_values always applies
    # upstream by default. Without that clip, land itself could exceed market_value (row 3's
    # market_value is only $80k), which this mechanism does not and cannot fix -- it only
    # corrects BUILDING; land staying within market_value is a precondition, not a guarantee
    # this function provides.
    non_abated = non_abated.assign(new_land=np.minimum(200_000.0, non_abated.market_value.values))

    # Baseline: building held at OPA's own value (no override) -- reproduces the overshoot.
    baseline = carry_forward_exemptions(non_abated, new_land_col="new_land", homestead_cap=100_000)
    over_baseline = (baseline.reform_taxable_total - non_abated.market_value) > 1
    assert over_baseline.any(), "fixture should reproduce the overshoot without the override"

    override = compute_residual_building_value(non_abated, land_col="new_land")
    fixed = carry_forward_exemptions(non_abated, new_land_col="new_land", homestead_cap=100_000,
                                     gross_building_override=override)
    over_fixed = (fixed.reform_taxable_total - non_abated.market_value) > 1
    assert not over_fixed.any(), "no row should exceed market_value once building is the residual"

    fixed = fixed.reform_taxable_total.reset_index(drop=True)
    # Row 0 (plain): residual building = market - land = 200k - 200k = 0; total = market exactly.
    assert fixed.iloc[0] == pytest.approx(200_000.0)
    # Row 1 (homestead): total = market_value - homestead_cap exactly (relief nets once, not
    # zero or twice, regardless of which building split it's applied to).
    assert fixed.iloc[1] == pytest.approx(300_000.0 - 100_000.0)
    # Row 3 (institutional, was index 4 before dropping row 2): stays fully exempt no matter what.
    assert fixed.iloc[3] == pytest.approx(0.0)


def test_carry_forward_override_cannot_preserve_abated_building():
    """The mistake this guards against: an abated row's building CANNOT be preserved by
    supplying its own observed value (exempt_building) as gross_building_override. The
    function's other_exempt is derived from that SAME row's historical exempt total -- for an
    abated row that IS exempt_building -- so it cancels the override back to exactly zero,
    silently zeroing out the building the four LYCD notebooks currently, correctly, restore.
    Abated rows must be excluded from this call, not routed through it with an override."""
    # index reset to 0 deliberately -- .iloc[[2]] keeps the original label 2, and an override
    # Series that isn't aligned to it would silently reindex to all-NaN/0, masking the actual
    # mechanism this test exists to demonstrate.
    df = _exemption_cases().iloc[[2]].reset_index(drop=True).copy()   # abated: land 40k, exempt_building 300k
    df["new_land"] = 500_000.0   # far above OPA's own 40k -- an LYCD-sized correction

    # An override equal to the row's OWN exempt_building looks like "preserve it" but is not.
    misguided_override = pd.Series([300_000.0], index=df.index)
    result = carry_forward_exemptions(df, new_land_col="new_land", homestead_cap=100_000,
                                      gross_building_override=misguided_override)
    assert result.reform_taxable_building.iloc[0] == pytest.approx(0.0), (
        "this is the trap, not the expectation: supplying exempt_building as the override "
        "still zeroes the building, because other_exempt is derived from that same value"
    )
    assert result.reform_taxable_total.iloc[0] == pytest.approx(500_000.0)   # land only


def test_carry_forward_override_does_not_affect_reconstruction_guard():
    """The guard validates the exemption RULE against OPA's own real numbers and must not be
    fooled by a garbage override -- it always uses OPA's own gross building, never the
    override, which only applies to the real (non-guard) computation."""
    df = _exemption_cases()
    df["same_land"] = df.taxable_land + df.exempt_land
    garbage_override = pd.Series([0.0] * len(df))   # would break every reconstruction if used
    res = carry_forward_exemptions(df, new_land_col="same_land", homestead_cap=100_000,
                                   gross_building_override=garbage_override)
    assert res.diagnostics["reconstruction_match_rate"] == 1.0
    assert res.diagnostics["used_gross_building_override"] is True


# --- reallocate_land_within_total: the assessment-standards reform ------------------------
# Total assessment and rate both unchanged; only the land/building SPLIT moves. The claim
# under test throughout: a bill can only move if its exemption depends on that split.

def _realloc(df, new_land, cap=100_000, **kw):
    return reallocate_land_within_total(
        df.assign(new_land=new_land), new_land_col="new_land", homestead_cap=cap, **kw)


def test_reallocate_reconstructs_opa_base_from_opa_land():
    df = _exemption_cases()
    res = _realloc(df, (df.taxable_land + df.exempt_land).values)
    assert res.diagnostics["reconstruction_match_rate"] == 1.0
    old = (df.taxable_land + df.taxable_building).clip(lower=0)
    assert np.allclose(res.alloc_taxable_total, old)
    assert res.diagnostics["n_taxable_changed"] == 0


def test_reallocate_only_split_dependent_exemptions_move():
    """The headline claim of the reform: with the total and the rate held fixed, the only
    parcels whose bill moves are those whose exemption is a share of the BUILDING line --
    the abated ones. Plain, homestead-only, homestead-wiped and institutional rows are all
    bit-for-bit unchanged no matter how far the land component moves."""
    df = _exemption_cases()
    res = _realloc(df, 2 * (df.taxable_land + df.exempt_land).values)
    old = (df.taxable_land + df.taxable_building).clip(lower=0).values
    new = res.alloc_taxable_total.values

    moved = np.abs(new - old) > 1
    assert list(np.flatnonzero(moved)) == [2, 5], "only the two abated rows may move"
    assert res.diagnostics["n_taxable_changed"] == 2

    # Sec. 3(c): land and improvement stay complementary parts of one unchanged total.
    gross_total = (df.taxable_land + df.exempt_land + df.taxable_building + df.exempt_building)
    assert np.allclose(res.alloc_land + res.alloc_building, gross_total)

    # 0 plain: no exemption at all, so the split is invisible to the bill.
    assert new[0] == pytest.approx(200_000.0)
    # 1 homestead: min(cap, total) with the total unchanged -> unchanged, only the line it is
    #   drawn against moves.
    assert new[1] == pytest.approx(200_000.0)
    # 3 homestead-wiped: total 80k still under the 100k cap, so it stays exempt -- unlike the
    #   revaluation reading, where a bigger land value can lift it back into the base.
    assert new[3] == pytest.approx(0.0)
    # 4 institutional: fully exempt regardless.
    assert new[4] == pytest.approx(0.0)
    # 6 homestead spilling onto land: total unchanged, so unchanged.
    assert new[6] == pytest.approx(20_000.0)


def test_reallocate_fully_abated_taxable_is_land_only():
    """A 100%-abated parcel exempts whatever the improvement is now assessed at, so its
    taxable value is exactly the reallocated land component -- raise the land share and it
    pays more. This is the ordinance's fiscal question, in one row."""
    df = _exemption_cases()
    res = _realloc(df, 2 * (df.taxable_land + df.exempt_land).values)
    # row 2: gross total 340k, land doubled 40k -> 80k, so building 260k, all abated.
    assert res.alloc_land.iloc[2] == pytest.approx(80_000.0)
    assert res.alloc_building.iloc[2] == pytest.approx(260_000.0)
    assert res.alloc_taxable_building.iloc[2] == pytest.approx(0.0)
    assert res.alloc_taxable_total.iloc[2] == pytest.approx(res.alloc_land.iloc[2])
    # ...and it moves in the other direction too, which is why a fiscal note needs both.
    lower = _realloc(df, 0.5 * (df.taxable_land + df.exempt_land).values)
    assert lower.alloc_taxable_total.iloc[2] == pytest.approx(20_000.0)


def test_reallocate_partial_abatement_carries_as_a_share():
    """Row 5 is 75% abated (300k of a 400k building). The share, not the dollar amount, is
    what carries -- so the exemption tracks the new building line."""
    df = _exemption_cases()
    res = _realloc(df, 2 * (df.taxable_land + df.exempt_land).values)
    # building 340k after reallocation; 75% of it exempt = 255k, leaving 85k taxable, then the
    # 100k homestead takes all 85k and spills 15k onto the 120k land line -> 105k.
    assert res.alloc_building.iloc[5] == pytest.approx(340_000.0)
    assert res.alloc_taxable_total.iloc[5] == pytest.approx(105_000.0)
    assert res.exemption_kind.iloc[5] == "building_share"

    # Same 75% abatement without a homestead on top, so the abated dollars are directly
    # visible rather than buried under the homestead's bite.
    clean = pd.DataFrame({
        "taxable_land": [60_000.0], "taxable_building": [100_000.0],
        "exempt_land": [0.0], "exempt_building": [300_000.0], "homestead_exemption": [0.0],
    })   # gross: land 60k, building 400k, total 460k, 75% abated
    out = _realloc(clean, [120_000.0])
    assert out.alloc_building.iloc[0] == pytest.approx(340_000.0)
    exempted = out.alloc_building.iloc[0] - out.alloc_taxable_building.iloc[0]
    assert exempted == pytest.approx(0.75 * 340_000.0)
    assert out.alloc_taxable_total.iloc[0] == pytest.approx(120_000.0 + 85_000.0)


def test_reallocate_partial_institutional_carries_in_dollars():
    """Row 4's exemption (1.4M) exceeds its building line (900k), so it is relief against
    total value, not against the improvement -- it carries in dollars and, the total being
    unchanged, the parcel stays fully exempt."""
    df = _exemption_cases()
    res = _realloc(df, 2 * (df.taxable_land + df.exempt_land).values)
    assert res.exemption_kind.iloc[4] == "full"       # fully exempt today, so 'full' wins
    assert bool(res.institutional_exempt.iloc[4])
    assert res.alloc_taxable_total.iloc[4] == pytest.approx(0.0)

    # A partially-relieved (not fully exempt) version of the same shape: exemption exceeds the
    # building line, so it is total_share and the bill does not move.
    partial = pd.DataFrame({
        "taxable_land": [100_000.0], "taxable_building": [50_000.0],
        "exempt_land": [200_000.0], "exempt_building": [150_000.0],
        "homestead_exemption": [0.0],
    })
    out = _realloc(partial, [300_000.0])
    assert out.exemption_kind.iloc[0] == "total_share"
    assert out.alloc_taxable_total.iloc[0] == pytest.approx(150_000.0)   # unchanged


def test_reallocate_prorata_relief_is_not_an_abatement():
    """Relief split across BOTH lines in the parcel's own land/building ratio is a percentage
    of total value, not an exemption of the improvement -- even though the dollars fit inside
    the building line. Real TY2026 data has 345 of these; classifying them as building_share
    would make their relief track a building value that the reform deliberately moves."""
    prorata = pd.DataFrame({           # 50% relief, applied 20/80 across land and building
        "taxable_land": [35_180.0], "taxable_building": [140_720.0],
        "exempt_land": [35_180.0], "exempt_building": [140_720.0],
        "homestead_exemption": [0.0],
    })                                  # gross land 70,360 / building 281,440, total 351,800
    res = _realloc(prorata, [140_000.0])
    assert res.diagnostics["n_prorata_reclassified"] == 1
    assert res.exemption_kind.iloc[0] == "total_share"
    assert res.reform_change.iloc[0] == pytest.approx(0.0), "relief on total value cannot move"

    # The homestead spilling onto land is NOT this pattern -- it must stay building_share.
    abated_with_spill = pd.DataFrame({
        "taxable_land": [20_000.0], "taxable_building": [0.0],
        "exempt_land": [58_900.0], "exempt_building": [235_600.0],
        "homestead_exemption": [100_000.0],
    })   # abatement 194,500 of a 235,600 building; the 100k homestead takes the rest and
         # spills 58,900 onto land -- exactly the exempt_land OPA records.
    out = _realloc(abated_with_spill, [120_000.0])
    assert out.diagnostics["n_prorata_reclassified"] == 0
    assert out.exemption_kind.iloc[0] == "building_share"


def test_reallocate_reform_change_excludes_reconstruction_residual():
    """A parcel whose recorded homestead disagrees with the relief actually applied cannot be
    reproduced by the rule -- and its bill does not depend on the land/building split, so the
    reform must report zero for it. Differencing against OPA's recorded total instead would
    report the rule's own residual as a tax change (1,818 such parcels on TY2026)."""
    # homestead_exemption records the full 100k cap, but only 25,240 was actually applied.
    odd = pd.DataFrame({
        "taxable_land": [75_720.0], "taxable_building": [277_640.0],
        "exempt_land": [0.0], "exempt_building": [25_240.0],
        "homestead_exemption": [100_000.0],
    })
    # floor lifted because this one-row frame IS the mismatch case under test; on the real
    # roll these are 0.5% of parcels and the 95% floor holds comfortably.
    res = _realloc(odd, [200_000.0], reconstruction_floor=0.0)
    assert res.diagnostics["n_reconstruction_mismatch"] == 1
    # Against OPA's recorded total this looks like a change; against the rule itself it is zero.
    recorded_delta = res.alloc_taxable_total.iloc[0] - (odd.taxable_land + odd.taxable_building).iloc[0]
    assert abs(recorded_delta) > 1
    assert res.reform_change.iloc[0] == pytest.approx(0.0)
    assert res.diagnostics["n_taxable_changed"] == 0
    assert res.diagnostics["n_taxable_changed_vs_recorded"] == 1


def test_reallocate_vacant_parcel_never_moves():
    """Vacant land has no building line to reallocate away from, so the reform cannot touch
    it -- which is why LYCD's badly over-valued vacant leg is irrelevant to this scenario."""
    vacant = pd.DataFrame({
        "taxable_land": [30_000.0], "taxable_building": [0.0],
        "exempt_land": [0.0], "exempt_building": [0.0], "homestead_exemption": [0.0],
    })
    res = _realloc(vacant, [500_000.0])       # LYCD-sized over-valuation
    assert res.alloc_land.iloc[0] == pytest.approx(30_000.0)
    assert res.alloc_building.iloc[0] == pytest.approx(0.0)
    assert res.alloc_taxable_total.iloc[0] == pytest.approx(30_000.0)
    assert res.diagnostics["n_taxable_changed"] == 0


def test_reallocate_caps_land_at_the_parcel_total():
    """A land component above the parcel's own total assessment is capped there (the
    structure is then implicitly worth zero); the total, and an unexempted bill, still do
    not move."""
    df = _exemption_cases()
    res = _realloc(df, np.full(len(df), 10_000_000.0))
    gross_total = (df.taxable_land + df.exempt_land + df.taxable_building + df.exempt_building)
    assert np.allclose(res.alloc_land, gross_total)
    assert np.allclose(res.alloc_building, 0.0)
    assert res.diagnostics["n_land_capped"] == len(df)
    assert res.alloc_taxable_total.iloc[0] == pytest.approx(200_000.0)   # plain row unmoved


def test_reallocate_exemption_kinds():
    df = _exemption_cases()
    res = _realloc(df, (df.taxable_land + df.exempt_land).values)
    assert list(res.exemption_kind) == [
        "none", "homestead", "building_share", "homestead", "full", "building_share", "homestead",
    ]
    assert res.diagnostics["exemption_kind_counts"]["building_share"] == 2


def test_reallocate_reconstruction_floor_raises():
    df = _exemption_cases()
    with pytest.raises(ValueError, match="reproduces OPA's taxable total"):
        _realloc(df, (df.taxable_land + df.exempt_land).values,
                 cap=10_000, reconstruction_floor=0.99)


def test_reallocate_requires_homestead_column():
    df = _exemption_cases().drop(columns=["homestead_exemption"])
    with pytest.raises(ValueError, match="homestead_exemption"):
        _realloc(df, 1.0)


# --- homestead_order: which line the Homestead Exemption comes off -------------------------
# Irrelevant at one rate, decisive under a split rate. 53 Pa.C.S. Sec. 8583(c) says building
# first; the other orders are what an amendment could say.

def _homestead_home(land, building, cap=100_000.0):
    """One homestead parcel as OPA records it today: the exclusion off the building first."""
    off_building = min(cap, building)
    return pd.DataFrame({
        "taxable_land": [land - (cap - off_building)], "taxable_building": [building - off_building],
        "exempt_land": [cap - off_building], "exempt_building": [off_building],
        "homestead_exemption": [cap],
    })


def _split(order, land, building, **kw):
    res = _realloc(_homestead_home(land, building), [land], homestead_order=order, **kw)
    return res.alloc_taxable_land.iloc[0], res.alloc_taxable_building.iloc[0]


def test_homestead_order_moves_only_the_split():
    """$60k land, $190k building, $100k exclusion: the same $150k is taxable under every order;
    only the line it sits on changes."""
    assert _split("building_first", 60_000, 190_000) == pytest.approx((60_000, 90_000))
    assert _split("land_first", 60_000, 190_000) == pytest.approx((0, 150_000))
    assert _split("value_share", 60_000, 190_000) == pytest.approx((36_000, 114_000))       # 24% off land
    w = 4 * 60_000 / (4 * 60_000 + 190_000)                                               # land's tax share at 4:1
    assert _split("tax_share", 60_000, 190_000, homestead_rate_ratio=4.0) == pytest.approx(
        (60_000 - w * 100_000, 190_000 - (1 - w) * 100_000))


def test_homestead_order_spills_what_a_line_cannot_absorb():
    """Land-first and tax-share both want more than $50k of land can give; the rest comes off the
    building, and the amount exempted is still the whole $100k."""
    assert _split("land_first", 50_000, 100_000) == pytest.approx((0, 50_000))
    assert _split("tax_share", 50_000, 100_000, homestead_rate_ratio=4.0) == pytest.approx((0, 50_000))
    assert _split("building_first", 50_000, 100_000) == pytest.approx((50_000, 0))


def test_value_share_cuts_the_bill_by_cap_over_value_at_any_rates():
    land, building = _split("value_share", 60_000, 190_000)
    for land_mills, building_mills in ((25.5, 6.4), (14.0, 14.0), (40.0, 2.0)):
        gross_bill = 60_000 * land_mills + 190_000 * building_mills
        assert land * land_mills + building * building_mills == pytest.approx((1 - 100_000 / 250_000) * gross_bill)


def test_homestead_order_leaves_totals_counts_and_guard_unchanged():
    """The order is invisible at one rate: taxable totals, reform_change, the change count and the
    reconstruction guard are identical under every order, on every kind of exemption."""
    df = _exemption_cases()
    base = _realloc(df, 2 * (df.taxable_land + df.exempt_land).values)
    assert base.diagnostics["homestead_order"] == "building_first"
    for order in HOMESTEAD_ORDERS:
        res = _realloc(df, 2 * (df.taxable_land + df.exempt_land).values,
                       homestead_order=order, homestead_rate_ratio=4.0)
        assert np.allclose(res.alloc_taxable_total, base.alloc_taxable_total)
        assert np.allclose(res.reform_change, base.reform_change)
        assert res.diagnostics["n_taxable_changed"] == base.diagnostics["n_taxable_changed"]
        assert res.diagnostics["reconstruction_match_rate"] == 1.0
        assert res.diagnostics["homestead_order"] == order


def test_default_homestead_order_is_statutory():
    df = _exemption_cases()
    new_land = 2 * (df.taxable_land + df.exempt_land).values
    default, explicit = _realloc(df, new_land), _realloc(df, new_land, homestead_order="building_first")
    assert np.array_equal(default.alloc_taxable_land, explicit.alloc_taxable_land)
    assert np.array_equal(default.alloc_taxable_building, explicit.alloc_taxable_building)


def test_carry_forward_takes_the_homestead_order_too():
    df = _homestead_home(60_000, 190_000).assign(new_land=60_000.0, market_value=250_000.0)
    res = carry_forward_exemptions(df, new_land_col="new_land", homestead_cap=100_000, homestead_order="land_first")
    assert (res.reform_taxable_land.iloc[0], res.reform_taxable_building.iloc[0]) == pytest.approx((0, 150_000))


def test_uncap_bare_land_values_bare_lots_at_the_sales_estimate():
    """A bare lot takes its sales-based land whole, up or down, with no phantom building line and
    its own relief carried in dollars; a parcel with a building is left exactly as re-split."""
    df = pd.DataFrame({
        #                 bare, up   built   bare+relief  bare, down
        "taxable_land":     [50_000, 60_000, 30_000, 50_000],
        "taxable_building": [0.0, 140_000, 0.0, 0.0],
        "exempt_land":      [0.0, 0.0, 20_000, 0.0],
        "exempt_building":  [0.0, 0.0, 0.0, 0.0],
        "homestead_exemption": [0.0, 0.0, 0.0, 0.0],
        "s5_land":          [80_000, 90_000, 80_000, 30_000],
    })
    alloc = reallocate_land_within_total(df, new_land_col="s5_land", homestead_cap=100_000)
    assert alloc.alloc_building.iloc[3] == pytest.approx(20_000)       # the phantom line being removed
    land, building, bare = uncap_bare_land(alloc, df, np.ones(len(df), bool))
    assert list(bare) == [True, False, True, True]
    assert land == pytest.approx([80_000, 90_000, 60_000, 30_000])
    assert building == pytest.approx([0, 110_000, 0, 0])
    # a parcel that pays no tax is never touched
    land_x, _, bare_x = uncap_bare_land(alloc, df, np.array([False, True, True, True]))
    assert not bare_x[0] and land_x[0] == pytest.approx(alloc.alloc_taxable_land.iloc[0])


def test_homestead_order_is_validated():
    df = _homestead_home(60_000, 190_000)
    with pytest.raises(ValueError, match="homestead_order must be one of"):
        _realloc(df, [60_000], homestead_order="pro_rata")
    with pytest.raises(ValueError, match="rate_ratio"):
        _realloc(df, [60_000], homestead_order="tax_share")


# --- paint_land_surface: an external $/sqft surface onto LVTShift's own parcels ------------

def _painted_city():
    """The toy city with LYCD already run (so dor_area_sqft exists), plus a surface that
    covers zone A only -- zone B is the 'not in the AVM universe' cohort."""
    gdf, gma, pin_areas = _toy_city()
    gdf = compute_lycd_land_values(gdf, gma, pin_areas).gdf
    # OPA's gross building line: four-fifths of value on the improved parcels, none on the vacant lots.
    gdf["opa_gross_building"] = np.where(gdf["category_code"].eq("6"), 0.0, 0.8 * gdf["market_value"])
    in_a = gdf["zone"].eq("A")
    surface = pd.DataFrame({
        # un-padded keys on purpose: the join must normalise to 9 digits
        "parcel_number": gdf.loc[in_a, "parcel_number"].str.lstrip("0"),
        "s2_k20": 7.5,
    })
    return gdf, surface


def test_paint_surface_uses_own_area_and_caps_improved():
    gdf, surface = _painted_city()
    res = paint_land_surface(gdf, surface, rate_col="s2_k20", knn_k=3)
    d = res.diagnostics
    assert d["n_matched"] == int(gdf["zone"].eq("A").sum())
    assert d["n_knn"] == int((~gdf["zone"].eq("A")).sum())
    # A matched improved parcel: rate x ITS OWN area, and the cap does not bind here
    # (7.5 x 1000 = 7,500 < market 100,000).
    a = gdf[gdf["zone"].eq("A") & gdf["category_code"].eq("1")].index[0]
    assert res.land_value[a] == pytest.approx(7.5 * gdf.loc[a, "dor_area_sqft"])
    assert res.source[a] == "surface"


def test_paint_surface_knn_fills_rate_not_dollars():
    gdf, surface = _painted_city()
    res = paint_land_surface(gdf, surface, rate_col="s2_k20", knn_k=3)
    # Zone B parcels are unmatched; they take the neighbours' RATE (7.5, the only rate that
    # exists) and multiply by their own area -- the vacant 500 sqft lot must not inherit a
    # 1,000 sqft neighbour's dollar value.
    b_vac = gdf[gdf["zone"].eq("B") & gdf["category_code"].eq("6")].index[0]
    assert res.source[b_vac] == "knn"
    assert res.psf[b_vac] == pytest.approx(7.5)
    assert res.land_value[b_vac] == pytest.approx(7.5 * 500.0)


def test_paint_surface_cap_binds_on_improved_only():
    gdf, surface = _painted_city()
    surface["s2_k20"] = 1_000.0      # $1,000/sqft: 1,000 sqft lot -> $1M land, market is $100k
    res = paint_land_surface(gdf, surface, rate_col="s2_k20", knn_k=3)
    a_imp = gdf[gdf["zone"].eq("A") & gdf["category_code"].eq("1")].index[0]
    a_vac = gdf[gdf["zone"].eq("A") & gdf["category_code"].eq("6")].index[0]
    assert res.land_value[a_imp] == pytest.approx(gdf.loc[a_imp, "market_value"])
    assert res.land_value[a_vac] == pytest.approx(1_000.0 * 500.0)    # vacant uncapped
    assert res.diagnostics["n_land_exceeds_total"] >= 1
    assert res.diagnostics["n_capped"] == res.diagnostics["n_land_exceeds_total"]
    off = paint_land_surface(gdf, surface, rate_col="s2_k20", knn_k=3, cap_improved_at_market=False)
    assert off.diagnostics["n_capped"] == 0
    assert off.land_value[a_imp] == pytest.approx(1_000.0 * 1_000.0)


def test_paint_surface_bare_is_opas_building_line_not_the_category_code():
    gdf, surface = _painted_city()
    surface["s2_k20"] = 1_000.0      # land far above every total, so the cap binds wherever it applies
    a_imp, a_imp2 = gdf[gdf["zone"].eq("A") & gdf["category_code"].eq("1")].index[:2]
    a_vac = gdf[gdf["zone"].eq("A") & gdf["category_code"].eq("6")].index[0]
    gdf.loc[a_imp2, "opa_gross_building"] = 0.0                       # improved code, $0 building: bare
    gdf.loc[a_vac, "opa_gross_building"] = 0.6 * gdf.loc[a_vac, "market_value"]   # vacant code, a building line
    res = paint_land_surface(gdf, surface, rate_col="s2_k20", knn_k=3)
    assert res.land_value[a_imp] == pytest.approx(gdf.loc[a_imp, "market_value"])            # built: capped
    assert res.land_value[a_imp2] == pytest.approx(1_000.0 * gdf.loc[a_imp2, "dor_area_sqft"])  # bare: whole
    assert res.land_value[a_vac] == pytest.approx(gdf.loc[a_vac, "market_value"])            # built: capped
    d = res.diagnostics
    assert d["bare_rule"] == "opa_gross_building <= $1"
    assert d["n_bare_with_improved_code"] == 1 and d["n_built_with_vacant_code"] == 1


def test_paint_surface_category_rule_is_an_explicit_opt_in():
    gdf, surface = _painted_city()
    surface["s2_k20"] = 1_000.0
    a_imp2 = gdf[gdf["zone"].eq("A") & gdf["category_code"].eq("1")].index[1]
    gdf.loc[a_imp2, "opa_gross_building"] = 0.0
    old = paint_land_surface(gdf, surface, rate_col="s2_k20", knn_k=3, building_col=None)
    assert old.land_value[a_imp2] == pytest.approx(gdf.loc[a_imp2, "market_value"])   # the code rule caps it
    assert old.diagnostics["bare_rule"] == "category_code"
    with pytest.raises(KeyError, match="opa_gross_building"):
        paint_land_surface(gdf.drop(columns="opa_gross_building"), surface, rate_col="s2_k20", knn_k=3)


# --- paint_land_surface with a published support edge ---------------------------------------
# The surface says where its sales stop testing it (philly_open_avmkit's
# land_surface_support.json). Beyond that edge a rate is an extrapolation: a filled parcel's
# rate is re-sized to its own lot, and a vacant lot -- the one case nothing caps -- is carried at
# OPA's value in the painted land (audit 2026-09-21, finding 3), with its rate kept so a reform step
# can relevel it instead (relevel_beyond_support, below).
SUPPORT = {"edge_sqft": 10_000.0, "size_elasticity": -0.5,
           "area_column": "painted_area_sqft", "flag_column": "beyond_support"}


def _supported_city():
    gdf, surface = _painted_city()
    gdf["opa_gross_land"] = np.where(gdf["category_code"].eq("6"), gdf["market_value"], 0.2 * gdf["market_value"])
    area = gdf.set_index(gdf["parcel_number"].str.lstrip("0"))["dor_area_sqft"]
    surface["painted_area_sqft"] = surface["parcel_number"].map(area).to_numpy()
    surface["beyond_support"] = False
    return gdf, surface


def test_paint_surface_without_support_is_unchanged_by_the_new_columns():
    gdf, surface = _supported_city()
    before = paint_land_surface(gdf, surface[["parcel_number", "s2_k20"]], rate_col="s2_k20", knn_k=3)
    same = paint_land_surface(gdf, surface, rate_col="s2_k20", knn_k=3)
    assert before.land_value.equals(same.land_value)
    assert not same.beyond_support.any()
    assert same.diagnostics["n_carried_at_opa"] == 0


def test_paint_surface_fill_resizes_the_neighbours_rate_to_the_subjects_own_lot():
    gdf, surface = _supported_city()
    res = paint_land_surface(gdf, surface, rate_col="s2_k20", knn_k=3, support=SUPPORT)
    # Zone B's vacant lot is 500 sqft and unmatched; every donor is a 1,000 sqft zone-A lot at
    # $7.50. Re-sized to the subject: 7.5 x (500/1000) ** -0.5.
    b_vac = gdf[gdf["zone"].eq("B") & gdf["category_code"].eq("6")].index[0]
    assert res.source[b_vac] == "knn"
    assert res.psf[b_vac] == pytest.approx(7.5 * 0.5 ** -0.5)
    assert res.land_value[b_vac] == pytest.approx(7.5 * 0.5 ** -0.5 * 500.0)
    # A same-sized unmatched neighbour is not re-sized at all.
    b_imp = gdf[gdf["zone"].eq("B") & gdf["category_code"].eq("1") & gdf["dor_area_sqft"].eq(1000.0)].index[0]
    assert res.psf[b_imp] == pytest.approx(7.5)


def test_paint_surface_carries_a_vacant_tract_beyond_the_edge_at_opa():
    gdf, surface = _supported_city()
    b_vac = gdf[gdf["zone"].eq("B") & gdf["category_code"].eq("6")].index[0]
    b_imp = gdf[gdf["zone"].eq("B") & gdf["category_code"].eq("1")].index[0]
    gdf.loc[[b_vac, b_imp], "dor_area_sqft"] = 5_000_000.0        # two 115-acre parcels
    gdf.loc[b_imp, "market_value"] = 3_000_000.0
    res = paint_land_surface(gdf, surface, rate_col="s2_k20", knn_k=3, support=SUPPORT)
    assert res.beyond_support[b_vac] and res.beyond_support[b_imp]
    # The vacant one keeps OPA's value, not rate x 5M sqft, and says so.
    assert res.source[b_vac] == "opa_beyond_support"
    assert res.land_value[b_vac] == pytest.approx(gdf.loc[b_vac, "opa_gross_land"])
    # The improved one keeps its painted land, which the cap bounds at its total.
    assert res.source[b_imp] == "knn"
    assert res.land_value[b_imp] == pytest.approx(min(res.psf[b_imp] * 5_000_000.0, 3_000_000.0))
    d = res.diagnostics
    assert d["n_carried_at_opa"] == 1 and d["carried_value"] == pytest.approx(gdf.loc[b_vac, "opa_gross_land"])
    assert d["surface_value_on_carried"] == pytest.approx(res.psf[b_vac] * 5_000_000.0)


def test_paint_surface_carries_a_bare_lot_with_an_improved_code_beyond_the_edge():
    gdf, surface = _supported_city()
    b = gdf[gdf["zone"].eq("B") & gdf["category_code"].eq("1")].index[0]
    gdf.loc[b, "dor_area_sqft"] = 5_000_000.0
    gdf.loc[b, ["opa_gross_building", "opa_gross_land"]] = [0.0, gdf.loc[b, "market_value"]]
    res = paint_land_surface(gdf, surface, rate_col="s2_k20", knn_k=3, support=SUPPORT)
    # Bare on OPA's split, so neither capped nor taken at an extrapolated rate: carried at OPA.
    assert res.source[b] == "opa_beyond_support"
    assert res.land_value[b] == pytest.approx(gdf.loc[b, "opa_gross_land"])


def test_paint_surface_honours_the_surfaces_own_flag_and_skips_flagged_donors():
    gdf, surface = _supported_city()
    # Flag every donor but one, and give that one a distinctive rate: the fill must use it alone.
    surface["beyond_support"] = True
    keep = surface.index[5]
    surface.loc[keep, ["beyond_support", "s2_k20"]] = [False, 3.0]
    res = paint_land_surface(gdf, surface, rate_col="s2_k20", knn_k=3, support=SUPPORT)
    b_imp = gdf[gdf["zone"].eq("B") & gdf["category_code"].eq("1") & gdf["dor_area_sqft"].eq(1000.0)].index[0]
    assert res.psf[b_imp] == pytest.approx(3.0)
    # A matched vacant lot the SURFACE flags is carried even though its own lot is small.
    a_vac = gdf[gdf["zone"].eq("A") & gdf["category_code"].eq("6")].index[0]
    assert res.source[a_vac] == "opa_beyond_support"
    assert res.land_value[a_vac] == pytest.approx(gdf.loc[a_vac, "opa_gross_land"])


def test_paint_surface_with_support_requires_the_surfaces_columns():
    gdf, surface = _supported_city()
    with pytest.raises(KeyError):
        paint_land_surface(gdf, surface[["parcel_number", "s2_k20"]], rate_col="s2_k20", support=SUPPORT)


def test_uncap_bare_land_carries_opa_on_a_bare_lot_beyond_the_support_edge():
    """The carry_opa, opa_relevelled and surface readings of a bare lot the land sales cannot test:
    the sensitivities beside the relevelled reading. Within the edge, and with no edge published,
    nothing changes."""
    df = pd.DataFrame({
        #                  bare, beyond  bare, within  built, beyond
        "taxable_land":     [100_000.0, 100_000.0, 40_000.0],
        "taxable_building": [0.0, 0.0, 160_000.0],
        "exempt_land":      [0.0, 0.0, 0.0],
        "exempt_building":  [0.0, 0.0, 0.0],
        "homestead_exemption": [0.0, 0.0, 0.0],
        "s5_land":          [900_000.0, 900_000.0, 90_000.0],
    })
    alloc = reallocate_land_within_total(df, new_land_col="s5_land", homestead_cap=100_000)
    pays, beyond = np.ones(3, bool), np.array([True, False, True])
    old, _, _ = uncap_bare_land(alloc, df, pays)
    carried, building, bare = uncap_bare_land(alloc, df, pays, beyond_support=beyond)
    assert list(bare) == [True, True, False]
    assert carried[0] == pytest.approx(100_000) and carried[1] == pytest.approx(900_000)
    assert old[0] == pytest.approx(900_000)                 # no edge published: taken whole, as before
    assert carried[2] == old[2] and building[2] > 0         # a flagged parcel WITH a building is untouched
    relevelled, _, _ = uncap_bare_land(alloc, df, pays, beyond_support=beyond, large_tracts="opa_relevelled",
                                       opa_level=0.8)
    assert relevelled[0] == pytest.approx(125_000)
    whole, _, _ = uncap_bare_land(alloc, df, pays, beyond_support=beyond, large_tracts="surface",
                                  uncarried_land=np.array([750_000.0, 1.0, 1.0]))
    assert whole[0] == pytest.approx(750_000) and whole[1] == pytest.approx(900_000)
    with pytest.raises(ValueError, match="opa_level"):
        uncap_bare_land(alloc, df, pays, beyond_support=beyond, large_tracts="opa_relevelled")
    with pytest.raises(ValueError, match="large_tracts must be one of"):
        uncap_bare_land(alloc, df, pays, beyond_support=beyond, large_tracts="keep")



def test_large_lot_band_factor_cuts_bands_as_philly_open_avmkit_does():
    bands = {"edge to 1 acre": 1.1, "1 acre and over": 1.8}
    f = large_lot_band_factor([9_999.0, 10_000.0, 10_001.0, 43_560.0, 43_561.0, np.nan], bands, 10_000.0)
    assert np.isnan(f[:2]).all() and np.isnan(f[5])          # at or inside the edge, or unknown: no factor
    assert list(f[2:5]) == [1.1, 1.1, 1.8]
    with pytest.raises(KeyError, match="1 acre and over"):
        large_lot_band_factor([20_000.0], {"edge to 1 acre": 1.1}, 10_000.0)


def _large_lot_frame():
    return pd.DataFrame({
        #          built, beyond   bare, beyond    bare, within   built, beyond (use-restricted flag, ignored)
        "taxable_land":     [40_000.0, 100_000.0, 50_000.0, 10_000.0],
        "taxable_building": [160_000.0, 0.0, 0.0, 90_000.0],
        "exempt_land":      [0.0, 0.0, 0.0, 0.0],
        "exempt_building":  [0.0, 0.0, 0.0, 0.0],
        "homestead_exemption": [0.0, 0.0, 0.0, 0.0],
        "s5_land":          [200_000.0, 100_000.0, 80_000.0, 100_000.0],   # the export: capped, bare carried
        "s5_uncarried":     [300_000.0, 360_000.0, 80_000.0, 150_000.0],   # rate x area, never capped
    })


def test_relevel_beyond_support_divides_the_uncapped_value_before_the_cap():
    """The factor applies to the rate; the re-split caps afterwards. Dividing the capped value instead
    would take parcel 0 to 200,000 / 1.2 = 166,667 though its relevelled land (250,000) still exceeds
    its 200,000 total."""
    df = _large_lot_frame()
    beyond = np.array([True, True, False, True])
    factor = np.array([1.2, 1.8, np.nan, 1.5])
    r = relevel_beyond_support(df, land_col="s5_land", uncarried_col="s5_uncarried",
                               beyond_support=beyond, relevel_factor=factor)
    assert r.land.tolist() == [pytest.approx(250_000.0), pytest.approx(200_000.0), 80_000.0, pytest.approx(100_000.0)]
    assert r.relevelled.tolist() == [True, True, False, True]
    alloc = reallocate_land_within_total(df.assign(reform_land=r.land), new_land_col="reform_land", homestead_cap=0)
    assert alloc.alloc_land[0] == pytest.approx(200_000.0)            # capped at the total, after the relevel
    assert r.diagnostics["n_beyond_support"] == 3
    assert r.diagnostics["parcels_by_factor"] == {1.2: 1, 1.5: 1, 1.8: 1}


def test_relevel_beyond_support_refuses_a_flagged_parcel_without_a_factor():
    df = _large_lot_frame()
    with pytest.raises(ValueError, match="1 parcels beyond the support edge have no relevel factor"):
        relevel_beyond_support(df, land_col="s5_land", uncarried_col="s5_uncarried",
                               beyond_support=np.array([True, True, False, False]),
                               relevel_factor=np.array([1.2, np.nan, np.nan, np.nan]))


def test_uncap_bare_land_relevelled_takes_the_column_whole_except_use_restricted_tracts():
    df = _large_lot_frame()
    beyond = np.array([True, True, False, True])
    r = relevel_beyond_support(df, land_col="s5_land", uncarried_col="s5_uncarried",
                               beyond_support=beyond, relevel_factor=np.array([1.2, 1.8, np.nan, 1.5]))
    df["reform_land"] = r.land
    alloc = reallocate_land_within_total(df, new_land_col="reform_land", homestead_cap=0)
    pays = np.ones(4, bool)
    land, building, bare = uncap_bare_land(alloc, df, pays, "reform_land", beyond_support=beyond,
                                           large_tracts="relevelled", keep_opa=np.zeros(4, bool))
    assert list(bare) == [False, True, True, False]
    assert land[1] == pytest.approx(200_000.0) and building[1] == 0.0   # 360,000 / 1.8, above OPA's 100,000
    assert land[2] == pytest.approx(80_000.0)                            # within the edge: the surface whole
    # a use-restricted bare tract keeps OPA's value; the flag on a built parcel changes nothing
    kept, _, _ = uncap_bare_land(alloc, df, pays, "reform_land", beyond_support=beyond,
                                 large_tracts="relevelled", keep_opa=np.array([False, True, False, True]))
    assert kept[1] == pytest.approx(100_000.0)
    assert kept[3] == land[3] and kept[0] == land[0]
    with pytest.raises(ValueError, match="keep_opa"):
        uncap_bare_land(alloc, df, pays, "reform_land", beyond_support=beyond, large_tracts="relevelled")
    assert LARGE_TRACT_TREATMENTS[0] == "relevelled"

def test_paint_surface_does_not_carry_a_vacant_coded_parcel_opa_values_as_a_building():
    """A vacant-coded lot beyond the edge that OPA books mostly to a building line (a parking lot
    with a booth, parkland) keeps its surface land, capped at its total, like any improved parcel."""
    gdf, surface = _supported_city()
    b_vac = gdf[gdf["zone"].eq("B") & gdf["category_code"].eq("6")].index[0]
    gdf.loc[b_vac, "dor_area_sqft"] = 5_000_000.0
    gdf.loc[b_vac, "opa_gross_land"] = 1_000.0              # market value 5,000: the rest is "building"
    res = paint_land_surface(gdf, surface, rate_col="s2_k20", knn_k=3, support=SUPPORT)
    assert res.beyond_support[b_vac]
    assert res.source[b_vac] == "knn"
    assert res.diagnostics["n_carried_at_opa"] == 0


# --- condo_unit_areas ---------------------------------------------------------------------
# OPA records the unit, DOR the building's lot; the area chain hands a unit the whole lot, a
# sibling's whole lot by KNN, or a 1-sqft placeholder. Each unit takes its share instead.
def _condo_building():
    gdf = pd.DataFrame({
        "parcel_number": ["888000001", "888000002", "888000003", "888000004", "100000001"],
        "dor_area_sqft": [10_000.0, 10_000.0, 1.0, 10_000.0, 2_000.0],
        "area_source": ["pin_dor", "knn", "opa_total_area", "knn", "opa_total_area"],
    })
    condos = pd.DataFrame({
        "parcel_number": ["888000001", "888000002", "888000003", "888000004", "100000001"],
        "is_condo_unit": [True, True, True, True, False],
        "condo_master_pin": ["5000", "5000", "5000", "5000", None],
        "condo_unit_share": [0.5, 0.3, 0.2, 0.0, np.nan],
        "condo_master_lot_sqft": [9_900.0] * 4 + [np.nan],
    })
    pin_areas = pd.DataFrame({"pin": ["5000"], "pin_area_sqft": [10_000.0]})
    return gdf, condos, pin_areas


def test_condo_units_take_their_share_of_this_repos_lot():
    gdf, condos, pin_areas = _condo_building()
    r = condo_unit_areas(gdf, condos, pin_areas)
    # share x the master lot as THIS repo measures it (10,000 by PIN, not the sibling's 9,900)
    assert r.area.tolist()[:3] == pytest.approx([5_000.0, 3_000.0, 2_000.0])
    assert r.area[3] == 0.0 and r.source[3] == "condo_accessory"      # parking stall
    assert r.source[:3].eq("condo_share").all()
    assert r.area[4] == 2_000.0 and r.source[4] == "opa_total_area"   # not a condo: untouched
    assert r.diagnostics["area_after_sqft"] == pytest.approx(r.diagnostics["master_area_sqft"])
    assert r.diagnostics["n_master_fallback"] == 0


def test_condo_master_lot_falls_back_to_the_sibling_repos_measure():
    gdf, condos, _ = _condo_building()
    r = condo_unit_areas(gdf, condos, pd.DataFrame({"pin": ["9"], "pin_area_sqft": [1.0]}))
    assert r.area[0] == pytest.approx(0.5 * 9_900.0)
    assert r.diagnostics["n_master_fallback"] == 4


def test_condo_shares_past_one_raise():
    gdf, condos, pin_areas = _condo_building()
    condos.loc[3, "condo_unit_share"] = 0.4      # the building's land counted 1.4 times
    with pytest.raises(ValueError, match="sum past 1"):
        condo_unit_areas(gdf, condos, pin_areas)


# --- corrected_lot_areas -------------------------------------------------------------------
# The sibling repo replaces an OPA area more than 2x both polygons; LVTShift's own override
# waits for 3x, so the 2-3x band (and borrowed polygons) kept OPA's record under a rate fitted
# on the corrected area.
def _flagged_lots():
    gdf = pd.DataFrame({
        "parcel_number": ["1", "2", "3", "4", "5"],
        "dor_area_sqft": [25_000.0, 9_000.0, 1_200.0, 4_000.0, 3_000.0],
        "area_source": ["opa_total_area", "pin_override", "condo_share", "opa_total_area", "opa_total_area"],
    })
    roll = pd.DataFrame({
        "parcel_number": ["000000001", "000000002", "000000003", "000000004", "000000005"],
        "land_area_sqft": [10_000.0, 9_100.0, 30_000.0, 4_000.0, 1_500.0],
        "land_area_source": ["pwd_dor", "pwd_dor", "pwd_dor", "opa", "pwd_dor"],
    })
    return gdf, roll


def test_flagged_opa_areas_take_the_sibling_repos_polygon():
    gdf, roll = _flagged_lots()
    r = corrected_lot_areas(gdf, roll)
    assert r.area[0] == 10_000.0 and r.source[0] == "pwd_dor"     # 2.5x: LVTShift's 3x rule missed it
    assert r.area[4] == 1_500.0 and r.source[4] == "pwd_dor"
    assert r.area[1] == 9_000.0 and r.source[1] == "pin_override"  # already on this repo's polygon
    assert r.area[2] == 1_200.0 and r.source[2] == "condo_share"   # a unit's share, not the lot
    assert r.area[3] == 4_000.0 and r.source[3] == "opa_total_area"  # not flagged: untouched
    assert r.diagnostics["n_corrected"] == 2
    assert r.diagnostics["n_kept_own_source"] == 2


def test_a_correction_that_grows_a_lot_raises():
    gdf, roll = _flagged_lots()
    roll.loc[0, "land_area_sqft"] = 50_000.0
    with pytest.raises(ValueError, match="GROW"):
        corrected_lot_areas(gdf, roll)


def test_a_flagged_lot_left_inflated_on_another_source_raises():
    gdf, roll = _flagged_lots()
    gdf.loc[1, "dor_area_sqft"] = 30_000.0     # a pin_override still 3.3x the sibling's polygon
    with pytest.raises(ValueError, match="still exceed"):
        corrected_lot_areas(gdf, roll)


def test_a_roll_without_the_area_rule_raises():
    gdf, roll = _flagged_lots()
    with pytest.raises(ValueError, match="predates"):
        corrected_lot_areas(gdf, roll.drop(columns="land_area_source"))
