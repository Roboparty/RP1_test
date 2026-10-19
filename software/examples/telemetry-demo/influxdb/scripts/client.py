"""Small InfluxDB v2 HTTP client. Uses only the Python standard library."""

from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

FIELDS = (
    "cmd_pos_deg", "pos_deg", "tracking_error_deg", "temp_c", "spd_deg_s", "torque_nm"
)
TAGS = ("run_id", "case_id", "subject", "source_kind")


def load_env(path: str | None) -> None:
    """Load KEY=value files without executing shell code; existing env wins."""
    if not path:
        return
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
            raise ValueError(f"Invalid environment assignment at line {number}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def timestamp_ns(value: str) -> int:
    """Convert offset-aware RFC3339 to integer ns without a float round-trip."""
    match = re.fullmatch(r"(.+T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})", value)
    if not match:
        raise ValueError("observed_at must be RFC3339 with an explicit timezone")
    moment = datetime.fromisoformat(match[1] + match[3].replace("Z", "+00:00"))
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = moment.astimezone(timezone.utc) - epoch
    fraction = int((match[2] or "").ljust(9, "0"))
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + fraction


def escape_tag(value: str) -> str:
    if any(c in value for c in "\r\n\x00"):
        raise ValueError("Tags cannot contain line breaks or NUL")
    return value.replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")


def line_protocol(tags: Mapping[str, str], fields: Mapping[str, float | None], observed_ns: int) -> str:
    """Encode one point. Missing values are omitted; never substitute zero."""
    if set(tags) != set(TAGS) or any(not tags[k] for k in TAGS):
        raise ValueError(f"Exactly these non-empty tags are required: {', '.join(TAGS)}")
    if tags["source_kind"] not in ("synthetic", "real"):
        raise ValueError("source_kind must be synthetic or real")
    unknown = set(fields) - set(FIELDS)
    if unknown:
        raise ValueError(f"Unsupported fields: {', '.join(sorted(unknown))}")
    encoded = []
    for key in FIELDS:
        value = fields.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
            raise ValueError(f"{key} must be a finite number or null")
        encoded.append(f"{key}={float(value)!r}")
    if not encoded:
        raise ValueError("At least one measured field is required")
    if isinstance(observed_ns, bool) or not isinstance(observed_ns, int):
        raise ValueError("timestamp must be integer nanoseconds")
    identity = ",".join(f"{key}={escape_tag(tags[key])}" for key in TAGS)
    return f"joint_telemetry,{identity} {','.join(encoded)} {observed_ns}"


class InfluxError(RuntimeError):
    pass


class InfluxClient:
    def __init__(self) -> None:
        self.url = os.getenv("INFLUX_URL", "http://127.0.0.1:8086").rstrip("/")
        parsed = urllib.parse.urlsplit(self.url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("INFLUX_URL must be an HTTP(S) URL without embedded credentials")
        self.org = os.getenv("INFLUX_ORG", "rp1_test")
        self.bucket = os.getenv("INFLUX_BUCKET", "telemetry")
        self.token = os.getenv("INFLUX_TOKEN", "")

    def request(self, method: str, path: str, body: bytes | None = None,
                content_type: str = "application/json", *, auth: bool = True,
                accept: str = "application/json") -> bytes:
        if auth and not self.token:
            raise ValueError("Set INFLUX_TOKEN or supply --env-file with a local token")
        headers = {"Accept": accept, "Content-Type": content_type}
        if auth:
            headers["Authorization"] = f"Token {self.token}"
        request = urllib.request.Request(self.url + path, body, headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            # Response bodies can contain deployment details; do not echo them.
            raise InfluxError(f"InfluxDB {method} {path.split('?')[0]} returned HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise InfluxError(f"InfluxDB endpoint unreachable ({type(exc).__name__})") from None

    def health(self) -> dict:
        return json.loads(self.request("GET", "/health", auth=False))

    def write(self, points: str) -> None:
        params = urllib.parse.urlencode({"org": self.org, "bucket": self.bucket, "precision": "ns"})
        self.request("POST", f"/api/v2/write?{params}", points.encode(), "text/plain; charset=utf-8")

    def query(self, flux: str) -> str:
        params = urllib.parse.urlencode({"org": self.org})
        payload = json.dumps({"query": flux, "type": "flux", "dialect": {"annotations": ["datatype", "group", "default"]}}).encode()
        return self.request("POST", f"/api/v2/query?{params}", payload, accept="text/csv").decode()
