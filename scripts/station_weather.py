#!/usr/bin/env python3
"""
Daily station weather for every PSA, checked against neighboring stations, rolled up by month.

Sources (public, no login)
  * RAWS daily summaries from FEMS (wxMinMax): max temperature, min RH, max wind and gust, rain total.
  * RAWS daily max ERC and BI from FEMS (nfdrMinMax, observed values, each GACC's NFDRS fuel model),
    plus each station's ERC percentiles (percentileLevels) so a day can be compared with its history.
  * ASOS/AWOS daily summaries from the Iowa Environmental Mesonet (daily.py): max temperature, min RH,
    wind, rain total, and the NCEI normal high and normal rain for the date.

Neighbor check (every station, every day), so one bad sensor doesn't drive a PSA's numbers
  * Temperature: the station's max temp is compared with the median of stations within neighbor_km
    (and max_elev_diff_ft), each adjusted to the station's elevation (lapse_f_per_1000ft). A value
    more than temp_flag_f (or 3 robust standard deviations, whichever is larger) warmer is "hot";
    colder is "cold".
  * Rain: "rain alone" = rain_flag_in or more when no neighbor got more than dry_neighbor_in;
    "wet" = an inch or more and over 4 times the wettest neighbor; "missed" = nothing when the
    neighbors' median is missed_rain_in or more and 60% of them got at least half that (a clogged, frozen or broken gauge).
  * Impossible values (rain below 0 or above max_daily_rain_in, temps outside -60 to 135 F) are "bad".
  Flagged values are left out of PSA numbers but kept on file with the reason. Over a month, a station
  that is hot/cold on persistent_days or more days is marked "runs hot"/"runs cold", and one whose
  monthly rain is month_ratio times its neighbors' (or a third of it) "reads wet"/"reads dry"; those
  are left out of that month's PSA numbers too.

Files
  data/wx_raw/<date>.json.gz          every station's values that day (national; kept for re-checking)
  data/<GACC>/station_daily.csv       each station, each day for the last 62 days, with neighbor values and flags
  data/<GACC>/psa_daily.csv           each PSA, each day: median of good stations
  data/<GACC>/station_monthly.csv     each station, each month: totals, means, flag days, month flags
  data/<GACC>/psa_monthly.csv         each PSA, each month: rain, temperature, ERC, BI and flagged stations
  data/<GACC>/qc_flags.csv            every flagged station-day
  data/<GACC>/stations.csv            the stations used in that GACC

Usage
    python scripts/station_weather.py                    # fetch the last 10 days that are missing, rebuild their months
    python scripts/station_weather.py --from 2026-10-01  # backfill from a date (if the services still have it)
    python scripts/station_weather.py --rebuild          # re-check every saved day with current settings
    MOCK_DIR=path python scripts/station_weather.py      # offline testing
"""

import argparse
import csv
import gzip
import io
import json
import os
import statistics
import sys
import urllib.parse
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gaccs  # noqa: E402
import common  # noqa: E402
from common import num  # noqa: E402

FEMS_GQL = "https://fems.fs2c.usda.gov/api/climatology/graphql"
IEM = "https://mesonet.agron.iastate.edu"
KT_TO_MPH = 1.15078
RAW_DIR = os.path.join(gaccs.DATA, "wx_raw")
RAWS_LIST = os.path.join(gaccs.DATA, "stations_raws.csv")
ASOS_LIST = os.path.join(gaccs.DATA, "stations_asos.csv")
PCTL_PATH = os.path.join(gaccs.DATA, "erc_percentiles.csv")
KEEP_DAILY = 62
REFETCH_DAYS = 3          # days this recent are fetched again (late reports)

ST_COLS = ["station_id", "name", "type", "network", "lat", "lon", "elev_ft", "state", "psa", "gacc"]
SD_COLS = ["date", "station_id", "name", "type", "psa", "elev_ft", "max_temp_f", "temp_nbr_f", "temp_diff_f", "temp_nbrs",
           "min_rh", "max_wind_mph", "max_gust_mph", "precip_in", "precip_nbr_in", "precip_nbr_max_in", "precip_nbrs",
           "erc", "erc_pctl", "bi", "normal_high_f", "flags"]
PD_COLS = ["date", "psa", "stations", "max_temp_f", "precip_in", "rain_stations", "min_rh", "max_wind_mph", "erc", "bi", "flagged"]
SM_COLS = ["month", "station_id", "name", "type", "psa", "days", "precip_days", "precip_in", "precip_nbr_in", "max_temp_mean_f",
           "temp_diff_mean_f", "temp_checked_days", "hot_days", "cold_days", "rain_flag_days", "erc_mean", "erc_max", "erc_90_days",
           "bi_mean", "bi_max", "normal_high_mean_f", "normal_precip_in", "month_flags"]
PM_COLS = ["month", "psa", "days", "stations", "raws", "asos", "precip_in", "precip_stations", "precip_normal_in", "precip_pct_normal",
           "max_temp_mean_f", "max_temp_f", "temp_anom_f", "temp_anom_stations", "min_rh_mean", "erc_mean", "erc_max", "erc_90_days",
           "bi_mean", "bi_max", "flagged_stations", "flag_list", "complete"]
QF_COLS = ["date", "station_id", "name", "type", "psa", "flag", "value", "neighbors", "neighbor_value", "note"]
FLAG_TEXT = {"hot": "max temp much warmer than neighbors", "cold": "max temp much colder than neighbors",
             "rain_alone": "rain no neighbor saw", "wet": "far more rain than any neighbor",
             "missed": "no rain while most neighbors got rain", "bad_temp": "impossible temperature", "bad_rain": "impossible rain total"}


# ------------------------------------------------------------------ FEMS
def gql(query):
    payload = json.loads(common.http(FEMS_GQL, {"query": query}))
    if payload.get("errors"):
        raise RuntimeError(f"FEMS error: {str(payload['errors'])[:300]}")
    return payload["data"]


def paged(query_fn, key):
    out, page = [], 0
    while True:
        d = gql(query_fn(page))[key]
        out.extend(d.get("data") or [])
        page += 1
        if page >= int((d.get("_metadata") or {}).get("page_count") or 1) or common.MOCK_DIR:
            return out


def load_raws(locate):
    if common.stale(RAWS_LIST):
        rows = {}
        for st in gaccs.STATES:
            page = 0
            while True:
                q = f'''query {{ stationMetaData(stateId: "{st}", page: {page}, per_page: 1000) {{
                    _metadata {{ page page_count total_count }}
                    data {{ station_id station_name latitude longitude elevation state station_status }} }} }}'''
                try:
                    d = gql(q)["stationMetaData"]
                except Exception as e:
                    print(f"  RAWS list {st}: {e}")
                    break
                for s in d.get("data") or []:
                    lat, lon = num(s.get("latitude")), num(s.get("longitude"))
                    if lat is None or lon is None or str(s.get("station_status") or "").lower().startswith("inact"):
                        continue
                    psa = locate(lat, lon)
                    if psa:
                        rows[str(s["station_id"])] = {"station_id": str(s["station_id"]), "name": s.get("station_name", ""), "type": "RAWS",
                                                      "network": "RAWS", "lat": lat, "lon": lon, "elev_ft": s.get("elevation") or "",
                                                      "state": s.get("state", st), "psa": psa, "gacc": gaccs.gacc_of(psa)}
                page += 1
                if page >= int((d.get("_metadata") or {}).get("page_count") or 1) or common.MOCK_DIR:
                    break
        if rows:
            gaccs.write_csv(RAWS_LIST, sorted(rows.values(), key=lambda r: r["station_id"]), ST_COLS)
        elif not os.path.exists(RAWS_LIST):
            raise RuntimeError("no RAWS stations found inside the PSAs")
    return gaccs.read_csv(RAWS_LIST)


def load_asos(locate):
    if common.stale(ASOS_LIST):
        rows = {}
        for net in [f"{st}_ASOS" for st in gaccs.STATES] + ["AWOS"]:
            try:
                j = json.loads(common.http(f"{IEM}/geojson/network/{net}.geojson", retries=2, timeout=60))
            except Exception as e:
                print(f"  ASOS list {net}: {str(e)[:100]}")
                continue
            for f in j.get("features") or []:
                p, g = f.get("properties") or {}, f.get("geometry") or {}
                try:
                    lon, lat = float(g["coordinates"][0]), float(g["coordinates"][1])
                except (KeyError, TypeError, ValueError, IndexError):
                    continue
                if str(p.get("online", True)).lower() in ("false", "0"):
                    continue
                psa, sid = locate(lat, lon), str(p.get("sid") or p.get("id") or "").strip()
                if psa and sid:
                    el = num(p.get("elevation"))
                    rows["ASOS-" + sid] = {"station_id": "ASOS-" + sid, "name": str(p.get("sname") or sid).title(), "type": "ASOS",
                                           "network": net, "lat": lat, "lon": lon, "elev_ft": "" if el is None else round(el * 3.28084),
                                           "state": net.split("_")[0][:2] if "_" in net else "IA", "psa": psa, "gacc": gaccs.gacc_of(psa)}
        if rows:
            gaccs.write_csv(ASOS_LIST, sorted(rows.values(), key=lambda r: r["station_id"]), ST_COLS)
    return gaccs.read_csv(ASOS_LIST) if os.path.exists(ASOS_LIST) else []


def fetch_raws_daily(d0, d1, ids):
    """{date: {station: {t, rh, ws, g, p}}} from FEMS wxMinMax."""
    out = defaultdict(dict)
    for i in range(0, len(ids), 60):
        chunk = ",".join(ids[i:i + 60])
        rows = paged(lambda p: f'''query {{ wxMinMax(startDate: "{d0}", endDate: "{d1}", stationIds: "{chunk}", page: {p}, per_page: 5000) {{
            _metadata {{ page page_count total_count }}
            data {{ station_id summary_date observation_type temperature_max relative_humidity_min wind_speed_max
                    peak_gust_speed_max daily_precipitation_total }} }} }}''', "wxMinMax")
        for r in rows:
            if str(r.get("observation_type") or "O").upper().startswith("F"):
                continue
            d = str(r.get("summary_date") or "")[:10]
            out[d][str(r.get("station_id"))] = {"t": num(r.get("temperature_max")), "rh": num(r.get("relative_humidity_min")),
                                                "ws": num(r.get("wind_speed_max")), "g": num(r.get("peak_gust_speed_max")),
                                                "p": num(r.get("daily_precipitation_total"))}
    return out


def fetch_nfdr_daily(d0, d1, ids, fm):
    """{date: {station: (erc, bi)}} from FEMS nfdrMinMax (observed)."""
    out = defaultdict(dict)
    for i in range(0, len(ids), 60):
        chunk = ",".join(ids[i:i + 60])
        rows = paged(lambda p: f'''query {{ nfdrMinMax(startDate: "{d0}", endDate: "{d1}", stationIds: "{chunk}", fuelModels: "{fm}", nfdrType: "O", page: {p}, per_page: 5000) {{
            _metadata {{ page page_count total_count }}
            data {{ station_id summary_date nfdr_type fuel_model energy_release_component_max burning_index_max }} }} }}''', "nfdrMinMax")
        for r in rows:
            d = str(r.get("summary_date") or "")[:10]
            e, b = num(r.get("energy_release_component_max")), num(r.get("burning_index_max"))
            e = e if e is not None and 0 <= e < 200 else None
            b = b if b is not None and 0 <= b < 500 else None
            if e is not None or b is not None:
                out[d][str(r.get("station_id"))] = (e, b)
    return out


def fetch_pctl(ids, fm, years):
    """{station: {80, 90, 97}} ERC percentiles (whole year, all hours) from FEMS percentileLevels."""
    out = {}
    y1 = date.today().year - 1
    for i in range(0, len(ids), 40):
        chunk = ",".join(ids[i:i + 40])
        try:
            rows = paged(lambda p: f'''query {{ percentileLevels(stationIds: "{chunk}", fuelModel: "{fm}", percentileLevels: "80,90,97",
                climatology: {{ startYear: {y1 - years + 1}, endYear: {y1}, startMonthDay: "01-01", endMonthDay: "12-31", startHour: 0, endHour: 23 }},
                page: {p}, per_page: 1000) {{ _metadata {{ page page_count total_count }} data {{ station_id energy_release_component }} }} }}''', "percentileLevels")
        except Exception as e:
            print(f"  ERC percentiles: {str(e)[:160]}")
            return out
        for r in rows:
            v = r.get("energy_release_component")
            if isinstance(v, str):
                try:
                    v = json.loads(v)
                except ValueError:
                    v = None
            if isinstance(v, list) and v and isinstance(v[0], dict):
                v = {str(k): x for d in v for k, x in d.items()}
            if isinstance(v, dict):
                pick = lambda q: next((num(x) for k, x in v.items() if str(k).startswith(str(q))), None)
                out[str(r.get("station_id"))] = {"p80": pick(80), "p90": pick(90), "p97": pick(97)}
    return out


# ------------------------------------------------------------------ IEM
def fetch_asos_daily(d0, d1, stations):
    """{date: {ASOS-id: {t, rh, ws, g, p, nh, np}}} from IEM daily summaries, one request per network."""
    out = defaultdict(dict)
    want = defaultdict(set)
    for s in stations:
        want[s["network"]].add(s["station_id"][5:])
    for net, sids in sorted(want.items()):
        q = {"network": net, "stations": "_ALL", "sts": d0, "ets": d1, "format": "csv", "na": "blank",
             "var": "max_temp_f,min_rh,max_wind_speed_kts,max_wind_gust_kts,precip_in,climo_high_f,climo_precip_in"}
        try:
            text = common.http(f"{IEM}/cgi-bin/request/daily.py?" + urllib.parse.urlencode(q), retries=3, timeout=180)
        except Exception as e:
            print(f"  ASOS daily {net}: {str(e)[:120]}")
            continue
        for r in csv.DictReader(io.StringIO(text)):
            sid = (r.get("station") or "").strip()
            if sid not in sids:
                continue
            d = (r.get("day") or r.get("date") or "")[:10]
            kt = lambda k: None if num(r.get(k)) is None else round(num(r.get(k)) * KT_TO_MPH, 1)
            p = num(r.get("precip_in"))
            out[d]["ASOS-" + sid] = {"t": num(r.get("max_temp_f")), "rh": num(r.get("min_rh")), "ws": kt("max_wind_speed_kts"),
                                     "g": kt("max_wind_gust_kts"), "p": None if p is None else (0.0 if 0 < p < 0.001 else p),
                                     "nh": num(r.get("climo_high_f")), "np": num(r.get("climo_precip_in"))}
    return out


# ------------------------------------------------------------------ neighbors and checks
def neighbor_lists(stations, cfg_of):
    """station -> [(other, km)] within that station's GACC neighbor_km (grid-bucketed)."""
    cell = defaultdict(list)
    for s in stations:
        cell[(int(s["lat"] // 1), int(s["lon"] // 1))].append(s)
    out = {}
    for s in stations:
        r = float(cfg_of(s)["qc"]["neighbor_km"])
        cx, cy, nb = int(s["lat"] // 1), int(s["lon"] // 1), []
        span = max(1, int(r / 80) + 1)
        for dx in range(-span, span + 1):
            for dy in range(-span - 1, span + 2):
                for o in cell.get((cx + dx, cy + dy), []):
                    if o is s:
                        continue
                    km = common.dist_km(s["lat"], s["lon"], o["lat"], o["lon"])
                    if km <= r:
                        nb.append((o, km))
        nb.sort(key=lambda x: x[1])
        out[s["station_id"]] = nb
    return out


def check_day(day, stations, nbrs, cfg_of):
    """Neighbor checks for one day. Returns {station: result dict}."""
    res = {}
    for s in stations:
        v = day.get(s["station_id"])
        if not v:
            continue
        qc, flags = cfg_of(s)["qc"], []
        t, p = v.get("t"), v.get("p")
        if t is not None and not -60 <= t <= 135:
            flags.append("bad_temp")
            t = None
        if p is not None and not 0 <= p <= float(qc["max_daily_rain_in"]):
            flags.append("bad_rain")
            p = None
        r = {"t": t, "p": p, "flags": flags, "tn": None, "td": None, "tk": 0, "pn": None, "pmax": None, "pk": 0}
        res[s["station_id"]] = r
    elev = lambda s: num(s.get("elev_ft"))
    for s in stations:
        r = res.get(s["station_id"])
        if not r:
            continue
        qc = cfg_of(s)["qc"]
        # temperature
        if r["t"] is not None and elev(s) is not None:
            adj = []
            for o, _ in nbrs[s["station_id"]]:
                ro = res.get(o["station_id"])
                if not ro or ro["t"] is None or elev(o) is None or abs(elev(o) - elev(s)) > float(qc["max_elev_diff_ft"]):
                    continue
                adj.append(ro["t"] + float(qc["lapse_f_per_1000ft"]) * (elev(o) - elev(s)) / 1000.0)
                if len(adj) >= int(qc["max_neighbors"]):
                    break
            if len(adj) >= int(qc["min_neighbors"]):
                m = statistics.median(adj)
                mad = statistics.median(abs(a - m) for a in adj)
                lim = max(float(qc["temp_flag_f"]), 3 * 1.4826 * mad)
                r.update(tn=round(m, 1), td=round(r["t"] - m, 1), tk=len(adj))
                if r["t"] - m > lim:
                    r["flags"].append("hot")
                elif m - r["t"] > lim:
                    r["flags"].append("cold")
        # rain
        if r["p"] is not None:
            pn = [res[o["station_id"]]["p"] for o, _ in nbrs[s["station_id"]] if o["station_id"] in res and res[o["station_id"]]["p"] is not None]
            pn = pn[:int(qc["max_neighbors"])]                  # nearest ones only: rain is local
            if len(pn) >= int(qc["min_neighbors"]):
                med, mx = statistics.median(pn), max(pn)
                r.update(pn=round(med, 2), pmax=round(mx, 2), pk=len(pn))
                if r["p"] >= float(qc["rain_flag_in"]) and mx <= float(qc["dry_neighbor_in"]):
                    r["flags"].append("rain_alone")
                elif r["p"] >= 1.0 and r["p"] > 4 * max(mx, float(qc["dry_neighbor_in"])):
                    r["flags"].append("wet")
                elif r["p"] <= 0.005 and med >= float(qc["missed_rain_in"]) and sum(1 for x in pn if x >= float(qc["missed_rain_in"]) / 2) >= 0.6 * len(pn):
                    r["flags"].append("missed")
    return res


TEMP_BAD = {"hot", "cold", "bad_temp"}
RAIN_BAD = {"rain_alone", "wet", "missed", "bad_rain"}


# ------------------------------------------------------------------ raw day files
def raw_path(d):
    return os.path.join(RAW_DIR, f"{d}.json.gz")


def read_raw(d):
    p = raw_path(d)
    if not os.path.exists(p):
        return None
    with gzip.open(p, "rt") as fh:
        return json.load(fh)


def write_raw(d, obj):
    os.makedirs(RAW_DIR, exist_ok=True)
    with gzip.open(raw_path(d), "wt") as fh:
        json.dump(obj, fh, separators=(",", ":"))


def fetch_days(dates, raws, asos, cfgs):
    """Fetch every source for the date range and save one raw file per date (merging with what's saved)."""
    d0, d1 = min(dates), max(dates)
    got, missing = defaultdict(dict), []
    ids = [s["station_id"] for s in raws]
    try:
        for d, v in fetch_raws_daily(d0, d1, ids).items():
            for sid, x in v.items():
                got[d][sid] = dict(x)
    except Exception as e:
        print(f"RAWS daily {d0}..{d1}: {e}")
        missing.append("raws")
    by_fm = defaultdict(list)
    for s in raws:
        by_fm[str(cfgs[s["gacc"]].get("erc_fuel_model", "Y")).upper()].append(s["station_id"])
    for fm, fids in by_fm.items():
        try:
            for d, v in fetch_nfdr_daily(d0, d1, fids, fm).items():
                for sid, (e, b) in v.items():
                    got[d].setdefault(sid, {}).update(erc=e, bi=b)
        except Exception as e:
            print(f"ERC/BI ({fm}) {d0}..{d1}: {e}")
            missing.append("erc")
    use = [s for s in asos if cfgs[s["gacc"]].get("use_asos", True)]
    if use:
        try:
            for d, v in fetch_asos_daily(d0, d1, use).items():
                got[d].update(v)
        except Exception as e:
            print(f"ASOS daily {d0}..{d1}: {e}")
            missing.append("asos")
    n = 0
    for d in dates:
        if not got.get(d):
            print(f"{d}: no station data returned; will retry next run")
            continue
        old = read_raw(d) or {}
        st = old.get("stations", {})
        for sid, x in got[d].items():
            st[sid] = {**st.get(sid, {}), **{k: v for k, v in x.items() if v is not None}}
        write_raw(d, {"date": d, "fetched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "missing": sorted(set(missing)), "stations": st})
        n += 1
    return n


# ------------------------------------------------------------------ summaries
def med(a):
    a = [x for x in a if x is not None]
    return round(statistics.median(a), 2) if a else ""


def mean(a, nd=1):
    a = [x for x in a if x is not None]
    return round(sum(a) / len(a), nd) if a else ""


def build_month(month, stations, nbrs, cfg_of, pct):
    """All rows for one month (national): station-days, flags, PSA-days, station-months, PSA-months."""
    days = []
    d, end = gaccs.month_start(month), gaccs.month_end(month)
    while d <= end:
        raw = read_raw(d.isoformat())
        if raw:
            days.append((d.isoformat(), raw["stations"], check_day(raw["stations"], stations, nbrs, cfg_of)))
        d += timedelta(days=1)
    by_id = {s["station_id"]: s for s in stations}
    sd, qf, pd = [], [], []
    acc = defaultdict(lambda: defaultdict(list))       # station -> field -> values
    for ds, vals, res in days:
        psa_vals = defaultdict(lambda: defaultdict(list))
        for sid, r in res.items():
            s, v = by_id[sid], vals[sid]
            erc, bi, pc = v.get("erc"), v.get("bi"), pct.get(sid) or {}
            ep = ""
            if erc is not None and pc.get("p90"):
                ep = 97 if pc.get("p97") and erc >= pc["p97"] else 90 if erc >= pc["p90"] else 80 if pc.get("p80") and erc >= pc["p80"] else ""
            sd.append({"date": ds, "station_id": sid, "name": s["name"], "type": s["type"], "psa": s["psa"], "elev_ft": s.get("elev_ft", ""),
                       "max_temp_f": "" if v.get("t") is None else v["t"], "temp_nbr_f": "" if r["tn"] is None else r["tn"],
                       "temp_diff_f": "" if r["td"] is None else r["td"], "temp_nbrs": r["tk"] or "",
                       "min_rh": "" if v.get("rh") is None else v["rh"], "max_wind_mph": "" if v.get("ws") is None else v["ws"],
                       "max_gust_mph": "" if v.get("g") is None else v["g"], "precip_in": "" if v.get("p") is None else v["p"],
                       "precip_nbr_in": "" if r["pn"] is None else r["pn"], "precip_nbr_max_in": "" if r["pmax"] is None else r["pmax"],
                       "precip_nbrs": r["pk"] or "", "erc": "" if erc is None else erc, "erc_pctl": ep, "bi": "" if bi is None else bi,
                       "normal_high_f": "" if v.get("nh") is None else v["nh"], "flags": " ".join(r["flags"])})
            for f in r["flags"]:
                tf = f in TEMP_BAD
                qf.append({"date": ds, "station_id": sid, "name": s["name"], "type": s["type"], "psa": s["psa"], "flag": f,
                           "value": v.get("t") if tf else v.get("p"), "neighbors": r["tk"] if tf else r["pk"],
                           "neighbor_value": (r["tn"] if tf else (r["pmax"] if f in ("rain_alone", "wet") else r["pn"])) if (r["tk"] if tf else r["pk"]) else "",
                           "note": FLAG_TEXT.get(f, f)})
            a = acc[sid]
            a["days"].append(ds)
            tok, pok = not (set(r["flags"]) & TEMP_BAD), not (set(r["flags"]) & RAIN_BAD)
            if r["t"] is not None and tok:
                a["t"].append(r["t"])
                if v.get("nh") is not None:
                    a["anom"].append(r["t"] - v["nh"])
                    a["nh"].append(v["nh"])
            if r["td"] is not None:
                a["td"].append(r["td"])
            if r["p"] is not None and pok:
                a["p"].append(r["p"])
                if v.get("np") is not None:
                    a["np"].append(v["np"])
            if v.get("rh") is not None and tok:
                a["rh"].append(v["rh"])
            if erc is not None:
                a["erc"].append(erc)
                if pc.get("p90"):
                    a["ep90"].append(1 if erc >= pc["p90"] else 0)
                    a["epc"].append(pc["p90"])
            if bi is not None:
                a["bi"].append(bi)
            for f in r["flags"]:
                a["f_" + f].append(1)
            pv = psa_vals[s["psa"]]
            pv["n"].append(sid)
            if r["t"] is not None and tok:
                pv["t"].append(r["t"])
            if r["p"] is not None and pok:
                pv["p"].append(r["p"])
            if v.get("rh") is not None and tok:
                pv["rh"].append(v["rh"])
            if v.get("ws") is not None:
                pv["ws"].append(v["ws"])
            if erc is not None:
                pv["erc"].append(erc)
            if bi is not None:
                pv["bi"].append(bi)
            if r["flags"]:
                pv["fl"].append(sid)
        for psa, pv in psa_vals.items():
            pd.append({"date": ds, "psa": psa, "stations": len(pv["n"]), "max_temp_f": med(pv["t"]), "precip_in": med(pv["p"]),
                       "rain_stations": len(pv["p"]), "min_rh": med(pv["rh"]), "max_wind_mph": med(pv["ws"]),
                       "erc": med(pv["erc"]), "bi": med(pv["bi"]), "flagged": len(pv["fl"])})

    # station months, with month-long flags (runs hot/cold, reads wet/dry)
    ndays = (end - gaccs.month_start(month)).days + 1
    sm = {}
    for sid, a in acc.items():
        s, qc = by_id[sid], cfg_of(by_id[sid])["qc"]
        flags = []
        hot, cold = len(a["f_hot"]), len(a["f_cold"])
        if hot >= int(qc["persistent_days"]) or (len(a["td"]) >= 10 and mean(a["td"]) != "" and mean(a["td"]) >= 0.6 * float(qc["temp_flag_f"])):
            flags.append("runs hot")
        if cold >= int(qc["persistent_days"]) or (len(a["td"]) >= 10 and mean(a["td"]) != "" and mean(a["td"]) <= -0.6 * float(qc["temp_flag_f"])):
            flags.append("runs cold")
        sm[sid] = {"month": month, "station_id": sid, "name": s["name"], "type": s["type"], "psa": s["psa"], "days": len(a["days"]),
                   "precip_days": len(a["p"]), "precip_in": round(sum(a["p"]), 2) if a["p"] else "", "precip_nbr_in": "",
                   "max_temp_mean_f": mean(a["t"]), "temp_diff_mean_f": mean(a["td"]), "temp_checked_days": len(a["td"]),
                   "hot_days": hot, "cold_days": cold, "rain_flag_days": sum(len(a["f_" + f]) for f in RAIN_BAD),
                   "erc_mean": mean(a["erc"]), "erc_max": max(a["erc"]) if a["erc"] else "", "erc_90_days": sum(a["ep90"]) if a["ep90"] else "",
                   "bi_mean": mean(a["bi"]), "bi_max": max(a["bi"]) if a["bi"] else "",
                   "normal_high_mean_f": mean(a["nh"]), "normal_precip_in": round(sum(a["np"]), 2) if a["np"] else "",
                   "month_flags": flags, "_anom": a["anom"], "_epc": a["epc"]}
    # monthly rain vs neighbors (stations with rain on at least 80% of the days that have passed)
    full = lambda x: x["precip_days"] >= 0.8 * min(ndays, max(1, len(days)))
    for sid, x in sm.items():
        if not full(x):
            continue
        qc = cfg_of(by_id[sid])["qc"]
        nb = [sm[o["station_id"]]["precip_in"] for o, _ in nbrs[sid] if o["station_id"] in sm and full(sm[o["station_id"]])]
        if len(nb) >= int(qc["min_neighbors"]):
            m = statistics.median(nb)
            x["precip_nbr_in"] = round(m, 2)
            ratio = float(qc["month_ratio"])
            if m >= 0.5 and x["precip_in"] <= m / ratio:
                x["month_flags"].append("reads dry")
            elif x["precip_in"] >= 1.0 and x["precip_in"] > ratio * max(m, 0.1):
                x["month_flags"].append("reads wet")

    # PSA months
    pm = []
    for psa in sorted({x["psa"] for x in sm.values()}):
        xs = [x for x in sm.values() if x["psa"] == psa]
        good_t = [x for x in xs if not {"runs hot", "runs cold"} & set(x["month_flags"])]
        good_p = [x for x in xs if not {"reads wet", "reads dry"} & set(x["month_flags"]) and full(x)]
        pdays = [r for r in pd if r["psa"] == psa]
        ercs = [r["erc"] for r in pdays if r["erc"] != ""]
        bis = [r["bi"] for r in pdays if r["bi"] != ""]
        tmax = [r["max_temp_f"] for r in pdays if r["max_temp_f"] != ""]
        anom = [v for x in good_t for v in x["_anom"]]
        norm_p = [x["normal_precip_in"] for x in good_p if x["normal_precip_in"] != ""]
        p_asos = [x["precip_in"] for x in good_p if x["normal_precip_in"] != ""]
        epc = [v for x in xs for v in x["_epc"]]
        p90 = statistics.median(epc) if epc else None
        flagged = [x for x in xs if x["month_flags"] or x["hot_days"] or x["cold_days"] or x["rain_flag_days"]]
        pp = med([x["precip_in"] for x in good_p])
        pn = round(statistics.median(norm_p), 2) if norm_p else ""
        pm.append({"month": month, "psa": psa, "days": len(pdays), "stations": len(xs), "raws": sum(1 for x in xs if x["type"] == "RAWS"),
                   "asos": sum(1 for x in xs if x["type"] == "ASOS"), "precip_in": pp, "precip_stations": len(good_p),
                   "precip_normal_in": pn,
                   "precip_pct_normal": round(100 * statistics.median(p_asos) / pn) if pn and p_asos and pn > 0 and len(days) >= 0.8 * ndays else "",
                   "max_temp_mean_f": mean(tmax), "max_temp_f": max(tmax) if tmax else "",
                   "temp_anom_f": mean(anom), "temp_anom_stations": sum(1 for x in good_t if x["_anom"]),
                   "min_rh_mean": mean([r["min_rh"] for r in pdays if r["min_rh"] != ""]),
                   "erc_mean": mean(ercs), "erc_max": max(ercs) if ercs else "",
                   "erc_90_days": sum(1 for e in ercs if p90 is not None and e >= p90) if p90 is not None else "",
                   "bi_mean": mean(bis), "bi_max": max(bis) if bis else "",
                   "flagged_stations": len(flagged),
                   "flag_list": "; ".join(flag_text(x) for x in flagged),
                   "complete": "yes" if len(days) >= ndays else f"{len(days)} of {ndays} days"})
    smr = [{**{k: v for k, v in x.items() if not k.startswith("_")}, "month_flags": "; ".join(x["month_flags"])} for x in sm.values()]
    return sd, qf, pd, smr, pm, [d for d, _, _ in days]


def flag_text(x):
    """'Name (runs hot, 3 rain flags)' for one station-month."""
    parts = list(x["month_flags"])
    if x["hot_days"] and "runs hot" not in parts:
        parts.append(f"{x['hot_days']} hot day{'s' if x['hot_days'] > 1 else ''}")
    if x["cold_days"] and "runs cold" not in parts:
        parts.append(f"{x['cold_days']} cold day{'s' if x['cold_days'] > 1 else ''}")
    if x["rain_flag_days"]:
        parts.append(f"{x['rain_flag_days']} rain flag{'s' if x['rain_flag_days'] > 1 else ''}")
    return f"{x['name']} ({', '.join(parts)})"


def merge_month(path, rows, cols, month, key="month", keep=None):
    old = [r for r in gaccs.read_csv(path) if (r.get(key) or "")[:7] != month]
    allr = old + rows
    if keep:
        allr = [r for r in allr if keep(r)]
    allr.sort(key=lambda r: tuple(str(r.get(c, "")) for c in cols[:2]))
    gaccs.write_csv(path, allr, cols)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--from", dest="frm")
    ap.add_argument("--to")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--today", help="pretend today is YYYY-MM-DD (testing)")
    args = ap.parse_args()

    psas = common.load_psas()
    locate = common.make_locator(psas)
    codes = sorted({gaccs.gacc_of(p) for p in psas})
    cfgs = {g: gaccs.load_config(g) for g in codes}
    raws = load_raws(locate)
    try:
        asos = load_asos(locate)
    except Exception as e:
        print(f"ASOS/AWOS list unavailable: {e}")
        asos = []
    for s in raws + asos:
        s["lat"], s["lon"] = float(s["lat"]), float(s["lon"])
    raws = [s for s in raws if s["gacc"] in cfgs]
    asos = [s for s in asos if s["gacc"] in cfgs]
    stations = raws + [s for s in asos if cfgs[s["gacc"]].get("use_asos", True)]
    cfg_of = lambda s: cfgs[s["gacc"]]
    print(f"{len(psas)} PSAs in {len(codes)} GACCs; {len(raws)} RAWS, {len(asos)} ASOS/AWOS")

    today = date.fromisoformat(args.today) if args.today else datetime.now(ZoneInfo("America/Denver")).date()
    if args.frm:
        d0, d1 = date.fromisoformat(args.frm), date.fromisoformat(args.to) if args.to else today - timedelta(days=1)
        todo = [(d0 + timedelta(days=k)).isoformat() for k in range((d1 - d0).days + 1)]
    else:
        todo = [(today - timedelta(days=k)).isoformat() for k in range(args.days, 0, -1)]
        todo = [d for d in todo if read_raw(d) is None or (today - date.fromisoformat(d)).days <= REFETCH_DAYS
                or (read_raw(d) or {}).get("missing")]
    fetched = 0
    for i in range(0, len(todo), 31):                       # services take date ranges; a month at a time
        part = todo[i:i + 31]
        if part:
            fetched += fetch_days(part, raws, asos, cfgs)
    print(f"Fetched {fetched} of {len(todo)} day(s)")

    # ERC percentiles per station (refreshed every 90 days)
    pct = {r["station_id"]: {k: num(r.get(k)) for k in ("p80", "p90", "p97")} for r in gaccs.read_csv(PCTL_PATH)}
    if common.stale(PCTL_PATH, 90) and raws:
        by_fm = defaultdict(list)
        for s in raws:
            by_fm[str(cfgs[s["gacc"]].get("erc_fuel_model", "Y")).upper()].append(s["station_id"])
        new = {}
        for fm, ids in by_fm.items():
            for sid, v in fetch_pctl(ids, fm, 20).items():
                new[sid] = {**v, "fuel_model": fm}
        if new:
            pct.update(new)
            gaccs.write_csv(PCTL_PATH, [{"station_id": k, **v} for k, v in sorted(pct.items())], ["station_id", "fuel_model", "p80", "p90", "p97"])

    saved = sorted(f[:10] for f in os.listdir(RAW_DIR)) if os.path.isdir(RAW_DIR) else []
    months = sorted({d[:7] for d in (saved if args.rebuild else todo)} & {d[:7] for d in saved})
    if not months:
        print("No saved days to summarize yet.")
        return 0 if fetched or not todo else 1
    nbrs = neighbor_lists(stations, cfg_of)
    keep_from = (today - timedelta(days=KEEP_DAILY)).isoformat()
    for month in months:
        sd, qf, pd, sm, pm, dlist = build_month(month, stations, nbrs, cfg_of, pct)
        for g in codes:
            gdir = gaccs.gacc_dir(g)
            mine = lambda rows: [r for r in rows if gaccs.gacc_of(r["psa"]) == g]
            merge_month(os.path.join(gdir, "station_daily.csv"), mine(sd), SD_COLS, month, "date", keep=lambda r: r["date"] >= keep_from)
            merge_month(os.path.join(gdir, "qc_flags.csv"), mine(qf), QF_COLS, month, "date")
            merge_month(os.path.join(gdir, "psa_daily.csv"), mine(pd), PD_COLS, month, "date")
            merge_month(os.path.join(gdir, "station_monthly.csv"), mine(sm), SM_COLS, month)
            merge_month(os.path.join(gdir, "psa_monthly.csv"), mine(pm), PM_COLS, month)
        nflag = len({(r["station_id"], r["date"]) for r in qf})
        print(f"{month}: {len(dlist)} day(s), {len({r['station_id'] for r in sd})} stations reporting, {nflag} flagged station-days, "
              f"{sum(1 for r in sm if r['month_flags'])} stations flagged for the month")
    for g in codes:
        gaccs.write_csv(os.path.join(gaccs.gacc_dir(g), "stations.csv"),
                        sorted((s for s in raws + asos if s["gacc"] == g), key=lambda s: (s["psa"], s["name"])),
                        ["station_id", "name", "type", "lat", "lon", "elev_ft", "state", "psa"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
