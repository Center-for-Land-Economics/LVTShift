"""07_category_change_scen{A,B,C}.png in LVT Shift canonical _make_category_chart style,
recolored to CLE brand (purple increase / teal decrease), true net change + parcel counts."""
import sys
from pathlib import Path
import pandas as pd
import matplotlib
matplotlib.use("Agg")

CITY_DIR = Path(__file__).resolve().parent
REPO_ROOT = CITY_DIR.parent.parent
sys.path.insert(0, str(REPO_ROOT))
from lvt.viz import _make_category_chart

REPORT = REPO_ROOT / "analysis" / "reports" / "new_york_city"
PURPLE, TEAL = "#352977", "#2FA39B"
MIN_COUNT = 50
SCEN_TITLE = {"A": "A — within current classes", "B": "B — declassification (true market)",
              "C": "C — RPTL 581 repeal (condos/co-ops at market)"}
ct = pd.read_csv(REPORT / "category_change_by_scenario.csv")

for s in ["A", "B", "C"]:
    summ = pd.DataFrame({
        "PROPERTY_CATEGORY": ct["PROPERTY_CATEGORY"], "property_count": ct["n"],
        "median_tax_change_pct": ct[f"{s}_median_pct"], "median_tax_change": ct[f"{s}_median_dollar"],
        "total_tax_change_dollars": ct[f"{s}_net_change"],
    })
    dropped = summ.loc[summ["property_count"] < MIN_COUNT, "PROPERTY_CATEGORY"].tolist()
    # scale the right-hand Net Change gutter to the widest bar so outlier rows
    # (e.g. Vacant Land +390% under B) don't collide with the Net Change column
    max_abs = summ.loc[summ["property_count"] >= MIN_COUNT, "median_tax_change_pct"].abs().max()
    fig = _make_category_chart(summ, cat_col="PROPERTY_CATEGORY",
                               city_title=f"NYC · Scenario {SCEN_TITLE[s]}", min_count=MIN_COUNT,
                               right_col_pad=max(120.0, float(max_abs) * 1.15))
    for p in fig.axes[0].patches:
        try:
            p.set_facecolor(PURPLE if p.get_width() > 0 else TEAL)
        except Exception:
            pass
    fig.savefig(REPORT / f"07_category_change_scen{s}.png", dpi=130, bbox_inches="tight")
    print(f"07_category_change_scen{s}.png written; dropped (n<{MIN_COUNT}): {dropped or 'none'}")
