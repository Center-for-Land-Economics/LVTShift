# CLAUDE.md — Philadelphia

Directory-scoped guidance for `cities/philadelphia/`. This repo keeps the OPA standard model,
`model.ipynb` (so the cross-city comparison includes Philadelphia), its cache builder
`scripts/build_philadelphia_parcel_cache.py`, and `lvt/philadelphia.py`. The section below
documents the OPA / Carto data patterns all of that rests on.

The Philadelphia research built on this model (the LYCD and post-abatement notebooks, the
LYCD-land reassessment, wage-tax swap, LVT + UBI, single-tax ledger and OCD notebooks, the
ownership, deck, one-pager, web-map and opportunity-site scripts, and the dated audits) moved
on 2026-09-28 to the private sibling repo `Progress-and-Poverty-Institute/philly_land_tax_research`
(checked out at `../philly_land_tax_research`), with its history. Its own
`cities/philadelphia/CLAUDE.md` holds the four paradigm sections. **A path named below that is
not in this repo (a notebook, script, analysis folder or doc) is in that one.**

Read this alongside the root `CLAUDE.md`, which holds the repo-wide rules these sections
assume: the two recurring failure shapes, the design patterns, the module map, and the
Documentation index. Where a rule below says "see the guard-shape rule above", it means the
root file.

## Philadelphia — OPA / Carto Data Patterns

Philadelphia parcel data comes from the **OPA (Office of Property Assessment)** via Carto, not ArcGIS FeatureServer. Use `requests` + Carto SQL API directly — `get_feature_data_with_geometry` will not work here.

**Data sources:**
- OPA properties (current): `https://phl.carto.com/api/v2/sql?q=SELECT ... FROM opa_properties_public&format=csv` — geometry is WKB in `the_geom` column, parse with `gpd.GeoSeries.from_wkb()`
- Assessment history: `https://phl.carto.com/api/v2/sql?q=SELECT ... FROM assessments WHERE year=XXXX&format=csv` — use this for billing-year taxable values

**Assessment vintage matters.** The current `opa_properties_public` table always reflects the *latest* assessment year. Philadelphia reassesses annually, so current OPA values may be 1–2 assessment cycles ahead of the billing year you're modeling. For FY2024 modeling, `assessments WHERE year=2024` (not the current OPA table) reduces the city-levy cross-check gap from +21% to +5%. Always specify `year=` and join back to current OPA for geometry + category codes.

**Revenue structure:** Philadelphia is consolidated city-county. The published 1.3998% rate is **city (0.6317%) + school district (0.7681%)**. City budget documents report only the city's ~$796M share; school district is a separate budget (~$970M). When modeling the combined rate, validate the city-only portion (0.6317% × taxable base) against city actuals. A ~5% gap is normal delinquency.

**Exemptions are already in OPA taxable columns.** `taxable_land` and `taxable_building` already net out the Homestead Exemption, 10-year construction abatement, and institutional exemptions. Do not double-apply these.

**LOOP and Senior Freeze are NOT in OPA.** Philadelphia's Longtime Owner Occupant Program (LOOP) and Senior Citizen Tax Freeze are administered by Revenue, not OPA, and are not available as public parcel-level datasets. They contribute a small portion of the revenue gap (~1–3%), with assessment vintage mismatch being the dominant factor.

**OPA land/building split:** a large majority of improved parcels sit at a land ratio of exactly 0.200 (OPA's default formula), measured on FULL value (`taxable + exempt`). Measured on the taxable columns the share reads roughly half that, because the Homestead Exemption comes off the building line and drags a homesteaded parcel's taxable land share off 0.200 even though OPA assigned it 0.200 — always measure the split on full value. The default is least common on commercial, industrial and the top value decile. This attenuates split-rate impact. Document this limitation in the notebook. Measurement: `analysis/opa_split_equity/01_land_ratio_audit.py`.

**Philadelphia category classification uses four stacked overrides** (in order):
1. `taxable_building <= 0` → "Vacant Land" (catch-all for zero-improvement parcels)
2. Non-vacant OPA code AND `taxable_building <= 0` AND `taxable_land > 0` → split three ways by
   `lvt.philadelphia.split_zero_building_parcels` (see below)
3. Vacant OPA code (6/12/13) AND `taxable_building > 0` → "Improved Vacant Land" (OPA calls it vacant but carries a building record; ~1,500 parcels with small structures)
4. `full_exmp = 1` (both taxable values = 0) → "[OPA category] — Exempt" (reclassifies exempt parcels out of "Vacant Land" into typed buckets)

Always apply these in this order. Override 3 before Override 4 matters: the full-exempt check must come after the improved-vacant reclassification or some parcels can end up in the wrong bucket.

**A $0 taxable building line does not mean "abated" — it has three causes, and conflating them
silently revoked ~13K homesteaders' Homestead Exemption.** Override 2 originally treated every
non-vacant parcel with `taxable_building <= 0` as an active construction abatement. About half of
that cohort is instead an ordinary homesteaded rowhome whose building assessment is *smaller than
the Homestead Exemption*, so the exemption consumes the whole building line and spills onto land.
Those parcels then got the abated treatment — `model_building` restored from `exempt_building` —
which taxed away a homestead that every other homesteader in the city kept. By dollars the error
was invisible (the genuine abatements hold ~93% of the exempt building value, so the solved
millages barely moved), which is the guard-shape failure again: the aggregate that was checked is
not the quantity that broke.

The discriminator is the exempt total against the year's statutory Homestead Exemption, which is a
flat amount and so can never explain more than its own cap:
- `exempt_total > cap` → an abatement is present.
- `0 < exempt_total <= cap` → homesteaded parcel, restored to its OPA category. It keeps its
  exemption under the reform, so its $0 building line is already correct — do not impute one.
- `exempt_total == 0` → the improvement really is worth $0; leave it as Override 1 set it.

`split_zero_building_parcels` implements this and is shared by all six classifying notebooks
rather than copy-pasted (this bug was in all six). It takes the cap from
`tax_year_params(year).homestead_exemption` — never a literal, because the amount has moved four
times ($30K → $40K → $45K → $80K → $100K, empirically confirmed against the `assessments` billing
table) and the wrong year's cap moves parcels across the boundary with no visible symptom. It
guards itself against OPA's own per-parcel `homestead_exemption` flag and raises if fewer than 85%
of the parcels it called homestead-zeroed actually carry a homestead; a wrong cap drops that rate
to 6-24%.

**The homestead-zeroed parcels still show a large *percentage* increase (small in dollars), and
that is a real result, not residue of the bug.** Their entire remaining taxable base is land, so a
land-heavy millage hits all of it. Note this depends on OPA applying the Homestead Exemption to the
building line first and the remainder to land; had it been applied land-first these parcels would
show a *decrease*. The allocation is OPA's, not a modeling choice, but it is load-bearing for this
cohort.

**Abated parcels: restore the building value from `exempt_building`, do not impute it.** A parcel with an active abatement has `taxable_building = 0`, so it enters the split-rate base at LR=1.0 and looks like pure land. The reform scenario needs its real improvement value back. OPA already records it: `exempt_building` is the assessed building value being exempted, and `market_value − (taxable_land + exempt_land)` reproduces it to the dollar. Use it, with a `market_value − taxable_land` fallback for the mid-construction parcels where `exempt_building = 0`. Keep `current_tax` as actual (land-only). This previously imputed `model_building = 4 × taxable_land`, inverting OPA's 20%-land default ratio — a reconstruction standing in for an observed quantity, and biased: it recovered a median 0.72× of the recorded value (p10 0.23) and understated the restored base by ~20%. All four standard-export notebooks (`model.ipynb`, `model_post_abatement.ipynb`, `model_lycd.ipynb`, `model_lycd_post_abatement.ipynb`) now use the observed column, so each pre/post pair varies only the abatement treatment, and the OPA/LYCD pair varies only the land source, rather than also varying how the improvement value is estimated.

**The post-abatement notebooks' abated cohort is the union of two tests, not `split_zero_building_parcels` alone.** That test only catches a FULL (100%-exempt) construction abatement — one whose taxable building line nets to $0. A graduated residential abatement past year 1, the 90% commercial/industrial schedule, and a rehab abatement all leave a taxable building line, so the zero-building test alone cannot see them; relying on it there understated the post-abatement baseline and, in the LYCD notebook specifically, let a missed abatement's exemption be carried forward by `carry_forward_exemptions` as if it were unrelated relief instead of expiring. `lvt.philadelphia.expand_abatement_cohort` reads the full population from `scripts/build_philadelphia_abatement_classification.py`'s billed-history classification (`abatement_classification_ty<year>.parquet`) and unions it onto the zero-building cohort; both `model_post_abatement.ipynb` and `model_lycd_post_abatement.ipynb` call it (Override 3.5) and restore every abated parcel's gross building (`taxable_building + exempt_building`), not `exempt_building` alone, since a partially-abated parcel's taxable line would otherwise be dropped. The notebooks print both parts of the cohort and the restored building total. The zero-building test alone gets the parcels it misses wrong in direction, not just in size: their abatement is carried forward when it should expire.

The equity script in philly_land_tax_research (`scripts/philadelphia_equity_by_land_surface.py`) rebuilds its "abatements expired" treatment on this same cohort and asserts it reproduces both post-abatement exports to the cent, so the exports and the standard ones must be rerun together.

**`model_lycd.ipynb` briefly regressed to a `4 × land` imputation for abated parcels, on a rationale that was wrong even when written.** A version of that notebook reverted from `exempt_building` back to `model_building = 4 × model_land`, reasoning that `exempt_building` was "the post-abatement treatment" and that using it would make the pre-abatement LYCD scenario indistinguishable from `model_lycd_post_abatement.ipynb`. Both premises were false: restoring `exempt_building` only changes the reform-scenario building value, not `current_tax` (which stays land-only under the actual abatement either way), so it is not a post-abatement treatment. And once `model.ipynb` itself moved to `exempt_building`, the reverted `model_lycd.ipynb` differed from the OPA panel by imputation method *and* land source simultaneously — the opposite of the stated design goal of isolating land values alone. Fixed by restoring the shared pattern; see `docs/LYCD_LAND_MODEL_ROADMAP.md`'s former "open items" entry for the before/after numbers.

**The zero-building test finds only full abatements; the abatement stock is larger and runs on
three schedules.** `split_zero_building_parcels` sees a parcel only when its building line nets
to $0. Graduated residential abatements after year 1, the 90% commercial/industrial schedule,
and rehab abatements (which exempt only the value the work added) all leave a taxable building
line, so that test cannot see them. The full population is `reallocate_land_within_total`'s
`building_share` exemption kind, and not all of it is abatement.
`scripts/build_philadelphia_abatement_classification.py` sorts it by reading each parcel's billed
exempt share of building value across the `assessments` history (2015 on):
- a 10-point annual step down (relief starting TY2022+) → `new_graduated_residential`
- a held 0.90 (starting TY2022+) → `new_flat_90_commercial` (reaches commercial rehab, not only new buildings)
- a held 1.00 → `old_flat_100`; a flat partial share → `old_flat_partial` (mostly rehab)
- relief present since 2015 or earlier, or a share that *rises* after its first two years →
  `non_abatement_relief`. An abatement's share can only hold or fall. This relief grows at every
  reassessment and rarely follows a permit. Its program is unidentified, which sits awkwardly
  with the LOOP note above; do not label it LOOP without a source.

**Relief that reaches the land is not an abatement either.** The classification's population is
the `building_share` kind only, so a zero-building parcel whose non-homestead relief spills onto
land (the `total_share` kind) is never classified, and the zero-building test used to sweep it into
the abated cohort: on TY2026, ~2,600 mostly owner-occupied homes with relief since 2015 or earlier
that steps up at each reassessment. An abatement exempts the improvement only, so
`split_zero_building_parcels` now returns these as `total_value_relief`, restored to their OPA
category; `expand_abatement_cohort(category_map=...)` also releases zero-building parcels the
classification labels `non_abatement_relief`. Under a split rate, how such relief divides between
land and building is unset by statute, like the homestead's: `reallocate_land_within_total` takes
`total_value_relief` (force it to `total_share`, carried in dollars) and `relief_order`
(`building_first`, the default and OPA's recording, or `value_share`, pro rata).

**Don't use the homestead flag to find owner-occupants among abated parcels.** A property
with a 10-year residential abatement isn't eligible for the Homestead Exemption until the
abatement ends (phila.gov), so the flag is empty for exactly those parcels. Use the ownership
analysis's proxy instead: an individual owner whose mailing address is the property
(`analysis/ownership/philadelphia/owner_lib.py`, `opa_mailing.parquet`). Check it against the
homestead flag on non-abated homes, where the flag is valid, as
`scripts/philadelphia_abatement_phase_in.py` does.

Why this reads history rather than permits: a permit shows only that work was authorised.
L&I permits are the independent check instead, and the script raises if their agreement
collapses. Use the live `permits` table; `li_permits` is an archive ending March 2020 and
cannot see any post-reform abatement. L&I labels apartment buildings "Commercial" by construction
code, so its commercial/residential field is not the abatement class. Homestead amounts per year
are inferred from the modal exempt total and asserted against `tax_year_params`.

**Fully exempt SFR parcels are low-value homesteaders, not vacant lots.** ~27K SFR parcels with `market_value <= $80K` have their entire assessed value wiped out by the Homestead Exemption. They show up as `full_exmp=1` and end up in "Vacant Land" if not reclassified. Override 4 moves them to "Single Family Residential — Exempt."

**Kernel name:** On Windows, the `cle-venv-new` kernel may not be registered. Check `jupyter kernelspec list` and use the available kernel (e.g., `python3`) for `nbconvert --execute`.

**The four standard-export Philadelphia notebooks** are `model.ipynb` (OPA), `model_lycd.ipynb` (LYCD), `model_post_abatement.ipynb` and `model_lycd_post_abatement.ipynb`. All four export to `analysis/data/philadelphia*.csv` with a `parcel_id` column (via `parcel_id_col='parcel_number'` in `save_standard_export`). The wage-tax-swap, LVT-UBI, OCD and single-tax notebooks in the same directory each follow a different paradigm and export convention — see their sections below.

**The parcel cache is keyed by tax year: `cities/philadelphia/data/parcels_ty<YEAR>.gpq`.** Build it
with `python scripts/build_philadelphia_parcel_cache.py --year 2026`; the notebooks only read it and
raise a clear error if it is missing. The year suffix is deliberate — `opa_properties_public` always
carries the *latest* assessment year, so an unsuffixed cache makes it easy to model one year's
taxable values against another year's rate and revenue target with no visible symptom. The builder
emits the full column superset every notebook needs: the assessment value columns plus `pin` +
`total_area` (LYCD lot-area chain) and `owner_1` + `owner_2` (owner-concentration analysis in
`model.ipynb`). Keep `pin` integer-typed — the LYCD notebooks stringify it as a join key, and a float
dtype would add a `.0` suffix and break the PIN match. Also note Carto's `assessments.year` column is
varchar: the filter must be `WHERE year = '2026'` (quoted); an unquoted integer comparison returns
HTTP 400.

**Tax-year rates and revenue targets live in `lvt/philadelphia.py`, not in the notebooks.** Each
notebook sets `TAX_YEAR` and derives millage, the city-only cross-check rate, the validation target
and the cache path from `tax_year_params()`. This exists because the combined rate has been 1.3998%
for years while the *City/School split moved* (0.6317/0.7681 at TY2024 → 0.6159/0.7839 from TY2025),
so a hardcoded `city_only_rate = 6.317` keeps validating against the wrong denominator while nothing
about the combined-levy model looks wrong. Rates and targets are cited per year in that module. Note
TY2027 has **no** revenue target — its bills are not due until March 2027 — so the validation cell
skips the assert and labels the run forward-looking; its City/School split is carried forward from
FY2026 and is not independently confirmed.

**Lot area: never take a pre-computed `Shape__Area` from an ArcGIS service without checking the
service CRS.** The DOR parcels FeatureServer is EPSG:3857, where area is inflated by `1/cos^2(lat)`.
`scripts/fetch_dor_parcel_areas.py` fetches PIN-keyed areas and de-distorts them per parcel; the
notebooks assert total lot area stays under 1.5x the city. See the guard-shape rule above.

Worth knowing *why that barely moved the results*: LYCD land value is `zone_psf x area` where
`zone_psf` is itself `median(market_value / area)`, so the method is **exactly scale-invariant in lot
area** — a uniform area error cancels completely. Only *mixed* conventions and *relative* area errors
matter. Do not infer from the small headline movement that the area layer is unimportant; it is
load-bearing for any per-parcel land value and for anything that is not scale-invariant.

**Neither land surface is right about vacant land, and the error runs opposite ways.** Tested
against observed vacant-land sales (`scripts/vacant_land_ratio_study.py`): LYCD over-values it,
OPA under-values it, and correcting both cuts the LYCD/OPA land-base ratio from ~1.46x to ~1.22x
— so the surface disagreement that dominates every Philadelphia result here is mostly one
correctable error in one property class. Both surfaces are also severely dispersed on vacant
land (COD far above the IAAO standard), which is the quantified form of the LVT-UBI guide's
"assessment error becomes confiscation at full capture". Findings and limits:
`docs/VACANT_LAND_VALUATION.md`.

**Surface parking is hidden inside "Vacant Land", and only `building_code` separates it.**
OPA gives most parking lots `category_code = 6`, so the model's Vacant Land category
contains them. The separator is the `R*` building-code family: `RA` (non-commercial lot),
`RB` (non-paid commercial), `RE` (paid commercial), `^R[A-F]\d$` (lot with structure), plus
`^O[AB]\d$` for commercial parking garages. Three traps: `5R` is CONDO PARKING SPACE (~4.5K
individual deeded spaces, not lots — exclude); `R30`/`R10`/`R5x` are `ROW B/GAR` rowhouses,
so never match on `R*` alone; and `DD0`/`DE0` are office buildings whose descriptions merely
mention parking. `building_code` is **not** in the parcel cache — `analysis/ownership/philadelphia/fetch_opa_attributes.py`
pulls it as a sidecar. See `analysis/ownership/philadelphia/` for the ownership-by-type
analysis built on it, and note the City's Land Use layer is *not* a usable parking source
(it identifies ~200 parking parcels citywide against the assessor's ~2,200). The codes live
once, as `lvt.philadelphia.SURFACE_PARKING_CODES`, `SURFACE_PARKING_WITH_STRUCTURE_RE` and
`PARKING_GARAGE_RE`; the ownership analysis imports them.

**Commercial building types come from OPA's description, not its code.** The codes run two
schemes (commercial `LC0` warehouse, residential-style `O30` rowhouse), so
`lvt.philadelphia.commercial_building_type` groups `building_code_description` by ordered
prefix rules (`COMMERCIAL_BUILDING_TYPES`, first match wins), taking only parking from the
code. `zoning_family` groups OPA's zoning, which OPA writes without hyphens (`CMX5`, `CA1`).
The one-pager's `commercial_breakdown` uses both and asserts that the rules place all but a
few percent of the sector's tax. It reports each group's median land share beside its bill
change, because under a total-held-fixed split rate that share is the whole mechanism:
building-heavy towers and hotels pay less, land-heavy warehouses, strip centres and gas
stations pay more. S5 prices expensive land and large lots least reliably, so those are the
groups whose magnitudes deserve the most suspicion.

**OPA owner names truncate at two widths, 25 and 40 characters** — the length histogram
spikes at both. Truncation cuts the legal suffix off (`S&S REALITY INVESTMENTS L`), so any
LLC/corporation classifier is biased downward in a known direction: entities move *out* of
the LLC bucket, never into it. Treat every LLC share as a floor.

**`parcels.gpq` row order does NOT match the CSV row order.** Do not join by index. Verified: 480K out of 579K rows differ between `taxable_land` in `parcels.gpq` and `taxable_land_value` in `philadelphia.csv`. Always join on `parcel_id` ↔ `parcel_number` (stripping leading zeros: `parcels['parcel_id'] = parcels['parcel_number'].astype(str).str.lstrip('0').astype('Int64')`).

**`parcels.gpq` geometry is Point (OPA centroids), not polygons.** Its `geometry` column holds one
point per parcel — there is no lot-outline geometry in this cache. `scripts/build_philadelphia_webmap.py --geometry polygon`
upgrades ~90% of parcels to real DOR lot outlines by joining `parcels.gpq.pin` to a *different* file:
`cities/philadelphia/data/Philadelphia_DOR_Parcels_2023.geojson` (504 MB, WGS84, integer `PIN`
field). The sibling shapefile in `cities/philadelphia/data/dor_parcels/` looks like the obvious
choice (94 MB vs. 504 MB) but **has no `PIN` field at all** — only `PARCELID` and `TENCODE`, neither
of which matches `parcels.gpq.pin` — so it cannot be used for this join.

**The two scenario CSV encoding/data quirks the webmap build works around:**
- `philadelphia_lycd.csv` and `philadelphia_lycd_post_abatement.csv` write `property_category` as
  UTF-8 re-encoded through cp1252 (`Commercial â€” Exempt` instead of `Commercial — Exempt`). The
  OPA-land CSVs (`philadelphia.csv`, `philadelphia_post_abatement.csv`) do not have this problem —
  it is specific to whatever the LYCD notebooks' CSV write path does differently. Worth fixing at
  the source; the webmap build repairs it with a cp1252→UTF-8 round-trip rather than depending on it
  being fixed upstream.
- 10 `parcel_id` values appear twice across the export (14 rows total) with genuinely different
  attributes on each occurrence — not a formatting artifact. `881000395` is one example: one row is
  fully exempt, the other is an abated parcel with `current_tax = 1140.84`. Any downstream consumer
  that dedupes on `parcel_id` needs to do so *positionally* and identically across all four exports
  (they share one row order), not independently per file, or different scenarios can end up
  describing different physical parcels under the same id.
- LYCD pre-abatement's abated-parcel improvement-value imputation bug (was copy-pasted from the
  post-abatement notebooks) is fixed — see the "`model_lycd.ipynb` needs the same abated-parcel
  imputation" note earlier in this section.

**GDAL cannot read GeoParquet in this environment on either side.** Ubuntu's `gdal-bin` (used for
the webmap's tile build in WSL) ships no Parquet/Arrow driver and no plugin package supplies one.
Separately, the Windows conda `gdal`/`pyogrio` install fails outright (`GDAL DLL could not be
found`), so no vector I/O of any format works from Windows Python. Do not assume `ogr2ogr -f Parquet`
works just because `ogr2ogr` is on PATH — check `ogr2ogr --formats` for the format you need.
