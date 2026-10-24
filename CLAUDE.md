# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

LVTShift is a toolkit for modeling Land Value Tax (LVT) policy shifts in U.S. cities/counties. It analyzes how shifting from traditional property taxes to land value taxes affects neighborhoods, property types, and demographic groups. Created by the Center for Land Economics.

The repo is designed to be driven by an agent. The user-facing entry points are four skills (see Skills below); most work happens inside per-city Jupyter notebooks that import the `lvt/` package.

## Environment Setup

```bash
# Any Python 3.11+ environment works; install the requirements into it
pip install -r requirements.txt
# Make the lvt package importable from anywhere, including sibling repos
# (editable: Python reads this checkout's lvt/ folder, so there is no copy to drift)
pip install -e . --no-deps

# Environment variables
cp env.template .env
# Add CENSUS_API_KEY (free at https://api.census.gov/data/key_signup.html)
```

Notebooks use the standard `python3` Jupyter kernel (provided by `ipykernel`, which is in the requirements). Execute headlessly with:

```bash
jupyter nbconvert --to notebook --execute --inplace \
  --ExecutePreprocessor.timeout=600 \
  --ExecutePreprocessor.kernel_name=python3 \
  model.ipynb
```

### Loading `.env` in notebooks and scripts

Python does **not** auto-load `.env` files. `os.getenv("CENSUS_API_KEY")` only sees keys
that are already in the process environment, which on Windows (and a fresh shell on
Mac/Linux) usually means it returns an empty string even when `.env` is present.

Every notebook or script that needs `CENSUS_API_KEY` (or any other secret) must call
`load_dotenv` explicitly. The standard pattern, used by the canonical notebook
template (`.claude/skills/build-notebook.md`):

```python
import sys
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, '../..')
REPO_ROOT = Path('../..').resolve()
load_dotenv(REPO_ROOT / '.env')
```

Without this, `os.getenv("CENSUS_API_KEY")` returns `""` and the Census/equity sections
silently fall through.

## Skills

The four user-facing skills live in `.claude/skills/` with slash-command wrappers in `.claude/commands/`:

- **model-city** (`/lvt-city`) — full pipeline for a new city. Orchestrates the sub-skills `discover-data.md` → `model-policy.md` → `build-notebook.md` → `validate.md`, then runs the canonical census join + standard export + 7-PNG report.
- **legality-analyzer** (`/legality-analyzer`) — legal pathway brief for a city/state, grounded in `docs/LVT_LEGAL_DECISIONING_GUIDE.md`.
- **explain-model** (`/explain-model`) — plain-language methodology audit of an existing city model.
- **refine-model** (`/refine-model`) — re-run an existing model with changed parameters (split ratio, abatement, exemptions, levy scope) via minimal notebook edits.

When modifying the pipeline, change the skill files — they are the canonical spec; this file is only a map.

## Architecture

The core modules live in the `lvt/` package, used from Jupyter notebooks in `cities/`:

```
lvt/cloud_utils.py     → Fetch parcel data from county ArcGIS FeatureServers
lvt/census_utils.py    → Fetch Census demographics, spatial join to parcels
lvt/lvt_utils.py       → Tax modeling (split-rate, abatement, exemptions) — rate shifts
lvt/reassessment.py    → Revenue-neutral reassessment — base shifts (single + multi-district, decomposition) + IAAO ratio-study & equity metrics
lvt/policy_analysis.py → Identify vacant land, parking lots, development barriers
lvt/wage_tax_utils.py  → Wage-tax-for-land-tax swap — a different instrument, at tract granularity
lvt/ubi_utils.py       → Full land-rent capture + per-capita dividend — a rent flow, not an assessed stock
lvt/transit_utils.py   → GTFS feeds, routed walk-shed isochrones, OSM parking analysis
lvt/idle_land.py       → Vacant / surface-parking / paved / undeveloped parcels from 1 m land cover + OSM + roll (Scranton map, Philly comparison)
lvt/viz.py             → Scatter plots, quintile analysis, demographic charts, city report
lvt/parcel_map.py      → Per-parcel GeoParquet export + self-contained interactive HTML map
```

Notebooks import via the package:
```python
import sys; sys.path.insert(0, '../..')  # from cities/<city>/
from lvt.lvt_utils import model_split_rate_tax
from lvt.cloud_utils import get_feature_data_with_geometry
```

**Data flow**: Fetch parcels (cloud_utils) → classify property types → rebuild current tax → model LVT scenarios (lvt_utils) → merge Census demographics (census_utils) → `save_standard_export()` → `create_city_report()` (viz) → `save_parcel_map_export()` + `create_parcel_map()` (parcel_map)

### Key Design Patterns

- **Flexible column names**: Every function accepts column names as parameters (no hardcoded field names) to support different jurisdictions' data schemas
- **Revenue neutrality**: All tax models maintain identical total revenue. Uses `(target_revenue * 1000) / denominator` for rate calculation. Iterative solver (up to 40 iterations) when percentage caps exist
- **Millage rates are per $1000**: `model_split_rate_tax()` returns millage in per-$1000 units. Tax = `value * millage / 1000`
- **Exemption hierarchy**: Full exemption flags applied first → dollar exemptions to improvements → remaining to land
- **Centroid-based spatial joins**: Parcels joined to Census block groups via centroids in EPSG:3857 to avoid boundary edge cases
- **Census fetching**: TIGERweb block-group request (Layer 1), automatically chunked by tract for very large counties; calls run in a background thread with a 90-second timeout
- **Undefined is NaN, not 0**: a percent change on a $0 current bill, or a change on a missing bill, is undefined. Never `.replace([np.inf, -np.inf], 0).fillna(0)` a tax change; use `lvt_utils.pct_change_of_totals`. The export builder recomputes `tax_change` itself and leaves it NaN where a bill is missing; `tests/test_lvt_utils_regressions.py` pins both.

### The recurring failure shape: a guard that reads a different column than the one that broke

Every silent data defect found in this repo so far has the same shape - a check passes because it
reads a quantity the bug does not touch, so a wrong number never produces a symptom. When adding a
guard, ask what it would read if the thing you fear went wrong, and assert against the **billed or
observed** quantity rather than against a reconstruction.

- **LVT-UBI baseline** (`ubi_utils`) - revenue validation ran on `taxable_total` (pure OPA) and
  passed at +0.00% while the modeled LYCD baseline was over 12% too high; the zero-sum check was
  internally consistent with the same wrong baseline. Now guarded by `baseline_total_col`.
- **DC exemptions** - `TAXRATE` is nonzero on large institutional parcels billed `ANNUALTAX = 0`,
  so keying exemption off the nominal rate silently admitted billions of assessed value.
- **Philadelphia lot area** - a pre-computed `Shape__Area` from an EPSG:3857 service is inflated by
  `1/cos^2(lat)` with nothing downstream to notice. Guarded by a total-area assert.
- **Parcel cache vintage** - `opa_properties_public` always carries the latest assessment year, so
  modeling one year's values against another's rates has no visible symptom. Guarded by the
  year-keyed cache filename.
- **A guard can itself encode a wrong expectation.** The LVT-UBI LYCD band was derived from the two
  models' split-rate land *millages* rather than their land *bases* - revenue neutrality at 4:1
  makes millage vary as `4000*T/(4L+B)`, so the millage ratio is
  `(4*L_lycd+B_lycd)/(4*L_opa+B_opa)` = 1.257, a different quantity from `L_lycd/L_opa`. The band
  rejected a correct run. A guard's expected value needs a derivation, not a recollection.

### The second recurring shape: a claim that outran the artifact it cites

The first shape is a guard reading the wrong column. The second is prose - or a hand-typed
constant - asserting what the project's own artifacts no longer support. No test catches it,
because nothing tests prose. Instances so far: a headline kept after new bounds contradicted
it; an assumption's arithmetic reported as a demonstration (true in half the export, stated
with none of its conditions); a sibling-repo constant retyped inside the block warning against
retyping it, whose own audit had found the same shape at 36-44%; unsourced parameters exported
under authoritative column names. Each was written by the agent that built the thing it
described.

The remedy is mechanical, not vigilance: make the artifact the source (a test that reads the
sibling JSON, a provenance frame exported beside the data), and point prose at generated
tables instead of restating their values.

### The third recurring shape: a synthetic test that agrees with itself

The first two shapes are about reading the wrong quantity. This one is about reading it on the
wrong data. A test written from the same mental model as the code it checks will pass on
fixtures built from that model, and only real data disagrees. Every instance so far was caught
by running against the parcel cache or the sales file, never by the test suite:

- **The abated-parcel override.** A unit test compared two calls that were equally wrong and
  passed; the cache showed $16.4B of building value silently zeroed.
- **Pro-rata institutional relief** classified as an abatement — the synthetic fixture had no
  parcel whose relief was split across both lines in the parcel's own ratio.
- **The exemption rule's reconstruction residual** read as a tax change, on parcels whose
  recorded homestead disagrees with the relief actually applied. No fixture has that
  disagreement, because it is a data defect, not a rule.
- **Borrowed incentive tests** (openavmkit's Lars-Tests) that, run on Philadelphia's geography,
  rewarded a *flat* land surface — one compared values across a lot-size quartile, another
  grouped by clusters whose members average 7 km apart. Both ranked the candidates backwards
  until their grouping was checked against the ground.
- **A stubbed calibration** that left raw values in files the pipeline reads as calibrated.
  Nothing errored; the only symptom was the numbers, and only a diff against the other file
  showed it.

The remedy is a habit, not a guard: **before trusting a new mechanism, run it on the real data
and look at the distribution, not just the assertion.** Where the fixture cannot express the
failure — a data defect, a real geography — say so in the test and check the cache instead.

### Jurisdiction-Specific Patterns

The tax base works differently by state; `model-policy.md` documents all real patterns with code. Examples: Ohio's 35% assessment ratio (Cincinnati), Minnesota Tax Capacity class rates (St. Paul), derived millage from observed bills (Baltimore), dual homestead/non-homestead rates (Rochester), per-levy abatement (Spokane), Texas entity-specific homestead/over-65 exemptions (Bryan, College Station).

One recurring pattern worth knowing: **condo collapse** (St. Paul / Ramsey County). Some assessors give condo units token land values ($1,000/unit). Before modeling, condos are collapsed by `PlatID` into buildings — summing values/taxes, imputing the land/building split from the neighborhood median improvement ratio of non-condo parcels, and unioning geometries via `collapse_geometries()`.

### Washington, DC — Derived-Bill Modeling and the Nominal-Rate-vs-Billed-Amount Trap

DC is a single unified city/county/state-equivalent government (FIPS 11001) — the real property tax is
one levy, no county/school layer to add. Parcel data comes from DC GIS's `Property_and_Land_WebMercator`
FeatureServer, Layer 40 (Owner Polygons / Common Ownership Layer), which merges geometry with CAMA
assessed values and a **live-computed per-parcel tax bill (`ANNUALTAX`)** — DC is unusual among modeled
cities in that `current_tax` is taken directly from this observed-bill field (the Baltimore B4 pattern
in `model-policy.md`) rather than reconstructed from class + value, since `ANNUALTAX` already reflects
DC's per-class rate brackets, the Homestead Deduction, Senior/Disabled 50% relief, and any mixed-use
blending exactly as billed.

**Exemption trap: flag full exemption off `ANNUALTAX <= 0` (the billed amount), never `TAXRATE == 0`.**
Large civic/institutional parcels carry a nonzero nominal Class 2 rate but are billed nothing; letting
their assessed value into the land base collapses the solved millage. See the guard-shape rule above.

**Relief mechanics** (`HSTDCODE` column, verified empirically against the data): `1` = Homestead
Deduction only ($91,950 for TY2026); `5` = Homestead + Senior, and `3` = Homestead + Disabled — both
get an *additional* 50% cut on the computed bill (confirmed: `ANNUALTAX / (CAPCURR * TAXRATE / 100)`
≈ 0.50 for codes 3/5, ≈ 1.00 for codes 1/7); `7` = a rarer homestead variant, no 50% cut. The reform
preserves this structure via `model_split_rate_tax`'s `exemption_col` (dollar homestead deduction) and
`credit_rate_col` (0.5 for codes 3/5) parameters.

**Structural result — residential goes up, not down. This is not a bug.** DC already taxes commercial
property at roughly double the residential rate, so a single citywide land/improvement millage pair
solved across all classes equalizes that differential: commercial buildings get a large cut while
residential land rises to meet the new blended rate. A real consequence of flattening an
already-differentiated class-rate system, verified against each class's current rate.

**Known revenue gap, documented and not closed — do not re-chase it.** Modeled current-tax revenue
undershoots DC OCFO's Real Property estimate by roughly 9%. Already ruled out: duplicate SSLs, condo
double-counting, assessment-year confusion. Leading unconfirmed hypothesis: OCFO's total includes
Public Utility Real Property, assessed by a separate OTR unit and absent from this FeatureServer.

**Vacant/Blighted (Class 3/4) reaches idle *buildings*, not idle *land*** — a real loophole,
confirmed in the data, and the reason `PROPTYPE`-based "Vacant Land" jumps so hard under the
reform: DC barely taxes bare land today whatever the nominal vacant-property rate says. Full
statutory analysis, the registration safe harbors and the demolition cliff:
`docs/WASHINGTON_DC_VACANT_LAND.md`.

### lvt_utils.py (core modeling)

Key functions:
- `model_split_rate_tax(df, land_value_col, improvement_value_col, current_revenue, land_improvement_ratio)` → returns `(land_millage, imp_millage, revenue, df_with_new_tax)`
- `model_full_building_abatement` / `model_stacking_improvement_exemption` → building-exemption pathway
- `calculate_current_tax(...)` → rebuild the existing system, honoring exemptions
- `calculate_category_tax_summary(df, category_col, current_tax_col, new_tax_col)` → summary stats by property type
- `save_standard_export(...)` → standardized 16-column per-parcel CSV in `analysis/data/<city>.csv`
- `build_standard_export_frame(...)` → shared column builder behind both `save_standard_export` and the parcel-map export (so the two never drift)
- `ensure_geodataframe(df)` → handles WKT, WKB hex, and binary geometry encodings

### parcel_map.py (interactive maps)

- `save_parcel_map_export(gdf, city, output_path, model_type, land_millage, improvement_millage, ...)` → GeoParquet in `analysis/maps/<city>.parquet` with geometry (WGS84) + the standard tax columns + optional per-parcel identity columns (`parcel_id`, `parcel_url`, `owner_name`, `owner_address`). These identity columns are intentionally NOT in the flat cross-city CSV. All identity params are optional and column-name-driven.
- `create_parcel_map(source, city, ...)` → interactive HTML (`analysis/reports/<city>/parcel_map.html`) coloring parcels by `tax_change_pct` (green = pays less, red = pays more), road/satellite base-map toggle, fullscreen, click-for-details (land/building values + county-record link), and a click-through gallery of the report PNGs. Small/medium cities get a self-contained Leaflet file (parcel data embedded, `simplify_tolerance_m` default 2 m lightens only the viewer geometry, not the stored parquet). Cities above `tile_threshold` (default 100k parcels) instead get **vector tiles** — see `create_parcel_tile_map`.
- `create_parcel_tile_map(source, city, ...)` → builds `analysis/reports/<city>/<city>.pmtiles` (tippecanoe) + a MapLibre GL viewer. Keeps large cities fast (e.g. Phoenix 525k: a 0.8 MB HTML + streamed tiles instead of a 190 MB single file). Must be served over a **range-capable** HTTP server — run `python3 scripts/serve_maps.py` (plain `python3 -m http.server` can't byte-serve PMTiles).

### cloud_utils.py (data fetching)

- `get_feature_data_with_geometry(dataset_name, base_url, layer_id, paginate=True)` — primary function
- Handles ArcGIS 2000-record pagination, CRS detection from layer metadata, ring→Polygon conversion

### census_utils.py (demographics)

- `get_census_data_with_boundaries(fips_code, year)` → returns `(census_data, census_boundaries)`
- `match_to_census_blockgroups(gdf, census_gdf)` → spatial join parcels to block groups
- Auto-detects large counties (Cook, LA, Harris, …) for chunked fetching

### Philadelphia — see `cities/philadelphia/CLAUDE.md`, and the research repo

This repo keeps Philadelphia's OPA standard model (`cities/philadelphia/model.ipynb`), its cache
builder and `lvt/philadelphia.py`; the OPA/Carto data patterns they rest on (classification
overrides, assessment-vintage and lot-area traps, abatement and homestead rules, the parcel
cache) are in the **directory-scoped** `cities/philadelphia/CLAUDE.md`. Read it before touching
any Philadelphia notebook, cache or export.

The Philadelphia research program moved on 2026-09-28 to the private sibling repo
`Progress-and-Poverty-Institute/philly_land_tax_research` (`../philly_land_tax_research`), with
its history: the LYCD, post-abatement, reassessment, wage-tax, LVT + UBI, single-tax and OCD
notebooks; the ownership, deck, one-pager, web-map and opportunity-site work; the land-surface
and ownership papers; the age-by-tenure analysis; and the dated audits. It imports `lvt` from
this checkout through an editable install and reaches the gitignored data here through
junctions, so **a change to `lvt/` or to the shared data affects it**. A path cited in this
repo that is not here is in that one.

## Notebooks

Located in `cities/<city>/model.ipynb`. Each follows the 7-section template in `.claude/skills/build-notebook.md`:
1. Imports, constants, `.env` load
2. Fetch/load parcel data from county GIS (cached in `cities/<city>/data/`)
3. Classify parcels (exempt flags, property categories) and validate against official tax base data
4. Rebuild the current tax and check against the official revenue figure
5. Run the split-rate or abatement model
6. Optional exploration charts
7. Census join → `save_standard_export()` → `create_city_report()` → `save_parcel_map_export()` + `create_parcel_map()` (exact closing pattern — do not modify)

`save_standard_export` accepts an optional `parcel_id_col` parameter. When provided (e.g., `parcel_id_col='parcel_number'`), a `parcel_id` column is prepended to the output CSV, enabling downstream spatial joins without re-running the notebook.

## Documentation

- `cities/philadelphia/CLAUDE.md` — **directory-scoped**: the OPA/Carto data patterns behind
  Philadelphia's standard model and `lvt/philadelphia.py`. Loaded when working in that subtree;
  read it explicitly if you are reasoning about Philadelphia from elsewhere.
- `../philly_land_tax_research` — the Philadelphia research repo (see the Philadelphia section
  above). Its `CLAUDE.md` indexes its papers, the vacant-land valuation study, the web-map docs
  and the dated audits.
- `docs/LVT_MODELING_GUIDE.md` — step-by-step guide for adding a new city
- `docs/WAGE_TAX_SWAP_GUIDE.md` — methodology for the wage-tax-for-land-tax swap paradigm
- `docs/LVT_UBI_GUIDE.md` — methodology for the LVT + UBI (full land-rent capture) paradigm
- `docs/LVT_LEGAL_DECISIONING_GUIDE.md` — legal framework behind the legality-analyzer skill
- `docs/SINGLE_TAX_LEDGER_SPEC.md` — spec for the single-tax static ledger, plus its build deviations
- `docs/KAPPA_CAPITALIZATION_EVIDENCE.md` — published capitalization/incidence estimates
  behind the single-tax model's `kappa` bounds, and the mapping from what the literature
  measures to what the ledger needs
- `docs/WASHINGTON_DC_VACANT_LAND.md` — why DC's Class 3/4 regime reaches idle buildings, not idle land
- `docs/LYCD_LAND_MODEL_ROADMAP.md` — what LYCD is (the allocation method), its defects in order
  of consequence, the tunings possible inside the method, and what an assessor-grade land model
  would look like; also documents the LYCD-land reassessment notebook built alongside it
- `docs/LVT_MODELING_GUIDE_ARCHIVE.md` — legacy modeling guide (pre-refactor, kept for reference)

## Code Style

- No decorative comment headers (avoid `# =============================================================================`)
- Use simple comments (e.g., `# Step 2: Load data`)
- No decorative print statements (avoid `print("=" * 60)`)
- Extensive type hints (Optional, Union, List, Tuple)
- Detailed docstrings with Parameters/Returns sections

## Data Files

All data files (.csv, .xlsx, .parquet, .gpq, .geojson, .shp) are gitignored. Each city's scraped data is cached locally in `cities/<city>/data/`; notebooks auto-detect cached files and skip re-scraping when a recent file exists. Standard exports land in `analysis/data/` and reports in `analysis/reports/` (both gitignored). The `data_scrape` flag in notebooks controls whether to fetch fresh data or load from cached files.
The `*.csv` rule applies everywhere, so a hand-sourced *input* to an analysis script (budget
figures, rates) saved as a CSV silently never reaches git; keep such inputs in the script with a
source comment per value (e.g. `REVENUES` in `analysis/howard_tax_swap/swap_scenarios.py`).

## Batch Execution

`scripts/run_all_cities.py` runs all city notebooks via `nbconvert`. It:
- Auto-detects missing data caches and patches `data_scrape = 0 → 1` (handles both `data_scrape` and `scrape_data` variable names)
- Executes to a throw-away `_executed.ipynb`, leaving the source notebook untouched
- Reports pass/fail and CSV row counts

`scripts/patch_notebooks.py` applies idempotent fixes to notebook cells — safe to re-run. Add new patches as functions there rather than editing notebooks manually.

## Ratio Harmonization

All runnable city notebooks are set to 4:1 split-rate. The canonical variable names are:
- `LAND_IMPROVEMENT_RATIO = 4.0` (most cities)
- `MODEL_TYPE = 'split_rate:4.0'` (canonical string — use colon format, not underscore)

## Known Blocked Cities

**Cincinnati** — The original `CAGIS_Open_Data` ArcGIS service (layer 12) returns 400 errors. The nearest replacement (`Cincinnati_Parcels_Indicators_2025/127`) is missing `MKTLND` (land value), which is required for LVT modeling. Notebook has auto-scrape fallback code but will fail until a new source with parcel-level land values is found.

**Spokane** — After scraping parcel geometry, the notebook requires `charge_info_1.xlsx` and `charge_info_2.xlsx` — per-parcel levy charge tables manually downloaded from Spokane County. No download URL exists in the codebase. Obtain from: https://gisdatacatalog-spokanecounty.opendata.arcgis.com/pages/parcel-data-file-downloads

**Denver** — Depends on manually-assembled `data/1-assemble-universe.parquet` and `data/_just_city_county_millage_2024.csv` (never committed, created by Lars Doucet locally). Notebook also has syntax errors (mismatched quotes) and non-standard property categories. Needs a full rebuild from public data sources.

## Fort Collins Notes

The `build_fort_collins_propinfo_cache.py` script (fetches per-parcel state tax relief from the Larimer County Treasurer API) was never committed. The notebook now defaults to `owner_tax_share = 1.0` when the script/cache is absent, which slightly overstates current tax for parcels receiving state relief but does not affect LVT % change calculations.

The Larimer County GIS parcel shapefile (`GIS_ParcelOwnerSHP.zip`) contains date fields (`DEEDDATE`, `SALEDATE`, `UPDATEDATE`) with year=0 values that Fiona cannot parse. Fix: `gpd.read_file(url, ignore_fields=['DEEDDATE', 'SALEDATE', 'UPDATEDATE'])`.

## Scranton Notes

The `GISViewer/Parcels` layer `cities/scranton/model.ipynb` fetches has every assessment field null. `GISViewer/LandRecords/MapServer/85` carries owner, class code and land/building values, but they are the pre-reassessment roll. Usable for telling built from unbuilt parcels, not for modeling the 2026 repeal. The 2026 roll is a paid County IT extract (`analysis/scranton_repeal/rtk_tracker.csv`). The idle-land work and its sources are in `analysis/scranton_repeal/vacant_parking/README.md`.

## Notebook Data-Scrape Variable Names

Some notebooks use `scrape_data = 0` (Bellingham, Spokane) instead of `data_scrape = 0`. The batch runner patches both. When writing new notebooks, prefer `data_scrape` for consistency.

## Cook County / PTAXSIM Pattern (Oak Forest)

Oak Forest uses a different data stack than all other cities — CCAO open data + PTAXSIM SQLite database, loaded from the sibling project `C:/projects/oak-forest-profit-loss-map/`.

**Critical: always filter PTAXSIM queries to Oak Forest tax codes first.** The `pin` table has 1.86 million Cook County rows. A naïve `WHERE year = 2023` query takes forever. Always add `AND p.tax_code_num IN (SELECT tax_code_num FROM city_tcs)` where `city_tcs` filters by `agency_num = '030900000'`. Oak Forest has 53 tax codes; the filtered query runs in 0.1s.

**Taxable EAV approach.** Don't compute `(av_clerk - exemptions) × eq_factor` directly for the split-rate base. Instead, back-calculate from city_tax: `effective_taxable_eav = city_tax / (city_rate / 1000)`. This automatically handles TIF base-EAV constraints for the 453 TIF-district parcels — no manual TIF correction needed. PTAXSIM already computes city_tax as the city's proportional share of the total bill, which for TIF parcels reflects only the frozen base EAV.

**Scale PTAXSIM to match official levy.** The proportional method (`tax_bill_total × city_rate / total_code_rate`) produces ~2% above the official levy. Apply `scale = CITY_LEVY / raw_total` before using city_tax values.

**Census join: skip `get_census_data_with_boundaries`.** It downloads all of Cook County's ~1,300 block groups including TIGERweb boundaries — takes 600+ seconds and times out. Use `get_census_data('17031', 2022)` (ACS-only, fast) then filter to the 24 Oak Forest block groups already in `parcel_universe['census_block_group_geoid']`. The CCAO parcel universe pre-computes block group GEOIDs; set `df['std_geoid'] = df['census_block_group_geoid']` and skip `match_to_census_blockgroups` entirely.

**Improvement ratio quality.** CCAO board-certified AV has genuine parcel-level land/building splits (unlike Philadelphia's OPA which defaults 80% of parcels to 20% LR). Oak Forest median improvement ratio = 0.852, with <0.2% of parcels at any single default value. The split is trustworthy.

**Constitutional cap note (Illinois).** Illinois Art. IX §4(b) limits the class differential in Cook County to 2.5×. A 4:1 modeled ratio is illustrative but would require a constitutional amendment to implement. The legally achievable ratio under state enabling legislation is 2.5:1.
