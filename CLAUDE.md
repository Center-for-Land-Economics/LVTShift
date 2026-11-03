# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

LVTShift is a toolkit for modeling Land Value Tax (LVT) policy shifts in U.S. cities/counties. It analyzes how shifting from traditional property taxes to land value taxes affects neighborhoods, property types, and demographic groups. Created by the Center for Land Economics.

The repo is designed to be driven by an agent. The user-facing entry points are four skills (see Skills below); most work happens inside per-city Jupyter notebooks that import the `lvt/` package.

## Environment Setup

```bash
# Any Python 3.11+ environment works; install the requirements into it
pip install -r requirements.txt

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

Six core modules live in the `lvt/` package, used from Jupyter notebooks in `cities/`:

```
lvt/cloud_utils.py     → Fetch parcel data from county ArcGIS FeatureServers
lvt/census_utils.py    → Fetch Census demographics, spatial join to parcels
lvt/lvt_utils.py       → Tax modeling (split-rate, abatement, exemptions)
lvt/policy_analysis.py → Identify vacant land, parking lots, development barriers
lvt/transit_utils.py   → GTFS feeds, routed walk-shed isochrones, OSM parking analysis
lvt/waterfront_utils.py→ TIGER water fetch, per-parcel distance-to-water flags, waterfront SFH split
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

### Jurisdiction-Specific Patterns

The tax base works differently by state; `model-policy.md` documents all real patterns with code. Examples: Ohio's 35% assessment ratio (Cincinnati), Minnesota Tax Capacity class rates (St. Paul), derived millage from observed bills (Baltimore), dual homestead/non-homestead rates (Rochester), per-levy abatement (Spokane), Texas entity-specific homestead/over-65 exemptions (Bryan, College Station).

One recurring pattern worth knowing: **condo collapse** (St. Paul / Ramsey County). Some assessors give condo units token land values ($1,000/unit). Before modeling, condos are collapsed by `PlatID` into buildings — summing values/taxes, imputing the land/building split from the neighborhood median improvement ratio of non-condo parcels, and unioning geometries via `collapse_geometries()`.

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

## Notebooks

Located in `cities/<city>/model.ipynb`. Each follows the 7-section template in `.claude/skills/build-notebook.md`:
1. Imports, constants, `.env` load
2. Fetch/load parcel data from county GIS (cached in `cities/<city>/data/`)
3. Classify parcels (exempt flags, property categories) and validate against official tax base data
4. Rebuild the current tax and check against the official revenue figure
5. Run the split-rate or abatement model
6. Optional exploration charts
7. Census join → `save_standard_export()` → `create_city_report()` → `save_parcel_map_export()` + `create_parcel_map()` (exact closing pattern — do not modify)

## Documentation

- `docs/LVT_MODELING_GUIDE.md` — step-by-step guide for adding a new city
- `docs/LVT_LEGAL_DECISIONING_GUIDE.md` — legal framework behind the legality-analyzer skill
- `docs/LVT_MODELING_GUIDE_ARCHIVE.md` — legacy modeling guide (pre-refactor, kept for reference)

## Code Style

- No decorative comment headers (avoid `# =============================================================================`)
- Use simple comments (e.g., `# Step 2: Load data`)
- No decorative print statements (avoid `print("=" * 60)`)
- Extensive type hints (Optional, Union, List, Tuple)
- Detailed docstrings with Parameters/Returns sections

## Data Files

All data files (.csv, .xlsx, .parquet, .gpq, .geojson, .shp) are gitignored. Each city's scraped data is cached locally in `cities/<city>/data/`; notebooks auto-detect cached files and skip re-scraping when a recent file exists. Standard exports land in `analysis/data/`, reports (7 PNGs + `parcel_map.html`) in `analysis/reports/`, and per-parcel map GeoParquets in `analysis/maps/` (all gitignored).
