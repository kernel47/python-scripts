#!/usr/bin/env python3
"""Icinga 2 satellite health, Python 3.9+. See icinga_satellite_health.md."""
import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
import logging
import math
import os
import signal
import socket
import sys
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union
from urllib.parse import urlsplit
import warnings

ICINGA_API_URL = "https://localhost:5665"
ICINGA_API_USER = ""
ICINGA_API_PASSWORD = ""
VERIFY_TLS = True
TCP_TIMEOUT = 3.0
CLUSTER_LAG_WARNING = 10.0
CLUSTER_LAG_CRITICAL = 30.0
SATELLITES = {
    "EMEA": [
        {"name": "emea-sat-01", "endpoint": "emea-sat-01", "zone": "backup-emea", "host": "emea-sat-01.domain"},
        {"name": "emea-sat-02", "endpoint": "emea-sat-02", "zone": "backup-emea", "host": "emea-sat-02.domain"},
    ],
    "APAC": [
        {"name": "apac-sat-01", "endpoint": "apac-sat-01", "zone": "backup-apac", "host": "apac-sat-01.domain"},
        {"name": "apac-sat-02", "endpoint": "apac-sat-02", "zone": "backup-apac", "host": "apac-sat-02.domain"},
    ],
    "AMER": [
        {"name": "amer-sat-01", "endpoint": "amer-sat-01", "zone": "backup-amer", "host": "amer-sat-01.domain"},
        {"name": "amer-sat-02", "endpoint": "amer-sat-02", "zone": "backup-amer", "host": "amer-sat-02.domain"},
    ],
}
CODES = {"OK": 0, "WARNING": 1, "CRITICAL": 2, "UNKNOWN": 3}
LOG = logging.getLogger("satellite_health")


class CheckError(Exception):
    """Safe diagnostic, never includes credentials or API response bodies."""


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CheckError("Invalid CLI arguments; see --help")


@dataclass
class SatelliteResult:
    name: str
    region: str
    endpoint: str
    zone: str
    cluster_connected: Optional[bool] = None
    cluster_lag: Optional[float] = None
    tcp_reachable: Optional[bool] = None
    tcp_latency_ms: Optional[float] = None
    status: str = "UNKNOWN"
    message: str = "Not checked"


@contextmanager
def deadline(seconds: float) -> Iterator[None]:
    """Linux master: bounds the entire check, including blocking DNS resolution."""
    if not hasattr(signal, "setitimer"):
        raise CheckError("A POSIX system with setitimer is required")
    def expired(signum: int, frame: Any) -> None:
        raise CheckError("Overall check timeout exceeded")
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class IcingaAPI:
    def __init__(self, url: str, user: str, password: str,
                 verify: Union[bool, str], timeout: float) -> None:
        try:
            import requests
        except ImportError:
            raise CheckError("Missing dependency: install python3-requests") from None
        self.requests = requests
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.trust_env = False  # No proxy or .netrc credentials for the local API.
        self.session.auth = (user, password)
        self.session.verify = verify
        self.session.headers.update({"Accept": "application/json"})

    def close(self) -> None:
        self.session.close()

    def objects(self, kind: str) -> Dict[str, Dict[str, Any]]:
        # Retrieve all attrs: absent optional lag fields on older versions are harmless.
        try:
            with warnings.catch_warnings():
                if self.session.verify is False:
                    warnings.simplefilter("ignore", self.requests.packages.urllib3.exceptions.InsecureRequestWarning)
                response = self.session.get(self.url + "/v1/objects/" + kind,
                                            timeout=(self.timeout, self.timeout),
                                            allow_redirects=False)
            if response.status_code != 200:
                raise CheckError(f"Master API returned HTTP {response.status_code} for {kind}")
            payload = response.json()
        except self.requests.exceptions.RequestException:
            raise CheckError("Master API request failed (network, TLS or timeout)") from None
        except ValueError:
            raise CheckError("Master API returned invalid JSON") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise CheckError("Unexpected master API response schema")
        objects: Dict[str, Dict[str, Any]] = {}
        for row in payload["results"]:
            if (not isinstance(row, dict) or not isinstance(row.get("name"), str)
                    or not isinstance(row.get("attrs"), dict) or row["name"] in objects):
                raise CheckError("Invalid or duplicate API object")
            objects[row["name"]] = row["attrs"]
        LOG.debug("Retrieved %d %s", len(objects), kind)
        return objects

    def endpoints(self) -> Dict[str, Dict[str, Any]]:
        return self.objects("endpoints")

    def zones(self) -> Dict[str, Dict[str, Any]]:
        return self.objects("zones")


def cluster_lag(attrs: Dict[str, Any], now: float) -> Optional[float]:
    """Same semantics as ApiListener::CalculateZoneLag, per endpoint.

    This is replay-log lag, NOT time since the last message or check latency.
    The script runs on the master, so timestamps share the same system clock.
    """
    position = attrs.get("remote_log_position")
    if (type(attrs.get("connected")) is not bool or type(attrs.get("syncing")) is not bool
            or type(position) not in (int, float) or not math.isfinite(position) or position < 0):
        return None
    if (attrs["syncing"] or not attrs["connected"]) and position != 0:
        return max(0.0, now - position)
    return 0.0


def test_tcp(host: str, timeout: float) -> Tuple[bool, Optional[float]]:
    start = time.perf_counter()
    try:
        with socket.create_connection((host, 5665), timeout=timeout):
            return True, round((time.perf_counter() - start) * 1000, 3)
    except OSError:
        return False, None


def calculate_status(connected: bool, lag: Optional[float], tcp: Optional[bool],
                     warning: float, critical: float) -> Tuple[str, str]:
    if not connected:
        if tcp is True:
            return "WARNING", "Endpoint reachable but not connected to Icinga cluster"
        if tcp is False:
            return "CRITICAL", "Satellite disconnected from cluster and unreachable on TCP/5665"
        return "CRITICAL", "Satellite disconnected from Icinga cluster; TCP test disabled"
    status = "CRITICAL" if lag is not None and lag >= critical else (
        "WARNING" if lag is not None and lag >= warning else "OK")
    message = "Cluster connected"
    if status != "OK":
        message += f", high cluster lag ({lag:.3f}s)"
    if tcp is False:
        message += ", direct TCP unavailable"
    return status, message


def aggregate_status(statuses: List[str]) -> str:
    # Incomplete observations take precedence, as required for internal/API failures.
    return max(statuses, key=CODES.__getitem__, default="UNKNOWN")


def aggregate_regions(results: List[SatelliteResult]) -> Dict[str, Any]:
    regions: Dict[str, Any] = {}
    for result in results:
        regions.setdefault(result.region, {"satellites": []})["satellites"].append(asdict(result))
    for region in regions.values():
        region["status"] = aggregate_status([s["status"] for s in region["satellites"]])
    return regions


def clean(value: str) -> str:
    return " ".join(value.split()).replace("|", "/")


def web_status(status: str) -> str:
    """Unicode status indicators; rendering depends on browser emoji fonts."""
    icons = {"OK": "🟢", "WARNING": "🟡", "CRITICAL": "🔴", "UNKNOWN": "🟣"}
    return f"{icons[status]} {status}"


def format_icinga(report: Dict[str, Any], warning: float, critical: float) -> str:
    satellites = [s for r in report["regions"].values() for s in r["satellites"]]
    counts = {s: sum(x["status"] == s for x in satellites) for s in CODES}
    perf = [f"{s.lower()}={counts[s]}" for s in CODES]
    summary = report.get("message") or f"{counts['OK']}/{len(satellites)} satellites OK"
    issues = [f"{s['name']}: {s['message']}" for s in satellites if s["status"] != "OK"]
    if issues and not report.get("message"):
        summary += "; " + "; ".join(issues[:2])
        if len(issues) > 2:
            summary += f"; +{len(issues) - 2} other issue(s)"
    lines = []
    for region, data in report["regions"].items():
        lines.append(f"{clean(region)}: {web_status(data['status'])}")
        for s in data["satellites"]:
            # Hex encoding is collision-free, including punctuation in endpoint names.
            label = "ep_" + s["endpoint"].encode("utf-8").hex()
            detail = s["message"]
            if s["tcp_latency_ms"] is not None:
                perf.append(f"'{label}_latency'={s['tcp_latency_ms']:.3f}ms;;;0;")
                detail += f" - tcp={s['tcp_latency_ms']:.3f}ms"
            if s["cluster_lag"] is not None:
                perf.append(f"'{label}_lag'={s['cluster_lag']:.3f}s;{warning};{critical};0;")
                detail += f" - lag={s['cluster_lag']:.3f}s"
            else:
                detail += " - lag unavailable"
            lines.append(f"  {clean(s['name'])}: {web_status(s['status'])} - {clean(detail)}")
    return "\n".join([f"{web_status(report['status'])} - {clean(summary)} | {' '.join(perf)}"] + lines)


def format_json(report: Dict[str, Any]) -> str:
    return json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False)


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = Parser(description=__doc__)
    p.add_argument("--debug", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-tcp", action="store_true")
    p.add_argument("--api-url", default=os.getenv("ICINGA_API_URL", ICINGA_API_URL))
    p.add_argument("--credentials-file", help='JSON file containing "user" and "password"')
    p.add_argument("--tcp-timeout", type=float, default=TCP_TIMEOUT)
    p.add_argument("--api-timeout", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=50.0, help="Overall deadline, including DNS")
    p.add_argument("--lag-warning", type=float, default=CLUSTER_LAG_WARNING)
    p.add_argument("--lag-critical", type=float, default=CLUSTER_LAG_CRITICAL)
    tls = p.add_mutually_exclusive_group()
    tls.add_argument("--ca-file", help="Trusted CA PEM file")
    tls.add_argument("--insecure", action="store_true", help="Explicitly disable TLS verification")
    a = p.parse_args(argv)
    for value in (a.tcp_timeout, a.api_timeout, a.timeout, a.lag_warning, a.lag_critical):
        if not math.isfinite(value) or value <= 0:
            raise CheckError("Timeouts and thresholds must be finite and positive")
    if a.lag_warning >= a.lag_critical:
        raise CheckError("Lag warning must be less than lag critical")
    url = urlsplit(a.api_url)
    if (url.scheme != "https" or not url.hostname or url.username is not None
            or url.password is not None or url.query or url.fragment or url.path not in ("", "/")):
        raise CheckError("API URL must be an HTTPS origin without credentials, query or path")
    return a


def credentials(path: Optional[str]) -> Tuple[str, str]:
    data: Dict[str, Any] = {}
    if path:
        try:
            with open(path, encoding="utf-8") as stream:
                data = json.load(stream)
        except (OSError, ValueError):
            raise CheckError("Cannot read credentials JSON file") from None
        if not isinstance(data, dict):
            raise CheckError("Credentials file must contain a JSON object")
    user = os.getenv("ICINGA_API_USER", data.get("user", ICINGA_API_USER))
    password = os.getenv("ICINGA_API_PASSWORD", data.get("password", ICINGA_API_PASSWORD))
    if not isinstance(user, str) or not isinstance(password, str) or not user or not password:
        raise CheckError("Missing API credentials (environment or --credentials-file)")
    return user, password


def run_check(a: argparse.Namespace) -> Dict[str, Any]:
    user, password = credentials(a.credentials_file)
    results = []
    seen = set()
    for region, satellites in SATELLITES.items():
        if not satellites:
            raise CheckError("Empty satellite region")
        for s in satellites:
            if any(not isinstance(s.get(k), str) or not s[k].strip() for k in ("name", "endpoint", "zone", "host")):
                raise CheckError("Invalid satellite configuration")
            if s["endpoint"] in seen:
                raise CheckError("Duplicate satellite endpoint")
            seen.add(s["endpoint"])
    if not seen:
        raise CheckError("Empty satellite configuration")
    api = IcingaAPI(a.api_url, user, password, False if a.insecure else (a.ca_file or VERIFY_TLS), a.api_timeout)
    try:
        endpoints = api.endpoints()
        observed_at = time.time()
        zones = api.zones()
    finally:
        api.close()
    for region, satellites in SATELLITES.items():
        for s in satellites:
            result = SatelliteResult(s["name"], region, s["endpoint"], s["zone"])
            attrs = endpoints.get(s["endpoint"])
            zone = zones.get(s["zone"])
            if attrs is None or zone is None:
                result.message = "Endpoint or Zone missing from master API"
            elif not isinstance(zone.get("endpoints"), list) or s["endpoint"] not in zone["endpoints"]:
                result.message = "Endpoint is not a member of configured Zone"
            elif type(attrs.get("connected")) is not bool:
                result.message = "Endpoint runtime connected attribute missing or invalid"
            else:
                result.cluster_connected = attrs["connected"]
                result.cluster_lag = cluster_lag(attrs, observed_at)
                if not a.no_tcp:
                    result.tcp_reachable, result.tcp_latency_ms = test_tcp(s["host"], a.tcp_timeout)
                result.status, result.message = calculate_status(
                    result.cluster_connected, result.cluster_lag, result.tcp_reachable,
                    a.lag_warning, a.lag_critical)
            results.append(result)
    regions = aggregate_regions(results)
    status = aggregate_status([r.status for r in results])
    return {"status": status, "exit_code": CODES[status], "regions": regions,
            "observed_at": observed_at, "lag_source": "Endpoint replay log (CalculateZoneLag semantics)"}


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    json_mode, debug = "--json" in argv, "--debug" in argv
    warning, critical = CLUSTER_LAG_WARNING, CLUSTER_LAG_CRITICAL
    if debug:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")
        # Do not enable requests/urllib3 wire diagnostics.
        logging.getLogger("urllib3").setLevel(logging.CRITICAL)
    try:
        a = parse_args(argv)
        warning, critical = a.lag_warning, a.lag_critical
        with deadline(a.timeout):
            report = run_check(a)
    except Exception as exc:
        message = str(exc) if isinstance(exc, CheckError) else "Internal error (use --debug for stack locations)"
        report = {"status": "UNKNOWN", "exit_code": 3, "message": message, "regions": {}}
        if debug:
            # Stack locations only: exception strings/locals can contain passwords.
            import traceback
            LOG.debug("Failure type: %s", type(exc).__name__)
            for frame in traceback.extract_tb(exc.__traceback__):
                LOG.debug("%s:%d in %s", frame.filename, frame.lineno, frame.name)
    print(format_json(report) if json_mode else format_icinga(report, warning, critical))
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
