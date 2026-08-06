"""Scrape VGSI (Vision) Gorham, ME parcel pages for use codes + assessed values.

The Maine statewide ADB has land/building values but NO use codes for Gorham
(LAND_USE is blank for all parcels). VGSI is the town's authoritative CAMA system
and carries the Use Code, Zone, and per-year assessment. We pull it, cache it, and
later join it to the state geometry layer on MAP_BK_LOT <-> Mblu.

Output: cities/gorham/data/vgsi.parquet
"""
import re
import time
import concurrent.futures as cf
import urllib.request
from pathlib import Path

import pandas as pd

DATA = Path(__file__).resolve().parent / "data"
DATA.mkdir(exist_ok=True)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
URL = "https://gis.vgsi.com/gorhamme/Parcel.aspx?pid={pid}"
MAX_PID = 5600  # confirmed: 5400 exists, 5600 is a 302 (not found)


def label(html, lid):
    m = re.search(rf'id="MainContent_{lid}"[^>]*>(.*?)</(?:span|td)>', html, re.S)
    if not m:
        return None
    return re.sub(r"<[^>]+>", "", m.group(1)).strip() or None


def money(s):
    if not s:
        return None
    s = s.replace("$", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def parse(pid, html):
    # Assessment grid: latest row = [year, improvements, land, total]
    impr = land = total = None
    m = re.search(r'grdCurrentValueAsmt.*?</table>', html, re.S)
    if m:
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r'<td[^>]*>(.*?)</td>', m.group(0), re.S)]
        # cells come in groups of 4; take the first data row (latest year)
        if len(cells) >= 4:
            impr, land, total = money(cells[1]), money(cells[2]), money(cells[3])
    return {
        "pid": pid,
        "acct": label(html, "lblAcctNum"),
        "mblu": label(html, "lblMblu"),
        "location": label(html, "lblLocation"),
        "owner": label(html, "lblGenOwner"),
        "use_code": label(html, "lblUseCode"),
        "use_desc": label(html, "lblUseCodeDescription"),
        "zone": label(html, "lblZone"),
        "nbhd": label(html, "lblNbhd"),
        "acres": label(html, "lblLndSize"),
        "vg_impr": impr,
        "vg_land": land,
        "vg_total": total,
    }


def fetch(pid):
    req = urllib.request.Request(URL.format(pid=pid), headers={"User-Agent": UA})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                if r.status != 200:
                    return None
                html = r.read().decode("utf-8", errors="ignore")
            if "MainContent_lblLocation" not in html:
                return None
            return parse(pid, html)
        except Exception:
            if attempt == 2:
                return None
            time.sleep(1.0 + attempt)
    return None


def main():
    rows = []
    pids = list(range(1, MAX_PID + 1))
    done = 0
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        for res in ex.map(fetch, pids):
            done += 1
            if res:
                rows.append(res)
            if done % 500 == 0:
                print(f"  processed {done}/{len(pids)}, kept {len(rows)}", flush=True)
    df = pd.DataFrame(rows).sort_values("pid").reset_index(drop=True)
    df.to_parquet(DATA / "vgsi.parquet")
    print(f"\nDONE. {len(df):,} parcels scraped -> {DATA / 'vgsi.parquet'}")
    print("\nUse code x description (top 30):")
    print(df.groupby(["use_code", "use_desc"]).size().sort_values(ascending=False).head(30))
    print(f"\nTotal VGSI assessed: ${df.vg_total.sum():,.0f}")
    print(f"Land ${df.vg_land.sum():,.0f}  Impr ${df.vg_impr.sum():,.0f}")


if __name__ == "__main__":
    main()
