"""Scranton's vacant lots and unbuilt paved lots gathered into one square.

Two figures: the vacant, paved and surface-parking lots alone, and the same
with undeveloped tracts of an acre or more added as their own band.

The Scranton sibling of philly_land_tax_research's
scripts/build_philly_gathered_square.py: the square's area equals the summed
ground area of every parcel in the chosen categories, split by area into one
band per category. Nothing is exaggerated.

Reads analysis/reports/scranton/vacant_parking_parcels.csv, so run
build_vacant_parking_map.py first. The city outline and its land area come
from the Census TIGERweb place boundary (Scranton city, GEOID 4269000).

Output: analysis/reports/scranton/vacant_parking_gathered.png and
vacant_parking_gathered_with_tracts.png, each with a JSON of the numbers drawn
on it.
"""
import json
import sys
from pathlib import Path

import geopandas as gpd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
from matplotlib.patches import Rectangle
from shapely.geometry import box

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
OUT_DIR = REPO_ROOT / 'analysis' / 'reports' / 'scranton'
DATA_DIR = HERE / 'data'

PLACE_URL = ('https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/'
             'Places_CouSub_ConCity_SubMCD/MapServer/4/query')
PLACE_GEOID = '4269000'
CRS_PLOT = 'EPSG:2271'  # PA State Plane North, feet
SQFT_PER_ACRE = 43560.0
SQM_PER_ACRE = 4046.8564224

# Bands: category key in the parcel CSV, label, color
VACANT = ('vacant', 'Vacant lots', '#FFB020')
PAVED = ('paved', 'Paved lots, nothing built', '#C77DFF')
PARKING = ('parking', 'Surface parking', '#FF4D5A')
TRACT = ('tract', 'Undeveloped tracts, 1 acre+', '#5FB35F')

# One figure per variant, bands listed bottom to top. The base figure leaves
# out undeveloped tracts (mostly wooded hillside and mine land) so they do not
# swamp the urban lots; the second adds them. Both leave out parking lots that
# carry a building.
VARIANTS = [
    {'name': 'vacant_parking_gathered', 'bands': [VACANT, PAVED, PARKING],
     'headline': 'of Scranton is vacant or paved with nothing built on it.',
     'not_counted': 'undeveloped tracts of an acre or more (mostly wooded hillside and '
                    'mine land), and parking lots that also hold a building.'},
    {'name': 'vacant_parking_gathered_with_tracts', 'bands': [TRACT, VACANT, PAVED, PARKING],
     'headline': 'of Scranton is undeveloped, vacant, or paved with nothing built on it.',
     'not_counted': 'parking lots that also hold a building.'},
]
LABEL_GAP_FT = 1800       # from the square's side to its callout text
LABEL_SPACING_FT = 3400   # minimum vertical gap between callouts on one side

# Palette of the Philadelphia figure
BG = '#0d1117'
INK = '#f2f5f8'
MUTED = '#93a1b0'
CITY_FILL = '#242c3a'
CITY_EDGE = '#8496ab'


def fetch_city(refresh: bool = False) -> gpd.GeoDataFrame:
    """Scranton city's Census place polygon with AREALAND, cached."""
    cache = DATA_DIR / 'scranton_place_boundary.gpkg'
    if cache.exists() and not refresh:
        return gpd.read_file(cache)
    r = requests.get(PLACE_URL, params={'where': f"GEOID='{PLACE_GEOID}'",
                                        'outFields': 'NAME,GEOID,AREALAND,AREAWATER',
                                        'outSR': 4326, 'f': 'geojson'}, timeout=120)
    r.raise_for_status()
    city = gpd.GeoDataFrame.from_features(r.json()['features'], crs=4326)
    DATA_DIR.mkdir(exist_ok=True)
    city.to_file(cache)
    return city


def place_square(city_geom, side: float) -> tuple:
    """Lower-left corner of a side x side square inside the city, nearest its centre.

    Falls back to centring on the city's representative point if no grid
    position fits entirely inside the outline.
    """
    centre = city_geom.representative_point()
    minx, miny, maxx, maxy = city_geom.bounds
    best, best_d = (centre.x - side / 2, centre.y - side / 2), np.inf
    for cx in np.linspace(minx, maxx, 60):
        for cy in np.linspace(miny, maxy, 60):
            d = np.hypot(cx - centre.x, cy - centre.y)
            if d < best_d and box(cx - side / 2, cy - side / 2,
                                  cx + side / 2, cy + side / 2).within(city_geom):
                best, best_d = (cx - side / 2, cy - side / 2), d
    return best


def label_heights(targets: list, spacing: float) -> list:
    """Push callout heights up so that no two are closer than spacing."""
    out = []
    for t in targets:
        out.append(t if not out else max(t, out[-1] + spacing))
    return out


def render(variant: dict, acres_by_cat: dict, city: gpd.GeoDataFrame,
           city_land_ac: float) -> dict:
    """Draw one variant's figure; return the numbers drawn on it."""
    bands = variant['bands']
    acres = {key: acres_by_cat.get(key, 0.0) for key, _, _ in bands}
    total_ac = sum(acres.values())
    side = float(np.sqrt(total_ac * SQFT_PER_ACRE))  # feet
    x0, y0 = place_square(city.geometry.iloc[0], side)

    fig = plt.figure(figsize=(11.0, 11.8), dpi=200, facecolor=BG)
    ax = fig.add_axes([0.0, 0.0, 1.0, 1.0])
    ax.set_facecolor(BG)
    city.plot(ax=ax, facecolor=CITY_FILL, edgecolor=CITY_EDGE, linewidth=1.8, zorder=1)
    minx, miny, maxx, maxy = city.total_bounds
    # Leave the top of the frame for the headline and the bottom for the
    # footnote; the city sits between them.
    h = maxy - miny
    ax.set_ylim(miny - h * 0.13, maxy + h * 0.30)
    ax.set_aspect('equal', adjustable='datalim')
    ax.set_axis_off()

    # The square, split by area into one band per category
    y, centres = y0, []
    for key, _, color in bands:
        band_h = side * acres[key] / total_ac
        ax.add_patch(Rectangle((x0, y), side, band_h, facecolor=color,
                               edgecolor='none', zorder=5))
        centres.append(y + band_h / 2)
        y += band_h
    ax.add_patch(Rectangle((x0, y0), side, side, fill=False, edgecolor=INK,
                           linewidth=1.4, zorder=6))

    # Callouts alternate sides, each at its band's height unless that would
    # crowd the callout below it
    for parity in (0, 1):
        idx = [i for i in range(len(bands)) if i % 2 == parity]
        heights = label_heights([centres[i] for i in idx], LABEL_SPACING_FT)
        for i, ty in zip(idx, heights):
            key, label, _ = bands[i]
            left = parity == 0
            tx = x0 - LABEL_GAP_FT if left else x0 + side + LABEL_GAP_FT
            ax.annotate(f'{label}\n{acres[key]:,.0f} acres',
                        xy=(x0 + side * 0.5, centres[i]), xytext=(tx, ty),
                        color=INK, fontsize=12.5, va='center', ha='right' if left else 'left',
                        zorder=8, linespacing=1.4,
                        arrowprops=dict(arrowstyle='-', color=INK, alpha=0.5, linewidth=0.9))

    # Scale reference along the base of the square
    tick = min(side * 0.11, 700)
    ax.annotate('', xy=(x0, y0 - tick), xytext=(x0 + side, y0 - tick),
                arrowprops=dict(arrowstyle='<->', color=MUTED, linewidth=1.0), zorder=8)
    ax.text(x0 + side / 2, y0 - tick * 1.4, f'{side / 5280:.1f} miles',
            color=MUTED, fontsize=11, ha='center', va='top', zorder=8)

    pct = total_ac / city_land_ac * 100
    fig.text(0.035, 0.978, f'{total_ac:,.0f} acres', color=INK,
             fontsize=44, fontweight='bold', va='top')
    fig.text(0.035, 0.912, variant['headline'], color=INK, fontsize=20, va='top')
    fig.text(0.035, 0.872,
             f'Gathered into one place it would cover this much of the city: '
             f'{side / 5280:.1f} miles on a side,\n'
             f'{pct:.1f}% of all the land in Scranton.',
             color=MUTED, fontsize=12.5, va='top', linespacing=1.55)

    fig.text(0.035, 0.030,
             'Sources: Lackawanna County parcel records; Chesapeake Bay Land Use/Land Cover, '
             '2021/22 (Chesapeake Conservancy, USGS);\n'
             'OpenStreetMap parking; U.S. Census Bureau city boundary. Square area equals the '
             'summed ground area of every such parcel.\n'
             f"Not counted: {variant['not_counted']}",
             color=MUTED, fontsize=8.2, va='bottom', linespacing=1.6, zorder=10,
             bbox=dict(facecolor=BG, edgecolor='none', alpha=0.82, pad=4.0))
    fig.text(0.965, 0.030, 'Progress and Poverty Institute', color=MUTED,
             fontsize=9, ha='right', va='bottom')

    out = OUT_DIR / f"{variant['name']}.png"
    fig.savefig(out, facecolor=BG)
    plt.close(fig)
    print(f'wrote {out}')
    return {**{f'{k}_acres': v for k, v in acres.items()}, 'total_acres': total_ac,
            'square_side_ft': side, 'square_side_mi': side / 5280,
            'city_land_acres': city_land_ac, 'pct_of_city_land': pct}


def main() -> int:
    parcels = pd.read_csv(OUT_DIR / 'vacant_parking_parcels.csv', dtype={'ParcelPIN': str})
    acres_by_cat = parcels.groupby('category')['acres'].sum().astype(float).to_dict()

    city = fetch_city('--refresh' in sys.argv)
    city_land_ac = float(city['AREALAND'].iloc[0]) / SQM_PER_ACRE
    city = city.to_crs(CRS_PLOT)

    for variant in VARIANTS:
        stats = render(variant, acres_by_cat, city, city_land_ac)
        (OUT_DIR / f"{variant['name']}.json").write_text(json.dumps(stats, indent=2))
        print(json.dumps(stats, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
