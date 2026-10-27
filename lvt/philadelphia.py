"""Philadelphia tax-year constants, in one place.

Every Philadelphia notebook needs the same three things for a given tax year: the millage,
the city-only rate for the revenue cross-check, and the collections figure to check against.
Copying those into six notebooks is how the stale `city_only_rate = 6.317` survived past the
TY2025 rate reallocation — the combined rate stayed 1.3998% so nothing looked wrong, but the
City/School split had moved and the cross-check was quietly validating against the wrong
denominator. Keep them here and import.

Scripts, notebooks and analysis folders cited below that are not in LVTShift are in the sibling
Philadelphia research repo, `philly_land_tax_research`, which imports this module.

Rate and revenue sources are cited per year. The combined rate has been 1.3998% throughout;
what changes is how it splits, which only affects the cross-check, not the revenue-neutral
model itself (that runs on the combined levy).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

__all__ = ["TaxYear", "tax_year_params", "parcel_cache_path", "SUPPORTED_TAX_YEARS",
           "ZeroBuildingSplit", "split_zero_building_parcels",
           "AbatementCohortExpansion", "expand_abatement_cohort", "ABATEMENT_SCHEDULES",
           "LycdResult", "compute_lycd_land_values",
           "ExemptionCarryForward", "carry_forward_exemptions",
           "compute_residual_building_value",
           "LandReallocation", "reallocate_land_within_total", "uncap_bare_land", "LARGE_TRACT_TREATMENTS",
           "LargeTractRelevel", "relevel_beyond_support", "large_lot_band_factor", "LARGE_LOT_BANDS", "ACRE_SQFT",
           "HOMESTEAD_ORDERS",
           "LandSurfaceResult", "paint_land_surface",
           "PHILADELPHIA_LAND_SQFT", "VACANT_CATEGORY_CODES",
           "SURFACE_PARKING_CODES", "SURFACE_PARKING_WITH_STRUCTURE_RE", "PARKING_GARAGE_RE",
           "COMMERCIAL_BUILDING_TYPES", "COMMERCIAL_OTHER", "commercial_building_type", "zoning_family"]


@dataclass(frozen=True)
class TaxYear:
    year: int
    city_rate_pct: float
    school_rate_pct: float
    city_revenue_target: int | None
    target_kind: str      # 'actual' | 'projection' | 'none'
    source: str
    homestead_exemption: int   # statutory Homestead Exemption for this tax year

    @property
    def combined_rate_pct(self) -> float:
        return round(self.city_rate_pct + self.school_rate_pct, 6)

    @property
    def combined_mills(self) -> float:
        """Millage per $1,000 of assessed value (rate% x 10)."""
        return round(self.combined_rate_pct * 10, 6)

    @property
    def city_mills(self) -> float:
        return round(self.city_rate_pct * 10, 6)

    def describe(self) -> str:
        tgt = (f"${self.city_revenue_target:,} ({self.target_kind})"
               if self.city_revenue_target else "no target (forward-looking year)")
        return (f"TY{self.year}: {self.city_rate_pct}% city + {self.school_rate_pct}% school "
                f"= {self.combined_rate_pct}% ({self.combined_mills} mills) | city target {tgt} "
                f"| homestead ${self.homestead_exemption:,}")


_PARAMS: dict[int, TaxYear] = {
    2024: TaxYear(
        year=2024,
        city_rate_pct=0.6317,
        school_rate_pct=0.7681,
        city_revenue_target=795_796_126,
        target_kind="actual",
        homestead_exemption=80_000,
        source="FY2024 city-only current-year Real Estate Tax collections.",
    ),
    2026: TaxYear(
        year=2026,
        city_rate_pct=0.6159,
        school_rate_pct=0.7839,
        city_revenue_target=891_102_000,
        target_kind="projection",
        source=(
            "Rate: City of Philadelphia Quarterly City Managers Report, period ending "
            "2026-03-31, Summary Table R-1 ('FY 2026 Tax Rate: .6159% City plus .7839% "
            "School District Total 1.3998%'). Target: same report, Table R-2, Real Property "
            "'Current' full-year Current Projection ($891,102k). This is a Q3 projection, "
            "not a closed-out actual."
        ),
        homestead_exemption=100_000,
    ),
    2027: TaxYear(
        year=2027,
        city_rate_pct=0.6159,
        school_rate_pct=0.7839,
        city_revenue_target=None,
        target_kind="none",
        source=(
            "The adopted FY2027 budget holds the combined rate at 1.3998% with no property "
            "tax increase. The City/School split is CARRIED FORWARD from FY2026 and is NOT "
            "independently confirmed — the FY2027-2031 Five-Year Plan shifts more Real Estate "
            "Tax revenue to the School District, so the split may move again. No revenue "
            "target: TY2027 bills are not due until March 2027, so there are no collections "
            "to validate against. Treat TY2027 output as a forward-looking scenario."
        ),
        homestead_exemption=100_000,
    ),
}

SUPPORTED_TAX_YEARS = tuple(sorted(_PARAMS))


def tax_year_params(year: int) -> TaxYear:
    """Return the rate/target constants for a Philadelphia tax year."""
    try:
        return _PARAMS[year]
    except KeyError:
        raise KeyError(
            f"No Philadelphia tax-year parameters for {year}. "
            f"Known years: {list(SUPPORTED_TAX_YEARS)}. Add an entry to lvt/philadelphia.py "
            "with a cited rate and revenue source rather than assuming the previous year's."
        ) from None


@dataclass(frozen=True)
class ZeroBuildingSplit:
    """How the zeroed-building-line cohort divides. See `split_zero_building_parcels`."""
    category: "pd.Series"
    abated: "pd.Series"
    homestead_zeroed: "pd.Series"
    no_exemption: "pd.Series"
    homestead_claim_rate: float
    total_value_relief: "pd.Series | None" = None   # relief against total value: not abated

    def describe(self) -> str:
        tvr = 0 if self.total_value_relief is None else int(self.total_value_relief.sum())
        return (f"zero-building line: {int(self.abated.sum()):,} abated | "
                f"{int(self.homestead_zeroed.sum()):,} homestead-zeroed "
                f"({self.homestead_claim_rate:.1%} confirmed by OPA's homestead flag) | "
                f"{tvr:,} total-value relief (not abated) | "
                f"{int(self.no_exemption.sum()):,} genuinely $0 improvement")


def split_zero_building_parcels(
    gdf,
    category,
    homestead_exemption: float,
    category_map: dict,
    *,
    genuine_vacant_codes=("6", "12", "13"),
    homestead_claim_floor: float = 0.85,
) -> ZeroBuildingSplit:
    """Split parcels whose taxable building line is $0 into their three real populations.

    `taxable_building <= 0` on a non-vacant parcel does not mean "abated". It means the
    building line nets to zero, which happens three different ways, and treating all of
    them as abated construction put ~13k modest homesteaded rowhomes in the abated bucket
    and then revoked their Homestead Exemption under the reform while every other
    homesteader in the city kept theirs.

    The discriminator is the exempt total. The Homestead Exemption is a flat statutory
    amount, so it can never explain more than `homestead_exemption` of exempted value:

    - `exempt_total >  homestead_exemption` -> an abatement is present.
    - `0 < exempt_total <= homestead_exemption` -> ordinary homesteaded property whose
      building assessment is smaller than the exemption. Restored to its OPA category;
      it keeps its exemption under the reform, so its $0 building line is correct as-is.
    - `exempt_total == 0` -> nothing is exempted and the improvement really is worth $0.
      Left as Override 1 classified it.

    Within the first group, relief that reaches the LAND beyond what the homestead spills there
    is relief against total value, not a construction abatement: an abatement exempts the
    improvement only, so the one land exemption an abated parcel can show is homestead spill
    (the same test `reallocate_land_within_total` uses to tell `total_share` from
    `building_share`). On TY2026 this is ~2,600 mostly owner-occupied homes whose relief has run
    since 2015 or earlier and steps up at every reassessment, like an assessment-increase cap. Its
    program is unidentified (it is not LOOP, which Revenue applies at billing and OPA does not
    record; see `cities/philadelphia/CLAUDE.md`), but no 10-year abatement runs that long, so it
    continues after today's abatements expire. Calling these parcels abated strips that relief
    from the post-abatement baseline. They are returned as `total_value_relief`, restored to
    their OPA category, and keep their exemption.

    Pass `homestead_exemption` from `tax_year_params(year)`, not a literal: the amount has
    moved four times ($30K -> $40K -> $45K -> $80K -> $100K) and using the wrong year's cap
    silently moves parcels across the abated/homestead boundary.

    Parameters
    ----------
    gdf : GeoDataFrame or DataFrame
        Needs `taxable_land`, `taxable_building`, `exempt_land`, `exempt_building` and
        `category_code`. `homestead_exemption` (per-parcel, from OPA) is optional but
        enables the guard.
    category : pd.Series
        The PROPERTY_CATEGORY series as of Override 1.
    homestead_exemption : float
        The tax year's statutory Homestead Exemption.
    category_map : dict
        OPA `category_code` -> category name, used to restore homestead-zeroed parcels.
    homestead_claim_floor : float
        Minimum share of homestead-zeroed parcels that must actually carry a homestead on
        OPA's own flag. Below this the split is not describing what it claims to.

    Returns
    -------
    ZeroBuildingSplit
    """
    import pandas as pd

    def _num(col):
        return pd.to_numeric(gdf[col], errors="coerce").fillna(0.0)

    codes = gdf["category_code"].astype(str)
    exempt_total = _num("exempt_land") + _num("exempt_building")
    zero_building = (
        (_num("taxable_building") <= 0)
        & (_num("taxable_land") > 0)
        & (~codes.isin(set(genuine_vacant_codes)))
    )

    # Non-homestead relief that reached the land line: relief against total value.
    hs_amt = (_num("homestead_exemption").clip(lower=0) if "homestead_exemption" in gdf.columns
              else pd.Series(0.0, index=gdf.index))
    hs_active = (hs_amt > 0) & (exempt_total > 0)
    other = (exempt_total - hs_amt.where(hs_active, 0.0)).clip(lower=0)
    gross_bldg = _num("taxable_building") + _num("exempt_building")
    hs_spill = (hs_amt - (gross_bldg - other).clip(lower=0)).clip(lower=0).where(hs_active, 0.0)
    total_value_relief = (zero_building & (exempt_total > homestead_exemption)
                          & ((_num("exempt_land") - hs_spill).clip(lower=0) > 1.0))

    abated = zero_building & (exempt_total > homestead_exemption) & ~total_value_relief
    homestead_zeroed = zero_building & (exempt_total > 0) & (exempt_total <= homestead_exemption)
    no_exemption = zero_building & (exempt_total <= 0)

    out = category.copy()
    out[abated] = "Abated / Construction Exemption"
    out[homestead_zeroed] = codes[homestead_zeroed].map(category_map).fillna("Other")
    out[total_value_relief] = codes[total_value_relief].map(category_map).fillna("Other")

    # The split is exemption arithmetic, so check it against OPA's own homestead flag --
    # the column that would move if the split were separating the wrong two populations.
    claim_rate = float("nan")
    if "homestead_exemption" in gdf.columns and homestead_zeroed.any():
        hs = pd.to_numeric(gdf["homestead_exemption"], errors="coerce").fillna(0.0)
        claim_rate = float((hs[homestead_zeroed] > 0).mean())
        if claim_rate < homestead_claim_floor:
            raise ValueError(
                f"Only {claim_rate:.1%} of parcels classified as homestead-zeroed actually "
                f"carry a Homestead Exemption on OPA's flag (floor {homestead_claim_floor:.0%}). "
                f"The ${homestead_exemption:,.0f} cap is probably wrong for this tax year -- "
                "check tax_year_params(year).homestead_exemption against the assessment vintage."
            )

    return ZeroBuildingSplit(out, abated, homestead_zeroed, no_exemption, claim_rate, total_value_relief)


ABATEMENT_SCHEDULES = (
    "old_flat_100", "old_flat_partial", "new_graduated_residential",
    "new_flat_90_commercial", "unresolved",
)


@dataclass(frozen=True)
class AbatementCohortExpansion:
    """How the zero-building abated cohort grows once the full schedule classification is read.
    See `expand_abatement_cohort`."""
    category: "pd.Series"            # PROPERTY_CATEGORY, with `missed` parcels reclassified
    abated: "pd.Series"              # zero-building abated OR classified abatement, unioned
    missed: "pd.Series"              # classified abatement the zero-building test alone missed
    classified: "pd.Series"          # every parcel scripts/build_philadelphia_abatement_classification.py calls an abatement
    restored_building: "pd.Series"   # gross (pre-exemption) building value for every row
    released: "pd.Series | None" = None   # zero-building parcels the classification calls relief, not abatement

    def describe(self) -> str:
        return (
            f"abatement cohort: {int(self.abated.sum()):,} total "
            f"({int(self.missed.sum()):,} added by the schedule classification, of "
            f"{int(self.classified.sum()):,} the script classifies as a construction abatement) "
            f"| restored building value: ${self.restored_building[self.abated].sum()/1e9:.2f}B"
        )


def expand_abatement_cohort(
    gdf,
    category: "pd.Series",
    abated_mask: "pd.Series",
    year: int,
    *,
    data_dir: str | Path = "data",
    category_map: "dict | None" = None,
) -> AbatementCohortExpansion:
    """Add the abatement schedules `split_zero_building_parcels` cannot see to the abated cohort.

    `split_zero_building_parcels` only catches a parcel whose taxable building line nets to $0
    -- a full (100%-exempt) construction abatement. Three real schedules leave a taxable
    building line and so are invisible to that test: a graduated residential abatement after
    year 1 (10-point annual step-down), the 90% commercial/industrial schedule, and a
    rehabilitation abatement (which exempts only the value the work added). Calling all of
    these "not abated" understates the post-abatement baseline and leaves their exemption
    silently carried forward by anything that reads PROPERTY_CATEGORY to decide what counts as
    an abatement. See `cities/philadelphia/CLAUDE.md`, "The zero-building test finds only full
    abatements".

    `scripts/build_philadelphia_abatement_classification.py` classifies the full population by
    reading each parcel's billed exemption history rather than inferring it from a permit; its
    output (`schedule_type`) is read here, unioned onto the zero-building cohort, and any
    parcel it calls an abatement is reclassified into `'Abated / Construction Exemption'` if it
    is not there already.

    **Restored building value.** For every row (not only `abated` ones -- the caller selects),
    the gross (pre-exemption) building value is `taxable_building + exempt_building`. For a
    fully-abated, zero-building-line parcel this reduces to `exempt_building` alone (unchanged
    from the prior behavior). For a schedule this function newly catches -- which still carries
    a nonzero `taxable_building` -- using `exempt_building` alone would drop that taxable
    portion; the sum of the two is the parcel's actual assessed improvement, restored whole.
    Falls back to `market_value - taxable_land` only where both `taxable_building` and
    `exempt_building` are $0 (mid-construction parcels with no assessed improvement yet).

    Parameters
    ----------
    gdf : DataFrame
        Needs `parcel_number`, `taxable_building`, `exempt_building`, `market_value`,
        `taxable_land`.
    category : pd.Series
        The PROPERTY_CATEGORY series as of the caller's most recent override.
    abated_mask : pd.Series
        The zero-building abated mask from `split_zero_building_parcels`.
    year : int
        Tax year -- selects `abatement_classification_ty<year>.parquet`.
    data_dir : str or Path
        Directory holding the classification parquet (default `'data'`, i.e.
        `cities/philadelphia/data` when run from that directory).
    category_map : dict, optional
        OPA `category_code` -> category name. When given, a zero-building parcel the
        classification labels `non_abatement_relief` (relief present since the start of the
        history, or rising with assessments -- never a 10-year abatement) is released from the
        abated cohort and restored to its OPA category, so it keeps its relief. Without it the
        cohort is returned as before.

    Returns
    -------
    AbatementCohortExpansion
    """
    import pandas as pd

    cls_path = Path(data_dir) / f"abatement_classification_ty{year}.parquet"
    if not cls_path.exists():
        raise FileNotFoundError(
            f"{cls_path} not found. Build it with:\n"
            f"    python scripts/build_philadelphia_abatement_classification.py --year {year}\n"
            "(run from the sibling philly_land_tax_research repo, where that script lives). "
            "This is the full abatement-schedule classification (graduated residential, 90% "
            "commercial, rehab) that the zero-building test alone cannot see -- see "
            "cities/philadelphia/CLAUDE.md, \"The zero-building test finds only full abatements\"."
        )
    cls = pd.read_parquet(cls_path, columns=["parcel_number", "schedule_type"])
    cls["parcel_number"] = cls["parcel_number"].astype(str).str.zfill(9)
    schedule_map = cls.drop_duplicates("parcel_number").set_index("parcel_number")["schedule_type"]

    parcel_number = gdf["parcel_number"].astype(str).str.zfill(9)
    schedule_type = parcel_number.map(schedule_map)
    classified = schedule_type.isin(ABATEMENT_SCHEDULES)
    missed = classified & ~abated_mask

    released = pd.Series(False, index=abated_mask.index)
    if category_map is not None:
        released = abated_mask & schedule_type.eq("non_abatement_relief").to_numpy()
    out_category = category.copy()
    out_category[missed] = "Abated / Construction Exemption"
    codes = gdf["category_code"].astype(str)
    out_category[released] = codes[released].map(category_map or {}).fillna("Other")
    out_abated = (abated_mask & ~released) | missed

    # Guard: this must read the SAME quantity that broke -- whether a classified-abatement
    # parcel actually ends up in the abated category -- not a reconstruction of it.
    escaped = classified & (out_category != "Abated / Construction Exemption")
    if escaped.any():
        raise ValueError(
            f"{int(escaped.sum()):,} classified-abatement parcels are not in the "
            "'Abated / Construction Exemption' category after reclassification -- the merge "
            "or the reclassification mask is wrong."
        )

    def _num(col):
        return pd.to_numeric(gdf[col], errors="coerce").fillna(0.0)

    gross_bldg = _num("taxable_building") + _num("exempt_building")
    implied_bldg = (_num("market_value") - _num("taxable_land")).clip(lower=0)
    restored = gross_bldg.where(gross_bldg > 0, implied_bldg)

    return AbatementCohortExpansion(out_category, out_abated, missed, classified, restored, released)


def parcel_cache_path(year: int, data_dir: str | Path = "data") -> Path:
    """Path to the year-specific shared parcel cache.

    The cache is keyed by tax year on purpose: `opa_properties_public` always carries the
    latest assessment year, so an unsuffixed cache makes it easy to model one year's
    taxable values against another year's expectations without any visible symptom.
    """
    return Path(data_dir) / f"parcels_ty{year}.gpq"


# Philadelphia land area, 134.2 sq mi in sqft. Parcels exclude streets, rights-of-way and
# water, so a correct lot-area layer sums to *less* than this.
PHILADELPHIA_LAND_SQFT = 134.2 * 27_878_400

VACANT_CATEGORY_CODES = ("6", "12", "13")


def _parcel_coords(gdf) -> "np.ndarray":
    """Parcel centroids in metres (EPSG:3857), the frame every KNN step in this module uses."""
    import numpy as np

    geom = gdf.geometry.to_crs("EPSG:3857")
    if (geom.geom_type != "Point").any():
        geom = geom.centroid
    return np.column_stack([geom.x.values, geom.y.values])


def _knn_median_fill(coords: "np.ndarray", values: "np.ndarray", k: int) -> "np.ndarray":
    """Fill NaN entries of `values` with the median over the `k` nearest non-NaN parcels.

    Used for a lot AREA and for a $/sqft RATE, never for a dollar land value: a parcel's own
    size must still determine its own land value when its rate comes from a spatial smooth
    (audit 2026-08-08 finding 5).
    """
    import numpy as np
    from scipy.spatial import cKDTree

    values = np.asarray(values, dtype=float)
    have = ~np.isnan(values)
    need = np.isnan(values)
    if need.sum() == 0 or have.sum() == 0:
        return values
    tree = cKDTree(coords[have])
    _, ix = tree.query(coords[need], k=min(k, int(have.sum())), workers=-1)
    if ix.ndim == 1:
        ix = ix[:, None]
    filled = values.copy()
    filled[need] = np.median(values[have][ix], axis=1)
    return filled


def _knn_resized_rate_fill(coords, rates, painted_area, own_area, donor_ok, elasticity: float,
                           k: int) -> "np.ndarray":
    """Fill NaN `rates` from the `k` nearest donors, each donor's rate first re-sized from the
    lot it was painted at to the SUBJECT's own lot: `r_j * (A_i / a_j) ** elasticity`.

    A size-sensitive surface paints every parcel at its own lot size, so a neighbour's rate is
    a statement about a lot the neighbour's size. Handing it unchanged to a subject two
    hundred times larger prices a rail yard at a rowhouse lot's rate (audit 2026-09-21,
    finding 3). The surface's adjustment is multiplicative in area, so re-sizing a donor's rate
    is exact for the comparable grid that produced it. Donors are restricted to `donor_ok`
    (parcels the surface's own evidence supports); a subject with no usable area of its own
    takes the donors' median unre-sized, as `_knn_median_fill` would.
    """
    import numpy as np
    from scipy.spatial import cKDTree

    rates = np.asarray(rates, dtype=float)
    a_don = np.asarray(painted_area, dtype=float)
    a_own = np.asarray(own_area, dtype=float)
    need = np.isnan(rates)
    have = ~need & np.asarray(donor_ok, dtype=bool) & (a_don > 0)
    if need.sum() == 0 or have.sum() == 0:
        return rates
    tree = cKDTree(coords[have])
    _, ix = tree.query(coords[need], k=min(k, int(have.sum())), workers=-1)
    if ix.ndim == 1:
        ix = ix[:, None]
    own = a_own[need]
    own = np.where(np.isfinite(own) & (own > 0), own, np.nan)[:, None]
    scale = np.where(np.isnan(own), 1.0, (own / a_don[have][ix]) ** elasticity)
    filled = rates.copy()
    filled[need] = np.median(rates[have][ix] * scale, axis=1)
    return filled


@dataclass(frozen=True)
class LycdResult:
    """Output of `compute_lycd_land_values`: the frame with LYCD columns, plus diagnostics."""
    gdf: "pd.DataFrame"
    diagnostics: dict

    def describe(self) -> str:
        d = self.diagnostics
        lines = [
            "Lot area source: " + ", ".join(f"{k}={v:,}" for k, v in d["area_source_counts"].items()),
            f"  OPA records overridden by surveyed polygon: {d['n_pin_override']:,}",
            f"  total lot area = {d['city_area_ratio']:.2f}x the city (expect <1.0)",
            f"GMA assignment: {d['n_gma_matched']:,} matched ({d['gma_match_rate']:.1%}); "
            + ", ".join(f"{k}={v:,}" for k, v in d["gma_level_counts"].items()),
            f"Market-value cap (improved only): {d['n_capped']:,} parcels, "
            f"${d['land_base_pre_cap']/1e9:.2f}B -> ${d['land_base_post_cap']/1e9:.2f}B "
            f"({d['cap_removed_share']:.1%} removed)",
        ]
        return "\n".join(lines)


def compute_lycd_land_values(
    gdf,
    gma,
    pin_areas,
    *,
    land_pct_improved=0.20,
    land_pct_vacant: float = 1.00,
    min_improved: int = 50,
    opa_pin_disagree_factor: float = 3.0,
    vacant_codes=VACANT_CATEGORY_CODES,
    cap_improved_at_market: bool = True,
    cap_include_building: bool = False,
    knn_k: int = 5,
    city_area_sqft: float = PHILADELPHIA_LAND_SQFT,
    max_city_area_ratio: float = 1.5,
    zone_group_col: "str | None" = None,
) -> LycdResult:
    """GMA-hierarchical LYCD land values, the construction shared by every Philadelphia notebook.

    "Least You Can Do": a parcel's land value is a uniform local land rate times its lot area,
    where the rate is the median total value per square foot of *improved* parcels in the
    parcel's OPA Geographic Market Area, times an allocation share::

        zone_psf        = median(market_value / lot_area) over improved parcels in the zone
        lycd_land_value = zone_psf x share x lot_area
        share           = land_pct_improved for improved parcels, land_pct_vacant for vacant ones

    The zone is the finest GMA level with at least `min_improved` improved parcels (L3, else
    L2, else L1). Parcels outside GMA coverage take the median *rate* ($/sqft) of their
    `knn_k` nearest GMA-assigned neighbours, applied to their OWN area and land_pct -- a
    parcel's own size still determines its land value even when its zone rate comes from a
    spatial smooth (fixed 2026-09; previously imputed the neighbours' dollar land value
    directly, so a small unmatched parcel next to large ones inherited a land value sized for
    their lots -- audit 2026-08-08 finding 5). Improved parcels are then clipped at
    `market_value` (or, with `cap_include_building=True`, at `market_value - taxable_building`
    -- see that parameter's docstring for why this is off by default). Vacant parcels are not
    capped, because OPA under-assesses bare land and the clip would erase the
    development-potential signal.

    Two refinements, off by default, layer on the base construction without changing it for
    callers that do not use them (`model_lycd_refined_prototype.ipynb` uses both):

    - `zone_group_col`: a column on `gdf` with a per-parcel group label (e.g. "Residential"
      vs "NonResidential"). When given, zone medians are computed per (group, GMA level) cell
      instead of pooling every improved parcel type together, so a rowhouse is priced against
      other residential comps, not whatever commercial parcel happens to share its zone. When
      `None` (the default), every row shares one implicit group and results are identical to
      the ungrouped construction -- verified bit-for-bit against `model_lycd.ipynb`'s prior
      inline version.
    - `land_pct_improved` as a `pandas.Series` (aligned to `gdf.index`) instead of a scalar:
      a per-parcel allocation share, e.g. an external land-share estimate for some categories
      and the flat default for others. The value at vacant rows is ignored -- `land_pct_vacant`
      always wins there, so callers need not special-case vacant parcels when building the
      Series.

    Lot area uses ONE convention, true ground square feet, with no spatial join: OPA
    `total_area` first, then the Mercator-corrected PIN-keyed DOR polygon area, then KNN from
    parcels already on that convention. A surveyed polygon overrides OPA's record when OPA is
    more than `opa_pin_disagree_factor` times larger (a handful of impossible OPA records). The
    method is exactly scale-invariant in lot area, so only *mixed* conventions and *relative*
    errors move results; the total-area guard catches the mixed-convention failure that once
    tripled the city's land area.

    Parameters
    ----------
    gdf : GeoDataFrame
        The year-keyed parcel cache. Needs `parcel_number` (zero-padded string), `pin`,
        `category_code`, `total_area`, `market_value`, `taxable_building` (OPA's raw,
        pre-abatement-restoration figure -- used only for the market-value cap) and point
        geometry.
    gma : DataFrame
        `parcel_gma_assignment.parquet`: `key` (parcel_number) + `gma1`, `gma2`, `gma3`.
    pin_areas : DataFrame
        `parcel_areas_by_pin_current.parquet`: `pin` + `pin_area_sqft`, Mercator-corrected.
        Do NOT pass the older `parcel_areas_by_pin.parquet` -- those areas are Web Mercator.
    cap_improved_at_market : bool
        Apply the improved-parcel market-value clip. Off only for diagnostics.
    cap_include_building : bool, default False
        Cap land at `market_value - taxable_building` instead of `market_value` alone, so
        land plus building cannot exceed the parcel's total value. **Off by default because
        it is a much bigger change than it looks.** `market_value` is OPA's own
        `taxable_land + taxable_building` (verified: true for all but 10 of 583,249 TY2026
        parcels), so `market_value - taxable_building` reduces to essentially OPA's OWN land
        estimate for any parcel not otherwise capped. Measured on TY2026 (2026-09-05,
        `cap_improved_at_market=True` baseline): turning this on moves 142,515 parcels,
        drops 23.9% of single-family parcels to *exactly* OPA's ~0.20 land ratio -- the same
        default-ratio collapse LYCD exists to break -- and cuts the citywide LYCD/OPA land
        ratio from 1.54x to 1.32x. It is a real fix for a real problem: 24.4% of taxable
        non-vacant parcels already have `lycd_land_value + taxable_building > market_value`
        under the plain cap, $9.31B of double-counted value in aggregate (a materially larger
        and more consequential finding than the audit's original framing of 19,514 parcels
        already at land ratio 1.00). But "cap land, hold building fixed" is only one of
        several ways to resolve that inconsistency, and every one of them changes the model's
        headline land premium by a similar order of magnitude. Decide deliberately per
        notebook rather than accepting this as a silent default; see
        `docs/LYCD_LAND_MODEL_ROADMAP.md` for the finding and the open design question.
    land_pct_improved : float or pandas.Series
        See "Two refinements" above.
    zone_group_col : str, optional
        See "Two refinements" above.

    Returns
    -------
    LycdResult
        `.gdf` is a copy of `gdf` with `dor_area_sqft`, `area_source`, `gma1/2/3`,
        `gma_level`, `lycd_zone_psf`, `lycd_land_pct` and `lycd_land_value` added;
        `.diagnostics` holds the counts the notebooks print and assert on.
    """
    import numpy as np
    import pandas as pd
    from scipy.spatial import cKDTree

    out = gdf.copy()
    vacant = set(str(c) for c in vacant_codes)

    # --- Lot area: single convention, no spatial join ---
    out["pin"] = out["pin"].astype(str).str.strip()
    pin_lookup = pin_areas.copy()
    pin_lookup["pin"] = pin_lookup["pin"].astype(str).str.strip()
    pin_lookup = pin_lookup.drop_duplicates("pin").set_index("pin")["pin_area_sqft"]

    opa_area = pd.to_numeric(out["total_area"], errors="coerce").fillna(0.0)
    pin_area = out["pin"].map(pin_lookup)
    has_opa = opa_area > 0
    has_pin = pin_area.notna() & (pin_area > 0)
    override = has_opa & has_pin & ((opa_area / pin_area) > opa_pin_disagree_factor)

    area = pd.Series(
        np.where(override, pin_area, np.where(has_opa, opa_area, np.where(has_pin, pin_area, np.nan))),
        index=out.index, dtype=float,
    )
    area_src = pd.Series(
        np.where(override, "pin_override",
                 np.where(has_opa, "opa_total_area", np.where(has_pin, "pin_dor", "knn"))),
        index=out.index,
    )

    coords = _parcel_coords(out)

    def _knn_median(values: np.ndarray) -> np.ndarray:
        return _knn_median_fill(coords, values, knn_k)

    n_area_knn = int(area.isna().sum())
    area = pd.Series(_knn_median(area.values), index=out.index)
    out["dor_area_sqft"] = area
    out["area_source"] = area_src

    city_area_ratio = float(area.sum() / city_area_sqft)
    if city_area_ratio >= max_city_area_ratio:
        raise ValueError(
            f"Total lot area is {city_area_ratio:.2f}x the city -- the area layer is inflated. "
            "Check that PIN areas are Mercator-corrected and that no spatial join has been "
            "reintroduced."
        )

    # --- GMA zone labels ---
    g = gma.copy()
    g["key"] = g["key"].astype(str).str.zfill(9)
    g = g.drop_duplicates("key").set_index("key")
    for lvl in ("gma1", "gma2", "gma3"):
        out[lvl] = out["parcel_number"].astype(str).str.zfill(9).map(g[lvl])
    gma_matched = out["gma3"].notna()

    # --- Zone medians of total $/sqft over improved parcels, hierarchical ---
    cat = out["category_code"].astype(str).str.strip()
    # category_code may arrive as '1.0' from a float column; normalise like the notebooks do
    cat = pd.to_numeric(cat, errors="coerce").astype("Int64").astype(str)
    market = pd.to_numeric(out["market_value"], errors="coerce")
    has_market = market.notna() & (market > 0)
    has_area = area.notna() & (area > 0)
    is_improved = ~cat.isin(vacant) & has_market & has_area
    is_vacant = cat.isin(vacant) & has_market & has_area

    # Zone-median cell key: (zone_group, gma level) when zone_group_col is given, else just
    # the gma level -- a constant group prefix on every row partitions identically to no
    # grouping at all, so this one code path serves both cases.
    if zone_group_col is not None:
        if zone_group_col not in out.columns:
            raise ValueError(f"zone_group_col {zone_group_col!r} is not a column on gdf")
        group = out[zone_group_col]
    else:
        group = pd.Series("_all", index=out.index)

    def _cell_key(level_col: str) -> pd.Series:
        return pd.Series(list(zip(group, out[level_col])), index=out.index)

    key1, key2, key3 = _cell_key("gma1"), _cell_key("gma2"), _cell_key("gma3")

    total_psf = pd.Series(np.where(is_improved | is_vacant, market / area, np.nan), index=out.index)
    imp = pd.DataFrame({"psf": total_psf[is_improved], "k1": key1[is_improved],
                        "k2": key2[is_improved], "k3": key3[is_improved]})
    l3_n = key3.map(imp.groupby("k3")["psf"].count()).fillna(0)
    l2_n = key2.map(imp.groupby("k2")["psf"].count()).fillna(0)
    l3_med = key3.map(imp.groupby("k3")["psf"].median())
    l2_med = key2.map(imp.groupby("k2")["psf"].median())
    l1_med = key1.map(imp.groupby("k1")["psf"].median())

    zone_psf = pd.Series(
        np.where(l3_n >= min_improved, l3_med, np.where(l2_n >= min_improved, l2_med, l1_med)),
        index=out.index, dtype=float,
    )
    out["gma_level"] = np.where(
        ~gma_matched, "knn",
        np.where(l3_n >= min_improved, "L3", np.where(l2_n >= min_improved, "L2", "L1")),
    )

    if isinstance(land_pct_improved, pd.Series):
        land_pct_improved_arr = land_pct_improved.reindex(out.index).astype(float).to_numpy()
    else:
        land_pct_improved_arr = float(land_pct_improved)
    land_pct = pd.Series(np.where(is_vacant, land_pct_vacant, land_pct_improved_arr), index=out.index)

    # --- KNN fallback for parcels outside GMA coverage: impute the RATE, not the dollar
    # value. Imputing a dollar land value directly (the pre-2026-09 behaviour) had an
    # unmatched small parcel next to large ones inherit a land value sized for their lots,
    # ignoring its own area -- audit 2026-08-08 finding 5. Imputing the $/sqft rate instead
    # and then multiplying by this parcel's OWN area and land_pct fixes that: the KNN
    # contributes only a locally-typical price level, exactly as it does for area.
    n_knn = int((~gma_matched).sum())
    zone_psf = pd.Series(_knn_median(zone_psf.values), index=out.index)
    out["lycd_zone_psf"] = zone_psf
    out["lycd_land_pct"] = land_pct
    land = (zone_psf * land_pct * area).clip(lower=0).fillna(0.0)

    # --- Market-value cap, improved parcels only ---
    # cap_include_building (off by default -- see its docstring): also subtract taxable_building
    # from the ceiling, so land plus building cannot exceed market_value. `taxable_building` is
    # OPA's raw pre-abatement figure, 0 for abated parcels at this point in the pipeline (their
    # building is restored downstream, after this function returns), so this option is a no-op
    # for abated parcels either way.
    is_vac_code = cat.isin(vacant)
    pre_cap = float(land.sum())
    n_capped = 0
    if cap_improved_at_market:
        mv_cap = market.fillna(0).clip(lower=0)
        if cap_include_building:
            building = pd.to_numeric(out["taxable_building"], errors="coerce").fillna(0.0).clip(lower=0)
            mv_cap = (mv_cap - building).clip(lower=0)
        capped = (land > mv_cap) & ~is_vac_code
        n_capped = int(capped.sum())
        land = pd.Series(np.where(~is_vac_code, np.minimum(land, mv_cap), land), index=out.index)
    post_cap = float(land.sum())
    out["lycd_land_value"] = land

    diagnostics = {
        "area_source_counts": area_src.value_counts().to_dict(),
        "n_area_knn": n_area_knn,
        "n_pin_override": int(override.sum()),
        "total_lot_area_sqft": float(area.sum()),
        "city_area_ratio": city_area_ratio,
        "n_gma_matched": int(gma_matched.sum()),
        "gma_match_rate": float(gma_matched.mean()),
        "n_land_knn": n_knn,
        "gma_level_counts": out["gma_level"].value_counts().to_dict(),
        "n_capped": n_capped,
        "land_base_pre_cap": pre_cap,
        "land_base_post_cap": post_cap,
        "cap_removed_share": (pre_cap - post_cap) / pre_cap if pre_cap > 0 else float("nan"),
        "land_pct_improved": (
            f"per-parcel Series (median {float(np.median(land_pct_improved_arr)):.4f})"
            if isinstance(land_pct_improved, pd.Series) else land_pct_improved
        ),
        "land_pct_vacant": land_pct_vacant,
        "min_improved": min_improved,
        "zone_group_col": zone_group_col,
        "cap_include_building": cap_include_building,
    }
    return LycdResult(out, diagnostics)


@dataclass(frozen=True)
class CondoAreaResult:
    """Output of `condo_unit_areas`. Series aligned to the input frame's index."""
    area: "pd.Series"       # lot area, condo units replaced by their share of the building's lot
    source: "pd.Series"     # the input area source, or 'condo_share' | 'condo_accessory'
    diagnostics: dict

    def describe(self) -> str:
        d = self.diagnostics
        return (
            f"condo units: {d['n_units']:,} re-sized to their share of {d['n_buildings']:,} "
            f"buildings' lots ({d['n_accessory']:,} accessory units at zero; "
            f"{d['n_master_fallback']:,} lots measured by the sibling repo, the rest by PIN) | "
            f"condo lot area {d['area_before_sqft']/1e6:,.1f}M -> {d['area_after_sqft']/1e6:,.1f}M sqft "
            f"(buildings' lots total {d['master_area_sqft']/1e6:,.1f}M) | prior source: "
            + ", ".join(f"{k}={v:,}" for k, v in d["prior_source_counts"].items())
        )


def condo_unit_areas(
    gdf,
    condos,
    pin_areas,
    *,
    area_col: str = "dor_area_sqft",
    source_col: str = "area_source",
    parcel_col: str = "parcel_number",
    share_tolerance: float = 1e-6,
) -> CondoAreaResult:
    """Give each condominium unit its share of the building's lot as its lot area.

    OPA records a condo unit, DOR records the building's lot, and nothing in this repo's parcel
    cache says which is which. `compute_lycd_land_values`' area chain therefore hands a unit
    either OPA's `total_area` (the 1-sqft placeholder or the whole lot, depending on the
    building), the unit's own PIN (which, for the unit that anchors the building, is the whole
    lot), or a neighbour's area by KNN (a sibling unit's whole lot). A land rate multiplied by
    that area is wrong in both directions, and in the whole-lot case every unit in a building
    carries the building's full land value, so the building's land is counted once per unit.

    `philly_open_avmkit` resolves each unit to its master lot (`download_dor_parcels.py`:
    point-in-polygon against DOR) and publishes the unit's livable-area share of that lot
    (`condo_unit_share`, summing to 1 per building; 0 for accessory units such as parking
    stalls, which own none of the ground). This function imports that SHARE -- a ratio, as
    `paint_land_surface` imports a rate -- and multiplies by this repo's own PIN area for the
    master lot, so the lot is measured on this repo's convention. A master PIN absent from
    `pin_areas` falls back to the sibling repo's `condo_master_lot_sqft` (true ground square
    feet in both repos) and is counted.

    The guard reads the quantity the defect inflated: per building, the units' re-sized areas
    must sum to no more than the building's lot.

    Parameters
    ----------
    gdf : DataFrame
        Needs `parcel_col`, `area_col` and `source_col` (run `compute_lycd_land_values` first).
    condos : DataFrame
        `philly_open_avmkit`'s `in/opa_parcels.parquet` columns `parcel_number`,
        `is_condo_unit`, `condo_master_pin`, `condo_unit_share`, `condo_master_lot_sqft`.
    pin_areas : DataFrame
        `pin` + `pin_area_sqft`, the same Mercator-corrected table `compute_lycd_land_values` reads.

    Returns
    -------
    CondoAreaResult
        `.area` and `.source` replace `area_col` and `source_col`; parcels that are not condo
        units keep their values unchanged.
    """
    import numpy as np
    import pandas as pd

    c = condos[condos["is_condo_unit"].fillna(False).astype(bool)].copy()
    c = c[c["condo_unit_share"].notna() & c["condo_master_pin"].notna()]
    c["key"] = c["parcel_number"].astype(str).str.strip().str.zfill(9)
    c = c.drop_duplicates("key").set_index("key")
    c["master"] = c["condo_master_pin"].astype(str).str.strip()

    lookup = pin_areas.copy()
    lookup["pin"] = lookup["pin"].astype(str).str.strip()
    lookup = lookup.drop_duplicates("pin").set_index("pin")["pin_area_sqft"]
    own_lot = c["master"].map(lookup)
    fallback = own_lot.isna() | ~(own_lot > 0)
    c["master_area"] = own_lot.where(~fallback, pd.to_numeric(c["condo_master_lot_sqft"], errors="coerce"))
    c = c[c["master_area"].notna() & (c["master_area"] > 0)]
    c["unit_area"] = c["condo_unit_share"].astype(float) * c["master_area"]

    keys = gdf[parcel_col].astype(str).str.strip().str.zfill(9)
    is_unit = keys.isin(c.index)
    k = keys[is_unit]
    new_area = pd.Series(c["unit_area"].reindex(k).to_numpy(float), index=k.index)
    share = pd.Series(c["condo_unit_share"].reindex(k).to_numpy(float), index=k.index)
    master = pd.Series(c["master"].reindex(k).to_numpy(), index=k.index)
    master_area = pd.Series(c["master_area"].reindex(k).to_numpy(float), index=k.index)

    per_bldg = pd.DataFrame({"master": master, "share": share, "area": new_area, "lot": master_area})
    by = per_bldg.groupby("master").agg(share=("share", "sum"), area=("area", "sum"), lot=("lot", "first"))
    over = by["share"] > 1 + share_tolerance
    if over.any():
        raise ValueError(
            f"{int(over.sum()):,} condo buildings' unit shares sum past 1 (max {by['share'].max():.6f}); "
            "the sibling repo's condo_unit_share is double-counting a building's units")
    over_lot = by["area"] > by["lot"] * (1 + share_tolerance)
    if over_lot.any():
        raise ValueError(f"{int(over_lot.sum()):,} condo buildings' units hold more than the building's lot")

    area = pd.to_numeric(gdf[area_col], errors="coerce").astype(float).copy()
    source = gdf[source_col].astype(object).copy()
    before = float(area[is_unit].sum())
    prior = source[is_unit].value_counts().to_dict()
    area[is_unit] = new_area
    source[is_unit] = np.where(share.to_numpy() > 0, "condo_share", "condo_accessory")

    diagnostics = {
        "n_units": int(is_unit.sum()),
        "n_condo_rows_in_source": int(len(c)),
        "n_buildings": int(by.shape[0]),
        "n_accessory": int((share == 0).sum()),
        "n_master_fallback": int(fallback.reindex(k).fillna(False).sum()),
        "area_before_sqft": before,
        "area_after_sqft": float(new_area.sum()),
        "master_area_sqft": float(by["lot"].sum()),
        "prior_source_counts": prior,
    }
    return CondoAreaResult(area, source, diagnostics)


@dataclass(frozen=True)
class LotAreaCorrectionResult:
    """Output of `corrected_lot_areas`. Series aligned to the input frame's index."""
    area: "pd.Series"       # lot area, implausible OPA records replaced by the sibling repo's
    source: "pd.Series"     # the input area source, or `corrected_label`
    diagnostics: dict

    def describe(self) -> str:
        d = self.diagnostics
        return (
            f"recorded lot areas: {d['n_corrected']:,} OPA records replaced by the sibling repo's "
            f"polygon area ({d['n_flagged_in_roll']:,} flagged there, {d['n_flagged_matched']:,} "
            f"on this frame, {d['n_kept_own_source']:,} already on another source here) | "
            f"their area {d['area_before_sqft']/1e6:,.1f}M -> {d['area_after_sqft']/1e6:,.1f}M sqft"
        )


def corrected_lot_areas(
    gdf,
    roll,
    *,
    area_col: str = "dor_area_sqft",
    source_col: str = "area_source",
    parcel_col: str = "parcel_number",
    flag_value: str = "pwd_dor",
    replace_sources=("opa_total_area",),
    corrected_label: str = "pwd_dor",
    max_residual_ratio: float = 2.0,
) -> LotAreaCorrectionResult:
    """Replace OPA lot areas the sibling repo has found implausible with its polygon area.

    `compute_lycd_land_values` takes OPA's `total_area` first and lets a surveyed polygon
    override it only when OPA is more than 3x the parcel's own PIN polygon. `philly_open_avmkit`
    applies a tighter rule with a second witness: an OPA area more than 2x both the DOR polygon
    and PWD's polygon for the same account is replaced (`land_area_source == 'pwd_dor'` on its
    land roll, the area it used in `land_area_sqft`). Its surfaces are fitted and painted on the
    corrected areas, so a replaced lot carries a rate for its true size; multiplied by an OPA
    area still 2-3x too large (or by a polygon borrowed from a neighbouring pin), that rate
    overstates land. LVTShift has no PWD polygons, so it cannot apply the rule itself.

    This imports the correction for exactly the flagged parcels, and only where this repo's
    chain still uses the value the rule rejected (`replace_sources`, OPA's recorded area).
    Parcels already on another source keep it: a `pin_override` row is this repo's own polygon,
    and a condo unit's `condo_share` is a share of its building's lot, not the lot. Both repos
    measure true ground square feet, as `condo_unit_areas` relies on for its master-lot fallback.

    The guard reads the quantity the defect inflated: after the correction, no flagged parcel
    may keep an area more than `max_residual_ratio` times the sibling repo's, whatever its source.

    Parameters
    ----------
    gdf : DataFrame
        Needs `parcel_col`, `area_col` and `source_col` (run `compute_lycd_land_values` first,
        and `condo_unit_areas` before this, so condo units are already on their share).
    roll : DataFrame
        `philly_open_avmkit`'s `out/land/land_roll_ty<YEAR>.csv` columns `parcel_number`,
        `land_area_sqft` and `land_area_source`.

    Returns
    -------
    LotAreaCorrectionResult
        `.area` and `.source` replace `area_col` and `source_col`; unflagged parcels are unchanged.
    """
    import numpy as np
    import pandas as pd

    missing = {"parcel_number", "land_area_sqft", "land_area_source"} - set(roll.columns)
    if missing:
        raise ValueError(f"the land roll lacks {sorted(missing)}: it predates the sibling repo's "
                         "lot-area rule. Rebuild it with build_land_roll.py")
    r = roll[roll["land_area_source"].astype(str).eq(flag_value)].copy()
    r["key"] = r["parcel_number"].astype(str).str.strip().str.zfill(9)
    r["sibling_area"] = pd.to_numeric(r["land_area_sqft"], errors="coerce")
    r = r[r["sibling_area"] > 0].drop_duplicates("key").set_index("key")["sibling_area"]

    keys = gdf[parcel_col].astype(str).str.strip().str.zfill(9)
    area = pd.to_numeric(gdf[area_col], errors="coerce").astype(float).copy()
    source = gdf[source_col].astype(object).copy()
    sibling = pd.Series(keys.map(r).to_numpy(float), index=gdf.index)
    flagged = sibling.notna()
    fix = flagged & source.isin(list(replace_sources))

    # A correction shrinks an inflated record. One that grows it means the keys or the two
    # repos' area conventions have come apart, and nothing downstream would notice.
    grows = fix & (sibling > area)
    if grows.any():
        raise ValueError(f"{int(grows.sum()):,} flagged lots would GROW under the sibling repo's area; "
                         "the correction is meant to replace inflated OPA records")

    before = float(area[fix].sum())
    area[fix] = sibling[fix]
    source[fix] = corrected_label

    residual = flagged & (area > max_residual_ratio * sibling)
    if residual.any():
        by = source[residual].value_counts().to_dict()
        raise ValueError(f"{int(residual.sum()):,} flagged lots still exceed {max_residual_ratio:g}x the "
                         f"sibling repo's area after the correction (sources: {by}); add their source "
                         "to replace_sources or explain why it is right")

    diagnostics = {
        "n_flagged_in_roll": int(len(r)),
        "n_flagged_matched": int(flagged.sum()),
        "n_corrected": int(fix.sum()),
        "n_kept_own_source": int((flagged & ~fix).sum()),
        "kept_source_counts": source[flagged & ~fix].value_counts().to_dict(),
        "area_before_sqft": before,
        "area_after_sqft": float(area[fix].sum()),
    }
    return LotAreaCorrectionResult(area, source, diagnostics)


@dataclass(frozen=True)
class LandSurfaceResult:
    """Output of `paint_land_surface`. Series aligned to the input frame's index."""
    psf: "pd.Series"            # $/sqft actually used, after the KNN fill
    land_value: "pd.Series"     # psf x LVTShift's own lot area, improved parcels capped
    source: "pd.Series"         # 'surface' (joined) | 'knn' (filled from matched neighbours)
                                #   | 'opa_beyond_support' (a vacant lot past the support edge)
    diagnostics: dict
    beyond_support: "pd.Series | None" = None   # lot larger than the surface's sales can test

    def describe(self) -> str:
        d = self.diagnostics
        return (
            f"land surface '{d['label']}': {d['n_matched']:,} parcels joined "
            f"({d['match_rate']:.1%}); {d['n_knn']:,} filled by {d['knn_k']}-NN rate "
            f"(carrying {d['knn_value_share']:.1%} of market value) | "
            f"cap on parcels with a building ({d.get('bare_rule', 'category_code')} is bare): {d['n_capped']:,} parcels, "
            f"${d['land_base_pre_cap']/1e9:.2f}B -> ${d['land_base_post_cap']/1e9:.2f}B "
            f"({d['cap_removed_share']:.1%} removed) | "
            f"land > total on {d['n_land_exceeds_total']:,} improved parcels before the cap"
            + ("" if not d.get("support_edge_sqft") else
               f" | beyond the {d['support_edge_sqft']:,.0f} sqft support edge: {d['n_beyond_support']:,} parcels, "
               f"{d['n_carried_at_opa']:,} bare ones carried at OPA (${d['carried_value']/1e9:.2f}B, "
               f"against ${d['surface_value_on_carried']/1e9:.2f}B painted)")
        )


def paint_land_surface(
    gdf,
    surface,
    *,
    rate_col: str,
    parcel_col: str = "parcel_number",
    area_col: str = "dor_area_sqft",
    knn_k: int = 5,
    cap_improved_at_market: bool = True,
    vacant_codes=VACANT_CATEGORY_CODES,
    label: "str | None" = None,
    support: "dict | None" = None,
    carry_col: str = "opa_gross_land",
    building_col: "str | None" = "opa_gross_building",
) -> LandSurfaceResult:
    """Paint an externally estimated land $/sqft surface onto LVTShift's parcel frame.

    The surface comes from `philly_open_avmkit`'s `run_land_surfaces.py`
    (`out/land/land_surfaces_ty2026.csv`): one row per parcel in that repo's AVM universe with
    several candidate land rates. This function is the seam the roadmap's repo boundary
    describes -- the sibling repo answers "what is a square foot of land worth here," LVTShift
    answers "what does that do to the tax base" -- and it deliberately imports a RATE, not a
    dollar value, so that LVTShift's own lot-area convention (`compute_lycd_land_values`'s
    `dor_area_sqft` chain) decides every parcel's land value. The two repos' areas agree within
    5% on 99.9% of joined parcels, but a rate keeps them decoupled by construction.

    Coverage is the reason this is a function and not a merge. Some of LVTShift's parcels are
    not in the AVM universe at all, and an inner join would silently drop them and their share
    of the roll (`diagnostics` reports both). Unmatched parcels take the median rate of their
    `knn_k` nearest matched neighbours -- the same `_knn_median_fill` LYCD uses for parcels
    outside GMA coverage -- and are flagged `source == 'knn'` so the report can say how much
    of the base rests on the fill.

    A parcel with a building has its land clipped at `market_value`; a bare one is taken whole.
    "Bare" is OPA's own split: `building_col` (gross building, taxable plus exempt) at $1 or
    less -- the same test the re-split's `uncap_bare_land` applies, so the painted land and the
    reform that reads it agree on which lots are bare. The category code is not the test: OPA
    files lots built on after the roll was set, parking lots and parkland under improved codes
    with a $0 building line, and some vacant-coded parcels (parking, playgrounds) carry most of
    their value on a building line; on TY2026 the code rule capped ~730 bare lots at OPA's land
    and left ~140 parcels with a building uncapped above their total. `building_col=None` keeps
    the category-code rule (LYCD's own convention, `compute_lycd_land_values`).
    `n_land_exceeds_total` counts how often the surface's land alone exceeds OPA's total
    assessment on a parcel with a building before the cap binds: under the draft ordinance's
    Sec. 3(c) that is not a permitted assessment, so it is a finding about the totals, and the
    report needs the count.

    `support` is the surface's own statement of where its evidence ends: the parsed
    `land_surface_support.json` that `run_land_surfaces.py` writes beside the surface
    (`edge_sqft`, `size_elasticity`, and the names of the surface's painted-area and
    beyond-support columns). With it, three things change, all of them about lots the sales
    cannot speak to. (1) The KNN fill re-sizes each neighbour's rate to the subject's own lot
    (`_knn_resized_rate_fill`) and draws only on neighbours inside the support edge. (2) Every
    parcel whose own lot, or whose painted lot, exceeds the edge is flagged `beyond_support`.
    (3) A flagged BARE parcel (by the same test as the cap) is carried at `carry_col` (OPA's own
    gross land value) and marked `source == 'opa_beyond_support'`: a parcel with a building has
    its land capped at its total, so an extrapolated rate cannot add value to the base, but a bare
    lot is taken whole and nothing bounds it (audit 2026-09-21, finding 3). The rate is kept on every flagged parcel, so a
    reform step can still rescale it (`relevel_beyond_support`) instead of taking the carry. Without
    `support` the function behaves exactly as before, for surfaces that publish no edge.

    Parameters
    ----------
    gdf : GeoDataFrame
        Needs `parcel_col`, `category_code`, `market_value`, `area_col` (run
        `compute_lycd_land_values` first), `building_col` unless it is None, and point
        geometry; `carry_col` when `support` is given.
    surface : DataFrame
        `parcel_col` (any zero-padding; normalised to 9 digits) and `rate_col` in $/sqft.
    rate_col : str
        Which candidate to paint, e.g. `'s2_k20'`.
    """
    import numpy as np
    import pandas as pd

    label = label or rate_col
    out_index = gdf.index
    keys = gdf[parcel_col].astype(str).str.strip().str.zfill(9)

    # Indexed strictly: a surface said to publish a support edge must carry its columns.
    extra = [support["area_column"], support["flag_column"]] if support else []
    s = surface[[parcel_col, rate_col, *extra]].copy()
    s[parcel_col] = s[parcel_col].astype(str).str.strip().str.zfill(9)
    s[rate_col] = pd.to_numeric(s[rate_col], errors="coerce")
    s = s.dropna(subset=[rate_col]).drop_duplicates(parcel_col).set_index(parcel_col)

    psf = pd.Series(keys.map(s[rate_col]).to_numpy(dtype=float), index=out_index)
    matched = psf.notna()
    coords = _parcel_coords(gdf)
    area = pd.to_numeric(gdf[area_col], errors="coerce")
    if support:
        edge = float(support["edge_sqft"])
        painted_area = pd.to_numeric(keys.map(s[support["area_column"]]), errors="coerce").to_numpy(float)
        flagged = keys.map(s[support["flag_column"]].astype(str).str.lower().eq("true")).eq(True).to_numpy()
        psf_filled = pd.Series(_knn_resized_rate_fill(
            coords, psf.to_numpy(), painted_area, area.to_numpy(float), ~flagged,
            float(support["size_elasticity"]), knn_k), index=out_index)
        beyond = pd.Series(flagged | (area.fillna(0).to_numpy(float) > edge), index=out_index)
    else:
        psf_filled = pd.Series(_knn_median_fill(coords, psf.to_numpy(), knn_k), index=out_index)
        beyond = pd.Series(False, index=out_index)
    source = pd.Series(np.where(matched, "surface", "knn"), index=out_index)

    market = pd.to_numeric(gdf["market_value"], errors="coerce")
    cat = pd.to_numeric(gdf["category_code"].astype(str).str.strip(), errors="coerce").astype("Int64").astype(str)
    is_vac = cat.isin(set(str(c) for c in vacant_codes))
    if building_col is None:
        bare = is_vac
    else:
        if building_col not in gdf.columns:
            raise KeyError(f"paint_land_surface: '{building_col}' (OPA's gross building line) is needed to tell bare "
                           "lots from built ones; pass building_col=None for the category-code rule")
        bare = pd.to_numeric(gdf[building_col], errors="coerce").fillna(0.0).le(1.0)

    land = (psf_filled * area).clip(lower=0).fillna(0.0)
    # Bare by OPA's own split, not by the category code: OPA books most of the value of many
    # vacant-coded parcels (parking lots, parkland) to a building line, and carrying its land
    # figure alone would strip that value out of land. Those have a total to cap against.
    opa_land = pd.to_numeric(gdf[carry_col], errors="coerce") if support else None
    carried = (beyond & bare & opa_land.notna() & (market.fillna(0).clip(lower=0) - opa_land.fillna(0)).le(1.0)
               if support else beyond & bare)
    surface_value_on_carried = float(land[carried].sum())
    if carried.any():
        land = land.where(~carried, opa_land.fillna(0.0).clip(lower=0))
        source = source.where(~carried, "opa_beyond_support")
    pre_cap = float(land.sum())
    mv = market.fillna(0).clip(lower=0)
    exceeds = (land > mv) & ~bare & (mv > 0)
    n_capped = 0
    if cap_improved_at_market:
        n_capped = int(exceeds.sum())
        land = pd.Series(np.where(~bare, np.minimum(land, mv), land), index=out_index)
    post_cap = float(land.sum())

    mv_total = float(mv.sum())
    diagnostics = {
        "label": label,
        "rate_col": rate_col,
        "n_parcels": int(len(gdf)),
        "n_matched": int(matched.sum()),
        "match_rate": float(matched.mean()),
        "n_knn": int((~matched).sum()),
        "knn_k": knn_k,
        "knn_value_share": float(mv[~matched].sum() / mv_total) if mv_total else float("nan"),
        "n_capped": n_capped,
        "n_land_exceeds_total": int(exceeds.sum()),
        "bare_rule": "category_code" if building_col is None else f"{building_col} <= $1",
        "n_bare_with_improved_code": int((bare & ~is_vac).sum()),
        "n_built_with_vacant_code": int((~bare & is_vac).sum()),
        "land_base_pre_cap": pre_cap,
        "land_base_post_cap": post_cap,
        "cap_removed_share": (pre_cap - post_cap) / pre_cap if pre_cap > 0 else float("nan"),
        "median_psf_matched": float(psf[matched].median()) if matched.any() else float("nan"),
        "median_psf_knn": float(psf_filled[~matched].median()) if (~matched).any() else float("nan"),
        "support_edge_sqft": float(support["edge_sqft"]) if support else None,
        "n_beyond_support": int(beyond.sum()),
        "n_carried_at_opa": int(carried.sum()),
        "carried_value": float(land[carried].sum()),
        "surface_value_on_carried": surface_value_on_carried,
    }
    return LandSurfaceResult(psf_filled, land, source, diagnostics, beyond)


@dataclass(frozen=True)
class _ExemptionParts:
    """OPA's recorded values and exemptions, decomposed the way both reform functions need.

    Shared by `carry_forward_exemptions` and `reallocate_land_within_total` so the two cannot
    disagree about what a parcel's exemptions are; only how they carry into the reform differs.
    """
    gross_land: "pd.Series"
    gross_building: "pd.Series"
    exempt_land: "pd.Series"
    exempt_total: "pd.Series"
    homestead_amount: "pd.Series"
    old_taxable: "pd.Series"
    homestead_active: "pd.Series"       # flag set AND an exemption applied this vintage
    other_exempt: "pd.Series"           # non-homestead exemption dollars (abatement / partial)
    homestead_wiped: "pd.Series"        # fully exempt today only because the homestead covers it
    institutional: "pd.Series"          # fully exempt today for a reason the homestead cannot explain


def _decompose_exemptions(gdf, homestead_col: str, match_tolerance: float) -> _ExemptionParts:
    import numpy as np
    import pandas as pd

    if homestead_col not in gdf.columns:
        raise ValueError(
            f"{homestead_col!r} is not in the frame. Rebuild the parcel cache with "
            "scripts/build_philadelphia_parcel_cache.py --force; the exemption rules key the "
            "Homestead Exemption off OPA's per-parcel flag."
        )

    def _num(col):
        return pd.to_numeric(gdf[col], errors="coerce").fillna(0.0).astype(float)

    tax_land, tax_bldg = _num("taxable_land"), _num("taxable_building")
    ex_land, ex_bldg = _num("exempt_land"), _num("exempt_building")
    hs_amt = _num(homestead_col).clip(lower=0)
    gross_bldg = (tax_bldg + ex_bldg).clip(lower=0)
    gross_land = (tax_land + ex_land).clip(lower=0)
    ex_total = ex_land + ex_bldg
    old_taxable = (tax_land + tax_bldg).clip(lower=0)
    full_exmp = old_taxable <= 0

    hs_active = (hs_amt > 0) & (ex_total > 0)
    other_exempt = (ex_total - np.where(hs_active, hs_amt, 0.0)).clip(lower=0)
    homestead_wiped = full_exmp & hs_active & (other_exempt <= match_tolerance)
    institutional = full_exmp & ~homestead_wiped
    return _ExemptionParts(gross_land, gross_bldg, ex_land, ex_total, hs_amt, old_taxable,
                           hs_active, other_exempt, homestead_wiped, institutional)


HOMESTEAD_ORDERS = ("building_first", "land_first", "value_share", "tax_share")


def _check_homestead_order(homestead_order: str, rate_ratio) -> None:
    if homestead_order not in HOMESTEAD_ORDERS:
        raise ValueError(f"homestead_order must be one of {HOMESTEAD_ORDERS}, not {homestead_order!r}")
    if homestead_order == "tax_share" and not (rate_ratio is not None and float(rate_ratio) > 0):
        raise ValueError("homestead_order='tax_share' weights land by the land/building rate ratio; "
                         "pass it as a positive rate_ratio")


def _apply_exemptions(new_land, bldg, other_exempt, parts: _ExemptionParts, homestead_cap: float,
                      *, homestead_order: str = "building_first", rate_ratio: "float | None" = None,
                      value_share_relief=None):
    """Building-first application of the dollar exemptions, then the homestead at min(cap, value).

    `other_exempt` is passed explicitly (rather than read from `parts`) because the two reform
    functions differ in exactly this: carry-forward keeps the recorded dollars, reallocation
    re-derives an abatement's dollars from the new building value.

    `homestead_order` decides which line the homestead is drawn against. The amount is
    `min(cap, value)` under every order, so at a single rate the order cannot move a bill. Under a
    split rate it decides what the exclusion is worth:

    - ``building_first`` -- off the improvement, the remainder off land. This is the law, 53
      Pa.C.S. Sec. 8583(c): in a political subdivision taxing land and improvements at different
      rates, the exclusion "shall be applied first to the value of the improvements". OPA's
      TY2026 records follow it on every homestead-only parcel. Worth `cap x building rate`.
    - ``land_first`` -- off land, the remainder off the improvement. Worth `cap x land rate`
      while the land line covers the cap.
    - ``value_share`` -- in proportion to the two lines' values, so the exclusion cuts the
      parcel's bill by the fraction `cap / value` at any pair of rates.
    - ``tax_share`` -- in proportion to each line's share of the parcel's tax before the
      exclusion: land weighted by `rate_ratio`, the land/building rate ratio.

    Every order other than the statutory one needs Sec. 8583(c) amended. Whatever one line cannot
    absorb falls on the other, so the amount exempted is the same under every order.

    `value_share_relief` (boolean, aligned) takes those rows' non-homestead dollars off land and
    building in proportion to the two lines' values instead of building-first -- the treatment
    `reallocate_land_within_total(relief_order="value_share")` gives relief against total value.
    The dollars exempted are the same; only their line moves.
    """
    import numpy as np
    import pandas as pd

    _check_homestead_order(homestead_order, rate_ratio)
    idx = parts.gross_land.index
    b1 = (bldg - other_exempt).clip(lower=0)
    spill = (other_exempt - bldg).clip(lower=0)
    l1 = (new_land - spill).clip(lower=0)
    if value_share_relief is not None and np.any(value_share_relief):
        o, b, l = (np.asarray(x, float) for x in (other_exempt, bldg, new_land))
        tot = b + l
        want = o * np.divide(l, tot, out=np.zeros_like(o), where=tot > 0)
        from_land = np.clip(want, np.clip(o - b, 0, None), np.minimum(o, l))
        m = np.asarray(value_share_relief, bool)
        l1 = pd.Series(np.where(m, np.clip(l - from_land, 0, None), np.asarray(l1, float)), index=idx)
        b1 = pd.Series(np.where(m, np.clip(b - (o - from_land), 0, None), np.asarray(b1, float)), index=idx)
    hs_ex = pd.Series(np.where(parts.homestead_active, np.minimum(homestead_cap, b1 + l1), 0.0), index=idx)
    if homestead_order == "building_first":
        b2 = (b1 - hs_ex).clip(lower=0)
        spill2 = (hs_ex - b1).clip(lower=0)
        l2 = (l1 - spill2).clip(lower=0)
    else:
        h, b, l = hs_ex.to_numpy(float), np.asarray(b1, float), np.asarray(l1, float)
        if homestead_order == "land_first":
            want_from_land = h
        else:
            k = 1.0 if homestead_order == "value_share" else float(rate_ratio)
            denom = k * l + b
            want_from_land = h * np.divide(k * l, denom, out=np.zeros_like(h), where=denom > 0)
        # h <= b + l, so the land line can always take between max(0, h - b) and min(h, l).
        from_land = np.clip(want_from_land, np.clip(h - b, 0, None), np.minimum(h, l))
        l2 = pd.Series(np.clip(l - from_land, 0, None), index=idx)
        b2 = pd.Series(np.clip(b - (h - from_land), 0, None), index=idx)
    b2 = b2.where(~parts.institutional, 0.0)
    l2 = l2.where(~parts.institutional, 0.0)
    return l2, b2


def _reconstruction_guard(parts: _ExemptionParts, rec_land, rec_bldg, match_tolerance: float,
                          reconstruction_floor: float):
    """OPA's own values through the rule must reproduce OPA's own taxable total per parcel."""
    rec_total = rec_land + rec_bldg
    match = (rec_total - parts.old_taxable).abs() <= match_tolerance
    match_rate = float(match.mean())
    if match_rate < reconstruction_floor:
        raise ValueError(
            f"Exemption rule reproduces OPA's taxable total on only {match_rate:.2%} of "
            f"parcels (floor {reconstruction_floor:.0%}). The rule does not describe how this "
            "vintage's exemptions were applied -- check homestead_cap against the assessment year "
            "and the homestead flag's vintage before trusting any reform base built on it."
        )
    mismatch_dollars = float((rec_total - parts.old_taxable)[~match].abs().sum())
    return match_rate, mismatch_dollars


@dataclass(frozen=True)
class ExemptionCarryForward:
    """Output of `carry_forward_exemptions`. All Series are aligned to the input index."""
    reform_taxable_land: "pd.Series"
    reform_taxable_building: "pd.Series"
    reform_taxable_total: "pd.Series"
    institutional_exempt: "pd.Series"     # stays fully exempt under the reform
    homestead_active: "pd.Series"         # a Homestead Exemption is re-applied under the reform
    other_exempt_dollars: "pd.Series"     # non-homestead exemption carried forward in dollars
    diagnostics: dict

    def describe(self) -> str:
        d = self.diagnostics
        return (
            f"exemption carry-forward: {d['n_homestead_active']:,} homestead parcels re-exempted "
            f"at min(cap, value) | {d['n_other_exempt']:,} carry non-homestead dollars "
            f"(abatement / partial) | {d['n_institutional_exempt']:,} stay fully exempt | "
            f"{d['n_homestead_wiped_reentering']:,} homestead-wiped parcels re-enter the base "
            f"| flag-without-exemption (vintage mismatch): {d['n_homestead_flag_no_exemption']:,} "
            f"| OPA-base reconstruction match: {d['reconstruction_match_rate']:.2%}"
        )


def carry_forward_exemptions(
    gdf,
    *,
    new_land_col: str,
    homestead_cap: float,
    homestead_col: str = "homestead_exemption",
    match_tolerance: float = 1.0,
    reconstruction_floor: float = 0.95,
    gross_building_override: "pd.Series | None" = None,
    homestead_order: str = "building_first",
    homestead_rate_ratio: "float | None" = None,
) -> ExemptionCarryForward:
    """Re-apply each parcel's existing exemptions to a base whose LAND value has changed.

    "Same assessment methodology, land valued differently" means every relief that exists today
    still exists tomorrow, applied the way OPA applies it. Three kinds of relief show up in the
    OPA columns, and they carry forward three different ways:

    - **Homestead Exemption** -- a flat statutory amount, `homestead_cap`, applied to the
      building line first and the remainder to land (verified against OPA data: essentially
      no homestead parcel has exempt land while taxable building remains). Under the reform
      it is re-applied as `min(cap, new total)`, so a parcel whose value the new land lifts
      above the cap becomes taxable again. Active only where OPA's per-parcel flag is set
      AND an exemption was actually applied this vintage (the flag is current-vintage).
    - **Everything else that is a dollar amount** -- the 10-year construction abatement
      (building only) and partial institutional relief -- carried forward in dollars
      (`exempt_total - homestead`), applied building-first. The building value is unchanged
      by a land reform, so an abatement's dollars are exactly right.
    - **Full institutional exemption** -- a rate, not an amount. A fully exempt parcel whose
      exemption is not explained by the homestead alone stays fully exempt.

    The guard is a reconstruction: feeding OPA's own gross land back through this rule must
    reproduce OPA's taxable total per parcel. The match rate is reported and enforced
    (`reconstruction_floor`); if the rule mis-described the exemptions, this is the number that
    would move. **The guard always checks against OPA's own recorded building value,
    regardless of `gross_building_override`** -- it validates the exemption RULE (does this
    rule reproduce today's actual bill from today's actual numbers), which is a fact about the
    rule, not about whichever building scenario the real call below is exploring.

    `gross_building_override` lets a caller replace "building unchanged, land revised" with a
    different building scenario for the real (non-guard) computation -- see
    `compute_residual_building_value` for the reason this exists: holding OPA's own building
    value fixed while substantially raising land (e.g. LYCD's land estimate) routinely pushes
    land + building above `market_value`, because `market_value` is OPA's own
    `taxable_land + taxable_building`, so a land correction implies a building correction too if
    the total is to stay anchored to the (trusted) market value. The override only changes
    which building value the SAME exemption rule above is applied to; it changes no exemption
    logic itself.

    **The override cannot make an abated parcel's building value "pass through unchanged."**
    `other_exempt` is computed from that SAME parcel's historical exempt total (here, its
    `exempt_building`), and it is always subtracted from whatever gross building value this
    function is given -- so feeding an abated row's own `exempt_building` back in as the
    override cancels it to exactly zero, not to itself. This is not a bug to work around with a
    cleverer override; it is what "carry forward the existing exemption rule" correctly means
    for a rule that says the abatement's dollars stay exempt. It is right for the reassessment
    use case ("same methodology, only land revalued") and wrong for anything that means the
    abatement to have ended. Exclude abated rows from this call entirely; do not rely on an
    override to preserve them.

    Parameters
    ----------
    gdf : DataFrame
        Needs `taxable_land`, `taxable_building`, `exempt_land`, `exempt_building`,
        `market_value`, `homestead_col` and `new_land_col`.
    new_land_col : str
        The reform's gross (pre-exemption) land value, e.g. `lycd_land_value`.
    homestead_cap : float
        The tax year's statutory Homestead Exemption, from `tax_year_params(year)`.
    gross_building_override : pandas.Series, optional
        Per-parcel gross (pre-exemption) building value to use for the real computation
        instead of OPA's own `taxable_building + exempt_building`. Aligned to `gdf.index`;
        missing/negative values are treated as 0. Leave `None` for the existing behavior
        (building held at OPA's own recorded value, land alone revised).
    homestead_order, homestead_rate_ratio
        Which line the homestead is drawn against in the reform; see `_apply_exemptions`. The
        default is the statutory building-first order, and the guard always uses it, because it
        checks the rule against OPA's records.

    Returns
    -------
    ExemptionCarryForward
    """
    import pandas as pd

    _check_homestead_order(homestead_order, homestead_rate_ratio)
    p = _decompose_exemptions(gdf, homestead_col, match_tolerance)

    # Guard: OPA's own land AND OPA's own building through the same rule must reproduce OPA's
    # own taxable total -- this validates the rule itself, so it never uses the override.
    rec_land, rec_bldg = _apply_exemptions(p.gross_land, p.gross_building, p.other_exempt, p, homestead_cap)
    match_rate, mismatch_dollars = _reconstruction_guard(
        p, rec_land, rec_bldg, match_tolerance, reconstruction_floor)

    new_land = pd.to_numeric(gdf[new_land_col], errors="coerce").fillna(0.0).clip(lower=0)
    if gross_building_override is not None:
        bldg_for_reform = gross_building_override.reindex(gdf.index).fillna(0.0).clip(lower=0).astype(float)
    else:
        bldg_for_reform = p.gross_building
    ref_land, ref_bldg = _apply_exemptions(new_land, bldg_for_reform, p.other_exempt, p, homestead_cap,
                                           homestead_order=homestead_order, rate_ratio=homestead_rate_ratio)

    diagnostics = {
        "n_homestead_active": int(p.homestead_active.sum()),
        "n_homestead_flag_no_exemption": int(((p.homestead_amount > 0) & (p.exempt_total <= 0)).sum()),
        "n_other_exempt": int((p.other_exempt > match_tolerance).sum()),
        "n_institutional_exempt": int(p.institutional.sum()),
        "n_homestead_wiped": int(p.homestead_wiped.sum()),
        "n_homestead_wiped_reentering": int((p.homestead_wiped & ((ref_land + ref_bldg) > 0)).sum()),
        "reconstruction_match_rate": match_rate,
        "reconstruction_mismatch_dollars": mismatch_dollars,
        "homestead_cap": float(homestead_cap),
        "homestead_order": homestead_order,
        "used_gross_building_override": gross_building_override is not None,
    }
    return ExemptionCarryForward(
        ref_land, ref_bldg, ref_land + ref_bldg, p.institutional, p.homestead_active,
        p.other_exempt, diagnostics,
    )


def compute_residual_building_value(
    gdf,
    land_col: str,
    market_value_col: str = "market_value",
) -> "pd.Series":
    """Gross building value implied by holding `market_value` fixed while revising land.

    Background: a revised land estimate (e.g. LYCD's) routinely exceeds OPA's own implicit
    land share. Every LYCD notebook currently holds `model_building` at OPA's own recorded
    value regardless, which means `model_land + model_building` can exceed `market_value` --
    not a rare edge case: measured on TY2026 with the plain land-only cap, 24.4% of taxable
    non-vacant parcels already have `lycd_land_value + taxable_building > market_value`, $9.31B
    of double-counted value in aggregate (see `docs/LYCD_LAND_MODEL_ROADMAP.md`).

    Capping LAND to fix this (`compute_lycd_land_values`'s `cap_include_building`) turns out to
    reduce to capping land at essentially OPA's own land estimate for most parcels, since
    `market_value` IS `taxable_land + taxable_building` under OPA's own methodology (true for
    all but 10 of 583,249 TY2026 parcels) -- it defeats the land correction it was meant to
    accompany. This function takes the other side of the same identity instead: hold
    `market_value` (OPA's trusted TOTAL) fixed, and let BUILDING absorb the correction implied
    by a larger land estimate, exactly as every notebook already does implicitly for the small
    cohort whose land is capped at exactly `market_value` (documented there as "structure
    implicitly worth zero") -- this just extends the same idea to every parcel, proportional to
    how much the land estimate moved, rather than only to the fully-capped extreme.

    This is NOT the same as `model_building = market_value - land` computed carelessly: that
    naive version compares a GROSS residual (against gross `market_value`) to OPA's actual
    `taxable_building` column, which is NET of exemptions (Homestead above all) -- silently
    re-admitting exempted value. Measured on TY2026 that inflated the taxable building base by
    $43.7B (+40%), touching 99.6% of improved taxable parcels. The fix is sequencing: this
    function returns a GROSS building value (paired with a GROSS land value), meant to be fed
    into `carry_forward_exemptions`'s `gross_building_override` -- which re-applies OPA's own
    exemption rule (homestead, abatement, institutional) to the (land, building) pair AFTER
    this residual split, so exemptions net out exactly once, not zero or twice.

    **Do not use this for abated parcels, even with an `abated_mask`-style branch that returns
    their observed `exempt_building` unchanged -- that was tried and does not work.** Feeding
    an abated row's own `exempt_building` through as this function's output and then into
    `carry_forward_exemptions`'s `gross_building_override` does NOT preserve it. That function's
    own exemption-netting computes `other_exempt` from the SAME parcel's historical exempt
    total and subtracts it from whatever gross building value it is given -- for an abated row
    that dollar amount IS `exempt_building`, so it cancels back to zero regardless of what this
    function returns. Measured on TY2026: routing all 14,287 abated parcels through this
    pipeline (with an abated-passthrough branch that looked correct in isolation) silently
    zeroed out $16.4B of building value that the four LYCD notebooks currently, correctly,
    restore via `exempt_building`. Abated parcels must be excluded from the whole
    residual-and-carry-forward pipeline -- computed and merged in separately, exactly as every
    LYCD notebook already does -- not routed through it with a clever override. This is a
    property of `carry_forward_exemptions`'s exemption-netting (it assumes today's exemption
    RULES persist into the reform, correct for "same methodology, land revalued"), not of this
    function; it applies to any override, not just this one.

    Parameters
    ----------
    gdf : DataFrame
        Needs `land_col` and `market_value_col`.
    land_col : str
        The (possibly revised) GROSS land value column -- e.g. `lycd_land_value`, already
        capped at `market_value` by `compute_lycd_land_values`'s default plain cap, for
        NON-ABATED rows only (see above -- exclude abated rows before calling this). **This
        function corrects BUILDING only; it assumes land is already <= market_value and does
        not enforce that itself.** If land exceeds market_value (an uncapped land column, or a
        per-row ceiling smaller than the land value used), the residual floors at 0 but land
        alone still exceeds market_value, so the total can still overshoot -- the
        `.clip(lower=0)` here is a floor for the ordinary case, not a substitute for the land
        cap.

    Returns
    -------
    pandas.Series
        Gross (pre-exemption) building value, aligned to `gdf.index`, for use as
        `carry_forward_exemptions`'s `gross_building_override` on the non-abated subset.
    """
    import numpy as np
    import pandas as pd

    market = pd.to_numeric(gdf[market_value_col], errors="coerce").fillna(0.0).clip(lower=0)
    land = pd.to_numeric(gdf[land_col], errors="coerce").fillna(0.0).clip(lower=0)
    return (market - land).clip(lower=0)


@dataclass(frozen=True)
class LandReallocation:
    """Output of `reallocate_land_within_total`. All Series are aligned to the input index."""
    alloc_land: "pd.Series"              # gross (pre-exemption) land after reallocation
    alloc_building: "pd.Series"          # gross building = OPA's total - alloc_land
    alloc_taxable_land: "pd.Series"
    alloc_taxable_building: "pd.Series"
    alloc_taxable_total: "pd.Series"
    reconstructed_taxable_total: "pd.Series"   # the SAME rule applied to OPA's own land
    exemption_kind: "pd.Series"          # none | homestead | building_share | total_share | full
    institutional_exempt: "pd.Series"
    diagnostics: dict

    @property
    def reform_change(self) -> "pd.Series":
        """The reform's own effect on taxable value, free of the rule's reconstruction error.

        `alloc_taxable_total - reconstructed_taxable_total`, i.e. the same exemption rule applied
        to the new land component minus that rule applied to OPA's own land. Any error in the
        rule's description of a parcel's exemptions is present in both terms and cancels exactly.
        Differencing against OPA's *recorded* taxable total instead would report the rule's
        residual (0.5% of parcels on TY2026, where OPA's `homestead_exemption` column disagrees
        with the relief actually applied that vintage) as though the reform had moved those bills.
        """
        return self.alloc_taxable_total - self.reconstructed_taxable_total

    def describe(self) -> str:
        d = self.diagnostics
        return (
            f"land reallocated within OPA's total: {d['n_taxable_changed']:,} parcels' taxable "
            f"total moves (of {d['n_parcels']:,}) | exemption kinds: "
            + ", ".join(f"{k}={v:,}" for k, v in d["exemption_kind_counts"].items())
            + f" | land capped at total on {d['n_land_capped']:,} | "
            f"OPA-base reconstruction match: {d['reconstruction_match_rate']:.2%} "
            f"({d['n_reconstruction_mismatch']:,} parcels the rule cannot reproduce today, "
            "excluded from the change count by differencing against the rule itself)"
        )


def reallocate_land_within_total(
    gdf,
    *,
    new_land_col: str,
    homestead_cap: float,
    homestead_col: str = "homestead_exemption",
    match_tolerance: float = 1.0,
    reconstruction_floor: float = 0.95,
    homestead_order: str = "building_first",
    homestead_rate_ratio: "float | None" = None,
    total_value_relief=None,
    relief_order: str = "building_first",
) -> LandReallocation:
    """Re-split OPA's own total assessment into a new land component and a residual building.

    This is the *assessment-standards* reform, not a revaluation. A draft Philadelphia ordinance
    amending Phila. Code Sec. 2-305 would require the land component to approximate the market
    value of the land as if vacant, and would forbid deriving it as a fixed percentage of total
    assessed value -- while explicitly leaving total value, the tax rate, and every unabated
    parcel's liability alone (its Sec. 3(c) makes land and improvement "complementary parts of a
    single assessment at actual value"). That is what this function models:

        alloc_land     = min(new_land, gross_total)      # gross_total = OPA's own land + building
        alloc_building = gross_total - alloc_land

    so `alloc_land + alloc_building == gross_total` by construction. Contrast
    `carry_forward_exemptions`, which holds OPA's *building* fixed and lets the total rise with a
    larger land estimate -- the revaluation reading, where every bill moves and the rate must be
    rolled back.

    **Who actually pays differently.** Because the total is unchanged and the rate is unchanged, a
    parcel's bill can only move if its exemption depends on the land/building *split*. Three cases:

    - **No exemption, or homestead only** -- bill unchanged. The Homestead Exemption is a flat
      `min(cap, total)`; the total did not move, so neither does the exemption, only which line it
      is drawn against. This is the overwhelming majority of parcels and the reason the ordinance
      can be described as revenue-neutral for unabated property.
    - **Non-homestead dollars that fit inside the building line** (`building_share`) -- the 10-year
      construction abatement above all, which exempts the assessed value of the *improvement*.
      Carried as a SHARE of the building line (`other_exempt / gross_building`), re-applied to
      `alloc_building`. A fully abated parcel exempts 100% of whatever the improvement is now
      assessed at, so its taxable value becomes exactly `alloc_land`: raise the land component and
      an abated parcel pays more, lower it and it pays less. This is the fiscal effect the
      ordinance's own drafting notes ask a fiscal note to model in both directions.
    - **Non-homestead dollars that are relief against total value** (`total_share`) -- partial
      institutional relief. Carried in unchanged dollars; the total is unchanged, so these bills
      do not move either. Two shapes qualify: dollars exceeding the building line, and dollars
      that fit inside it but are split pro-rata across both lines (measured on TY2026: 345
      parcels exempt land and building in exactly the parcel's own land/building ratio, which is
      a percentage of total value wearing an abatement's clothing).

    Fully exempt parcels whose exemption the homestead cannot explain (`full`) stay fully exempt.

    **Under a split rate the homestead's line matters.** Everything above holds at one rate. Once a
    caller taxes `alloc_taxable_land` and `alloc_taxable_building` at different rates, which line
    the homestead comes off decides what it is worth, and the statutory building-first order makes
    it worth `cap x building rate`. `homestead_order` selects the rule (see `_apply_exemptions`);
    the taxable total, `reform_change` and every count here are the same under all of them, and
    only the split between the two taxable lines differs.

    **Read the effect off `reform_change`, not off OPA's recorded taxable total.** The exemption
    rule reproduces OPA's own bill on 99.5% of TY2026 parcels, not all of them -- OPA's
    `homestead_exemption` column sometimes records the statutory cap while a smaller amount was
    actually applied. Differencing the reform against OPA's recorded total would report that
    residual as a tax change: measured on TY2026, 1,818 homestead parcels that this reform cannot
    touch (their exemption does not depend on the split) would appear to move. Differencing
    against the rule's own reconstruction cancels it exactly.

    **Vacant parcels never move**, whatever LYCD says about them: their building line is already
    zero, so `alloc_land` is capped at the total and the split is unchanged. The vacant leg of
    LYCD -- the one defect `docs/VACANT_LAND_VALUATION.md` shows is empirically wrong, and the one
    that dominates the revaluation reading -- is therefore irrelevant here. That is a real property
    of the reform, not a modeling convenience.

    Guard: feeding OPA's own gross land back through this rule must reproduce OPA's own taxable
    total per parcel (`reconstruction_floor`), the same reconstruction `carry_forward_exemptions`
    enforces. It holds exactly because `alloc_land = gross_land` implies `alloc_building =
    gross_building` implies `share * alloc_building = other_exempt`.

    Parameters
    ----------
    gdf : DataFrame
        Needs `taxable_land`, `taxable_building`, `exempt_land`, `exempt_building`,
        `homestead_col` and `new_land_col`.
    new_land_col : str
        The proposed GROSS land component, e.g. `lycd_land_value`. Capped at the parcel's own
        total here, so an uncapped column is safe to pass.
    homestead_cap : float
        The tax year's statutory Homestead Exemption, from `tax_year_params(year)`.
    homestead_order : str, default "building_first"
        Which line the homestead is drawn against in the reform: "building_first" (53 Pa.C.S.
        Sec. 8583(c), current law), "land_first", "value_share" or "tax_share". The guard always
        uses the statutory order, because it checks the rule against OPA's records.
    homestead_rate_ratio : float, optional
        The land/building rate ratio; required for "tax_share".
    total_value_relief : boolean array-like, optional
        Parcels whose non-homestead relief is relief against total value even though its
        dollars fit inside the building line -- e.g. the long-running, assessment-cap-like relief
        the abatement classification labels `non_abatement_relief`. Forced to
        `total_share`, so the relief is carried in dollars rather than as a share of a building
        line that shrinks when the land component rises.
    relief_order : str, default "building_first"
        Which line `total_share` dollars come off in the reform: "building_first" (how OPA
        records them, and the guard's rule) or "value_share" (in proportion to the two lines'
        values, the treatment `homestead_order="value_share"` gives the homestead). Under a
        split rate the building-first order taxes a relieved parcel's re-estimated land at the
        land rate while its relief is spent on the building; how such relief should split is not
        set by statute, like the homestead's.

    Returns
    -------
    LandReallocation
    """
    import numpy as np
    import pandas as pd

    _check_homestead_order(homestead_order, homestead_rate_ratio)
    if relief_order not in ("building_first", "value_share"):
        raise ValueError(f"relief_order must be 'building_first' or 'value_share', not {relief_order!r}")
    p = _decompose_exemptions(gdf, homestead_col, match_tolerance)
    gross_total = p.gross_land + p.gross_building
    forced_total = pd.Series(
        np.zeros(len(gdf), bool) if total_value_relief is None else np.asarray(total_value_relief, bool),
        index=p.gross_land.index)

    # An abatement exempts the improvement, so it carries as a share of the building line; a
    # partial institutional exemption is a share of total value, so it carries in dollars.
    #
    # Two signals separate them, and BOTH are needed. The dollars must fit inside the building
    # line -- but that alone misclassifies pro-rata relief: measured on TY2026, 345 parcels split
    # their exemption across land and building in exactly the parcel's own land/building ratio
    # (a percentage of total value), while still fitting inside the building line. So the second
    # test asks whether any non-homestead exemption actually reached OPA's `exempt_land`. Under
    # this rule's application order (dollar exemptions off building first, then the homestead,
    # then the remainder onto land), the only land exemption an abated parcel can show is
    # homestead spill; anything beyond that means the relief is against total value.
    hs_spill = pd.Series(
        np.where(p.homestead_active,
                 (p.homestead_amount - (p.gross_building - p.other_exempt).clip(lower=0)).clip(lower=0),
                 0.0),
        index=p.gross_land.index,
    )
    non_homestead_exempt_land = (p.exempt_land - hs_spill).clip(lower=0)
    is_building_share = (
        (p.other_exempt > match_tolerance)
        & (p.other_exempt <= p.gross_building + match_tolerance)
        & (non_homestead_exempt_land <= match_tolerance)
        & ~forced_total
    )
    share = pd.Series(
        np.where(p.gross_building > 0, p.other_exempt / p.gross_building.where(p.gross_building > 0, 1.0), 0.0),
        index=p.gross_land.index,
    ).clip(lower=0, upper=1)

    def _exempt_dollars(alloc_building):
        """Non-homestead exemption dollars against a given building line."""
        return pd.Series(np.where(is_building_share, share * alloc_building, p.other_exempt),
                         index=p.gross_land.index).clip(lower=0)

    # Guard: OPA's own land through this rule must reproduce OPA's own taxable total.
    rec_land, rec_bldg = _apply_exemptions(
        p.gross_land, p.gross_building, _exempt_dollars(p.gross_building), p, homestead_cap)
    match_rate, mismatch_dollars = _reconstruction_guard(
        p, rec_land, rec_bldg, match_tolerance, reconstruction_floor)

    proposed = pd.to_numeric(gdf[new_land_col], errors="coerce").fillna(0.0).clip(lower=0)
    alloc_land = np.minimum(proposed, gross_total)
    alloc_building = (gross_total - alloc_land).clip(lower=0)
    is_total_share = (p.other_exempt > match_tolerance) & ~is_building_share & ~p.institutional
    new_land_out, new_bldg_out = _apply_exemptions(
        alloc_land, alloc_building, _exempt_dollars(alloc_building), p, homestead_cap,
        homestead_order=homestead_order, rate_ratio=homestead_rate_ratio,
        value_share_relief=is_total_share if relief_order == "value_share" else None)
    new_total = new_land_out + new_bldg_out

    kind = pd.Series("none", index=p.gross_land.index, dtype=object)
    kind[p.homestead_active] = "homestead"
    kind[is_building_share] = "building_share"
    kind[(p.other_exempt > match_tolerance) & ~is_building_share] = "total_share"
    kind[p.institutional] = "full"

    # The reform's effect is measured against the rule's own reconstruction, not against OPA's
    # recorded taxable total, so the rule's residual error cancels instead of masquerading as a
    # tax change -- see LandReallocation.reform_change.
    rec_total = rec_land + rec_bldg
    changed = (new_total - rec_total).abs() > match_tolerance
    diagnostics = {
        "n_parcels": int(len(gdf)),
        "n_taxable_changed": int(changed.sum()),
        "n_taxable_changed_vs_recorded": int((new_total - p.old_taxable).abs().gt(match_tolerance).sum()),
        "n_reconstruction_mismatch": int((rec_total - p.old_taxable).abs().gt(match_tolerance).sum()),
        "n_land_capped": int((proposed > gross_total + match_tolerance).sum()),
        "n_building_share": int((kind == "building_share").sum()),
        "n_total_share": int((kind == "total_share").sum()),
        "n_institutional_exempt": int(p.institutional.sum()),
        "n_homestead_active": int(p.homestead_active.sum()),
        # Pro-rata relief that fits inside the building line but reaches land: the population the
        # exempt_land test above exists to keep out of building_share.
        "n_prorata_reclassified": int(
            ((p.other_exempt > match_tolerance)
             & (p.other_exempt <= p.gross_building + match_tolerance)
             & (non_homestead_exempt_land > match_tolerance)).sum()),
        "exemption_kind_counts": kind.value_counts().to_dict(),
        "taxable_change_dollars": float((new_total - rec_total).sum()),
        "reconstruction_match_rate": match_rate,
        "reconstruction_mismatch_dollars": mismatch_dollars,
        "homestead_cap": float(homestead_cap),
        "homestead_order": homestead_order,
        "relief_order": relief_order,
        "n_total_value_relief_forced": int((forced_total & (p.other_exempt > match_tolerance)).sum()),
    }
    return LandReallocation(
        alloc_land, alloc_building, new_land_out, new_bldg_out, new_total, rec_total, kind,
        p.institutional, diagnostics,
    )


ACRE_SQFT = 43_560.0
LARGE_LOT_BANDS = ("edge to 1 acre", "1 acre and over")   # philly_open_avmkit's band names


def large_lot_band_factor(area, band_factors: dict, edge_sqft: float, acre_sqft: float = ACRE_SQFT):
    """A surface's held-out relevel factor for each lot, by lot-size band beyond the support edge.

    Bands as philly_open_avmkit's `check_large_lot_relevel.py` cuts them: `edge < area <= acre` is
    "edge to 1 acre", `area > acre` is "1 acre and over". `band_factors` maps those names to the
    surface's median ratio to price on held-out sales in the band. Returns a float array, NaN at or
    inside the edge (and where the area is missing), so a caller cannot mistake "no factor" for 1.
    """
    import numpy as np

    missing = [b for b in LARGE_LOT_BANDS if b not in band_factors]
    if missing:
        raise KeyError(f"band_factors lacks {missing}; it has {sorted(band_factors)}")
    a = np.asarray(area, dtype=float)
    out = np.full(a.shape, np.nan)
    out[(a > edge_sqft) & (a <= acre_sqft)] = float(band_factors[LARGE_LOT_BANDS[0]])
    out[a > acre_sqft] = float(band_factors[LARGE_LOT_BANDS[1]])
    return out


@dataclass(frozen=True)
class LargeTractRelevel:
    """Output of `relevel_beyond_support`. Series aligned to the input frame's index."""
    land: "pd.Series"         # the land column for the re-split: relevelled beyond the edge, else unchanged
    relevelled: "pd.Series"   # which parcels were relevelled
    diagnostics: dict


def relevel_beyond_support(frame, *, land_col: str, uncarried_col: str, beyond_support,
                           relevel_factor) -> LargeTractRelevel:
    """The land a sales surface supports on lots larger than its sales can test: its own value there,
    divided by its measured error on the large lots that did sell.

    A surface's rate past its support edge is an extrapolation from comparables a tenth the size or
    smaller, and on the held-out sales beyond the edge it errs by a measurable amount in each lot-size
    band (philly_open_avmkit measures S5 at about 1.09x price from the edge to an acre and 1.83x
    above). Dividing by that factor puts the surface on level there without replacing it: the value
    is still the surface's own comparables, rescaled, which is what `large_tracts="relevelled"` in
    `uncap_bare_land` then takes whole on a bare lot.

    The factor applies to the RATE, before any cap: `uncarried_col` is the surface's own value (rate
    x lot area, never capped at the parcel's total, never replaced by OPA's), and the re-split
    (`reallocate_land_within_total`) caps the result afterwards on a parcel with a building. Dividing
    an already-capped value instead would take a parcel whose relevelled land still exceeds its total
    below the total, understating it.

    Parcels not flagged `beyond_support` keep `land_col` unchanged. A flagged parcel with no usable
    factor (NaN or <= 0) or no uncarried value raises, naming the count: a gap in the factors must not
    silently fall back to the extrapolated value.
    """
    import numpy as np
    import pandas as pd

    beyond = np.asarray(beyond_support, dtype=bool)
    factor = np.asarray(relevel_factor, dtype=float)
    base = pd.to_numeric(frame[land_col], errors="coerce").to_numpy(dtype=float)
    uncarried = pd.to_numeric(frame[uncarried_col], errors="coerce").to_numpy(dtype=float)
    if not (len(beyond) == len(factor) == len(frame)):
        raise ValueError("beyond_support and relevel_factor must align with frame")
    no_factor = beyond & ~(np.isfinite(factor) & (factor > 0))
    if no_factor.any():
        raise ValueError(f"{int(no_factor.sum()):,} parcels beyond the support edge have no relevel factor")
    no_value = beyond & ~np.isfinite(uncarried)
    if no_value.any():
        raise ValueError(f"{int(no_value.sum()):,} parcels beyond the support edge have no {uncarried_col}")
    land = base.copy()
    land[beyond] = np.clip(uncarried[beyond], 0, None) / factor[beyond]
    factors, counts = np.unique(np.round(factor[beyond], 6), return_counts=True)
    diagnostics = {
        "n_beyond_support": int(beyond.sum()),
        "land_col_beyond": float(np.nansum(base[beyond])),
        "uncarried_beyond": float(uncarried[beyond].sum()),
        "relevelled_beyond": float(land[beyond].sum()),
        "parcels_by_factor": {float(f): int(n) for f, n in zip(factors, counts)},
    }
    return LargeTractRelevel(pd.Series(land, index=frame.index), pd.Series(beyond, index=frame.index), diagnostics)


# The headline reading first: the one-pager and its walkthrough list the readings in this order.
LARGE_TRACT_TREATMENTS = ("relevelled", "carry_opa", "opa_relevelled", "surface")


def uncap_bare_land(alloc: LandReallocation, frame, taxable, new_land_col: str = "s5_land", *,
                    beyond_support=None, large_tracts: str = "carry_opa", uncarried_land=None,
                    opa_level: "float | None" = None, keep_opa=None):
    """Take the sales-based land value whole on parcels that carry no building -- where the
    sales can speak to a lot that size.

    `reallocate_land_within_total` caps land at the parcel's own total, which for a bare lot IS
    its land: the re-split can then never assess such a lot above what OPA already says, and where
    the sales estimate is LOWER it books the shortfall as `alloc_building` -- a building line on a
    lot with no building. Under the flat rate that function models this is harmless (the two lines
    sum to the same total, so the bill is identical, which is the sense in which its docstring says
    vacant parcels never move). Stacking a split rate on top is what makes it bite: measured on
    TY2026, 14,831 of 30,564 taxable vacant lots would have a phantom $0.82B taxed at the building
    rate, and the other half would have their under-assessment frozen in place.

    So the cap is lifted exactly where there is no improvement to hold a total fixed against. For a
    bare lot "hold the total fixed" and "value the land at what it sells for" are the same
    instruction, and a total that disagrees with the sales evidence is an assessment error, not a
    constraint. Nothing with a building on it is touched, and no building is ever revalued.

    The parcel's own exemption is carried in dollars (144 bare parcels have one, nearly all partial
    institutional relief, which is relief against total value and so does not follow the split).

    "What it sells for" is known only for lots like the ones that sold. The surface publishes the
    lot size past which its held-out sales stop showing it to be better than OPA
    (`land_beyond_support` in the export, from philly_open_avmkit's `support_edge.csv`); its rate
    there is an extrapolation from comparables a tenth the size or smaller, and on a bare lot
    nothing bounds it. `beyond_support` is that flag as a boolean array, and `large_tracts` says
    what a flagged bare lot is worth:

    - ``relevelled``: `new_land_col` taken whole, where the caller has built that column with
      `relevel_beyond_support` (the surface divided by its measured error in the lot's size band),
      except the tracts `keep_opa` marks, which keep OPA's value: land whose use is restricted (a
      rail corridor, a port terminal, parkland), where a market-rate estimate prices a use the lot
      cannot have. The Council one-pager's reading, and the same column it gives built parcels.
    - ``carry_opa`` (the library default, so a caller that names no treatment keeps the old
      behaviour): OPA's own value, so the lot's bill moves with the rate alone. OPA runs low on the
      large tracts that do sell.
    - ``opa_relevelled``: OPA's value divided by `opa_level`, its measured median ratio to price
      on still-vacant sales beyond the edge.
    - ``surface``: `uncarried_land`, the extrapolated rate taken whole, which runs high.

    With `beyond_support=None` every bare lot takes `new_land_col` whole, for a surface that
    publishes no edge.

    `alloc` is the reallocation of `frame` on `new_land_col`; `taxable` marks the parcels that pay
    tax at all. Returns the taxable land and building lines with bare lots re-valued, and the
    bare-lot mask. The one-pager and the abatement phase-in both use this, so the two describe one
    reform.
    """
    import numpy as np

    if large_tracts not in LARGE_TRACT_TREATMENTS:
        raise ValueError(f"large_tracts must be one of {LARGE_TRACT_TREATMENTS}, got {large_tracts!r}")
    land = alloc.alloc_taxable_land.to_numpy().copy()
    building = alloc.alloc_taxable_building.to_numpy().copy()
    gross_total = (frame.taxable_land + frame.exempt_land + frame.taxable_building + frame.exempt_building).to_numpy()
    bare = taxable & ((frame.taxable_building + frame.exempt_building).to_numpy() <= 1.0)
    exempt_dollars = (gross_total - alloc.reconstructed_taxable_total.to_numpy()).clip(min=0)
    whole = frame[new_land_col].to_numpy(dtype=float).copy()
    if beyond_support is not None:
        beyond = bare & np.asarray(beyond_support, dtype=bool)
        if large_tracts == "relevelled":
            if keep_opa is None:
                raise ValueError("large_tracts='relevelled' needs keep_opa, the use-restricted tracts that "
                                 "keep OPA's value (all False if there are none)")
            kept = beyond & np.asarray(keep_opa, dtype=bool)
            whole[kept] = gross_total[kept]
        elif large_tracts == "carry_opa":
            whole[beyond] = gross_total[beyond]
        elif large_tracts == "opa_relevelled":
            if not opa_level or opa_level <= 0:
                raise ValueError("large_tracts='opa_relevelled' needs opa_level, OPA's measured median ratio")
            whole[beyond] = gross_total[beyond] / float(opa_level)
        else:
            if uncarried_land is None:
                raise ValueError("large_tracts='surface' needs uncarried_land, the surface's own value")
            whole[beyond] = np.asarray(uncarried_land, dtype=float)[beyond]
    land[bare] = (whole[bare] - exempt_dollars[bare]).clip(min=0)
    building[bare] = 0.0
    return land, building, bare


# --- Commercial property: building type and zoning family -------------------------------------
# Neither field is in the parcel cache; `analysis/ownership/philadelphia/fetch_opa_attributes.py`
# pulls `building_code`, `building_code_description` and `zoning` from OPA as a sidecar.
#
# Parking is read from the CODE, and these three are the repo's one definition of it (the
# ownership analysis imports them). Its descriptions collide with two things that are not
# parking lots: `5R` CONDO PARKING SPACE, a deeded space inside a garage, and R10/R30/R5x
# ROW B/GAR rowhouses, which a match on `R*` alone would sweep in.
SURFACE_PARKING_CODES = frozenset({"RA", "RB", "RE"})           # RA non-commercial; RB unpaid, RE paid commercial
SURFACE_PARKING_WITH_STRUCTURE_RE = re.compile(r"^R[A-F]\d$")   # a lot with a booth or building: RC0, RD6, RF0
PARKING_GARAGE_RE = re.compile(r"^O[AB]\d$")                    # GAR W/COMM AREA (OA*), GAR NO COMM AREA (OB*)

# Everything else is read from the DESCRIPTION: OPA's codes run two schemes (commercial `LC0`
# warehouse, residential-style `O30` rowhouse), and the description is what a reviewer can check.
# First match wins, so the order matters: condos before industrial (INDUS CONDO), storefronts
# before plain rowhouses (ROW W/OFF STR).
COMMERCIAL_BUILDING_TYPES = (
    ("Condominium units", r"^(?:COM CONDO|INDUS CONDO|CONDO )"),
    ("Land coded vacant, air rights", r"^(?:VACANT LAND|AIR RIGHTS)"),
    ("Offices and banks", r"^(?:OFF BLD|BANK/OFF|MISC RESEARCH)"),
    ("Hotels", r"^(?:HOTEL|MOTEL)"),
    ("Shopping centres and supermarkets", r"^(?:SHOP CENT|SUPERMKT|DEPT)"),
    ("Gas stations and auto businesses", r"^(?:GAS STAT|AUTO )"),
    ("Restaurants and bars", r"^(?:REST'?RNT|TAVERN|FAST ?FOOD|DINER)"),
    ("Warehouses, factories, other industrial", r"^(?:IND\b|INDUS|COLD STORAGE|ASSEMBLY PLANT|PUB UTIL)"),
    ("Storefronts with offices or apartments", r"^(?:STR/OFF|ROW W/OFF|ROW B/OFF|S/D OFF|DET OFF)"),
    ("Stores and services", r"^(?:STORE|CLEANING|LAUNDR)"),
    ("Health, schools, worship, entertainment",
     r"^(?:HEALTH FAC|SCHOOL|HSE WORSHIP|AMUSE?\b|MISC DAY CARE|MISC MUSEUM|MISC FUNERAL|CEMETERY)"),
    ("Houses and apartments in commercial categories", r"^(?:ROW|APT|S/D|DET|TWIN)"),
)
COMMERCIAL_OTHER = "Other"


def commercial_building_type(description, code) -> "pd.Series":
    """Group OPA building descriptions into the types a Council staffer would recognise.

    `description` and `code` are aligned Series of OPA's `building_code_description` and
    `building_code`. Parking comes from the code (see the comment above), everything else from the
    description by `COMMERCIAL_BUILDING_TYPES`; anything unmatched is "Other", and a caller that
    reports these groups should check how much tax lands there.
    """
    import numpy as np
    import pandas as pd

    desc = description.fillna("").astype(str).str.strip().str.upper()
    c = code.fillna("").astype(str).str.strip().str.upper()
    garage = c.str.match(PARKING_GARAGE_RE.pattern)
    lot = c.isin(SURFACE_PARKING_CODES) | c.str.match(SURFACE_PARKING_WITH_STRUCTURE_RE.pattern)
    conditions = [garage.to_numpy(), lot.to_numpy()] + [desc.str.contains(p, regex=True).to_numpy()
                                                        for _, p in COMMERCIAL_BUILDING_TYPES]
    labels = ["Parking garages", "Parking lots"] + [label for label, _ in COMMERCIAL_BUILDING_TYPES]
    return pd.Series(np.select(conditions, labels, default=COMMERCIAL_OTHER), index=description.index)


def zoning_family(zoning) -> "pd.Series":
    """Group Philadelphia zoning codes by the Zoning Code's own district families.

    OPA writes codes without the hyphen (`CMX5`, `CA1`, `SPPOA`), so they are normalised first.
    """
    import numpy as np
    import pandas as pd

    z = zoning.fillna("").astype(str).str.upper().str.replace(r"[\s-]", "", regex=True)
    families = [
        ("CMX-5 Center City core", z == "CMX5"),
        ("CMX-4 Center City commercial", z == "CMX4"),
        ("CMX-3 community commercial", z == "CMX3"),
        ("CMX-1/2/2.5 neighborhood commercial", z.isin(["CMX1", "CMX2", "CMX2.5"])),
        ("CA-1/2 auto-oriented commercial", z.isin(["CA1", "CA2"])),
        ("Industrial (I-1/2/3, I-P, ICMX, IRMX)", z.isin(["I1", "I2", "I3", "IP", "ICMX", "IRMX"])),
        ("Special purpose (SP)", z.str.startswith("SP")),
        ("Residential zoning", z.str.startswith("R")),
    ]
    return pd.Series(np.select([m.to_numpy() for _, m in families], [f for f, _ in families], default="Unmatched"),
                     index=zoning.index)
