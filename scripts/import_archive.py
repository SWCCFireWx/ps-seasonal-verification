#!/usr/bin/env python3
"""
Load past National Significant Wildland Fire Potential Outlooks from the NIFC archive.

NIFC keeps one zip per issuance (2023 onward) on https://www.nifc.gov/nicc/predictive-services/outlooks
("National Significant Wildland Fire Potential Archive"); older ones are available by email from
nicc.predictiveservices@firenet.gov. Each zip covers four months, named by their letters
(202507_JASO = issued July 2025 for July, August, September, October).

What's read from a zip, in order of preference:
  * shapefiles (.shp/.dbf, one per outlook month): a PSA code field (PSANationa or similar) and a
    category field (Above / Normal / Below) -> used directly. Shapefiles of Above/Below areas without
    PSA codes are matched to PSAs by where most of each PSA falls (geographic or Web Mercator only).
  * KML / KMZ: the same, from placemark data, names or styles.
  * CSV / Excel-exported text with PSA and category columns.
  * Images and PDFs are saved beside the issue (data/raw/outlooks/<issue>/) as the record; they
    can't be turned into PSA categories automatically.
Which file is Month 1-4 comes from its name (month1, M2, "Aug", "August", ...).

Issues already archived from the live service are never overwritten (use --replace to allow it).
Writes data/<GACC>/outlooks.csv, logs each issue in data/outlook_log.csv and data/archive_import_log.csv.

Usage:
    python scripts/import_archive.py                     # every zip linked on the NIFC page
    python scripts/import_archive.py --years 2024-2025   # only these years
    python scripts/import_archive.py --zip a.zip b.zip   # zips you have (e.g. emailed older years)
    python scripts/import_archive.py --list              # show what's on the page, change nothing
"""

import argparse
import csv
import html
import io
import json
import math
import os
import re
import struct
import sys
import urllib.parse
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gaccs  # noqa: E402
import common  # noqa: E402

PAGE = "https://www.nifc.gov/nicc/predictive-services/outlooks"
SITE = "https://www.nifc.gov"
RAW_DIR = os.path.join(gaccs.DATA, "raw", "outlooks")
LOG_PATH = os.path.join(gaccs.DATA, "outlook_log.csv")
IMPORT_LOG = os.path.join(gaccs.DATA, "archive_import_log.csv")
COLS = ["issue_month", "lead", "valid_month", "psa", "gacc", "psa_name", "potential", "update_text", "pulled_at_utc"]
CODES = {"JFMA": 1, "FMAM": 2, "MAMJ": 3, "AMJJ": 4, "MJJA": 5, "JJAS": 6, "JASO": 7, "ASON": 8, "SOND": 9, "ONDJ": 10, "NDJF": 11, "DJFM": 12}
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]


# ------------------------------------------------------------------ which issue, which month
def issue_of(name):
    """'.../2025/July (JASO)/202507_JASO.zip', '2023.JJAS_.zip', '2023_10-ONDJ.zip' -> issue month 'YYYY-MM'."""
    s = urllib.parse.unquote(os.path.basename(name))
    full = urllib.parse.unquote(name)
    code = next((c for c in re.findall(r"(?<![A-Za-z])([A-Z]{4})(?![a-z])", s) if c in CODES), None)
    m = re.search(r"(20\d\d)[ _.-]?(0[1-9]|1[0-2])(?!\d)", s)
    if m and (not code or CODES[code] == int(m.group(2))):
        return f"{m.group(1)}-{m.group(2)}"
    years = re.findall(r"(?<!\d)(20\d\d)(?!\d)", s) or re.findall(r"(?<!\d)(20\d\d)(?!\d)", full)
    if code and years:
        return f"{years[-1]}-{CODES[code]:02d}"
    return None


def lead_of(fname, issue):
    """Which outlook month (1-4) a file inside a zip is, from its name; None if it can't tell."""
    b = os.path.basename(fname).lower()
    m = re.search(r"(?:month|mon|mo|m|outlook|day)[ _-]?([1-4])(?!\d)", b)
    if m:
        return int(m.group(1))
    m = re.search(r"(?<![0-9])([1-4])(?:st|nd|rd|th)?[ _-]?month", b)
    if m:
        return int(m.group(1))
    start = int(issue[5:]) - 1
    for k in range(4):
        mn = MONTHS[(start + k) % 12]
        if re.search(rf"(?<![a-z]){mn}(?![a-z])|(?<![a-z]){mn[:3]}(?![a-z])", b):
            return k + 1
    m = re.search(r"(20\d\d)[_-]?(0[1-9]|1[0-2])(?!\d)", b)          # a valid-month date in the name
    if m:
        k = (int(m.group(1)) * 12 + int(m.group(2))) - (int(issue[:4]) * 12 + int(issue[5:]))
        if 0 <= k < 4:
            return k + 1
    return None


def category_of(v):
    s = str(v or "").strip().lower()
    if s.startswith("above"):
        return "Above"
    if s.startswith("below"):
        return "Below"
    if s.startswith("normal") or s in ("near normal", "near-normal"):
        return "Normal"
    return None


# ------------------------------------------------------------------ dbf / shp (no extra packages)
def read_dbf(b):
    n, hlen = struct.unpack("<IH", b[4:10])
    fields, p = [], 32
    while b[p] != 0x0D and p < hlen:
        name = b[p:p + 11].split(b"\x00")[0].decode("latin-1").strip()
        fields.append((name, chr(b[p + 11]), b[p + 16]))
        p += 32
    rlen = struct.unpack("<H", b[10:12])[0]
    rows = []
    for i in range(n):
        r = b[hlen + i * rlen: hlen + (i + 1) * rlen]
        if not r or r[0:1] == b"*":
            rows.append(None)
            continue
        o, row = 1, {}
        for name, _, ln in fields:
            row[name] = r[o:o + ln].decode("latin-1", "replace").strip()
            o += ln
        rows.append(row)
    return rows


def read_shp(b):
    """[[ring, ...] per record] for polygon shapefiles (types 5, 15, 25)."""
    out, p = [], 100
    while p + 8 <= len(b):
        ln = struct.unpack(">i", b[p + 4:p + 8])[0] * 2
        rec = b[p + 8:p + 8 + ln]
        p += 8 + ln
        if len(rec) < 44 or struct.unpack("<i", rec[:4])[0] not in (5, 15, 25):
            out.append([])
            continue
        nparts, npts = struct.unpack("<ii", rec[36:44])
        parts = list(struct.unpack(f"<{nparts}i", rec[44:44 + 4 * nparts])) + [npts]
        base = 44 + 4 * nparts
        pts = [struct.unpack("<dd", rec[base + 16 * k: base + 16 * k + 16]) for k in range(npts)]
        out.append([pts[parts[k]:parts[k + 1]] for k in range(nparts)])
    return out


def to_lonlat(prj):
    """Coordinate converter from a .prj, or None if the projection isn't one we can undo."""
    t = (prj or "").upper()
    if not t or t.startswith("GEOGCS") or "GEOGCRS" in t[:10]:
        return lambda x, y: (x, y)
    if "MERCATOR" in t and ("AUXILIARY_SPHERE" in t or "3857" in t or "WEB" in t or "PSEUDO" in t):
        R = 6378137.0
        return lambda x, y: (math.degrees(x / R), math.degrees(2 * math.atan(math.exp(y / R)) - math.pi / 2))
    return None


# ------------------------------------------------------------------ turning a layer into PSA categories
def layer_rows(records, psa_names):
    """records: [(attrs dict, rings or None)]. Returns ({psa: category}, how) or (None, reason)."""
    known = set(psa_names)
    by_name = {v.strip().lower(): k for k, v in psa_names.items() if v}
    # 1. a PSA code field
    out = {}
    for a, _ in records:
        if not a:
            continue
        cat = next((category_of(v) for v in a.values() if category_of(v)), None)
        psa = next((str(v).strip().upper() for v in a.values() if str(v).strip().upper() in known), None)
        if psa is None:
            psa = next((by_name[str(v).strip().lower()] for v in a.values() if str(v).strip().lower() in by_name), None)
        if psa and cat:
            out[psa] = cat
    if len(out) >= max(5, 0.3 * len([a for a, _ in records if a])):
        return out, "PSA codes"
    # 2. Above/Below areas: give each PSA the category covering most of it
    areas = [(next((category_of(v) for v in (a or {}).values() if category_of(v)), None), rings) for a, rings in records]
    areas = [(c, r) for c, r in areas if c and r]
    if not areas:
        return None, "no PSA codes or Above/Below categories found"
    return areas, "areas"


def psas_from_areas(areas, psas_geo):
    out = {}
    boxes = [(c, None, rings) for c, rings in areas]
    for code, g in psas_geo.items():
        pts = sample_points(g, 25)
        votes = Counter()
        for x, y in pts:
            hit = None
            for c, _, rings in boxes:
                inside = False
                for r in rings:
                    if common.point_in_ring(x, y, r):
                        inside = not inside
                if inside:
                    hit = c
                    break
            votes[hit or "Normal"] += 1
        if pts:
            out[code] = votes.most_common(1)[0][0]
    return out


def sample_points(geom, n):
    x0, y0, x1, y1 = common.bbox(geom)
    pts, k = [], int(math.sqrt(n * 3)) + 2
    for i in range(k):
        for j in range(k):
            x, y = x0 + (i + .5) * (x1 - x0) / k, y0 + (j + .5) * (y1 - y0) / k
            for poly in common.polys_of(geom):
                if poly and common.point_in_ring(x, y, poly[0]) and not any(common.point_in_ring(x, y, h) for h in poly[1:]):
                    pts.append((x, y))
                    break
    return pts


def kml_records(text):
    recs = []
    styles = {}
    for m in re.finditer(r'<Style[^>]*id="([^"]+)"[^>]*>(.*?)</Style>', text, re.S):
        styles["#" + m.group(1)] = m.group(2)
    for pm in re.findall(r"<Placemark[^>]*>(.*?)</Placemark>", text, re.S):
        a = {}
        for k, v in re.findall(r'<(?:SimpleData|Data) name="([^"]+)"[^>]*>\s*(?:<value>)?(.*?)(?:</value>)?\s*</(?:SimpleData|Data)>', pm, re.S):
            a[k] = html.unescape(re.sub(r"<[^>]+>", "", v)).strip()
        nm = re.search(r"<name>(.*?)</name>", pm, re.S)
        if nm:
            a["_name"] = html.unescape(nm.group(1)).strip()
        st = re.search(r"<styleUrl>(.*?)</styleUrl>", pm)
        if st:
            a["_style"] = st.group(1).lstrip("#")
        desc = re.search(r"<description>(.*?)</description>", pm, re.S)
        if desc:
            d = html.unescape(desc.group(1))
            for k, v in re.findall(r"<t[dh][^>]*>\s*([^<]+?)\s*</t[dh]>\s*<td[^>]*>\s*([^<]*?)\s*</td>", d):
                a.setdefault(k, v)
            for w in ("Above", "Below", "Normal"):
                if re.search(rf"\b{w}\b", d) and not any(category_of(v) for v in a.values()):
                    a["_desc_cat"] = w
        rings = []
        for poly in re.findall(r"<Polygon>(.*?)</Polygon>", pm, re.S):
            for cs in re.findall(r"<coordinates>(.*?)</coordinates>", poly, re.S):
                rings.append([tuple(map(float, p.split(",")[:2])) for p in cs.split() if p.count(",") >= 1])
        recs.append((a, rings or None))
    return recs


def csv_records(text):
    return [(r, None) for r in csv.DictReader(io.StringIO(text))]


# ------------------------------------------------------------------ one zip
def read_zip(blob, issue, psa_names, psas_geo, save_dir):
    """{lead: {psa: category}}, notes."""
    z = zipfile.ZipFile(io.BytesIO(blob))
    names = [n for n in z.namelist() if not n.endswith("/") and "__MACOSX" not in n]
    for n in list(names):                                   # zips inside zips
        if n.lower().endswith(".zip"):
            try:
                inner = zipfile.ZipFile(io.BytesIO(z.read(n)))
                for m in inner.namelist():
                    names.append(("inner", n, m))
            except zipfile.BadZipFile:
                pass
    read = lambda n: (zipfile.ZipFile(io.BytesIO(z.read(n[1]))).read(n[2]) if isinstance(n, tuple) else z.read(n))
    nm = lambda n: n[2] if isinstance(n, tuple) else n
    notes, layers = [], {}
    os.makedirs(save_dir, exist_ok=True)
    for n in names:                                         # keep images and PDFs as the record
        if nm(n).lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".pdf")):
            fn = re.sub(r"[^\w.-]+", "_", os.path.basename(nm(n)))
            lead = lead_of(nm(n), issue)
            if lead and nm(n).lower().endswith(".png") and not os.path.exists(os.path.join(save_dir, f"month{lead}_outlook.png")):
                fn = f"month{lead}_outlook.png"
            with open(os.path.join(save_dir, fn), "wb") as fh:
                fh.write(read(n))
    # shapefiles
    shps = [n for n in names if nm(n).lower().endswith(".shp")]
    for n in shps:
        stem = nm(n)[:-4]
        sib = {nm(x)[len(stem):].lower(): x for x in names if nm(x)[:-4] == stem}
        if ".dbf" not in sib:
            notes.append(f"{os.path.basename(nm(n))}: no .dbf")
            continue
        attrs = read_dbf(read(sib[".dbf"]))
        rings = None
        conv = to_lonlat(read(sib[".prj"]).decode("latin-1") if ".prj" in sib else "")
        if conv:
            rings = [[[conv(x, y) for x, y in r] for r in rec] for rec in read_shp(read(n))]
        recs = [(a, rings[i] if rings and i < len(rings) else None) for i, a in enumerate(attrs)]
        layers.setdefault(lead_of(nm(n), issue), []).append((os.path.basename(nm(n)), recs, conv is not None))
    # KML / KMZ
    for n in names:
        low = nm(n).lower()
        text = None
        if low.endswith(".kml"):
            text = read(n).decode("utf-8", "replace")
        elif low.endswith(".kmz"):
            kz = zipfile.ZipFile(io.BytesIO(read(n)))
            k = next((x for x in kz.namelist() if x.lower().endswith(".kml")), None)
            text = kz.read(k).decode("utf-8", "replace") if k else None
        if text:
            layers.setdefault(lead_of(nm(n), issue), []).append((os.path.basename(nm(n)), kml_records(text), True))
        elif low.endswith(".csv"):
            layers.setdefault(lead_of(nm(n), issue), []).append((os.path.basename(nm(n)), csv_records(read(n).decode("utf-8-sig", "replace")), False))
    out = {}
    # a single file covering all four months (a month column)
    if None in layers and len(layers) == 1:
        for fname, recs, _ in layers[None]:
            split = defaultdict(list)
            for a, r in recs:
                ld = next((lead_of(str(v), issue) for k, v in (a or {}).items() if re.search(r"month|period|valid", k, re.I)), None)
                split[ld].append((a, r))
            if len(split) > 1 and None not in split:
                for ld, rr in split.items():
                    layers.setdefault(ld, []).append((fname, rr, True))
        if len(layers) > 1:
            layers.pop(None, None)
    for lead, items in sorted(layers.items(), key=lambda x: (x[0] is None, x[0] or 0)):
        if lead is None:
            notes.append("couldn't tell which outlook month: " + ", ".join(f for f, _, _ in items))
            continue
        for fname, recs, geo_ok in items:
            res, how = layer_rows(recs, psa_names)
            if res is None:
                notes.append(f"{fname}: {how}")
                continue
            if how == "areas":
                if not geo_ok:
                    notes.append(f"{fname}: Above/Below areas in a projection this script can't read")
                    continue
                res = psas_from_areas(res, psas_geo)
                how = "matched by area"
            if res:
                out.setdefault(lead, {}).update(res)
                notes.append(f"Month {lead} from {fname} ({how}, {len(res)} PSAs)")
                break
    if not out:
        kinds = sorted({os.path.splitext(nm(n))[1].lower() for n in names})
        notes.append("no PSA categories found (files: " + ", ".join(kinds) + "); images/PDFs saved as the record")
    return out, notes


# ------------------------------------------------------------------ the archive page
def page_zips():
    text = common.http(PAGE, retries=3, timeout=90)
    links = set()
    for href in re.findall(r'href="([^"]+?\.zip)"', text, re.I):
        u = html.unescape(href)
        if "archive" not in u.lower() and "outlook" not in u.lower():
            continue
        links.add(urllib.parse.urljoin(SITE, u))
    return sorted(links)


def save_issue(issue, cats, names, source):
    pulled = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    for lead, d in cats.items():
        vm = gaccs.add_months(issue, lead - 1)
        for psa, c in d.items():
            rows.append({"issue_month": issue, "lead": lead, "valid_month": vm, "psa": psa, "gacc": gaccs.gacc_of(psa),
                         "psa_name": names.get(psa, ""), "potential": c, "update_text": f"NIFC archive: {source}", "pulled_at_utc": pulled})
    by = defaultdict(list)
    for r in rows:
        by[r["gacc"]].append(r)
    for g, grows in by.items():
        gaccs.load_config(g)
        p = os.path.join(gaccs.gacc_dir(g), "outlooks.csv")
        keep = [r for r in gaccs.read_csv(p) if r["issue_month"] != issue]
        allr = sorted(keep + grows, key=lambda r: (r["issue_month"], int(r["lead"]), r["psa"]))
        gaccs.write_csv(p, allr, COLS)
    log = gaccs.read_csv(LOG_PATH)
    log.append({"issue_month": issue, "pulled_at_utc": pulled, "rows": len(rows), "hash": "", "note": f"imported from NIFC archive ({source})"})
    gaccs.write_csv(LOG_PATH, log, ["issue_month", "pulled_at_utc", "rows", "hash", "note"])
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", help="e.g. 2025 or 2023-2025")
    ap.add_argument("--zip", nargs="*", help="zip files on disk instead of the NIFC page")
    ap.add_argument("--replace", action="store_true", help="overwrite issues already saved from the live service")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.zip:
        sources = [(p, None) for p in args.zip]
    else:
        sources = [(u, None) for u in page_zips()]
    if args.years:
        a, _, b = args.years.partition("-")
        lo, hi = int(a), int(b or a)
        sources = [(s, x) for s, x in sources if (issue_of(s) or "0000")[:4].isdigit() and lo <= int((issue_of(s) or "0000")[:4]) <= hi]
    print(f"{len(sources)} archive zip(s) to look at")
    if args.list:
        for s, _ in sources:
            print(f"  {issue_of(s) or '??'}  {urllib.parse.unquote(s)}")
        return 0

    psas_geo = common.load_psas()
    idx = json.load(open(gaccs.INDEX_PATH)) if os.path.exists(gaccs.INDEX_PATH) else {"gaccs": []}
    names = {p: "" for p in psas_geo}
    for g in idx.get("gaccs", []):
        names.update(g.get("psa_names") or {})
    live = {r["issue_month"] for r in gaccs.read_csv(LOG_PATH) if r.get("rows") not in ("", "0") and "archive" not in (r.get("note") or "")}
    log = gaccs.read_csv(IMPORT_LOG)
    done = 0
    for src, _ in sources:
        issue = issue_of(src)
        label = urllib.parse.unquote(os.path.basename(src))
        if not issue:
            print(f"  {label}: can't tell the issue month from the name; skipped")
            continue
        if issue in live and not args.replace:
            print(f"  {issue}: already saved from the live service; skipped")
            continue
        try:
            blob = open(src, "rb").read() if os.path.exists(src) else common.http(src, retries=3, timeout=180, binary=True)
            cats, notes = read_zip(blob, issue, names, psas_geo, os.path.join(RAW_DIR, issue))
        except Exception as e:
            cats, notes = {}, [f"couldn't read: {e}"]
        n = save_issue(issue, cats, names, label) if cats else 0
        done += bool(n)
        print(f"  {issue}: {n} PSA rows" + "".join(f"\n      {x}" for x in notes))
        log.append({"issue_month": issue, "source": label, "rows": n, "months": ",".join(map(str, sorted(cats))),
                    "notes": " | ".join(notes)[:900], "imported_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    gaccs.write_csv(IMPORT_LOG, log, ["issue_month", "source", "rows", "months", "notes", "imported_at_utc"])
    print(f"Imported {done} issue(s). Run verify_outlook.py (the workflow does) to score them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
