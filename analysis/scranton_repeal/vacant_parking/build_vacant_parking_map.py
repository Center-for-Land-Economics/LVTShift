"""Map Scranton's vacant lots and surface parking lots, parcel by parcel.

Sources (all public, all cached under ./data/ on first run):
  - Lackawanna County GIS, GISViewer/LandRecords MapServer layer 85: parcel
    polygons joined to the assessment roll (owner, class code, land and
    improvement value). The GISViewer/Parcels layer the city notebook reads has
    the same polygons but its assessment fields are all null.
  - Chesapeake Bay Land Use/Land Cover Database, 2024 Edition, Lackawanna
    County 2021/22 raster (1 m; Chesapeake Conservancy / USGS ScienceBase).
    Its Structures class is the building footprint layer; its Other Impervious
    class is parking lots, driveways and sidewalks.
  - OpenStreetMap amenity=parking polygons via Overpass
    (lvt.transit_utils.fetch_osm_parking), which say which paving is parking.

The roll values in the county layer are the pre-reassessment base values, so
they are used only to tell built parcels from unbuilt ones (improvement value
and its share of total value), never as dollar amounts.

Per parcel, from the land cover: structure_share (structures, including under
tree canopy), paved_share (other impervious, including under canopy) and
parking_coverage (pixels both paved and inside OSM parking). Categories, in
priority order:
  Surface parking lot     parking_coverage >= PARKING_COVERAGE and
                          structure_share < MAX_LOT_STRUCTURE; or the assessor's
                          description says "PARKING", the improvement value is
                          zero and structure_share < MAX_LOT_STRUCTURE.
  Parking with building   parking_coverage >= PARKING_COVERAGE with a structure
                          on the parcel (a store and its lot).
  Paved lot               paved_share >= MIN_PAVED_SHARE, structure_share <
                          MAX_LOT_STRUCTURE, improvements < MAX_PARKING_IMP_SHARE
                          of value, not in OSM: unmapped parking or a paved yard.
  Vacant lot              improvement value zero, structure_share <
                          MAX_VACANT_STRUCTURE, under LARGE_TRACT_ACRES.
  Undeveloped tract       the same, LARGE_TRACT_ACRES or more (wooded hillside,
                          mine land, rail and utility land).
A parcel the roll calls unbuilt but the imagery shows a structure on is not
vacant. The reverse (roll value, no structure in the imagery) is not used to
call a parcel vacant: it is mostly houses hidden under tree canopy.
Zero-improvement slivers under MIN_VACANT_SQFT (alley remnants) are left out.
Taxable vs exempt comes from the second letter of the county class code
(DwellingType: T = taxable, E = exempt).

Run from anywhere:
    python analysis/scranton_repeal/vacant_parking/build_vacant_parking_map.py [--refresh]

Writes analysis/reports/scranton/vacant_parking_map.html (self-contained
Leaflet page) plus a flagged-parcel CSV and a summary CSV beside it.
"""
import argparse
import html
import json
import os
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import requests
from rasterio.features import rasterize
from rasterio.windows import from_bounds

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
sys.path.insert(0, str(REPO_ROOT))
from lvt.transit_utils import ACRE_SQM, fetch_osm_parking  # noqa: E402

LAYER_URL = ('https://gis.lackawannacounty.org/arcgis/rest/services/'
             'GISViewer/LandRecords/MapServer/85/query')
# ScienceBase item 68011bfad4be0263cab10002 (Lackawanna County, 42069), public S3 copy
LULC_URL = ('https://prod-is-usgs-sb-prod-publish.s3.amazonaws.com/68011bfad4be0263cab10002/'
            'lack_42069_lulc_2022_2024-Edition.tif')
# Raster values from the 2024 Edition User Guide (General classes IMPS, IMPO, TCIS)
STRUCTURE_CODES = (21, 24)  # Structures; Tree Canopy over Structures
PAVED_CODES = (22, 25)      # Other Impervious; Tree Canopy over Other Impervious
MUNICIPALITY = 'Scranton'
UTM = 'EPSG:26918'  # UTM 18N, meters
SQFT_PER_SQM = 10.7639

PARKING_COVERAGE = 0.4      # share of parcel both paved and inside OSM parking
MAX_LOT_STRUCTURE = 0.10    # a parking or paved lot carries no more building than this
MAX_VACANT_STRUCTURE = 0.15 # a vacant lot carries no more building than this (sheds, overhang)
MIN_PAVED_SHARE = 0.5
MAX_PARKING_IMP_SHARE = 0.25
LARGE_TRACT_ACRES = 1.0
MIN_VACANT_SQFT = 500
# OSM parking= values that are not surface lots
NON_SURFACE_PARKING = ('underground', 'multi-storey', 'lane', 'rooftop', 'street_side')

DATA_DIR = HERE / 'data'
OUT_DIR = REPO_ROOT / 'analysis' / 'reports' / 'scranton'

CATEGORIES = [
    # key, label, color
    ('parking', 'Surface parking lot', '#d9480f'),
    ('parking_bldg', 'Mostly parking, with a building', '#f59f00'),
    ('paved', 'Paved lot, not mapped as parking', '#9c36b5'),
    ('vacant', 'Vacant lot (under 1 acre)', '#1971c2'),
    ('tract', 'Undeveloped tract (1 acre+)', '#74b816'),
]


def fetch_parcels(refresh: bool = False) -> gpd.GeoDataFrame:
    """Scranton parcels with roll attributes, cached as GeoParquet."""
    cache = DATA_DIR / 'scranton_landrecords_raw.parquet'
    if cache.exists() and not refresh:
        return gpd.read_parquet(cache)
    DATA_DIR.mkdir(exist_ok=True)
    ids = requests.get(LAYER_URL, params={'where': f"Municipality='{MUNICIPALITY}'",
                                          'returnIdsOnly': 'true', 'f': 'json'},
                       timeout=120).json()['objectIds']
    ids = sorted(ids)
    parts = []
    for i in range(0, len(ids), 500):
        r = requests.post(LAYER_URL, data={'objectIds': ','.join(map(str, ids[i:i + 500])),
                                           'outFields': '*', 'outSR': 4326, 'f': 'geojson'},
                          timeout=180)
        r.raise_for_status()
        parts.append(gpd.GeoDataFrame.from_features(r.json()['features'], crs=4326))
    gdf = pd.concat(parts, ignore_index=True)
    gdf.to_parquet(cache)
    return gdf


def fetch_land_cover(refresh: bool = False) -> Path:
    """Download (once) the Lackawanna County 2021/22 land cover raster."""
    path = DATA_DIR / 'lulc' / LULC_URL.rsplit('/', 1)[1]
    if path.exists() and not refresh:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(LULC_URL, stream=True, timeout=600) as r:
        r.raise_for_status()
        with open(path, 'wb') as fh:
            for chunk in r.iter_content(1 << 20):
                fh.write(chunk)
    return path


def prepare_parcels(raw: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Numeric values, one row per PIN (multipart parcels dissolved), projected."""
    gdf = raw.to_crs(UTM)
    for col in ['LandValue', 'ImprovedValue', 'TotalValue']:
        gdf[col] = pd.to_numeric(gdf[col], errors='coerce')
    gdf['geometry'] = gdf.geometry.buffer(0)
    # A PIN that appears more than once is one parcel drawn in several pieces,
    # each row carrying the same roll values; keep one row of values.
    attrs = gdf.drop(columns='geometry').drop_duplicates('ParcelPIN').set_index('ParcelPIN')
    geom = gdf.dissolve(by='ParcelPIN')[['geometry']]
    out = gpd.GeoDataFrame(attrs.join(geom), geometry='geometry', crs=UTM).reset_index()
    out['taxable'] = out['DwellingType'].fillna('').str[1].eq('T')
    out['imp_share'] = (out['ImprovedValue'] / out['TotalValue'].replace(0, np.nan)).fillna(0)
    return out


def land_cover_shares(parcels: gpd.GeoDataFrame, parking: gpd.GeoDataFrame,
                      lulc_path: Path) -> gpd.GeoDataFrame:
    """Add structure_share, paved_share and parking_coverage from the 1 m raster.

    Parcels are burned onto the raster grid; where parcel polygons overlap, a
    pixel goes to the later parcel. Shares are of each parcel's own pixels.
    """
    with rasterio.open(lulc_path) as src:
        crs = src.crs
        pp = parcels.to_crs(crs)
        win = from_bounds(*pp.total_bounds, src.transform).round_offsets().round_lengths()
        lc = src.read(1, window=win)
        transform = src.window_transform(win)
    ids = rasterize(((g, i + 1) for i, g in enumerate(pp.geometry)), out_shape=lc.shape,
                    transform=transform, fill=0, dtype='int32')
    in_osm_lot = rasterize(parking.to_crs(crs).geometry, out_shape=lc.shape,
                           transform=transform, fill=0, dtype='uint8').astype(bool)
    inside = ids > 0
    idx, n = ids[inside], len(pp) + 1
    px = np.bincount(idx, minlength=n)[1:]

    def share(mask):
        return np.bincount(idx, weights=mask[inside], minlength=n)[1:] / np.maximum(px, 1)

    paved = np.isin(lc, PAVED_CODES)
    out = parcels.copy()
    out['structure_share'] = share(np.isin(lc, STRUCTURE_CODES))
    out['paved_share'] = share(paved)
    out['parking_coverage'] = share(paved & in_osm_lot)
    return out


def classify(parcels: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Add acres and category columns to parcels carrying land cover shares."""
    out = parcels.copy()
    out['acres'] = out.geometry.area / ACRE_SQM
    no_building_value = out['ImprovedValue'].fillna(0).eq(0)
    lot_structure = out['structure_share'] < MAX_LOT_STRUCTURE
    mapped_parking = out['parking_coverage'] >= PARKING_COVERAGE
    text_parking = out['Dimensions'].fillna('').str.contains('PARKING') & no_building_value
    # The land cover maps rail ballast as Other Impervious, so rail corridors
    # (RRL parcel numbers, railroad owners) would otherwise read as paved lots.
    rail = (out['ParcelPIN'].str.contains('RRL')
            | out['OwnerName'].fillna('').str.contains(r'RAIL ?ROAD|RAILWAY|\bR R\b', regex=True))
    paved_lot = ((out['paved_share'] >= MIN_PAVED_SHARE) & lot_structure
                 & (out['imp_share'] < MAX_PARKING_IMP_SHARE) & ~rail)
    vacant = (no_building_value & (out['structure_share'] < MAX_VACANT_STRUCTURE)
              & (out.geometry.area * SQFT_PER_SQM >= MIN_VACANT_SQFT))

    cat = pd.Series(None, index=out.index, dtype=object)
    cat[vacant & (out['acres'] >= LARGE_TRACT_ACRES)] = 'tract'
    cat[vacant & (out['acres'] < LARGE_TRACT_ACRES)] = 'vacant'
    cat[paved_lot] = 'paved'
    cat[mapped_parking & ~lot_structure] = 'parking_bldg'
    cat[(mapped_parking | text_parking) & lot_structure] = 'parking'
    out['category'] = cat
    return out


def summarize(flagged: gpd.GeoDataFrame) -> pd.DataFrame:
    labels = {k: lab for k, lab, _ in CATEGORIES}
    rows = []
    for key, _, _ in CATEGORIES:
        for taxable in (True, False):
            sub = flagged[(flagged['category'] == key) & (flagged['taxable'] == taxable)]
            rows.append({'category': labels[key], 'taxable': taxable,
                         'parcels': len(sub), 'acres': round(sub['acres'].sum(), 1)})
    return pd.DataFrame(rows)


def _round_coords(coords, p=5):
    if isinstance(coords[0], (int, float)):
        return [round(coords[0], p), round(coords[1], p)]
    return [_round_coords(c, p) for c in coords]


def _features(gdf: gpd.GeoDataFrame, props_fn) -> list:
    merc = gdf.to_crs(3857)
    merc['geometry'] = merc.geometry.simplify(1.0, preserve_topology=True)
    wgs = merc.to_crs(4326)
    feats = []
    for row in wgs.itertuples(index=False):
        if row.geometry is None or row.geometry.is_empty:
            continue
        geo = row.geometry.__geo_interface__
        feats.append({'type': 'Feature', 'properties': props_fn(row),
                      'geometry': {'type': geo['type'], 'coordinates': _round_coords(geo['coordinates'])}})
    return feats


def _clean(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    s = str(v).strip()
    return s or None


def write_map(flagged: gpd.GeoDataFrame, parking: gpd.GeoDataFrame, n_parcels: int, out_path: Path):
    parcel_feats = _features(flagged, lambda r: {
        'pid': r.ParcelPIN, 'adr': _clean(r.LOCATION), 'own': _clean(r.OwnerName),
        'cat': r.category, 'tx': bool(r.taxable), 'cls': _clean(r.DwellingType),
        'ac': round(float(r.acres), 3), 'pk': round(float(r.parking_coverage) * 100),
        'st': round(float(r.structure_share) * 100), 'pv': round(float(r.paved_share) * 100),
        'desc': _clean(r.Dimensions),
    })
    osm_feats = _features(parking, lambda r: {'t': _clean(r.parking_type) or 'untagged'})
    center = flagged.to_crs(4326).union_all().centroid
    doc = (_HTML
           .replace('__PARCELS__', json.dumps({'type': 'FeatureCollection', 'features': parcel_feats},
                                              separators=(',', ':')))
           .replace('__OSM__', json.dumps({'type': 'FeatureCollection', 'features': osm_feats},
                                          separators=(',', ':')))
           .replace('__CATS__', json.dumps([{'k': k, 'l': lab, 'c': c} for k, lab, c in CATEGORIES]))
           .replace('__CENTER__', json.dumps([round(center.y, 5), round(center.x, 5)]))
           .replace('__NPARCELS__', json.dumps(n_parcels))
           .replace('__PARAMS__', html.escape(
               f'Surface parking: at least {PARKING_COVERAGE:.0%} of the parcel is paved '
               f'ground inside an OpenStreetMap parking lot, and buildings cover under '
               f'{MAX_LOT_STRUCTURE:.0%} of it in the 2021/22 land cover (or the '
               f'assessor\'s description says parking and it is unbuilt). Paved lot: at '
               f'least {MIN_PAVED_SHARE:.0%} paved with no building, but not mapped as '
               f'parking in OpenStreetMap, so it may be parking or a paved yard. Vacant: '
               f'no building value on the roll and buildings cover under '
               f'{MAX_VACANT_STRUCTURE:.0%} of the parcel; slivers under '
               f'{MIN_VACANT_SQFT:,} sq ft are left out.')))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(doc, encoding='utf-8')


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--refresh', action='store_true', help='re-download parcels, land cover and OSM parking')
    args = ap.parse_args()

    raw = fetch_parcels(args.refresh)
    parcels = prepare_parcels(raw)
    osm_cache = DATA_DIR / 'osm_parking.gpkg'
    if args.refresh and osm_cache.exists():
        osm_cache.unlink()
    parking = fetch_osm_parking(parcels, str(osm_cache), to_crs=UTM,
                                exclude_types=NON_SURFACE_PARKING)

    lulc_path = fetch_land_cover(args.refresh)

    classified = classify(land_cover_shares(parcels, parking, lulc_path))
    flagged = classified[classified['category'].notna()].copy()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary = summarize(flagged)
    summary.to_csv(OUT_DIR / 'vacant_parking_summary.csv', index=False)
    cols = ['ParcelPIN', 'LOCATION', 'OwnerName', 'category', 'taxable', 'DwellingType',
            'DwellingCode', 'acres', 'structure_share', 'paved_share', 'parking_coverage',
            'LandValue', 'ImprovedValue', 'Dimensions']
    flagged[cols].sort_values(['category', 'acres'], ascending=[True, False]).to_csv(
        OUT_DIR / 'vacant_parking_parcels.csv', index=False)
    write_map(flagged, parking, len(parcels), OUT_DIR / 'vacant_parking_map.html')

    print(f'{len(parcels):,} Scranton parcels; {len(parking):,} OSM surface parking polygons')
    print(summary.to_string(index=False))
    print(f'Map: {OUT_DIR / "vacant_parking_map.html"}')


_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Scranton Vacant and Parking Land</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
  :root{--ground:#f4f6f7;--panel:#fff;--ink:#16202a;--muted:#5c6b78;--line:#dde3e8;--accent:#0f766e;
    --sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
  *{box-sizing:border-box} html,body{margin:0;background:var(--ground)}
  .wrap{font-family:var(--sans);color:var(--ink);max-width:1180px;margin:0 auto;padding:22px 16px 28px;line-height:1.5}
  .eyebrow{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--accent);font-weight:600}
  h1{font-size:clamp(20px,2.4vw,27px);line-height:1.18;margin:.35em 0 .3em;font-weight:650;letter-spacing:-.01em}
  .sub{margin:0;color:var(--muted);font-size:14px;max-width:80ch}
  .stats{display:flex;flex-wrap:wrap;gap:10px;margin:18px 0 14px}
  .chip{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:9px 13px;min-width:120px;border-left-width:5px}
  .chip .k{font-size:11px;letter-spacing:.03em;text-transform:uppercase;color:var(--muted)}
  .chip .v{font-size:19px;font-weight:650;font-variant-numeric:tabular-nums}
  .chip .s{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}
  .controls{display:flex;flex-wrap:wrap;gap:8px 18px;margin:0 0 12px;font-size:13.5px}
  .controls label{display:flex;align-items:center;gap:6px;cursor:pointer}
  .controls i{width:14px;height:12px;border-radius:2px;display:inline-block}
  .stage{display:grid;grid-template-columns:1fr 300px;gap:16px;align-items:start}
  @media(max-width:800px){.stage{grid-template-columns:1fr}}
  .mapbox{position:relative;border:1px solid var(--line);border-radius:12px;overflow:hidden;background:#eef1f3}
  #map{height:660px;width:100%}
  @media(max-width:800px){#map{height:480px}}
  .inspector{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;position:sticky;top:12px;min-height:200px}
  .hint{color:var(--muted);font-size:13px}
  .ins-cat{font-size:11px;letter-spacing:.05em;text-transform:uppercase;font-weight:650}
  .ins-id{font-size:18px;font-weight:650;font-variant-numeric:tabular-nums;margin:2px 0 1px}
  .ins-adr{color:var(--muted);font-size:13.5px}
  table.kv{width:100%;border-collapse:collapse;margin-top:10px;font-size:13.5px}
  table.kv td{padding:6px 0;border-top:1px solid var(--line);vertical-align:top}
  table.kv td.k{color:var(--muted);padding-right:10px} table.kv td.v{text-align:right}
  .ft{margin-top:16px;color:var(--muted);font-size:12.5px;max-width:90ch}.ft b{color:var(--ink)}
  .leaflet-control-layers{font-family:var(--sans);font-size:13px;border-radius:10px !important}
</style>
</head>
<body>
<div class="wrap">
  <div class="eyebrow">LVTShift &middot; Scranton split-rate repeal</div>
  <h1>Vacant lots and surface parking in Scranton</h1>
  <p class="sub">Every parcel in the City of Scranton whose land is mostly unbuilt or used for surface parking, from the Lackawanna County parcel map and OpenStreetMap. Click a parcel for its owner and details.</p>
  <div class="stats" id="stats"></div>
  <div class="controls" id="controls"></div>
  <div class="stage">
    <div class="mapbox"><div id="map"></div></div>
    <aside class="inspector" id="insp"><div class="hint"><b>Click any parcel</b> to see its parcel number, owner, class code, and how much of it is building, paving and mapped parking.</div></aside>
  </div>
  <p class="ft"><b>How parcels are classified.</b> __PARAMS__ "Mostly parking, with a building" passes the same parking test but also holds a building, such as a store and its lot. Tax-exempt owners (government, churches, schools, hospitals) are drawn with a lighter fill and can be hidden. Assessed values in this county layer predate the 2026 reassessment and are used only to tell built from unbuilt parcels. Building and paving shares come from 1 m land cover mapped from 2021/22 aerial imagery; a building under dense tree canopy can be missed.</p>
  <p class="ft">Sources: Lackawanna County GIS (LandRecords parcels); Chesapeake Bay Land Use/Land Cover Database 2024 Edition (Chesapeake Conservancy, USGS); OpenStreetMap contributors via Overpass. Base maps: OpenStreetMap, Esri World Imagery.</p>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const PARCELS = __PARCELS__, OSM = __OSM__, CATS = __CATS__, CENTER = __CENTER__, NPARCELS = __NPARCELS__;
const CAT = Object.fromEntries(CATS.map(c => [c.k, c]));
const map = L.map('map', { preferCanvas: true }).setView(CENTER, 13);
const road = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 20, maxNativeZoom: 19, attribution: '&copy; OpenStreetMap contributors' });
const sat = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', { maxZoom: 20, maxNativeZoom: 19, attribution: 'Tiles &copy; Esri' });
road.addTo(map);
const osmLayer = L.geoJSON(OSM, { interactive: false, style: { color: '#7b2d8b', weight: 1.2, fill: false, dashArray: '3 3' } });
L.control.layers({ 'Road': road, 'Satellite': sat }, { 'OpenStreetMap parking outlines': osmLayer }, { collapsed: false }).addTo(map);

const show = Object.fromEntries(CATS.map(c => [c.k, true])); let showExempt = true;
const style = f => ({ stroke: true, weight: 0.6, color: CAT[f.properties.cat].c, fillColor: CAT[f.properties.cat].c,
  fillOpacity: f.properties.tx ? 0.75 : 0.28, opacity: f.properties.tx ? 0.9 : 0.6 });
const insp = document.getElementById('insp');
let selected = null;
const esc = s => s == null ? '' : String(s).replace(/[&<>"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[ch]));
function inspect(p, lyr) {
  if (selected) layer.resetStyle(selected);
  selected = lyr; lyr.setStyle({ weight: 2.6, color: '#0b1116' }); lyr.bringToFront();
  const c = CAT[p.cat];
  const acres = p.ac >= 1 ? p.ac.toFixed(2) + ' acres' : Math.round(p.ac * 43560).toLocaleString() + ' sq ft';
  insp.innerHTML = '<div class="ins-cat" style="color:' + c.c + '">' + esc(c.l) + '</div>' +
    '<div class="ins-id">Parcel ' + esc(p.pid) + '</div>' +
    (p.adr ? '<div class="ins-adr">' + esc(p.adr) + '</div>' : '') +
    '<table class="kv"><tbody>' +
    '<tr><td class="k">Owner</td><td class="v">' + esc(p.own || '—') + '</td></tr>' +
    '<tr><td class="k">Tax status</td><td class="v">' + (p.tx ? 'Taxable' : 'Exempt') + ' (class ' + esc(p.cls) + ')</td></tr>' +
    '<tr><td class="k">Size</td><td class="v">' + acres + '</td></tr>' +
    '<tr><td class="k">Building cover</td><td class="v">' + p.st + '%</td></tr>' +
    '<tr><td class="k">Paved (non-road)</td><td class="v">' + p.pv + '%</td></tr>' +
    '<tr><td class="k">Paved and mapped as parking</td><td class="v">' + p.pk + '%</td></tr>' +
    (p.desc ? '<tr><td class="k">Assessor note</td><td class="v">' + esc(p.desc) + '</td></tr>' : '') +
    '</tbody></table>';
}
const layer = L.geoJSON(PARCELS, {
  style, filter: f => show[f.properties.cat] && (showExempt || f.properties.tx),
  onEachFeature: (f, lyr) => lyr.on('click', () => inspect(f.properties, lyr))
}).addTo(map);
try { map.fitBounds(layer.getBounds(), { padding: [10, 10] }); } catch (e) {}
function redraw() { layer.clearLayers(); selected = null; layer.addData(PARCELS); renderStats(); }

const ctl = document.getElementById('controls');
ctl.innerHTML = CATS.map(c => '<label><input type="checkbox" data-k="' + c.k + '" checked><i style="background:' + c.c + '"></i>' + c.l + '</label>').join('') +
  '<label><input type="checkbox" id="exempt" checked>Include tax-exempt owners</label>';
ctl.querySelectorAll('input[data-k]').forEach(el => el.addEventListener('change', () => { show[el.dataset.k] = el.checked; redraw(); }));
document.getElementById('exempt').addEventListener('change', e => { showExempt = e.target.checked; redraw(); });

function renderStats() {
  const chips = CATS.map(c => {
    const fs = PARCELS.features.filter(f => f.properties.cat === c.k && (showExempt || f.properties.tx));
    const ac = fs.reduce((s, f) => s + f.properties.ac, 0);
    return '<div class="chip" style="border-left-color:' + c.c + '"><div class="k">' + c.l + '</div><div class="v">' +
      fs.length.toLocaleString() + '</div><div class="s">' + Math.round(ac).toLocaleString() + ' acres</div></div>';
  });
  chips.push('<div class="chip" style="border-left-color:#adb5bd"><div class="k">All city parcels</div><div class="v">' + NPARCELS.toLocaleString() + '</div><div class="s">' + (showExempt ? 'flags include exempt owners' : 'flags: taxable only') + '</div></div>');
  document.getElementById('stats').innerHTML = chips.join('');
}
renderStats();
</script>
</body>
</html>
"""

if __name__ == '__main__':
    main()
