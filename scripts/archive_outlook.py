#!/usr/bin/env python3
"""
Archive the National Significant Wildland Fire Potential Outlook (Month 1 to Month 4), split by GACC.

The outlook is issued on (or within the first few workdays of) the 1st of each month. Month 1 is the
issue month, Month 2 the next month, and Months 3-4 the extended outlook; each covers the whole
calendar month. The PSA-by-PSA categories (Above / Normal / Below) come from the NPSG map service
Outlooks_Monthly_Extended (the same product as the NIFC maps), and the four NIFC map images
(month1_outlook.png ... month4_outlook.png) are saved alongside as the official record.

Runs every night: a new issue month is saved as soon as the service shows it; a correction to the
current issue (any PSA changing) replaces it and is logged. Rows go to data/<GACC>/outlooks.csv,
raw service pulls to data/raw/outlooks/<issue month>/service.json, and the maps beside them.

Usage:
    python scripts/archive_outlook.py              # normal nightly run
    python scripts/archive_outlook.py --force      # re-save the current issue even if unchanged
    MOCK_DIR=path python scripts/archive_outlook.py
"""

import argparse
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gaccs  # noqa: E402
import common  # noqa: E402

PNG_URL = "https://www.nifc.gov/nicc-files/predictive/outlooks/month{}_outlook.png"
RAW_DIR = os.path.join(gaccs.DATA, "raw", "outlooks")
LOG_PATH = os.path.join(gaccs.DATA, "outlook_log.csv")
LOG_COLS = ["issue_month", "pulled_at_utc", "rows", "hash", "note"]
COLS = ["issue_month", "lead", "valid_month", "psa", "gacc", "psa_name", "potential", "update_text", "pulled_at_utc"]
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_month(s):
    """'November 2026', 'Nov 2026', '11/2026', '2026-11' -> '2026-11' (or None)."""
    s = str(s or "").strip().lower()
    m = re.search(r"([a-z]{3})[a-z]*\.?\s*,?\s*(\d{4})", s)
    if m and m.group(1) in MONTHS:
        return f"{m.group(2)}-{MONTHS[m.group(1)]:02d}"
    m = re.search(r"(\d{4})[-/](\d{1,2})\b", s) or None
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    m = re.search(r"\b(\d{1,2})[-/](\d{4})", s)
    if m:
        return f"{m.group(2)}-{int(m.group(1)):02d}"
    return None


def potential_of(v):
    s = str(v or "").strip().lower()
    return "Above" if s.startswith("above") else "Below" if s.startswith("below") else "Normal" if s.startswith("normal") else ""


def get_ci(a, *names):
    low = {k.lower(): v for k, v in a.items()}
    for n in names:
        if low.get(n.lower()) not in (None, ""):
            return low[n.lower()]
    return None


def fetch_layers():
    """Attributes of the four outlook layers: the map service first, then its OGC endpoint."""
    errors = []
    for base in common.OUTLOOK_SVC:
        try:
            layers = [[f.get("attributes", {}) for f in common.arcgis_query(f"{base}/{i}")] for i in range(4)]
            if any(layers):
                return layers
            errors.append(f"{base}: no features")
        except Exception as e:
            errors.append(f"{base}: {e}")
            print(f"  {base}: {e}")
    for base in common.OUTLOOK_OGC:
        try:
            layers = [[f["attributes"] for f in common.ogc_items(base, i)] for i in range(4)]
            if any(layers):
                print(f"  read the outlook from {base}")
                return layers
            errors.append(f"{base}: no features")
        except Exception as e:
            errors.append(f"{base}: {e}")
            print(f"  {base}: {e}")
    raise RuntimeError("outlook service unavailable: " + " | ".join(x[:300] for x in errors))


def save_pngs(issue):
    """Download the four NIFC maps into data/raw/outlooks/<issue>/ when new or changed."""
    d = os.path.join(RAW_DIR, issue)
    os.makedirs(d, exist_ok=True)
    saved = []
    for n in range(1, 5):
        try:
            b = common.http(PNG_URL.format(n), retries=2, timeout=60, binary=True)
        except Exception as e:
            print(f"  month{n}_outlook.png: {e}")
            continue
        if not b.startswith(b"\x89PNG"):
            print(f"  month{n}_outlook.png: not a PNG; skipped")
            continue
        p = os.path.join(d, f"month{n}_outlook.png")
        if os.path.exists(p):
            with open(p, "rb") as fh:
                if fh.read() == b:
                    continue
        with open(p, "wb") as fh:
            fh.write(b)
        saved.append(n)
    if saved:
        print(f"  saved outlook maps for {issue}: month " + ", ".join(map(str, saved)))
    return saved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--only-if-missing", action="store_true",
                    help="do nothing if this month's outlook is already saved (the backup runs on the 2nd and 3rd)")
    args = ap.parse_args()
    now = datetime.now(timezone.utc)
    pulled = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    this_month = gaccs.ym(now.astimezone(ZoneInfo("America/Denver")).date())
    if args.only_if_missing and not args.force and any(r.get("issue_month") == this_month and r.get("rows") not in ("", "0")
                                                       for r in gaccs.read_csv(LOG_PATH)):
        print(f"The {this_month} outlook is already saved; nothing to do.")
        return 0

    try:
        layers = fetch_layers()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        save_pngs(this_month)          # keep the official maps even if the service is down
        return 1

    texts = [next((get_ci(a, "Updatetime") for a in L if get_ci(a, "Updatetime")), "") for L in layers]
    parsed = [parse_month(t) for t in texts]
    issue, note = parsed[0], ""
    if not issue:
        issue, note = this_month, f"issue month not readable from the service ('{texts[0]}'); used {this_month}"
    elif issue > this_month:
        note = f"service says {issue}, later than this month ({this_month})"
    print(f"Outlook service: Updatetime = {texts}; issue month {issue}")

    rows, names, seen = [], {}, set()
    for i, feats in enumerate(layers):
        lead = i + 1
        valid = gaccs.add_months(issue, i)
        for a in feats:
            psa = str(get_ci(a, "PSANationa", "psa_national", "nat_code") or "").strip().upper()
            if not common.valid_psa(psa) or (lead, psa) in seen:
                continue
            seen.add((lead, psa))
            nm = str(get_ci(a, "PSANAME") or "").strip()
            names[psa] = nm
            rows.append({"issue_month": issue, "lead": lead, "valid_month": valid, "psa": psa, "gacc": gaccs.gacc_of(psa),
                         "psa_name": nm, "potential": potential_of(get_ci(a, "FirePotent")), "update_text": texts[i] or "",
                         "pulled_at_utc": pulled})
    if not rows:
        common_log(issue, pulled, 0, "", "no PSAs returned")
        print("ERROR: the outlook service returned no PSAs", file=sys.stderr)
        return 1

    h = hashlib.sha1(json.dumps(sorted((r["lead"], r["psa"], r["potential"]) for r in rows)).encode()).hexdigest()[:12]
    log = [r for r in gaccs.read_csv(LOG_PATH) if r.get("issue_month") == issue and r.get("rows") not in ("", "0")]
    if log and log[-1].get("hash") == h and not args.force:
        print(f"{issue} outlook already archived and unchanged." +
              (f" The {this_month} outlook isn't in the service yet." if issue < this_month else ""))
        save_pngs(issue)
        if args.only_if_missing and issue < this_month and now.astimezone(ZoneInfo("America/Denver")).day >= 3:
            print(f"ERROR: the {this_month} outlook still isn't out after the last backup run (the 3rd). "
                  "Run the workflow by hand once it's posted.", file=sys.stderr)
            return 1
        return 0
    action = "forced re-save" if args.force else ("corrected issue replaced" if log else "ok")

    os.makedirs(os.path.join(RAW_DIR, issue), exist_ok=True)
    with open(os.path.join(RAW_DIR, issue, "service.json"), "w") as fh:
        json.dump({"issue_month": issue, "pulled_at_utc": pulled, "updatetime": texts, "layers": layers}, fh, separators=(",", ":"))
    by = defaultdict(list)
    for r in rows:
        by[r["gacc"]].append(r)
    for g, grows in by.items():
        gaccs.load_config(g)                                        # creates default settings for a new GACC
        p = os.path.join(gaccs.gacc_dir(g), "outlooks.csv")
        keep = [r for r in gaccs.read_csv(p) if r["issue_month"] != issue]
        allr = keep + grows
        allr.sort(key=lambda r: (r["issue_month"], int(r["lead"]), r["psa"]))
        gaccs.write_csv(p, allr, COLS)
    gaccs.update_index({g: [r["psa"] for r in grows] for g, grows in by.items()}, names)
    common_log(issue, pulled, len(rows), h, "; ".join(x for x in (action, note) if x))
    save_pngs(issue)
    cnt = defaultdict(lambda: defaultdict(int))
    for r in rows:
        cnt[r["lead"]][r["potential"] or "none"] += 1
    print(f"Archived {issue} outlook ({action}): " + " | ".join(
        f"Month {k} ({gaccs.add_months(issue, k - 1)}): " + ", ".join(f"{v} {c}" for c, v in sorted(d.items())) for k, d in sorted(cnt.items())))
    return 0


def common_log(issue, pulled, n, h, note):
    rows = gaccs.read_csv(LOG_PATH)
    rows.append({"issue_month": issue, "pulled_at_utc": pulled, "rows": n, "hash": h, "note": note})
    gaccs.write_csv(LOG_PATH, rows, LOG_COLS)


if __name__ == "__main__":
    sys.exit(main())
