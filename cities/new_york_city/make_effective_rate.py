"""Effective tax rate BY CLASS on TRUE market value (§581-uplifted condos/co-ops).
Writes effective_rate_by_class.{csv,md} + 10_effective_rate_by_class.png."""
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

CITY_DIR = Path(__file__).resolve().parent
REPO_ROOT = CITY_DIR.parent.parent
REPORT = REPO_ROOT / "analysis" / "reports" / "new_york_city"
PURPLE, TEAL, GRAY = "#352977", "#2FA39B", "#8A8791"

CLASS_RATE = {"1": 19.843, "2": 12.439, "3": 11.108, "4": 10.848}  # FY2026 final, %
ASSESS_RATIO = {"1": 6.0, "2": 45.0, "3": 45.0, "4": 45.0}         # NYC target %, confirmed vs roll
CONDO_UPLIFT, COOP_UPLIFT = 1.0 / 0.18, 1.0 / 0.23

df = pd.read_parquet(CITY_DIR / "data" / "model_output.parquet")
# true market: §581 uplift for Class 2 condos/co-ops
uplift = pd.Series(1.0, index=df.index)
uplift[(df["PROPERTY_CATEGORY"] == "Condominium") & (df["TAXCLASS"] == "2")] = CONDO_UPLIFT
uplift[(df["PROPERTY_CATEGORY"] == "Co-op") & (df["TAXCLASS"] == "2")] = COOP_UPLIFT
df["true_market"] = df["FULLVAL"] * uplift

rows = []
for c in ["1", "2", "3", "4"]:
    g = df[df["TAXCLASS"] == c]
    tm, tax = g["true_market"].sum(), g["current_tax"].sum()
    rows.append({
        "Class": c,
        "assessment_ratio_pct": ASSESS_RATIO[c],
        "class_tax_rate_pct": CLASS_RATE[c],
        "nominal_effective_pct": ASSESS_RATIO[c] / 100 * CLASS_RATE[c],
        "actual_effective_on_true_market_pct": tax / tm * 100 if tm else np.nan,
        "current_tax": tax, "true_market": tm,
    })
tbl = pd.DataFrame(rows)
tbl.to_csv(REPORT / "effective_rate_by_class.csv", index=False)

# Class-2 breakdown: rentals vs condos/co-ops
c2 = df[df["TAXCLASS"] == "2"]
sub = {"Class 2 — condos/co-ops": c2["PROPERTY_CATEGORY"].isin(["Condominium", "Co-op"]),
       "Class 2 — rentals & other": ~c2["PROPERTY_CATEGORY"].isin(["Condominium", "Co-op"])}
c2rows = []
for label, m in sub.items():
    gg = c2[m]; c2rows.append((label, gg["current_tax"].sum() / gg["true_market"].sum() * 100))

md = ["# Effective tax rate by class — on TRUE market value (FY2026)\n",
      "Effective rate = current tax / true market value. Class 2 condos/co-ops use the §581 uplift",
      "(condo ÷0.18, co-op ÷0.23) so their market value is not the depressed as-if-rental figure.",
      "'Nominal' = assessment ratio × class tax rate; 'Actual' is lower than nominal for Class 1 because",
      "the 6%/20% growth caps hold billable AV below the 6% target, and exemptions further reduce it.\n",
      "| Class | Assessment ratio | Class tax rate | Nominal (ratio×rate) | Actual on true market |",
      "|---|---:|---:|---:|---:|"]
for r in rows:
    md.append(f"| {r['Class']} | {r['assessment_ratio_pct']:.0f}% | {r['class_tax_rate_pct']:.3f}% | "
              f"{r['nominal_effective_pct']:.3f}% | {r['actual_effective_on_true_market_pct']:.3f}% |")
md += ["\n## Class 2 breakdown (effective rate on true market)\n"]
for label, v in c2rows:
    md.append(f"- {label}: {v:.3f}%")
md += ["\nAssessment ratios confirmed against the roll: Class 1 actual assessed = 6.0% of market;",
       "Classes 2/3/4 = 45%. Class 1's *effective* ratio is below 6% due to the growth caps."]
(REPORT / "effective_rate_by_class.md").write_text("\n".join(md))

# Chart 10: actual effective rate on true market by class
fig, ax = plt.subplots(figsize=(8, 5))
classes = ["1", "2", "3", "4"]
vals = [tbl.set_index("Class").loc[c, "actual_effective_on_true_market_pct"] for c in classes]
names = ["Class 1\n(1-3 fam homes)", "Class 2\n(apartments,\ncondos, co-ops)", "Class 3\n(utilities)", "Class 4\n(commercial)"]
colors = [TEAL, PURPLE, GRAY, PURPLE]
ax.bar(range(4), vals, color=colors)
ax.set_xticks(range(4)); ax.set_xticklabels(names, fontsize=9)
ax.set_ylabel("Effective tax rate (% of true market value)")
ax.set_title("NYC effective property-tax rate by class (FY2026, on true market)")
for i, v in enumerate(vals):
    ax.text(i, v + 0.03, f"{v:.2f}%", ha="center", fontsize=10, fontweight="bold")
ax.text(0.98, 0.95, "Homes 0.67% & condos/co-ops 0.76% pay far less per $ of market\n"
        "than rentals (2.9%), commercial (2.3%), utilities (4.8%).\n"
        "Class 2 condos/co-ops §581-uplifted; actual < nominal (ratio×rate) due to caps/exemptions",
        transform=ax.transAxes, ha="right", va="top", fontsize=7.5, style="italic", color="#555")
ax.spines[["top", "right"]].set_visible(False)
fig.tight_layout(); fig.savefig(REPORT / "10_effective_rate_by_class.png"); plt.close(fig)

print(tbl.round(3).to_string(index=False))
print("Class 2 split:", [(l, round(v, 3)) for l, v in c2rows])
print("wrote effective_rate_by_class.{csv,md} + 10_effective_rate_by_class.png")
