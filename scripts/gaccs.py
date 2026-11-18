"""
Shared GACC definitions and helpers for the monthly Significant Wildland Fire Potential Outlook
verification site.

PSA codes in the national services start with the GACC's two-letter prefix (SW05, NR12, SA21C, ...).
Each GACC has its own settings file, config/gaccs/<CODE>.json, created with defaults the first
time that GACC appears. Edit it by hand (or on GitHub) to change that GACC's settings.
"""

import csv
import json
import os
import re
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config", "gaccs")
INDEX_PATH = os.path.join(ROOT, "config", "gaccs.json")
DATA = os.path.join(ROOT, "data")

# prefix -> (center, name, time zone used for "the day" and "the month")
KNOWN = {
    "NW": ("NWCC", "Northwest", "America/Los_Angeles"),
    "NC": ("ONCC", "Northern California", "America/Los_Angeles"),
    "SC": ("OSCC", "Southern California", "America/Los_Angeles"),
    "GB": ("GBCC", "Great Basin", "America/Boise"),
    "NR": ("NRCC", "Northern Rockies", "America/Denver"),
    "RM": ("RMCC", "Rocky Mountain", "America/Denver"),
    "SW": ("SWCC", "Southwest", "America/Denver"),
    "SA": ("SACC", "Southern Area", "America/Chicago"),
    "EA": ("EACC", "Eastern Area", "America/Chicago"),
    "AK": ("AICC", "Alaska", "America/Anchorage"),
}
# states searched for RAWS and ASOS/AWOS (stations are then assigned to PSAs by location)
STATES = ["AL", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME",
          "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
          "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "PR", "AK", "HI"]

DEFAULT_QC = {
    "neighbor_km": 80,              # stations within this distance are compared with each other
    "min_neighbors": 3,             # a value is only checked when at least this many neighbors reported
    "max_neighbors": 8,             # only the nearest this many are used
    "max_elev_diff_ft": 3000,       # temperature neighbors must be within this much elevation
    "lapse_f_per_1000ft": 3.5,      # neighbor max temps are adjusted to the station's elevation
    "temp_flag_f": 8,               # flag a max temp this much warmer (or colder) than its neighbors
    "rain_flag_in": 0.5,            # flag a daily total this large when every neighbor stayed nearly dry...
    "dry_neighbor_in": 0.05,        # ...meaning no neighbor got more than this
    "missed_rain_in": 0.5,          # flag 0.00 when the nearest neighbors' median is at least this much
    "max_daily_rain_in": 12,        # anything above this is treated as a bad report
    "persistent_days": 5,           # a station flagged hot (or cold) this many days in a month is marked "runs hot"
    "month_ratio": 3,               # a monthly total this many times its neighbors' (or 1/x of it) is marked wet/dry
}


def gacc_of(psa):
    m = re.match(r"[A-Z]+", str(psa or "").upper())
    return m.group(0) if m else "XX"


def default_config(code, gacc_field=""):
    center, name, tz = KNOWN.get(code, (code + "CC", gacc_field or code, "America/Denver"))
    return {
        "_readme": "Settings for this GACC. Edit this file (on GitHub or by hand) to change them.",
        "code": code, "center": center, "name": name, "timezone": tz,
        "sig_fire_acres": 100,          # a wildfire that reaches this size counts as a significant fire in its discovery month
        "normal_years": 10,             # "normal" = the same month in this many previous years
        "min_fires_for_above": 2,       # a month needs at least this many significant fires to count as Above normal
        "erc_fuel_model": "Y",          # NFDRS 2016 fuel model for ERC and BI (V, W, X, Y or Z)
        "use_asos": True,               # also use ASOS/AWOS airport stations (Iowa Environmental Mesonet)
        "fire_min_acres": 100,          # nightly fire-growth days: growth of at least this many acres in one day
        "qc": dict(DEFAULT_QC),
    }


def config_path(code):
    return os.path.join(CONFIG_DIR, f"{code}.json")


def load_config(code, create=True, gacc_field=""):
    p = config_path(code)
    if not os.path.exists(p):
        if not create:
            return None
        save_config(code, default_config(code, gacc_field))
    with open(p) as fh:
        cfg = json.load(fh)
    base = default_config(code, gacc_field)       # fill in anything added since the file was written
    for k, v in base.items():
        cfg.setdefault(k, v)
    cfg["qc"] = {**DEFAULT_QC, **(cfg.get("qc") or {})}
    return cfg


def save_config(code, cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(config_path(code), "w") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")


def gacc_dir(code):
    p = os.path.join(DATA, code)
    os.makedirs(p, exist_ok=True)
    return p


def update_index(psas_by_gacc, names=None):
    """config/gaccs.json lists the GACCs and their PSAs (code -> name); the website reads it to build the GACC bar."""
    names = names or {}
    idx = {"gaccs": []}
    if os.path.exists(INDEX_PATH):
        with open(INDEX_PATH) as fh:
            idx = json.load(fh)
    known = {g["code"]: g for g in idx.get("gaccs", [])}
    for code, psas in psas_by_gacc.items():
        cfg = load_config(code)
        g = known.get(code, {"code": code})
        pn = dict(g.get("psa_names") or {})
        pn.update({p: n for p, n in names.items() if gacc_of(p) == code and n})
        g.update({"name": cfg.get("name", code), "center": cfg.get("center", ""),
                  "psas": sorted(set(g.get("psas", [])) | set(psas)), "psa_names": dict(sorted(pn.items()))})
        known[code] = g
    order = list(KNOWN)
    idx["gaccs"] = sorted(known.values(), key=lambda g: (order.index(g["code"]) if g["code"] in order else 99, g["code"]))
    os.makedirs(os.path.dirname(INDEX_PATH), exist_ok=True)
    with open(INDEX_PATH, "w") as fh:
        json.dump(idx, fh, indent=1)
        fh.write("\n")
    return idx


def index_psas():
    """{gacc: [psa, ...]} from config/gaccs.json."""
    if not os.path.exists(INDEX_PATH):
        return {}
    with open(INDEX_PATH) as fh:
        return {g["code"]: list(g.get("psas") or []) for g in json.load(fh).get("gaccs", [])}


# ------------------------------------------------------------------ months
def ym(d):
    return f"{d.year:04d}-{d.month:02d}"


def add_months(ym_s, n):
    y, m = int(ym_s[:4]), int(ym_s[5:7])
    k = y * 12 + (m - 1) + n
    return f"{k // 12:04d}-{k % 12 + 1:02d}"


def month_start(ym_s):
    return date(int(ym_s[:4]), int(ym_s[5:7]), 1)


def month_end(ym_s):
    return date.fromordinal(month_start(add_months(ym_s, 1)).toordinal() - 1)


def month_of(iso):
    return str(iso)[:7]


# ------------------------------------------------------------------ csv
def read_csv(p):
    if not os.path.exists(p):
        return []
    with open(p, newline="") as fh:
        return list(csv.DictReader(fh))


def write_csv(p, rows, cols):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, p)


def pctl(vals, q):
    """Percentile with linear interpolation (q from 0 to 100)."""
    v = sorted(vals)
    if not v:
        return None
    k = (len(v) - 1) * q / 100.0
    lo = int(k)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)
