#!/usr/bin/env python3
"""
FireWatch service — continuous ground-temperature and wildfire monitoring.

What it does
------------
* Polls NASA FIRMS for VIIRS 375 m (S-NPP / NOAA-20 / NOAA-21) and MODIS 1 km
  active-fire detections.
* Optionally pulls NOAA GOES-East / GOES-West ABI Level-2 Fire/Hot-Spot
  Characterization straight from the public AWS mirror. This is the fast
  channel: the CONUS sector is re-imaged every 5 minutes, so a fire can
  surface here hours before a polar orbiter flies over.
* Stores every detection in SQLite, so "new since last check" survives
  restarts and you get real change detection rather than a rolling snapshot.
* Clusters detections into incidents, tracks whether each one is growing,
  and raises alerts (console, optional webhook) the moment a cluster appears
  or intensifies.
* Serves firewatch.html plus a small JSON API, so the dashboard picks up the
  richer server feed automatically.

Dependencies
------------
Core path is the Python standard library only — no pip install needed:

    python3 firewatch_server.py --firms-key YOUR_KEY

The GOES rapid-scan channel additionally needs numpy and an HDF5 reader:

    pip install numpy h5netcdf        # or: pip install numpy netCDF4

Without them the server logs one line and carries on with FIRMS alone.

Get a free FIRMS map key at https://firms.modaps.eosdis.nasa.gov/api/map_key/
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import math
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable

try:
    from firewatch_effis import EffisClient, fwi_class      # noqa: F401
except ImportError:                                         # optional module
    EffisClient = None

VERSION = "1.1"
HERE = Path(__file__).resolve().parent
UA = {"User-Agent": f"FireWatch/{VERSION} (+satellite fire monitoring)"}

log = logging.getLogger("firewatch")


# =====================================================================
# helpers
# =====================================================================

def utcnow() -> float:
    return datetime.now(timezone.utc).timestamp()


def http_get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def norm_conf(source: str, raw: Any) -> int:
    """Collapse three incompatible confidence schemes onto 0/1/2."""
    if raw is None or raw == "":
        return 1
    if source == "GOES":
        try:
            m = int(raw)
        except (TypeError, ValueError):
            return 1
        if m in (10, 11, 30, 31, 13, 33):
            return 2
        if m in (14, 34, 12, 32):
            return 1
        return 0
    s = str(raw).strip().lower()
    if s in ("h", "high"):
        return 2
    if s in ("n", "nominal"):
        return 1
    if s in ("l", "low"):
        return 0
    try:
        n = float(s)
    except ValueError:
        return 1
    return 2 if n >= 80 else 1 if n >= 30 else 0


def haversine_km(a1: float, o1: float, a2: float, o2: float) -> float:
    r = math.radians
    d_phi, d_lam = r(a2 - a1), r(o2 - o1)
    h = math.sin(d_phi / 2) ** 2 + math.cos(r(a1)) * math.cos(r(a2)) * math.sin(d_lam / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def in_box(lat: float, lon: float, b: tuple[float, float, float, float]) -> bool:
    if lat < b[1] or lat > b[3]:
        return False
    if b[0] <= b[2]:
        return b[0] <= lon <= b[2]
    return lon >= b[0] or lon <= b[2]           # box straddles the antimeridian


def det_id(source: str, lat: float, lon: float, ts: float) -> str:
    return f"{source}|{lat:.4f}|{lon:.4f}|{int(ts)}"


# =====================================================================
# NASA FIRMS
# =====================================================================

FIRMS_SOURCES = [
    ("VIIRS_SNPP_NRT", "VIIRS", "Suomi-NPP"),
    ("VIIRS_NOAA20_NRT", "VIIRS", "NOAA-20"),
    ("VIIRS_NOAA21_NRT", "VIIRS", "NOAA-21"),
    ("MODIS_NRT", "MODIS", "Terra/Aqua"),
]


class FirmsClient:
    BASE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"

    def __init__(self, key: str, bbox, sources=None):
        self.key = key
        self.bbox = bbox
        self.sources = sources or [s[0] for s in FIRMS_SOURCES]
        self.last_error: str | None = None

    def _url(self, source: str, days: int) -> str:
        w, s, e, n = self.bbox
        world = w <= -179.9 and e >= 179.9 and s <= -89.9 and n >= 89.9
        area = "world" if world else f"{w},{s},{e},{n}"
        return f"{self.BASE}/{self.key}/{source}/{area}/{days}"

    def fetch(self, hours: int) -> list[dict]:
        days = max(1, min(5, math.ceil(hours / 24) + 1))
        out: list[dict] = []
        for src_id, family, platform in FIRMS_SOURCES:
            if src_id not in self.sources:
                continue
            try:
                raw = http_get(self._url(src_id, days), timeout=120).decode("utf-8", "replace")
            except urllib.error.HTTPError as ex:
                body = ""
                try:
                    body = ex.read().decode("utf-8", "replace")[:200]
                except Exception:
                    pass
                if ex.code in (401, 403):
                    self.last_error = f"FIRMS rejected the map key ({ex.code}). {body}"
                    log.error("%s", self.last_error)
                    return out
                log.warning("FIRMS %s: HTTP %s %s", src_id, ex.code, body)
                continue
            except Exception as ex:                                   # noqa: BLE001
                log.warning("FIRMS %s: %s", src_id, ex)
                continue

            head = raw[:120].lower()
            if "latitude" not in head:
                # FIRMS answers errors as plain text with a 200
                msg = raw.strip().splitlines()[0][:200] if raw.strip() else "empty response"
                if "invalid" in msg.lower() or "key" in msg.lower():
                    self.last_error = f"FIRMS: {msg}"
                    log.error("FIRMS %s: %s", src_id, msg)
                    return out
                log.warning("FIRMS %s: unexpected response: %s", src_id, msg)
                continue

            out.extend(self._parse(raw, family, platform))
        self.last_error = None
        return out

    @staticmethod
    def _parse(raw: str, family: str, platform: str) -> list[dict]:
        out: list[dict] = []
        for row in csv.DictReader(io.StringIO(raw)):
            try:
                lat, lon = float(row["latitude"]), float(row["longitude"])
                d = row["acq_date"].strip()
                t = str(row.get("acq_time", "0")).strip().zfill(4)
                ts = datetime.strptime(f"{d} {t[:2]}:{t[2:4]}", "%Y-%m-%d %H:%M") \
                    .replace(tzinfo=timezone.utc).timestamp()
            except (KeyError, ValueError, TypeError):
                continue

            def num(*keys):
                for k in keys:
                    v = row.get(k)
                    if v not in (None, "", "nan"):
                        try:
                            return float(v)
                        except ValueError:
                            pass
                return None

            out.append({
                "id": det_id(family, lat, lon, ts),
                "lat": lat, "lon": lon, "ts": ts,
                "source": family,
                "sat": (row.get("satellite") or platform).strip() or platform,
                "frp": num("frp"),
                "bt": num("bright_ti4", "brightness"),
                "conf": norm_conf(family, row.get("confidence")),
                "daynight": (row.get("daynight") or "").strip() or None,
                # per-detection ground footprint; it grows off-nadir, and the
                # dashboard draws the real pixel rather than a fixed dot
                "scan": num("scan"),
                "track": num("track"),
            })
        return out


# =====================================================================
# NOAA GOES ABI-L2-FDC (the fast channel)
# =====================================================================

# Mask codes that mean "there is a fire in this pixel".
# 10/30 processed, 11/31 saturated, 12/32 cloud-contaminated,
# 13/33 high probability, 14/34 medium, 15/35 low.
GOES_FIRE_MASK = {10, 11, 12, 13, 14, 15, 30, 31, 32, 33, 34, 35}
GOES_STRICT_MASK = {10, 11, 13, 30, 31, 33}


class GoesClient:
    """Reads the newest ABI fire product straight off the public S3 mirror.

    No AWS credentials are involved: the NOAA Open Data buckets answer plain
    anonymous HTTPS, so this is a directory listing plus one file download.
    """

    def __init__(self, buckets=("noaa-goes19", "noaa-goes18"), product="ABI-L2-FDCC",
                 strict=False, bbox=(-180, -90, 180, 90)):
        self.buckets = list(buckets)
        self.product = product
        self.strict = strict
        self.bbox = bbox
        self.available = False
        self.reason = ""
        self._seen: dict[str, str] = {}
        self._probe()

    def _probe(self) -> None:
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.reason = "numpy is not installed"
            return
        if not self._reader():
            self.reason = "no HDF5/netCDF reader (pip install h5netcdf, or netCDF4)"
            return
        self.available = True

    @staticmethod
    def _reader():
        for mod in ("h5netcdf", "netCDF4", "h5py"):
            try:
                __import__(mod)
                return mod
            except ImportError:
                continue
        return None

    # ---- S3 listing -------------------------------------------------
    def _latest_key(self, bucket: str) -> str | None:
        now = datetime.now(timezone.utc)
        for back in range(0, 4):                       # walk back up to 4 hours
            t = now - timedelta(hours=back)
            prefix = f"{self.product}/{t.year}/{t.timetuple().tm_yday:03d}/{t.hour:02d}/"
            url = (f"https://{bucket}.s3.amazonaws.com/?list-type=2"
                   f"&prefix={urllib.parse.quote(prefix)}&max-keys=400")
            try:
                xml = http_get(url, timeout=45)
            except Exception as ex:                    # noqa: BLE001
                log.debug("GOES list %s: %s", bucket, ex)
                continue
            try:
                root = ET.fromstring(xml)
            except ET.ParseError:
                continue
            ns = "{http://s3.amazonaws.com/doc/2006-03-01/}"
            keys = [c.findtext(f"{ns}Key") for c in root.findall(f"{ns}Contents")]
            keys = sorted(k for k in keys if k and k.endswith(".nc"))
            if keys:
                return keys[-1]
        return None

    def _read(self, blob: bytes, sat_label: str) -> list[dict]:
        import numpy as np
        reader = self._reader()
        fh = io.BytesIO(blob)

        if reader == "h5netcdf":
            import h5netcdf
            ds = h5netcdf.File(fh, "r")
            get = lambda n: np.asarray(ds[n][:])                            # noqa: E731
            attr = lambda v, a: ds[v].attrs.get(a)                          # noqa: E731
            gattr = lambda v, a: ds[v].attrs.get(a)                         # noqa: E731
            close = ds.close
        elif reader == "netCDF4":
            import netCDF4
            ds = netCDF4.Dataset("inmem", mode="r", memory=blob)
            get = lambda n: np.asarray(ds.variables[n][:])                  # noqa: E731
            attr = lambda v, a: getattr(ds.variables[v], a, None)           # noqa: E731
            gattr = attr
            close = ds.close
        else:
            import h5py
            ds = h5py.File(fh, "r")
            get = lambda n: np.asarray(ds[n][:])                            # noqa: E731
            attr = lambda v, a: ds[v].attrs.get(a)                          # noqa: E731
            gattr = attr
            close = ds.close

        try:
            mask = get("Mask").astype("float64")
            x = get("x").astype("float64")
            y = get("y").astype("float64")
            # scaled ints in the file; apply the CF scale/offset if present
            for var, arr in (("x", x), ("y", y)):
                sf, off = attr(var, "scale_factor"), attr(var, "add_offset")
                if sf is not None:
                    arr *= float(np.ravel(sf)[0])
                if off is not None:
                    arr += float(np.ravel(off)[0])

            proj = "goes_imager_projection"
            lon_origin = float(np.ravel(gattr(proj, "longitude_of_projection_origin"))[0])
            pph = float(np.ravel(gattr(proj, "perspective_point_height"))[0])
            r_eq = float(np.ravel(gattr(proj, "semi_major_axis"))[0])
            r_pol = float(np.ravel(gattr(proj, "semi_minor_axis"))[0])

            try:
                t0 = float(np.ravel(get("t"))[0])
                ts = datetime(2000, 1, 1, 12, tzinfo=timezone.utc).timestamp() + t0
            except Exception:                                                # noqa: BLE001
                ts = utcnow()

            wanted = GOES_STRICT_MASK if self.strict else GOES_FIRE_MASK
            hit = np.isin(mask, list(wanted))
            idx = np.argwhere(hit)
            if idx.size == 0:
                return []
            if idx.shape[0] > 20000:                       # sanity cap
                idx = idx[:20000]

            power = temp = None
            for name, ref in (("Power", "power"), ("Temp", "temp")):
                try:
                    v = get(name).astype("float64")
                    sf, off = attr(name, "scale_factor"), attr(name, "add_offset")
                    if sf is not None:
                        v = v * float(np.ravel(sf)[0])
                    if off is not None:
                        v = v + float(np.ravel(off)[0])
                    fill = attr(name, "_FillValue")
                    if fill is not None:
                        v = np.where(np.isclose(v, float(np.ravel(fill)[0])), np.nan, v)
                    if ref == "power":
                        power = v
                    else:
                        temp = v
                except Exception:                                            # noqa: BLE001
                    pass

            rows, cols = idx[:, 0], idx[:, 1]
            # Element-wise, never meshed: a mesh of 20 000 scan angles would be
            # a 400-million-element array for a few thousand fire pixels.
            lat_v, lon_v = self._pairs(x[cols], y[rows], lon_origin, pph + r_eq, r_eq, r_pol)

            out: list[dict] = []
            for k in range(len(rows)):
                la, lo = float(lat_v[k]), float(lon_v[k])
                if not (math.isfinite(la) and math.isfinite(lo)):
                    continue
                if not in_box(la, lo, self.bbox):
                    continue
                p = None if power is None else power[rows[k], cols[k]]
                tv = None if temp is None else temp[rows[k], cols[k]]
                out.append({
                    "id": det_id("GOES", la, lo, ts),
                    "lat": la, "lon": lo, "ts": ts,
                    "source": "GOES", "sat": sat_label,
                    "frp": None if p is None or not math.isfinite(p) else round(float(p), 1),
                    "bt": None if tv is None or not math.isfinite(tv) else round(float(tv), 1),
                    "conf": norm_conf("GOES", int(mask[rows[k], cols[k]])),
                    "daynight": None,
                    "scan": 2.0, "track": 2.0,          # ABI fixed grid at 2 km
                })
            return out
        finally:
            try:
                close()
            except Exception:                                                # noqa: BLE001
                pass

    @staticmethod
    def _pairs(xs, ys, lon_origin, H, r_eq, r_pol):
        """Element-wise (not meshed) scan-angle -> lat/lon for paired arrays."""
        import numpy as np
        lam_sq = (r_eq * r_eq) / (r_pol * r_pol)
        a = np.sin(xs) ** 2 + np.cos(xs) ** 2 * (np.cos(ys) ** 2 + lam_sq * np.sin(ys) ** 2)
        b = -2.0 * H * np.cos(xs) * np.cos(ys)
        c = H * H - r_eq * r_eq
        disc = b * b - 4.0 * a * c
        with np.errstate(invalid="ignore", divide="ignore"):
            r_s = (-b - np.sqrt(disc)) / (2.0 * a)
            s_x = r_s * np.cos(xs) * np.cos(ys)
            s_y = -r_s * np.sin(xs)
            s_z = r_s * np.cos(xs) * np.sin(ys)
            lat = np.degrees(np.arctan(lam_sq * s_z / np.sqrt((H - s_x) ** 2 + s_y ** 2)))
            lon = lon_origin - np.degrees(np.arctan2(s_y, H - s_x))
        lat = np.where(disc < 0, np.nan, lat)
        lon = np.where(disc < 0, np.nan, lon)
        return lat, lon

    def fetch(self) -> list[dict]:
        if not self.available:
            return []
        out: list[dict] = []
        for bucket in self.buckets:
            try:
                key = self._latest_key(bucket)
                if not key:
                    continue
                if self._seen.get(bucket) == key:
                    continue                              # same granule as last cycle
                blob = http_get(f"https://{bucket}.s3.amazonaws.com/{key}", timeout=180)
                label = f"GOES-{bucket[-2:]}"
                rows = self._read(blob, label)
                self._seen[bucket] = key
                log.info("GOES %s %s -> %d fire pixels", bucket, key.rsplit("/", 1)[-1], len(rows))
                out.extend(rows)
            except Exception as ex:                                           # noqa: BLE001
                log.warning("GOES %s: %s", bucket, ex)
        return out


# =====================================================================
# storage
# =====================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS detections (
  id TEXT PRIMARY KEY, lat REAL, lon REAL, ts REAL, source TEXT, sat TEXT,
  frp REAL, bt REAL, conf INTEGER, daynight TEXT, first_seen REAL,
  scan REAL, track REAL
);
CREATE INDEX IF NOT EXISTS det_ts ON detections(ts);
CREATE INDEX IF NOT EXISTS det_seen ON detections(first_seen);

CREATE TABLE IF NOT EXISTS incident_history (
  inc_id TEXT, t REAL, frp REAL, n INTEGER,
  PRIMARY KEY (inc_id, t)
);

CREATE TABLE IF NOT EXISTS events (
  t REAL, inc_id TEXT, sev TEXT, title TEXT, meta TEXT, lat REAL, lon REAL
);
CREATE INDEX IF NOT EXISTS ev_t ON events(t);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self._local = threading.local()
        with self.conn() as c:
            c.executescript(SCHEMA)

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            self._local.c = c
        return c

    def upsert(self, dets: Iterable[dict]) -> list[dict]:
        """Insert, returning only the rows that were genuinely new."""
        now = utcnow()
        fresh: list[dict] = []
        with self.lock:
            c = self.conn()
            for d in dets:
                cur = c.execute(
                    "INSERT OR IGNORE INTO detections"
                    " (id,lat,lon,ts,source,sat,frp,bt,conf,daynight,first_seen,scan,track)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (d["id"], d["lat"], d["lon"], d["ts"], d["source"], d.get("sat"),
                     d.get("frp"), d.get("bt"), d.get("conf", 1), d.get("daynight"), now,
                     d.get("scan"), d.get("track")))
                if cur.rowcount:
                    fresh.append(d)
            c.commit()
        return fresh

    def recent(self, hours: float, bbox) -> list[dict]:
        cut = utcnow() - hours * 3600
        c = self.conn()
        rows = c.execute(
            "SELECT * FROM detections WHERE ts >= ? ORDER BY ts DESC LIMIT 60000", (cut,)
        ).fetchall()
        out = []
        for r in rows:
            if not in_box(r["lat"], r["lon"], bbox):
                continue
            out.append({
                "id": r["id"], "lat": r["lat"], "lon": r["lon"], "ts": r["ts"] * 1000,
                "source": r["source"], "sat": r["sat"], "frp": r["frp"], "bt": r["bt"],
                "conf": r["conf"], "daynight": r["daynight"],
                "scan": r["scan"], "track": r["track"],
                "first_seen": r["first_seen"] * 1000,
            })
        return out

    def push_history(self, inc_id: str, t: float, frp: float, n: int) -> None:
        with self.lock:
            c = self.conn()
            c.execute("INSERT OR REPLACE INTO incident_history VALUES (?,?,?,?)",
                      (inc_id, round(t), frp, n))
            c.commit()

    def history(self, inc_id: str, limit: int = 60) -> list[dict]:
        rows = self.conn().execute(
            "SELECT t,frp,n FROM incident_history WHERE inc_id=? ORDER BY t DESC LIMIT ?",
            (inc_id, limit)).fetchall()
        return [{"t": r["t"] * 1000, "v": r["frp"], "n": r["n"]} for r in reversed(rows)]

    def add_event(self, ev: dict) -> None:
        with self.lock:
            c = self.conn()
            c.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?)",
                      (ev["t"], ev["inc_id"], ev["sev"], ev["title"], ev["meta"],
                       ev["lat"], ev["lon"]))
            c.commit()

    def events(self, limit: int = 200) -> list[dict]:
        rows = self.conn().execute(
            "SELECT * FROM events ORDER BY t DESC LIMIT ?", (limit,)).fetchall()
        return [{"t": r["t"] * 1000, "incId": r["inc_id"], "sev": r["sev"],
                 "title": r["title"], "meta": r["meta"], "lat": r["lat"], "lon": r["lon"]}
                for r in rows]

    def prune(self, keep_days: float) -> None:
        cut = utcnow() - keep_days * 86400
        with self.lock:
            c = self.conn()
            c.execute("DELETE FROM detections WHERE ts < ?", (cut,))
            c.execute("DELETE FROM incident_history WHERE t < ?", (cut,))
            c.execute("DELETE FROM events WHERE t < ?", (cut,))
            c.commit()


# =====================================================================
# clustering & severity
# =====================================================================

CELL_DEG = 0.05
LINK_KM = 4.5


def cluster(dets: list[dict]) -> list[dict]:
    """Single-linkage clustering over a coarse spatial hash.

    Three sensors at three resolutions see the same fire, so grouping has to
    happen in geographic space rather than on any one sensor's pixel grid.
    """
    n = len(dets)
    if n == 0:
        return []
    cells: dict[tuple[int, int], list[int]] = {}
    for i, d in enumerate(dets):
        cells.setdefault((int(d["lat"] // CELL_DEG), int(d["lon"] // CELL_DEG)), []).append(i)

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for (ci, cj), idxs in cells.items():
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                nb = cells.get((ci + di, cj + dj))
                if not nb:
                    continue
                for a in idxs:
                    for b in nb:
                        if a >= b:
                            continue
                        if haversine_km(dets[a]["lat"], dets[a]["lon"],
                                        dets[b]["lat"], dets[b]["lon"]) <= LINK_KM:
                            union(a, b)

    groups: dict[int, list[dict]] = {}
    for i, d in enumerate(dets):
        groups.setdefault(find(i), []).append(d)

    out = []
    for g in groups.values():
        wsum = lat = lon = 0.0
        frp = maxfrp = 0.0
        first, last = math.inf, 0.0
        conf = 0
        srcs = set()
        for d in g:
            w = max(0.5, d.get("frp") or 1.0)
            lat += d["lat"] * w
            lon += d["lon"] * w
            wsum += w
            frp += d.get("frp") or 0.0
            maxfrp = max(maxfrp, d.get("frp") or 0.0)
            first = min(first, d["ts"])
            last = max(last, d["ts"])
            conf = max(conf, d.get("conf", 1))
            srcs.add(d["source"])
        lat /= wsum
        lon /= wsum
        out.append({
            "id": f"I{lat:.2f}_{lon:.2f}", "lat": lat, "lon": lon,
            "count": len(g), "frp": round(frp, 1), "maxFrp": round(maxfrp, 1),
            "first": first, "last": last, "conf": conf, "sources": sorted(srcs),
        })
    return out


def severity(inc: dict, now: float) -> str:
    fresh = (now - inc["last"]) < 3 * 3600
    if fresh and (inc["frp"] >= 400 or inc["count"] >= 30 or inc["maxFrp"] >= 250):
        return "critical"
    if inc["frp"] >= 80 or inc["count"] >= 8 or inc["maxFrp"] >= 90:
        return "serious"
    if inc["frp"] >= 15 or inc["count"] >= 3:
        return "warning"
    return "good"


SEV_RANK = {"critical": 3, "serious": 2, "warning": 1, "good": 0}


# =====================================================================
# the monitor loop
# =====================================================================

class Monitor:
    def __init__(self, args):
        self.args = args
        self.bbox = tuple(args.bbox)
        self.store = Store(args.db)
        self.firms = FirmsClient(args.firms_key, self.bbox) if args.firms_key else None
        self.goes = None
        if args.goes:
            self.goes = GoesClient(buckets=args.goes_buckets, product=args.goes_product,
                                   strict=args.goes_strict, bbox=self.bbox)
            if not self.goes.available:
                log.warning("GOES rapid scan disabled: %s", self.goes.reason)
        self.effis = None
        if args.effis and EffisClient is not None:
            self.effis = EffisClient(self.bbox, enabled=True)
        elif args.effis:
            log.warning("EFFIS requested but firewatch_effis.py is not next to the server")
        self.perimeters: list[dict] = []
        self.perim_at = 0.0
        self.source_status: dict[str, dict] = {}
        self.last_poll = 0.0
        self.last_new = 0
        self.status = "starting"
        self.note = ""
        self.stop = threading.Event()

    # -- one cycle ----------------------------------------------------
    def cycle(self) -> None:
        t0 = time.time()
        dets: list[dict] = []
        parts = []

        if self.goes and self.goes.available:
            g = self.goes.fetch()
            dets += g
            parts.append(f"GOES {len(g)}")
            self.note_source("GOES", len(g), None)

        if self.firms:
            f = self.firms.fetch(self.args.hours)
            dets += f
            parts.append(f"FIRMS {len(f)}")
            self.note_source("FIRMS", len(f), self.firms.last_error)

        if self.effis:
            try:
                e = self.effis.hotspots(self.args.hours)
                dets += e
                parts.append(f"EFFIS {len(e)}")
                self.note_source("EFFIS", len(e), self.effis.last_error)
            except Exception as ex:                                       # noqa: BLE001
                log.warning("EFFIS hot spots: %s", ex)
                self.note_source("EFFIS", 0, str(ex))
            # Perimeters change on a daily rhythm, not a five-minute one.
            if time.time() - self.perim_at > 3600:
                try:
                    self.perimeters = self.effis.perimeters()
                    self.perim_at = time.time()
                    log.info("EFFIS burnt-area perimeters: %d", len(self.perimeters))
                except Exception as ex:                                   # noqa: BLE001
                    log.warning("EFFIS perimeters: %s", ex)

        fresh = self.store.upsert(dets)
        self.last_poll = utcnow()
        self.last_new = len(fresh)

        recent = self.store.recent(self.args.hours, self.bbox)
        incs = cluster([{**d, "ts": d["ts"] / 1000} for d in recent])
        now = utcnow()
        by_inc_new: dict[str, int] = {}
        for d in fresh:
            for inc in incs:
                if haversine_km(d["lat"], d["lon"], inc["lat"], inc["lon"]) <= 8:
                    by_inc_new[inc["id"]] = by_inc_new.get(inc["id"], 0) + 1
                    break

        for inc in incs:
            self.store.push_history(inc["id"], now, inc["frp"], inc["count"])
            inc["sev"] = severity(inc, now)
            inc["nNew"] = by_inc_new.get(inc["id"], 0)

        self.raise_alerts(incs)
        self.status = "ok"
        self.note = " · ".join(parts) if parts else "no source configured"
        if self.firms and self.firms.last_error:
            self.status = "error"
            self.note = self.firms.last_error
        log.info("cycle %.1fs — %s — %d new of %d stored (%d clusters)",
                 time.time() - t0, self.note, len(fresh), len(recent), len(incs))

    def note_source(self, name: str, n: int, err: str | None) -> None:
        """Per-source freshness. A dashboard that shows one global 'updated'
        time hides the case that matters: one feed stalled while the others
        kept going."""
        st = self.source_status.setdefault(name, {})
        st["name"] = name
        st["lastTry"] = utcnow() * 1000
        st["error"] = err
        if not err:
            st["lastOk"] = utcnow() * 1000
            st["count"] = n
        st["ok"] = not err

    # -- alerts -------------------------------------------------------
    def raise_alerts(self, incs: list[dict]) -> None:
        now = utcnow()
        known = {r["inc_id"] for r in
                 self.store.conn().execute("SELECT DISTINCT inc_id FROM events").fetchall()}
        raised = []
        for inc in incs:
            if not inc["nNew"]:
                continue
            if inc["conf"] < self.args.min_conf:
                continue
            if inc["frp"] < self.args.min_frp:
                continue
            if SEV_RANK[inc["sev"]] < SEV_RANK[self.args.min_sev]:
                continue
            hist = self.store.history(inc["id"], 6)
            growth = 0.0
            if len(hist) > 1 and hist[0]["v"]:
                growth = (inc["frp"] - hist[0]["v"]) / hist[0]["v"]
            brand_new = inc["id"] not in known
            ev = {
                "t": now, "inc_id": inc["id"], "sev": inc["sev"],
                "title": ("New thermal anomaly detected" if brand_new
                          else "Fire activity intensifying" if growth > 0.25
                          else "New hot pixels on active fire"),
                "meta": (f"{inc['lat']:.3f}, {inc['lon']:.3f} · {inc['nNew']} new px · "
                         f"{inc['frp']:.0f} MW · {'+'.join(inc['sources'])}"),
                "lat": inc["lat"], "lon": inc["lon"],
            }
            self.store.add_event(ev)
            raised.append(ev)

        if not raised:
            return
        raised.sort(key=lambda e: -SEV_RANK[e["sev"]])
        for ev in raised[:20]:
            log.warning("ALERT [%s] %s — %s", ev["sev"].upper(), ev["title"], ev["meta"])
        if self.args.webhook:
            self.post_webhook(raised)

    def post_webhook(self, evs: list[dict]) -> None:
        top = evs[0]
        text = (f"*FireWatch — {top['sev'].upper()}*\n{top['title']}\n{top['meta']}"
                + (f"\n_and {len(evs) - 1} more_" if len(evs) > 1 else ""))
        body = json.dumps({"text": text, "events": evs[:20]}).encode()
        try:
            req = urllib.request.Request(self.args.webhook, data=body,
                                         headers={**UA, "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=20).read()
        except Exception as ex:                                               # noqa: BLE001
            log.warning("webhook failed: %s", ex)

    # -- thread -------------------------------------------------------
    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.cycle()
                self.store.prune(self.args.keep_days)
            except Exception as ex:                                           # noqa: BLE001
                self.status = "error"
                self.note = str(ex)
                log.exception("cycle failed: %s", ex)
            self.stop.wait(self.args.interval)


# =====================================================================
# HTTP
# =====================================================================

class Handler(BaseHTTPRequestHandler):
    monitor: Monitor = None                                   # type: ignore[assignment]
    server_version = f"FireWatch/{VERSION}"

    def log_message(self, fmt, *a):                            # quieter access log
        log.debug("%s - %s", self.address_string(), fmt % a)

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json; charset=utf-8")

    def do_HEAD(self):                                          # noqa: N802
        self.do_GET()

    def do_GET(self):                                           # noqa: N802
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        m = self.monitor
        path = u.path.rstrip("/") or "/"

        try:
            if path == "/" or path == "/index.html":
                f = HERE / "firewatch.html"
                if not f.exists():
                    self._send(404, b"firewatch.html is not next to firewatch_server.py",
                               "text/plain; charset=utf-8")
                    return
                self._send(200, f.read_bytes(), "text/html; charset=utf-8")
                return

            if path == "/api/health":
                self._json({
                    "ok": True, "version": VERSION, "status": m.status, "note": m.note,
                    "lastPoll": m.last_poll * 1000, "interval": m.args.interval,
                    "firms": bool(m.firms),
                    "goes": bool(m.goes and m.goes.available),
                    "effis": bool(m.effis),
                    "perimeters": len(m.perimeters),
                    "sources": list(m.source_status.values()),
                    "goesReason": (m.goes.reason if m.goes else "not enabled"),
                    "bbox": list(m.bbox),
                })
                return

            if path == "/api/detections":
                hours = float(q.get("hours", [m.args.hours])[0])
                bbox = m.bbox
                if "bbox" in q:
                    try:
                        parts = [float(v) for v in q["bbox"][0].split(",")]
                        if len(parts) == 4:
                            bbox = tuple(parts)
                    except ValueError:
                        pass
                dets = m.store.recent(min(hours, 24 * 7), bbox)
                srcs = {}
                if m.firms:
                    srcs["FIRMS"] = "NASA FIRMS NRT"
                if m.goes and m.goes.available:
                    srcs["GOES"] = f"NOAA {m.args.goes_product} via AWS Open Data"
                self._json({"detections": dets, "sources": srcs, "note": m.note,
                            "serverTime": utcnow() * 1000})
                return

            if path == "/api/incidents":
                hours = float(q.get("hours", [m.args.hours])[0])
                recent = m.store.recent(hours, m.bbox)
                incs = cluster([{**d, "ts": d["ts"] / 1000} for d in recent])
                now = utcnow()
                for inc in incs:
                    inc["sev"] = severity(inc, now)
                    inc["hist"] = m.store.history(inc["id"])
                    inc["first"] *= 1000
                    inc["last"] *= 1000
                incs.sort(key=lambda i: (-SEV_RANK[i["sev"]], -i["frp"]))
                self._json({"incidents": incs[:300]})
                return

            if path == "/api/perimeters":
                self._json({"perimeters": m.perimeters, "updated": m.perim_at * 1000})
                return

            if path == "/api/fwi":
                try:
                    lat = float(q.get("lat", ["0"])[0])
                    lon = float(q.get("lon", ["0"])[0])
                except ValueError:
                    self._json({"error": "bad lat/lon"}, 400)
                    return
                self._json({"fwi": m.effis.fwi(lat, lon) if m.effis else None})
                return

            if path == "/api/sources":
                self._json({"sources": list(m.source_status.values())})
                return

            if path == "/api/events":
                self._json({"events": m.store.events(int(q.get("limit", [200])[0]))})
                return

            self._send(404, b"not found", "text/plain; charset=utf-8")
        except BrokenPipeError:
            pass
        except Exception as ex:                                               # noqa: BLE001
            log.exception("request failed")
            try:
                self._json({"error": str(ex)}, 500)
            except Exception:                                                 # noqa: BLE001
                pass


# =====================================================================
# self-test — verifies parsing, clustering, storage and alerting offline
# =====================================================================

def selftest() -> int:
    import random
    import tempfile

    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        ok = ok and bool(cond)

    print("FIRMS CSV parsing")
    csv_txt = (
        "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,"
        "instrument,confidence,version,bright_ti5,frp,daynight\n"
        "38.1234,23.7654,335.2,0.42,0.39,2026-08-14,1312,N,VIIRS,n,2.0NRT,295.1,12.4,D\n"
        "38.1300,23.7700,367.9,0.42,0.39,2026-08-14,1312,N,VIIRS,h,2.0NRT,301.0,88.0,D\n"
        "-9.5000,-60.5000,330.0,0.5,0.5,2026-08-14,0512,1,VIIRS,l,2.0NRT,290.0,3.1,N\n")
    rows = FirmsClient._parse(csv_txt, "VIIRS", "NOAA-20")
    check("three rows parsed", len(rows) == 3)
    check("timestamp is UTC 13:12", datetime.fromtimestamp(rows[0]["ts"], timezone.utc)
          .strftime("%Y-%m-%d %H:%M") == "2026-08-14 13:12")
    check("confidence n->1, h->2, l->0",
          [r["conf"] for r in rows] == [1, 2, 0])
    check("frp carried through", rows[1]["frp"] == 88.0)

    print("confidence normalisation")
    check("MODIS 85 -> high", norm_conf("MODIS", "85") == 2)
    check("MODIS 40 -> nominal", norm_conf("MODIS", 40) == 1)
    check("MODIS 10 -> low", norm_conf("MODIS", 10) == 0)
    check("GOES mask 10 -> high", norm_conf("GOES", 10) == 2)
    check("GOES mask 15 -> low", norm_conf("GOES", 15) == 0)

    print("geometry")
    check("haversine Athens->Thessaloniki ~ 300 km",
          290 < haversine_km(37.98, 23.73, 40.64, 22.94) < 310)
    check("antimeridian box contains 179E", in_box(0, 179, (170, -10, -170, 10)))
    check("antimeridian box excludes 0E", not in_box(0, 0, (170, -10, -170, 10)))

    print("clustering")
    random.seed(7)
    dets = []
    for cx, cy, k in ((38.10, 23.76, 12), (38.90, 24.90, 7), (-9.50, -60.50, 20)):
        for i in range(k):
            la = cx + random.uniform(-0.01, 0.01)
            lo = cy + random.uniform(-0.01, 0.01)
            dets.append({"id": det_id("VIIRS", la, lo, 1e9 + i), "lat": la, "lon": lo,
                         "ts": 1.0e9 + i, "source": "VIIRS", "frp": 20.0, "conf": 2})
    incs = cluster(dets)
    check("three clusters found", len(incs) == 3)
    check("counts preserved", sorted(i["count"] for i in incs) == [7, 12, 20])
    check("centroid near seed",
          any(abs(i["lat"] - 38.10) < 0.05 and abs(i["lon"] - 23.76) < 0.05 for i in incs))

    print("severity")
    base = {"count": 1, "maxFrp": 1, "last": utcnow()}
    check("big cluster -> critical", severity({**base, "frp": 900, "count": 40}, utcnow()) == "critical")
    check("mid cluster -> serious", severity({**base, "frp": 120}, utcnow()) == "serious")
    check("small -> warning", severity({**base, "frp": 20}, utcnow()) == "warning")
    check("tiny -> good", severity({**base, "frp": 1}, utcnow()) == "good")

    print("store + change detection")
    with tempfile.TemporaryDirectory() as td:
        st = Store(os.path.join(td, "t.db"))
        batch = [{"id": d["id"], "lat": d["lat"], "lon": d["lon"], "ts": utcnow() - 600,
                  "source": "VIIRS", "sat": "NOAA-20", "frp": 20.0, "bt": 340.0,
                  "conf": 2, "daynight": "D"} for d in dets]
        first = st.upsert(batch)
        second = st.upsert(batch)
        check("first insert is all new", len(first) == len(batch))
        check("re-insert yields nothing new", len(second) == 0)
        extra = dict(batch[0], id="VIIRS|99.0000|99.0000|1")
        extra.update(lat=41.0, lon=25.0)
        check("one genuinely new row detected", len(st.upsert([extra])) == 1)
        got = st.recent(2, (-180, -90, 180, 90))
        check("recent() returns stored rows", len(got) == len(batch) + 1)
        check("recent() filters by bbox", len(st.recent(2, (23.0, 37.0, 24.5, 38.5))) == 12)
        st.push_history("Itest", utcnow() - 60, 100.0, 5)
        st.push_history("Itest", utcnow(), 180.0, 9)
        h = st.history("Itest")
        check("history ordered oldest-first", len(h) == 2 and h[0]["v"] == 100.0)
        st.add_event({"t": utcnow(), "inc_id": "Itest", "sev": "serious",
                      "title": "t", "meta": "m", "lat": 1.0, "lon": 2.0})
        check("event round-trips", len(st.events()) == 1)

    print("GOES geolocation")
    try:
        import numpy as np
        # A pixel on the sub-satellite point must map to (0, lon_origin).
        lat, lon = GoesClient._pairs(np.array([0.0]), np.array([0.0]),
                                     -75.0, 42164160.0, 6378137.0, 6356752.31414)
        check("nadir maps to 0N", abs(float(lat[0])) < 1e-6)
        check("nadir maps to sub-satellite longitude", abs(float(lon[0]) + 75.0) < 1e-6)
        # A known off-nadir angle must land in the western hemisphere at sane latitude.
        lat2, lon2 = GoesClient._pairs(np.array([0.03]), np.array([0.06]),
                                       -75.0, 42164160.0, 6378137.0, 6356752.31414)
        check("off-nadir latitude plausible", 20 < float(lat2[0]) < 45)
        check("off-nadir longitude plausible", -110 < float(lon2[0]) < -60)
    except ImportError:
        print("  SKIP  numpy not installed")

    print()
    print("SELF-TEST", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


# =====================================================================
# entry point
# =====================================================================

def parse_bbox(s: str):
    parts = [float(v) for v in s.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox must be west,south,east,north")
    return parts


def main() -> int:
    p = argparse.ArgumentParser(
        description="FireWatch — real-time satellite fire and ground-temperature monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  # dashboard on http://localhost:8000, global, FIRMS only
  python3 firewatch_server.py --firms-key YOUR_KEY

  # California, GOES rapid scan on, alert a Slack incoming webhook
  python3 firewatch_server.py --firms-key YOUR_KEY \\
      --bbox -125,32,-110,43 --goes --webhook https://hooks.slack.com/services/...

  # verify everything offline, no network and no key needed
  python3 firewatch_server.py --selftest
""")
    p.add_argument("--firms-key", default=os.environ.get("FIRMS_MAP_KEY", ""),
                   help="NASA FIRMS map key (or set FIRMS_MAP_KEY)")
    p.add_argument("--bbox", type=parse_bbox, default=[13.4, 42.3, 19.5, 46.6],
                   help="west,south,east,north (default: Croatia). Use -180,-90,180,90 for the world")
    p.add_argument("--hours", type=float, default=24, help="rolling window to serve (default 24)")
    p.add_argument("--interval", type=int, default=300, help="seconds between polls (default 300)")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--db", default=str(HERE / "firewatch.db"))
    p.add_argument("--keep-days", type=float, default=14, help="history retention (default 14)")

    p.add_argument("--goes", action="store_true", help="enable NOAA GOES rapid scan (5-10 min)")
    p.add_argument("--goes-product", default="ABI-L2-FDCC",
                   help="ABI-L2-FDCC (CONUS, 5 min) or ABI-L2-FDCF (full disk, 10 min)")
    p.add_argument("--goes-buckets", nargs="+", default=["noaa-goes19", "noaa-goes18"],
                   help="AWS Open Data buckets, east first")
    p.add_argument("--goes-strict", action="store_true",
                   help="only 'processed' and 'saturated' fire pixels — fewest false alarms")

    p.add_argument("--effis", action="store_true",
                   help="add EFFIS/GWIS: European hot spots, burnt-area perimeters, fire danger")
    p.add_argument("--effis-probe", action="store_true",
                   help="list the layers EFFIS currently advertises, then exit")

    p.add_argument("--webhook", default="", help="POST alerts as JSON (Slack-compatible)")
    p.add_argument("--min-frp", type=float, default=0.0, help="alert threshold, megawatts")
    p.add_argument("--min-conf", type=int, default=1, choices=[0, 1, 2],
                   help="alert threshold: 0 any, 1 nominal+, 2 high only")
    p.add_argument("--min-sev", default="warning",
                   choices=["good", "warning", "serious", "critical"])

    p.add_argument("--once", action="store_true", help="run one poll, print a summary, exit")
    p.add_argument("--selftest", action="store_true", help="offline checks, no network needed")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    if args.selftest:
        return selftest()

    if args.effis_probe:
        if EffisClient is None:
            print("firewatch_effis.py is not next to the server")
            return 1
        EffisClient(args.bbox).probe()
        return 0

    if not args.firms_key and not args.goes and not args.effis:
        p.error("nothing to poll: pass --firms-key, --goes or --effis "
                "(get a free key at https://firms.modaps.eosdis.nasa.gov/api/map_key/)")

    mon = Monitor(args)
    Handler.monitor = mon

    if args.once:
        mon.cycle()
        recent = mon.store.recent(args.hours, mon.bbox)
        incs = cluster([{**d, "ts": d["ts"] / 1000} for d in recent])
        now = utcnow()
        for i in incs:
            i["sev"] = severity(i, now)
        incs.sort(key=lambda i: (-SEV_RANK[i["sev"]], -i["frp"]))
        print(f"\n{len(recent)} detections in the last {args.hours:g} h · "
              f"{len(incs)} clusters · {mon.last_new} new this poll\n")
        for i in incs[:15]:
            print(f"  {i['sev'].upper():9s} {i['lat']:8.3f},{i['lon']:9.3f}  "
                  f"{i['frp']:8.1f} MW  {i['count']:4d} px  {'+'.join(i['sources'])}")
        return 0

    t = threading.Thread(target=mon.run, daemon=True)
    t.start()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    log.info("FireWatch %s — dashboard at http://%s:%d", VERSION, args.host, args.port)
    log.info("watching bbox %s · every %ds · FIRMS %s · GOES %s · EFFIS %s",
             args.bbox, args.interval,
             "on" if args.firms_key else "off",
             "on" if (mon.goes and mon.goes.available) else "off",
             "on" if mon.effis else "off")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
        mon.stop.set()
        srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
