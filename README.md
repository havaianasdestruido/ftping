# ftping

A daily reachability monitor for the FTP endpoints in the [ftp-sites.org list](https://gist.githubusercontent.com/mnjstwins/85ac8348d6faeb32b25908d447943300/raw/efd798cbf3649bb9681e3d1897492b61036d86e8/ftp-sites.org%2520list).

## What it does

- `.github/workflows/daily-ftp-ping.yml` runs every day at **03:17 UTC** and can also be started manually with **Actions → Daily FTP reachability → Run workflow**.
- `scripts/ping_sites.py` downloads the current list from the Gist, then checks every valid FTP endpoint concurrently by opening a TCP connection (normally port 21). This is a TCP reachability check, not an ICMP echo ping, which makes it suitable for GitHub-hosted runners.
- Each run appends one JSON object per endpoint to `reports/YYYY-MM-DD.jsonl`, using the UTC date. A rerun on the same day appends to that day's file; it never truncates reports or removes older dates.
- Each run also writes `reports/YYYY-MM-DD-success.json`, which lists **only the endpoints that answered** (status `up`) for that date. Reruns never overwrite an existing report: if the day's success file is already there, the new report is written next to it as `reports/YYYY-MM-DD-success-1.json`, then `-2`, and so on, so every run's results are kept.
- Each JSONL record includes the check timestamp, URL, host, port, `up`/`down` status, connection latency, and error details when a connection fails. A down site is recorded but does not fail the workflow.
- For every endpoint in `reports/YYYY-MM-DD-success.json`, the run also logs in anonymously over FTP, lists the server's **root directory**, and writes `reports/dir/<host>.json` (for example `reports/dir/ftp.website.org.json`). Only `up` endpoints are listed, each host once. A failed login or listing is recorded in the file's `status`/`error` fields and does not fail the workflow. Each file is replaced on every run, so older snapshots are kept in Git history.
- Each `reports/dir/<host>.json` captures far more than names and sizes:
  - **Server metadata** — greeting, login reply, `SYST` type, the `FEAT` feature list, and the encoding (`UTF8` is switched on when the server supports it).
  - **Listing metadata** — which command produced the entries (`mlsd`, `list`, or `eplf`), whether the entry timestamps are UTC or the server's local time, when the listing started and finished, and how long it took.
  - **Root directory metadata** — the root's own `modify`, `create`, `unix.mode`, `unix.owner`, `unix.group` and `perm` facts, taken from the MLSD `cdir` row or `MLST`.
  - **Entry summary** — how many files, directories, links and other entries the root holds, the total size of the files, and the oldest and newest modification times. `unparsed_lines` counts listing lines no parser understood, so an empty `entries` list can be told apart from an unfamiliar listing format.
  - **Per entry** — name, type, size, `modified_at`, `created_at`, `accessed_at`, `changed_at`, the owner (`created_by`), `group`, `uid`, `gid`, octal `mode`, symbolic `permissions`, FTP `perm` letters, `unique_id`, `media_type` and `link_target`.
- Facts a server does not report are `null` rather than missing, so every file has the same keys. MLSD/MLST timestamps are UTC (a trailing `Z`); LIST has no timezone, so its timestamps are the server's local wall-clock time and `timestamps_timezone` is `server-local`. LIST output is parsed whether the server speaks Unix `ls -l`, MS-DOS/IIS `dir`, or EPLF.
- The workflow commits the report back to the branch on which it ran. It requests `contents: write` permission for this.

Example report line:

```json
{"checked_at":"2026-10-13T03:17:00.123Z","url":"ftp://mirror.example.org/","host":"mirror.example.org","port":21,"status":"up","latency_ms":18.42,"error":null}
```

Example root listing (`reports/dir/mirror.example.org.json`):

```json
{
  "host": "mirror.example.org",
  "url": "ftp://mirror.example.org/",
  "port": 21,
  "scheme": "ftp",
  "checked_at": "2026-10-13T03:17:00.123Z",
  "tcp_latency_ms": 18.42,
  "started_at": "2026-10-13T03:17:00.900Z",
  "listed_at": "2026-10-13T03:17:01.002Z",
  "duration_ms": 102.4,
  "path": "/",
  "login": "anonymous",
  "status": "ok",
  "error": null,
  "listing_method": "mlsd",
  "timestamps_timezone": "utc",
  "unparsed_lines": 0,
  "server": {
    "welcome": "220 ProFTPD Server (mirror) [mirror.example.org]",
    "login_response": "Anonymous access granted, restrictions apply",
    "system": "UNIX Type: L8",
    "features": ["EPRT", "EPSV", "MDTM", "MLST", "SIZE", "UTF8"],
    "encoding": "utf-8"
  },
  "root": {
    "name": "/",
    "type": "directory",
    "size": 4096,
    "modified_at": "2026-10-13T03:00:00Z",
    "created_at": null,
    "accessed_at": null,
    "changed_at": null,
    "created_by": "ftpadmin",
    "group": "ftpgroup",
    "uid": 1000,
    "gid": 100,
    "permissions": "drwxr-xr-x",
    "mode": "0755",
    "perm": "flcdmpe",
    "unique_id": "816U0A9E9D5",
    "media_type": null,
    "link_target": null
  },
  "summary": {
    "entries": 2,
    "files": 1,
    "directories": 1,
    "links": 0,
    "other": 0,
    "file_bytes": 1024,
    "oldest_modified_at": "2026-09-30T10:10:10Z",
    "latest_modified_at": "2026-10-13T03:17:00Z"
  },
  "entries": [
    {
      "name": "pub",
      "type": "directory",
      "size": null,
      "modified_at": "2026-10-13T03:00:00Z",
      "created_at": "2024-01-01T00:00:00Z",
      "accessed_at": "2026-10-13T03:00:05Z",
      "changed_at": "2026-09-30T10:10:10Z",
      "created_by": "ftpadmin",
      "group": "ftpgroup",
      "uid": 1000,
      "gid": 100,
      "permissions": "drwxr-xr-x",
      "mode": "0755",
      "perm": "flcdmpe",
      "unique_id": "816U0A9E9D5",
      "media_type": null,
      "link_target": null
    },
    {
      "name": "README.txt",
      "type": "file",
      "size": 1024,
      "modified_at": "2026-10-13T03:17:00Z",
      "created_at": null,
      "accessed_at": null,
      "changed_at": null,
      "created_by": "ftpadmin",
      "group": "ftpgroup",
      "uid": 1000,
      "gid": 100,
      "permissions": "-rw-r--r--",
      "mode": "0644",
      "perm": "adfr",
      "unique_id": "816U0A9E9D6",
      "media_type": "text/plain",
      "link_target": null
    }
  ]
}
```

What each part of that file means:

| Field | Meaning |
| --- | --- |
| `checked_at`, `tcp_latency_ms` | The TCP reachability check from `reports/YYYY-MM-DD.jsonl` that this listing belongs to. |
| `started_at`, `listed_at`, `duration_ms` | When the FTP listing began, when it finished, and how long it took. |
| `path`, `login` | The listed directory (`/`, the root) and the login used (always anonymous). |
| `status`, `error` | `ok`, or `error` with the failure type and message when login or listing failed. |
| `listing_method` | `mlsd` (RFC 3659, best), `eplf`, or `list` (plain `LIST`) — which command produced the entries. |
| `timestamps_timezone` | `utc` when entry timestamps carry a `Z`, `server-local` when the server only reports wall-clock time. |
| `server` | Greeting, login reply, `SYST` type, `FEAT` feature names, and the encoding in use. |
| `root` | The root directory's own metadata, from the MLSD `cdir` row or `MLST`. |
| `unparsed_lines` | How many LIST lines no parser understood (`0` for MLSD and EPLF). |
| `summary` | Entry counts by type, total file bytes, oldest and newest modification times. |
| `entries` | One object per root entry, with the metadata fields listed above. |

Per-entry metadata comes from the server's facts: `modified_at` (`modify` or `MDTM`), `created_at` (`create`), `accessed_at` (`unix.atime`), `changed_at` (`unix.ctime`), `created_by` (`unix.owner`), `group` (`unix.group`), `uid`/`gid` (`unix.uid`/`unix.gid`), `mode` and `permissions` (`unix.mode`), `perm`, `unique_id` (`unique`), `media_type`, and `link_target`. Servers report different subsets, and plain `LIST` carries no creation times at all, so unreported values are `null`.

Example success report (`reports/YYYY-MM-DD-success.json`), containing only the reachable endpoints:

```json
{
  "date": "2026-10-13",
  "generated_at": "2026-10-13T03:17:05.480Z",
  "checked": 3,
  "up": 1,
  "servers": [
    {
      "checked_at": "2026-10-13T03:17:00.123Z",
      "url": "ftp://mirror.example.org/",
      "host": "mirror.example.org",
      "port": 21,
      "status": "up",
      "latency_ms": 18.42
    }
  ]
}
```

## Run locally

Requires Python 3.10+ and no third-party packages.

```bash
python scripts/ping_sites.py
```

To test with a local list rather than fetching the Gist:

```bash
python scripts/ping_sites.py --sites-file ./sites.txt
```

Optional flags:

```text
--timeout 5       TCP connect timeout per endpoint, in seconds
--workers 64      number of concurrent checks
--reports-dir ./reports
```

Run the tests with:

```bash
python -m unittest discover -s tests -v
```

## Explore the reports in a notebook

[`ftping_analysis.ipynb`](ftping_analysis.ipynb) loads everything under `reports/` into pandas and renders charts
for the output data: daily reachability, latency distributions, failure reasons, listing methods and login errors,
the largest FTP roots, the most common `FEAT` features, and a per-host drill-down. The rendered tables and images
stay embedded in the notebook on purpose — outputs are not cleared after a test run, so the committed `.ipynb`
doubles as the rendered report, viewable directly on GitHub.

It needs Python 3.10+ with `pandas`, `matplotlib` and `numpy` (plus `jupyter`/`ipykernel` to run it):

```bash
pip install pandas matplotlib numpy jupyter
jupyter lab ftping_analysis.ipynb
```

or re-execute it headlessly after a new daily report lands (this refreshes every table and figure in place):

```bash
jupyter nbconvert --to notebook --execute --inplace ftping_analysis.ipynb
```

The notebook finds `reports/` relative to its own location, so no paths need updating when new dates appear.

For scheduled runs, ensure GitHub Actions is enabled for the repository and that workflow runs are allowed to write repository contents (the workflow declares `permissions: contents: write`). Scheduled workflows start only after this workflow is present on the repository's default branch.
