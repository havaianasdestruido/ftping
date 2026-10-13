import ftplib
import json
import socket
import tempfile
import unittest
from datetime import date
from pathlib import Path

from unittest import mock

from scripts import ping_sites
from scripts.ping_sites import (
    Site,
    _entry_from_list_line,
    _entry_from_mlsd,
    parse_site_list,
    probe_site,
    write_directory_listings,
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


class ListingParsingTests(unittest.TestCase):
    def test_mlsd_facts_map_to_entry_types(self) -> None:
        self.assertEqual(
            _entry_from_mlsd("pub", {"type": "dir"}),
            {"name": "pub", "type": "directory", "size": None},
        )
        self.assertEqual(
            _entry_from_mlsd("readme.txt", {"type": "file", "size": "120"}),
            {"name": "readme.txt", "type": "file", "size": 120},
        )
        self.assertIsNone(_entry_from_mlsd(".", {"type": "cdir"}))
        self.assertIsNone(_entry_from_mlsd("..", {"type": "pdir"}))

    def test_unix_list_lines_are_parsed(self) -> None:
        self.assertEqual(
            _entry_from_list_line(
                "drwxr-xr-x    2 ftp  ftp  4096 Oct 13 13:09 my dir"
            ),
            {"name": "my dir", "type": "directory", "size": 4096},
        )
        self.assertEqual(
            _entry_from_list_line("-rw-r--r--    1 ftp  ftp  10 Jan 01 2020 a.txt"),
            {"name": "a.txt", "type": "file", "size": 10},
        )
        self.assertEqual(
            _entry_from_list_line("lrwxrwxrwx 1 ftp ftp 4 Jan 01 2020 link -> target"),
            {"name": "link", "type": "link", "size": 4},
        )
        self.assertIsNone(_entry_from_list_line("total 8"))


class WriteDirectoryListingsTests(unittest.TestCase):
    def test_lists_only_up_hosts_once_each_under_dir(self) -> None:
        records = [
            record("ftp://mirror.example.org/", "mirror.example.org", "up"),
            record("ftp://mirror.example.org/pub", "mirror.example.org", "up"),
            record("ftp://dead.example.org/", "dead.example.org", "down"),
        ]
        entries = [{"name": "pub", "type": "directory", "size": None}]
        with tempfile.TemporaryDirectory() as temp_dir:
            reports_dir = Path(temp_dir)
            with mock.patch.object(
                ping_sites, "list_root_directory", return_value=entries
            ) as listing:
                paths = write_directory_listings(records, reports_dir)
            files = sorted(p.relative_to(reports_dir).as_posix() for p in reports_dir.rglob("*") if p.is_file())
            document = json.loads((reports_dir / "dir" / "mirror.example.org.json").read_text("utf-8"))

        self.assertEqual(listing.call_count, 1)
        self.assertEqual(files, ["dir/mirror.example.org.json"])
        self.assertEqual([p.name for p in paths], ["mirror.example.org.json"])
        self.assertEqual(document["status"], "ok")
        self.assertEqual(document["url"], "ftp://mirror.example.org/")
        self.assertEqual(document["path"], "/")
        self.assertEqual(document["entries"], entries)

    def test_ftp_failure_is_recorded_without_failing(self) -> None:
        records = [record("ftp://locked.example.org/", "locked.example.org", "up")]
        with tempfile.TemporaryDirectory() as temp_dir:
            reports_dir = Path(temp_dir)
            with mock.patch.object(
                ping_sites,
                "list_root_directory",
                side_effect=ftplib.error_perm("530 Login incorrect."),
            ):
                write_directory_listings(records, reports_dir)
            document = json.loads((reports_dir / "dir" / "locked.example.org.json").read_text("utf-8"))

        self.assertEqual(document["status"], "error")
        self.assertEqual(document["error"]["type"], "error_perm")
        self.assertEqual(document["entries"], [])


if __name__ == "__main__":
    unittest.main()
