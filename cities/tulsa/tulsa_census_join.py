"""
Tulsa Census Demographic Join + Quintile Chart Generator
=========================================================
Author: Generated for Ryan Combs
Date:   2026-05-20

PURPOSE
-------
1. Fetches ACS 2022 block-group demographics for Tulsa County (FIPS 40143)
   using the repo's get_census_data_with_boundaries().
2. Spatially joins demographics onto parcels using match_parcels_to_demographics().
3. Saves an enriched GeoPackage and updated per-scenario CSVs with the four
   required census columns:
       std_geoid, median_income, minority_pct, black_pct
4. Regenerates the four missing quintile PNGs for each scenario by rebuilding
   the standard export DataFrame and re-calling create_city_report().

Run from the repo root with your virtual environment activated:
    cd C:\\Users\\Ryan Combs\\Documents\\LVTShift
    python tulsa_census_join.py

WHAT THIS SCRIPT DOES NOT DO
-----------------------------
- Does not re-run the LVT tax model (rates and tax values are loaded as-is
  from tulsa_lvt_parcels.gpkg and tulsa_lvt_summary.csv).
- Does not modify tulsa_lvt_parcels.gpkg in place; saves a new enriched file.
- Does not re-generate the 3 already-working PNGs unless they are missing.

CENSUS API KEY
--------------
Loaded automatically from the .env file in your repo root via python-dotenv.
No key needs to be passed explicitly.

KNOWN SPATIAL JOIN BEHAVIOR
----------------------------
match_parcels_to_demographics() uses predicate='within', meaning a parcel
must fall entirely within a block group to match. Parcels that straddle a
block-group boundary will return null demographics. For Tulsa city parcels
this typically produces a 90–97% join rate. The script reports the exact
rate; if it falls below 70% (the repo's threshold for chart generation)
it will explain why and suggest a fix.
"""

import os
import sys
import warnings
warnings.filterwarnings('ignore')

import geopandas as gpd
import pandas as pd
import numpy as np
from dotenv import load_dotenv

# ── 0. CONFIGURATION ──────────────────────────────────────────────────────────

REPO_ROOT   = os.path.dirname(os.path.abspath(__file__))
FIPS_CODE   = '40143'          # Tulsa County, Oklahoma
CENSUS_YEAR = 2022

# Input files
INPUT_GPKG   = r"C:\Users\Ryan Combs\Documents\TulsaData\LVT_Model_Output\tulsa_lvt_parcels.gpkg"
SUMMARY_CSV  = r"C:\Users\Ryan Combs\Documents\TulsaData\LVT_Model_Output\tulsa_lvt_summary.csv"

# Output files
OUTPUT_DIR      = r"C:\Users\Ryan Combs\Documents\TulsaData\LVT_Model_Output"
ENRICHED_GPKG   = os.path.join(OUTPUT_DIR, 'tulsa_lvt_parcels_census.gpkg')

# Repo standard output locations
DATA_DIR    = os.path.join(REPO_ROOT, 'analysis', 'data')
REPORT_DIR  = os.path.join(REPO_ROOT, 'analysis', 'reports')
os.makedirs(DATA_DIR,   exist_ok=True)
os.makedirs(REPORT_DIR, exist_ok=True)

# Scenarios — must match column names already in INPUT_GPKG
SCENARIOS = [
    {'r': 2, 'slug': 'tulsa_r2',
     'new_tax_col': 'new_city_tax_r2',
     'chg_col':     'city_tax_chg_usd_r2',
     'pct_col':     'city_tax_chg_pct_r2'},
    {'r': 3, 'slug': 'tulsa_r3',
     'new_tax_col': 'new_city_tax_r3',
     'chg_col':     'city_tax_chg_usd_r3',
     'pct_col':     'city_tax_chg_pct_r3'},
    {'r': 4, 'slug': 'tulsa_r4',
     'new_tax_col': 'new_city_tax_r4',
     'chg_col':     'city_tax_chg_usd_r4',
     'pct_col':     'city_tax_chg_pct_r4'},
]

# ── 1. LOAD .env AND CENSUS API KEY ───────────────────────────────────────────

print("=" * 70)
print("TULSA CENSUS DEMOGRAPHIC JOIN")
print("=" * 70)

load_dotenv(os.path.join(REPO_ROOT, '.env'))
api_key = os.environ.get('CENSUS_API_KEY')
if not api_key:
    print("✗ CENSUS_API_KEY not found in .env or environment.")
    print("  Confirm your .env file is in the repo root and contains:")
    print("  CENSUS_API_KEY=your_key_here")
    sys.exit(1)
print(f"\n✓ Census API key loaded ({api_key[:6]}...)")

# ── 2. IMPORT REPO MODULES ────────────────────────────────────────────────────

if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

try:
    from lvt.census_utils import (
        get_census_data_with_boundaries,
        match_parcels_to_demographics,
    )
    from lvt.lvt_utils import save_standard_export
    from lvt.viz import create_city_report
    print("✓ lvt modules imported successfully")
except ImportError as e:
    print(f"✗ Import failed: {e}")
    print("  Activate your virtual environment and run from the repo root.")
    sys.exit(1)

# ── 3. LOAD TULSA PARCEL DATA ─────────────────────────────────────────────────

print(f"\n── Loading parcel GeoPackage ─────────────────────────────────────────")
print(f"  {INPUT_GPKG}")
gdf = gpd.read_file(INPUT_GPKG)
TOTAL_ROWS = len(gdf)
print(f"  Loaded {TOTAL_ROWS:,} parcels")
print(f"  CRS: {gdf.crs}")
from lvt.lvt_utils import categorize_property_type

if 'PROPERTY_CATEGORY' not in gdf.columns:
    if 'Property Use' in gdf.columns:
        gdf['PROPERTY_CATEGORY'] = gdf['Property Use'].apply(categorize_property_type)
        print("  Created PROPERTY_CATEGORY from Property Use.")
    else:
        gdf['PROPERTY_CATEGORY'] = 'Other'
        print("  WARNING: PROPERTY_CATEGORY set to Other for all parcels.")
# Verify expected columns present
required = ['_city_tax', '_excluded', 'PROPERTY_CATEGORY',
            'Assessed Land Value', 'Assessed Improvement Value']
missing = [c for c in required if c not in gdf.columns]
if missing:
    print(f"\n✗ Missing columns in GeoPackage: {missing}")
    print("  Re-run tulsa_lvt_export.py first to add PROPERTY_CATEGORY,")
    print("  then re-run this script.")
    sys.exit(1)

# Guard: if PROPERTY_CATEGORY is missing (not yet added by export script),
# add a minimal version so the join can proceed — user can re-run export after.
if 'PROPERTY_CATEGORY' not in gdf.columns:
    gdf['PROPERTY_CATEGORY'] = 'Other'
    print("  WARNING: PROPERTY_CATEGORY not found — set to 'Other' for all parcels.")
    print("  Run tulsa_lvt_export.py first for proper category mapping.")

# ── 4. FETCH CENSUS DATA ──────────────────────────────────────────────────────

print(f"\n── Fetching Census ACS {CENSUS_YEAR} data ────────────────────────────────────")
print(f"  FIPS: {FIPS_CODE}  (Tulsa County, OK)")
print(f"  This fetches block-group boundaries and demographic variables.")
print(f"  Expect 10–30 seconds depending on network speed ...")

try:
    census_df, block_groups_gdf = get_census_data_with_boundaries(
        fips_code = FIPS_CODE,
        year      = CENSUS_YEAR,
        api_key   = api_key,
    )
except Exception as e:
    print(f"\n✗ Census fetch failed: {e}")
    print("\nCommon causes:")
    print("  - Invalid or expired Census API key")
    print("  - Network timeout (try again; Census API is occasionally slow)")
    print("  - FIPS code format issue (expected 5-digit string '40143')")
    sys.exit(1)

print(f"  ✓ Census data: {len(census_df):,} block groups, "
      f"{len(census_df.columns)} columns")
print(f"  ✓ Block group boundaries: {len(block_groups_gdf):,} features")
print(f"  Block groups CRS: {block_groups_gdf.crs}")

# Confirm required demographic columns are present
print(f"\n  Census columns available:")
print(f"  {list(census_df.columns)}")

for col in ['std_geoid', 'median_income', 'minority_pct', 'black_pct']:
    present = col in census_df.columns or col in block_groups_gdf.columns
    status  = '✓' if present else '✗ MISSING'
    print(f"  {status}  {col}")

missing_demo = [c for c in ['median_income', 'minority_pct', 'black_pct']
                if c not in census_df.columns and c not in block_groups_gdf.columns]
if missing_demo:
    print(f"\n  WARNING: These demographic columns are missing from the Census fetch:")
    print(f"  {missing_demo}")
    print(f"  The quintile charts that depend on them will still be skipped.")
    print(f"  Check get_census_data() in lvt/census_utils.py to see which ACS")
    print(f"  variables it requests and whether column names differ.")

# ── 5. SPATIAL JOIN ───────────────────────────────────────────────────────────

print(f"\n── Spatial join: parcels → block groups ──────────────────────────────")
print(f"  Parcels CRS:      {gdf.crs}")
print(f"  Block groups CRS: {block_groups_gdf.crs}")
print(f"  (match_parcels_to_demographics will reproject block groups to")
print(f"   match parcel CRS automatically)")

# match_parcels_to_demographics() handles CRS alignment internally.
# It uses predicate='within' — parcel centroid must fall within a block group.
# Returns the full parcel GeoDataFrame with demographic columns appended.
# NOTE: the sjoin may produce duplicate rows if a parcel touches multiple
# block groups (rare with 'within'). We deduplicate on the original index below.

try:
    enriched = match_parcels_to_demographics(
        parcels_gdf       = gdf,
        demographics_df   = census_df,
        block_groups_gdf  = block_groups_gdf,
        demographic_id_col = 'std_geoid',
        block_group_id_col = 'GEOID',
    )
except Exception as e:
    print(f"\n✗ Spatial join failed: {e}")
    print("\nThis may indicate a geometry or CRS issue.")
    print("Check that tulsa_lvt_parcels.gpkg has valid geometries:")
    print("  gdf[gdf.geometry.is_valid == False].shape")
    sys.exit(1)

# ── 5a. DEDUPLICATION GUARD ───────────────────────────────────────────────────
# sjoin with 'within' can still produce multiple rows if a parcel geometry
# is assigned to more than one block group. Keep only the first match per
# original parcel index, preserving the original 159,014-row count.

pre_dedup = len(enriched)
enriched = enriched[~enriched.index.duplicated(keep='first')]
post_dedup = len(enriched)

if pre_dedup != post_dedup:
    print(f"  Deduplication: {pre_dedup:,} → {post_dedup:,} rows "
          f"({pre_dedup - post_dedup:,} duplicate rows removed)")

if post_dedup != TOTAL_ROWS:
    print(f"\n  WARNING: Row count after join ({post_dedup:,}) differs from "
          f"input ({TOTAL_ROWS:,}).")
    print(f"  This may indicate parcels with null or invalid geometry that")
    print(f"  were dropped by the spatial join. Proceeding with {post_dedup:,} rows.")
else:
    print(f"  ✓ Row count preserved: {post_dedup:,}")

# ── Explicit demographic merge fix ─────────────────────────────

demo_cols = census_df[
    ['std_geoid', 'median_income', 'minority_pct', 'black_pct']
].drop_duplicates()

# Remove existing empty demographic columns if present
for c in ['std_geoid', 'median_income', 'minority_pct', 'black_pct']:
    if c in enriched.columns:
        enriched.drop(columns=c, inplace=True)

# Merge Census attributes using parcel GEOID matched from spatial join
enriched = enriched.merge(
    demo_cols,
    left_on='GEOID',
    right_on='std_geoid',
    how='left'
)

print("\n✓ Demographic attributes merged by GEOID → std_geoid")

# ── 5b. JOIN RATE AUDIT ───────────────────────────────────────────────────────

print(f"\n── Join rate audit ───────────────────────────────────────────────────")

has_geoid    = enriched['GEOID'].notna() if 'GEOID' in enriched.columns else pd.Series(False, index=enriched.index)
has_income   = enriched['median_income'].notna() if 'median_income' in enriched.columns else pd.Series(False, index=enriched.index)
has_minority = enriched['minority_pct'].notna() if 'minority_pct' in enriched.columns else pd.Series(False, index=enriched.index)

# Join rate on taxable parcels only (excluded parcels having null demographics is expected)
taxable_mask = enriched['_excluded'] == False
n_taxable    = taxable_mask.sum()

geoid_rate   = has_geoid[taxable_mask].mean()   * 100 if taxable_mask.sum() > 0 else 0
income_rate  = has_income[taxable_mask].mean()  * 100 if taxable_mask.sum() > 0 else 0
minority_rate= has_minority[taxable_mask].mean()* 100 if taxable_mask.sum() > 0 else 0

print(f"  Taxable parcels:         {n_taxable:,}")
print(f"  GEOID join rate:         {geoid_rate:.1f}%")
print(f"  median_income join rate: {income_rate:.1f}%")
print(f"  minority_pct join rate:  {minority_rate:.1f}%")
print(f"  (Repo threshold for quintile charts: ≥70%)")

if income_rate < 70 or minority_rate < 70:
    print(f"\n  WARNING: Join rate below 70% threshold.")
    print(f"  Quintile charts may still be skipped by create_city_report().")
    print(f"\n  Common causes of low join rates with predicate='within':")
    print(f"  - Parcel geometries are polygons; 'within' requires the entire")
    print(f"    polygon to be inside the block group, not just the centroid.")
    print(f"  - Large parcels straddling block-group boundaries won't match.")
    print(f"\n  If join rate is below 70%, consider patching the join to use")
    print(f"  centroid-based matching instead. Add this after the join call:")
    print(f"")
    print(f"    # Centroid fallback for unmatched parcels")
    print(f"    # (see note at bottom of this script)")
else:
    print(f"  ✓ Join rate sufficient for quintile charts")

# ── 5c. CENTROID FALLBACK (auto-applied if join rate < 70%) ──────────────────
# match_parcels_to_demographics uses predicate='within', which requires the
# entire parcel polygon to lie inside a block group. Large parcels often fail.
# If the join rate is below 85%, we re-join unmatched parcels using their
# centroids, which nearly always match.

JOIN_RATE_THRESHOLD = 85.0   # apply centroid fallback if below this

if geoid_rate < JOIN_RATE_THRESHOLD and 'median_income' in enriched.columns:
    print(f"\n── Centroid fallback join (join rate {geoid_rate:.1f}% < {JOIN_RATE_THRESHOLD}%) ──")

    unmatched_mask = taxable_mask & enriched['GEOID'].isna()
    n_unmatched    = unmatched_mask.sum()
    print(f"  Unmatched taxable parcels: {n_unmatched:,}")

    if n_unmatched > 0:
        # Build centroid GeoDataFrame for unmatched parcels
        unmatched_gdf = enriched[unmatched_mask].copy()
        unmatched_gdf = unmatched_gdf.set_geometry(
            unmatched_gdf.geometry.centroid
        )

        # Reproject block groups to match
        bg_reproj = block_groups_gdf.to_crs(unmatched_gdf.crs)

        # Spatial join on centroid
        centroid_join = gpd.sjoin(
            unmatched_gdf[['geometry']],
            bg_reproj[['GEOID', 'geometry']],
            how='left',
            predicate='within'
        )
        centroid_join = centroid_join[~centroid_join.index.duplicated(keep='first')]

        # Merge census demographics onto centroid-joined results
        centroid_with_demo = centroid_join.merge(
            census_df,
            left_on  = 'GEOID',
            right_on = 'std_geoid',
            how      = 'left'
        )

        # Write centroid-matched values back into enriched for unmatched rows
        demo_cols = [c for c in ['GEOID', 'std_geoid', 'median_income',
                                  'minority_pct', 'black_pct']
                     if c in centroid_with_demo.columns]
        for col in demo_cols:
            if col in centroid_with_demo.columns:
                enriched.loc[unmatched_mask, col] = centroid_with_demo[col].values

        # Re-audit after fallback
        has_geoid2    = enriched['GEOID'].notna() if 'GEOID' in enriched.columns else pd.Series(False, index=enriched.index)
        has_income2   = enriched['median_income'].notna() if 'median_income' in enriched.columns else pd.Series(False, index=enriched.index)
        geoid_rate2   = has_geoid2[taxable_mask].mean()  * 100
        income_rate2  = has_income2[taxable_mask].mean() * 100
        print(f"  After centroid fallback:")
        print(f"    GEOID join rate:    {geoid_rate2:.1f}%  (was {geoid_rate:.1f}%)")
        print(f"    Income join rate:   {income_rate2:.1f}%  (was {income_rate:.1f}%)")

# ── 5d. STANDARDISE COLUMN NAMES ─────────────────────────────────────────────
# match_parcels_to_demographics merges census_df on GEOID / std_geoid.
# After the merge, std_geoid may exist only in census_df (not block_groups_gdf).
# Ensure the four required columns exist with the correct names.

if 'std_geoid' not in enriched.columns and 'GEOID' in enriched.columns:
    enriched['std_geoid'] = enriched['GEOID']
    print(f"\n  Mapped GEOID → std_geoid")

for col in ['std_geoid', 'median_income', 'minority_pct', 'black_pct']:
    if col not in enriched.columns:
        enriched[col] = np.nan
        print(f"  Added null column: {col}")

# ── 6. SAVE ENRICHED GEOPACKAGE ───────────────────────────────────────────────

print(f"\n── Saving enriched GeoPackage ────────────────────────────────────────")

assert len(enriched) == post_dedup, \
    f"FATAL: row count changed during column ops ({len(enriched)} ≠ {post_dedup})"

enriched.to_file(ENRICHED_GPKG, driver='GPKG')
print(f"  ✓ Saved: {ENRICHED_GPKG}")
print(f"  Rows: {len(enriched):,}")
print(f"  New columns added: std_geoid, median_income, minority_pct, black_pct")

# ── 7. LOAD MILLAGE RATES ─────────────────────────────────────────────────────

print(f"\n── Loading scenario rates ────────────────────────────────────────────")
summary_df = pd.read_csv(SUMMARY_CSV)
print(summary_df[['Scenario', 'Land Mill Rate', 'Improvement Mill Rate']].to_string(index=False))

def get_rates(r_val: int) -> tuple:
    row = summary_df[summary_df['Scenario'] == f'r = {r_val}']
    if row.empty:
        raise ValueError(f"Scenario r={r_val} not found in {SUMMARY_CSV}")
    return float(row['Land Mill Rate'].iloc[0]), float(row['Improvement Mill Rate'].iloc[0])

# ── 8. REBUILD STANDARD EXPORTS AND REGENERATE ALL 7 CHARTS ──────────────────

print(f"\n{'=' * 70}")
print("REGENERATING STANDARD EXPORTS WITH CENSUS DATA")
print(f"{'=' * 70}")

enriched['_excluded_int'] = enriched['_excluded'].astype(int)

all_results = {}

for sc in SCENARIOS:
    r         = sc['r']
    slug      = sc['slug']
    land_mill, impr_mill = get_rates(r)

    print(f"\n── Scenario r = {r}  ({slug}) ────────────────────────────────────")

    # Verify scenario columns present in enriched GeoDataFrame
    for col in [sc['new_tax_col'], sc['chg_col'], sc['pct_col']]:
        if col not in enriched.columns:
            print(f"  ✗ Missing column: {col}  — skipping scenario.")
            continue

    export_df = pd.DataFrame({
        'PROPERTY_CATEGORY':         enriched['PROPERTY_CATEGORY'],
        'current_tax':               enriched['_city_tax'].fillna(0.0),
        'new_tax':                   enriched[sc['new_tax_col']].fillna(0.0),
        'tax_change':                enriched[sc['chg_col']].fillna(0.0),
        'tax_change_pct':            enriched[sc['pct_col']],
        'taxable_land_value':        enriched['Assessed Land Value'].fillna(0.0),
        'taxable_improvement_value': enriched['Assessed Improvement Value'].fillna(0.0),
        '_exempt_flag':              enriched['_excluded_int'],
        # Census columns — now populated from the join
        'std_geoid':                 enriched['std_geoid'],
        'median_income':             enriched['median_income'],
        'minority_pct':              enriched['minority_pct'],
        'black_pct':                 enriched['black_pct'],
    })

    csv_path = os.path.join(DATA_DIR, f'{slug}.csv')

    out_df = save_standard_export(
        df                      = export_df,
        city                    = slug,
        output_path             = csv_path,
        model_type              = f'split_rate:{float(r):.1f}',
        land_millage            = land_mill,
        improvement_millage     = impr_mill,
        property_category_col   = 'PROPERTY_CATEGORY',
        current_tax_col         = 'current_tax',
        new_tax_col             = 'new_tax',
        tax_change_col          = 'tax_change',
        tax_change_pct_col      = 'tax_change_pct',
        taxable_land_col        = 'taxable_land_value',
        taxable_improvement_col = 'taxable_improvement_value',
        exempt_flag_col         = '_exempt_flag',
        geoid_col               = 'std_geoid',
        income_col              = 'median_income',
        minority_col            = 'minority_pct',
        black_col               = 'black_pct',
    )

    # Report census coverage in the export
    income_cov   = out_df['median_income'].notna().mean() * 100
    minority_cov = out_df['minority_pct'].notna().mean()  * 100
    print(f"  Census coverage in export — income: {income_cov:.1f}%  "
          f"minority: {minority_cov:.1f}%")

    # Generate city report — will now include quintile charts if coverage ≥ 70%
    print(f"  Generating charts → {REPORT_DIR}/{slug}/")
    report_result = create_city_report(
        df         = out_df,
        city       = slug,
        output_dir = REPORT_DIR,
        show       = False,
    )

    charts = report_result.get('charts_saved', [])
    print(f"  Charts saved ({len(charts)}/7):")
    for p in charts:
        print(f"    ✓  {os.path.basename(p)}")

    expected = {
        'category_impact.png', 'ten_pct_share.png', 'distribution.png',
        'income_quintile_non_vacant.png', 'income_quintile_residential.png',
        'minority_quintile_non_vacant.png', 'minority_quintile_residential.png',
    }
    saved_basenames = {os.path.basename(p) for p in charts}
    skipped = expected - saved_basenames
    if skipped:
        print(f"  Charts still skipped:")
        for s in sorted(skipped):
            print(f"    ✗  {s}")
        if income_cov < 70 or minority_cov < 70:
            print(f"  Census coverage ({income_cov:.1f}% / {minority_cov:.1f}%) is below")
            print(f"  the 70% threshold required by create_city_report().")
            print(f"  The centroid fallback above should address this — if coverage")
            print(f"  is still low, check whether block group boundaries for Tulsa")
            print(f"  County cover the full city extent.")

    all_results[slug] = {**report_result, 'income_cov': income_cov, 'minority_cov': minority_cov}

# ── 9. FINAL SUMMARY ──────────────────────────────────────────────────────────

print(f"\n{'=' * 70}")
print("COMPLETE")
print(f"{'=' * 70}")
print(f"\nEnriched GeoPackage: {ENRICHED_GPKG}")
print(f"Standard CSVs:       {DATA_DIR}")
print(f"Chart PNGs:          {REPORT_DIR}")
print()
print(f"  {'Scenario':<15} {'Charts':>7}  {'Income cov':>12}  {'Minority cov':>13}  {'Rev delta':>10}")
print(f"  {'-'*15} {'-'*7}  {'-'*12}  {'-'*13}  {'-'*10}")
for slug, res in all_results.items():
    n      = len(res.get('charts_saved', []))
    icov   = res.get('income_cov', 0)
    mcov   = res.get('minority_cov', 0)
    delta  = res.get('revenue_delta_pct', float('nan'))
    print(f"  {slug:<15} {n:>5}/7  {icov:>11.1f}%  {mcov:>12.1f}%  {delta:>+9.4f}%")

print()
print("Next steps:")
print("  - Review all 7 PNGs in each analysis/reports/<slug>/ folder.")
print("  - Compare category_impact and quintile charts against your manual slides.")
print("  - To add Tulsa to the cross-city comparison:")
print("    cd analysis && jupyter notebook cross_city.ipynb")
