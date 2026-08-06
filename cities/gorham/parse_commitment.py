"""Parse the CCRA Gorham 2025-2026 Real Estate Tax Commitment Book from the
`pdftotext -layout` extraction (authoritative, complete, current roll @ 14.800 mills).

Each parcel's value line is: {internalID} {NAME...} {Land} {Building} {Exemption} {Assessment} {Tax}
with the account number ("0001-0001") a few lines below and the Homestead flag on the
"Acres" line. Validated against the book's printed grand Subtotal.

Self-contained: downloads the commitment PDF and runs `pdftotext -layout` (poppler) if the
cached extraction is absent. Output: data/commitment.parquet (next to this script).
"""
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path

import pandas as pd

DATA = Path(__file__).resolve().parent / "data"
DATA.mkdir(exist_ok=True)
PDF = DATA / "re_commit.pdf"
TXT = DATA / "re_commit_layout.txt"
PDF_URL = ("https://www.cumberlandcountyme.gov/Departments/Regional%20Assessing/"
           "Gorham/2025%20RE%20Commitment%20-%20Map%20Lot.pdf")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def ensure_layout_text():
    if TXT.exists():
        return
    if not PDF.exists():
        print(f"Downloading commitment book -> {PDF}")
        req = urllib.request.Request(PDF_URL, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=120) as r, open(PDF, "wb") as f:
            f.write(r.read())
    if not shutil.which("pdftotext"):
        raise RuntimeError("pdftotext (poppler) not found on PATH; install poppler to parse the PDF")
    print("Extracting layout text with pdftotext -layout")
    subprocess.run(["pdftotext", "-layout", str(PDF), str(TXT)], check=True)

# {internalID} {name} {land} {building} {exemption} {assessment} {tax}
# Tax token allowed to be "1,575,862." (period, cents wrapped) as well as "8,888.88".
VALUE_RE = re.compile(
    r"^\s*(\d+)\s+(.+?)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+[\d,]+\.?\d{0,2}\s*$"
)
# Account lines may carry letter suffixes: 0001-0011-SOL (solar), 0003-0022L, 0001-0011-0001
ACCT_RE = re.compile(r"^\s*(\d{3,5}-[0-9A-Z]+(?:-[0-9A-Z]+)?)\s*$")
# "Acres  68.71  95 Homestead"  ->  exemption/special-use description
ACRES_RE = re.compile(r"Acres\s+[\d.]+\s+\d+\s+(.+?)\s*$")


def money(s):
    return float(s.replace(",", ""))


def _seg(p):
    """Strip leading zeros but keep any letters: '0011'->'11', '0022L'->'22L', 'SOL'->'SOL'."""
    m = re.match(r"^(\d+)([A-Z]*)$", p)
    if m:
        return str(int(m.group(1))) + m.group(2)
    return p.lstrip("0") or p


def norm_acct_to_maplot(acct):
    return "-".join(_seg(p) for p in acct.split("-"))


def main():
    ensure_layout_text()
    lines = open(TXT, encoding="utf-8", errors="ignore").read().splitlines()

    # Anchor on ACCOUNT lines; each record = span since previous account.
    # The parcel value line is the FIRST value-matching line in that span
    # (records begin with {internalID} {name} {values}); later value-like lines
    # inside a record are ignored.
    recs = []
    acct_idxs = [i for i, ln in enumerate(lines) if ACCT_RE.match(ln)]
    prev = -1
    for ai in acct_idxs:
        acct = ACCT_RE.match(lines[ai]).group(1)
        block = lines[prev + 1:ai]
        prev = ai
        vm = None
        for ln in block:
            m = VALUE_RE.match(ln)
            if m:
                vm = m
                break
        if vm is None:
            continue
        internal, name, land, bldg, exem, assess = vm.groups()
        homestead = any("Homestead" in ln for ln in block)
        exempt_desc = None
        for ln in block:
            am = ACRES_RE.search(ln)
            if am:
                exempt_desc = am.group(1).strip()
                break
        recs.append({
            "account": acct,
            "maplot": norm_acct_to_maplot(acct),
            "name": name.strip(),
            "land": money(land),
            "building": money(bldg),
            "exemption": money(exem),
            "assessment": money(assess),   # taxable total (net of exemption)
            "homestead": homestead,
            "exempt_desc": exempt_desc,
        })

    df = pd.DataFrame(recs)
    # drop dup accounts (keep first) just in case
    df = df.drop_duplicates("account").reset_index(drop=True)
    df.to_parquet(DATA / "commitment.parquet")

    print(f"Parsed records: {len(df):,}  unique accounts: {df.account.nunique():,}")
    print(f"\n{'':12}{'PARSED':>18}{'BOOK SUBTOTAL':>18}")
    checks = [
        ("Land", df.land.sum(), 783_259_400),
        ("Building", df.building.sum(), 2_520_461_600),
        ("Exemption", df.exemption.sum(), 402_032_800),
        ("Assessment", df.assessment.sum(), 2_901_688_200),
        ("Tax(calc)", df.assessment.sum() * 0.0148, 42_944_985.36),
    ]
    for name, got, book in checks:
        flag = "OK" if abs(got - book) / book < 0.001 else f"DIFF {(got/book-1)*100:+.2f}%"
        print(f"{name:12}{got:>18,.0f}{book:>18,.0f}  {flag}")

    print(f"\nland<0: {(df.land<0).sum()}  building==0: {(df.building==0).sum()}  "
          f"exemption>0: {(df.exemption>0).sum()}  homestead: {df.homestead.sum()}")
    print("\nSample:")
    print(df.head(6).to_string())


if __name__ == "__main__":
    main()
