"""Scranton's vacant lots and unbuilt paved lots gathered into one square.

The Scranton sibling of philly_land_tax_research's
scripts/build_philly_gathered_square.py: the square's area equals the summed
ground area of every parcel in the chosen categories, split by area into one
band per category. Nothing is exaggerated.

Reads analysis/reports/scranton/vacant_parking_parcels.csv, so run
build_vacant_parking_map.py first. The city outline and its land area come
from the Census TIGERweb place boundary (Scranton city, GEOID 4269000).

Output: analysis/reports/scranton/vacant_parking_gathered.png and a JSON of the
numbers drawn on it.
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

# Bands, bottom to top: category key in the parcel CSV, label, color.
# Undeveloped tracts (mostly wooded hillside and mine land) and parking lots
# that carry a building are left out; the footnote says so.
BANDS = [
    ('vacant', 'Vacant lots', '#FFB020'),
    ('paved', 'Paved lots, nothing built', '#C77DFF'),
    ('parking', 'Surface parking', '#FF4D5A'),
]

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


def main() -> int:
    parcels = pd.read_csv(OUT_DIR / 'vacant_parking_parcels.csv', dtype={'ParcelPIN': str})
    acres = {key: float(parcels.loc[parcels['category'] == key, 'acres'].sum())
             for key, _, _ in BANDS}
    total_ac = sum(acres.values())

    city = fetch_city('--refresh' in sys.argv)
    city_land_ac = float(city['AREALAND'].iloc[0]) / SQM_PER_ACRE
    city = city.to_crs(CRS_PLOT)

    side = float(np.sqrt(total_ac * SQFT_PER_ACRE))  # feet
    centre = city.geometry.iloc[0].representative_point()
    x0, y0 = centre.x - side / 2, centre.y - side / 2

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
    y = y0
    for i, (key, label, color) in enumerate(BANDS):
        band_h = side * acres[key] / total_ac
        ax.add_patch(Rectangle((x0, y), side, band_h, facecolor=color,
                               edgecolor='none', zorder=5))
        # Callouts alternate sides so the thin bands do not crowd each other
        left = i % 2 == 0
        tx = x0 - side * 1.25 if left else x0 + side * 1.25
        ty = y0 + side * (-0.15 + 0.55 * i)
        ax.annotate(f'{label}\n{acres[key]:,.0f} acres',
                    xy=(x0 + side * 0.5, y + band_h * 0.5), xytext=(tx, ty),
                    color=INK, fontsize=12.5, va='center', ha='right' if left else 'left',
                    zorder=8, linespacing=1.4,
                    arrowprops=dict(arrowstyle='-', color=INK, alpha=0.5, linewidth=0.9))
        y += band_h
    ax.add_patch(Rectangle((x0, y0), side, side, fill=False, edgecolor=INK,
                           linewidth=1.4, zorder=6))

    # Scale reference along the base of the square
    ax.annotate('', xy=(x0, y0 - side * 0.11), xytext=(x0 + side, y0 - side * 0.11),
                arrowprops=dict(arrowstyle='<->', color=MUTED, linewidth=1.0), zorder=8)
    ax.text(x0 + side / 2, y0 - side * 0.155, f'{side / 5280:.1f} miles',
            color=MUTED, fontsize=11, ha='center', va='top', zorder=8)

    pct = total_ac / city_land_ac * 100
    fig.text(0.035, 0.978, f'{total_ac:,.0f} acres', color=INK,
             fontsize=44, fontweight='bold', va='top')
    fig.text(0.035, 0.912, 'of Scranton is vacant or paved with nothing built on it.',
             color=INK, fontsize=20, va='top')
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
             'Not counted: undeveloped tracts of an acre or more (mostly wooded hillside and '
             'mine land), and parking lots that also hold a building.',
             color=MUTED, fontsize=8.2, va='bottom', linespacing=1.6, zorder=10,
             bbox=dict(facecolor=BG, edgecolor='none', alpha=0.82, pad=4.0))
    fig.text(0.965, 0.030, 'Progress and Poverty Institute', color=MUTED,
             fontsize=9, ha='right', va='bottom')

    out = OUT_DIR / 'vacant_parking_gathered.png'
    fig.savefig(out, facecolor=BG)
    plt.close(fig)

    stats = {**{f'{k}_acres': v for k, v in acres.items()}, 'total_acres': total_ac,
             'square_side_ft': side, 'square_side_mi': side / 5280,
             'city_land_acres': city_land_ac, 'pct_of_city_land': pct}
    (OUT_DIR / 'vacant_parking_gathered.json').write_text(json.dumps(stats, indent=2))
    print(f'wrote {out}')
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
