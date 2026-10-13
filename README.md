# ftping

A daily reachability monitor for the FTP endpoints in the [ftp-sites.org list](https://gist.githubusercontent.com/mnjstwins/85ac8348d6faeb32b25908d447943300/raw/efd798cbf3649bb9681e3d1897492b61036d86e8/ftp-sites.org%2520list).

## What it does

- `.github/workflows/daily-ftp-ping.yml` runs every day at **03:17 UTC** and can also be started manually with **Actions → Daily FTP reachability → Run workflow**.
- `scripts/ping_sites.py` downloads the current list from the Gist, then checks every valid FTP endpoint concurrently by opening a TCP connection (normally port 21). This is a TCP reachability check, not an ICMP echo ping, which makes it suitable for GitHub-hosted runners.
- Each run appends one JSON object per endpoint to `reports/YYYY-MM-DD.jsonl`, using the UTC date. A rerun on the same day appends to that day's file; it never truncates reports or removes older dates.
- Each run also writes `reports/YYYY-MM-DD-success.json`, which lists **only the endpoints that answered** (status `up`) for that date. Reruns never overwrite an existing report: if the day's success file is already there, the new report is written next to it as `reports/YYYY-MM-DD-success-1.json`, then `-2`, and so on, so every run's results are kept.
- Each JSONL record includes the check timestamp, URL, host, port, `up`/`down` status, connection latency, and error details when a connection fails. A down site is recorded but does not fail the workflow.
- For every endpoint in `reports/YYYY-MM-DD-success.json`, the run also logs in anonymously over FTP, lists the server's **root directory**, and writes `reports/dir/<host>.json` (for example `reports/dir/ftp.website.org.json`). Each file lists the root files and folders with their type and size. Only `up` endpoints are listed, each host once. A failed login or listing is recorded in the file's `status`/`error` fields and does not fail the workflow. Each file is replaced on every run, so older snapshots are kept in Git history.
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
  "checked_at": "2026-10-13T03:17:00.123Z",
  "listed_at": "2026-10-13T03:17:01.002Z",
  "path": "/",
  "login": "anonymous",
  "status": "ok",
  "error": null,
  "entries": [
    {"name": "pub", "type": "directory", "size": null},
    {"name": "README.txt", "type": "file", "size": 1024}
  ]
}
```

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

For scheduled runs, ensure GitHub Actions is enabled for the repository and that workflow runs are allowed to write repository contents (the workflow declares `permissions: contents: write`). Scheduled workflows start only after this workflow is present on the repository's default branch.
