"""Small Prometheus and Home Assistant clients shared by the homelab InkyPi plugins."""
import json
import logging
import os
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

# Site-specific defaults (URLs, hostnames, entity ids) live in src/config/local_settings.json,
# which is gitignored. See local_settings.example.json for the keys each plugin reads.
LOCAL_SETTINGS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "local_settings.json")
_local_cache = None


def local_setting(key, default=None):
    """Return a value from local_settings.json, or the default when the file or key is absent."""
    global _local_cache
    if _local_cache is None:
        try:
            with open(LOCAL_SETTINGS_FILE) as fh:
                _local_cache = json.load(fh)
        except (OSError, json.JSONDecodeError):
            _local_cache = {}
    return _local_cache.get(key, default)


class Prom:
    def __init__(self, base_url, timeout=10):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def query(self, q):
        resp = requests.get(f"{self.base}/api/v1/query", params={"query": q}, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "success":
            raise RuntimeError(f"Prometheus query failed: {q}")
        return data["data"]["result"]

    def online(self):
        try:
            self.query("up")
            return True
        except Exception as e:
            logger.error(f"Prometheus unreachable at {self.base}: {e}")
            return False

    def scalar(self, q, default=0.0):
        try:
            r = self.query(q)
            return float(r[0]["value"][1]) if r else default
        except Exception as e:
            logger.warning(f"Prometheus scalar failed for {q}: {e}")
            return default

    def rows(self, q):
        """Return [(labels dict, float value)] for every series in the result."""
        try:
            return [(r["metric"], float(r["value"][1])) for r in self.query(q)]
        except Exception as e:
            logger.warning(f"Prometheus rows failed for {q}: {e}")
            return []

    def by_label(self, q, label):
        return sorted(((m.get(label, "?"), v) for m, v in self.rows(q)), key=lambda kv: -kv[1])

    def range(self, q, start, end, step):
        try:
            resp = requests.get(
                f"{self.base}/api/v1/query_range",
                params={"query": q, "start": start.timestamp(), "end": end.timestamp(), "step": step},
                timeout=self.timeout + 5,
            )
            resp.raise_for_status()
            result = resp.json()["data"]["result"]
            return [(float(t), float(v)) for t, v in result[0]["values"]] if result else []
        except Exception as e:
            logger.warning(f"Prometheus range failed for {q}: {e}")
            return []


class HomeAssistant:
    def __init__(self, base_url, token, timeout=15):
        self.base = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}
        self.timeout = timeout
        self._states = None

    def fetch(self, entities):
        """Fetch just the named entities' states in one round trip via the template endpoint.

        /api/states returns every entity in the instance (thousands, a megabyte or more of JSON);
        rendering a template that emits a small JSON object is far cheaper on a Pi.
        """
        import json

        parts = ", ".join(f'"{e}": states("{e}")' for e in entities)
        template = "{{ {" + parts + "} | to_json }}"
        resp = requests.post(f"{self.base}/api/template", headers=self.headers, json={"template": template}, timeout=self.timeout)
        resp.raise_for_status()
        fetched = json.loads(resp.text)
        self._states = self._states or {}
        self._states.update({e: {"entity_id": e, "state": v} for e, v in fetched.items()})
        return self._states

    def states(self):
        """Full state list. Only used when fetch() was not called first; expensive on small hosts."""
        if self._states is None:
            resp = requests.get(f"{self.base}/api/states", headers=self.headers, timeout=self.timeout)
            resp.raise_for_status()
            self._states = {s["entity_id"]: s for s in resp.json()}
        return self._states

    def raw(self, entity, default=None):
        s = self.states().get(entity)
        if not s or s["state"] in ("unknown", "unavailable", "None", ""):
            return default
        return s["state"]

    def num(self, entity, default=0.0):
        try:
            return float(self.raw(entity, default))
        except (TypeError, ValueError):
            return default

    def history(self, entities, start, end=None):
        """Return {entity: [(datetime, float)]} between start and end (aware datetimes)."""
        url = f"{self.base}/api/history/period/{start.isoformat()}"
        params = {"filter_entity_id": ",".join(entities), "minimal_response": "", "no_attributes": ""}
        if end:
            params["end_time"] = end.isoformat()
        resp = requests.get(url, headers=self.headers, params=params, timeout=self.timeout + 15)
        resp.raise_for_status()
        out = {}
        for series in resp.json():
            if not series:
                continue
            entity = series[0].get("entity_id")
            points = []
            for p in series:
                try:
                    points.append((datetime.fromisoformat(p["last_changed"].replace("Z", "+00:00")), float(p["state"])))
                except (KeyError, TypeError, ValueError):
                    continue
            out[entity] = points
        return out


def bucket_series(points, start, end, buckets):
    """Average an irregular (datetime, value) series into fixed buckets; None where no data."""
    if not points:
        return [None] * buckets
    width = (end - start).total_seconds() / buckets
    sums = [0.0] * buckets
    counts = [0] * buckets
    # carry the last known value forward so sparse state-change series still fill buckets
    last = None
    idx = 0
    for i in range(buckets):
        b_start = start.timestamp() + i * width
        b_end = b_start + width
        while idx < len(points) and points[idx][0].timestamp() < b_end:
            if points[idx][0].timestamp() >= b_start:
                sums[i] += points[idx][1]
                counts[i] += 1
            last = points[idx][1]
            idx += 1
        if counts[i] == 0 and last is not None and b_start <= end.timestamp():
            sums[i], counts[i] = last, 1
    now_ts = datetime.now(start.tzinfo).timestamp()
    return [
        (sums[i] / counts[i]) if counts[i] and (start.timestamp() + i * width) <= now_ts else None
        for i in range(buckets)
    ]


def fmt_si(n, digits=1, suffix=""):
    n = float(n or 0)
    for value, unit in ((1e21, "Z"), (1e18, "E"), (1e15, "P"), (1e12, "T"), (1e9, "G"), (1e6, "M"), (1e3, "k")):
        if abs(n) >= value:
            return f"{n / value:.{digits}f}{unit}{suffix}"
    return f"{n:.0f}{suffix}"
