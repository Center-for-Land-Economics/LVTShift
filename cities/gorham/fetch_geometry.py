"""Fetch Gorham parcel geometry from the Maine statewide Organized-Towns layer and
cache to cities/gorham/data/parcels.gpq (keyed by normalized MAP_BK_LOT for joining
the CCRA commitment values). The statewide service has no city-attribute shortcut in
lvt.cloud_utils, so we page it directly with a TOWN filter."""
import json, re, time, urllib.parse, urllib.request
from pathlib import Path

import geopandas as gpd

DATA = Path(__file__).resolve().parent / "data"
DATA.mkdir(exist_ok=True)

BASE = "https://services1.arcgis.com/RbMX0mRVOFNTdLzd/arcgis/rest/services/Maine_Parcels_Organized_Towns/FeatureServer"


def norm_maplot(x):
    if x is None:
        return None
    return re.sub(r"\s+", "", str(x)).strip().upper().strip("-")


def esri_to_shapely(g):
    rings = g.get("rings")
    if not rings:
        return None
    from shapely.geometry import Polygon, MultiPolygon
    polys = [Polygon(r) for r in rings if len(r) >= 4]
    if not polys:
        return None
    return polys[0] if len(polys) == 1 else MultiPolygon(polys)


def main():
    rows, geoms = [], []
    off = 0
    while True:
        p = {"where": "TOWN='GORHAM' AND TYPE='Parcel'",
             "outFields": "MAP_BK_LOT,STATE_ID,PROP_LOC,TYPE,COUNTY",
             "returnGeometry": "true", "outSR": 4326,
             "resultOffset": off, "resultRecordCount": 2000, "f": "json"}
        url = BASE + "/10/query?" + urllib.parse.urlencode(p)
        d = json.load(urllib.request.urlopen(url, timeout=180))
        feats = d.get("features", [])
        if not feats:
            break
        for f in feats:
            geom = esri_to_shapely(f.get("geometry") or {})
            if geom is None:
                continue
            a = f["attributes"]
            rows.append(a)
            geoms.append(geom)
        if len(feats) < 2000:
            break
        off += 2000
        time.sleep(0.1)

    gdf = gpd.GeoDataFrame(rows, geometry=geoms, crs="EPSG:4326")
    gdf["maplot"] = gdf["MAP_BK_LOT"].map(norm_maplot)
    # keep one polygon per maplot (largest) to avoid dup joins
    gdf["area"] = gdf.geometry.area
    gdf = gdf.sort_values("area", ascending=False).drop_duplicates("maplot").drop(columns="area")
    gdf.to_parquet(DATA / "parcels.gpq")
    print(f"cached {len(gdf):,} Gorham parcel polygons -> {DATA / 'parcels.gpq'}")
    print(f"unique maplot keys: {gdf.maplot.nunique():,}")
    print(gdf[["MAP_BK_LOT", "maplot", "PROP_LOC"]].head().to_string())


if __name__ == "__main__":
    main()
