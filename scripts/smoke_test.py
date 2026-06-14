#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import http.cookiejar
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ADMIN_EMAIL = "admin@example.local"
ADMIN_PASSWORD = "change-me-now"


def password_hash(password: str) -> str:
    salt = secrets.token_hex(16)
    iterations = 260_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
    return f"{iterations}${salt}${digest.hex()}"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def seed_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              email TEXT NOT NULL UNIQUE,
              password_hash TEXT NOT NULL,
              display_name TEXT NOT NULL,
              role TEXT NOT NULL CHECK (role IN ('admin', 'member')),
              active INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL
            );
            CREATE TABLE peptides (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              notes TEXT,
              color TEXT NOT NULL DEFAULT '#60706a',
              created_at TEXT NOT NULL
            );
            CREATE TABLE dose_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              enrollment_id INTEGER,
              protocol_id INTEGER,
              protocol_step_id INTEGER,
              protocol_day INTEGER,
              source TEXT NOT NULL CHECK (source IN ('protocol', 'manual')),
              peptide_name TEXT NOT NULL,
              scheduled_dose_amount REAL,
              actual_dose_amount REAL NOT NULL,
              dose_unit TEXT NOT NULL DEFAULT 'mg',
              status TEXT NOT NULL CHECK (status IN ('completed', 'skipped')),
              site TEXT,
              notes TEXT,
              logged_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO users (email, password_hash, display_name, role, active, created_at) VALUES (?, ?, 'Admin', 'admin', 1, '2026-06-14T08:00:00')",
            (ADMIN_EMAIL, password_hash(ADMIN_PASSWORD)),
        )
        conn.execute(
            "INSERT INTO users (email, password_hash, display_name, role, active, created_at) VALUES (?, ?, 'Member', 'member', 1, '2026-06-14T08:00:00')",
            ("member@example.local", password_hash("member-password")),
        )
        conn.executemany(
            "INSERT INTO peptides (name, notes, color, created_at) VALUES (?, '', '#60706a', '2026-06-14T08:00:00')",
            [("SS-31",), ("Retatrutide",)],
        )
        conn.executemany(
            """
            INSERT INTO dose_logs
              (user_id, source, peptide_name, actual_dose_amount, dose_unit, status, site, notes, logged_at)
            VALUES (?, 'manual', ?, ?, 'mg', 'completed', '', '', ?)
            """,
            [
                (1, "SS-31", 1.0, "2026-06-13T08:00:00"),
                (2, "SS-31", 1.0, "2026-06-13T08:00:00"),
                (2, "Retatrutide", 2.0, "2026-06-13T08:00:00"),
            ],
        )


class Client:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def get(self, path: str) -> str:
        with self.opener.open(f"{self.base_url}{path}", timeout=10) as response:
            return response.read().decode("utf-8")

    def post(self, path: str, data: dict[str, str]) -> str:
        encoded = urllib.parse.urlencode(data).encode("utf-8")
        request = urllib.request.Request(f"{self.base_url}{path}", data=encoded, method="POST")
        with self.opener.open(request, timeout=10) as response:
            return response.read().decode("utf-8")


def require(text: str, needle: str) -> None:
    if needle not in text:
        raise AssertionError(f"missing expected text: {needle}")


def wait_for_server(client: Client, proc: subprocess.Popen[str]) -> None:
    deadline = time.time() + 15
    last_error: Exception | None = None
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early with code {proc.returncode}")
        try:
            if client.get("/healthz").strip() == "ok":
                return
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
            time.sleep(0.2)
    raise RuntimeError(f"server did not become healthy: {last_error}")


def main() -> int:
    port = free_port()
    db_path = Path(tempfile.gettempdir()) / f"peptide-inventory-smoke-{port}.db"
    db_path.unlink(missing_ok=True)
    seed_db(db_path)
    env = {
        **os.environ,
        "INVENTORY_HOST": "127.0.0.1",
        "INVENTORY_PORT": str(port),
        "INVENTORY_DB": str(db_path),
        "INVENTORY_SECRET": "smoke-test-secret",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.server"],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    client = Client(f"http://127.0.0.1:{port}")
    try:
        wait_for_server(client, proc)
        login = client.post("/login", {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        require(login, "Inventory")
        require(login, "SS-31")
        require(login, "tracked peptides")

        inventory = client.get("/inventory")
        require(inventory, "Add inventory")
        require(inventory, "Retatrutide")
        require(inventory, "Supplier code")
        require(inventory, "SK10")

        client.post(
            "/lots",
            {
                "peptide_name": "SS-31",
                "peptide_name_other": "",
                "vial_count": "3",
                "mg_per_vial": "10",
                "added_at": "2026-06-14",
                "notes": "smoke lot",
            },
        )
        home = client.get("/")
        require(home, "30 mg on hand")
        require(home, "Vials on hand")
        require(home, "3")
        require(home, "Vials used")
        require(home, "0 forecast dose logs")
        require(home, "Logged MG")
        require(home, "Total MG")

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO dose_logs
                  (user_id, source, peptide_name, actual_dose_amount, dose_unit, status, site, notes, logged_at)
                VALUES (1, 'manual', 'SS-31', 1.0, 'mg', 'completed', '', '', '2999-01-01T08:00:00')
                """
            )
        home = client.get("/")
        require(home, "30 mg on hand")
        require(home, "1 forecast dose logs")
        require(home, "1 mg")

        with sqlite3.connect(db_path) as conn:
            lot_id = conn.execute("SELECT id FROM inventory_lots WHERE peptide_name = 'SS-31' ORDER BY id DESC LIMIT 1").fetchone()[0]
        client.post("/lots/use", {"lot_id": str(lot_id)})
        home = client.get("/")
        require(home, "20 mg on hand")
        require(home, "2")
        require(home, "Vials used")

        client.post("/lots/restore", {"lot_id": str(lot_id)})
        home = client.get("/")
        require(home, "30 mg on hand")

        client.post(
            "/lots",
            {
                "peptide_name": "",
                "peptide_name_other": "New-Test-Peptide",
                "vial_count": "1",
                "mg_per_vial": "5",
                "added_at": "2026-06-14",
                "notes": "new peptide",
            },
        )
        with sqlite3.connect(db_path) as conn:
            row = conn.execute("SELECT name FROM peptides WHERE name = 'New-Test-Peptide'").fetchone()
            if not row:
                raise AssertionError("new peptide was not added to shared peptide catalog")

        client.post(
            "/lots",
            {
                "supplier_code": "SK10",
                "peptide_name": "",
                "peptide_name_other": "",
                "vial_count": "2",
                "mg_per_vial": "",
                "added_at": "2026-06-14",
                "notes": "supplier code smoke",
            },
        )
        with sqlite3.connect(db_path) as conn:
            row = conn.execute("SELECT name FROM peptides WHERE name = 'Selank'").fetchone()
            if not row:
                raise AssertionError("supplier code peptide was not added to shared peptide catalog")
            lot = conn.execute("SELECT peptide_name, vial_count, mg_per_vial, notes FROM inventory_lots WHERE peptide_name = 'Selank'").fetchone()
            if not lot or lot[1] != 2 or lot[2] != 10:
                raise AssertionError("supplier code did not create the expected Selank lot")

        member = Client(f"http://127.0.0.1:{port}")
        try:
            member.post("/login", {"email": "member@example.local", "password": "member-password"})
        except urllib.error.HTTPError as exc:
            if exc.code != 403:
                raise
        else:
            raise AssertionError("member was allowed to login")

        print(f"Smoke test passed at http://127.0.0.1:{port}")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        db_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
