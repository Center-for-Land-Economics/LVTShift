"""Download DC OTR tax roll (ITSPE), owner polygons, and condo crosswalk from DC GIS."""
import sys
import time
import concurrent.futures as cf
from pathlib import Path
from typing import List, Optional

import requests
import pandas as pd
import geopandas as gpd
from shapely.geometry import shape

BASE = "https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_DATA/Property_and_Land/MapServer"
DATA_DIR = Path(__file__).resolve().parent / "data"

ITSPE_FIELDS = [
    "SSL", "SQUARE", "SUFFIX", "LOT", "PROPTYPE", "TRIGROUP", "USECODE", "LANDAREA", "PREMISEADD",
    "NBHD", "NBHDNAME", "SUBNBHD", "UNITNUMBER", "OWNERNAME", "ADDRESS1", "CITYSTZIP",
    "OLDLAND", "OLDIMPR", "OLDTOTAL", "NEWLAND", "NEWIMPR", "NEWTOTAL", "PHASELAND", "PHASEBUILD",
    "PARTPART", "VACLNDUSE", "DELCODE", "HSTDCODE", "CLASSTYPE", "TAXRATE", "MIXEDUSE",
    "MIX1TXTYPE", "MIX1CLASS", "MIX1RATE", "MIX1LNDPCT", "MIX1LNDVAL", "MIX1BLDPCT", "MIX1BLDVAL",
    "MIX2TXTYPE", "MIX2CLASS", "MIX2RATE", "MIX2LNDPCT", "MIX2LNDVAL", "MIX2BLDPCT", "MIX2BLDVAL",
    "OWNOCCT", "COOPUNITS", "PCHILDCODE", "ABTLOTCODE", "ASSESSMENT", "ANNUALTAX", "EXTRACTDAT",
    "CAPCURR", "CAPPROP", "REASONCD", "CY1YEAR", "CY1TAX", "CY2YEAR", "CY2TAX", "PY1YEAR", "PY1TAX",
    "PRMS_WARD", "PRESSL", "PIPARENTLOT", "OBJECTID",
]


def _get(url: str, params: dict, tries: int = 5) -> dict:
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=120)
            r.raise_for_status()
            d = r.json()
            if "error" in d:
                raise RuntimeError(d["error"])
            return d
        except Exception as e:  # retry on transient failures
            if i == tries - 1:
                raise
            time.sleep(2 ** i)


def fetch_layer(layer_id: int, fields: List[str], geometry: bool = False, page: int = 1000,
                where: str = "1=1") -> pd.DataFrame:
    """Page through a DC GIS MapServer layer/table by resultOffset, in parallel."""
    url = f"{BASE}/{layer_id}/query"
    n = _get(url, {"where": where, "returnCountOnly": "true", "f": "json"})["count"]
    print(f"layer {layer_id}: {n:,} records", flush=True)
    fmt = "geojson" if geometry else "json"

    def one(offset: int) -> list:
        p = {"where": where, "outFields": ",".join(fields), "returnGeometry": str(geometry).lower(),
             "resultOffset": offset, "resultRecordCount": page, "orderByFields": "OBJECTID", "f": fmt}
        if geometry:
            p["outSR"] = 4326
        d = _get(url, p)
        if geometry:
            return [{**f["properties"], "geometry": shape(f["geometry"]) if f.get("geometry") else None}
                    for f in d["features"]]
        return [f["attributes"] for f in d["features"]]

    rows = []
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for i, chunk in enumerate(ex.map(one, range(0, n, page))):
            rows.extend(chunk)
            if i % 25 == 0:
                print(f"  {len(rows):,}/{n:,}", flush=True)
    df = pd.DataFrame(rows)
    print(f"layer {layer_id}: got {len(df):,} (unique OBJECTID {df['OBJECTID'].nunique():,})", flush=True)
    return df


if __name__ == "__main__":
    DATA_DIR.mkdir(exist_ok=True)
    which = sys.argv[1:] or ["itspe", "poly", "condo"]
    if "itspe" in which:
        fetch_layer(53, ITSPE_FIELDS).to_parquet(DATA_DIR / "itspe.parquet")
    if "condo" in which:
        fetch_layer(52, ["SSL", "MAT_SSL", "REGIME", "NAME", "UNITNUM", "OBJECTID"]).to_parquet(
            DATA_DIR / "condorelate.parquet")
    if "poly" in which:
        df = fetch_layer(40, ["SSL", "UNDERLIES_CONDO", "CONDOLOT", "OBJECTID"], geometry=True, page=1000)
        gpd.GeoDataFrame(df, geometry="geometry", crs="EPSG:4326").to_parquet(DATA_DIR / "owner_polygons.gpq")


def fetch_address_points() -> None:
    """Master Address Repository: SSL→MARID crosswalk and address-point coordinates."""
    global BASE
    saved = BASE
    BASE = "https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_DATA/Location_WebMercator/MapServer"
    try:
        fetch_layer(7, ["SSL", "MARID", "OBJECTID"]).to_parquet(DATA_DIR / "address_ssl_xref.parquet")
        fetch_layer(0, ["MAR_ID", "SSL", "LATITUDE", "LONGITUDE", "WARD", "OBJECTID"]).to_parquet(
            DATA_DIR / "address_points.parquet")
    finally:
        BASE = saved


if __name__ == "__main__" and "address" in sys.argv[1:]:
    fetch_address_points()
