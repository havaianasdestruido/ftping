import ftplib
import json
import socket
import tempfile
import threading
import unittest
from datetime import date, datetime
from pathlib import Path

from unittest import mock

from scripts import ping_sites
from scripts.ping_sites import (
    Site,
    _entry_from_list_line,
    _entry_from_mlsd,
    _parse_dos_timestamp,
    _parse_mlsd_timestamp,
    _normalize_mode,
    _parse_unix_timestamp,
    _symbolic_mode,
    list_root_directory,
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


class MlsdEntryTests(unittest.TestCase):
    def test_maps_every_supported_fact_to_the_entry(self) -> None:
        entry = _entry_from_mlsd(
            "report.pdf",
            {
                "type": "file",
                "size": "1024",
                "modify": "20251013031700",
                "create": "20240101000000",
                "unix.atime": "20251013120000",
                "unix.ctime": "20250930101010",
                "perm": "adfr",
                "unique": "816U0A9E9D5",
                "media-type": "application/pdf",
                "unix.mode": "0644",
                "unix.uid": "1000",
                "unix.gid": "100",
                "unix.owner": "ftpadmin",
                "unix.group": "ftpgroup",
            },
        )

        self.assertEqual(entry["name"], "report.pdf")
        self.assertEqual(entry["type"], "file")
        self.assertEqual(entry["size"], 1024)
        self.assertEqual(entry["modified_at"], "2025-10-13T03:17:00Z")
        self.assertEqual(entry["created_at"], "2024-01-01T00:00:00Z")
        self.assertEqual(entry["accessed_at"], "2025-10-13T12:00:00Z")
        self.assertEqual(entry["changed_at"], "2025-09-30T10:10:10Z")
        self.assertEqual(entry["created_by"], "ftpadmin")
        self.assertEqual(entry["group"], "ftpgroup")
        self.assertEqual(entry["uid"], 1000)
        self.assertEqual(entry["gid"], 100)
        self.assertEqual(entry["mode"], "0644")
        self.assertEqual(entry["permissions"], "-rw-r--r--")
        self.assertEqual(entry["perm"], "adfr")
        self.assertEqual(entry["unique_id"], "816U0A9E9D5")
        self.assertEqual(entry["media_type"], "application/pdf")

    def test_directories_keep_their_type_and_permissions(self) -> None:
        entry = _entry_from_mlsd(
            "pub", {"type": "dir", "modify": "20251013031700", "unix.mode": "0755"}
        )
        self.assertEqual(entry["type"], "directory")
        self.assertEqual(entry["permissions"], "drwxr-xr-x")
        self.assertIsNone(entry["size"])

    def test_symlinks_use_the_proftpd_type_fact(self) -> None:
        entry = _entry_from_mlsd("latest", {"type": "OS.unix=slink:/Pub/2025"})
        self.assertEqual(entry["type"], "link")
        # The target is a path, so its case must survive the type lookup.
        self.assertEqual(entry["link_target"], "/Pub/2025")

    def test_symlink_detection_also_covers_plain_link_types(self) -> None:
        entry = _entry_from_mlsd("alias", {"type": "symlink"})
        self.assertEqual(entry["type"], "link")

    def test_skips_the_current_and_parent_directory_rows(self) -> None:
        self.assertIsNone(_entry_from_mlsd(".", {"type": "cdir"}))
        self.assertIsNone(_entry_from_mlsd("..", {"type": "pdir"}))

    def test_unknown_facts_leave_metadata_empty_instead_of_failing(self) -> None:
        entry = _entry_from_mlsd("mystery", {"type": "file", "size": "not-a-number"})
        self.assertIsNone(entry["size"])
        self.assertIsNone(entry["modified_at"])
        self.assertIsNone(entry["created_by"])


class UnixListLineTests(unittest.TestCase):
    def test_parses_owner_group_mode_and_relative_year(self) -> None:
        entry = _entry_from_list_line(
            "-rw-r--r--    1 ftpadmin ftpgroup      1024 Oct 13 13:09 my file.txt"
        )
        self.assertEqual(entry["name"], "my file.txt")
        self.assertEqual(entry["type"], "file")
        self.assertEqual(entry["size"], 1024)
        self.assertEqual(entry["created_by"], "ftpadmin")
        self.assertEqual(entry["group"], "ftpgroup")
        self.assertEqual(entry["permissions"], "-rw-r--r--")
        self.assertTrue(str(entry["modified_at"]).startswith("20"))

    def test_parses_absolute_year_and_directories(self) -> None:
        entry = _entry_from_list_line(
            "drwxr-xr-x    2 ftp      ftp          4096 Oct 13  2025 archives"
        )
        self.assertEqual(entry["type"], "directory")
        self.assertEqual(entry["modified_at"], "2025-10-13T00:00:00")

    def test_parses_symlinks_and_keeps_the_target(self) -> None:
        entry = _entry_from_list_line(
            "lrwxrwxrwx 1 ftp ftp 4 Jan 01 2020 latest -> pub/2025"
        )
        self.assertEqual(entry["type"], "link")
        self.assertEqual(entry["name"], "latest")
        self.assertEqual(entry["link_target"], "pub/2025")

    def test_ignores_total_lines_and_junk(self) -> None:
        self.assertIsNone(_entry_from_list_line("total 8"))
        self.assertIsNone(_entry_from_list_line("230 Logged in"))

    def test_zero_byte_files_keep_their_size(self) -> None:
        entry = _entry_from_list_line("-rw-r--r-- 1 ftp ftp 0 Aug 01 12:00 empty.txt")
        self.assertEqual(entry["size"], 0)


class DosListLineTests(unittest.TestCase):
    def test_parses_ms_dos_directories_files_and_links(self) -> None:
        directory = _entry_from_list_line("08-01-25  12:00PM       <DIR>          pub")
        self.assertEqual(directory["type"], "directory")
        self.assertEqual(directory["modified_at"], "2025-08-01T12:00:00")
        self.assertIsNone(directory["size"])

        entry = _entry_from_list_line("08-01-25  12:00AM                1024 readme.txt")
        self.assertEqual(entry["type"], "file")
        self.assertEqual(entry["size"], 1024)
        self.assertEqual(entry["modified_at"], "2025-08-01T00:00:00")

        link = _entry_from_list_line("08-01-25  01:30PM       <SYMLINK>      latest [pub\\2025]")
        self.assertEqual(link["type"], "link")
        self.assertEqual(link["name"], "latest")
        self.assertEqual(link["link_target"], "pub\\2025")


class EplfListLineTests(unittest.TestCase):
    def test_parses_eplf_facts_tab_and_name(self) -> None:
        entry = _entry_from_list_line("+i81,m824255902,r,s1024,\tfile.txt")
        self.assertEqual(entry["name"], "file.txt")
        self.assertEqual(entry["type"], "file")
        self.assertEqual(entry["size"], 1024)
        self.assertEqual(entry["unique_id"], "81")
        self.assertTrue(str(entry["modified_at"]).endswith("Z"))

    def test_parses_eplf_directories(self) -> None:
        entry = _entry_from_list_line("+i0,/,\tpub")
        self.assertEqual(entry["type"], "directory")


class TimestampParsingTests(unittest.TestCase):
    def test_mlsd_timestamps_are_utc(self) -> None:
        self.assertEqual(_parse_mlsd_timestamp("20251013031700"), "2025-10-13T03:17:00Z")
        self.assertEqual(_parse_mlsd_timestamp("2025"), None)
        self.assertEqual(_parse_mlsd_timestamp(None), None)
        self.assertEqual(_parse_mlsd_timestamp(""), None)
        self.assertEqual(_parse_mlsd_timestamp("20251313031700"), None)

    def test_dos_timestamps_use_a_four_digit_year(self) -> None:
        self.assertEqual(_parse_dos_timestamp("08-01-25 12:00PM"), "2025-08-01T12:00:00")
        self.assertEqual(_parse_dos_timestamp("08-01-1998 12:00AM"), "1998-08-01T00:00:00")
        self.assertEqual(_parse_dos_timestamp("garbage"), None)

    def test_unix_timestamps_assume_the_current_year_unless_it_is_ahead(self) -> None:
        now = datetime(2026, 10, 13, 3, 17)
        self.assertEqual(
            _parse_unix_timestamp("Oct 01 13:09", now), "2026-10-01T13:09:00"
        )
        # A date only a day or two ahead is clock skew, not next year.
        self.assertEqual(
            _parse_unix_timestamp("Oct 14 03:00", now), "2026-10-14T03:00:00"
        )
        # Anything further ahead must belong to the previous year.
        self.assertEqual(
            _parse_unix_timestamp("Dec 31 23:59", now), "2025-12-31T23:59:00"
        )


class SymbolicModeTests(unittest.TestCase):
    def test_renders_octal_modes_as_ls_style_permissions(self) -> None:
        self.assertEqual(_symbolic_mode("0644", "file"), "-rw-r--r--")
        self.assertEqual(_symbolic_mode("0755", "directory"), "drwxr-xr-x")
        self.assertEqual(_symbolic_mode("0777", "link"), "lrwxrwxrwx")
        self.assertEqual(_symbolic_mode("4755", "file"), "-rwsr-xr-x")
        self.assertEqual(_symbolic_mode("2775", "directory"), "drwxrwsr-x")
        self.assertEqual(_symbolic_mode("1777", "directory"), "drwxrwxrwt")
        self.assertIsNone(_symbolic_mode("not-a-mode", "file"))
        self.assertIsNone(_symbolic_mode(None, "file"))

    def test_normalizes_the_octal_forms_servers_report(self) -> None:
        # pyftpdlib reports "0o755", other servers report three digits.
        self.assertEqual(_normalize_mode("0o755"), "0755")
        self.assertEqual(_normalize_mode("755"), "0755")
        self.assertEqual(_normalize_mode("0755"), "0755")
        self.assertEqual(_normalize_mode("4755"), "4755")
        self.assertIsNone(_normalize_mode(""))
        self.assertIsNone(_normalize_mode(None))


def _fake_mlsd_listing() -> list[str]:
    return [
        "type=file;size=1024;modify=20251013031700;create=20240101000000;"
        "perm=adfr;unique=816U0A9E9D5;unix.mode=0644;unix.uid=1000;unix.gid=100;"
        "unix.owner=ftpadmin;unix.group=ftpgroup; README.txt",
        "type=dir;size=4096;modify=20250930101010;unix.mode=0755;unix.uid=1000;"
        "unix.gid=100;unix.owner=ftpadmin;unix.group=ftpgroup; pub",
    ]


def _fake_list_listing() -> list[str]:
    return [
        "total 8",
        "drwxr-xr-x    2 ftpadmin ftpgroup     4096 Sep 30 10:10 pub",
        "-rw-r--r--    1 ftpadmin ftpgroup     1024 Oct 13 03:17 README.txt",
    ]


class FakeFtpServer:
    """A one-connection FTP server that answers just enough for these checks."""

    def __init__(self, listing_lines: list[str], support_mlsd: bool = True) -> None:
        self.listing_lines = listing_lines
        self.support_mlsd = support_mlsd
        self.commands: list[str] = []
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(5)
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve_politely, daemon=True)

    def __enter__(self) -> "FakeFtpServer":
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._thread.join(timeout=10)
        self._listener.close()

    def _serve_politely(self) -> None:
        try:
            connection, _ = self._listener.accept()
        except OSError:
            return
        with connection:
            connection.settimeout(10)
            self._control_loop(connection)

    def _control_loop(self, connection: socket.socket) -> None:
        connection.sendall(b"220-fake FTP server ready\r\n220 welcome to the fake server\r\n")
        data_listener: socket.socket | None = None
        buffer = b""
        while True:
            try:
                while b"\r\n" not in buffer:
                    chunk = connection.recv(4096)
                    if not chunk:
                        return
                    buffer += chunk
            except OSError:
                return
            line, _, buffer = buffer.partition(b"\r\n")
            command = line.decode("utf-8", "replace")
            self.commands.append(command)
            verb = command.split(" ", 1)[0].upper()
            argument = command.split(" ", 1)[1] if " " in command else ""

            if verb == "USER":
                connection.sendall(b"331 Anonymous login ok, send your email address.\r\n")
            elif verb == "PASS":
                connection.sendall(b"230 Anonymous access granted.\r\n")
            elif verb == "SYST":
                connection.sendall(b"215 UNIX Type: L8\r\n")
            elif verb == "FEAT":
                connection.sendall(
                    b"211-Features:\r\n UTF8\r\n MLST type*;size*;modify*;create*;\r\n211 End\r\n"
                )
            elif verb == "OPTS":
                if not self.support_mlsd and argument.upper().startswith("MLST"):
                    connection.sendall(b"502 MLST not supported.\r\n")
                else:
                    connection.sendall(b"200 Oki Doki.\r\n")
            elif verb == "TYPE":
                connection.sendall(b"200 Type set.\r\n")
            elif verb == "PASV":
                if data_listener is not None:
                    data_listener.close()
                data_listener = socket.socket()
                data_listener.bind(("127.0.0.1", 0))
                data_listener.listen(1)
                data_port = data_listener.getsockname()[1]
                numbers = ",".join(str(part) for part in (127, 0, 0, 1, data_port >> 8, data_port & 0xFF))
                connection.sendall(f"227 Entering Passive Mode ({numbers})\r\n".encode("ascii"))
            elif verb in {"MLSD", "LIST"}:
                self._send_listing(connection, data_listener, verb)
            elif verb == "MLST":
                connection.sendall(
                    b"250-Listing /\r\n"
                    b" type=dir;size=4096;modify=20251013031700;unix.mode=0755;"
                    b"unix.owner=ftpadmin;unix.group=ftpgroup; /\r\n"
                    b"250 End\r\n"
                )
            elif verb == "QUIT":
                connection.sendall(b"221 Goodbye.\r\n")
                return
            else:
                connection.sendall(b"502 Command not implemented.\r\n")

    def _send_listing(
        self,
        connection: socket.socket,
        data_listener: socket.socket | None,
        verb: str,
    ) -> None:
        if verb == "MLSD" and not self.support_mlsd:
            connection.sendall(b"502 MLSD not implemented.\r\n")
            return
        if data_listener is None:
            connection.sendall(b"425 Use PASV first.\r\n")
            return
        connection.sendall(b"150 Here comes the directory listing.\r\n")
        payload = "".join(f"{line}\r\n" for line in self.listing_lines).encode("utf-8")
        try:
            data_connection, _ = data_listener.accept()
            with data_connection:
                data_connection.sendall(payload)
        finally:
            data_listener.close()
        connection.sendall(b"226 Transfer complete.\r\n")


class ListRootDirectoryTests(unittest.TestCase):
    """End-to-end checks against a real FTP conversation on localhost."""

    def test_mlsd_server_reports_server_facts_and_entry_metadata(self) -> None:
        with FakeFtpServer(_fake_mlsd_listing()) as server:
            listing = list_root_directory("127.0.0.1", server.port, timeout=5)

        self.assertEqual(listing["listing_method"], "mlsd")
        self.assertEqual(listing["timestamps_timezone"], "utc")
        self.assertEqual(listing["server"]["system"], "UNIX Type: L8")
        self.assertEqual(listing["server"]["encoding"], "utf-8")
        self.assertTrue(str(listing["server"]["welcome"]).startswith("220"))
        self.assertIn("MLST", listing["server"]["features"])
        self.assertEqual(listing["root"]["type"], "directory")
        self.assertEqual(listing["root"]["modified_at"], "2025-10-13T03:17:00Z")

        readme, pub = listing["entries"]
        self.assertEqual(readme["name"], "README.txt")
        self.assertEqual(readme["size"], 1024)
        self.assertEqual(readme["created_at"], "2024-01-01T00:00:00Z")
        self.assertEqual(readme["created_by"], "ftpadmin")
        self.assertEqual(readme["group"], "ftpgroup")
        self.assertEqual(readme["uid"], 1000)
        self.assertEqual(readme["gid"], 100)
        self.assertEqual(readme["mode"], "0644")
        self.assertEqual(readme["permissions"], "-rw-r--r--")
        self.assertEqual(readme["perm"], "adfr")
        self.assertEqual(pub["mode"], "0755")
        self.assertEqual(pub["type"], "directory")
        self.assertEqual(pub["permissions"], "drwxr-xr-x")
        self.assertEqual(pub["modified_at"], "2025-09-30T10:10:10Z")

    def test_server_without_mlsd_falls_back_to_list(self) -> None:
        with FakeFtpServer(_fake_list_listing(), support_mlsd=False) as server:
            listing = list_root_directory("127.0.0.1", server.port, timeout=5)

        self.assertEqual(listing["listing_method"], "list")
        self.assertEqual(listing["timestamps_timezone"], "server-local")
        self.assertEqual(listing["root"]["type"], "directory")
        self.assertEqual(listing["root"]["mode"], "0755")
        self.assertEqual(listing["root"]["created_by"], "ftpadmin")
        self.assertEqual(listing["root"]["permissions"], "drwxr-xr-x")

        pub, readme = listing["entries"]
        self.assertEqual(pub["type"], "directory")
        self.assertEqual(pub["permissions"], "drwxr-xr-x")
        self.assertEqual(readme["created_by"], "ftpadmin")
        self.assertEqual(readme["group"], "ftpgroup")
        self.assertEqual(readme["permissions"], "-rw-r--r--")
        self.assertEqual(readme["size"], 1024)
        self.assertEqual(listing["unparsed_lines"], 0)

    def test_unparseable_list_output_is_counted_instead_of_dropped(self) -> None:
        junk = [
            "Directory PUB$:[000000]",
            "README.TXT;1             6  13-OCT-2026 03:17",
        ]
        with FakeFtpServer(junk, support_mlsd=False) as server:
            listing = list_root_directory("127.0.0.1", server.port, timeout=5)

        self.assertEqual(listing["listing_method"], "list")
        self.assertEqual(listing["entries"], [])
        self.assertEqual(listing["unparsed_lines"], 2)


class DirectoryListingDocumentTests(unittest.TestCase):
    def test_lists_only_up_hosts_once_each_under_dir(self) -> None:
        records = [
            record("ftp://mirror.example.org/", "mirror.example.org", "up"),
            record("ftp://mirror.example.org/pub", "mirror.example.org", "up"),
            record("ftp://dead.example.org/", "dead.example.org", "down"),
        ]
        entries = [
            {
                "name": "pub",
                "type": "directory",
                "size": None,
                "modified_at": "2025-09-30T10:10:10Z",
                "created_at": None,
                "accessed_at": None,
                "changed_at": None,
                "created_by": "ftpadmin",
                "group": "ftpgroup",
                "uid": 1000,
                "gid": 100,
                "permissions": "drwxr-xr-x",
                "mode": "0755",
                "perm": None,
                "unique_id": None,
                "media_type": None,
                "link_target": None,
            },
            {
                "name": "README.txt",
                "type": "file",
                "size": 1024,
                "modified_at": "2025-10-13T03:17:00Z",
                "created_at": None,
                "accessed_at": None,
                "changed_at": None,
                "created_by": None,
                "group": None,
                "uid": None,
                "gid": None,
                "permissions": "-rw-r--r--",
                "mode": None,
                "perm": None,
                "unique_id": None,
                "media_type": None,
                "link_target": None,
            },
        ]
        listing_result = {
            "listing_method": "mlsd",
            "timestamps_timezone": "utc",
            "unparsed_lines": 0,
            "server": {
                "welcome": "220 fake",
                "login_response": "Anonymous access granted.",
                "system": "UNIX Type: L8",
                "features": ["UTF8", "MLST"],
                "encoding": "utf-8",
            },
            "root": ping_sites._new_entry("/", "directory"),
            "entries": entries,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            reports_dir = Path(temp_dir)
            with mock.patch.object(
                ping_sites, "list_root_directory", return_value=listing_result
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
        self.assertEqual(document["login"], "anonymous")
        self.assertEqual(document["listing_method"], "mlsd")
        self.assertEqual(document["timestamps_timezone"], "utc")
        self.assertEqual(document["unparsed_lines"], 0)
        self.assertEqual(document["tcp_latency_ms"], 12.5)
        self.assertEqual(document["checked_at"], "2026-10-13T03:17:00.000Z")
        self.assertEqual(document["server"]["system"], "UNIX Type: L8")
        self.assertEqual(document["root"]["type"], "directory")
        self.assertEqual(document["entries"], entries)
        self.assertEqual(
            document["summary"],
            {
                "entries": 2,
                "files": 1,
                "directories": 1,
                "links": 0,
                "other": 0,
                "file_bytes": 1024,
                "oldest_modified_at": "2025-09-30T10:10:10Z",
                "latest_modified_at": "2025-10-13T03:17:00Z",
            },
        )
        self.assertTrue(str(document["started_at"]).endswith("Z"))
        self.assertTrue(str(document["listed_at"]).endswith("Z"))
        self.assertGreaterEqual(document["duration_ms"], 0)

    def test_unparseable_list_lines_are_counted(self) -> None:
        records = [record("ftp://vms.example.org/", "vms.example.org", "up")]
        listing_result = {
            "listing_method": "list",
            "timestamps_timezone": "server-local",
            "unparsed_lines": 2,
            "server": ping_sites._empty_server_metadata(),
            "root": ping_sites._new_entry("/", "directory"),
            "entries": [],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            reports_dir = Path(temp_dir)
            with mock.patch.object(
                ping_sites, "list_root_directory", return_value=listing_result
            ):
                write_directory_listings(records, reports_dir)
            document = json.loads((reports_dir / "dir" / "vms.example.org.json").read_text("utf-8"))

        self.assertEqual(document["listing_method"], "list")
        self.assertEqual(document["unparsed_lines"], 2)
        self.assertEqual(document["entries"], [])

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
        self.assertEqual(document["summary"]["entries"], 0)
        self.assertIsNone(document["server"]["system"])


if __name__ == "__main__":
    unittest.main()
