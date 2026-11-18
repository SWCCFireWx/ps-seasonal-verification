#!/usr/bin/env python3
"""
Score every archived monthly outlook (Month 1 to Month 4) against what happened, PSA by PSA,
using several separate measures, each giving Above / Normal / Below for the PSA and month:

  count    Significant fires vs past years (the official measure): fires that reached sig_fire_acres,
           compared with the same month in the previous normal_years years. Above = more than the
           top third of those years (and at least min_fires_for_above fires), Below = fewer than the
           bottom third.
  chance   The same fire count, judged by how unusual it is for that PSA's average (a Poisson chance):
           Above if a count this high happens less than 1 time in 3 at that average, Below if a count
           this low does. Behaves sensibly when the average is a fraction of a fire.
  acres    Acres burned by those fires vs past years (top / bottom third).
  f209     Fires with an ICS-209 report (the fires that needed more than initial attack) vs past years.
  fuels    Days the PSA's ERC reached its stations' 90th percentile. Normally about 1 day in 10:
           Above if fuels_above_share (20%) of days or more, Below if fuels_below_share (3%) or fewer.
  overall  The four measures above that aren't the official one's twin (chance, acres, f209, fuels)
           combined: Above = +1, Normal = 0, Below = -1, averaged; Above if the average is 1/3 or more,
           Below if -1/3 or less. Needs at least two measures.

Outcome (official measure): Correct, One category off, Opposite.
Status: "in progress" during the month, "provisional" for 30 days after it ends, then "final".
Also written: the normal fire average (used to weight scores and to pick out quiet months on the site).

Writes data/<GACC>/verification.csv. Usage: python scripts/verify_outlook.py [--today YYYY-MM-DD]
"""

import argparse
import math
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gaccs  # noqa: E402

COLS = ["issue_month", "lead", "valid_month", "psa", "forecast",
        "obs_fires", "obs_acres", "normal_years", "normal_counts", "normal_mean", "normal_low", "normal_high", "obs_category",
        "chance_high", "chance_low", "chance_category",
        "acres_mean", "acres_low", "acres_high", "acres_category",
        "fires_209", "f209_mean", "f209_low", "f209_high", "f209_category",
        "fuel_days", "fuel_hot_days", "fuel_share", "fuels_category",
        "overall_score", "overall_measures", "overall_category",
        "outcome", "status"]
RANK = {"Below": 0, "Normal": 1, "Above": 2}
VAL = {"Below": -1, "Normal": 0, "Above": 1}
MIN_YEARS = 3


def first_history_year():
    """Earliest year of fire records complete enough to use (at least a quarter of a typical year's records;
    WFIGS thins out before about 2014, and a nearly empty year would look like a quiet one)."""
    rows = gaccs.read_csv(os.path.join(gaccs.DATA, "fire_history", "fetch_log.csv"))
    ys = {int(r["year"]): int(float(r.get("fires") or 0)) for r in rows if str(r.get("year", "")).isdigit()}
    if not ys:
        return None
    full = [n for y, n in ys.items() if y < max(ys)]
    typical = gaccs.pctl(full, 50) if full else 0
    ok = [y for y, n in sorted(ys.items()) if n >= 0.25 * typical]
    first = ok[0] if ok else min(ys)
    while first + 1 in ys and first not in ok:
        first += 1
    return first


def tercile(v, lo, hi, min_above=0):
    if v > hi and v >= min_above:
        return "Above"
    if v < lo:
        return "Below"
    return "Normal"


def poisson_cdf(k, lam):
    """P(X <= k) for a Poisson average lam."""
    if k < 0:
        return 0.0
    if lam <= 0:
        return 1.0
    term = total = math.exp(-lam)
    for i in range(1, int(k) + 1):
        term *= lam / i
        total += term
    return min(1.0, total)


def chance(n, lam, min_above):
    """(chance of a count this high or higher, chance of this low or lower, category)."""
    p_high = 1.0 - poisson_cdf(n - 1, lam)
    p_low = poisson_cdf(n, lam)
    if lam <= 0:
        p_high = 0.0 if n > 0 else 1.0
    cat = "Above" if p_high <= 1 / 3 and n >= min_above else "Below" if p_low <= 1 / 3 else "Normal"
    return round(p_high, 3), round(p_low, 3), cat


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
            counts[r["month"]][r["psa"]] = (int(float(r.get("sig_fires") or 0)), float(r.get("sig_acres") or 0),
                                            int(float(r.get("fires_209") or 0)))
        wx = {(r["month"], r["psa"]): r for r in gaccs.read_csv(os.path.join(gdir, "psa_monthly.csv"))}
        has209 = any(v[2] > 0 for mm in counts.values() for v in mm.values())   # no 209 records at all: leave that measure out
        n_years, min_above = int(cfg.get("normal_years") or 10), int(cfg.get("min_fires_for_above") or 1)
        f_hi, f_lo, f_days = float(cfg.get("fuels_above_share") or .2), float(cfg.get("fuels_below_share") or .03), int(cfg.get("min_fuel_days") or 15)
        out = []
        for r in ol:
            vm = r["valid_month"]
            if vm > cur:
                continue
            y, m = int(vm[:4]), vm[5:]
            yrs = [yy for yy in range(y - n_years, y) if fy is not None and yy >= fy]
            n, ac, n209 = counts.get(vm, {}).get(r["psa"], (0, 0.0, 0))
            row = {"issue_month": r["issue_month"], "lead": r["lead"], "valid_month": vm, "psa": r["psa"], "forecast": r["potential"],
                   "obs_fires": n, "obs_acres": round(ac), "fires_209": n209}
            votes = []
            if len(yrs) >= MIN_YEARS:
                past = [counts.get(f"{yy}-{m}", {}).get(r["psa"], (0, 0.0, 0)) for yy in yrs]
                cs, aa, ff = [p[0] for p in past], [p[1] for p in past], [p[2] for p in past]
                lo, hi = gaccs.pctl(cs, 100 / 3), gaccs.pctl(cs, 200 / 3)
                alo, ahi = gaccs.pctl(aa, 100 / 3), gaccs.pctl(aa, 200 / 3)
                flo, fhi = gaccs.pctl(ff, 100 / 3), gaccs.pctl(ff, 200 / 3)
                mean = sum(cs) / len(cs)
                ph, pl, ccat = chance(n, mean, min_above)
                row.update(normal_years=f"{yrs[0]}-{yrs[-1]}", normal_counts=";".join(map(str, cs)), normal_mean=round(mean, 2),
                           normal_low=round(lo, 2), normal_high=round(hi, 2), obs_category=tercile(n, lo, hi, min_above),
                           chance_high=ph, chance_low=pl, chance_category=ccat,
                           acres_mean=round(sum(aa) / len(aa)), acres_low=round(alo), acres_high=round(ahi),
                           acres_category=tercile(ac, alo, ahi, 1),
                           )
                votes += [row["chance_category"], row["acres_category"]]
                if has209:
                    row.update(f209_mean=round(sum(ff) / len(ff), 2), f209_low=round(flo, 2), f209_high=round(fhi, 2),
                               f209_category=tercile(n209, flo, fhi, 1))
                    votes.append(row["f209_category"])
            w = wx.get((vm, r["psa"]))
            if w and str(w.get("erc_days") or "").isdigit() and int(w["erc_days"]) >= f_days and w.get("erc_90_days") not in (None, ""):
                share = int(float(w["erc_90_days"])) / int(w["erc_days"])
                row.update(fuel_days=w["erc_days"], fuel_hot_days=int(float(w["erc_90_days"])), fuel_share=round(share, 3),
                           fuels_category="Above" if share >= f_hi else "Below" if share <= f_lo else "Normal")
                votes.append(row["fuels_category"])
            if len(votes) >= 2:
                sc = sum(VAL[v] for v in votes) / len(votes)
                row.update(overall_score=round(sc, 2), overall_measures=len(votes),
                           overall_category="Above" if sc >= 1 / 3 - 1e-9 else "Below" if sc <= -1 / 3 + 1e-9 else "Normal")
            row["outcome"] = outcome(row["forecast"], row.get("obs_category", ""))
            end = gaccs.month_end(vm)
            row["status"] = "in progress" if vm == cur else "provisional" if today <= end + timedelta(days=30) else "final"
            out.append(row)
        out.sort(key=lambda x: (x["valid_month"], int(x["lead"]), x["psa"]))
        gaccs.write_csv(os.path.join(gdir, "verification.csv"), out, COLS)
        done = [x for x in out if x["status"] != "in progress" and x.get("outcome") in ("Correct", "One category off", "Opposite")]
        ok = sum(1 for x in done if x["outcome"] == "Correct")
        fuels = sum(1 for x in out if x.get("fuels_category"))
        print(f"{g}: {len(out)} PSA-months scored ({len(done)} complete; {ok} correct by fire count; {fuels} with a fuels result)"
              + ("" if fy else " - no fire history yet"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
