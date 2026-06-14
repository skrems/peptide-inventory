from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import sqlite3
import sys
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.supplier_codes import SUPPLIER_CODES, supplier_lookup


APP_NAME = "Peptide Inventory"
APP_VERSION = "v0.1"
ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
DB_PATH = Path(os.environ.get("INVENTORY_DB", ROOT / "data" / "app.db"))
HOST = os.environ.get("INVENTORY_HOST", "127.0.0.1")
PORT = int(os.environ.get("INVENTORY_PORT", "8081"))
SECRET = os.environ.get("INVENTORY_SECRET", secrets.token_hex(32))
APP_TIMEZONE_NAME = os.environ.get("INVENTORY_TIMEZONE", os.environ.get("TZ", "America/Los_Angeles"))

SESSIONS: dict[str, int] = {}


@dataclass
class RequestContext:
    user: sqlite3.Row | None
    flash: str | None = None
    error: str | None = None


def app_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(APP_TIMEZONE_NAME)
    except ZoneInfoNotFoundError:
        return ZoneInfo("America/Los_Angeles")


def now_iso() -> str:
    return datetime.now(app_timezone()).replace(tzinfo=None, microsecond=0).isoformat()


def today_stamp() -> str:
    return datetime.now(app_timezone()).strftime("%Y%m%d-%H%M%S")


def h(value: Any) -> str:
    return escape("" if value is None else str(value), quote=True)


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def verify_password(password: str, stored: str) -> bool:
    try:
        iterations_s, salt, expected = stored.split("$", 2)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations_s))
        return hmac.compare_digest(digest.hex(), expected)
    except Exception:
        return False


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS inventory_lots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              peptide_name TEXT NOT NULL,
              vial_count REAL NOT NULL,
              mg_per_vial REAL NOT NULL,
              added_at TEXT NOT NULL,
              notes TEXT,
              created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS inventory_adjustments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              peptide_name TEXT NOT NULL,
              amount_mg REAL NOT NULL,
              reason TEXT NOT NULL,
              notes TEXT,
              created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
              created_at TEXT NOT NULL
            );
            """
        )


def query(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, args).fetchall()


def one(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    return conn.execute(sql, args).fetchone()


def parse_cookies(raw: str | None) -> dict[str, str]:
    cookies: dict[str, str] = {}
    if not raw:
        return cookies
    for chunk in raw.split(";"):
        if "=" in chunk:
            key, value = chunk.split("=", 1)
            cookies[key.strip()] = value.strip()
    return cookies


def sign(value: str) -> str:
    digest = hmac.new(SECRET.encode(), value.encode(), "sha256").hexdigest()
    return f"{value}.{digest}"


def unsign(value: str) -> str | None:
    if "." not in value:
        return None
    raw, digest = value.rsplit(".", 1)
    expected = hmac.new(SECRET.encode(), raw.encode(), "sha256").hexdigest()
    if not hmac.compare_digest(digest, expected):
        return None
    return raw


def with_flash(path: str, message: str) -> str:
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}{urllib.parse.urlencode({'flash': message})}"


def safe_number(value: str, label: str, *, allow_zero: bool = False) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a number.") from exc
    if allow_zero:
        if number < 0:
            raise ValueError(f"{label} cannot be negative.")
    elif number <= 0:
        raise ValueError(f"{label} must be greater than zero.")
    return number


def peptide_catalog(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return query(conn, "SELECT name, color FROM peptides ORDER BY name")


def ensure_peptide(conn: sqlite3.Connection, name: str, notes: str = "") -> None:
    clean = name.strip()
    if not clean:
        raise ValueError("Peptide name is required.")
    conn.execute(
        """
        INSERT OR IGNORE INTO peptides (name, notes, color, created_at)
        VALUES (?, ?, '#60706a', ?)
        """,
        (clean, notes, now_iso()),
    )


def inventory_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    peptides = [row["name"] for row in peptide_catalog(conn)]
    lot_names = [row["peptide_name"] for row in query(conn, "SELECT DISTINCT peptide_name FROM inventory_lots")]
    log_names = [row["peptide_name"] for row in query(conn, "SELECT DISTINCT peptide_name FROM dose_logs")]
    adj_names = [row["peptide_name"] for row in query(conn, "SELECT DISTINCT peptide_name FROM inventory_adjustments")]
    names = sorted({*peptides, *lot_names, *log_names, *adj_names}, key=str.lower)
    rows: list[dict[str, Any]] = []
    for name in names:
        lot = one(
            conn,
            "SELECT COALESCE(SUM(vial_count * mg_per_vial), 0) total_mg, COALESCE(SUM(vial_count), 0) vials FROM inventory_lots WHERE peptide_name = ?",
            (name,),
        )
        used = one(
            conn,
            "SELECT COALESCE(SUM(actual_dose_amount), 0) used_mg, COUNT(*) logs FROM dose_logs WHERE peptide_name = ? AND status = 'completed' AND dose_unit = 'mg'",
            (name,),
        )
        adjustments = one(
            conn,
            "SELECT COALESCE(SUM(amount_mg), 0) amount_mg FROM inventory_adjustments WHERE peptide_name = ?",
            (name,),
        )
        latest_lot = one(
            conn,
            "SELECT mg_per_vial FROM inventory_lots WHERE peptide_name = ? ORDER BY added_at DESC, id DESC LIMIT 1",
            (name,),
        )
        total_mg = float(lot["total_mg"] or 0)
        used_mg = float(used["used_mg"] or 0)
        adjustment_mg = float(adjustments["amount_mg"] or 0)
        remaining_mg = total_mg + adjustment_mg - used_mg
        mg_per_vial = float(latest_lot["mg_per_vial"]) if latest_lot else 0.0
        rows.append(
            {
                "name": name,
                "total_mg": total_mg,
                "used_mg": used_mg,
                "adjustment_mg": adjustment_mg,
                "remaining_mg": remaining_mg,
                "vials_added": float(lot["vials"] or 0),
                "dose_logs": int(used["logs"] or 0),
                "mg_per_vial": mg_per_vial,
                "estimated_vials": remaining_mg / mg_per_vial if mg_per_vial > 0 else None,
            }
        )
    return rows


def fmt_mg(value: float) -> str:
    return f"{value:,.2f} mg".replace(".00 mg", " mg")


def fmt_num(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def peptide_options(conn: sqlite3.Connection, selected: str = "") -> str:
    options = []
    for row in peptide_catalog(conn):
        chosen = " selected" if row["name"] == selected else ""
        options.append(f'<option value="{h(row["name"])}"{chosen}>{h(row["name"])}</option>')
    return "\n".join(options)


def supplier_code_options() -> str:
    return "\n".join(
        f'<option value="{h(code)}">{h(data["name"])} · {fmt_num(float(data["mg_per_vial"]))} {h(data.get("unit", "mg"))}/vial · {h(data["vials_per_pack"])} pack</option>'
        for code, data in sorted(SUPPLIER_CODES.items())
    )


def layout(ctx: RequestContext, title: str, body: str) -> bytes:
    flash = f'<div class="flash">{h(ctx.flash)}</div>' if ctx.flash else ""
    error = f'<div class="flash error">{h(ctx.error)}</div>' if ctx.error else ""
    user_chip = ""
    if ctx.user:
        user_chip = f"""
        <div class="user-chip">
          <strong>{h(ctx.user['display_name'])}</strong>
          <span>{h(ctx.user['email'])}</span>
          <form method="post" action="/logout"><button class="text" type="submit">Log out</button></form>
        </div>
        """
    html = f"""<!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
      <meta name="theme-color" content="#315f94">
      <title>{h(title)} · {APP_NAME}</title>
      <link rel="stylesheet" href="/static/styles.css">
      <link rel="manifest" href="/static/manifest.webmanifest">
    </head>
    <body>
      <main class="app-shell">
        <header class="topbar">
          <div>
            <p class="eyebrow">Shared inventory control</p>
            <h1>{h(title)}</h1>
          </div>
          {user_chip}
        </header>
        <div class="notice">Inventory helper only. Confirm logs, vial labels, and dose units before making supply decisions.</div>
        {flash}
        {error}
        {body}
      </main>
      <script>
        const supplierCodes = {json.dumps(SUPPLIER_CODES, sort_keys=True)};
        document.addEventListener("input", (event) => {{
          if (!event.target.matches('[name="supplier_code"]')) return;
          const code = event.target.value.trim().toUpperCase().replace(/\\s+/g, "");
          const entry = supplierCodes[code];
          const form = event.target.closest("form");
          if (!entry || !form) return;
          const other = form.querySelector('[name="peptide_name_other"]');
          const mg = form.querySelector('[name="mg_per_vial"]');
          const vials = form.querySelector('[name="vial_count"]');
          if (other) other.value = entry.name || "";
          if (mg) mg.value = entry.mg_per_vial || "";
          if (vials && !vials.value) vials.value = entry.vials_per_pack || "";
        }});
      </script>
    </body>
    </html>"""
    return html.encode("utf-8")


def login_page(error: str | None = None) -> bytes:
    error_html = f'<div class="flash error">{h(error)}</div>' if error else ""
    html = f"""<!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Log in · {APP_NAME}</title>
      <link rel="stylesheet" href="/static/styles.css">
    </head>
    <body class="auth">
      <section class="auth-card">
        <div class="panel">
          <p class="eyebrow">Admin inventory</p>
          <h1>{APP_NAME}</h1>
          <p class="meta">Use your Peptide Power Assistant admin login.</p>
          {error_html}
          <form method="post" action="/login" class="stack">
            <label>Email <input name="email" type="email" autocomplete="username" required></label>
            <label>Password <input name="password" type="password" autocomplete="current-password" required></label>
            <button type="submit">Log in</button>
          </form>
        </div>
      </section>
    </body>
    </html>"""
    return html.encode("utf-8")


def render_home(ctx: RequestContext, conn: sqlite3.Connection) -> bytes:
    rows = inventory_rows(conn)
    total_remaining = sum(row["remaining_mg"] for row in rows)
    low_count = sum(1 for row in rows if row["remaining_mg"] <= 0)
    cards = "".join(
        f"""
        <article class="item">
          <div class="item-title">
            <div>
              <h3>{h(row['name'])}</h3>
              <p class="meta">{fmt_mg(row['remaining_mg'])} remaining · {fmt_num(row['estimated_vials'])} vials estimated</p>
            </div>
            <span class="badge {'red' if row['remaining_mg'] <= 0 else ''}">{row['dose_logs']} dose logs</span>
          </div>
          <div class="grid four compact-metrics">
            <div><span>Vials</span><strong>{fmt_num(row['estimated_vials'])}</strong></div>
            <div><span>Used</span><strong>{fmt_mg(row['used_mg'])}</strong></div>
            <div><span>Adjusted</span><strong>{fmt_mg(row['adjustment_mg'])}</strong></div>
            <div><span>Total MG</span><strong>{fmt_mg(row['total_mg'])}</strong></div>
          </div>
        </article>
        """
        for row in rows
    ) or '<div class="empty">No peptides or inventory yet.</div>'
    body = f"""
    <section class="panel hero-panel">
      <div>
        <p class="eyebrow">At a glance</p>
        <h2>{len(rows)} tracked peptides</h2>
        <p class="meta">{fmt_mg(total_remaining)} total remaining across shared inventory. {low_count} peptides are at or below zero.</p>
      </div>
      <a class="button" href="/inventory">Manage inventory</a>
    </section>
    <section class="panel">
      <div class="panel-head"><h2>Current inventory</h2></div>
      <div class="card-list">{cards}</div>
    </section>
    """
    return layout(ctx, "Inventory", body)


def render_inventory(ctx: RequestContext, conn: sqlite3.Connection) -> bytes:
    lots = query(conn, "SELECT * FROM inventory_lots ORDER BY added_at DESC, id DESC LIMIT 100")
    adjustments = query(conn, "SELECT * FROM inventory_adjustments ORDER BY created_at DESC, id DESC LIMIT 100")
    lot_html = "".join(
        f"""
        <article class="item">
          <div class="item-title">
            <div><h3>{h(row['peptide_name'])}</h3><p class="meta">{fmt_num(row['vial_count'])} vials · {fmt_num(row['mg_per_vial'])} mg/vial · added {h(row['added_at'])}</p></div>
          </div>
          <p class="meta">{h(row['notes'])}</p>
          <form method="post" action="/lots/delete" onsubmit="return confirm('Delete this inventory lot?');">
            <input type="hidden" name="lot_id" value="{row['id']}">
            <button class="danger" type="submit">Delete lot</button>
          </form>
        </article>
        """
        for row in lots
    ) or '<div class="empty">No inventory lots yet.</div>'
    adjustment_html = "".join(
        f"""
        <article class="item">
          <h3>{h(row['peptide_name'])}: {fmt_mg(float(row['amount_mg']))}</h3>
          <p class="meta">{h(row['reason'])} · {h(row['created_at'])}</p>
          <p class="meta">{h(row['notes'])}</p>
        </article>
        """
        for row in adjustments
    ) or '<div class="empty">No adjustments yet.</div>'
    body = f"""
    <section class="panel">
      <div class="panel-head"><h2>Add inventory</h2></div>
      <form method="post" action="/lots" class="stack">
        <div class="grid two">
          <label>Supplier code
            <input name="supplier_code" list="supplier-codes" placeholder="SK10, RT10, 2S10...">
            <datalist id="supplier-codes">
              {supplier_code_options()}
            </datalist>
          </label>
          <label>Existing peptide
            <select name="peptide_name">
              <option value="">Choose existing...</option>
              {peptide_options(conn)}
            </select>
          </label>
          <label>Or add new peptide <input name="peptide_name_other" placeholder="New peptide name"></label>
          <label>Vials on hand <input name="vial_count" type="number" step="0.01" min="0" required></label>
          <label>MG per vial <input name="mg_per_vial" type="number" step="0.01" min="0" required></label>
          <label>Date added <input name="added_at" type="date" value="{datetime.now(app_timezone()).date().isoformat()}"></label>
          <label>Notes <input name="notes" placeholder="optional"></label>
        </div>
        <div class="button-row"><button type="submit">Add inventory</button></div>
      </form>
    </section>
    <section class="panel">
      <div class="panel-head"><h2>Manual adjustment</h2></div>
      <form method="post" action="/adjustments" class="stack">
        <div class="grid two">
          <label>Peptide
            <select name="peptide_name" required>
              {peptide_options(conn)}
            </select>
          </label>
          <label>Amount MG <input name="amount_mg" type="number" step="0.01" placeholder="-5 or 5" required></label>
          <label>Reason <input name="reason" placeholder="waste, correction, found vial..." required></label>
          <label>Notes <input name="notes" placeholder="optional"></label>
        </div>
        <div class="button-row"><button class="secondary" type="submit">Save adjustment</button></div>
      </form>
    </section>
    <section class="panel">
      <div class="panel-head"><h2>Inventory lots</h2></div>
      <div class="card-list">{lot_html}</div>
    </section>
    <section class="panel">
      <div class="panel-head"><h2>Recent adjustments</h2></div>
      <div class="card-list">{adjustment_html}</div>
    </section>
    <div class="button-row"><a class="button secondary" href="/">Back to dashboard</a></div>
    """
    return layout(ctx, "Manage Inventory", body)


class App(BaseHTTPRequestHandler):
    server_version = "PeptideInventory/0.1"

    def do_HEAD(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        self.send_response(HTTPStatus.OK if parsed.path in {"/", "/login", "/healthz"} else HTTPStatus.NOT_FOUND)
        self.end_headers()

    def do_GET(self) -> None:
        try:
            self.route_get()
        except Exception as exc:
            self.error_response(exc)

    def do_POST(self) -> None:
        try:
            self.route_post()
        except Exception as exc:
            self.error_response(exc)

    def route_get(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/healthz":
            return self.text("ok")
        if parsed.path.startswith("/static/"):
            return self.serve_static(STATIC_DIR / parsed.path.removeprefix("/static/"))
        if parsed.path == "/login":
            return self.html(login_page())
        ctx = self.context()
        if not ctx.user:
            return self.redirect("/login")
        if ctx.user["role"] != "admin":
            raise PermissionError("Admin access required.")
        with db() as conn:
            if parsed.path == "/":
                return self.html(render_home(ctx, conn))
            if parsed.path == "/inventory":
                return self.html(render_inventory(ctx, conn))
        self.not_found()

    def route_post(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        data = self.form_data()
        if parsed.path == "/login":
            return self.login(data)
        if parsed.path == "/logout":
            return self.logout()
        ctx = self.context()
        if not ctx.user:
            return self.redirect("/login")
        if ctx.user["role"] != "admin":
            raise PermissionError("Admin access required.")
        with db() as conn:
            if parsed.path == "/lots":
                self.add_lot(conn, ctx.user["id"], data)
                return self.redirect(with_flash("/inventory", "Inventory added"))
            if parsed.path == "/lots/delete":
                conn.execute("DELETE FROM inventory_lots WHERE id = ?", (int(data["lot_id"]),))
                return self.redirect(with_flash("/inventory", "Inventory lot deleted"))
            if parsed.path == "/adjustments":
                self.add_adjustment(conn, ctx.user["id"], data)
                return self.redirect(with_flash("/inventory", "Adjustment saved"))
        self.not_found()

    def context(self) -> RequestContext:
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        cookies = parse_cookies(self.headers.get("Cookie"))
        sid = unsign(cookies.get("sid", "")) if cookies.get("sid") else None
        user = None
        if sid:
            user_id = SESSIONS.get(sid)
            if user_id:
                with db() as conn:
                    user = one(conn, "SELECT * FROM users WHERE id = ? AND active = 1", (user_id,))
        return RequestContext(
            user=user,
            flash=params.get("flash", [None])[0],
            error=params.get("error", [None])[0],
        )

    def form_data(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(min(length, 1024 * 1024)).decode("utf-8")
        parsed = urllib.parse.parse_qs(raw, keep_blank_values=True)
        return {key: ",".join(values) if len(values) > 1 else values[-1] for key, values in parsed.items()}

    def login(self, data: dict[str, str]) -> None:
        email = data.get("email", "").strip().lower()
        password = data.get("password", "")
        with db() as conn:
            user = one(conn, "SELECT * FROM users WHERE email = ? AND active = 1", (email,))
        if not user or not verify_password(password, user["password_hash"]):
            return self.html(login_page("Invalid email or password."), HTTPStatus.UNAUTHORIZED)
        if user["role"] != "admin":
            return self.html(login_page("Admin access required."), HTTPStatus.FORBIDDEN)
        sid = secrets.token_urlsafe(32)
        SESSIONS[sid] = int(user["id"])
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", f"sid={sign(sid)}; HttpOnly; SameSite=Lax; Path=/")
        self.end_headers()

    def logout(self) -> None:
        cookies = parse_cookies(self.headers.get("Cookie"))
        sid = unsign(cookies.get("sid", "")) if cookies.get("sid") else None
        if sid:
            SESSIONS.pop(sid, None)
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/login")
        self.send_header("Set-Cookie", "sid=; Max-Age=0; Path=/")
        self.end_headers()

    def add_lot(self, conn: sqlite3.Connection, user_id: int, data: dict[str, str]) -> None:
        code_data = supplier_lookup(data.get("supplier_code", ""))
        peptide_name = (data.get("peptide_name_other") or data.get("peptide_name") or "").strip()
        if code_data:
            peptide_name = str(code_data["name"])
        ensure_peptide(conn, peptide_name, "Added from inventory app.")
        vial_count = safe_number(data.get("vial_count", ""), "Vials on hand")
        mg_per_vial = float(code_data["mg_per_vial"]) if code_data else safe_number(data.get("mg_per_vial", ""), "MG per vial")
        added_at = (data.get("added_at") or datetime.now(app_timezone()).date().isoformat()).strip()
        notes = data.get("notes", "").strip()
        if code_data:
            code_note = f"Supplier code {code_data['code']}"
            notes = f"{code_note}. {notes}" if notes else code_note
        conn.execute(
            """
            INSERT INTO inventory_lots (peptide_name, vial_count, mg_per_vial, added_at, notes, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (peptide_name, vial_count, mg_per_vial, added_at, notes, user_id, now_iso()),
        )

    def add_adjustment(self, conn: sqlite3.Connection, user_id: int, data: dict[str, str]) -> None:
        peptide_name = data.get("peptide_name", "").strip()
        if not peptide_name:
            raise ValueError("Peptide is required.")
        amount_mg = float(data.get("amount_mg", ""))
        reason = data.get("reason", "").strip()
        if not reason:
            raise ValueError("Reason is required.")
        conn.execute(
            """
            INSERT INTO inventory_adjustments (peptide_name, amount_mg, reason, notes, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (peptide_name, amount_mg, reason, data.get("notes", "").strip(), user_id, now_iso()),
        )

    def serve_static(self, path: Path) -> None:
        resolved = path.resolve()
        if not str(resolved).startswith(str(STATIC_DIR.resolve())) or not resolved.exists():
            return self.not_found()
        content_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
        data = resolved.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def html(self, content: bytes, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def text(self, content: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def not_found(self) -> None:
        self.send_response(HTTPStatus.NOT_FOUND)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Not found")

    def error_response(self, exc: Exception) -> None:
        status = HTTPStatus.FORBIDDEN if isinstance(exc, PermissionError) else HTTPStatus.BAD_REQUEST
        content = layout(RequestContext(self.context().user, error=str(exc)), "Error", f'<section class="panel"><div class="empty">{h(exc)}</div></section>')
        self.html(content, status)

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))


def main() -> None:
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), App)
    print(f"{APP_NAME} running at http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
