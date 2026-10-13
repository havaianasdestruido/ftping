import json
import socket
import tempfile
import unittest
from datetime import date
from pathlib import Path

from scripts.ping_sites import (
    Site,
    parse_site_list,
    probe_site,
    write_report,
    write_success_report,
)


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


def record(url: str, host: str, status: str) -> dict[str, object]:
    return {
        "checked_at": "2026-10-13T03:17:00.000Z",
        "url": url,
        "host": host,
        "port": 21,
        "status": status,
        "latency_ms": 12.5,
        "error": None if status == "up" else {"type": "TimeoutError", "message": "timed out"},
    }


class WriteSuccessReportTests(unittest.TestCase):
    def test_writes_only_up_servers_with_date_and_success_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            reports_dir = Path(temp_dir)

            success_path = write_success_report(
                [
                    record("ftp://mirror.example.org/", "mirror.example.org", "up"),
                    record("ftp://dead.example.org/", "dead.example.org", "down"),
                ],
                reports_dir,
                date(2026, 10, 13),
            )

            self.assertEqual(success_path.name, "2026-10-13-success.json")
            document = json.loads(success_path.read_text(encoding="utf-8"))

        self.assertEqual(document["date"], "2026-10-13")
        self.assertEqual(document["checked"], 2)
        self.assertEqual(document["up"], 1)
        self.assertEqual(
            document["servers"],
            [
                {
                    "checked_at": "2026-10-13T03:17:00.000Z",
                    "url": "ftp://mirror.example.org/",
                    "host": "mirror.example.org",
                    "port": 21,
                    "status": "up",
                    "latency_ms": 12.5,
                }
            ],
        )
        self.assertTrue(str(document["generated_at"]).endswith("Z"))

    def test_writes_an_empty_server_list_when_nothing_is_up(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            reports_dir = Path(temp_dir)
            success_path = write_success_report(
                [record("ftp://dead.example.org/", "dead.example.org", "down")],
                reports_dir,
                date(2026, 10, 13),
            )
            document = json.loads(success_path.read_text("utf-8"))
            leftovers = sorted(path.name for path in reports_dir.iterdir())

        self.assertEqual(success_path.name, "2026-10-13-success.json")
        self.assertEqual(document["up"], 0)
        self.assertEqual(document["servers"], [])
        self.assertEqual(leftovers, ["2026-10-13-success.json"])

    def test_reruns_get_numbered_names_without_overwriting_earlier_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            reports_dir = Path(temp_dir)
            first_path = write_success_report(
                [record("ftp://old.example.org/", "old.example.org", "up")],
                reports_dir,
                date(2026, 10, 13),
            )
            prior_day = reports_dir / "2026-10-12-success.json"
            prior_day.write_text('{"servers": []}\n', encoding="utf-8")

            second_path = write_success_report(
                [record("ftp://new-a.example.org/", "new-a.example.org", "up")],
                reports_dir,
                date(2026, 10, 13),
            )
            third_path = write_success_report(
                [record("ftp://new-b.example.org/", "new-b.example.org", "up")],
                reports_dir,
                date(2026, 10, 13),
            )

            first_urls = [
                server["url"]
                for server in json.loads(first_path.read_text("utf-8"))["servers"]
            ]
            second_urls = [
                server["url"]
                for server in json.loads(second_path.read_text("utf-8"))["servers"]
            ]
            third_urls = [
                server["url"]
                for server in json.loads(third_path.read_text("utf-8"))["servers"]
            ]
            prior_day_contents = prior_day.read_text(encoding="utf-8")
            leftovers = sorted(path.name for path in reports_dir.iterdir())

        self.assertEqual(first_path.name, "2026-10-13-success.json")
        self.assertEqual(second_path.name, "2026-10-13-success-1.json")
        self.assertEqual(third_path.name, "2026-10-13-success-2.json")
        self.assertEqual(first_urls, ["ftp://old.example.org/"])
        self.assertEqual(second_urls, ["ftp://new-a.example.org/"])
        self.assertEqual(third_urls, ["ftp://new-b.example.org/"])
        self.assertEqual(prior_day_contents, '{"servers": []}\n')
        self.assertEqual(
            leftovers,
            [
                "2026-10-12-success.json",
                "2026-10-13-success-1.json",
                "2026-10-13-success-2.json",
                "2026-10-13-success.json",
            ],
        )

    def test_reuses_a_numbered_name_when_the_file_is_gone(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            reports_dir = Path(temp_dir)
            write_success_report([], reports_dir, date(2026, 10, 13))
            second_path = write_success_report([], reports_dir, date(2026, 10, 13))
            second_path.unlink()

            third_path = write_success_report([], reports_dir, date(2026, 10, 13))

        self.assertEqual(second_path.name, "2026-10-13-success-1.json")
        self.assertEqual(third_path.name, "2026-10-13-success-1.json")


if __name__ == "__main__":
    unittest.main()
