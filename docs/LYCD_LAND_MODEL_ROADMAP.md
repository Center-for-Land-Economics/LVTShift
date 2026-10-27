# From LYCD to an assessor-grade land model

Design note, 2026-09-04. What LYCD is, what it is not, which of its defects are tunable inside
the method, and what a land model an assessor could actually adopt would look like. Mechanism
only: every number this note leans on lives in a generated or dated artifact, cited inline,
and should be re-read there rather than here.

## What LYCD is

LYCD ("Least You Can Do") estimates a parcel's land value as

    land_value = zone_land_psf x lot_area
    zone_land_psf = median(market_value / lot_area over improved parcels in the zone) x k

with `k = 0.20` for improved parcels and `k = 1.00` for vacant ones, the zone taken from OPA's
own Geographic Market Area hierarchy (L3 if it holds enough improved parcels, else L2, else
L1), and improved-parcel land clipped at the parcel's own market value. All four Philadelphia
LYCD notebooks share this construction through one function,
`lvt.philadelphia.compute_lycd_land_values`, including `model_lycd_refined_prototype.ipynb`'s
land-share and zone-stratification refinements, which the function supports as two optional
parameters (see `cities/philadelphia/CLAUDE.md`).

In the appraisal literature this is the **allocation** method: land as a share of total value.
IAAO's mass-appraisal standard lists allocation last among land techniques, to be used where
vacant sales are absent. That is the honest description of LYCD's role in this repo: a floor
that breaks OPA's default-ratio collapse (most improved parcels sit at exactly 0.200 land
ratio) using OPA's own geography and OPA's own totals, so that a split-rate *distribution* can
be modeled without waiting for a reassessment. It is not a land valuation, and the audit trail
says so in four places (`analysis/audits/philadelphia_four_scenario_audit_2026-08-08.md`).

Two properties of the construction matter for everything below:

- **Scale-invariant in lot area.** Because the zone rate is itself `median(value / area)`, a
  uniform area error cancels exactly. Only *mixed* area conventions and *relative* area errors
  move results, which is why the Web Mercator area defect was invisible in the headline and
  catastrophic per parcel.
- **`k` is not a land share.** It multiplies a zone-median *total* $/sqft. The realized land
  share at `k = 0.20` differs from 0.20 and varies by parcel, so calibrating `k` to an external
  land-share estimate is a category error (audit, finding 3 and the refuted-candidates list).

## What is wrong with it, in order of consequence

| Defect | Evidence | Fixable inside the method? |
|---|---|---|
| Vacant land priced at 100% of the zone-median *improved* rate | `docs/VACANT_LAND_VALUATION.md`: LYCD over-values bare land in every cut; OPA under-values it; correcting both closes most of the LYCD/OPA land-base gap | Yes, with sales |
| Flat `k = 0.20` for every improved parcel | Audit finding 3: the OPA-vs-LYCD winner comparison reverses under the FHFA-calibrated prototype | Partly (per-cell share) |
| Linear in lot area | Sibling ratio study: PRD far above 1.0 on vacant land; `philly_open_avmkit` measures a negative partial correlation of $/sqft with log area and a positive one with frontage/depth | Yes |
| ~~KNN fallback imputes neighbours' dollar land value, not zone $/sqft times own area~~ | Audit finding 5 | **Fixed 2026-09-05** |
| Records with no lot area of their own (condominium units above all) take a neighbour's whole-lot area, then hit the cap at 100% of unit value | `model_lycd_reassessment.ipynb` Step 7, the `knn` lot-area-source row | Needs a per-unit share of the building lot, not a tuning. Painted surfaces already take it (`condo_unit_areas`); the LYCD construction does not |
| ~~Cap clips land to market value, leaving land + building above market on 24.4% of taxable parcels ($9.31B aggregate)~~ | Audit finding 2, re-measured 2026-09-05 -- materially larger than the audit's original framing (19,514 parcels at land ratio 1.00) | **Fixed 2026-09-05**, wired into `model_lycd.ipynb` |
| Hard L3 medians step at zone boundaries | Not quantified | Yes (smoothing) |

The first row dominates. It is the one defect that is *empirically decidable*: for a bare lot
the sale price is the land price, and the city records thousands of them.

Separately, read every COD on Philadelphia vacant land against its noise floor. The sibling
`property_valuation_simulations` project measures per-sale transaction noise on Philadelphia
repeat sales large enough that a perfect assessment would still show a COD in the mid-20s to
mid-30s (`docs/VACANT_LAND_VALUATION.md`, "Not all of that dispersion is the surface's
fault"). A land model that hits IAAO's 20 on a naive ratio study is not the target; one whose
COD net of noise approaches it is.

## Stage A: tuning inside the method

Each of these keeps `zone rate x area` as the published form and is days of work. Validate every
step with `scripts/vacant_land_ratio_study.py`; success is a median ratio near 1.0 and a PRD
near 1.0, with COD read net of the noise floor.

1. **Calibrate the vacant leg to sales.** Replace `k = 1.00` with a per-zone vacant $/sqft
   fitted on scrutinized, time-adjusted vacant sales, falling back up the GMA hierarchy where
   sales are thin. `philly_open_avmkit` already has the scrutinized sales, the bulk-deed
   filter, and the structure-presence flag that removes "vacant" sales carrying a building.
2. **Derive the improved-parcel share per cell instead of assuming it.** The openavmkit LYCD
   branch derives `k` as `median vacant $/sqft / median improved $/sqft` per zone-group cell.
   Feed it the *sales-based* vacant rate from step 1, not OPA's vacant assessments, or the
   share inherits OPA's under-assessment of bare land. The FHFA tract shares used by
   `cities/philadelphia/model_lycd_refined_prototype.ipynb` remain a single-family cross-check.
3. **Add a size curve and a buildability flag.** `land = rate x area^beta` with `beta < 1`, a
   frontage-to-depth term, and a sub-minimum-lot dummy from the zoning dimensional table
   transcribed in `philly_open_avmkit` (`data/philly_zoning_dimensions.csv`). The sibling
   repo's Ridge calibration measured all three effects on vacant sales. This is what moves PRD
   toward 1.0, which is the regressivity problem.
4. **Fix the KNN form (done, no downside). The cap form has a properly built and verified
   fix now, not wired into any notebook.** Imputing the zone $/sqft and multiplying by the
   parcel's own area (rather than imputing the neighbours' dollar land value directly) is
   fixed 2026-09-05.

   The cap turned out to be a harder problem than this note first suggested. Capping LAND at
   `market_value - building` (the roadmap's original "simple" recommendation, implemented as
   `compute_lycd_land_values(..., cap_include_building=True)`, off by default) reduces to
   capping land at essentially OPA's own land estimate for most parcels, since `market_value`
   IS `taxable_land + taxable_building` under OPA's own methodology. Measured on TY2026: it
   moves 142,515 parcels, drops 23.9% of single-family parcels to exactly OPA's ~0.20 default
   ratio -- the artifact LYCD exists to break -- and cuts the citywide LYCD/OPA ratio from
   1.54x to 1.32x. Left as an opt-in, not a default.

   A second design, built and verified 2026-09-05, does not have that cost: hold land at
   LYCD's full value (untouched) and let BUILDING absorb the correction instead --
   `lvt.philadelphia.compute_residual_building_value` computes `market_value - land` as a
   GROSS building estimate, fed into `carry_forward_exemptions`'s new
   `gross_building_override` parameter, which re-applies OPA's own exemption rule (homestead,
   institutional) to the (land, residual-building) pair so exemptions net exactly once. This
   is NOT the same as `model_building = market_value - land` computed carelessly against
   OPA's *taxable* (post-exemption) building column -- that naive version silently re-admits
   exempted value (measured: +$43.7B, +40%, touching 99.6% of improved taxable parcels
   before the exemption-netting was added).

   **Abated parcels must be excluded from this mechanism entirely, not routed through it with
   an override.** `carry_forward_exemptions`'s exemption-netting always subtracts a parcel's
   own historical exempt dollar total from whatever building value it is given; for an
   abated row that total IS `exempt_building`, so supplying it back as the override cancels
   it to exactly zero rather than preserving it. This was tried, looked correct in a
   synthetic unit test that (in hindsight) was tautological, and on real data silently
   zeroed $16.4B of building value the four LYCD notebooks currently, correctly, restore.
   Caught by comparing against the real cache before trusting the synthetic test; see the
   dedicated regression test `test_carry_forward_override_cannot_preserve_abated_building`.

   Measured on TY2026, with abated parcels correctly excluded: zero double-counting outside
   the vacant-land cohort (already, separately, deliberately uncapped by design) and one
   pre-existing single-parcel data anomaly (`homestead_exemption` recorded at $1.3M, ~13x the
   statutory cap); the abated cohort is bit-for-bit unaffected; and the citywide combined
   (land + building) base moves by only -$4.24B (-1.8%), a small, targeted correction rather
   than the land-cap version's large one -- because land is never touched, only building,
   and only where the correction actually binds (85.7% of non-abated taxable parcels see
   some change, but the SFR median is $0 -- OPA's own building estimate is usually already
   below the residual ceiling for a typical home; the effect concentrates in categories
   where LYCD's zone rate diverges more from OPA's implicit split, e.g. industrial building
   down a median $105,765, hotel building UP a median $200,670).

   **Wired into all three split-rate LYCD notebooks** (`model_lycd.ipynb` 2026-09-06,
   `model_lycd_post_abatement.ipynb` and `model_lycd_refined_prototype.ipynb` 2026-09-07).
   Same pattern each time: land untouched; abated parcels kept on their existing
   `exempt_building` restoration (or, in the post-abatement notebook, on whatever Step 5's
   post-abatement baseline already set, since that step's own building value is unaffected --
   it pairs with OPA's own `taxable_land`, not LYCD's, so it never had the double-count in
   the first place); ordinary parcels already in today's taxable base (`full_exmp == 0`)
   get the exemption-aware residual, deliberately not extended to let today's fully-exempt
   parcels re-enter under a larger land value, which is `model_lycd_reassessment.ipynb`'s
   question, not these notebooks'. Each notebook asserts the no-overshoot invariant and zero
   homestead-wiped re-entries on every run. All three re-executed cleanly, revenue exactly
   neutral in each, reform improvement base falling by a comparable amount in every case:

   | Notebook | Reform improvement base | Land millage | Improvement millage |
   |---|---|---|---|
   | `model_lycd.ipynb` | $126.21B -> $121.97B | 21.91 -> 22.15 | 5.48 -> 5.54 |
   | `model_lycd_post_abatement.ipynb` | $126.21B -> $121.97B | 24.23 -> 24.50 | 6.06 -> 6.12 |
   | `model_lycd_refined_prototype.ipynb` | $126.21B -> $120.75B | 21.33 -> 21.63 | 5.33 -> 5.41 |

   Category-level changes are modest and continuous in all three (no sign flips), and
   `model_lycd_reassessment.ipynb`'s drift check (which reads `model_lycd.ipynb`'s exported
   land base, untouched by any of this) still passes.

   The "market minus depreciated structure cost" extraction-method version (using a genuine
   independent structure-cost estimate rather than either OPA's building figure or a
   market-value residual) needs a cost model this repo does not have, regardless of which
   approach is chosen above.
5. **Smooth the rate surface.** Replace hard L3 medians with a kernel-weighted rate so two
   sides of one street get one land rate. Boundary cliffs are what appeals litigate.

Stratifying the zone median by residential versus non-residential context (the prototype's
second refinement) is cheap and worth keeping, but it is an input-quality step, not a valuation
step.

## Stage B: what an assessor-grade land model looks like

Three evidence legs feeding one reconciled land-price surface, then a published schedule.

1. **Vacant-land sales model.** Direct evidence. A smoothed spatial surface plus lot size,
   shape, zoning and buildability, corner, topography, and structure presence. The sibling
   repo has the sales and most features; its generic vacant AVM currently trails OPA on COD,
   so this leg needs the land-specific features rather than the residential feature set.
2. **Extraction from improved parcels.** For residential, the Kolbe et al. semi-parametric
   decomposition already runs city-wide in `philly_open_avmkit` and is calibrated on vacant
   sales (`out/models/kolbe_eval.md`). Commercial has no Kolbe estimate; use cost-approach
   extraction (improved value minus depreciated replacement cost), which is how assessors do
   it and which OPA's building attributes support.
3. **Teardown sales.** Improved parcels sold and then demolished are land sales. Join RTT
   sales to L&I demolition permits (already downloaded by the sibling repo's commercial
   enrichment). This is the only land evidence inside built-out rowhouse fabric where vacant
   sales do not exist, which is most of the city.

Reconcile the three into a single $/sqft surface, then express every parcel as
`base rate x area x listed adjustments`. That table is the assessor-grade LYCD: the same
structure the method has now, with the base rate estimated from land evidence rather than
from a fifth of total value. A schedule with adjustments is something OPA can adopt and the
Board of Revision of Taxes can review. A gradient-boosted surface is not.

What makes it usable as the basis of an LVT, beyond accuracy:

- **A land-specific ratio study**, stratified by ward, price decile, and tract income and
  race, within IAAO tolerances. `agc_assessments` (Allegheny, not Philadelphia) is the worked
  template for the stratified form; `dc-lvt-assessment-legislation` drafts the statutory ask
  that makes such a study mandatory.
- **A stated method that decomposes each parcel's value**, so an appeal can contest a specific
  adjustment rather than a black box.
- **An annual update path** using time-adjusted sales.
- **An explicit rule, not a model, for parcels with no market**: side yards, sub-minimum lots,
  assemblage remnants. These are the population most exposed under full capture, and both
  current surfaces are worst on exactly them.

## Stage B, first pass (2026-09-05): what the sales say about the form

The first sales-based surfaces are built, in `philly_open_avmkit`
(`notebooks/pipeline/land_surfaces.py`, `run_land_surfaces.py`; generated comparison in
`out/land/report.md`), and LVTShift consumes them through `lvt.philadelphia.paint_land_surface`
with `LVT_LAND_SURFACE=<candidate>` on `model_lycd_reassessment.ipynb`. What was learned about
the *form*, which is what this note is about:

- **The evidence, not the method, was the binding problem.** openavmkit's `vacant_sale` is
  current OPA status joined at download time, so lots that sold bare and were then built on
  never reach any vacant model. Recovered alongside teardowns (from a new demolition-permit
  download), those streams price land at two to three times what still-vacant lots do, and
  the two agree with each other. The whole Stage A discussion of "calibrate the vacant leg to
  sales" was calibrating to the wrong sales.
- **Interpolation beats the schedule form, measurably.** Held out by parcel, with both
  refitted inside each fold and judged by a paired bootstrap, a Gaussian-kernel kNN over the
  witnesses' log $/sqft is less dispersed than a per-cell rate schedule built from the same
  witnesses, and less dispersed than OPA; the schedule is indistinguishable from OPA. The
  Stage B sketch above assumed the published form would be the schedule. It should be the
  surface, published with each parcel's comparables (`out/land/s2_witnesses.parquet`) — a
  comps-based appraisal, which is the most familiar appeal evidence there is, and which
  § 3(d)'s "location value response surfaces" already permits.
- **What did not change.** Both fitted surfaces cascade to OPA's L2 market areas almost
  everywhere, because the finest level has a median of two witnesses per cell. A size
  adjustment is still needed for the PRD. And nothing here is IAAO-grade: the trimmed
  dispersion of the best surface sits roughly double the standard, and the net-of-noise range
  the harness reports is a lower bound that lifts every candidate, OPA included.
- **Two methodological traps, recorded so they are not re-learned:** COD is a *mean* absolute
  deviation, so a handful of large tracts transferred for token sums (no lower $/sqft bound)
  dominated every dispersion figure and made the fold spread five times wider than it is;
  and candidates must be compared with a paired test, because they all predict the same rows.

## Stage B, second pass (2026-09-12): the certified surface, and what it does to a land tax

`philly_open_avmkit` now builds a land roll and picks its surface by rule
(`out/land/land_roll_meta.json`): least held-out dispersion among candidates that pass the T1
improvement-neutrality bar and are not gated. That is **S5, paired-sales comparables** — each
parcel's ten nearest land sales adjusted for lot size and sub-minimum status by amounts measured
on matched pairs, reconciled by a weighted median, published as a comp grid
(`out/land/s5_comps.parquet`). It is statistically tied with the kNN interpolation and beats
the schedule and OPA. Gradient-boosted trees are more accurate but gated on a statutory ground
(the location factor has no sale behind it, so it cannot be contested like a comp grid). The
paper's generator asserts it is reading the certified surface, so a change of pick fails loudly.

The same choice decides whether a land-value tax is progressive by neighbourhood income and
race: flat to regressive on OPA or LYCD land (LYCD's vacant-land over-valuation lands in poorer,
non-white block groups), progressive on every sales-based surface under every abatement
treatment. Mechanism and figures: `scripts/philadelphia_equity_by_land_surface.py` and the
paper's equity section; do not restate its numbers here.

Lars's unmerged `upstream/docs_land` branch of openavmkit (May 2026) builds a much more
elaborate schedule painter — six evidence streams, per-neighbourhood tables with size curves,
and eleven LVT-specific "Lars-Tests" — but its extraction stream and its reconciliation both
lean on `assr_impr_value`, which in Philadelphia is a fifth of total value by rule.

Four of those eleven tests now run here, as `philly_open_avmkit/notebooks/pipeline/land_tests.py`
(reimplemented, not vendored: two more are circular on Philadelphia's data for the same
`assr_impr_value` reason, two need zoning tables this repo lacks, and one duplicates the
held-out study). They measure the § 3(b) properties directly — improvement-neutrality on
matched pairs, within-market-area uniformity, boundary discontinuity, and spatial
autocorrelation of the ratio residuals — and they confirm what the report had inferred from
zone medians.

**Both of the two tests that needed adapting had the same defect, and it is worth recognising
in any future borrowed test: as written they reward a *flat* surface.** Lars's L1 compares
land *values* between parcels in the same lot-size quartile, so a surface neutral in rate
fails and a size-blind one passes; his L2 uses OPA's `land_he_id` clusters, which group
parcels by characteristics rather than location (median within-cluster distance seven
kilometres), so the flattest candidate wins. Adding a distance and size match to the first and
moving the second to OPA's finest market areas reversed their ranking of the candidates. A
borrowed incentive test needs its grouping checked against the geography it will be run on
before its output means anything.

## Repo boundaries

`philly_open_avmkit` owns sales scrutiny, the AVM, the Kolbe decomposition and the ratio-study
machinery; that is where Stage B is built. LVTShift owns the choice of which land surface to
model on, the tax mechanics, and the distributional analysis. Stage A steps 4 and 5 are
LVTShift changes to `compute_lycd_land_values`; steps 1 to 3 need sales and so belong in the
sibling repo, with LVTShift consuming a per-parcel surface keyed on OPA `parcel_number`.

## What was built alongside this note

`cities/philadelphia/model_lycd_reassessment.ipynb` models the *reassessment* question that
precedes any rate change: OPA's current methodology and flat rate, with only the land component
re-valued by LYCD, rolled back to revenue neutrality. It uses the `lvt.reassessment` machinery
and the shared LYCD construction, carries every parcel's existing exemptions forward under the
rule in "The rules the shared functions encode" below, and then decomposes a stacked
reassess-then-split-rate shift into its two components. Its export is
`analysis/data/philadelphia_lycd_reassessment_ty<YEAR>.csv`.

The same notebook's Step 6b (2026-09-05) models the *other* reading of "LYCD land, rates
unchanged": the one a draft ordinance amending Phila. Code § 2-305 actually describes, where the
land component is re-split *inside* OPA's unchanged total (building = total − land) and the
statutory rate is untouched, so only exemptions that depend on the split -- abatements -- can
move a bill. `lvt.philadelphia.reallocate_land_within_total` is that reform; it shares the
exemption decomposition with `carry_forward_exemptions` and differs only in what it holds fixed.

## The rules the shared functions encode

The notebooks that call these live in `philly_land_tax_research`; the rules live here, beside
the code in `lvt/philadelphia.py`.

- **`carry_forward_exemptions`** is "same methodology, land valued differently". The Homestead
  Exemption is re-applied as `min(cap, new total)`, building first, so a homestead whose new land
  lifts it above the cap becomes taxable again (the "re-entering" cohort). Abatement and partial
  relief carry forward in dollars, since a land reform leaves the building value unchanged; a
  full institutional exemption is a rate, not an amount, and stays. The homestead keys off OPA's
  per-parcel `homestead_exemption` dollars AND an exemption actually applied this vintage,
  because the flag is current-vintage. Its guard is a reconstruction: OPA's own gross land fed
  through the same rule must reproduce OPA's taxable total per parcel (floor 95%; the observed
  rate is printed).
- **`reallocate_land_within_total`** holds OPA's total fixed instead of OPA's building:
  `alloc_land = min(new_land, total)`, `alloc_building = total − alloc_land`, rate untouched. It
  shares `_decompose_exemptions` / `_apply_exemptions` / `_reconstruction_guard` with
  `carry_forward_exemptions`, so the two cannot disagree about what a parcel's exemptions are.
  With total and rate fixed, only split-dependent relief (`exemption_kind == 'building_share'`)
  can move a bill; homestead, total-value relief and vacant parcels are bit-for-bit unchanged.
  Two classification rules: relief that fits inside the building line but is split pro-rata
  across land and building is a share of total value, not an abatement, so classify on
  `exempt_land` net of homestead spill rather than on fit alone; and read the effect off
  `.reform_change` (rule on new land minus rule on OPA's land), never against OPA's recorded
  taxable total, or the reconstruction residual shows up as a tax change the reform cannot cause.
- **`homestead_order`** (on both functions: `building_first` default, `land_first`,
  `value_share`, `tax_share` + `homestead_rate_ratio`) decides which taxable line the Homestead
  Exemption comes off. At one rate it is worth `cap × rate` whatever the line; under a split rate
  the statutory building-first order (53 Pa.C.S. § 8583(c), which OPA's records follow) makes it
  worth `cap × building rate`, which is why a shift that cuts rentals' bills can raise most
  owner-occupants'. Taxable totals, `reform_change` and the guard are identical under every
  order, so the order is invisible until two rates are applied. Every order but the default needs
  § 8583(c) amended; `value_share` is the neutral one (the exclusion cuts a bill by
  `cap / value` at any pair of rates).
- **`paint_land_surface`** imports an external land **rate**, never a dollar value, and
  multiplies it by this repo's own `dor_area_sqft`, which keeps the two repos' area conventions
  decoupled. Parcels outside the AVM universe take their nearest matched neighbours' median rate
  and are flagged `land_surface_source == 'knn'`. Land is capped at the total only on a parcel
  with a building on OPA's own split (`opa_gross_building` above $1), the test the re-split's
  `uncap_bare_land` uses; a bare lot is taken whole whatever its category code (OPA files lots
  built on after the roll, parking and parkland under improved codes with a $0 building line).
  `building_col=None` restores LYCD's category-code cap. Given `support=` (the sibling repo's `land_surface_support.json`),
  it does three more things. The KNN fill re-sizes each neighbour's rate to the subject's own lot
  (`_knn_resized_rate_fill`) and draws only on in-support neighbours, because a neighbour's rate
  describes a lot of the neighbour's size. Every parcel whose own or painted lot exceeds the
  support edge is flagged `land_beyond_support`. And a flagged bare parcel is carried at
  `opa_gross_land` (`opa_beyond_support`); parcels with a building are left alone because the
  market-value cap already bounds them. `uncap_bare_land` applies the same rule, by the same
  definition of bare, through `beyond_support`.
- **`relevel_beyond_support`** is the reform step's reading of those flagged parcels, built and
  bare alike: the surface's own uncapped value divided by its held-out median ratio to price in
  the lot's size band (`large_lot_band_factor`: edge to 1 acre, 1 acre and over), with the
  re-split's cap applied afterwards, never before. `uncap_bare_land(large_tracts="relevelled")`
  then takes that column whole on a bare lot, except the tracts `keep_opa` marks as
  use-restricted, which keep OPA's value. The other `LARGE_TRACT_TREATMENTS` (carry OPA, OPA
  relevelled, the surface whole) vary the bare side only and are the sensitivities.
- **`condo_unit_areas`** fixes the condo defect in the table above on the painted path: each
  unit's area becomes the sibling repo's `condo_unit_share` (livable-area share, summing to 1
  per building; 0 for accessory units) times this repo's PIN area for the master lot. It imports
  a share, not an area, for the same reason `paint_land_surface` imports a rate. Guard: a
  building's unit areas may not sum past its lot.
- **`corrected_lot_areas`** imports the sibling repo's `land_area_sqft` where its land roll
  replaced OPA's recorded area (`land_area_source == 'pwd_dor'`: OPA's area more than 2x both
  the DOR and PWD polygons), for parcels whose `area_source` is still `opa_total_area`. It
  imports an area, unlike the two above, because this repo has no PWD polygons to re-derive it;
  both repos measure true ground square feet. Guard: no flagged parcel may leave with an area
  more than 2x the sibling repo's.
- **`compute_lycd_land_values`'s prototype parameters** default to base behaviour:
  `zone_group_col` (zone medians per (group, GMA level) cell) and `land_pct_improved` as a
  per-parcel Series (an FHFA tract land share). The Philadelphia-specific inputs for both stay in
  the calling notebook.

## Against the draft ordinance

The PPI report on the ordinance is in `philly_land_tax_research`, at
`analysis/land_assessment/philadelphia/paper/`. It leads with the sales-based S5 surface; LYCD is
its Appendix A, the simplest alternative, where it puts the two readings side by side and runs
the ordinance's own § 3(b)/§ 4 tests on OPA's surface, the raw LYCD surface, and the re-split
surface the ordinance would certify. The mechanism worth carrying
here, not the numbers (the generator owns those): **neither surface satisfies § 3(b)'s
uniformity sentence, and they fail different halves of it.** OPA's land is a deterministic
function of the building (land = 0.20 × total ⇒ land = 0.25 × building, so within-zone rank
correlation is exactly 1), which is also why abated new construction gets land far above its
neighbours'. Raw LYCD fails on *presence*: its `k = 1.00` vacant leg over `k = 0.20` improved is
literally variation on the absence of improvements. The re-split escapes that only because a
vacant parcel's land is capped at its own total -- i.e. it inherits OPA's vacant value, and with
it OPA's vacant-land accuracy against sales. The report also drafts § 3(e) language that
prohibits the parcel-level fixed ratio while permitting the market-area form, with the interim
certification moved to the *rate source*.

## Open items

- Lars Doucet's write-up of the method (`openavmkit/valuing land - the simplest viable
  method.pdf`) is image-only and was not machine-readable when this note was written; check
  whether it already specifies any of the Stage A refinements before implementing them.
- No published artifact yet carries the land-share band the audit asked for (finding 6).

## Resolved

- **The second analysis: a sales-based land surface** (requested 2026-09-05 as a base rate in
  LYCD's schedule form; built 2026-09-05 to 2026-09-12). Built in `philly_open_avmkit` and
  consumed here through `paint_land_surface`; see "Stage B, first pass" and "Stage B, second
  pass" above. The schedule form did not survive the held-out sales: the certified surface is
  S5, paired-sales comparables, published with its comparables rather than as a rate table.
  `model_lycd_reassessment.ipynb` runs it with `LVT_LAND_SURFACE=s5`, and the ordinance report
  in `philly_land_tax_research` (`analysis/land_assessment/philadelphia/paper/`) now leads with
  it, with the ordinance tests carrying sales-based columns beside OPA's and LYCD's.
- **Land + building can exceed market_value; exemption-aware residual building fixes it**
  (built and verified 2026-09-05; wired into all three split-rate LYCD notebooks
  2026-09-06/07). See Stage A
  item 4 above for the full account, including the abated-parcel trap
  (`lvt.philadelphia.compute_residual_building_value`,
  `carry_forward_exemptions`'s `gross_building_override`). Measured effect: zero
  double-counting outside the vacant-land cohort and one data anomaly; abated cohort
  bit-for-bit unchanged; citywide combined base -$4.24B (-1.8%); land untouched.
- **KNN fallback imputes rate, not dollar value** (2026-09-05). Parcels outside GMA coverage
  now take the median $/sqft of their `knn_k` nearest neighbours and apply it to their own
  area and land_pct, instead of inheriting a neighbour's own dollar land value regardless of
  size (audit finding 5). No known downside; the citywide land base moved up modestly
  ($62.54B to $66.19B over taxable parcels, ratio 1.44x to 1.54x of OPA) because the
  KNN-imputed cohort's own areas differ systematically from their neighbours'.
- **`model_lycd.ipynb`'s abated-parcel building imputation** (2026-09-04). The notebook had
  regressed to `model_building = 4 x model_land` for abated parcels, while `model.ipynb`,
  `model_post_abatement.ipynb` and `model_lycd_post_abatement.ipynb` all restore the observed
  `exempt_building` value. Fixed to match; re-executed. Reform improvement base moved
  $118.53B -> $126.21B (abated bldg base $8.53B -> $16.21B), land millage 23.23 -> 22.76 mills,
  improvement millage 5.81 -> 5.69 mills. The LYCD land base itself is untouched
  ($82.37B all parcels, $62.54B over taxable parcels) since the fix only touches building
  imputation. See `cities/philadelphia/CLAUDE.md` for the full account of why the earlier
  revert's rationale was wrong.
