import json
import socket
import tempfile
import unittest
from datetime import date
from pathlib import Path

from scripts.ping_sites import Site, parse_site_list, probe_site, write_report


class ParseSiteListTests(unittest.TestCase):
    def test_parses_ftp_urls_and_bare_hosts(self) -> None:
        sites = parse_site_list(
            "\n"
            "# FTP mirrors\n"
            "ftp://mirror.example.org/\n"
            "ftp://files.example.org:2121/pub\n"
            "legacy.example.org/\n"
            "http://ignore.example.org/\n"
        )

        self.assertEqual(
            sites,
            [
                Site("ftp://mirror.example.org/", "mirror.example.org", 21),
                Site("ftp://files.example.org:2121/pub", "files.example.org", 2121),
                Site("ftp://legacy.example.org/", "legacy.example.org", 21),
            ],
        )

    def test_ignores_malformed_urls_and_embedded_credentials(self) -> None:
        sites = parse_site_list(
            "ftp://user:secret@example.org/\n"
            "ftp://example.org:not-a-port/\n"
            "ftp://example.org:70000/\n"
            "not a host\n"
        )
        self.assertEqual(sites, [])


class ProbeSiteTests(unittest.TestCase):
    def test_reports_an_open_tcp_endpoint_as_up(self) -> None:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            record = probe_site(
                Site(f"ftp://127.0.0.1:{port}/", "127.0.0.1", port), timeout=1
            )

        self.assertEqual(record["status"], "up")
        self.assertIsNone(record["error"])
        self.assertEqual(record["port"], port)
        self.assertGreaterEqual(record["latency_ms"], 0)
        self.assertTrue(str(record["checked_at"]).endswith("Z"))


class WriteReportTests(unittest.TestCase):
    def test_appends_valid_jsonl_without_replacing_existing_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            reports_dir = Path(temp_dir)
            report_date = date(2026, 10, 13)
            report_path = reports_dir / "2026-10-13.jsonl"
            older_record = {"status": "previous run"}
            report_path.write_text(json.dumps(older_record) + "\n", encoding="utf-8")
            prior_day_path = reports_dir / "2026-10-12.jsonl"
            prior_day_contents = '{"status":"older date"}\n'
            prior_day_path.write_text(prior_day_contents, encoding="utf-8")

            new_record = {
                "checked_at": "2026-10-13T03:17:00Z",
                "url": "ftp://mirror.example.org/",
                "host": "mirror.example.org",
                "port": 21,
                "status": "up",
                "latency_ms": 15.2,
                "error": None,
            }
            returned_path = write_report([new_record], reports_dir, report_date)

            self.assertEqual(returned_path, report_path)
            lines = report_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0]), older_record)
            self.assertEqual(json.loads(lines[1]), new_record)
            self.assertEqual(prior_day_path.read_text(encoding="utf-8"), prior_day_contents)

    def test_uses_utc_date_in_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = write_report([], Path(temp_dir), date(2026, 1, 2))
        self.assertEqual(report_path.name, "2026-01-02.jsonl")


if __name__ == "__main__":
    unittest.main()
