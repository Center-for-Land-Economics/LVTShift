"""NYC LVT comparison model — three revenue-neutral 3:1 split-rate scenarios.

Baseline: FY2026 final roll (DOF PVAD 8y4t-faws, year=2026, period=3).
current_tax = net billable taxable AV (curtxbtot - curtxbextot) x FY2026 class rate.
Scenarios (all revenue-neutral, all 3:1 land:building):
  A - within current classes (independent solve per tax class)
  B - FULL DECLASSIFICATION at true market value (no classes, no assessment-ratio distinction)
  C - within classes + RPTL 581 repeal (condos/co-ops at true market, within Class 2)
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

CITY_DIR = Path(__file__).resolve().parent
REPO_ROOT = CITY_DIR.parent.parent
sys.path.insert(0, str(REPO_ROOT))
from lvt.lvt_utils import model_split_rate_tax

DATA = CITY_DIR / "data" / "pvad_2026_final.parquet"

CLASS_RATE = {"1": 0.19843, "2": 0.12439, "3": 0.11108, "4": 0.10848}  # FY2026 final (adopted 2025-10-29)
OFFICIAL_LEVY = 37_976_587_244
BORO_NAME = {"1": "Manhattan", "2": "Bronx", "3": "Brooklyn", "4": "Queens", "5": "Staten Island"}
RATIO = 3.0

df = pd.read_parquet(DATA)
numc = ["curmkttot", "curmktland", "curactland", "curacttot", "curtxbtot",
        "curtxbextot", "curtrntot", "land_area", "gross_sqft", "units"]
for c in numc:
    df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)

df["TAXCLASS"] = df["curtaxclass"].str[0]
df["borough"] = df["boro"].map(BORO_NAME)
df["FULLVAL"] = df["curmkttot"]

# ---- Baseline current tax ----
df["net_taxable"] = (df["curtxbtot"] - df["curtxbextot"]).clip(lower=0)
land_frac = np.where(df["curacttot"] > 0, df["curactland"] / df["curacttot"], 0.0)
land_frac = np.clip(land_frac, 0.0, 1.0)
df["taxable_land"] = df["net_taxable"] * land_frac
df["taxable_building"] = df["net_taxable"] * (1.0 - land_frac)
df["rate"] = df["TAXCLASS"].map(CLASS_RATE).astype(float)
df["current_tax"] = df["net_taxable"] * df["rate"]

modeled = df["current_tax"].sum()
gap = modeled / OFFICIAL_LEVY - 1.0
print("=== REVENUE GATE ===")
print(f"Modeled ${modeled:,.0f}  Official ${OFFICIAL_LEVY:,.0f}  Gap {gap*100:+.2f}%  {'PASS' if abs(gap)<0.05 else 'FAIL'}")

# ---- Property category mapping ----
COOP = {"C6", "C8", "D0", "D4"}
CONDO_RES = {"R0", "R1", "R2", "R3", "R4", "R6", "R9", "RR"}
CONDO_COM = {"R5", "R7", "R8", "RA", "RB", "RG", "RH", "RK", "RP", "RS", "RT", "RW"}


def categorize(bc: str) -> str:
    bc = (bc or "").strip().upper()
    b0 = bc[:1]
    if bc in COOP:
        return "Co-op"
    if bc in CONDO_RES:
        return "Condominium"
    if bc in CONDO_COM:
        return "Office / Commercial Condo" if bc in {"R5", "R7", "R8", "RB"} else "Other Commercial"
    if b0 == "A":
        return "Single Family Residential"
    if b0 == "B":
        return "Small Multi-Family (2-4 units)"
    if bc == "C0":
        return "Small Multi-Family (2-4 units)"
    if b0 in {"C", "D"}:
        return "Multi-Family Rental (Class 2)"
    if b0 == "S":
        return "Mixed Use"
    if b0 == "K":
        return "Retail / General Commercial"
    if b0 == "O":
        return "Office / Commercial Condo"
    if b0 in {"E", "F"}:
        return "Industrial"
    if b0 == "G":
        return "Transportation - Parking"
    if b0 == "H":
        return "Hotel"
    if b0 == "V":
        return "Vacant Land"
    if b0 == "U":
        return "Utility (Class 3)"
    if b0 in {"W", "M", "I", "J", "N", "P", "Q", "Y", "T", "L"}:
        return "Other Institutional / Exempt"
    return "Other"


df["PROPERTY_CATEGORY"] = df["bldg_class"].map(categorize)
df.loc[(df["taxable_building"] <= 0) & (df["taxable_land"] > 0) & (df["bldg_class"].str[:1] == "V"),
       "PROPERTY_CATEGORY"] = "Vacant Land"

# ---- Diagnostic 1: effective multiplier by category ----
print("\n=== DIAGNOSTIC 1: effective multiplier (current_tax / FULLVAL) by category ===")
cat = df.groupby("PROPERTY_CATEGORY").agg(n=("parid", "size"), tax=("current_tax", "sum"), mkt=("FULLVAL", "sum"))
cat["eff_mult_pct"] = np.where(cat["mkt"] > 0, cat["tax"] / cat["mkt"] * 100, np.nan)
print(cat.sort_values("tax", ascending=False).round(3).to_string())

# ---- Diagnostic 2: class revenue shares ----
cls = df.groupby("TAXCLASS").agg(n=("parid", "size"), tax=("current_tax", "sum"))
cls["share_pct"] = cls["tax"] / cls["tax"].sum() * 100

# ================= SCENARIO A: within current classes =================
df["new_tax_A"] = 0.0
rates_A = {}
for c in ["1", "2", "3", "4"]:
    m = df["TAXCLASS"] == c
    grp = df.loc[m].copy()
    rev = grp["current_tax"].sum()
    lm, im, _rev, out = model_split_rate_tax(grp, "taxable_land", "taxable_building", rev, RATIO)
    df.loc[m, "new_tax_A"] = out["new_tax"].values
    rates_A[c] = (lm, im, rev)

# §581 uplift for Class 2 condos/co-ops (IBO discount): condos ~18% of market, co-ops ~23%.
CONDO_UPLIFT = 1.0 / 0.18   # ~5.5556
COOP_UPLIFT = 1.0 / 0.23    # ~4.3478
condo_c2 = (df["PROPERTY_CATEGORY"] == "Condominium") & (df["TAXCLASS"] == "2")
coop_c2 = (df["PROPERTY_CATEGORY"] == "Co-op") & (df["TAXCLASS"] == "2")
fix_mask = condo_c2 | coop_c2

# ============ SCENARIO B: FULL DECLASSIFICATION at TRUE MARKET VALUE ============
# Remove class rates AND assessment-ratio distinction. Base = true market per parcel, split
# land/building by actual-assessed land fraction, exemptions preserved proportionally. Class 2
# condos/co-ops use §581-uplifted true market. One citywide revenue-neutral 3:1 split-rate.
print("\n=== SCENARIO B (full declassification, true market) ===")
uplift = pd.Series(1.0, index=df.index)
uplift[condo_c2] = CONDO_UPLIFT
uplift[coop_c2] = COOP_UPLIFT
true_market = df["FULLVAL"] * uplift
nonexempt_frac = np.where(df["curtxbtot"] > 0, df["net_taxable"] / df["curtxbtot"], 0.0)
df["tm_base"] = (true_market * nonexempt_frac).astype(float)
df["tm_land"] = df["tm_base"] * land_frac
df["tm_building"] = df["tm_base"] * (1.0 - land_frac)
rev_all = df["current_tax"].sum()
lmB, imB, _revB, outB = model_split_rate_tax(df.copy(), "tm_land", "tm_building", rev_all, RATIO)
df["new_tax_B"] = outB["new_tax"].values
rates_B = (lmB, imB)
print(f"true-market base ${df['tm_base'].sum():,.0f}; solved land {lmB:.4f} / bldg {imB:.4f} per $1,000")

# ============ SCENARIO C v2: repeal RPTL 581 — condos & co-ops at true market (within Class 2) ============
print("\n=== SCENARIO C v2 (RPTL 581 repeal) ===")
print(f"condo x{CONDO_UPLIFT:.4f}, co-op x{COOP_UPLIFT:.4f}; condos {condo_c2.sum():,}, co-ops {coop_c2.sum():,}")
df["adjC_land"] = df["taxable_land"].astype(float)
df["adjC_building"] = df["taxable_building"].astype(float)
df.loc[condo_c2, "adjC_land"] *= CONDO_UPLIFT
df.loc[condo_c2, "adjC_building"] *= CONDO_UPLIFT
df.loc[coop_c2, "adjC_land"] *= COOP_UPLIFT
df.loc[coop_c2, "adjC_building"] *= COOP_UPLIFT
df["new_tax_C"] = 0.0
rates_C = {}
for c in ["1", "2", "3", "4"]:
    m = df["TAXCLASS"] == c
    grp = df.loc[m].copy()
    rev = grp["current_tax"].sum()
    lm, im, _rev, out = model_split_rate_tax(grp, "adjC_land", "adjC_building", rev, RATIO)
    df.loc[m, "new_tax_C"] = out["new_tax"].values
    rates_C[c] = (lm, im, rev)

sfr_mask = df["PROPERTY_CATEGORY"] == "Single Family Residential"
sfr_mult = df.loc[sfr_mask, "current_tax"].sum() / df.loc[sfr_mask, "FULLVAL"].sum()

# ---- Diagnostic 3: direction checks ----
print("\n=== DIAGNOSTIC 3: direction (new B and C vs current) ===")
class1_res = df["TAXCLASS"] == "1"
rentals_c2 = (df["PROPERTY_CATEGORY"] == "Multi-Family Rental (Class 2)") & (df["TAXCLASS"] == "2")
for label, mask, col in [("Class-1 homes (Scenario B)", class1_res, "new_tax_B"),
                         ("SFR (Scenario B)", sfr_mask, "new_tax_B"),
                         ("Class-2 Condo (Scenario C)", condo_c2, "new_tax_C"),
                         ("Class-2 Co-op (Scenario C)", coop_c2, "new_tax_C"),
                         ("Class-2 Rentals (Scenario C)", rentals_c2, "new_tax_C")]:
    sub = df.loc[mask & (df["current_tax"] > 0)]
    cur, new = sub["current_tax"].sum(), sub[col].sum()
    med = (sub[col] - sub["current_tax"]).median()
    print(f"{label:30s}: ${cur:,.0f} -> ${new:,.0f}  ({(new/cur-1)*100:+.1f}% agg, median {med:+,.0f})  "
          f"{'MORE' if new > cur else 'LESS'}")

# ---- Save intermediate ----
keep = ["parid", "borough", "TAXCLASS", "PROPERTY_CATEGORY", "bldg_class", "FULLVAL",
        "net_taxable", "taxable_land", "taxable_building", "current_tax",
        "new_tax_A", "new_tax_B", "new_tax_C"]
df[keep].to_parquet(CITY_DIR / "data" / "model_output.parquet", index=False)

print("\n=== SOLVED RATES (per $1,000 taxable/true-market value) ===")
for c in ["1", "2", "3", "4"]:
    print(f"  A class {c}: land {rates_A[c][0]:.3f} / bldg {rates_A[c][1]:.3f}")
print(f"  B (single, true market): land {lmB:.4f} / bldg {imB:.4f}")
for c in ["1", "2", "3", "4"]:
    print(f"  C class {c}: land {rates_C[c][0]:.3f} / bldg {rates_C[c][1]:.3f}")

# ---- Per-parcel comparison CSV ----
out_csv = REPO_ROOT / "analysis" / "data" / "new_york_city.csv"
out_csv.parent.mkdir(parents=True, exist_ok=True)
csv_df = df[["parid", "borough", "TAXCLASS", "PROPERTY_CATEGORY", "FULLVAL",
             "taxable_land", "taxable_building", "current_tax",
             "new_tax_A", "new_tax_B", "new_tax_C"]].rename(columns={"parid": "BBL"})
for c in ["FULLVAL", "taxable_land", "taxable_building", "current_tax", "new_tax_A", "new_tax_B", "new_tax_C"]:
    csv_df[c] = csv_df[c].round(2)
csv_df.to_csv(out_csv, index=False)
print(f"Saved {out_csv} ({len(csv_df):,} rows)")

# ---- Summary JSON ----
summary = {
    "official_levy": OFFICIAL_LEVY,
    "modeled_levy": float(modeled),
    "revenue_gap_pct": float(gap * 100),
    "class_rates_pct": {k: v * 100 for k, v in CLASS_RATE.items()},
    "sfr_effective_multiplier_pct": float(sfr_mult * 100),
    "rates_A": {c: {"land": rates_A[c][0], "bldg": rates_A[c][1], "revenue": rates_A[c][2]} for c in rates_A},
    "rates_B": {"land": lmB, "bldg": imB, "revenue": float(rev_all),
                "definition": "full declassification at true market value (§581-uplifted condos/co-ops)"},
    "rates_C": {c: {"land": rates_C[c][0], "bldg": rates_C[c][1]} for c in rates_C},
    "class_shares_pct": {c: float(cls.loc[c, "tax"] / cls["tax"].sum() * 100) for c in cls.index},
    "scenario_C_definition": "RPTL 581 repeal — assess Class 2 condos & co-ops at true market",
    "condo_uplift_factor": CONDO_UPLIFT,
    "coop_uplift_factor": COOP_UPLIFT,
    "condo_fix": {
        "condo_current": float(df.loc[condo_c2, "current_tax"].sum()),
        "condo_C": float(df.loc[condo_c2, "new_tax_C"].sum()),
        "coop_current": float(df.loc[coop_c2, "current_tax"].sum()),
        "coop_C": float(df.loc[coop_c2, "new_tax_C"].sum()),
        "rentals_current": float(df.loc[rentals_c2, "current_tax"].sum()),
        "rentals_C": float(df.loc[rentals_c2, "new_tax_C"].sum()),
    },
}
with open(CITY_DIR / "data" / "summary.json", "w") as f:
    json.dump(summary, f, indent=2)
print("Saved model_output.parquet + summary.json")
