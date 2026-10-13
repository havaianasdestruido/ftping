#!/usr/bin/env python3
"""Check the FTP endpoints in the ftp-sites.org list and write dated JSONL and JSON reports."""

from __future__ import annotations

import argparse
import ftplib
import json
import math
import os
import re
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
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
SUCCESS_SUFFIX = "-success"
DIRECTORY_REPORTS_NAME = "dir"
LISTING_TIMEOUT_SECONDS = 15.0

# RFC 3659 facts plus the optional Unix fact set, which carries the ownership
# and permission metadata that the base facts leave out. Servers that do not
# understand a fact name simply do not report it, and a server that rejects the
# OPTS MLST command is retried with its own default fact set.
MLSD_FACTS = (
    "type",
    "size",
    "modify",
    "create",
    "perm",
    "unique",
    "media-type",
    "unix.mode",
    "unix.uid",
    "unix.gid",
    "unix.owner",
    "unix.group",
    "unix.atime",
    "unix.ctime",
)

# LIST reports server-local wall-clock time, so a date without a year is
# interpreted as ls(1) does: the current year, unless that is in the future.
_UNIX_LIST_FUTURE_TOLERANCE = timedelta(days=1)

_MONTH_NUMBERS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


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


def utc_timestamp() -> str:
    """Return the current UTC time as an ISO-8601 string with a trailing Z."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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

    checked_at = utc_timestamp()
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


def available_report_path(reports_dir: Path, name: str) -> Path:
    """Return reports_dir/name, or the first free -1, -2, ... variant of it.

    Reruns must not clobber reports that earlier runs already wrote, so a
    taken name gets a numeric suffix before the extension:
    ``2026-10-13-success.json``, ``2026-10-13-success-1.json``,
    ``2026-10-13-success-2.json``, and so on.
    """
    candidate = reports_dir / name
    stem, suffix = Path(name).stem, Path(name).suffix
    counter = 1
    while candidate.exists():
        candidate = reports_dir / f"{stem}-{counter}{suffix}"
        counter += 1
    return candidate


def write_success_report(
    records: Iterable[dict[str, object]],
    reports_dir: Path,
    report_date: date | None = None,
) -> Path:
    """Write reports/YYYY-MM-DD-success.json holding only the endpoints that were up.

    This report stays a single valid JSON document that consumers can load in
    one pass. Reruns never overwrite an existing report: if the day's success
    file already exists, the new one is written next to it as
    ``2026-10-13-success-1.json``, then ``-2``, and so on, so every run's
    results are kept.
    """
    day = report_date or datetime.now(timezone.utc).date()
    reports_dir.mkdir(parents=True, exist_ok=True)
    success_path = available_report_path(
        reports_dir, f"{day.isoformat()}{SUCCESS_SUFFIX}.json"
    )

    all_records = list(records)
    up_records = [record for record in all_records if record.get("status") == "up"]
    document = {
        "date": day.isoformat(),
        "generated_at": utc_timestamp(),
        "checked": len(all_records),
        "up": len(up_records),
        "servers": [
            {key: value for key, value in record.items() if key != "error"}
            for record in up_records
        ],
    }

    payload = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    # Write to a temporary file first so an interrupted run cannot leave a
    # half-written report behind for the commit step to pick up.
    temp_path = success_path.with_name(f".{success_path.name}.tmp")
    try:
        temp_path.write_text(payload, encoding="utf-8")
        os.replace(temp_path, success_path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise

    return success_path


def _split_facts(text: str) -> dict[str, str]:
    """Split ``type=file;size=10;`` into ``{"type": "file", "size": "10"}``."""
    facts: dict[str, str] = {}
    for item in text.split(";"):
        if not item:
            continue
        key, separator, value = item.partition("=")
        key = key.strip().lower()
        if key:
            facts[key] = value.strip() if separator else ""
    return facts


def _optional_int(value: str | None) -> int | None:
    """Return value as an int when it is a plain decimal number, else None."""
    if value is None:
        return None
    text = str(value).strip()
    return int(text) if re.fullmatch(r"-?\d+", text) else None


def _parse_mlsd_timestamp(value: str | None) -> str | None:
    """Convert an RFC 3659 timestamp (UTC ``YYYYMMDDHHMMSS[.sss]``) to ISO-8601.

    MLSD and MLST report times in UTC, so the result carries a trailing ``Z``.
    """
    if not value:
        return None
    match = re.fullmatch(
        r"(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?(\d{2})?(?:\.(\d+))?", str(value).strip()
    )
    if match is None:
        return None
    year, month, day, hour, minute, second, fraction = match.groups()
    try:
        stamp = datetime(
            int(year),
            int(month),
            int(day),
            int(hour or 0),
            int(minute or 0),
            int(second or 0),
        )
    except ValueError:
        return None
    text = stamp.strftime("%Y-%m-%dT%H:%M:%S")
    if fraction:
        text += "." + fraction[:3].ljust(3, "0")
    return f"{text}Z"


def _parse_unix_timestamp(date_field: str, now: datetime | None = None) -> str | None:
    """Convert an ``ls -l`` timestamp ("Aug  1 12:00" / "Aug  1 2025") to ISO-8601.

    LIST only reports the server's wall-clock time, so the result has no UTC
    offset. An hour/minute field instead of a year means "within the last six
    months", so the current year is used unless that lands in the future.
    """
    parts = date_field.split()
    if len(parts) != 3:
        return None
    month = _MONTH_NUMBERS.get(parts[0][:3].lower())
    if month is None:
        return None
    try:
        day = int(parts[1])
    except ValueError:
        return None

    clock = parts[2]
    reference = now or datetime.now()
    try:
        if ":" in clock:
            hour, minute = (int(part) for part in clock.split(":", 1))
            stamp = datetime(reference.year, month, day, hour, minute)
            if stamp - reference > _UNIX_LIST_FUTURE_TOLERANCE:
                stamp = stamp.replace(year=reference.year - 1)
        else:
            stamp = datetime(int(clock), month, day)
    except ValueError:
        return None
    return stamp.isoformat(timespec="seconds")


def _parse_dos_timestamp(value: str) -> str | None:
    """Convert an MS-DOS/IIS LIST timestamp ("08-01-25 12:00PM") to ISO-8601."""
    match = re.fullmatch(
        r"(\d{1,2})-(\d{1,2})-(\d{2}|\d{4})\s+(\d{1,2}):(\d{2})\s*([AaPp][Mm])?",
        value.strip(),
    )
    if match is None:
        return None
    month, day, year, hour, minute, meridiem = match.groups()
    hour_value = int(hour)
    if meridiem is not None:
        hour_value = hour_value % 12 + (12 if meridiem.lower() == "pm" else 0)
    year_value = int(year)
    if len(year) == 2:
        year_value += 2000 if year_value < 70 else 1900
    try:
        stamp = datetime(year_value, int(month), int(day), hour_value, int(minute))
    except ValueError:
        return None
    return stamp.isoformat(timespec="seconds")


_PERMISSION_BITS = ("---", "--x", "-w-", "-wx", "r--", "r-x", "rw-", "rwx")


def _normalize_mode(mode: str | None) -> str | None:
    """Normalize an octal Unix mode to "0755" form from "0o755", "755" or "0755"."""
    if mode is None:
        return None
    digits = str(mode).strip().lower().removeprefix("0o").removeprefix("o")
    if re.fullmatch(r"[0-7]{3,4}", digits) is None:
        return None
    return digits.rjust(4, "0")


def _symbolic_mode(mode: str | None, entry_type: str) -> str | None:
    """Render a normalized octal Unix mode such as "0755" as "-rwxr-xr-x"."""
    digits = _normalize_mode(mode)
    if digits is None:
        return None
    prefix = {"directory": "d", "link": "l", "file": "-"}.get(entry_type, "-")
    special = int(digits[0], 8)
    permissions = prefix
    for index, digit in enumerate(digits[1:]):
        value = int(digit, 8)
        permissions += _PERMISSION_BITS[value]
        execute = permissions[-1]
        if index == 0 and special & 0b100:  # setuid
            permissions = permissions[:-1] + ("s" if execute == "x" else "S")
        elif index == 1 and special & 0b010:  # setgid
            permissions = permissions[:-1] + ("s" if execute == "x" else "S")
        elif index == 2 and special & 0b001:  # sticky
            permissions = permissions[:-1] + ("t" if execute == "x" else "T")
    return permissions


def _new_entry(name: str, entry_type: str) -> dict[str, object]:
    """Return an entry with every metadata key present so the JSON shape is stable.

    Facts that a server does not report stay ``null``: MLSD servers differ
    widely in which facts they support, and plain LIST does not carry creation
    or access times at all.
    """
    return {
        "name": name,
        "type": entry_type,
        "size": None,
        "modified_at": None,
        "created_at": None,
        "accessed_at": None,
        "changed_at": None,
        "created_by": None,
        "group": None,
        "uid": None,
        "gid": None,
        "permissions": None,
        "mode": None,
        "perm": None,
        "unique_id": None,
        "media_type": None,
        "link_target": None,
    }


def _entry_from_mlsd(name: str, facts: dict[str, str]) -> dict[str, object] | None:
    """Map one MLSD/MLST fact set to an entry, or None for the cdir/pdir rows."""
    raw_type = str(facts.get("type", ""))
    kind = raw_type.lower()
    if kind in {"cdir", "pdir"} or name in {".", ".."}:
        return None

    link_target = None
    if kind == "dir":
        entry_type = "directory"
    elif kind == "file":
        entry_type = "file"
    elif "link" in kind or "slink" in str(facts.get("os.unix", "")).lower():
        # ProFTPD-style symlinks report type=OS.unix=slink:/target, so the
        # target keeps the case the server sent.
        entry_type = "link"
        link_target = raw_type.partition(":")[2] or None
    else:
        entry_type = "other"

    entry = _new_entry(name, entry_type)
    entry["size"] = _optional_int(facts.get("size"))
    entry["modified_at"] = _parse_mlsd_timestamp(facts.get("modify"))
    entry["created_at"] = _parse_mlsd_timestamp(facts.get("create"))
    entry["accessed_at"] = _parse_mlsd_timestamp(facts.get("unix.atime"))
    entry["changed_at"] = _parse_mlsd_timestamp(facts.get("unix.ctime"))
    entry["created_by"] = facts.get("unix.owner") or None
    entry["group"] = facts.get("unix.group") or None
    entry["uid"] = _optional_int(facts.get("unix.uid"))
    entry["gid"] = _optional_int(facts.get("unix.gid"))
    entry["mode"] = _normalize_mode(facts.get("unix.mode"))
    entry["permissions"] = _symbolic_mode(facts.get("unix.mode"), entry_type)
    entry["perm"] = facts.get("perm") or None
    entry["unique_id"] = facts.get("unique") or None
    entry["media_type"] = facts.get("media-type") or None
    entry["link_target"] = link_target
    return entry


def _entry_from_unix_list_line(line: str) -> dict[str, object] | None:
    """Parse an ``ls -l`` line, including owner, group, mode and mtime."""
    match = re.match(
        r"^(?P<flag>[-dlbcpsD])(?P<mode>[rwxstST-]{9})\s+\d+\s+(?P<owner>\S+)\s+"
        r"(?P<group>\S+)\s+(?P<size>\d+(?:,\s*\d+)?)\s+"
        r"(?P<date>[A-Za-z]{3}\s+\d{1,2}\s+(?:\d{1,2}:\d{2}|\d{4}))\s+(?P<name>.+)$",
        line,
    )
    if match is None:
        return None

    flag = match.group("flag")
    entry_type = {"d": "directory", "-": "file", "l": "link"}.get(flag, "other")
    name = match.group("name").strip()
    link_target = None
    if entry_type == "link" and " -> " in name:
        name, link_target = (part.strip() for part in name.split(" -> ", 1))
    if name in {".", ".."} or not name:
        return None

    size_text = match.group("size")
    entry = _new_entry(name, entry_type)
    entry["size"] = int(size_text) if size_text.isdigit() else None
    entry["permissions"] = f"{flag}{match.group('mode')}"
    entry["modified_at"] = _parse_unix_timestamp(match.group("date"))
    entry["created_by"] = match.group("owner") or None
    entry["group"] = match.group("group") or None
    entry["link_target"] = link_target or None
    return entry


def _entry_from_dos_list_line(line: str) -> dict[str, object] | None:
    """Parse an MS-DOS/IIS ``dir`` line such as ``08-01-25 12:00PM <DIR> pub``."""
    match = re.match(
        r"^(?P<date>\d{1,2}-\d{1,2}-\d{2,4})\s+(?P<time>\d{1,2}:\d{2}(?:\s?[APap][Mm])?)\s+"
        r"(?P<size><DIR>|<JUNCTION>|<SYMLINK>|\d+)\s+(?P<name>.+)$",
        line,
    )
    if match is None:
        return None

    marker = match.group("size").upper()
    if marker == "<DIR>":
        entry_type = "directory"
    elif marker in {"<JUNCTION>", "<SYMLINK>"}:
        entry_type = "link"
    else:
        entry_type = "file"

    name = match.group("name").strip()
    link_target = None
    if entry_type == "link" and " -> " in name:
        name, link_target = (part.strip() for part in name.split(" -> ", 1))
    elif entry_type == "link" and name.endswith("]") and " [" in name:
        name, _, link_target = name[:-1].partition(" [")
    if name in {".", ".."} or not name:
        return None

    entry = _new_entry(name, entry_type)
    entry["size"] = int(marker) if entry_type == "file" else None
    entry["modified_at"] = _parse_dos_timestamp(
        f"{match.group('date')} {match.group('time')}"
    )
    entry["link_target"] = link_target or None
    return entry


def _entry_from_eplf_line(line: str) -> dict[str, object] | None:
    """Parse an EPLF line such as ``+i81,m824255902,r,s1024,\tfile.txt``."""
    facts_text, separator, name = line.partition("\t")
    if not separator or not facts_text.startswith("+") or not name:
        return None

    entry_type = "file"
    size = None
    modified_at = None
    unique_id = None
    for fact in facts_text[1:].split(","):
        if fact == "/":
            entry_type = "directory"
        elif fact.startswith("s") and fact[1:].isdigit():
            size = int(fact[1:])
        elif fact.startswith("m") and fact[1:].isdigit():
            try:
                modified_at = (
                    datetime.fromtimestamp(int(fact[1:]), timezone.utc)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                )
            except (OverflowError, OSError, ValueError):
                modified_at = None
        elif fact.startswith("i") and fact[1:]:
            unique_id = fact[1:]

    entry = _new_entry(name, entry_type)
    entry["size"] = size
    entry["modified_at"] = modified_at
    entry["unique_id"] = unique_id
    return entry


def _entry_from_list_line(line: str) -> dict[str, object] | None:
    """Parse one LIST line: Unix ``ls -l``, MS-DOS/IIS ``dir``, or EPLF."""
    text = line.rstrip("\r\n")
    if not text.strip() or text.strip().lower().startswith("total "):
        return None
    if "\t" in text and text.lstrip().startswith("+"):
        return _entry_from_eplf_line(text)
    return _entry_from_unix_list_line(text) or _entry_from_dos_list_line(text)


def _summarize_entries(entries: list[dict[str, object]]) -> dict[str, object]:
    """Count one directory's entries by type and total the sizes it reported."""
    summary: dict[str, object] = {
        "entries": len(entries),
        "files": 0,
        "directories": 0,
        "links": 0,
        "other": 0,
        "file_bytes": 0,
        "oldest_modified_at": None,
        "latest_modified_at": None,
    }
    counter = {
        "file": "files",
        "directory": "directories",
        "link": "links",
        "other": "other",
    }
    modified: list[str] = []
    for entry in entries:
        key = counter.get(str(entry.get("type")), "other")
        summary[key] = int(summary[key]) + 1
        size = entry.get("size")
        if key == "files" and isinstance(size, int) and size >= 0:
            summary["file_bytes"] = int(summary["file_bytes"]) + size
        stamp = entry.get("modified_at")
        if isinstance(stamp, str):
            modified.append(stamp)
    if modified:
        summary["oldest_modified_at"] = min(modified)
        summary["latest_modified_at"] = max(modified)
    return summary


def _empty_server_metadata() -> dict[str, object]:
    """Return the server-identification block with every key present."""
    return {
        "welcome": None,
        "login_response": None,
        "system": None,
        "features": [],
        "encoding": None,
    }


def _strip_response_code(response: str) -> str:
    """Drop the leading three-digit FTP reply code from a response."""
    text = str(response).strip()
    match = re.match(r"^\d{3}[-\s](.*)$", text, re.DOTALL)
    return (match.group(1) if match is not None else text).strip()


def _server_features(ftp: ftplib.FTP) -> list[str]:
    """Return the FEAT feature names the server advertises, if it supports FEAT."""
    try:
        response = ftp.sendcmd("FEAT")
    except (*ftplib.all_errors, UnicodeError):
        return []
    features: list[str] = []
    for line in str(response).splitlines():
        text = line.strip()
        if not text or text[:3].isdigit() or text.endswith(":"):
            continue
        name = text.split(" ", 1)[0]
        if name and name not in features:
            features.append(name)
    return features


def _collect_server_metadata(
    ftp: ftplib.FTP, login_response: str | None = None
) -> dict[str, object]:
    """Best-effort server identification: greeting, OS type, features, encoding."""
    server = _empty_server_metadata()
    try:
        welcome = ftp.getwelcome()
    except (*ftplib.all_errors, UnicodeError):
        welcome = None
    if welcome:
        server["welcome"] = str(welcome)[:500]
    if login_response:
        server["login_response"] = _strip_response_code(login_response)[:500]
    try:
        system = _strip_response_code(ftp.sendcmd("SYST"))
    except (*ftplib.all_errors, UnicodeError):
        system = ""
    server["system"] = system[:200] or None

    features = _server_features(ftp)
    server["features"] = features
    if "UTF8" in features:
        # Ask the server to speak UTF-8 so non-ASCII names arrive intact.
        try:
            ftp.voidcmd("OPTS UTF8 ON")
        except (*ftplib.all_errors, UnicodeError):
            pass
        else:
            ftp.encoding = "utf-8"
            server["encoding"] = "utf-8"
    return server


def _open_ftp_connection(
    host: str, port: int, timeout: float, scheme: str = "ftp"
) -> ftplib.FTP:
    """Connect to an FTP endpoint, using TLS for ftps:// URLs."""
    ftp: ftplib.FTP = ftplib.FTP_TLS() if scheme == "ftps" else ftplib.FTP()
    try:
        ftp.connect(host, port, timeout=timeout)
    except BaseException:
        ftp.close()
        raise
    return ftp


def _collect_mlsd(
    ftp: ftplib.FTP, path: str, facts: list[str]
) -> tuple[list[dict[str, object]], dict[str, str]]:
    """Run MLSD and return its entries plus the facts of the directory itself."""
    entries: list[dict[str, object]] = []
    root_facts: dict[str, str] = {}
    for name, reported in ftp.mlsd(path, facts=facts):
        lowered = {str(key).lower(): str(value) for key, value in reported.items()}
        if lowered.get("type", "").lower() in {"cdir", "pdir"}:
            # The cdir row describes the listed directory itself.
            if lowered["type"].lower() == "cdir" and not root_facts:
                root_facts = lowered
            continue
        entry = _entry_from_mlsd(name, lowered)
        if entry is not None:
            entries.append(entry)
    return entries, root_facts


def _mlsd_entries(
    ftp: ftplib.FTP, path: str
) -> tuple[list[dict[str, object]], dict[str, str]]:
    """List a directory with MLSD, retrying with the default facts if needed.

    Some servers reject an OPTS MLST fact list they do not recognize, so the
    second attempt sends no fact list and accepts the server's own defaults.
    """
    last_error: Exception = ftplib.error_perm("MLSD produced no result")
    for facts in (list(MLSD_FACTS), []):
        try:
            return _collect_mlsd(ftp, path, facts)
        except (*ftplib.all_errors, UnicodeError) as exc:
            last_error = exc
    raise last_error


def _mlst_root_facts(ftp: ftplib.FTP, path: str) -> dict[str, str]:
    """Ask for a directory's own facts with MLST; {} when it is unsupported."""
    try:
        response = ftp.sendcmd(f"MLST {path}")
    except (*ftplib.all_errors, UnicodeError):
        return {}
    for line in str(response).splitlines():
        text = line.strip()
        if not text or text[:3].isdigit() or text.endswith(":"):
            continue
        facts = _split_facts(text.split(" ", 1)[0])
        if facts:
            return facts
    return {}


def _list_entries(
    ftp: ftplib.FTP, path: str
) -> tuple[str, list[dict[str, object]], int]:
    """Fall back to LIST and parse whatever format the server speaks.

    The returned count of unparsed lines keeps an empty ``entries`` list
    honest: it separates an empty directory from a listing format this parser
    does not understand.
    """
    lines: list[str] = []
    ftp.retrlines(f"LIST {path}", lines.append)

    method = "list"
    for line in lines:
        text = line.strip()
        if not text:
            continue
        if text.startswith("+") and "\t" in text:
            method = "eplf"
        elif re.match(r"^\d{1,2}-\d{1,2}-\d{2,4}\s", text):
            method = "dos"
        break

    entries: list[dict[str, object]] = []
    unparsed = 0
    for line in lines:
        if not line.strip() or line.strip().lower().startswith("total "):
            continue
        entry = _entry_from_list_line(line)
        if entry is None:
            unparsed += 1
        else:
            entries.append(entry)
    return method, entries, unparsed


def list_root_directory(
    host: str,
    port: int,
    timeout: float = LISTING_TIMEOUT_SECONDS,
    path: str = "/",
    scheme: str = "ftp",
) -> dict[str, object]:
    """Log in anonymously and collect a directory's metadata and entries.

    MLSD is preferred: its facts (size, modify, create, perm, unix.*) are
    machine readable and its timestamps are UTC. Servers without MLSD support
    are handled with LIST, where Unix ``ls -l`` output (owner, group, mode and
    mtime), MS-DOS/IIS ``dir`` output and EPLF lines are parsed best-effort.
    """
    ftp = _open_ftp_connection(host, port, timeout, scheme=scheme)
    try:
        login_response = ftp.login()  # anonymous
        server = _collect_server_metadata(ftp, login_response)
        if scheme == "ftps":
            try:
                ftp.prot_p()
            except (*ftplib.all_errors, UnicodeError):
                pass

        try:
            entries, root_facts = _mlsd_entries(ftp, path)
            method, timestamps, unparsed = "mlsd", "utc", 0
        except (*ftplib.all_errors, UnicodeError):
            method, entries, unparsed = _list_entries(ftp, path)
            root_facts = _mlst_root_facts(ftp, path)
            timestamps = "utc" if method == "eplf" else "server-local"

        if not root_facts:
            # Many servers omit the cdir row from MLSD; MLST still describes the
            # directory itself when the server implements it.
            root_facts = _mlst_root_facts(ftp, path)

        root_entry = _new_entry(path, "directory")
        if root_facts:
            # A cdir fact set describes a directory, so report it as one.
            described = _entry_from_mlsd(path, {**root_facts, "type": "dir"})
            if described is not None:
                root_entry = described

        return {
            "listing_method": method,
            "timestamps_timezone": timestamps,
            "unparsed_lines": unparsed,
            "server": server,
            "root": root_entry,
            "entries": entries,
        }
    finally:
        try:
            ftp.quit()
        except (*ftplib.all_errors, UnicodeError):
            pass
        ftp.close()


def _safe_filename(host: str) -> str:
    return "".join(c if c.isalnum() or c in ".-_" else "_" for c in host)


def build_directory_listing(
    record: dict[str, object],
    timeout: float = LISTING_TIMEOUT_SECONDS,
    path: str = "/",
) -> dict[str, object]:
    """Describe the root directory of one up endpoint; never raises on FTP errors.

    The document keeps one section per kind of information: the reachability
    fields of the TCP check, timings, the server's own identification, the
    facts of the directory itself, an entry summary, and one record per entry
    with its type, size, times, ownership and permissions.
    """
    host = str(record["host"])
    port = int(record["port"])
    url = str(record.get("url") or f"ftp://{host}/")
    scheme = urlsplit(url).scheme.lower() or "ftp"
    entries: list[dict[str, object]] = []

    listing: dict[str, object] = {
        "host": host,
        "url": url,
        "port": port,
        "scheme": scheme,
        "checked_at": record.get("checked_at"),
        "tcp_latency_ms": record.get("latency_ms"),
        "started_at": None,
        "listed_at": None,
        "duration_ms": None,
        "path": path,
        "login": "anonymous",
        "status": "ok",
        "error": None,
        "listing_method": None,
        "timestamps_timezone": None,
        "unparsed_lines": 0,
        "server": _empty_server_metadata(),
        "root": _new_entry(path, "directory"),
        "summary": _summarize_entries(entries),
        "entries": entries,
    }

    started = time.perf_counter()
    listing["started_at"] = utc_timestamp()
    try:
        result = list_root_directory(
            host, port, timeout=timeout, path=path, scheme=scheme
        )
    except (*ftplib.all_errors, OSError, ValueError, UnicodeError) as exc:
        listing["status"] = "error"
        listing["error"] = {"type": type(exc).__name__, "message": str(exc)[:500]}
    else:
        listing["listing_method"] = result["listing_method"]
        listing["timestamps_timezone"] = result["timestamps_timezone"]
        listing["unparsed_lines"] = result.get("unparsed_lines", 0)
        listing["server"] = result["server"]
        listing["root"] = result["root"]
        entries = list(result["entries"])  # type: ignore[arg-type]
        listing["entries"] = entries
        listing["summary"] = _summarize_entries(entries)

    listing["listed_at"] = utc_timestamp()
    listing["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return listing


def write_directory_listings(
    records: Iterable[dict[str, object]],
    reports_dir: Path,
    timeout: float = LISTING_TIMEOUT_SECONDS,
    workers: int = DEFAULT_WORKERS,
) -> list[Path]:
    """Write reports/dir/<host>.json with the root listing of every up endpoint.

    Only endpoints whose status is ``up`` are listed. Each host is listed once
    even if several URLs point at it. Each file is replaced atomically with the
    latest snapshot; git history keeps earlier snapshots.
    """
    up_records: dict[str, dict[str, object]] = {}
    for record in records:
        if record.get("status") == "up":
            up_records.setdefault(str(record["host"]), record)

    directory = reports_dir / DIRECTORY_REPORTS_NAME
    directory.mkdir(parents=True, exist_ok=True)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        listings = list(
            executor.map(
                lambda record: build_directory_listing(record, timeout=timeout),
                up_records.values(),
            )
        )

    written: list[Path] = []
    for listing in listings:
        path = directory / f"{_safe_filename(str(listing['host']))}.json"
        temp_path = path.with_name(f".{path.name}.tmp")
        try:
            temp_path.write_text(
                json.dumps(listing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            os.replace(temp_path, path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
        written.append(path)
    return written


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
        description=(
            "Check FTP server TCP reachability, append a daily JSONL report, and write "
            "a dated success JSON report with the endpoints that were up."
        )
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
        help="directory for dated JSONL and success JSON reports (default: ./reports)",
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
        success_path = write_success_report(records, args.reports_dir)
        listing_paths = write_directory_listings(
            records, args.reports_dir, workers=args.workers
        )
    except (OSError, URLError, UnicodeError, ValueError) as exc:
        print(f"ftping: {exc}", file=sys.stderr)
        return 1

    up_count = sum(record["status"] == "up" for record in records)
    down_count = len(records) - up_count
    print(
        f"Checked {len(records)} FTP endpoints: {up_count} up, {down_count} down. "
        f"Appended JSONL report to {report_path}. "
        f"Wrote {up_count} reachable endpoint(s) to {success_path}. "
        f"Wrote {len(listing_paths)} root directory listing(s) to "
        f"{args.reports_dir / DIRECTORY_REPORTS_NAME}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
