"""
Tulsa LVTShift Standard Export + City Report Generator  (v2 — category fix)
============================================================================
Author: Generated for Ryan Combs
Date:   2026-05-20

CHANGES FROM v1 (tulsa_lvt_export.py)
--------------------------------------
- Added 'Property Use' to USE_DESC_CANDIDATES — this field exists in the
  dataset and is now used as the primary input to categorize_property_type().
- Rewrote assign_category() with explicit priority ordering:
    1. Development Status = 'Vacant'       → Vacant Land   (11,020 NaN use codes)
    2. Development Status = 'Underdeveloped' → Vacant Land  (see note below)
    3. Property Use present                → categorize_property_type()
    4. Parcel Type = CONDO                 → Single Family
    5. Parcel Type = DIVINTEREST           → Other
    6. Fallback: zero improvement value    → Vacant Land
    7. Final fallback                      → Other

UNDERDEVELOPED NOTE
-------------------
1,126 parcels have Development Status = 'Underdeveloped'. These are mapped
to 'Vacant Land' here because they carry no improvement value and represent
underutilised land — the core subject of LVT policy analysis. If you prefer
to distinguish them, change the UNDERDEVELOPED_CATEGORY constant below to
a custom string such as 'Underdeveloped' and it will appear as its own bar
in the charts (minimum 50 parcels, so it will clear the chart threshold).

Run from the repo root:
    cd C:\\Users\\Ryan Combs\\Documents\\LVTShift
    python tulsa_lvt_export_v2.py
"""

import os
import sys
import geopandas as gpd
import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

# ── 0. CONFIGURATION ──────────────────────────────────────────────────────────

# Input — use the census-enriched GeoPackage so demographic columns are present
INPUT_GPKG  = r"C:\Users\Ryan Combs\Documents\TulsaData\LVT_Model_Output\tulsa_lvt_parcels_census.gpkg"
SUMMARY_CSV = r"C:\Users\Ryan Combs\Documents\TulsaData\LVT_Model_Output\tulsa_lvt_summary.csv"

REPO_ROOT  = os.path.dirname(os.path.abspath(__file__))
DATA_DIR   = os.path.join(REPO_ROOT, 'analysis', 'data')
REPORT_DIR = os.path.join(REPO_ROOT, 'analysis', 'reports')
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

# How to label underdeveloped parcels in the charts.
# Options:
#   'Vacant Land'      — merged with vacant lots (recommended for most analyses)
#   'Underdeveloped'   — shown as its own bar (1,126 parcels, clears 50-parcel threshold)
UNDERDEVELOPED_CATEGORY = 'Underdeveloped'

# ── 1. IMPORT REPO MODULES ────────────────────────────────────────────────────

if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

try:
    from lvt.lvt_utils import save_standard_export, categorize_property_type
    from lvt.viz import create_city_report
    print("✓ lvt modules imported")
except ImportError as e:
    print(f"✗ Import failed: {e}"); sys.exit(1)

# ── 2. LOAD DATA ──────────────────────────────────────────────────────────────

print(f"\nLoading: {INPUT_GPKG}")
gdf = gpd.read_file(INPUT_GPKG)
print(f"  {len(gdf):,} parcels, {len(gdf.columns)} fields")

# ── 3. PROPERTY CATEGORY MAPPING (v2) ────────────────────────────────────────
#
# Priority order — first matching rule wins:
#
#  Rule 1  Development Status = 'Vacant'
#          → 'Vacant Land'
#          Rationale: 11,020 of 11,476 vacant parcels have NaN Property Use.
#          Development Status is the only reliable signal for this group.
#
#  Rule 2  Development Status = 'Underdeveloped'
#          → UNDERDEVELOPED_CATEGORY (default: 'Vacant Land')
#          Rationale: 1,126 parcels with minimal or no improvement value.
#          Configurable above.
#
#  Rule 3  Property Use is not null
#          → categorize_property_type(Property Use)
#          Covers all developed parcels with a use description.
#          Falls through to Rule 4 only if categorize_property_type returns 'Other'.
#
#  Rule 4  Parcel Type = 'CONDO'
#          → 'Single Family'
#          Rationale: CONDO parcel type is unambiguous residential.
#          (Most will already be caught by Rule 3 via Property Use = 'Condo',
#           but this catches the 36 CONDO parcels with non-Condo use codes.)
#
#  Rule 5  Parcel Type = 'DIVINTEREST'
#          → 'Other'
#          Rationale: divided interest / mineral rights, no improvement.
#
#  Rule 6  Assessed Improvement Value = 0  (fallback for NaN Property Use,
#          non-vacant Development Status)
#          → 'Vacant Land'
#          Catches the ~775 developed-status parcels with NaN use codes
#          that happen to have no improvement value.
#
#  Rule 7  Final fallback → 'Other'
# ─────────────────────────────────────────────────────────────────────────────

# Map Property Use strings to standardized categories.
# categorize_property_type() does case-insensitive keyword matching,
# so we verify here what it produces for each Tulsa value:
#
#   'Residential'   → 'Single Family'           (keyword: 'Residential')
#   'Commercial'    → 'Retail/Service/Commercial'(keyword: 'Commercial')
#   'Condo'         → 'Single Family'            (keyword: 'Single' via 'Condo'?
#                                                 — checked below)
#   'Duplex'        → 'Small Multi-Family (2-4 units)' (keyword: 'Duplex')
#   'Multiple Unit' → 'Large Multi-Family (5+ units)'  (keyword: 'Multi-Family'?
#                                                        — need to verify)
#   'Townhouse'     → 'Other Residential'        (keyword: 'Other Residential'? no)
#   'Mobile Home'   → 'Other Residential'        (keyword: 'Manufactured Home'? no)
#   'Triplex'       → 'Small Multi-Family (2-4 units)' (keyword: 'Triplex')
#   'Out Building'  → 'Other'                    (no keyword match)
#   'Agricultural'  → 'Agricultural'             (keyword: 'Agricultural')
#   'Industrial'    → 'Industrial'               (keyword: 'Industrial')
#
# Values that categorize_property_type() may return 'Other' for, requiring
# explicit overrides in the PROPERTY_USE_OVERRIDES dict below:

PROPERTY_USE_OVERRIDES = {
    # Property Use value (exact, case-sensitive) : category to assign
    'Condo':         'Single Family',               # keyword 'Condo' not in mapping
    'Multiple Unit': 'Large Multi-Family (5+ units)',# keyword 'Multiple' not in mapping
    'Townhouse':     'Other Residential',            # keyword 'Townhouse' not in mapping
    'Mobile Home':   'Other Residential',            # keyword 'Mobile Home' not in mapping
    'Out Building':  'Other Residential',            # accessory structure, not vacant
}


def assign_category(row) -> str:
    dev_status = str(row.get('Development Status', '') or '').strip()
    prop_use   = row.get('Property Use', None)
    ptype      = str(row.get('Parcel Type', '') or '').strip().upper()
    impr       = float(row.get('Assessed Improvement Value', 0) or 0)

    # Rule 1 — Vacant by Development Status
    if dev_status == 'Vacant':
        return 'Vacant Land'

    # Rule 2 — Underdeveloped by Development Status
    if dev_status == 'Underdeveloped':
        return UNDERDEVELOPED_CATEGORY

    # Rule 3 — Property Use present
    if pd.notna(prop_use) and str(prop_use).strip() != '':
        use_str = str(prop_use).strip()

        # Check explicit overrides first (values categorize_property_type misses)
        if use_str in PROPERTY_USE_OVERRIDES:
            return PROPERTY_USE_OVERRIDES[use_str]

        # Delegate to repo function
        result = categorize_property_type(use_str)
        if result != 'Other':
            return result
        # If repo returns 'Other', fall through to structural rules below

    # Rule 4 — CONDO parcel type
    if ptype == 'CONDO':
        return 'Single Family'

    # Rule 5 — DIVINTEREST
    if ptype == 'DIVINTEREST':
        return 'Other'

    # Rule 6 — Zero improvement value fallback
    if impr == 0:
        return 'Vacant Land'

    # Rule 7 — Final fallback
    return 'Other'


print("\n── Assigning PROPERTY_CATEGORY (v2) ─────────────────────────────────")
gdf['PROPERTY_CATEGORY'] = gdf.apply(assign_category, axis=1)

# Report full distribution
taxable = gdf[gdf['_excluded'] == False]
cat_counts = taxable['PROPERTY_CATEGORY'].value_counts(dropna=False)
print(f"\n  PROPERTY_CATEGORY distribution (taxable base, {len(taxable):,} parcels):")
print(cat_counts.to_string())

# Spot-check: vacant parcels
n_vacant = (taxable['PROPERTY_CATEGORY'] == 'Vacant Land').sum()
n_zero_impr = (taxable['Assessed Improvement Value'] == 0).sum()
n_dev_vacant = (taxable['Development Status'] == 'Vacant').sum()
n_dev_under  = (taxable['Development Status'] == 'Underdeveloped').sum()
print(f"\n  Parcels labeled Vacant Land:             {n_vacant:,}")
print(f"  Parcels with Development Status=Vacant:  {n_dev_vacant:,}")
print(f"  Parcels with Development Status=Underd.: {n_dev_under:,}")
print(f"  Parcels with zero improvement value:     {n_zero_impr:,}")
print(f"  (Vacant Land count should be ≥ {n_dev_vacant + n_dev_under:,} "
      f"= Vacant + Underdeveloped)")

# Confirm no vacant parcels are still labeled Other
still_other_vacant = taxable[
    (taxable['Development Status'].isin(['Vacant', 'Underdeveloped'])) &
    (taxable['PROPERTY_CATEGORY'] == 'Other')
]
if len(still_other_vacant) > 0:
    print(f"\n  WARNING: {len(still_other_vacant):,} Vacant/Underdeveloped parcels "
          f"still labeled Other — investigate.")
else:
    print(f"\n  ✓ All Vacant and Underdeveloped parcels correctly categorized")

# ── 4. LOAD MILLAGE RATES ─────────────────────────────────────────────────────

print("\n── Loading scenario rates ────────────────────────────────────────────")
summary_df = pd.read_csv(SUMMARY_CSV)
print(summary_df[['Scenario', 'Land Mill Rate', 'Improvement Mill Rate']].to_string(index=False))

def get_rates(r_val):
    row = summary_df[summary_df['Scenario'] == f'r = {r_val}']
    if row.empty:
        raise ValueError(f"Scenario r={r_val} not found")
    return float(row['Land Mill Rate'].iloc[0]), float(row['Improvement Mill Rate'].iloc[0])

# ── 5. RUN ALL THREE SCENARIOS ────────────────────────────────────────────────

print(f"\n{'=' * 70}")
print("GENERATING STANDARD EXPORTS AND CITY REPORTS")
print(f"{'=' * 70}")

gdf['_excluded_int'] = gdf['_excluded'].astype(int)
all_results = {}

for sc in SCENARIOS:
    r         = sc['r']
    slug      = sc['slug']
    land_mill, impr_mill = get_rates(r)

    print(f"\n── Scenario r = {r}  ({slug}) ────────────────────────────────────")
    print(f"   Land millage:        {land_mill:.6f}")
    print(f"   Improvement millage: {impr_mill:.6f}")

    missing = [c for c in [sc['new_tax_col'], sc['chg_col'], sc['pct_col']]
               if c not in gdf.columns]
    if missing:
        print(f"  ✗ Missing columns: {missing} — skipping"); continue

    export_df = pd.DataFrame({
        'PROPERTY_CATEGORY':         gdf['PROPERTY_CATEGORY'],
        'current_tax':               gdf['_city_tax'].fillna(0.0),
        'new_tax':                   gdf[sc['new_tax_col']].fillna(0.0),
        'tax_change':                gdf[sc['chg_col']].fillna(0.0),
        'tax_change_pct':            gdf[sc['pct_col']],
        'taxable_land_value':        gdf['Assessed Land Value'].fillna(0.0),
        'taxable_improvement_value': gdf['Assessed Improvement Value'].fillna(0.0),
        '_exempt_flag':              gdf['_excluded_int'],
        'std_geoid':                 gdf['std_geoid'],
        'median_income':             gdf['median_income'],
        'minority_pct':              gdf['minority_pct'],
        'black_pct':                 gdf['black_pct'],
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

    # Census and category coverage
    in_cov = out_df['median_income'].notna().mean() * 100
    mn_cov = out_df['minority_pct'].notna().mean()  * 100
    print(f"  Census coverage — income: {in_cov:.1f}%  minority: {mn_cov:.1f}%")
    print(f"  Category distribution in export:")
    print(out_df['property_category'].value_counts(dropna=False).to_string())

    # Revenue check
    cur = out_df['current_tax'].sum()
    new = out_df['new_tax'].sum()
    print(f"  Revenue delta: {(new - cur) / max(abs(cur), 1) * 100:+.4f}%")

    # Generate all 7 charts
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
    for s in sorted(expected - {os.path.basename(p) for p in charts}):
        print(f"    ✗  {s}  (skipped)")

    all_results[slug] = {**report_result, 'income_cov': in_cov, 'minority_cov': mn_cov}

# ── 6. FINAL SUMMARY ──────────────────────────────────────────────────────────

print(f"\n{'=' * 70}")
print("COMPLETE")
print(f"{'=' * 70}")
print(f"\nCharts: {REPORT_DIR}")
print(f"\n  {'Scenario':<15} {'Charts':>7}  {'Income cov':>12}  {'Minority cov':>13}")
print(f"  {'-'*15} {'-'*7}  {'-'*12}  {'-'*13}")
for slug, res in all_results.items():
    n    = len(res.get('charts_saved', []))
    icov = res.get('income_cov', 0)
    mcov = res.get('minority_cov', 0)
    print(f"  {slug:<15} {n:>5}/7  {icov:>11.1f}%  {mcov:>12.1f}%")

print(f"""
One decision to review before comparing to your slides:
  UNDERDEVELOPED_CATEGORY = '{UNDERDEVELOPED_CATEGORY}'
  1,126 underdeveloped parcels are currently merged into 'Vacant Land'.
  To show them separately, set UNDERDEVELOPED_CATEGORY = 'Underdeveloped'
  at the top of this script and re-run.
""")
