#!/usr/bin/env python3
"""
Significant fires per PSA per month, now and in past years, from the NIFC WFIGS incident records.

A significant fire here is a wildfire that reached sig_fire_acres (default 100) and is counted in the
month (GACC time zone) it was discovered, in the PSA where it started. Past years give "normal": the
same calendar month in the previous normal_years (default 10) years.

  * data/fire_history/<year>.csv   every wildfire of that year that reached the smallest size any GACC
                                   uses (national, one row per fire; kept so PSAs can be re-assigned)
  * data/<GACC>/sig_fires.csv      each significant fire: month, discovery date, PSA, name, acres, cause
  * data/<GACC>/fire_counts.csv    per PSA per month: significant fires, their acres, causes
  * data/<GACC>/climatology.csv    normal for the next four months: per PSA, the counts in each past year,
                                   the mean and the bottom/top-third thresholds

Past years are read once (refreshed every 180 days); this year (and last year, until April) every night,
since fire sizes keep growing after discovery and reports come in late.

Usage: python scripts/fire_history.py [--years N] [--refresh-all]     (MOCK_DIR=path for offline tests)
"""

import argparse
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gaccs  # noqa: E402
import common  # noqa: E402

SVC = "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/{}/FeatureServer/0"
LAYERS = ["WFIGS_Incident_Locations", "InFORM_FireOccurrence_Public"]
YTD = "WFIGS_Incident_Locations_YearToDate"
HIST_DIR = os.path.join(gaccs.DATA, "fire_history")
HIST_LOG = os.path.join(HIST_DIR, "fetch_log.csv")
HIST_COLS = ["source_id", "fire_name", "discovered_utc", "lat", "lon", "acres", "cause"]
SIG_COLS = ["month", "discovered", "psa", "source_id", "fire_name", "acres", "cause", "lat", "lon"]
COUNT_COLS = ["month", "psa", "sig_fires", "sig_acres", "lightning", "human", "other", "largest_acres"]
CLIM_COLS = ["psa", "month", "normal_years", "n_years", "mean_fires", "low", "high", "mean_acres", "acres_low", "acres_high", "counts", "acres"]
SIZE_FIELDS = ["IncidentSize", "FinalAcres", "CalculatedAcres", "DailyAcres", "DiscoveryAcres"]


def first(a, *keys):
    for k in keys:
        if a.get(k) not in (None, ""):
            return a[k]
    return None


def acres_of(a):
    vals = [common.num(a.get(k)) for k in SIZE_FIELDS]
    vals = [v for v in vals if v is not None and v >= 0]
    return max(vals) if vals else None


def cause_group(c):
    c = str(c or "").lower()
    return "lightning" if c.startswith("natural") or "lightning" in c else "human" if c.startswith("human") else "other"


def to_ms(v):
    """Discovery time as epoch ms (services return ms; some layers return ISO text)."""
    x = common.num(v)
    if x is not None:
        return x
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).replace(tzinfo=timezone.utc).timestamp() * 1000
    except ValueError:
        return None


def fetch_year(year, min_acres):
    """Wildfires discovered in `year` (UTC, padded a day each side) that reached min_acres."""
    t0, t1 = f"{year - 1}-12-31 00:00:00", f"{year + 1}-01-02 00:00:00"
    size = " OR ".join(f"{k} >= {min_acres:g}" for k in SIZE_FIELDS[:3])
    wheres = [f"FireDiscoveryDateTime >= TIMESTAMP '{t0}' AND FireDiscoveryDateTime < TIMESTAMP '{t1}' AND ({size})",
              f"FireDiscoveryDateTime >= TIMESTAMP '{t0}' AND FireDiscoveryDateTime < TIMESTAMP '{t1}' AND IncidentSize >= {min_acres:g}",
              f"FireDiscoveryDateTime >= DATE '{t0[:10]}' AND FireDiscoveryDateTime < DATE '{t1[:10]}' AND IncidentSize >= {min_acres:g}"]
    layers = LAYERS + ([YTD] if year == datetime.now(timezone.utc).year else [])
    for layer in layers:
        for w in wheres:
            try:
                feats = common.arcgis_query(SVC.format(layer), where=w, geometry=True)
            except Exception as e:
                print(f"  {layer} {year}: {str(e)[:160]}")
                continue
            out = {}
            for f in feats:
                a = f.get("attributes") or {}
                if str(a.get("IncidentTypeCategory") or "WF").upper() in ("RX", "CX"):
                    continue
                acres, disc = acres_of(a), to_ms(first(a, "FireDiscoveryDateTime", "CreatedOnDateTime_dt", "CreatedOnDateTime"))
                g = f.get("geometry") or {}
                lon, lat = g.get("x"), g.get("y")
                if lon is None or lat is None:
                    lat, lon = first(a, "InitialLatitude", "POOLatitude"), first(a, "InitialLongitude", "POOLongitude")
                if acres is None or acres < min_acres or disc is None or common.num(lat) is None or common.num(lon) is None:
                    continue
                if not t0[:10] <= datetime.fromtimestamp(disc / 1000, tz=timezone.utc).strftime("%Y-%m-%d") < t1[:10]:
                    continue
                sid = str(first(a, "UniqueFireIdentifier", "IrwinID", "GlobalID", "OBJECTID"))
                name = str(first(a, "IncidentName") or "Unnamed").strip()
                out[sid] = {"source_id": sid, "fire_name": name.title() if name.isupper() else name,
                            "discovered_utc": datetime.fromtimestamp(disc / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "lat": round(float(lat), 5), "lon": round(float(lon), 5), "acres": round(acres, 1),
                            "cause": str(first(a, "FireCause") or "").strip()}
            print(f"  {layer} {year}: {len(out)} wildfires of {min_acres:g}+ ac")
            return list(out.values())
    raise RuntimeError(f"couldn't read wildfire records for {year}")


def update_history(years, min_acres, refresh_all=False):
    os.makedirs(HIST_DIR, exist_ok=True)
    log = {r["year"]: r for r in gaccs.read_csv(HIST_LOG)}
    now = datetime.now(timezone.utc)
    cur = now.year
    for y in years:
        p = os.path.join(HIST_DIR, f"{y}.csv")
        r = log.get(str(y))
        live = y == cur or (y == cur - 1 and now.month <= 4)
        old = r and (time.time() - datetime.fromisoformat(r["fetched_at_utc"].replace("Z", "+00:00")).timestamp()) > 180 * 86400
        if os.path.exists(p) and r and float(r.get("min_acres") or 1e9) <= min_acres and not (live or old or refresh_all):
            continue
        try:
            rows = fetch_year(y, min_acres)
        except Exception as e:
            print(f"{y}: {e}")
            continue
        gaccs.write_csv(p, sorted(rows, key=lambda x: x["discovered_utc"]), HIST_COLS)
        log[str(y)] = {"year": y, "fetched_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "fires": len(rows), "min_acres": min_acres}
    gaccs.write_csv(HIST_LOG, [log[k] for k in sorted(log)], ["year", "fetched_at_utc", "fires", "min_acres"])


def counts_for(fires, psa_list):
    """month -> psa -> {n, acres, ...} from significant-fire rows."""
    c = defaultdict(lambda: defaultdict(lambda: {"sig_fires": 0, "sig_acres": 0.0, "lightning": 0, "human": 0, "other": 0, "largest_acres": 0.0}))
    for f in fires:
        x = c[f["month"]][f["psa"]]
        x["sig_fires"] += 1
        x["sig_acres"] += float(f["acres"])
        x[cause_group(f["cause"])] += 1
        x["largest_acres"] = max(x["largest_acres"], float(f["acres"]))
    return c


def normals(counts, psa, month, n_years, first_year):
    """Counts and acres for the same calendar month in the n_years before `month` (only years we have)."""
    y, m = int(month[:4]), month[5:]
    yrs = [yy for yy in range(y - n_years, y) if yy >= first_year]
    cs = [counts.get(f"{yy}-{m}", {}).get(psa, {}).get("sig_fires", 0) for yy in yrs]
    ac = [counts.get(f"{yy}-{m}", {}).get(psa, {}).get("sig_acres", 0.0) for yy in yrs]
    return yrs, cs, ac


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=None, help="past years to keep (default: the largest normal_years of any GACC)")
    ap.add_argument("--refresh-all", action="store_true")
    ap.add_argument("--today", help="pretend today is YYYY-MM-DD (testing)")
    args = ap.parse_args()
    psas = common.load_psas()
    locate = common.make_locator(psas)
    codes = sorted({gaccs.gacc_of(p) for p in psas})
    cfgs = {g: gaccs.load_config(g) for g in codes}
    min_acres = min(float(c.get("sig_fire_acres") or 100) for c in cfgs.values())
    n_back = args.years or max(int(c.get("normal_years") or 10) for c in cfgs.values())
    cur = int(args.today[:4]) if args.today else datetime.now(timezone.utc).year
    # reach far enough back that the earliest archived outlook (e.g. imported from the NIFC archive) has a full normal
    issued = [r["issue_month"] for r in gaccs.read_csv(os.path.join(gaccs.DATA, "outlook_log.csv")) if r.get("rows") not in ("", "0")]
    first_needed = min([int(m[:4]) for m in issued] + [cur])
    years = list(range(first_needed - n_back, cur + 1))
    update_history(years, min_acres, args.refresh_all)
    have = sorted(int(f[:-4]) for f in os.listdir(HIST_DIR) if f[:-4].isdigit() and f.endswith(".csv"))
    from verify_outlook import first_history_year      # skips early years with too few records to trust
    first_year = max(min(have) if have else cur, first_history_year() or 0)
    have = [y for y in have if y >= first_year]

    hist = {}
    for y in have:                                  # year files overlap by a day at each end; keep one copy (the later file's)
        for f in gaccs.read_csv(os.path.join(HIST_DIR, f"{y}.csv")):
            hist[f["source_id"]] = f
    hist = list(hist.values())
    placed = []
    for f in hist:
        lat, lon = common.num(f.get("lat")), common.num(f.get("lon"))
        if lat is None or lon is None:
            continue
        psa = locate(lat, lon)
        if psa:
            placed.append((psa, f))
    print(f"{len(hist)} wildfire records, {len(placed)} inside a PSA ({first_year}-{cur})")

    for g in codes:
        cfg = cfgs[g]
        tz = ZoneInfo(cfg.get("timezone", "America/Denver"))
        min_g = float(cfg.get("sig_fire_acres") or 100)
        n_years = int(cfg.get("normal_years") or 10)
        sig = []
        for psa, f in placed:
            if gaccs.gacc_of(psa) != g or float(f["acres"]) < min_g:
                continue
            d = datetime.fromisoformat(f["discovered_utc"].replace("Z", "+00:00")).astimezone(tz).date()
            if d.year < first_year or d.year > cur:
                continue
            sig.append({"month": gaccs.ym(d), "discovered": d.isoformat(), "psa": psa, "source_id": f["source_id"],
                        "fire_name": f["fire_name"], "acres": int(round(float(f["acres"]))), "cause": f["cause"],
                        "lat": f["lat"], "lon": f["lon"]})
        sig.sort(key=lambda r: (r["discovered"], r["psa"], r["fire_name"]))
        gdir = gaccs.gacc_dir(g)
        gaccs.write_csv(os.path.join(gdir, "sig_fires.csv"), sig, SIG_COLS)
        counts = counts_for(sig, None)
        crow = [{"month": m, "psa": p, **{k: (round(v) if isinstance(v, float) else v) for k, v in x.items()}}
                for m in sorted(counts) for p, x in sorted(counts[m].items())]
        gaccs.write_csv(os.path.join(gdir, "fire_counts.csv"), crow, COUNT_COLS)

        # normal for this month and the next three (the months an outlook issued now covers)
        today = datetime.fromisoformat(args.today).date() if args.today else datetime.now(tz).date()
        g_psas = sorted(p for p in psas if gaccs.gacc_of(p) == g)
        clim = []
        for k in range(4):
            month = gaccs.add_months(gaccs.ym(today), k)
            for psa in g_psas:
                yrs, cs, ac = normals(counts, psa, month, n_years, first_year)
                if not yrs:
                    continue
                clim.append({"psa": psa, "month": month, "normal_years": f"{yrs[0]}-{yrs[-1]}", "n_years": len(yrs),
                             "mean_fires": round(sum(cs) / len(cs), 2), "low": round(gaccs.pctl(cs, 100 / 3), 2), "high": round(gaccs.pctl(cs, 200 / 3), 2),
                             "mean_acres": round(sum(ac) / len(ac)), "acres_low": round(gaccs.pctl(ac, 100 / 3)), "acres_high": round(gaccs.pctl(ac, 200 / 3)),
                             "counts": ";".join(map(str, cs)), "acres": ";".join(str(round(a)) for a in ac)})
        gaccs.write_csv(os.path.join(gdir, "climatology.csv"), clim, CLIM_COLS)
        this = sum(1 for r in sig if r["month"] == gaccs.ym(today))
        print(f"{g}: {len(sig)} significant fires ({min_g:g}+ ac) since {first_year}; {this} so far in {gaccs.ym(today)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
