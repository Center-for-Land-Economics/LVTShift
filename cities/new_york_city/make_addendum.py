"""Addendum: category net-change table (CSV) + census income/race progressivity charts.
07 category charts are produced separately by make_category_charts.py (canonical style).
"""
import os
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from dotenv import load_dotenv

CITY_DIR = Path(__file__).resolve().parent
REPO_ROOT = CITY_DIR.parent.parent
REPORT = REPO_ROOT / "analysis" / "reports" / "new_york_city"
load_dotenv(REPO_ROOT / ".env")
KEY = os.getenv("CENSUS_API_KEY")
PURPLE, TEAL, GRAY = "#352977", "#2FA39B", "#8A8791"
SCEN = {"A": "new_tax_A", "B": "new_tax_B", "C": "new_tax_C"}
SCEN_TITLE = {"A": "A — within current classes", "B": "B — declassification (true market)",
              "C": "C — RPTL 581 repeal (condos/co-ops at market)"}
plt.rcParams.update({"font.family": "DejaVu Sans", "axes.spines.top": False, "axes.spines.right": False,
                     "figure.dpi": 130, "axes.titlesize": 12, "axes.titleweight": "bold"})

df = pd.read_parquet(CITY_DIR / "data" / "model_output.parquet")
df["BBL"] = df["parid"].astype(str)

# ---- category net-change table (feeds make_category_charts.py) ----
tax = df[df["current_tax"] > 0]
rows = []
for c, g in tax.groupby("PROPERTY_CATEGORY"):
    rec = {"PROPERTY_CATEGORY": c, "n": len(g), "current": g["current_tax"].sum()}
    for s, col in SCEN.items():
        chg = g[col] - g["current_tax"]; pct = chg / g["current_tax"]
        rec[f"{s}_net_change"] = float(g[col].sum() - g["current_tax"].sum())
        rec[f"{s}_median_dollar"] = float(chg.median())
        rec[f"{s}_mean_dollar"] = float(chg.mean())
        rec[f"{s}_agg_pct"] = float((g[col].sum()/g["current_tax"].sum()-1)*100)
        rec[f"{s}_median_pct"] = float(pct.median()*100)
    rows.append(rec)
ct = pd.DataFrame(rows)
ct.to_csv(REPORT / "category_change_by_scenario.csv", index=False)
print("=== category net-change zero-sum check ===")
for s in SCEN:
    print(f"  Scenario {s}: sum net_change = ${ct[f'{s}_net_change'].sum():,.0f}")

# ---- census join ----
tract_map = pd.read_parquet(CITY_DIR / "data" / "bbl_tract.parquet")
COUNTIES = ["005", "047", "061", "081", "085"]
VARS = "B19013_001E,B03002_001E,B03002_003E"
acs_rows = []
for co in COUNTIES:
    u = f"https://api.census.gov/data/2023/acs/acs5?get=NAME,{VARS}&for=tract:*&in=state:36&in=county:{co}&key={KEY}"
    hdr, *data = requests.get(u, timeout=120).json()
    acs_rows += [dict(zip(hdr, r)) for r in data]
acs = pd.DataFrame(acs_rows)
for c in ["B19013_001E", "B03002_001E", "B03002_003E"]:
    acs[c] = pd.to_numeric(acs[c], errors="coerce")
acs.loc[acs["B19013_001E"] < 0, "B19013_001E"] = np.nan
acs["tract_geoid"] = acs["state"] + acs["county"] + acs["tract"]
acs["med_income"] = acs["B19013_001E"]; acs["pop"] = acs["B03002_001E"]
acs["pct_white"] = np.where(acs["pop"] > 0, acs["B03002_003E"] / acs["pop"] * 100, np.nan)
acs = acs[["tract_geoid", "med_income", "pop", "pct_white"]]

# ALL residential structures (incl. Mixed Use = buildings with residential units, S0-S9)
RES = ["Single Family Residential", "Small Multi-Family (2-4 units)", "Condominium", "Co-op",
       "Multi-Family Rental (Class 2)", "Mixed Use"]
res = df[(df["PROPERTY_CATEGORY"].isin(RES)) & (df["current_tax"] > 0)].copy()
tract_map["block6"] = tract_map["BBL"].str[:6]
block_tract = tract_map.groupby("block6")["tract_geoid"].agg(lambda s: s.value_counts().index[0])
res = res.merge(tract_map[["BBL", "tract_geoid"]], on="BBL", how="left")
n_direct = res["tract_geoid"].notna().sum()
res["tract_geoid"] = res["tract_geoid"].fillna(res["BBL"].str[:6].map(block_tract))
n_after = res["tract_geoid"].notna().sum()
res = res.merge(acs, on="tract_geoid", how="left")
n_total = len(res); n_no_inc = res["med_income"].isna().sum()
res_i = res.dropna(subset=["med_income", "pop"]).copy()
res_r = res.dropna(subset=["pct_white", "pop"]).copy()
print(f"tract: direct {n_direct:,} + block-fill {n_after-n_direct:,}; residential {n_total:,}; "
      f"no ACS income {n_no_inc:,}; used income {len(res_i):,}")


def pop_weighted_quintiles(frame, col):
    tr = frame.groupby("tract_geoid").agg(val=(col, "first"), pop=("pop", "first")).dropna().sort_values("val")
    cum = tr["pop"].cumsum() / tr["pop"].sum()
    tr["q"] = np.clip(np.ceil(cum / 0.2), 1, 5).astype(int)
    return frame["tract_geoid"].map(tr["q"])

res_i["income_q"] = pop_weighted_quintiles(res_i, "med_income")
res_r["white_q"] = pop_weighted_quintiles(res_r, "pct_white")


def quintile_table(frame, qcol):
    rows = []
    for q, g in frame.groupby(qcol):
        rec = {"quintile": int(q), "n_parcels": len(g),
               "income_range": f"${g['med_income'].min():,.0f}-${g['med_income'].max():,.0f}" if qcol == "income_q" else "",
               "pct_white_range": f"{g['pct_white'].min():.0f}-{g['pct_white'].max():.0f}%" if qcol == "white_q" else ""}
        for s, col in SCEN.items():
            chg = g[col] - g["current_tax"]; pct = chg / g["current_tax"] * 100
            rec[f"{s}_median_dollar"] = float(chg.median()); rec[f"{s}_mean_dollar"] = float(chg.mean())
            rec[f"{s}_median_pct"] = float(pct.median()); rec[f"{s}_mean_pct"] = float(pct.mean())
        rows.append(rec)
    return pd.DataFrame(rows).sort_values("quintile")

inc_tbl = quintile_table(res_i, "income_q"); race_tbl = quintile_table(res_r, "white_q")
inc_tbl.to_csv(REPORT / "progressivity_income.csv", index=False)
race_tbl.to_csv(REPORT / "progressivity_race.csv", index=False)


# Deck shows Scenario A and C only (B's +100-135% blows out the scale). Tight shared axis over A & C.
CHART_SCEN = ["A", "C"]


def shared_ylim(tbl):
    vals = pd.concat([tbl[f"{s}_median_pct"] for s in CHART_SCEN])
    lo, hi = vals.min(), vals.max()
    return (float(np.floor(lo - 2)), float(np.ceil(hi + 4)))  # extra top room for bar labels

INCOME_YLIM = shared_ylim(inc_tbl); RACE_YLIM = shared_ylim(race_tbl)


def prog_chart(tbl, s, fname, xlab, title, ylim):
    fig, ax = plt.subplots(figsize=(8, 5)); q = tbl["quintile"]; vals = tbl[f"{s}_median_pct"]
    ax.bar(q, vals, color=[TEAL if v < 0 else PURPLE for v in vals])
    ax.axhline(0, color="k", lw=0.8); ax.set_ylim(*ylim)
    ax.set_xticks(q); ax.set_xticklabels([f"Q{int(i)}" for i in q])
    ax.set_xlabel(xlab); ax.set_ylabel("Median parcel tax change (%)"); ax.set_title(title)
    off = (ylim[1] - ylim[0]) * 0.02
    for xi, (v, d) in zip(q, zip(vals, tbl[f"{s}_median_dollar"])):
        ax.text(xi, v + (off if v >= 0 else -off), f"{v:+.1f}%\n(${d:,.0f})",
                ha="center", va="bottom" if v >= 0 else "top", fontsize=7)
    fig.tight_layout(); fig.savefig(REPORT / fname); plt.close(fig)

for s in CHART_SCEN:  # A and C only
    prog_chart(inc_tbl, s, f"08_progressivity_income_scen{s}.png",
               "Neighborhood income quintile (Q1 lowest -> Q5 highest)",
               f"Scenario {SCEN_TITLE[s]}\nResidential tax change by neighborhood income quintile", INCOME_YLIM)
    prog_chart(race_tbl, s, f"09_progressivity_race_scen{s}.png",
               "Tract % non-Hispanic white quintile (Q1 least white -> Q5 most white)",
               f"Scenario {SCEN_TITLE[s]}\nResidential tax change by neighborhood racial composition", RACE_YLIM)
# remove B progressivity charts (excluded from the deck)
for f in ["08_progressivity_income_scenB.png", "09_progressivity_race_scenB.png"]:
    (REPORT / f).unlink(missing_ok=True)
print(f"wrote 08/09 charts for A & C; shared ylim income {INCOME_YLIM}, race {RACE_YLIM}; B charts removed")

methods = f"""# NYC progressivity — methodology

Scope: residential parcels only (Class 1 + Class 2 residential), taxable. {n_total:,} in scope;
income cut used {len(res_i):,}, race cut {len(res_r):,}.
Census: ACS 5-year 2019-2023 (vintage 2023), tract, five NYC counties. Median HH income B19013_001E;
non-Hispanic white share B03002_003E/B03002_001E.
Parcel->tract: NYC PLUTO bct2020; condo unit lots (absent from PLUTO) filled via block->tract
(tracts nest above tax blocks): direct {n_direct:,}, block-fill {n_after-n_direct:,}. Dropped {n_no_inc:,}
(ACS-suppressed income tracts).
Quintiles: POPULATION-WEIGHTED (each ~20% of neighborhood population; parcel counts vary).
Caveats: neighborhood-level not household-level; RENTER PASS-THROUGH — Class 2 rental changes are
landlord-side, not direct tenant relief (esp. Scenario C's rental drop); median is the headline (means
in CSVs run higher, pulled by large condo/co-op parcels).
"""
(REPORT / "progressivity_methods.md").write_text(methods)
print("wrote progressivity_methods.md")
print("\nINCOME median %:\n", inc_tbl[["quintile", "income_range", "A_median_pct", "B_median_pct", "C_median_pct"]].round(1).to_string(index=False))
print("\nRACE median %:\n", race_tbl[["quintile", "pct_white_range", "A_median_pct", "B_median_pct", "C_median_pct"]].round(1).to_string(index=False))
