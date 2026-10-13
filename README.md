# ftping

A daily reachability monitor for the FTP endpoints in the [ftp-sites.org list](https://gist.githubusercontent.com/mnjstwins/85ac8348d6faeb32b25908d447943300/raw/efd798cbf3649bb9681e3d1897492b61036d86e8/ftp-sites.org%2520list).

## What it does

- `.github/workflows/daily-ftp-ping.yml` runs every day at **03:17 UTC** and can also be started manually with **Actions → Daily FTP reachability → Run workflow**.
- `scripts/ping_sites.py` downloads the current list from the Gist, then checks every valid FTP endpoint concurrently by opening a TCP connection (normally port 21). This is a TCP reachability check, not an ICMP echo ping, which makes it suitable for GitHub-hosted runners.
- Each run appends one JSON object per endpoint to `reports/YYYY-MM-DD.jsonl`, using the UTC date. A rerun on the same day appends to that day's file; it never truncates reports or removes older dates.
- Each JSONL record includes the check timestamp, URL, host, port, `up`/`down` status, connection latency, and error details when a connection fails. A down site is recorded but does not fail the workflow.
- The workflow commits the report back to the branch on which it ran. It requests `contents: write` permission for this.

Example report line:

```json
{"checked_at":"2026-10-13T03:17:00.123Z","url":"ftp://mirror.example.org/","host":"mirror.example.org","port":21,"status":"up","latency_ms":18.42,"error":null}
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
