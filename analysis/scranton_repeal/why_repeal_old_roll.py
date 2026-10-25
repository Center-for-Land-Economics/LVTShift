"""Who the split-rate repeal favors, tested on the pre-reassessment (2025) roll.

Fills the AUTOGEN regions of WHY_REPEAL.md. For every taxable parcel it
compares the 2025 bill under the split rate (land 249.19765, buildings
52.60679 mills) with the bill under one revenue-neutral rate on the same
2025 values. This isolates the repeal from the reassessment, but on
1968-base values: a parcel's land share on the 2026 roll can differ, so the
dollar figures are not anyone's 2026 change. The ranking of who gains (land
with little building on it) follows from the tax's structure and is the
part meant to carry over.

It also tests the budget's claim that most vacant, developable land is
built on, against the Philadelphia/Scranton idle-land comparison (both
cities through lvt/idle_land.py) and a side-yard check on Scranton's
vacant lots.

Reads vacant_parking/data/scranton_landrecords_raw.parquet (County layer
GISViewer/LandRecords/MapServer/85, cached by build_vacant_parking_map.py),
analysis/reports/scranton/vacant_parking_parcels.csv (written by
build_vacant_parking_map.py), and the comparison JSON in the sibling repo
philly_land_tax_research (analysis/idle_land/results/comparison.json).

Usage:
    python why_repeal_old_roll.py           # regenerate WHY_REPEAL.md in place
    python why_repeal_old_roll.py --check   # exit 1 with a diff if it is stale
"""
import difflib
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import geopandas as gpd
import pandas as pd

HERE = Path(__file__).resolve().parent
ROLL = HERE / 'vacant_parking' / 'data' / 'scranton_landrecords_raw.parquet'
MEMO = HERE / 'WHY_REPEAL.md'
REPO_ROOT = HERE.parents[1]
IDLE_PARCELS = REPO_ROOT / 'analysis' / 'reports' / 'scranton' / 'vacant_parking_parcels.csv'
COMPARISON = (REPO_ROOT.parent / 'philly_land_tax_research' / 'analysis' / 'idle_land'
              / 'results' / 'comparison.json')
SIDE_YARD_FT = 5  # a vacant lot within this distance of a built lot with the same owner name

LAND_MILLS = 249.19765
BLDG_MILLS = 52.60679
LEVY_2025 = 37_202_775.59  # File of the Council No. 107, 2025

CLASS_NAMES = {'R': 'Residential', 'C': 'Commercial', 'I': 'Industrial', 'A': 'Agricultural',
               'H': 'Hospital', 'L': 'L (code table pending)'}

# Officials at the 2025-12-16 vote, as owner-name patterns on the roll. A
# match is a name match, not confirmed ownership; the note says how strong.
OFFICIALS: List[Tuple[str, str, str]] = [
    ('Mayor Paige Cognetti', r'PAIGE GEBHARDT', 'full name on deed'),
    ('Gerald Smurl (council president)', r'SMURL GERALD|SMURL APARTMENTS LLC',
     'name and spouse; the LLC sits beside Smurl HVAC, 1111 Stafford Ave'),
    ('Mark McAndrew', r'MCANDREW MARK', 'name only'),
    ('Jessica Rothchild', r'ROTHCHILD JESSICA', 'name only'),
    ('Thomas Schuster', r'SCHUSTER THOMAS', 'two different Thomas Schusters; neither confirmed'),
    ('William King', r'KING WILLIAM F', 'name only'),
]

# Owners named in press coverage or in the Fidelity Bank controversy.
NAMED: List[Tuple[str, str]] = [
    ('Steamtown 300 LLC (Marketplace at Steamtown; press ties it to John Basalyga)', r'^STEAMTOWN 300 LLC$'),
    ('JBAS Realty LLC (John Basalyga)', r'^JBAS REALTY LLC$'),
    ('Linden Taylor LLC (Don Mammano; the contested 301 Penn Ave lot)', r'^LINDEN TAYLOR LLC$'),
    ('Fidelity Deposit & Discount Bank (any name containing FIDELITY)', r'FIDELITY'),
]


def load() -> pd.DataFrame:
    """Taxable parcels with split and single-rate 2025 bills."""
    df = pd.read_parquet(ROLL, columns=['ParcelPIN', 'OwnerName', 'LOCATION', 'DwellingType',
                                        'LandValue', 'ImprovedValue', 'TotalValue'])
    for c in ['LandValue', 'ImprovedValue', 'TotalValue']:
        df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0)
    df = df[df['DwellingType'].str[1:2] == 'T'].copy()
    # TotalValue falls below land + building on two bare-land parcels (likely a
    # preferential total); both bills use land + building, which reproduces the levy.
    df['TotalValue'] = df['LandValue'] + df['ImprovedValue']
    df['cls'] = df['DwellingType'].str[0]
    df['own'] = df['OwnerName'].str.strip().str.upper().str.replace(r'\s+', ' ', regex=True)
    df['bill_split'] = (df['LandValue'] * LAND_MILLS + df['ImprovedValue'] * BLDG_MILLS) / 1000
    single = df['bill_split'].sum() * 1000 / df['TotalValue'].sum()
    df['bill_single'] = df['TotalValue'] * single / 1000
    df['chg'] = df['bill_single'] - df['bill_split']
    df.attrs['single_mills'] = single
    return df


def money(x: float) -> str:
    return f'-${-x:,.0f}' if x < 0 else f'${x:,.0f}'


def pct(x: float) -> str:
    return f'{x:+.1%}' if abs(x) < 0.01 else f'{x:+.0%}'


def table(rows: List[List[str]], head: List[str]) -> str:
    out = ['| ' + ' | '.join(head) + ' |', '|' + '|'.join(['---'] * len(head)) + '|']
    out += ['| ' + ' | '.join(r) + ' |' for r in rows]
    return '\n'.join(out)


def group_row(g: pd.DataFrame) -> List[str]:
    split, chg = g['bill_split'].sum(), g['chg'].sum()
    land_share = g['LandValue'].sum() / max(g['TotalValue'].sum(), 1)
    return [f'{len(g):,}', f'{land_share:.0%}', money(split), money(chg), pct(chg / split)]


def build(df: pd.DataFrame) -> Dict[str, str]:
    levy = df['bill_split'].sum()
    land_tax = (df['LandValue'] * LAND_MILLS / 1000).sum()
    res = df[df['cls'] == 'R']
    res_pct = res['chg'] / res['bill_split']
    by_owner = df.groupby('own')['chg'].sum().sort_values()
    ranks = {o: i + 1 for i, o in enumerate(by_owner.index)}

    k: Dict[str, str] = {}
    k['LEVY_CHECK'] = f'{money(levy)} ({levy / LEVY_2025 - 1:+.2%} against the ordinance)'
    k['SINGLE_MILLS'] = f'{df.attrs["single_mills"]:.2f}'
    k['LAND_SHARE_AV'] = f'{df["LandValue"].sum() / df["TotalValue"].sum():.0%}'
    k['LAND_SHARE_TAX'] = f'{land_tax / levy:.0%}'
    k['SHARE_FALL'] = f'{(df["chg"] < 0).mean():.0%}'
    k['RES_MEDIAN'] = f'{pct(res_pct.median())} ({money(res["chg"].median())})'
    k['RES_SPREAD'] = f'{pct(res_pct.quantile(0.1))} to {pct(res_pct.quantile(0.9))}'
    k['N_OWNERS'] = f'{len(by_owner):,}'

    head = ['Parcels', 'Land share of value', '2025 split bill', 'Change under one rate', '%']
    rows = []
    for c, g in sorted(df.groupby('cls'), key=lambda t: -t[1]['bill_split'].sum()):
        if len(g) >= 5:
            rows.append([CLASS_NAMES.get(c, c)] + group_row(g))
    k['BY_CLASS'] = table(rows, ['Class'] + head)

    rows = []
    for o in by_owner.index[:15]:
        g = df[df['own'] == o]
        rows.append([str(ranks[o]), o] + group_row(g))
    k['TOP_OWNERS'] = table(rows, ['Rank', 'Owner (as on roll)'] + head)

    rows = []
    for label, pat in NAMED:
        g = df[df['own'].str.contains(pat, regex=True, na=False)]
        if len(g):
            r = ranks[g['own'].iloc[0]] if g['own'].nunique() == 1 else None
            rows.append([label, str(r) if r else 'several owners'] + group_row(g))
    k['NAMED_OWNERS'] = table(rows, ['Owner', 'Rank'] + head)

    rows = []
    for label, pat, note in OFFICIALS:
        g = df[df['own'].str.contains(pat, regex=True, na=False)]
        rows.append([label, note] + (group_row(g) if len(g) else ['0', '', '', '', '']))
    k['OFFICIALS'] = table(rows, ['Official', 'Match'] + head)
    return k


def side_yards() -> Tuple[int, int, float, float]:
    """Taxable vacant lots (under 1 acre) touching a built lot with the same owner name.

    Returns (lots, side-yard lots, acres, side-yard acres). Owner names are
    matched as written, so a side yard held under a different spelling is
    missed and the count is a floor.
    """
    v = pd.read_csv(IDLE_PARCELS, dtype={'ParcelPIN': str})
    v = v[(v['category'] == 'vacant') & v['taxable']].set_index('ParcelPIN')
    r = gpd.read_parquet(ROLL, columns=['ParcelPIN', 'OwnerName', 'ImprovedValue', 'geometry'])
    r['ImprovedValue'] = pd.to_numeric(r['ImprovedValue'], errors='coerce').fillna(0)
    r = r.to_crs('EPSG:2271')  # PA State Plane North, feet
    r['own'] = r['OwnerName'].str.strip().str.upper()
    lots = r[r['ParcelPIN'].isin(v.index)].dissolve('ParcelPIN', aggfunc='first').reset_index()
    lots['geometry'] = lots.buffer(SIDE_YARD_FT)
    built = r.loc[r['ImprovedValue'] > 0, ['own', 'geometry']]
    j = gpd.sjoin(lots[['ParcelPIN', 'own', 'geometry']], built, predicate='intersects')
    side = v.index.isin(j.loc[j['own_left'] == j['own_right'], 'ParcelPIN'].unique())
    return len(v), int(side.sum()), float(v['acres'].sum()), float(v.loc[side, 'acres'].sum())


def vacant_claim() -> Dict[str, str]:
    """The budget's vacant-land claim against both cities' idle-land shares."""
    comp = json.loads(COMPARISON.read_text(encoding='utf-8'))
    cats, taxable = comp['categories'], comp['taxable_acres']
    order = ['parking', 'paved', 'vacant', 'urban', 'tract', 'all_idle']
    labels = {'urban': '**Urban idle land** (the three above)', 'all_idle': '**All idle land**',
              'tract': 'Undeveloped tract (1 acre or more)'}
    rows = []
    for c in order:
        cell = lambda city: f"{cats[c][city + '_pct'] / 100:.1%} ({cats[c][city + '_acres']:,.0f} acres)"
        rows.append([labels.get(c, cats[c]['label']), cell('philadelphia'), cell('scranton')])

    vp = {city: (cats['vacant'][city + '_acres'] + cats['parking'][city + '_acres']) / taxable[city]
          for city in ['philadelphia', 'scranton']}
    n, n_side, ac, ac_side = side_yards()
    sc_filtered = vp['scranton'] - ac_side / taxable['scranton']

    k: Dict[str, str] = {}
    k['IDLE_TABLE'] = table(rows, ['Share of taxable land', 'Philadelphia', 'Scranton'])
    k['IDLE_URBAN_SC'] = f"{cats['urban']['scranton_pct'] / 100:.1%}"
    k['IDLE_URBAN_PH'] = f"{cats['urban']['philadelphia_pct'] / 100:.1%}"
    k['IDLE_VP_SC'] = f"{vp['scranton']:.1%}"
    k['IDLE_VP_PH'] = f"{vp['philadelphia']:.1%}"
    k['IDLE_VP_RATIO'] = f"{vp['scranton'] / vp['philadelphia']:.1f}"
    k['SIDE_YARD_LOTS'] = f'{n_side:,} of {n:,}'
    k['SIDE_YARD_ACRES'] = f'{ac_side:,.0f} of {ac:,.0f}'
    k['IDLE_VP_SC_FILTERED'] = f'{sc_filtered:.1%}'
    k['IDLE_VP_RATIO_FILTERED'] = f"{sc_filtered / vp['philadelphia']:.1f}"
    k['SIDE_YARD_FT'] = str(SIDE_YARD_FT)
    return k


def fill(text: str, keys: Dict[str, str]) -> str:
    for key, val in keys.items():
        pat = re.compile(rf'(<!--AUTOGEN:{key}-->)(.*?)(<!--/AUTOGEN:{key}-->)', re.S)
        if not pat.search(text):
            raise SystemExit(f'marker {key} missing from {MEMO.name}')
        block = '\n' in val
        text = pat.sub(lambda m: m.group(1) + (f'\n{val}\n' if block else val) + m.group(3), text)
    return text


def main() -> None:
    keys = {**build(load()), **vacant_claim()}
    old = MEMO.read_text(encoding='utf-8')
    new = fill(old, keys)
    if '--check' in sys.argv:
        if new != old:
            sys.stdout.writelines(difflib.unified_diff(old.splitlines(True), new.splitlines(True)))
            sys.exit(1)
        print(f'{MEMO.name} is current')
        return
    MEMO.write_text(new, encoding='utf-8')
    print(f'wrote {MEMO.name}')


if __name__ == '__main__':
    main()
