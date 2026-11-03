# Skill: Model the Policy

Sub-skill called from `model-city/SKILL.md` Step 2.

This skill covers everything from understanding the current tax system to implementing `current_tax` and the split-rate or abatement model. Read all sections before writing any code.

---

## Guiding Principle

**Understand the current system before touching the reform.** Every city has different assessment ratios, millage sources, exemption mechanics, and tax base definitions. Get `current_tax` right and matching an official figure before introducing `new_tax`.

---

## Part A — What You Need to Know First

### A1. What value is the millage applied to?

This is the most important question. Three common patterns:

| Pattern | Description | Cities |
|---|---|---|
| **Full market value** | Millage × market value / 1000 | Baltimore, South Bend |
| **Assessed value (fractional)** | Market value × assessment ratio, then × millage | Cincinnati (35%), most PA cities |
| **Tax Capacity** | State-defined formula applied to market value, then × levy rate | St. Paul (Minnesota) |

Ask: does the parcel file have a column for taxable assessed value, or must you compute it?

### A2. Where does the millage rate come from?

Five common sources, ranked by reliability:

1. **Official budget document** — most reliable; find the adopted millage rate for the specific levy
2. **State/county levy database** — e.g., Spokane County publishes per-parcel levy breakdowns
3. **Derived from actual tax bills** — if the parcel file has observed tax amounts, back-calculate: `millage = (tax_paid / taxable_value) * 1000`. Use Baltimore's approach.
4. **County assessor website** — look for "tax rates" or "millage rates" by tax district
5. **CAFR (Comprehensive Annual Financial Report)** — usually has the levy rate in the revenue section

**Do not guess. Do not use a statewide average.**

### A3. Which exemptions reduce the taxable base?

Three patterns:

| Type | How to apply |
|---|---|
| **Full exemption flag** | If flag = 1, `current_tax = 0` and exclude from revenue-neutral base |
| **Dollar amount column** | Subtract from improvement value first, then from land value if remainder |
| **Dollar amount + assessment ratio** | Apply dollar exemption to market value, then apply assessment ratio to remainder |

Always exclude fully-exempt parcels from the split-rate denominator so revenue neutrality applies only to taxable parcels.

### A4. Which levy or levies are we modeling?

- **City levy only**: Use only the city millage rate and city-taxable parcels
- **Full stack**: Sum all levies (city + county + school + special districts)
- **Per-levy (Spokane)**: Model each levy independently — some levies get the abatement, others don't

---

## Part B — Modeling the Current Tax

### B1. Basic Split-Rate (South Bend, most cities)

```python
MILLAGE = 3.3  # from official budget document
LAND_IMPROVEMENT_RATIO = 4.0

df['millage_rate'] = MILLAGE

current_revenue, _, df = calculate_current_tax(
    df=df,
    tax_value_col='taxable_value',   # land + improvement, post-exemption
    millage_rate_col='millage_rate',
    exemption_flag_col='full_exmp',  # 1 = skip this parcel
)
print(f"Modeled revenue: ${current_revenue:,.0f}")
print(f"Official revenue: ${TARGET_REVENUE:,.0f}")
print(f"Difference: {(current_revenue / TARGET_REVENUE - 1)*100:.2f}%")
```

### B2. Assessment Ratio (Cincinnati / Ohio — 35%)

Ohio taxes at 35% of market value. Apply the ratio before calculating current tax, and remember to apply it to both land and improvement columns before passing to the split-rate model.

```python
ASSESSMENT_RATIO = 0.35
CITY_MILLAGE = 6.1

# Apply relief/exemptions to market values first
parcels_model['taxable_market_land'] = parcels_model['LAND_MKT'].clip(lower=0)
parcels_model['taxable_market_improvement'] = (
    parcels_model['IMPR_MKT'] - parcels_model['total_relief']
).clip(lower=0)
parcels_model['taxable_market_total'] = (
    parcels_model['taxable_market_land'] + parcels_model['taxable_market_improvement']
)

# Apply 35% assessment ratio
parcels_model['taxable_assessed_land'] = parcels_model['taxable_market_land'] * ASSESSMENT_RATIO
parcels_model['taxable_assessed_improvement'] = parcels_model['taxable_market_improvement'] * ASSESSMENT_RATIO
parcels_model['taxable_assessed_total'] = parcels_model['taxable_market_total'] * ASSESSMENT_RATIO

parcels_model['millage_rate'] = CITY_MILLAGE
current_revenue, _, modeled = calculate_current_tax(
    df=parcels_model,
    tax_value_col='taxable_assessed_total',
    millage_rate_col='millage_rate',
)

# Split-rate also uses assessed values (ratio already baked in)
land_millage, improvement_millage, split_revenue, split_rate_modeled = model_split_rate_tax(
    df=split_rate_parcels,
    land_value_col='taxable_assessed_land',
    improvement_value_col='taxable_assessed_improvement',
    current_revenue=split_rate_target_revenue,
    land_improvement_ratio=LAND_IMPROVEMENT_RATIO,
)
```

**Why**: The assessment ratio scales both land and improvement uniformly, so it doesn't change relative tax burdens — but it must be applied consistently to both columns or the split-rate solver produces wrong millage rates.

### B3. Tax Capacity Approach (St. Paul / Minnesota)

Minnesota doesn't use a flat assessment ratio. Instead, the state defines "Tax Capacity" using class-rate schedules that vary by property type and value tier (e.g., first $500K of homestead residential at 1%, above at 1.25%). This is already computed in the parcel file as `TaxCapacity`.

```python
# Split TaxCapacity into land and improvement portions using EMV improvement ratio
st_paul_city['IR'] = (
    st_paul_city['EMVBuilding1'] / st_paul_city['EMVTotal1']
).fillna(0).clip(0, 1)

st_paul_city['TaxCapacity_Land'] = (1 - st_paul_city['IR']) * st_paul_city['TaxCapacity']
st_paul_city['TaxCapacity_Improvements'] = st_paul_city['IR'] * st_paul_city['TaxCapacity']

# Current tax = TotalTax1 (observed; full bill or city-only depending on scope)
st_paul_city['current_tax'] = st_paul_city['TotalTax1']
current_revenue = st_paul_city['current_tax'].sum()

# Split-rate operates on Tax Capacity components (not EMV)
land_millage, imp_millage, revenue, df = model_split_rate_tax(
    df=st_paul_city,
    land_value_col='TaxCapacity_Land',
    improvement_value_col='TaxCapacity_Improvements',
    current_revenue=current_revenue,
    land_improvement_ratio=4.0,
)
```

**Why**: Using Tax Capacity preserves Minnesota's class-rate preferences (farmers, low-value homes, etc. pay lower effective rates). Using raw EMV would eliminate that progressivity unintentionally.

**Condo collapse required**: Ramsey County assigns condo units token $1,000 land values. Before modeling, collapse condo units by PlatID, summing TaxCapacity and TotalTax1, and imputing land value from the neighborhood median improvement ratio of non-condo parcels. See St. Paul notebook for full code.

### B4. Derived Millage from Actual Bills (Baltimore)

When you have both observed tax amounts and taxable values in the parcel file, derive the millage rather than looking it up:

```python
# Baltimore: derive millage from observed CITY_TAX / ARTAXBAS
city_millage = round(
    (gdf["CITY_TAX"].sum() / gdf["ARTAXBAS"].sum()) * 1000.0, 4
)
print(f"Derived city millage: {city_millage:.4f} mills")

gdf["city_millage"] = city_millage
target_revenue = float(gdf["CITY_TAX"].sum())

current_revenue, _, gdf = calculate_current_tax(
    df=gdf,
    tax_value_col="ARTAXBAS",
    millage_rate_col="city_millage",
    exemption_flag_col="full_exmp",
)
```

**Why**: Baltimore already has a split-rate system (real property vs. personal property taxes differ). The observed bills are the most reliable ground truth.

### B5. Dual Millage — Homestead vs Non-Homestead (Rochester / New York)

New York allows cities to set different millage rates for homesteads (1-4 unit residential) vs. non-homesteads (everything else). Model them separately:

```python
homestead_codes = ['210', '215', '220', '230', '240', '241', '260', '270', '311', '312', '314', '322']
gdf['homestead_flag'] = gdf['CLASSCD'].astype(str).isin(homestead_codes).astype(int)

homestead_gdf = gdf[gdf['homestead_flag'] == 1].copy()
nonhomestead_gdf = gdf[gdf['homestead_flag'] != 1].copy()

homestead_gdf['millage_rate'] = 7.05     # from city budget
nonhomestead_gdf['millage_rate'] = 15.7

# Model each group independently — separate revenue neutrality
h_land_rate, h_bldg_rate, h_revenue, h_results = model_split_rate_tax(
    df=homestead_gdf,
    land_value_col='CURRENT_TAXABLE_LAND',
    improvement_value_col='CURRENT_TAXABLE_IMPR',
    current_revenue=homestead_gdf['current_tax'].sum(),
    land_improvement_ratio=10.0,  # Rochester uses 10:1
)

nh_land_rate, nh_bldg_rate, nh_revenue, nh_results = model_split_rate_tax(
    df=nonhomestead_gdf,
    land_value_col='CURRENT_TAXABLE_LAND',
    improvement_value_col='CURRENT_TAXABLE_IMPR',
    current_revenue=nonhomestead_gdf['current_tax'].sum(),
    land_improvement_ratio=10.0,
)

# Recombine
df = pd.concat([h_results, nh_results]).sort_index()
```

### B6. Building Abatement / Stacking Exemption (Spokane)

Spokane uses a 75% improvement exemption (with floor), applied independently per levy. Some levies are subject to the abatement; others are not.

```python
from lvt.lvt_utils import model_stacking_improvement_exemption

TARGET_LEVIES = ['SD081 Spokane B&I', 'SD081 Spokane General', 'Spokane General', 'Spokane General Senior Lift']
IMPROVEMENT_EXEMPTION_PCT = 0.75
BUILDING_ABATEMENT_FLOOR = 100_000

# For each target levy — model abatement independently
for levy_name in TARGET_LEVIES:
    levy_parcels = df[df['tax_code_area'].isin(areas_with_levy)].copy()
    levy_revenue = (levy_parcels['levy_tax']).sum()

    df_modeled = model_stacking_improvement_exemption(
        df=levy_parcels,
        land_col='LAND_VALUE',
        improvement_col='IMPROVEMENT_VALUE',
        current_revenue=levy_revenue,
        improvement_exemption_pct=IMPROVEMENT_EXEMPTION_PCT,
        building_abatement_floor=BUILDING_ABATEMENT_FLOOR,
    )
    # Update new_tax for these parcels in the main df
    df.loc[levy_parcels.index, 'new_tax'] += df_modeled['new_tax']

# Non-target levies: new_tax = current levy tax (no change)
```

**Why per-levy**: Revenue neutrality in Spokane must be maintained separately for each levy district — voters approved each levy independently, and each has its own tax base and legal constraints.

---

## Part C — Revenue Validation

Always check before proceeding to Step 3.

```python
gap_pct = (current_revenue / OFFICIAL_REVENUE - 1) * 100
print(f"Modeled: ${current_revenue:,.0f}   Official: ${OFFICIAL_REVENUE:,.0f}   Gap: {gap_pct:+.2f}%")
assert abs(gap_pct) < 5.0, f"Revenue gap too large: {gap_pct:.1f}%"
```

Acceptable gap: < 2% ideal, < 5% acceptable. If > 5%:
- Check whether TIF parcels are being included (exclude them from city revenue)
- Check whether fully-exempt parcels are being excluded
- Check whether the assessment ratio is applied correctly (not double-applied)
- Check whether the millage source matches the levy being modeled
- Check whether dollar exemptions are subtracted from the correct column

---

## Part D — Property Category Mapping

After current_tax is correct, classify every parcel into a standard category. The `PROPERTY_CATEGORY` column is required by `save_standard_export()`.

Standard categories:
```
Single Family Residential
Small Multi-Family (2-4 units)
Large Multi-Family (5+ units)
Other Residential
Condominium
Townhome / Rowhouse
Mixed Use
Commercial
Retail / General Commercial
Office / Commercial Condo
Hotel
Other Commercial
Industrial
Vacant Land
Agricultural
Transportation - Parking
Exempt
Other
```

Use the most specific category the source data can support without inventing detail. In dense cities with rich parcel class codes, do not collapse everything into broad residential/commercial buckets:

- Split residential condominiums into `Condominium` when condo/unit codes exist.
- Split townhomes, rowhomes, and rowhouse developments into `Townhome / Rowhouse` when identified separately from detached single-family homes.
- Split mixed commercial/residential parcels into `Mixed Use` when the assessor has a mixed-use code or clear mixed-use description. If a source splits mixed-use parcels into separate residential and commercial assessment records, preserve the source split instead of forcing a mixed-use label.
- Split hotels, motels, and rooming/apartment hotels into `Hotel`.
- Split office buildings and commercial condominium units/garages into `Office / Commercial Condo`.
- Split retail, shopping centers, supermarkets, one-story general commercial, and minor general-commercial improvements into `Retail / General Commercial`.
- Use `Other Commercial` for commercial subtypes that are meaningful but not one of the above and not numerous enough to warrant a city-specific category.
- Keep `Other Residential` and `Other` small. If either exceeds roughly 10% of parcels or contains a policy-relevant class, inspect the underlying codes and add a better category mapping.

Key override rule: **any parcel with $0 improvement value → 'Vacant Land'**, regardless of use code. This catches empty lots that are coded as "Commercial" or "Residential" by mistake.

```python
# Override: $0 improvement → Vacant Land
df.loc[df['improvement_value'] <= 0, 'PROPERTY_CATEGORY'] = 'Vacant Land'
```

### D2. Optional: Waterfront single-family split

Shoreline proximity is one of the largest single drivers of residential land
value. In a city with meaningful water frontage, the "Single Family
Residential" median masks a small band of high-land-value shoreline parcels
that behave very differently from the interior — which is exactly the signal an
LVT analysis is trying to surface. For those cities, split single-family homes
into waterfront and non-waterfront.

**When to apply it (the "decent chunk" gate).** Only split when *both* hold:
1. The city has significant water frontage — a coast/bay/estuary, or a major
   lake or wide river (not just creeks and retention ponds).
2. A non-trivial share of the single-family stock is actually on the water —
   as a rule of thumb, **≥ ~2% of single-family parcels within 100 m of
   significant water, or several hundred parcels**, whichever the city's size
   makes meaningful. Use `waterfront_share()` to check before deciding.

If the city is plainly landlocked, or the waterfront SFH count is trivial, skip
this — do not add a near-empty category.

**How (uses `lvt/waterfront_utils.py`, the single canonical utility — never
hand-roll a per-city distance computation):**

```python
from lvt.waterfront_utils import (
    add_waterfront_flags, waterfront_share, split_single_family_by_waterfront,
)

# 1. Flag parcels by distance to nearest TIGER water (cached in the city data dir).
#    min_water_area_m2 screens out drainage ponds; ~10,000 (1 ha) is a good floor.
gdf = add_waterfront_flags(
    gdf, fips_code=FIPS, cache_dir=DATA_DIR, min_water_area_m2=10_000,
)

# 2. Decide with the gate above.
stats = waterfront_share(gdf, category_col='PROPERTY_CATEGORY')
print(stats)  # {'sfh_total': ..., 'waterfront_count': ..., 'waterfront_pct': ...}

# 3. If it clears the gate, split SFH (default flag = within 100 m).
if stats['waterfront_pct'] >= 2.0:
    gdf = split_single_family_by_waterfront(gdf, category_col='PROPERTY_CATEGORY')
    # → 'Single Family Residential — Waterfront' vs 'Single Family Residential'
```

The split only rewrites the category string, so it flows through
`calculate_category_tax_summary`, the city report, and `save_standard_export`
with no other changes. Do this **after** classification and **before** the
census join / export. Record in a markdown cell that the split was applied (and
the waterfront share), or that it was considered and skipped.

---

## Part E — Output Checklist

Before leaving Step 2, verify:
- [ ] `current_tax` column exists and is non-zero for taxable parcels
- [ ] Revenue match within 5% of official figure (source documented)
- [ ] `PROPERTY_CATEGORY` column exists with no nulls
- [ ] Assessment ratio, millage rate(s), and exemption logic documented in a markdown cell
- [ ] Fully-exempt parcels identified and excluded from revenue-neutral base
- [ ] `taxable_land_value` and `taxable_improvement_value` columns exist (used by `save_standard_export`)
