"""
Tulsa Demographic Merge Fix + Quintile Chart Generator
=======================================================
Author: Generated for Ryan Combs
Date:   2026-05-20

ROOT CAUSE
----------
enriched['GEOID'] is 15 characters — a parcel-level identifier
    e.g.  '401430076391000'
census_df['std_geoid'] is 12 characters — a block-group identifier
    e.g.  '401430076391'

The first 12 characters of GEOID are the block-group GEOID.
Fix: truncate GEOID to 12 chars, then merge on that key.

WHAT THIS SCRIPT CHANGES
-------------------------
- Reads tulsa_lvt_parcels_census.gpkg (already has GEOID from spatial join)
- Truncates GEOID to 12 chars → 'GEOID_12'
- Merges census_df on GEOID_12 == std_geoid
- Confirms median_income and minority_pct coverage
- Saves updated tulsa_lvt_parcels_census.gpkg (overwrites)
- Rebuilds all three standard export CSVs with census columns populated
- Regenerates all 7 PNGs for each scenario

WHAT THIS SCRIPT DOES NOT TOUCH
---------------------------------
- tulsa_lvt_parcels.gpkg          (original validated model output)
- tulsa_lvt_summary.csv           (scenario rates)
- tulsa_lvt_exclusions.csv        (exclusion audit)
- Any of the 3 already-working PNGs

Run from repo root:
    cd C:\\Users\\Ryan Combs\\Documents\\LVTShift
    python tulsa_geoid_fix.py
"""

import os, sys, warnings
warnings.filterwarnings('ignore')

import geopandas as gpd
import pandas as pd
import numpy as np
from dotenv import load_dotenv

# ── 0. CONFIGURATION ──────────────────────────────────────────────────────────

REPO_ROOT   = os.path.dirname(os.path.abspath(__file__))
FIPS_CODE   = '40143'
CENSUS_YEAR = 2022

ENRICHED_GPKG = r"C:\Users\Ryan Combs\Documents\TulsaData\LVT_Model_Output\tulsa_lvt_parcels_census.gpkg"
SUMMARY_CSV   = r"C:\Users\Ryan Combs\Documents\TulsaData\LVT_Model_Output\tulsa_lvt_summary.csv"
DATA_DIR      = os.path.join(REPO_ROOT, 'analysis', 'data')
REPORT_DIR    = os.path.join(REPO_ROOT, 'analysis', 'reports')
os.makedirs(DATA_DIR,   exist_ok=True)
os.makedirs(REPORT_DIR, exist_ok=True)

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

# ── 1. SETUP ──────────────────────────────────────────────────────────────────

print("=" * 70)
print("TULSA GEOID FIX + QUINTILE CHART GENERATOR")
print("=" * 70)

load_dotenv(os.path.join(REPO_ROOT, '.env'))
api_key = os.environ.get('CENSUS_API_KEY')
if not api_key:
    print("✗ CENSUS_API_KEY not found in .env"); sys.exit(1)
print(f"\n✓ Census API key loaded ({api_key[:6]}...)")

if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

try:
    from lvt.census_utils import get_census_data_with_boundaries
    from lvt.lvt_utils import save_standard_export
    from lvt.viz import create_city_report
    print("✓ lvt modules imported")
except ImportError as e:
    print(f"✗ Import failed: {e}"); sys.exit(1)

# ── 2. LOAD ENRICHED GEOPACKAGE ───────────────────────────────────────────────

print(f"\n── Loading enriched GeoPackage ──────────────────────────────────────")
enriched = gpd.read_file(ENRICHED_GPKG)
TOTAL_ROWS = len(enriched)
print(f"  Rows: {TOTAL_ROWS:,}")
print(f"  GEOID dtype:  {enriched['GEOID'].dtype}")
print(f"  GEOID sample: {enriched['GEOID'].dropna().iloc[0]!r}  "
      f"(len={len(str(enriched['GEOID'].dropna().iloc[0]))})")

# ── 3. FETCH CENSUS DATA ──────────────────────────────────────────────────────

print(f"\n── Fetching Census ACS {CENSUS_YEAR} ─────────────────────────────────────────")
try:
    census_df, _ = get_census_data_with_boundaries(
        fips_code=FIPS_CODE, year=CENSUS_YEAR, api_key=api_key
    )
except Exception as e:
    print(f"✗ Census fetch failed: {e}"); sys.exit(1)

print(f"  census_df rows:       {len(census_df):,}")
print(f"  std_geoid dtype:      {census_df['std_geoid'].dtype}")
print(f"  std_geoid sample:     {census_df['std_geoid'].iloc[0]!r}  "
      f"(len={len(str(census_df['std_geoid'].iloc[0]))})")

# ── 4. THE FIX: TRUNCATE GEOID TO 12 CHARACTERS ──────────────────────────────
#
# enriched['GEOID'] = '401430076391000'  (15 chars — parcel-level)
# census_df['std_geoid'] = '401430076391'  (12 chars — block-group)
#
# The block-group GEOID is always the first 12 characters of the parcel GEOID:
#   state(2) + county(3) + tract(6) + block_group(1) = 12
#
# ─────────────────────────────────────────────────────────────────────────────

print(f"\n── GEOID truncation fix ─────────────────────────────────────────────")

enriched['GEOID_12'] = (
    enriched['GEOID']
    .astype(str)
    .str.strip()
    .str[:12]
)
# Null out the sentinel 'nan' strings that arise from NaN GEOIDs
enriched.loc[enriched['GEOID'].isna(), 'GEOID_12'] = np.nan

# Verify the truncation produces values that exist in census_df
std_geoids  = set(census_df['std_geoid'].astype(str).str.strip())
sample_keys = enriched['GEOID_12'].dropna().unique()[:10]
n_match     = sum(1 for k in sample_keys if k in std_geoids)
print(f"  Sample GEOID_12 values: {list(sample_keys)}")
print(f"  Matches in census_df:   {n_match}/{len(sample_keys)} of sample")
print(f"  census_df std_geoid sample: {census_df['std_geoid'].head(3).tolist()}")

if n_match == 0:
    print(f"\n  ✗ Still no matches after truncation.")
    print(f"  Inspect GEOID_12 vs std_geoid more carefully:")
    print(f"  GEOID_12[0]: {repr(enriched['GEOID_12'].dropna().iloc[0])}")
    print(f"  std_geoid[0]: {repr(census_df['std_geoid'].iloc[0])}")
    sys.exit(1)

# ── 5. DROP STALE DEMOGRAPHIC COLUMNS AND MERGE FRESH ────────────────────────

print(f"\n── Merging demographics onto GEOID_12 ───────────────────────────────")

# Drop any previously merged (and empty) demographic columns
stale = [c for c in ['std_geoid', 'median_income', 'minority_pct',
                      'black_pct', 'total_pop']
         if c in enriched.columns]
if stale:
    enriched.drop(columns=stale, inplace=True)
    print(f"  Dropped stale columns: {stale}")

# Select only the columns we need from census_df
demo_cols = census_df[
    ['std_geoid', 'median_income', 'minority_pct', 'black_pct']
].drop_duplicates('std_geoid').copy()

# Normalise both keys: str, stripped — leading zeros already preserved (both str)
demo_cols['std_geoid'] = demo_cols['std_geoid'].astype(str).str.strip()
enriched['GEOID_12']   = enriched['GEOID_12'].astype(str).str.strip()
enriched.loc[enriched['GEOID_12'] == 'nan', 'GEOID_12'] = np.nan

enriched = enriched.merge(
    demo_cols,
    left_on  = 'GEOID_12',
    right_on = 'std_geoid',
    how      = 'left',
)

# Dedup guard — merge should be 1:1 since we drop_duplicates on std_geoid
post_merge = len(enriched)
if post_merge != TOTAL_ROWS:
    print(f"  WARNING: row count changed {TOTAL_ROWS:,} → {post_merge:,} after merge.")
    print(f"  Deduplicating on original index ...")
    enriched = enriched[~enriched.index.duplicated(keep='first')]
    print(f"  After dedup: {len(enriched):,} rows")
else:
    print(f"  ✓ Row count preserved: {len(enriched):,}")

# ── 6. AUDIT COVERAGE ────────────────────────────────────────────────────────

print(f"\n── Coverage audit ───────────────────────────────────────────────────")

taxable_mask  = enriched['_excluded'] == False
n_taxable     = int(taxable_mask.sum())
income_cov    = enriched.loc[taxable_mask, 'median_income'].notna().mean()  * 100
minority_cov  = enriched.loc[taxable_mask, 'minority_pct'].notna().mean()  * 100
black_cov     = enriched.loc[taxable_mask, 'black_pct'].notna().mean()     * 100
geoid12_cov   = enriched.loc[taxable_mask, 'GEOID_12'].notna().mean()      * 100

print(f"  Taxable parcels:          {n_taxable:,}")
print(f"  GEOID_12 coverage:        {geoid12_cov:.1f}%")
print(f"  median_income coverage:   {income_cov:.1f}%")
print(f"  minority_pct coverage:    {minority_cov:.1f}%")
print(f"  black_pct coverage:       {black_cov:.1f}%")

if income_cov < 70:
    print(f"\n  WARNING: income coverage still below 70%.")
    print(f"  Rows with GEOID_12 but no income match:")
    has_key_no_income = (
        enriched['GEOID_12'].notna() & enriched['median_income'].isna()
    )
    print(f"  {has_key_no_income.sum():,} parcels")
    print(f"  Sample GEOID_12 values with no match:")
    print(enriched.loc[has_key_no_income, 'GEOID_12'].dropna().unique()[:10].tolist())
    print(f"  census_df std_geoid sample:")
    print(census_df['std_geoid'].head(10).tolist())
else:
    print(f"  ✓ Coverage sufficient for quintile charts (≥70% threshold met)")

# ── 7. SAVE UPDATED ENRICHED GEOPACKAGE ──────────────────────────────────────

print(f"\n── Saving updated enriched GeoPackage ───────────────────────────────")
enriched.to_file(ENRICHED_GPKG, driver='GPKG')
print(f"  ✓ Saved: {ENRICHED_GPKG}  ({len(enriched):,} rows)")

# ── 8. LOAD MILLAGE RATES ─────────────────────────────────────────────────────

summary_df = pd.read_csv(SUMMARY_CSV)

def get_rates(r_val):
    row = summary_df[summary_df['Scenario'] == f'r = {r_val}']
    if row.empty:
        raise ValueError(f"Scenario r={r_val} not found")
    return float(row['Land Mill Rate'].iloc[0]), float(row['Improvement Mill Rate'].iloc[0])

# ── 9. REBUILD EXPORTS AND REGENERATE ALL 7 CHARTS ───────────────────────────

print(f"\n{'=' * 70}")
print("REGENERATING STANDARD EXPORTS AND ALL 7 CHARTS")
print(f"{'=' * 70}")

enriched['_excluded_int'] = enriched['_excluded'].astype(int)
all_results = {}

for sc in SCENARIOS:
    r         = sc['r']
    slug      = sc['slug']
    land_mill, impr_mill = get_rates(r)

    print(f"\n── Scenario r = {r}  ({slug}) ────────────────────────────────────")

    missing_cols = [c for c in [sc['new_tax_col'], sc['chg_col'], sc['pct_col']]
                    if c not in enriched.columns]
    if missing_cols:
        print(f"  ✗ Missing columns: {missing_cols} — skipping."); continue

    export_df = pd.DataFrame({
        'PROPERTY_CATEGORY':         enriched['PROPERTY_CATEGORY'],
        'current_tax':               enriched['_city_tax'].fillna(0.0),
        'new_tax':                   enriched[sc['new_tax_col']].fillna(0.0),
        'tax_change':                enriched[sc['chg_col']].fillna(0.0),
        'tax_change_pct':            enriched[sc['pct_col']],
        'taxable_land_value':        enriched['Assessed Land Value'].fillna(0.0),
        'taxable_improvement_value': enriched['Assessed Improvement Value'].fillna(0.0),
        '_exempt_flag':              enriched['_excluded_int'],
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

    in_cov  = out_df['median_income'].notna().mean() * 100
    mn_cov  = out_df['minority_pct'].notna().mean()  * 100
    print(f"  Export census coverage — income: {in_cov:.1f}%  minority: {mn_cov:.1f}%")

    if in_cov < 70 and mn_cov < 70:
        print(f"  ✗ Coverage still below 70% — quintile charts will be skipped.")
        print(f"  Paste the coverage audit output above for further diagnosis.")

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
    skipped = expected - {os.path.basename(p) for p in charts}
    for s in sorted(skipped):
        print(f"    ✗  {s}  (skipped)")

    all_results[slug] = {**report_result, 'income_cov': in_cov, 'minority_cov': mn_cov}

# ── 10. FINAL SUMMARY ─────────────────────────────────────────────────────────

print(f"\n{'=' * 70}")
print("COMPLETE")
print(f"{'=' * 70}")
print(f"\n  {'Scenario':<15} {'Charts':>7}  {'Income cov':>12}  {'Minority cov':>13}")
print(f"  {'-'*15} {'-'*7}  {'-'*12}  {'-'*13}")
for slug, res in all_results.items():
    n    = len(res.get('charts_saved', []))
    icov = res.get('income_cov', 0)
    mcov = res.get('minority_cov', 0)
    print(f"  {slug:<15} {n:>5}/7  {icov:>11.1f}%  {mcov:>12.1f}%")
