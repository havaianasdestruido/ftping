#!/usr/bin/env python3
"""Check the FTP endpoints in the ftp-sites.org list and append a JSONL report."""

from __future__ import annotations

import argparse
import json
import math
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

SITES_URL = (
    "https://gist.githubusercontent.com/mnjstwins/"
    "85ac8348d6faeb32b25908d447943300/raw/"
    "efd798cbf3649bb9681e3d1897492b61036d86e8/ftp-sites.org%2520list"
)
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_WORKERS = 64
SOURCE_TIMEOUT_SECONDS = 30.0
REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Site:
    """An FTP endpoint, represented by the URL as listed and its TCP target."""

    url: str
    host: str
    port: int


def parse_site_list(contents: str) -> list[Site]:
    """Parse FTP URLs, ignoring blank lines, comments, and non-FTP entries."""
    sites: list[Site] = []

    for raw_line in contents.splitlines():
        candidate = raw_line.strip()
        if not candidate or candidate.startswith("#"):
            continue

        # Some plain-text FTP lists omit the scheme; treat those entries as FTP.
        if "://" not in candidate:
            candidate = f"ftp://{candidate}"

        try:
            parsed = urlsplit(candidate)
            host = parsed.hostname
            if parsed.scheme.lower() not in {"ftp", "ftps"} or not host:
                continue
            if any(character.isspace() for character in host):
                continue
            # Avoid accepting credentials from a remote list or ever writing them
            # into reports. The supplied ftp-sites.org list contains host URLs.
            if parsed.username is not None or parsed.password is not None:
                continue
            default_port = 990 if parsed.scheme.lower() == "ftps" else 21
            port = parsed.port if parsed.port is not None else default_port
        except ValueError:
            # Invalid ports or malformed URLs do not prevent checking other sites.
            continue

        if not 1 <= port <= 65535:
            continue

        sites.append(Site(url=candidate, host=host, port=port))

    return sites


def load_sites(source_url: str = SITES_URL, sites_file: Path | None = None) -> list[Site]:
    """Read a local list or fetch the source list, then parse its FTP endpoints."""
    if sites_file is not None:
        contents = sites_file.read_text(encoding="utf-8-sig")
    else:
        request = Request(
            source_url,
            headers={"User-Agent": "ftping-daily-monitor/1.0", "Accept": "text/plain"},
        )
        with urlopen(request, timeout=SOURCE_TIMEOUT_SECONDS) as response:
            contents = response.read().decode("utf-8-sig")

    sites = parse_site_list(contents)
    if not sites:
        raise ValueError("The site list did not contain any valid FTP endpoints")
    return sites


def probe_site(site: Site, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, object]:
    """Check whether an FTP endpoint accepts a TCP connection on its port."""
    started = time.perf_counter()
    error: dict[str, str] | None = None

    try:
        # FTP checks use TCP port 21, not ICMP. This works on GitHub-hosted
        # runners, where ICMP echo requests are commonly blocked.
        with socket.create_connection((site.host, site.port), timeout=timeout):
            status = "up"
    except (OSError, ValueError) as exc:
        status = "down"
        error = {
            "type": type(exc).__name__,
            "message": str(exc)[:500],
        }

    checked_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    checked_at = checked_at.replace("+00:00", "Z")
    latency_ms = round((time.perf_counter() - started) * 1000, 3)

    return {
        "checked_at": checked_at,
        "url": site.url,
        "host": site.host,
        "port": site.port,
        "status": status,
        "latency_ms": latency_ms,
        "error": error,
    }


def probe_sites(
    sites: list[Site],
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    workers: int = DEFAULT_WORKERS,
) -> list[dict[str, object]]:
    """Probe endpoints concurrently, retaining the source list's order."""
    with ThreadPoolExecutor(max_workers=workers) as executor:
        return list(executor.map(lambda site: probe_site(site, timeout), sites))


def write_report(
    records: Iterable[dict[str, object]],
    reports_dir: Path,
    report_date: date | None = None,
) -> Path:
    """Append records to reports/YYYY-MM-DD.jsonl without touching older files."""
    day = report_date or datetime.now(timezone.utc).date()
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"{day.isoformat()}.jsonl"

    with report_path.open("a", encoding="utf-8", newline="\n") as report:
        for record in records:
            report.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            report.write("\n")

    return report_path


def positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return parsed


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check FTP server TCP reachability and append a daily JSONL report."
    )
    parser.add_argument(
        "--sites-file",
        type=Path,
        help="read FTP URLs from a local text file instead of downloading the Gist",
    )
    parser.add_argument(
        "--source-url",
        default=SITES_URL,
        help="URL for the plain-text FTP site list (default: the supplied Gist)",
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=REPOSITORY_ROOT / "reports",
        help="directory for dated JSONL reports (default: ./reports)",
    )
    parser.add_argument(
        "--timeout",
        type=positive_float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"per-endpoint TCP connection timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS})",
    )
    parser.add_argument(
        "--workers",
        type=positive_int,
        default=DEFAULT_WORKERS,
        help=f"number of concurrent checks (default: {DEFAULT_WORKERS})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        sites = load_sites(source_url=args.source_url, sites_file=args.sites_file)
        records = probe_sites(sites, timeout=args.timeout, workers=args.workers)
        report_path = write_report(records, args.reports_dir)
    except (OSError, URLError, UnicodeError, ValueError) as exc:
        print(f"ftping: {exc}", file=sys.stderr)
        return 1

    up_count = sum(record["status"] == "up" for record in records)
    down_count = len(records) - up_count
    print(
        f"Checked {len(records)} FTP endpoints: {up_count} up, {down_count} down. "
        f"Appended JSONL report to {report_path}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
