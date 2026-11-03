"""Waterfront proximity analysis for LVT models.

Shoreline proximity is one of the largest single drivers of residential land
value. In a city with meaningful water frontage, lumping waterfront homes in
with inland homes muddies the per-category LVT signal: the "Single Family
Residential" median hides the fact that a small band of high-land-value
shoreline parcels behaves very differently from the interior.

This module provides a small, reusable pipeline:

1. ``fetch_tiger_water`` — pull the Census TIGER/Line AREAWATER (and optionally
   LINEARWATER) features for a county/county-equivalent FIPS, cached locally.
2. ``add_waterfront_flags`` — compute each parcel's distance to the nearest
   water feature and add ``water_dist_m`` plus one boolean column per threshold
   (e.g. ``waterfront_30m``, ``waterfront_100m``).
3. ``split_single_family_by_waterfront`` — suffix the single-family category
   (e.g. ``"Single Family Residential — Waterfront"``) for flagged parcels so
   the split flows through the standard category summary, report, and export
   unchanged.

Whether to apply the split is a per-city judgment: run ``waterfront_share`` and
only split when a decent chunk of the single-family stock is actually on the
water. See ``.claude/skills/model-policy.md`` for the decision rule.
"""

import os
from pathlib import Path
from typing import Iterable, List, Optional, Tuple, Union

import geopandas as gpd
import pandas as pd

# TIGER/Line AREAWATER MTFCC codes worth treating as "waterfront-making" water.
# H2051 ocean/bay/estuary/gulf/sea, H2053 sea/ocean, H2030 lake/pond,
# H2040 reservoir, H2081 glacier (rare), H3010/H3013 stream/river,
# H3020 canal/ditch/aqueduct. Streams are included but usually screened out by
# ``min_water_area_m2`` since drainage ditches should not flag a home.
_DEFAULT_TIGER_YEAR = 2023


def fetch_tiger_water(
    fips_code: str,
    year: int = _DEFAULT_TIGER_YEAR,
    cache_dir: Optional[Union[str, Path]] = None,
    include_linear: bool = False,
    min_water_area_m2: float = 0.0,
    mtfcc_keep: Optional[Iterable[str]] = None,
) -> gpd.GeoDataFrame:
    """Fetch TIGER/Line water features for a county (or county-equivalent).

    Downloads the AREAWATER shapefile (and optionally LINEARWATER) for the
    given 5-digit FIPS from the Census bulk-data site, caching the ``.zip`` so
    repeat runs are offline. Independent cities (e.g. Newport News, FIPS
    ``51700``) are county-equivalents in TIGER and resolve correctly.

    Parameters
    ----------
    fips_code : str
        5-digit state+county FIPS (e.g. ``"55025"`` Dane County, WI;
        ``"51700"`` Newport News city, VA).
    year : int
        TIGER vintage. Defaults to 2023.
    cache_dir : str or Path, optional
        Directory to cache downloaded zips. Defaults to ``./tiger_water_cache``.
        In a city notebook, pass ``DATA_DIR`` so the cache lives with the city.
    include_linear : bool
        Also load LINEARWATER (streams/rivers as lines). Off by default; most
        "waterfront" value comes from polygon water (lakes, bays, wide rivers).
    min_water_area_m2 : float
        Drop AREAWATER polygons smaller than this (measured in an equal-area
        projection). Use to screen out retention ponds and drainage features
        that should not flag a home as waterfront. ``0.0`` keeps everything.
    mtfcc_keep : iterable of str, optional
        If given, keep only features whose ``MTFCC`` is in this set.

    Returns
    -------
    GeoDataFrame
        Water features in EPSG:4326 with at least ``MTFCC`` and geometry.

    Raises
    ------
    ValueError
        If ``fips_code`` is not a 5-digit string.
    """
    if not isinstance(fips_code, str) or len(fips_code) != 5 or not fips_code.isdigit():
        raise ValueError("fips_code must be a 5-digit string (state+county)")

    cache_dir = Path(cache_dir) if cache_dir is not None else Path("tiger_water_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)

    layers = ["AREAWATER"] + (["LINEARWATER"] if include_linear else [])
    frames: List[gpd.GeoDataFrame] = []
    for layer in layers:
        zip_name = f"tl_{year}_{fips_code}_{layer.lower()}.zip"
        cache_path = cache_dir / zip_name
        if not cache_path.exists():
            url = (
                f"https://www2.census.gov/geo/tiger/TIGER{year}/"
                f"{layer}/{zip_name}"
            )
            _download(url, cache_path)
        gdf = gpd.read_file(cache_path)
        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326")
        else:
            gdf = gdf.to_crs("EPSG:4326")
        gdf["_layer"] = layer
        frames.append(gdf)

    water = pd.concat(frames, ignore_index=True)
    water = gpd.GeoDataFrame(water, geometry="geometry", crs="EPSG:4326")
    water = water[~water.geometry.is_empty & water.geometry.notna()]

    if mtfcc_keep is not None and "MTFCC" in water.columns:
        water = water[water["MTFCC"].isin(set(mtfcc_keep))]

    if min_water_area_m2 > 0 and len(water) > 0:
        # Measure area in an equal-area projection; keep all linear features.
        eq_area = water.to_crs("EPSG:6933")
        is_line = water.get("_layer", "AREAWATER").eq("LINEARWATER")
        keep = is_line | (eq_area.geometry.area >= min_water_area_m2)
        water = water[keep.values]

    return water.reset_index(drop=True)


def add_waterfront_flags(
    gdf: gpd.GeoDataFrame,
    fips_code: Optional[str] = None,
    water_gdf: Optional[gpd.GeoDataFrame] = None,
    thresholds_m: Tuple[int, ...] = (30, 100),
    dist_col: str = "water_dist_m",
    projected_crs: Optional[str] = None,
    **fetch_kwargs,
) -> gpd.GeoDataFrame:
    """Add distance-to-water and waterfront threshold flags to parcels.

    Computes, per parcel, the distance in meters to the nearest water feature,
    then adds one boolean column per entry in ``thresholds_m`` named
    ``waterfront_<t>m``. Distances are measured in a projected CRS (a local UTM
    zone by default) so the meters are accurate.

    Provide either ``fips_code`` (to fetch TIGER water) or a pre-loaded
    ``water_gdf`` (e.g. for testing or a custom hydrography source).

    Parameters
    ----------
    gdf : GeoDataFrame
        Parcels with a valid geometry column.
    fips_code : str, optional
        5-digit FIPS; used to fetch TIGER water when ``water_gdf`` is None.
    water_gdf : GeoDataFrame, optional
        Pre-loaded water features. Takes precedence over ``fips_code``.
    thresholds_m : tuple of int
        Distance cutoffs (meters) for the boolean flags. Default ``(30, 100)``.
    dist_col : str
        Name for the continuous distance column. Default ``"water_dist_m"``.
    projected_crs : str, optional
        CRS to project into for distance math. Defaults to the parcels'
        estimated local UTM zone (accurate meters). Web Mercator (3857) is
        intentionally NOT the default — its distances are distorted.
    **fetch_kwargs
        Forwarded to ``fetch_tiger_water`` (e.g. ``cache_dir``, ``year``,
        ``include_linear``, ``min_water_area_m2``).

    Returns
    -------
    GeoDataFrame
        A copy of ``gdf`` with ``dist_col`` and the ``waterfront_<t>m`` columns
        added. The original geometry/CRS is preserved.

    Raises
    ------
    ValueError
        If neither ``fips_code`` nor ``water_gdf`` is provided, or ``gdf`` has
        no CRS set.
    """
    if water_gdf is None and fips_code is None:
        raise ValueError("Provide either fips_code or water_gdf")
    if not isinstance(gdf, gpd.GeoDataFrame) or gdf.crs is None:
        raise ValueError("gdf must be a GeoDataFrame with a CRS set")

    if water_gdf is None:
        water_gdf = fetch_tiger_water(fips_code, **fetch_kwargs)

    if len(water_gdf) == 0:
        # No water in the county — nothing is waterfront.
        out = gdf.copy()
        out[dist_col] = float("inf")
        for t in thresholds_m:
            out[f"waterfront_{t}m"] = False
        return out

    crs = projected_crs or gdf.estimate_utm_crs()
    parcels_proj = gdf.to_crs(crs)
    water_proj = water_gdf.to_crs(crs)[["geometry"]].reset_index(drop=True)

    # Nearest-water distance via STRtree-backed spatial join (scales to 100k+).
    joined = gpd.sjoin_nearest(
        parcels_proj[["geometry"]],
        water_proj,
        how="left",
        distance_col=dist_col,
    )
    # sjoin_nearest can emit duplicate rows on ties — keep the (equal) min.
    dist = joined[dist_col].groupby(level=0).min()

    out = gdf.copy()
    out[dist_col] = dist.reindex(out.index).values
    for t in thresholds_m:
        out[f"waterfront_{t}m"] = out[dist_col] <= t
    return out


def split_single_family_by_waterfront(
    df: pd.DataFrame,
    flag_col: str = "waterfront_100m",
    category_col: str = "property_category",
    sfh_labels: Iterable[str] = ("Single Family Residential",),
    suffix: str = " — Waterfront",
) -> pd.DataFrame:
    """Suffix the single-family category for waterfront parcels.

    Rows whose ``category_col`` is in ``sfh_labels`` AND whose ``flag_col`` is
    truthy get ``suffix`` appended to their category, producing e.g.
    ``"Single Family Residential — Waterfront"``. Everything else is untouched.
    Because it only rewrites the category string, the split flows through
    ``calculate_category_tax_summary``, the city report, and the standard
    export with no other changes.

    Parameters
    ----------
    df : DataFrame
        Parcel frame carrying ``category_col`` and ``flag_col``.
    flag_col : str
        Boolean waterfront flag (e.g. ``"waterfront_100m"``).
    category_col : str
        Category column to rewrite. Default ``"property_category"``.
    sfh_labels : iterable of str
        Category value(s) treated as single-family and eligible for the split.
    suffix : str
        Appended to the category for waterfront single-family parcels.

    Returns
    -------
    DataFrame
        A copy of ``df`` with the split applied.

    Raises
    ------
    KeyError
        If ``flag_col`` or ``category_col`` is absent.
    """
    for col in (flag_col, category_col):
        if col not in df.columns:
            raise KeyError(f"column {col!r} not found in df")

    out = df.copy()
    sfh = set(sfh_labels)
    mask = out[category_col].isin(sfh) & out[flag_col].fillna(False).astype(bool)
    out.loc[mask, category_col] = out.loc[mask, category_col].astype(str) + suffix
    return out


def waterfront_share(
    df: pd.DataFrame,
    flag_col: str = "waterfront_100m",
    category_col: str = "property_category",
    sfh_labels: Iterable[str] = ("Single Family Residential",),
) -> dict:
    """Report how much of the single-family stock is on the water.

    Use the returned numbers to decide whether the waterfront split is worth
    applying for this city (the "decent chunk" gate in model-policy.md).

    Returns
    -------
    dict
        ``sfh_total`` (count of single-family parcels), ``waterfront_count``,
        and ``waterfront_pct`` (share of single-family parcels flagged).
    """
    for col in (flag_col, category_col):
        if col not in df.columns:
            raise KeyError(f"column {col!r} not found in df")

    sfh = df[df[category_col].isin(set(sfh_labels))]
    total = int(len(sfh))
    wf = int(sfh[flag_col].fillna(False).astype(bool).sum())
    pct = (wf / total * 100.0) if total else 0.0
    return {"sfh_total": total, "waterfront_count": wf, "waterfront_pct": pct}


def _download(url: str, dest: Path, timeout: int = 120) -> None:
    """Stream a URL to ``dest`` (used for TIGER zips)."""
    import requests

    resp = requests.get(url, timeout=timeout, stream=True)
    resp.raise_for_status()
    tmp = dest.with_suffix(dest.suffix + ".part")
    with open(tmp, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1 << 16):
            if chunk:
                f.write(chunk)
    os.replace(tmp, dest)
