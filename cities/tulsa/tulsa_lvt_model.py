"""
Tulsa City-Only Land Value Tax Shift Model
===========================================
Author: Generated for Ryan Combs
Date:   2026-05-19  (revised)

ASSUMPTIONS (confirmed by user):
---------------------------------
Scope:          City-only millage shift only. County, school, TCC, and tech school
                millages are held constant and not modeled.

Revenue target: Derived from parcel data as:
                City Tax_i = (City Millage_i / Total Millage_i) * Current Tax ($)_i
                Target = sum of City Tax_i across ALL parcels (before exclusions),
                so the full city levy is preserved even though exempt parcels
                are excluded from the rate-setting base.

Value base:     Assessed Land Value and Assessed Improvement Value for all rate math.
                Market values (Land Value, Improvement Value) are carried through
                for reference but are NOT used in any rate calculations.

Exclusions from rate-setting base (confirmed by user):
  - Parcel Type in: ROW, SUBD_ROW, UNPLAT_ROW, RAIL, UNPLAT_RAIL, ARK_RIV
  - Publicly Owned == 1
  - Exempt Flag == 1
  - Current Tax ($) == 0
  Homestead parcels are KEPT in the taxable base.
  DIVINTEREST parcels are KEPT pending further review.

Scenarios:      r = 2, r = 3, r = 4  (land rate = r * improvement rate)
                All three are revenue-neutral to the same city revenue target.

Neighborhood:   DISABLED. No confirmed neighborhood field exists in the dataset.
                To enable: set NBHD_FIELD = 'your_exact_column_name' below.

REVISIONS vs v1:
  - Neighborhood auto-detection removed entirely (was incorrectly selecting
    'Lot Area (SF)'). Aggregation skipped unless NBHD_FIELD is set manually.
  - QGIS export no longer uses merge(). Model result columns are written
    directly back to gdf by positional index alignment, guaranteeing the
    output GeoPackage has exactly 159,014 rows.
  - ParcelNo uniqueness is checked and reported before the model runs.

Outputs:
  - Console summary tables (validate before anything else)
  - tulsa_lvt_parcels.gpkg     — parcel-level results for QGIS (159,014 rows)
  - tulsa_lvt_summary.csv      — scenario summary table
  - tulsa_lvt_exclusions.csv   — excluded parcel audit log
"""

import geopandas as gpd
import pandas as pd
import numpy as np
import os
import warnings
warnings.filterwarnings('ignore')

# ── 0. CONFIGURATION ──────────────────────────────────────────────────────────

INPUT_PATH = r"C:\Users\Ryan Combs\Documents\TulsaData\TCMapper_Final2.gpkg"
OUTPUT_DIR = r"C:\Users\Ryan Combs\Documents\TulsaData\LVT_Model_Output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

SPLIT_RATIOS = [2, 3, 4]   # land rate = r * improvement rate

# Confirmed exclusion values for Parcel Type
EXCLUDE_PARCEL_TYPES = {'ROW', 'SUBD_ROW', 'UNPLAT_ROW', 'RAIL', 'UNPLAT_RAIL', 'ARK_RIV'}

# ── NEIGHBORHOOD FIELD ────────────────────────────────────────────────────────
# Set to None until a confirmed neighborhood column is identified.
# To enable aggregation, replace None with the exact column name, e.g.:
#   NBHD_FIELD = 'Neighborhood'
NBHD_FIELD = None

# Field name mapping (confirmed by user)
F_PARCEL_ID       = 'ParcelNo'
F_PARCEL_TYPE     = 'Parcel Type'
F_DEV_STATUS      = 'Development Status'
F_PUBLICLY_OWNED  = 'Publicly Owned'
F_EXEMPT_FLAG     = 'Exempt Flag'
F_HOMESTEAD       = 'Homestead Exemption'
F_CURRENT_TAX     = 'Current Tax ($)'
F_TOTAL_MILLAGE   = 'Total Millage'
F_CITY_MILLAGE    = 'City Millage'
F_COUNTY_MILLAGE  = 'County Millage'
F_SCHOOL_MILLAGE  = 'School Millage'
F_TCC_MILLAGE     = 'TCC Millage'
F_TECH_MILLAGE    = 'Tech School Millage'
F_LAND_VALUE      = 'Land Value'
F_IMPR_VALUE      = 'Improvement Value'
F_ASSESSED_LAND   = 'Assessed Land Value'
F_ASSESSED_IMPR   = 'Assessed Improvement Value'
F_TOTAL_ASSESSED  = 'Total Assessed Value'

# ── 1. LOAD DATA ──────────────────────────────────────────────────────────────

print("=" * 70)
print("TULSA CITY-ONLY LVT SHIFT MODEL")
print("=" * 70)
print(f"\nLoading: {INPUT_PATH}")
gdf = gpd.read_file(INPUT_PATH)
TOTAL_ROWS = len(gdf)
print(f"  Loaded {TOTAL_ROWS:,} parcels, {len(gdf.columns)} fields")
print(f"  CRS: {gdf.crs}")

# ── 2. PARCELNO UNIQUENESS CHECK ──────────────────────────────────────────────

print("\n── ParcelNo uniqueness check ────────────────────────────────────────")
n_unique   = gdf[F_PARCEL_ID].nunique()
n_null_id  = gdf[F_PARCEL_ID].isna().sum()
n_dupes    = TOTAL_ROWS - n_unique - n_null_id
print(f"  Total rows:          {TOTAL_ROWS:,}")
print(f"  Unique ParcelNo:     {n_unique:,}")
print(f"  Null ParcelNo:       {n_null_id:,}")
print(f"  Duplicate ParcelNo:  {n_dupes:,}")

if n_dupes > 0:
    dup_counts = gdf[F_PARCEL_ID].value_counts()
    dup_ids    = dup_counts[dup_counts > 1]
    print(f"\n  WARNING: {len(dup_ids):,} ParcelNo values appear more than once.")
    print(f"  Top 10 duplicates:")
    print(dup_ids.head(10).to_string())
    print(f"\n  IMPORTANT: Because ParcelNo is not unique, the QGIS export uses")
    print(f"  positional index alignment (not a key-based merge) to assign model")
    print(f"  results. Row order is preserved exactly as loaded.")
else:
    print(f"  ParcelNo is unique — no duplicates found.")

# ── 3. DERIVE CITY-ONLY CURRENT TAX PER PARCEL ───────────────────────────────

print("\n── Step 0: City-only current revenue ────────────────────────────────")

# Compute on gdf directly (all rows, original index preserved)
# Guard against division by zero on Total Millage
gdf['_city_tax'] = np.where(
    gdf[F_TOTAL_MILLAGE] > 0,
    (gdf[F_CITY_MILLAGE] / gdf[F_TOTAL_MILLAGE]) * gdf[F_CURRENT_TAX],
    0.0
)

city_revenue_target = gdf['_city_tax'].sum()
print(f"  Full dataset parcel count:     {TOTAL_ROWS:,}")
print(f"  Sum of Current Tax ($):        ${gdf[F_CURRENT_TAX].sum():>18,.2f}")
print(f"  City Revenue Target (derived): ${city_revenue_target:>18,.2f}")
print(f"  (Cross-check this against the City of Tulsa published property tax levy)")

# ── 4. BUILD EXCLUSION FLAGS ──────────────────────────────────────────────────

print("\n── Step 1: Exclusion audit ───────────────────────────────────────────")

# All flags written directly to gdf — original index intact throughout
gdf['_excl_parcel_type']  = gdf[F_PARCEL_TYPE].isin(EXCLUDE_PARCEL_TYPES)
gdf['_excl_publicly_owned'] = (
    gdf[F_PUBLICLY_OWNED].astype(str).str.strip()
    .isin(['1', '1.0', 'True', 'true', 'TRUE', 'Yes', 'yes'])
)
gdf['_excl_exempt_flag'] = (
    gdf[F_EXEMPT_FLAG].astype(str).str.strip()
    .isin(['1', '1.0', 'True', 'true', 'TRUE', 'Yes', 'yes'])
)
gdf['_excl_zero_tax'] = (gdf[F_CURRENT_TAX] == 0)

gdf['_excluded'] = (
    gdf['_excl_parcel_type']   |
    gdf['_excl_publicly_owned']|
    gdf['_excl_exempt_flag']   |
    gdf['_excl_zero_tax']
)

excl_reasons = {
    'Parcel Type (ROW/RAIL/etc)': '_excl_parcel_type',
    'Publicly Owned = 1':         '_excl_publicly_owned',
    'Exempt Flag = 1':            '_excl_exempt_flag',
    'Current Tax ($) = 0':        '_excl_zero_tax',
}

print(f"\n  {'Exclusion Reason':<35} {'Parcels':>10}  {'City Tax':>18}")
print(f"  {'-'*35} {'-'*10}  {'-'*18}")
for label, flag in excl_reasons.items():
    n = gdf[flag].sum()
    t = gdf.loc[gdf[flag], '_city_tax'].sum()
    print(f"  {label:<35} {n:>10,}  ${t:>17,.2f}")

n_excluded = int(gdf['_excluded'].sum())
n_taxable  = TOTAL_ROWS - n_excluded
print(f"\n  {'Total excluded (any reason)':<35} {n_excluded:>10,}")
print(f"  {'Remaining taxable parcels':<35} {n_taxable:>10,}")

# Homestead check — confirm kept in base
if F_HOMESTEAD in gdf.columns:
    hs_flag      = gdf[F_HOMESTEAD].astype(str).str.strip().isin(
                       ['1', '1.0', 'True', 'true', 'TRUE', 'Yes', 'yes'])
    hs_in_base   = (hs_flag & ~gdf['_excluded']).sum()
    hs_excl_other= (hs_flag &  gdf['_excluded']).sum()
    print(f"\n  Homestead parcels in taxable base:  {hs_in_base:,}")
    print(f"  Homestead parcels excluded (other): {hs_excl_other:,}  (excluded for another reason)")

# Save exclusion audit CSV
excl_df = gdf.loc[gdf['_excluded'], [
    F_PARCEL_ID, F_PARCEL_TYPE, F_PUBLICLY_OWNED, F_EXEMPT_FLAG,
    F_CURRENT_TAX, '_excl_parcel_type', '_excl_publicly_owned',
    '_excl_exempt_flag', '_excl_zero_tax'
]].copy()
excl_path = os.path.join(OUTPUT_DIR, 'tulsa_lvt_exclusions.csv')
excl_df.to_csv(excl_path, index=False)
print(f"\n  Exclusion audit saved: {excl_path}")

# ── 5. TAXABLE BASE TOTALS ────────────────────────────────────────────────────

print("\n── Step 2: Taxable base totals ──────────────────────────────────────")

taxable_mask = ~gdf['_excluded']   # boolean mask over original gdf index

sum_assessed_land    = gdf.loc[taxable_mask, F_ASSESSED_LAND].sum()
sum_assessed_impr    = gdf.loc[taxable_mask, F_ASSESSED_IMPR].sum()
sum_current_tax_base = gdf.loc[taxable_mask, F_CURRENT_TAX].sum()
sum_city_tax_base    = gdf.loc[taxable_mask, '_city_tax'].sum()

print(f"  Taxable parcels:                   {n_taxable:>12,}")
print(f"  Sum Assessed Land Value:           ${sum_assessed_land:>18,.2f}")
print(f"  Sum Assessed Improvement Value:    ${sum_assessed_impr:>18,.2f}")
print(f"  Sum Total Assessed Value:          ${gdf.loc[taxable_mask, F_TOTAL_ASSESSED].sum():>18,.2f}")
print(f"  Sum Current Tax ($) in base:       ${sum_current_tax_base:>18,.2f}")
print(f"  Sum City Tax in base:              ${sum_city_tax_base:>18,.2f}")
print(f"  City Revenue Target (full dataset):${city_revenue_target:>18,.2f}")
print(f"  Revenue gap (excluded city tax):   ${city_revenue_target - sum_city_tax_base:>18,.2f}")
print(f"  NOTE: Revenue neutrality targets the full dataset city revenue;")
print(f"        excluded parcels' city tax is reallocated across the taxable base.")

# ── 6. THREE-SCENARIO MODEL ───────────────────────────────────────────────────
#
#  KEY DESIGN: all model result columns are written directly to gdf using
#  .loc[taxable_mask, col] = value.  No merge, no reindex, no row duplication.
#  Excluded parcels receive NaN for all model result columns automatically.
#  gdf always has exactly TOTAL_ROWS rows throughout.
# ─────────────────────────────────────────────────────────────────────────────

print("\n── Step 3: Revenue-neutral rates and parcel-level results ───────────")

scenario_summaries = []

for r in SPLIT_RATIOS:
    label   = f'r{r}'
    new_col = f'new_city_tax_{label}'
    chg_col = f'city_tax_chg_usd_{label}'
    pct_col = f'city_tax_chg_pct_{label}'

    print(f"\n  ── Scenario r = {r} (land rate = {r}× improvement rate) ──")

    # Revenue-neutral rates (mills)
    # impr_rate = target / ((r*sum_land + sum_impr) / 1000)
    denominator = (r * sum_assessed_land + sum_assessed_impr) / 1000.0
    impr_rate   = city_revenue_target / denominator
    land_rate   = r * impr_rate

    print(f"    Improvement mill rate: {impr_rate:.6f}")
    print(f"    Land mill rate:        {land_rate:.6f}")

    # Initialise full-length columns with NaN
    gdf[new_col] = np.nan
    gdf[chg_col] = np.nan
    gdf[pct_col] = np.nan

    # Write results only to taxable rows (by index — no merge)
    gdf.loc[taxable_mask, new_col] = (
        (land_rate / 1000.0) * gdf.loc[taxable_mask, F_ASSESSED_LAND] +
        (impr_rate / 1000.0) * gdf.loc[taxable_mask, F_ASSESSED_IMPR]
    )
    gdf.loc[taxable_mask, chg_col] = (
        gdf.loc[taxable_mask, new_col] - gdf.loc[taxable_mask, '_city_tax']
    )
    gdf.loc[taxable_mask, pct_col] = np.where(
        gdf.loc[taxable_mask, '_city_tax'] > 0,
        (gdf.loc[taxable_mask, chg_col] / gdf.loc[taxable_mask, '_city_tax']) * 100,
        np.nan
    )

    # Verification
    modeled_revenue = gdf.loc[taxable_mask, new_col].sum()
    revenue_error   = modeled_revenue - city_revenue_target

    n_pay_more  = int((gdf.loc[taxable_mask, chg_col] > 0).sum())
    n_pay_less  = int((gdf.loc[taxable_mask, chg_col] < 0).sum())
    n_unchanged = int((gdf.loc[taxable_mask, chg_col] == 0).sum())
    n_null_pct  = int(gdf.loc[taxable_mask, pct_col].isna().sum())

    median_chg_usd = gdf.loc[taxable_mask, chg_col].median()
    median_chg_pct = gdf.loc[taxable_mask, pct_col].median()
    mean_chg_usd   = gdf.loc[taxable_mask, chg_col].mean()

    print(f"    Modeled revenue:       ${modeled_revenue:,.2f}")
    print(f"    Revenue target:        ${city_revenue_target:,.2f}")
    print(f"    Revenue error:         ${revenue_error:,.2f}  (should be ~$0)")
    print(f"    Parcels paying more:   {n_pay_more:,}")
    print(f"    Parcels paying less:   {n_pay_less:,}")
    print(f"    Parcels unchanged:     {n_unchanged:,}")
    print(f"    Null pct-change:       {n_null_pct:,}  (city_tax=0 in base)")
    print(f"    Median change ($):     ${median_chg_usd:,.2f}")
    print(f"    Median change (%):     {median_chg_pct:.2f}%")
    print(f"    Mean change ($):       ${mean_chg_usd:,.2f}")

    scenario_summaries.append({
        'Scenario':                f'r = {r}',
        'Land Mill Rate':          round(land_rate, 6),
        'Improvement Mill Rate':   round(impr_rate, 6),
        'City Revenue Target ($)': round(city_revenue_target, 2),
        'Modeled Revenue ($)':     round(modeled_revenue, 2),
        'Revenue Error ($)':       round(revenue_error, 2),
        'Taxable Parcels':         n_taxable,
        'Parcels Paying More':     n_pay_more,
        'Parcels Paying Less':     n_pay_less,
        'Parcels Unchanged':       n_unchanged,
        'Null Pct Change':         n_null_pct,
        'Median Change ($)':       round(median_chg_usd, 2),
        'Median Change (%)':       round(median_chg_pct, 2),
        'Mean Change ($)':         round(mean_chg_usd, 2),
        'Sum Assessed Land':       round(sum_assessed_land, 2),
        'Sum Assessed Impr':       round(sum_assessed_impr, 2),
    })

# ── 7. NEIGHBORHOOD AGGREGATION ───────────────────────────────────────────────

print("\n── Step 4: Neighborhood aggregation ─────────────────────────────────")
if NBHD_FIELD is None:
    print("  SKIPPED — no confirmed neighborhood field in dataset.")
    print("  To enable: set NBHD_FIELD = 'your_exact_column_name' at top of script.")
else:
    print(f"  Aggregating on '{NBHD_FIELD}' ({gdf[NBHD_FIELD].nunique()} unique values) ...")
    nbhd_agg_dict = {
        F_PARCEL_ID:    'count',
        '_city_tax':    'sum',
        F_ASSESSED_LAND:'sum',
        F_ASSESSED_IMPR:'sum',
    }
    for r in SPLIT_RATIOS:
        lbl = f'r{r}'
        nbhd_agg_dict[f'new_city_tax_{lbl}']     = 'sum'
        nbhd_agg_dict[f'city_tax_chg_usd_{lbl}'] = ['sum', 'median']

    nbhd_df = (gdf.loc[taxable_mask]
                  .groupby(NBHD_FIELD)
                  .agg(nbhd_agg_dict))
    nbhd_df.columns = [
        '_'.join(c).strip('_') if isinstance(c, tuple) else c
        for c in nbhd_df.columns
    ]
    nbhd_df = nbhd_df.reset_index()

    nbhd_path = os.path.join(OUTPUT_DIR, 'tulsa_lvt_neighborhood.csv')
    nbhd_df.to_csv(nbhd_path, index=False)
    print(f"  Saved: {nbhd_path}  ({len(nbhd_df)} neighborhoods)")

# ── 8. SCENARIO SUMMARY TABLE ─────────────────────────────────────────────────

print("\n── SCENARIO SUMMARY TABLE ───────────────────────────────────────────")
summary_df = pd.DataFrame(scenario_summaries)
print(summary_df.T.to_string())

summary_path = os.path.join(OUTPUT_DIR, 'tulsa_lvt_summary.csv')
summary_df.to_csv(summary_path, index=False)
print(f"\n  Summary saved: {summary_path}")

# ── 9. PARCEL-LEVEL OUTPUT FOR QGIS ──────────────────────────────────────────
#
#  gdf already contains every model column written in-place above.
#  No merge needed. Row count is guaranteed to equal TOTAL_ROWS.
# ─────────────────────────────────────────────────────────────────────────────

print("\n── Step 5: Parcel-level GeoPackage for QGIS ─────────────────────────")

# Sanity check before writing
assert len(gdf) == TOTAL_ROWS, (
    f"FATAL: gdf has {len(gdf):,} rows but should have {TOTAL_ROWS:,}. "
    "Something modified the dataframe length — do not write output."
)

gpkg_path = os.path.join(OUTPUT_DIR, 'tulsa_lvt_parcels.gpkg')
gdf.to_file(gpkg_path, driver='GPKG')
print(f"  Parcel GeoPackage saved: {gpkg_path}")
print(f"  Total parcels in output: {len(gdf):,}  ✓ matches input row count")
print(f"  Excluded parcels have NaN for all model result columns (visible in QGIS).")

# ── 10. FINAL CONFIRMATION ────────────────────────────────────────────────────

print("\n" + "=" * 70)
print("MODEL COMPLETE — review the summary tables above before proceeding.")
print("Cross-check City Revenue Target against published City of Tulsa levy.")
print("=" * 70)
print(f"\nOutput directory: {OUTPUT_DIR}")
print(f"  tulsa_lvt_summary.csv    — scenario summary (rates, counts, medians)")
print(f"  tulsa_lvt_exclusions.csv — audit log of excluded parcels")
print(f"  tulsa_lvt_parcels.gpkg   — {TOTAL_ROWS:,}-row parcel layer for QGIS")
if NBHD_FIELD:
    print(f"  tulsa_lvt_neighborhood.csv — neighborhood aggregation")
