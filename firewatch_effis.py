"""EFFIS / GWIS client — European Forest Fire Information System.

EFFIS is the Copernicus Emergency Management Service's fire system. For a
European user it adds three things FIRMS and GOES cannot:

  * hot spots re-processed for Europe, with the European agencies' own
    confidence handling;
  * **burnt area perimeters** — the actual shape of the burn, not a scatter of
    hot pixels. This is a different kind of fact: hot spots tell you a fire is
    burning, a perimeter tells you what it has already taken;
  * the Fire Weather Index, the number European fire services actually plan
    around.

Layer names are discovered from GetCapabilities at runtime rather than
hard-coded. EFFIS has renamed its layers more than once (modis.hs, modis_hs,
ms:modis.hs have all been correct at some point), and a monitoring tool that
silently returns nothing because a name changed is worse than one that says so.
Every method reports what it found, and `probe()` prints the whole list.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger("firewatch.effis")

BASE = "https://maps.effis.emergency.copernicus.eu"
UA = {"User-Agent": "FireWatch/1.1 (+wildfire monitoring)"}

# Candidate service endpoints, tried in order.
ENDPOINTS = [f"{BASE}/effis", f"{BASE}/gwis"]


def _get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class EffisClient:
    def __init__(self, bbox, enabled: bool = True):
        self.bbox = bbox
        self.enabled = enabled
        self.endpoint: str | None = None
        self.types: list[str] = []
        self.hs_types: list[str] = []
        self.ba_types: list[str] = []
        self.last_error: str | None = None
        self.discovered = False

    # ---- discovery ---------------------------------------------------
    def discover(self) -> bool:
        """Read WFS GetCapabilities and classify the feature types."""
        if self.discovered:
            return bool(self.hs_types or self.ba_types)
        for ep in ENDPOINTS:
            url = f"{ep}?service=WFS&version=1.1.0&request=GetCapabilities"
            try:
                xml = _get(url, timeout=45)
            except Exception as ex:                                     # noqa: BLE001
                log.debug("EFFIS capabilities %s: %s", ep, ex)
                continue
            try:
                root = ET.fromstring(xml)
            except ET.ParseError as ex:
                log.debug("EFFIS capabilities %s: bad XML (%s)", ep, ex)
                continue
            names: list[str] = []
            for el in root.iter():
                if _strip_ns(el.tag) == "FeatureType":
                    for ch in el:
                        if _strip_ns(ch.tag) == "Name" and ch.text:
                            names.append(ch.text.strip())
            if not names:
                continue
            self.endpoint = ep
            self.types = names
            # hot spots end in .hs / _hs; burnt areas carry ba and a polygon suffix
            self.hs_types = [n for n in names if re.search(r"[._]hs$|[._]hs[._]", n)]
            self.ba_types = [n for n in names if re.search(r"[._]ba", n)]
            self.discovered = True
            log.info("EFFIS %s: %d feature types (%d hot spot, %d burnt area)",
                     ep, len(names), len(self.hs_types), len(self.ba_types))
            return bool(self.hs_types or self.ba_types)
        self.discovered = True
        self.last_error = "EFFIS GetCapabilities unreachable or empty"
        log.warning("%s", self.last_error)
        return False

    def probe(self) -> None:
        """Print everything EFFIS advertises. For diagnosing a name change."""
        ok = self.discover()
        print(f"endpoint: {self.endpoint or 'none reachable'}")
        if not ok:
            print(f"error: {self.last_error}")
            return
        print(f"\n{len(self.types)} feature types:")
        for n in self.types:
            tag = ("hot spots" if n in self.hs_types
                   else "burnt area" if n in self.ba_types else "")
            print(f"  {n:<44} {tag}")
        print(f"\nselected hot spot layers : {self.hs_types or '(none matched)'}")
        print(f"selected burnt area layers: {self.ba_types or '(none matched)'}")

    # ---- fetching ----------------------------------------------------
    def _wfs_json(self, typename: str, count: int, extra: str = "") -> Any:
        w, s, e, n = self.bbox
        for fmt in ("application/json", "geojson", "json"):
            q = (f"{self.endpoint}?service=WFS&version=1.1.0&request=GetFeature"
                 f"&typename={urllib.parse.quote(typename)}"
                 f"&outputformat={urllib.parse.quote(fmt)}"
                 f"&maxfeatures={count}&bbox={w},{s},{e},{n},EPSG:4326{extra}")
            try:
                raw = _get(q, timeout=90)
            except Exception as ex:                                     # noqa: BLE001
                log.debug("EFFIS %s (%s): %s", typename, fmt, ex)
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                # a MapServer error comes back as XML; surface the message
                txt = raw.decode("utf-8", "replace")
                m = re.search(r"<ServiceException[^>]*>(.*?)</ServiceException>", txt, re.S)
                if m:
                    log.debug("EFFIS %s: %s", typename, m.group(1).strip()[:180])
                continue
            if isinstance(obj, dict) and obj.get("features") is not None:
                return obj
        return None

    @staticmethod
    def _prop(props: dict, *names, default=None):
        low = {k.lower(): v for k, v in props.items()}
        for n in names:
            if n in low and low[n] not in (None, "", "null"):
                return low[n]
        return default

    def hotspots(self, hours: float) -> list[dict]:
        """Active fire detections, normalised to the app's detection shape."""
        if not self.enabled or not self.discover():
            return []
        cut = datetime.now(timezone.utc) - timedelta(hours=hours)
        out: list[dict] = []
        for tn in self.hs_types[:4]:
            obj = self._wfs_json(tn, 8000)
            if not obj:
                continue
            for ft in obj.get("features", []):
                geom = ft.get("geometry") or {}
                if geom.get("type") != "Point":
                    continue
                lon, lat = geom["coordinates"][:2]
                pr = ft.get("properties") or {}
                ts = self._parse_time(pr)
                if ts is None or ts < cut:
                    continue
                sensor = str(self._prop(pr, "satellite", "sensor", "source", default="")).upper()
                family = "MODIS" if "MODIS" in sensor or "modis" in tn else "VIIRS"
                out.append({
                    "id": f"EFFIS|{family}|{lat:.4f}|{lon:.4f}|{int(ts.timestamp())}",
                    "lat": float(lat), "lon": float(lon), "ts": ts.timestamp(),
                    "source": family,
                    "sat": f"EFFIS · {sensor or family}",
                    "frp": _f(self._prop(pr, "frp", "power")),
                    "bt": _f(self._prop(pr, "bright_t31", "brightness", "temp")),
                    "conf": self._prop(pr, "confidence", "conf", default="n"),
                    "daynight": self._prop(pr, "daynight"),
                    "scan": _f(self._prop(pr, "scan")),
                    "track": _f(self._prop(pr, "track")),
                    "provider": "EFFIS",
                })
            log.info("EFFIS %s -> %d hot spots", tn, len(out))
        return out

    def perimeters(self, days: int = 30) -> list[dict]:
        """Burnt area polygons — what has already burned, not what is hot now."""
        if not self.enabled or not self.discover():
            return []
        out: list[dict] = []
        for tn in self.ba_types[:3]:
            obj = self._wfs_json(tn, 3000)
            if not obj:
                continue
            for ft in obj.get("features", []):
                geom = ft.get("geometry") or {}
                rings = []
                if geom.get("type") == "Polygon":
                    rings = [geom["coordinates"][0]]
                elif geom.get("type") == "MultiPolygon":
                    rings = [p[0] for p in geom["coordinates"]]
                if not rings:
                    continue
                pr = ft.get("properties") or {}
                out.append({
                    "id": str(self._prop(pr, "id", "gid", "fid", default=len(out))),
                    "area": _f(self._prop(pr, "area_ha", "area", "areaha")),
                    "place": self._prop(pr, "place_name", "commune", "province", "name"),
                    "country": self._prop(pr, "country", "countryful"),
                    "start": str(self._prop(pr, "firedate", "initialdate", "startdate", default="") or ""),
                    "rings": [[[round(float(x), 4), round(float(y), 4)] for x, y in r[:1200]]
                              for r in rings[:12]],
                })
            log.info("EFFIS %s -> %d burnt-area polygons", tn, len(out))
        return out

    @staticmethod
    def _parse_time(pr: dict):
        for key in ("acq_date", "firedate", "date", "acquisition_date", "datetime", "obs_date"):
            for k, v in pr.items():
                if k.lower() != key or not v:
                    continue
                txt = str(v).strip().replace("Z", "+00:00")
                for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
                    try:
                        dt = datetime.fromisoformat(txt) if fmt is None else datetime.strptime(txt, fmt)
                        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                    except ValueError:
                        continue
        return None

    # ---- fire danger -------------------------------------------------
    def fwi(self, lat: float, lon: float) -> dict | None:
        """Fire Weather Index at a point, via WMS GetFeatureInfo.

        FWI is what European fire services plan around, and it is a forecast
        rather than a detection — the one number here that says something about
        tomorrow instead of the last few hours.
        """
        if not self.enabled:
            return None
        d = 0.05
        bbox = f"{lon - d},{lat - d},{lon + d},{lat + d}"
        for ep in ENDPOINTS:
            for layer in ("ecmwf.fwi", "fwi", "ecmwf_fwi"):
                q = (f"{ep}?service=WMS&version=1.1.1&request=GetFeatureInfo"
                     f"&layers={layer}&query_layers={layer}&srs=EPSG:4326"
                     f"&bbox={bbox}&width=3&height=3&x=1&y=1"
                     f"&info_format=application/json")
                try:
                    obj = json.loads(_get(q, timeout=30))
                except Exception:                                       # noqa: BLE001
                    continue
                feats = obj.get("features") if isinstance(obj, dict) else None
                if not feats:
                    continue
                pr = feats[0].get("properties") or {}
                for k, v in pr.items():
                    val = _f(v)
                    if val is not None:
                        return {"layer": layer, "field": k, "value": val,
                                "class": fwi_class(val)}
        return None


def fwi_class(v: float) -> str:
    """EFFIS fire-danger classes."""
    if v < 5.2:
        return "very low"
    if v < 11.2:
        return "low"
    if v < 21.3:
        return "moderate"
    if v < 38.0:
        return "high"
    if v < 50.0:
        return "very high"
    return "extreme"


def _f(v) -> float | None:
    try:
        f = float(v)
        return f if f == f else None            # drop NaN
    except (TypeError, ValueError):
        return None
