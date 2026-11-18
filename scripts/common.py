"""
Shared pieces: http with retries (or offline mock files), PSA boundaries, point-in-PSA lookup, distance.

MOCK_DIR=path makes every request read a file from that folder instead (offline testing); see mock_name().
"""

import hashlib
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gaccs  # noqa: E402

MOCK_DIR = os.environ.get("MOCK_DIR")
UA = "PS-Seasonal-Verification (github actions)"
OUTLOOK_SVC = ["https://fsapps.nwcg.gov/psp/arcgis/rest/services/npsg/Outlooks_Monthly_Extended/MapServer",
               "https://fsapps.nwcg.gov/psp/rest/services/npsg/Outlooks_Monthly_Extended/MapServer"]
OUTLOOK_OGC = ["https://fsapps.nwcg.gov/psp/rest/services/npsg/Outlooks_Monthly_Extended/OGCFeatureServer",
               "https://fsapps.nwcg.gov/psp/arcgis/rest/services/npsg/Outlooks_Monthly_Extended/OGCFeatureServer"]
SEVENDAY_PSA = "https://fsapps.nwcg.gov/psp/arcgis/rest/services/npsg/outlooks_forecast/MapServer/0/query"
PSA_GEO = os.path.join(gaccs.DATA, "psa_polygons.geojson")
REFRESH_DAYS = 30


# ------------------------------------------------------------------ http
def mock_name(url, body):
    """File in MOCK_DIR that stands in for this request."""
    u = urllib.parse.urlparse(url)
    q = urllib.parse.parse_qs(u.query)
    if "OGCFeatureServer" in url:
        return f"outlook_ogc{u.path.rstrip('/').split('/')[-2]}.json"
    if "Outlooks_Monthly_Extended" in url:
        layer = u.path.rstrip("/").split("/")[-2]
        return f"outlook_layer{layer}{'_geo' if q.get('f', [''])[0] == 'geojson' else ''}.json"
    if url.endswith(".png"):
        return os.path.basename(u.path)
    if "outlooks_forecast" in url:
        return "psa.geojson"
    if "fems" in url:
        t = body["query"]
        kind = t.split("(")[0].split("{")[1].strip()
        if kind == "stationMetaData":
            st = t.split('stateId: "')[1][:2]
            return f"fems_stations_{st}.json"
        if kind == "percentileLevels":
            return "fems_pctl.json"
        start = t.split('startDate: "')[1][:10]
        return f"fems_{kind}_{start}.json"
    if "geojson/network" in url:
        return "iem_" + u.path.rsplit("/", 1)[1]
    if "daily.py" in url:
        return f"iem_daily_{q['network'][0]}_{q['sts'][0]}.csv"
    if "WFIGS" in url or "InFORM" in url:
        w = q.get("where", [""])[0]
        key = hashlib.md5(w.encode()).hexdigest()[:8]
        name = u.path.split("/services/")[1].split("/")[0]
        p = os.path.join(MOCK_DIR, f"{name}_{key}.json")
        return os.path.basename(p) if os.path.exists(p) else f"{name}.json"
    raise RuntimeError("no mock for " + url)


def http(url, body=None, retries=3, timeout=120, binary=False):
    if MOCK_DIR:
        p = os.path.join(MOCK_DIR, mock_name(url, body))
        if not os.path.exists(p):
            raise RuntimeError(f"mock missing: {os.path.basename(p)}")
        with open(p, "rb") as fh:
            b = fh.read()
        return b if binary else b.decode("utf-8", "replace")
    last = None
    for i in range(retries):
        try:
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(url, data=data, headers={
                "User-Agent": UA, "Content-Type": "application/json", "Accept": "application/json, */*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                b = r.read()
            return b if binary else b.decode("utf-8", "replace")
        except Exception as e:
            last = e
            time.sleep(4 * (i + 1))
    raise RuntimeError(f"{url[:140]} failed: {last}")


def arcgis_query(base, where="1=1", geometry=False, out_fields="*", geojson=False, page=2000, extra=None):
    """All features from an ArcGIS layer query. Pages through big layers; if the server refuses
    paging parameters it retries once without them (fine for layers under its record limit)."""
    def get(q):
        txt = http(base + "/query?" + urllib.parse.urlencode(q), retries=3, timeout=150)
        try:
            j = json.loads(txt)
        except ValueError:
            raise RuntimeError(f"not JSON from {base}: {txt[:200]!r}")
        if "error" in j:
            raise RuntimeError(f"service error from {base}: {j['error']}")
        return j
    q0 = {"where": where, "outFields": out_fields, "returnGeometry": "true" if geometry else "false",
          "outSR": "4326", "f": "geojson" if geojson else "json"}
    q0.update(extra or {})
    feats, offset = [], 0
    while True:
        try:
            j = get({**q0, "resultOffset": offset, "resultRecordCount": page})
        except RuntimeError as e:
            if offset or "service error" not in str(e) and "not JSON" not in str(e):
                raise
            print(f"  paged query refused ({str(e)[:200]}); trying without paging")
            return get(q0).get("features") or []
        batch = j.get("features") or []
        feats += batch
        more = j.get("exceededTransferLimit") or (j.get("properties") or {}).get("exceededTransferLimit")
        if not batch or not more or MOCK_DIR:
            return feats
        offset += len(batch)


def ogc_items(base, collection, limit=2000):
    """Features from an ArcGIS OGC Features endpoint as [{'attributes': ..., 'geometry': ...}] (fallback)."""
    url = f"{base}/collections/{collection}/items?" + urllib.parse.urlencode({"f": "json", "limit": limit})
    j = json.loads(http(url, retries=3, timeout=150))
    return [{"attributes": f.get("properties") or {}, "geometry": f.get("geometry")} for f in j.get("features") or []]


def num(v):
    try:
        x = float(v)
        return None if math.isnan(x) else x
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ geometry
def point_in_ring(x, y, ring):
    inside, j = False, len(ring) - 1
    for i in range(len(ring)):
        xi, yi, xj, yj = ring[i][0], ring[i][1], ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def polys_of(geom):
    return geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]


def bbox(geom):
    xs = [p[0] for poly in polys_of(geom) for p in poly[0]]
    ys = [p[1] for poly in polys_of(geom) for p in poly[0]]
    return min(xs), min(ys), max(xs), max(ys)


def make_locator(psas):
    boxes = {k: bbox(g) for k, g in psas.items()}

    def locate(lat, lon):
        for code, g in psas.items():
            x0, y0, x1, y1 = boxes[code]
            if not (x0 <= lon <= x1 and y0 <= lat <= y1):
                continue
            for poly in polys_of(g):
                if poly and point_in_ring(lon, lat, poly[0]) and not any(point_in_ring(lon, lat, h) for h in poly[1:]):
                    return code
        return None
    return locate


def dist_km(lat1, lon1, lat2, lon2):
    r = math.radians
    h = math.sin(r(lat2 - lat1) / 2) ** 2 + math.cos(r(lat1)) * math.cos(r(lat2)) * math.sin(r(lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(min(1.0, h)))


def stale(p, days=REFRESH_DAYS):
    return not os.path.exists(p) or time.time() - os.path.getmtime(p) > days * 86400


def thin(ring, step=0.004):
    out = []
    for x, y in ring:
        p = [round(x, 3), round(y, 3)]
        if not out or abs(p[0] - out[-1][0]) + abs(p[1] - out[-1][1]) >= step:
            out.append(p)
    if out and out[0] != out[-1]:
        out.append(out[0])
    return out


def _psa_features():
    """[(code, name, geometry)] from the monthly outlook service (falls back to the 7-day service)."""
    for base in OUTLOOK_SVC:
        try:
            feats = arcgis_query(base + "/0", geometry=True, out_fields="PSANationa,PSANAME", geojson=True,
                                 extra={"maxAllowableOffset": "0.005"})
        except Exception as e:
            print(f"  PSA boundaries from {base}: {e}")
            continue
        out = []
        for f in feats:
            p = {k.lower(): v for k, v in (f.get("properties") or {}).items()}
            out.append((str(p.get("psanationa") or "").strip().upper(), str(p.get("psaname") or "").strip(), f.get("geometry")))
        if out:
            return out
    params = {"where": "1=1", "outFields": "nat_code", "returnGeometry": "true", "maxAllowableOffset": "0.005", "outSR": "4326", "f": "geojson"}
    gj = json.loads(http(SEVENDAY_PSA + "?" + urllib.parse.urlencode(params)))
    return [(str({k.lower(): v for k, v in (f.get("properties") or {}).items()}.get("nat_code") or "").upper(), "", f.get("geometry"))
            for f in gj.get("features", [])]


def valid_psa(code):
    return bool(code) and code not in ("NONE", "NULL") and bool(__import__("re").fullmatch(r"[A-Z]{2}[A-Z0-9]{1,6}", code))


def load_psas():
    """{psa: geometry}; refreshed monthly into data/psa_polygons.geojson (plus a smaller file per GACC)."""
    if stale(PSA_GEO):
        merged, names = defaultdict(list), {}
        for code, name, g in _psa_features():
            if valid_psa(code) and g and g.get("type") in ("Polygon", "MultiPolygon"):
                merged[code].extend([[thin(r) for r in poly] for poly in polys_of(g)])
                if name:
                    names[code] = name
        if not merged:
            if os.path.exists(PSA_GEO):
                print("  PSA boundaries unavailable; using the saved copy")
            else:
                raise RuntimeError("no PSA polygons returned")
        else:
            feats = [{"type": "Feature", "properties": {"psa": k, "gacc": gaccs.gacc_of(k), "name": names.get(k, "")},
                      "geometry": {"type": "MultiPolygon", "coordinates": v}} for k, v in sorted(merged.items())]
            os.makedirs(gaccs.DATA, exist_ok=True)
            with open(PSA_GEO, "w") as fh:
                json.dump({"type": "FeatureCollection", "features": feats}, fh, separators=(",", ":"))
            for g in {f["properties"]["gacc"] for f in feats}:
                with open(os.path.join(gaccs.gacc_dir(g), "psa_polygons.geojson"), "w") as fh:
                    json.dump({"type": "FeatureCollection", "features": [f for f in feats if f["properties"]["gacc"] == g]}, fh, separators=(",", ":"))
            by = defaultdict(list)
            for k in merged:
                by[gaccs.gacc_of(k)].append(k)
            gaccs.update_index(by, names)
    with open(PSA_GEO) as fh:
        return {f["properties"]["psa"]: f["geometry"] for f in json.load(fh)["features"]}
