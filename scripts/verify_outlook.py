#!/usr/bin/env python3
"""
Score every archived monthly outlook (Month 1 to Month 4) against what happened, PSA by PSA.

Observed category for a PSA and month (counts decide):
  * fires = significant fires (sig_fire_acres or larger) discovered in that PSA that month
  * normal = the same calendar month in the previous normal_years years (only years on file):
    low = the bottom-third mark, high = the top-third mark of those yearly counts
  * Above  if fires > high and fires >= min_fires_for_above
    Below  if fires < low
    Normal otherwise
  The same is worked out from acres burned by those fires and shown beside it, but doesn't decide.

Outcome: Correct (same category), One category off (Above or Below vs Normal), Opposite (Above vs Below).
Status: "in progress" during the month, "provisional" for 30 days after it ends (sizes and late reports
still settle), then "final".

Writes data/<GACC>/verification.csv. Usage: python scripts/verify_outlook.py [--today YYYY-MM-DD]
"""

import argparse
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gaccs  # noqa: E402

COLS = ["issue_month", "lead", "valid_month", "psa", "forecast", "obs_fires", "obs_acres", "normal_years", "normal_counts",
        "normal_mean", "normal_low", "normal_high", "obs_category", "acres_mean", "acres_low", "acres_high", "acres_category",
        "outcome", "status"]
RANK = {"Below": 0, "Normal": 1, "Above": 2}
MIN_YEARS = 3


def first_history_year():
    rows = gaccs.read_csv(os.path.join(gaccs.DATA, "fire_history", "fetch_log.csv"))
    ys = [int(r["year"]) for r in rows if str(r.get("year", "")).isdigit()]
    return min(ys) if ys else None


def category(v, lo, hi, min_above=0):
    if v > hi and v >= min_above:
        return "Above"
    if v < lo:
        return "Below"
    return "Normal"


def outcome(f, o):
    if not f:
        return "No forecast"
    if not o:
        return ""
    d = abs(RANK[f] - RANK[o])
    return "Correct" if d == 0 else "One category off" if d == 1 else "Opposite"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--today")
    args = ap.parse_args()
    fy = first_history_year()
    codes = sorted(gaccs.index_psas())
    for g in codes:
        cfg = gaccs.load_config(g)
        tz = ZoneInfo(cfg.get("timezone", "America/Denver"))
        today = date.fromisoformat(args.today) if args.today else datetime.now(tz).date()
        cur = gaccs.ym(today)
        gdir = gaccs.gacc_dir(g)
        ol = gaccs.read_csv(os.path.join(gdir, "outlooks.csv"))
        if not ol:
            continue
        counts = defaultdict(dict)
        for r in gaccs.read_csv(os.path.join(gdir, "fire_counts.csv")):
            counts[r["month"]][r["psa"]] = (int(float(r["sig_fires"] or 0)), float(r["sig_acres"] or 0))
        n_years, min_above = int(cfg.get("normal_years") or 10), int(cfg.get("min_fires_for_above") or 1)
        out = []
        for r in ol:
            vm = r["valid_month"]
            if vm > cur:
                continue
            y, m = int(vm[:4]), vm[5:]
            yrs = [yy for yy in range(y - n_years, y) if fy is not None and yy >= fy]
            n, ac = counts.get(vm, {}).get(r["psa"], (0, 0.0))
            row = {"issue_month": r["issue_month"], "lead": r["lead"], "valid_month": vm, "psa": r["psa"], "forecast": r["potential"],
                   "obs_fires": n, "obs_acres": round(ac)}
            if len(yrs) >= MIN_YEARS:
                cs = [counts.get(f"{yy}-{m}", {}).get(r["psa"], (0, 0.0))[0] for yy in yrs]
                aa = [counts.get(f"{yy}-{m}", {}).get(r["psa"], (0, 0.0))[1] for yy in yrs]
                lo, hi = gaccs.pctl(cs, 100 / 3), gaccs.pctl(cs, 200 / 3)
                alo, ahi = gaccs.pctl(aa, 100 / 3), gaccs.pctl(aa, 200 / 3)
                oc = category(n, lo, hi, min_above)
                row.update(normal_years=f"{yrs[0]}-{yrs[-1]}", normal_counts=";".join(map(str, cs)), normal_mean=round(sum(cs) / len(cs), 2),
                           normal_low=round(lo, 2), normal_high=round(hi, 2), obs_category=oc,
                           acres_mean=round(sum(aa) / len(aa)), acres_low=round(alo), acres_high=round(ahi),
                           acres_category=category(ac, alo, ahi, 1))
            row["outcome"] = outcome(row["forecast"], row.get("obs_category", ""))
            end = gaccs.month_end(vm)
            row["status"] = "in progress" if vm == cur else "provisional" if today <= end + timedelta(days=30) else "final"
            out.append(row)
        out.sort(key=lambda x: (x["valid_month"], int(x["lead"]), x["psa"]))
        gaccs.write_csv(os.path.join(gdir, "verification.csv"), out, COLS)
        done = [x for x in out if x["status"] != "in progress" and x.get("outcome") in ("Correct", "One category off", "Opposite")]
        ok = sum(1 for x in done if x["outcome"] == "Correct")
        print(f"{g}: {len(out)} PSA-months scored ({len(done)} complete; {ok} correct)" + ("" if fy else " - no fire history yet"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
