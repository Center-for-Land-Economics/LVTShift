"""Build NYC LVT comparison summary tables (CSV + markdown) and PNG charts 01-06."""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter

CITY_DIR = Path(__file__).resolve().parent
REPO_ROOT = CITY_DIR.parent.parent
REPORT = REPO_ROOT / "analysis" / "reports" / "new_york_city"
REPORT.mkdir(parents=True, exist_ok=True)

df = pd.read_parquet(CITY_DIR / "data" / "model_output.parquet")
summary = json.load(open(CITY_DIR / "data" / "summary.json"))
SCEN = {"A": "new_tax_A", "B": "new_tax_B", "C": "new_tax_C"}
PURPLE, TEAL, GRAY = "#352977", "#2FA39B", "#8A8791"


def breakdown(group_col: str) -> pd.DataFrame:
    tax = df[df["current_tax"] > 0].copy()
    rows = []
    for gname, g in tax.groupby(group_col):
        rec = {group_col: gname, "n_taxable": len(g), "current_tax": g["current_tax"].sum()}
        for s, col in SCEN.items():
            chg = g[col] - g["current_tax"]
            pct = np.where(g["current_tax"] > 0, chg / g["current_tax"], 0.0)
            rec[f"{s}_new_tax"] = g[col].sum()
            rec[f"{s}_pct_more"] = float((pct > 0.01).mean() * 100)
            rec[f"{s}_pct_less"] = float((pct < -0.01).mean() * 100)
            rec[f"{s}_median_chg"] = float(chg.median())
            rec[f"{s}_mean_chg"] = float(chg.mean())
        rows.append(rec)
    return pd.DataFrame(rows).sort_values("current_tax", ascending=False)

for col, name in [("TAXCLASS", "by_class"), ("borough", "by_borough"), ("PROPERTY_CATEGORY", "by_category")]:
    breakdown(col).to_csv(REPORT / f"winners_losers_{name}.csv", index=False)
    print(f"wrote winners_losers_{name}.csv")

cat = df.groupby("PROPERTY_CATEGORY").agg(n=("parid", "size"), current_tax=("current_tax", "sum"),
                                          FULLVAL=("FULLVAL", "sum"))
cat["eff_multiplier_pct"] = np.where(cat["FULLVAL"] > 0, cat["current_tax"] / cat["FULLVAL"] * 100, np.nan)
cat = cat.sort_values("current_tax", ascending=False)
cat.to_csv(REPORT / "effective_multiplier_by_category.csv")

mlines = ["# Effective multiplier (current_tax / FULLVAL) by category\n",
          "| Property category | Parcels | Current tax | Market value (FULLVAL) | Eff. multiplier |",
          "|---|---:|---:|---:|---:|"]
for idx, r in cat.iterrows():
    mlines.append(f"| {idx} | {int(r['n']):,} | ${r['current_tax']:,.0f} | ${r['FULLVAL']:,.0f} | {r['eff_multiplier_pct']:.3f}% |")
(REPORT / "effective_multiplier_by_category.md").write_text("\n".join(mlines))

# ---- SUMMARY.md ----
s = summary
lines = ["# NYC LVT Comparison — Summary\n", "## Revenue gate\n",
         f"- Modeled ${s['modeled_levy']:,.0f}; Official FY2026 ${s['official_levy']:,.0f}; "
         f"Gap {s['revenue_gap_pct']:+.2f}% ({'PASS' if abs(s['revenue_gap_pct'])<5 else 'FAIL'})",
         f"- FY2026 final class rates: " + ", ".join(f"C{k} {v:.3f}%" for k, v in s['class_rates_pct'].items()) + "\n",
         "## Class revenue shares\n", "| Class | Share |", "|---|---|"]
for k in ["1", "2", "3", "4"]:
    lines.append(f"| {k} | {s['class_shares_pct'][k]:.2f}% |")
lines += ["\n## Solved split-rate millages (per $1,000, 3:1 land:building)\n",
          "| Scenario | Class | Land | Building |", "|---|---|---|---|"]
for c in ["1", "2", "3", "4"]:
    r = s['rates_A'][c]; lines.append(f"| A within-class (assessed AV) | {c} | {r['land']:.3f} | {r['bldg']:.3f} |")
b = s['rates_B']; lines.append(f"| B declassification (TRUE MARKET) | (all) | {b['land']:.4f} | {b['bldg']:.4f} |")
for c in ["1", "2", "3", "4"]:
    r = s['rates_C'][c]; lines.append(f"| C 581-repeal (assessed AV) | {c} | {r['land']:.3f} | {r['bldg']:.3f} |")
cf = s['condo_fix']
lines += ["\n> Scenario B is a FULL declassification: base = true market value per parcel (Class 2",
          "> condos/co-ops §581-uplifted), no class or assessment-ratio distinction. Its millage is on",
          "> true market (~$2.1T base), so it is NOT comparable to A/C millages (on assessed AV ~$300B).\n",
          "## Scenario C — RPTL 581 repeal (Class 2 condos & co-ops at true market)\n",
          f"- Condominium ${cf['condo_current']:,.0f} -> ${cf['condo_C']:,.0f} ({(cf['condo_C']/cf['condo_current']-1)*100:+.1f}%)",
          f"- Co-op ${cf['coop_current']:,.0f} -> ${cf['coop_C']:,.0f} ({(cf['coop_C']/cf['coop_current']-1)*100:+.1f}%)",
          f"- Class-2 Rentals ${cf['rentals_current']:,.0f} -> ${cf['rentals_C']:,.0f} ({(cf['rentals_C']/cf['rentals_current']-1)*100:+.1f}%)"]
(REPORT / "SUMMARY.md").write_text("\n".join(lines))
print("wrote SUMMARY.md + effective_multiplier tables")

# ---- CHARTS ----
plt.rcParams.update({"font.family": "DejaVu Sans", "axes.spines.top": False, "axes.spines.right": False,
                     "figure.dpi": 130, "axes.titlesize": 13, "axes.titleweight": "bold"})

# 01 effective multiplier by category
c1 = cat[cat["n"] > 500].sort_values("eff_multiplier_pct")
colors = [TEAL if i in ("Single Family Residential", "Small Multi-Family (2-4 units)") else
          (PURPLE if i in ("Condominium", "Co-op") else GRAY) for i in c1.index]
fig, ax = plt.subplots(figsize=(9, 6))
ax.barh(c1.index, c1["eff_multiplier_pct"], color=colors)
ax.set_xlabel("Effective tax rate (% of DOF market value)")
ax.set_title("NYC effective property-tax rate by property type (FY2026)")
ax.xaxis.set_major_formatter(PercentFormatter(decimals=1))
for y, v in enumerate(c1["eff_multiplier_pct"]):
    ax.text(v + 0.03, y, f"{v:.2f}%", va="center", fontsize=8)
ax.text(0.98, 0.02, "Condo/Co-op 'market value' = RPTL 581 income value (depressed)",
        transform=ax.transAxes, ha="right", fontsize=8, style="italic", color="#555")
fig.tight_layout(); fig.savefig(REPORT / "01_effective_multiplier_by_category.png"); plt.close(fig)

# 02 per-class solved millages A vs C (assessed AV base; B omitted — different base)
fig, ax = plt.subplots(figsize=(9, 5.5))
classes = ["1", "2", "3", "4"]; x = np.arange(len(classes)); w = 0.2
la = [summary["rates_A"][c]["land"] for c in classes]; ba = [summary["rates_A"][c]["bldg"] for c in classes]
lc = [summary["rates_C"][c]["land"] for c in classes]; bc = [summary["rates_C"][c]["bldg"] for c in classes]
ax.bar(x - 1.5*w, la, w, label="A land", color=PURPLE)
ax.bar(x - 0.5*w, ba, w, label="A building", color="#B7A9E0")
ax.bar(x + 0.5*w, lc, w, label="C land", color=TEAL)
ax.bar(x + 1.5*w, bc, w, label="C building", color="#A9DED9")
ax.set_xticks(x); ax.set_xticklabels([f"Class {c}" for c in classes])
ax.set_ylabel("Millage (per $1,000 taxable AV)")
ax.set_title("Solved 3:1 split-rate millages by class (Scenarios A & C, assessed-AV base)")
ax.legend(ncol=2, fontsize=8)
fig.tight_layout(); fig.savefig(REPORT / "02_solved_millages_by_class.png"); plt.close(fig)

# 03 who wins/loses by category (Scenario B)
bd_cat = breakdown("PROPERTY_CATEGORY"); bd_cat = bd_cat[bd_cat["n_taxable"] > 1000].sort_values("B_pct_less")
fig, ax = plt.subplots(figsize=(9, 7)); y = np.arange(len(bd_cat))
ax.barh(y, bd_cat["B_pct_less"], color=TEAL, label="% pay less")
ax.barh(y, -bd_cat["B_pct_more"], color=PURPLE, label="% pay more")
ax.set_yticks(y); ax.set_yticklabels(bd_cat["PROPERTY_CATEGORY"], fontsize=8)
ax.axvline(0, color="k", lw=0.8)
ax.set_xlabel("Share of parcels  (<- pay more | pay less ->)")
ax.set_title("Scenario B (declassification, true market): who wins / loses by property type")
ax.xaxis.set_major_formatter(lambda v, _: f"{abs(int(v))}%")
ax.legend(fontsize=9, loc="lower right")
fig.tight_layout(); fig.savefig(REPORT / "03_winners_losers_by_category_scenB.png"); plt.close(fig)

# 04 borough mean % change per scenario
tax = df[df["current_tax"] > 0].copy()
rows = []
for b, g in tax.groupby("borough"):
    for sname, col in SCEN.items():
        rows.append({"borough": b, "scenario": sname, "mean_pct": ((g[col]-g["current_tax"])/g["current_tax"]).mean()*100})
bdf = pd.DataFrame(rows).pivot(index="borough", columns="scenario", values="mean_pct").reindex(
    ["Manhattan", "Bronx", "Brooklyn", "Queens", "Staten Island"])
fig, ax = plt.subplots(figsize=(9, 5.5)); x = np.arange(len(bdf)); w = 0.26
for i, (sname, c) in enumerate([("A", PURPLE), ("B", TEAL), ("C", GRAY)]):
    ax.bar(x + (i-1)*w, bdf[sname], w, label=f"Scenario {sname}", color=c)
ax.axhline(0, color="k", lw=0.8); ax.set_xticks(x); ax.set_xticklabels(bdf.index)
ax.set_ylabel("Mean parcel tax change (%)"); ax.set_title("Average parcel tax change by borough and scenario")
ax.legend(fontsize=9)
fig.tight_layout(); fig.savefig(REPORT / "04_borough_change_by_scenario.png"); plt.close(fig)

# 05 total tax by class current vs A/B/C
byclass = df.groupby("TAXCLASS").agg(current=("current_tax", "sum"), A=("new_tax_A", "sum"),
                                     B=("new_tax_B", "sum"), C=("new_tax_C", "sum")).reindex(["1", "2", "3", "4"])
fig, ax = plt.subplots(figsize=(9, 5.5)); x = np.arange(len(byclass)); w = 0.2
for i, (sname, c) in enumerate([("current", "#111"), ("A", PURPLE), ("B", TEAL), ("C", GRAY)]):
    ax.bar(x + (i-1.5)*w, byclass[sname]/1e9, w, label=sname, color=c)
ax.set_xticks(x); ax.set_xticklabels([f"Class {c}" for c in byclass.index])
ax.set_ylabel("Total tax ($ billions)"); ax.set_title("Total tax by class: current vs A / B / C")
ax.legend(fontsize=9)
fig.tight_layout(); fig.savefig(REPORT / "05_total_tax_by_class.png"); plt.close(fig)

# 06 Scenario C condo/coop direction
c2 = df[df["TAXCLASS"] == "2"].copy()
grp = {"Condominium": c2["PROPERTY_CATEGORY"] == "Condominium", "Co-op": c2["PROPERTY_CATEGORY"] == "Co-op",
       "Multi-Family\nRental": c2["PROPERTY_CATEGORY"] == "Multi-Family Rental (Class 2)"}
labels = list(grp.keys())
cur = [c2.loc[m, "current_tax"].sum()/1e9 for m in grp.values()]
newC = [c2.loc[m, "new_tax_C"].sum()/1e9 for m in grp.values()]
fig, ax = plt.subplots(figsize=(8, 5.5)); x = np.arange(len(labels)); w = 0.38
ax.bar(x - w/2, cur, w, label="Current", color=GRAY)
ax.bar(x + w/2, newC, w, label="Scenario C (581 repeal)", color=PURPLE)
for i, (a, bb) in enumerate(zip(cur, newC)):
    ax.text(i - w/2, a + 0.05, f"${a:.1f}B", ha="center", fontsize=8)
    ax.text(i + w/2, bb + 0.05, f"${bb:.1f}B", ha="center", fontsize=8, color=PURPLE)
ax.set_xticks(x); ax.set_xticklabels(labels); ax.set_ylabel("Total tax ($ billions)")
ax.set_title("Scenario C (RPTL 581 repeal): condos & co-ops pay more,\nClass-2 rentals pay less (revenue-neutral within Class 2)")
ax.legend(fontsize=9)
fig.tight_layout(); fig.savefig(REPORT / "06_scenarioC_condo_coop_direction.png"); plt.close(fig)
print("wrote charts 01-06")
print("borough mean % change:\n", bdf.round(1).to_string())
