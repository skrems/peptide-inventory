from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import sys
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.supplier_codes import SUPPLIER_CODES, supplier_lookup


APP_NAME = "Peptide Inventory"
APP_VERSION = "v1.6"
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
              unit TEXT NOT NULL DEFAULT 'mg',
              vials_used REAL NOT NULL DEFAULT 0,
              vendor_name TEXT NOT NULL DEFAULT '',
              batch_date TEXT NOT NULL DEFAULT '',
              expiry_date TEXT NOT NULL DEFAULT '',
              added_at TEXT NOT NULL,
              notes TEXT,
              created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS inventory_adjustments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              peptide_name TEXT NOT NULL,
              amount_mg REAL NOT NULL,
              unit TEXT NOT NULL DEFAULT 'mg',
              reason TEXT NOT NULL,
              notes TEXT,
              created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS inventory_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_type TEXT NOT NULL,
              lot_id INTEGER,
              peptide_name TEXT NOT NULL,
              quantity_vials REAL NOT NULL DEFAULT 0,
              mg_per_vial REAL NOT NULL DEFAULT 0,
              amount_mg REAL NOT NULL DEFAULT 0,
              unit TEXT NOT NULL DEFAULT 'mg',
              reason TEXT,
              notes TEXT,
              created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
              created_at TEXT NOT NULL
            );
            """
        )
        columns = {row["name"] for row in query(conn, "PRAGMA table_info(inventory_lots)")}
        if "vials_used" not in columns:
            conn.execute("ALTER TABLE inventory_lots ADD COLUMN vials_used REAL NOT NULL DEFAULT 0")
        if "vendor_name" not in columns:
            conn.execute("ALTER TABLE inventory_lots ADD COLUMN vendor_name TEXT NOT NULL DEFAULT ''")
        if "batch_date" not in columns:
            conn.execute("ALTER TABLE inventory_lots ADD COLUMN batch_date TEXT NOT NULL DEFAULT ''")
        if "expiry_date" not in columns:
            conn.execute("ALTER TABLE inventory_lots ADD COLUMN expiry_date TEXT NOT NULL DEFAULT ''")
        if "unit" not in columns:
            conn.execute("ALTER TABLE inventory_lots ADD COLUMN unit TEXT NOT NULL DEFAULT 'mg'")
        adjustment_columns = {row["name"] for row in query(conn, "PRAGMA table_info(inventory_adjustments)")}
        if "unit" not in adjustment_columns:
            conn.execute("ALTER TABLE inventory_adjustments ADD COLUMN unit TEXT NOT NULL DEFAULT 'mg'")
        event_columns = {row["name"] for row in query(conn, "PRAGMA table_info(inventory_events)")}
        if "unit" not in event_columns:
            conn.execute("ALTER TABLE inventory_events ADD COLUMN unit TEXT NOT NULL DEFAULT 'mg'")


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


def ensure_peptide_unit(conn: sqlite3.Connection, name: str, unit: str) -> None:
    units = {
        normalize_unit(row["unit"])
        for row in query(
            conn,
            """
            SELECT unit FROM inventory_lots WHERE peptide_name = ?
            UNION SELECT unit FROM inventory_adjustments WHERE peptide_name = ?
            """,
            (name, name),
        )
    }
    if units and units != {unit}:
        existing = ", ".join(unit_label(value) for value in sorted(units))
        raise ValueError(f"{name} already uses {existing}; units cannot be mixed for one peptide.")


def inventory_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    catalog = peptide_catalog(conn)
    peptides = [row["name"] for row in catalog]
    peptide_colors = {row["name"]: row["color"] for row in catalog}
    lot_names = [row["peptide_name"] for row in query(conn, "SELECT DISTINCT peptide_name FROM inventory_lots")]
    adj_names = [row["peptide_name"] for row in query(conn, "SELECT DISTINCT peptide_name FROM inventory_adjustments")]
    names = sorted({*peptides, *lot_names, *adj_names}, key=str.lower)
    rows: list[dict[str, Any]] = []
    for name in names:
        unit_row = one(
            conn,
            """
            SELECT unit FROM (
              SELECT unit, created_at, id FROM inventory_lots WHERE peptide_name = ?
              UNION ALL
              SELECT unit, created_at, id FROM inventory_adjustments WHERE peptide_name = ?
            )
            ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            (name, name),
        )
        unit = normalize_unit(unit_row["unit"] if unit_row else "mg")
        lot = one(
            conn,
            """
            SELECT
              COALESCE(SUM(vial_count * mg_per_vial), 0) total_added_mg,
              COALESCE(SUM(CASE WHEN vial_count > COALESCE(vials_used, 0) THEN (vial_count - COALESCE(vials_used, 0)) * mg_per_vial ELSE 0 END), 0) on_hand_mg,
              COALESCE(SUM(vial_count), 0) vials,
              COALESCE(SUM(COALESCE(vials_used, 0)), 0) vials_used,
              COALESCE(SUM(CASE WHEN vial_count > COALESCE(vials_used, 0) THEN vial_count - COALESCE(vials_used, 0) ELSE 0 END), 0) vials_on_hand
            FROM inventory_lots
            WHERE peptide_name = ? AND lower(unit) = ?
            """,
            (name, unit),
        )
        baseline = one(
            conn,
            """
            SELECT MIN(created_at) inventory_started_at
            FROM (
              SELECT created_at FROM inventory_lots WHERE peptide_name = ? AND lower(unit) = ?
              UNION ALL
              SELECT created_at FROM inventory_adjustments WHERE peptide_name = ? AND lower(unit) = ?
            )
            """,
            (name, unit, name, unit),
        )
        inventory_started_at = baseline["inventory_started_at"] if baseline else None
        if inventory_started_at:
            logged = one(
                conn,
                """
                SELECT COALESCE(SUM(actual_dose_amount), 0) logged_amount, COUNT(*) logs
                FROM dose_logs
                WHERE peptide_name = ?
                  AND status = 'completed'
                  AND lower(dose_unit) = ?
                  AND logged_at >= ?
                """,
                (name, unit, inventory_started_at),
            )
        else:
            logged = {"logged_amount": 0, "logs": 0}
        adjustments = one(
            conn,
            "SELECT COALESCE(SUM(amount_mg), 0) amount_mg FROM inventory_adjustments WHERE peptide_name = ? AND lower(unit) = ?",
            (name, unit),
        )
        latest_lot = one(
            conn,
            "SELECT mg_per_vial FROM inventory_lots WHERE peptide_name = ? AND lower(unit) = ? ORDER BY added_at DESC, id DESC LIMIT 1",
            (name, unit),
        )
        total_added_mg = float(lot["total_added_mg"] or 0)
        on_hand_mg = float(lot["on_hand_mg"] or 0)
        logged_mg = float(logged["logged_amount"] or 0)
        adjustment_mg = float(adjustments["amount_mg"] or 0)
        remaining_mg = on_hand_mg + adjustment_mg
        mg_per_vial = float(latest_lot["mg_per_vial"]) if latest_lot else 0.0
        remaining_vials = float(lot["vials_on_hand"] or 0)
        projected_days = None
        if inventory_started_at and logged_mg > 0 and remaining_mg > 0:
            started_at = datetime.fromisoformat(inventory_started_at)
            elapsed_days = max((datetime.now(app_timezone()).replace(tzinfo=None) - started_at).total_seconds() / 86400, 1)
            daily_logged_mg = logged_mg / elapsed_days
            projected_days = remaining_mg / daily_logged_mg if daily_logged_mg > 0 else None
        rows.append(
            {
                "name": name,
                "unit": unit,
                "color": safe_color(peptide_colors.get(name)),
                "total_mg": remaining_mg,
                "total_added_mg": total_added_mg,
                "logged_mg": logged_mg,
                "adjustment_mg": adjustment_mg,
                "remaining_mg": remaining_mg,
                "vials_added": float(lot["vials"] or 0),
                "vials_used": float(lot["vials_used"] or 0),
                "dose_logs": int(logged["logs"] or 0),
                "mg_per_vial": mg_per_vial,
                "remaining_vials": remaining_vials,
                "projected_days": projected_days,
                "inventory_started_at": inventory_started_at,
            }
        )
    return rows


def safe_color(value: Any) -> str:
    color = str(value or "").strip()
    return color if re.fullmatch(r"#[0-9a-fA-F]{6}", color) else "#60706a"


def fmt_mg(value: float) -> str:
    return fmt_amount(value, "mg")


def normalize_unit(value: Any) -> str:
    unit = str(value or "mg").strip().lower()
    if unit not in {"mg", "iu"}:
        raise ValueError("Unit must be mg or IU.")
    return unit


def unit_label(unit: str) -> str:
    return "IU" if normalize_unit(unit) == "iu" else "mg"


def unit_heading(unit: str) -> str:
    return unit_label(unit).upper()


def fmt_amount(value: float, unit: str) -> str:
    label = unit_label(unit)
    return f"{value:,.2f} {label}".replace(f".00 {label}", f" {label}")


def fmt_num(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def health_state(row: dict[str, Any]) -> str:
    if row["remaining_mg"] <= 0:
        return "critical"
    if row["projected_days"] is None:
        return "unknown"
    if row["projected_days"] < 21:
        return "critical"
    if row["projected_days"] < 45:
        return "low"
    return "healthy"


def health_label(state: str) -> str:
    return {
        "healthy": "Healthy",
        "low": "Low",
        "critical": "Critical",
        "unknown": "No pace yet",
    }[state]


def runway_bar_width(row: dict[str, Any]) -> int:
    if row["remaining_mg"] <= 0:
        return 100
    if row["projected_days"] is None:
        return 8
    return max(8, min(100, round((row["projected_days"] / 90) * 100)))


def runway_text(row: dict[str, Any]) -> str:
    if row["remaining_mg"] <= 0:
        return "Out of stock"
    if row["projected_days"] is None:
        return "No usage pace yet"
    return f"~{fmt_num(row['projected_days'])} days left"


def order_by_text(row: dict[str, Any]) -> str:
    if row["remaining_mg"] <= 0:
        return "Order now"
    if row["projected_days"] is None:
        return "Watch stock"
    order_date = datetime.now(app_timezone()).date() + timedelta(days=max(row["projected_days"] - 14, 0))
    if row["projected_days"] <= 14:
        return "Order now"
    return order_date.strftime("%b %-d")


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


def nav_item(path: str, label: str, active: str) -> str:
    current = "active" if active == path else ""
    return f'<a class="{current}" href="{path}">{h(label)}</a>'


def layout(ctx: RequestContext, title: str, body: str, active: str = "/") -> bytes:
    flash = f'<div class="flash">{h(ctx.flash)}</div>' if ctx.flash else ""
    error = f'<div class="flash error">{h(ctx.error)}</div>' if ctx.error else ""
    user_chip = ""
    if ctx.user:
        user_chip = f"""
        <div class="user-chip">
          <strong>{h(ctx.user['display_name'])}</strong>
          <span>{h(ctx.user['email'])}</span>
          <span class="version-chip" title="Running application version">Version {h(APP_VERSION)}</span>
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
        <nav class="tabs">
          {nav_item("/", "Dashboard", active)}
          {nav_item("/inventory", "Manage", active)}
          {nav_item("/vials", "Vial view", active)}
          {nav_item("/one-k", "1K foot view", active)}
          {nav_item("/log", "Log", active)}
        </nav>
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
          const unit = form.querySelector('[name="unit"]');
          const vials = form.querySelector('[name="vial_count"]');
          if (other) other.value = entry.name || "";
          if (mg) mg.value = entry.mg_per_vial || "";
          if (unit) unit.value = (entry.unit || "mg").toLowerCase();
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
    totals_by_unit = {
        unit: sum(row["remaining_mg"] for row in rows if row["unit"] == unit)
        for unit in ("mg", "iu")
    }
    stock_summary = " · ".join(
        fmt_amount(total, unit) for unit, total in totals_by_unit.items() if total
    ) or "0 mg"
    states = {state: sum(1 for row in rows if health_state(row) == state) for state in ("critical", "low", "healthy", "unknown")}
    forecast_text = lambda row: f" · ~{fmt_num(row['projected_days'])} days at logged pace" if row["projected_days"] else ""
    cards = "".join(
        f"""
        <article class="item inventory-card {health_state(row)}">
          <div class="inventory-row">
            <div class="inventory-peptide">
              <h3>{h(row['name'])}</h3>
              <p class="meta">{fmt_amount(row['remaining_mg'], row['unit'])} on hand · {row['dose_logs']} forecast dose logs{forecast_text(row)}</p>
            </div>
            <div class="metric"><span>Vials on hand</span><strong>{fmt_num(row['remaining_vials'])}</strong></div>
            <div class="metric"><span>Vials used</span><strong>{fmt_num(row['vials_used'])}</strong></div>
            <div class="metric"><span>Logged {unit_heading(row['unit'])}</span><strong>{fmt_amount(row['logged_mg'], row['unit'])}</strong></div>
            <div class="metric"><span>Adjusted {unit_heading(row['unit'])}</span><strong>{fmt_amount(row['adjustment_mg'], row['unit'])}</strong></div>
            <div class="metric"><span>Total {unit_heading(row['unit'])}</span><strong>{fmt_amount(row['total_mg'], row['unit'])}</strong></div>
          </div>
          <div class="runway">
            <div class="runway-head">
              <span class="badge {health_state(row)}">{health_label(health_state(row))}</span>
              <strong>{runway_text(row)}</strong>
              <span>Order by: {order_by_text(row)}</span>
            </div>
            <div class="runway-track" aria-label="{h(row['name'])} inventory runway">
              <span class="runway-fill {health_state(row)}" style="width: {runway_bar_width(row)}%;"></span>
            </div>
          </div>
        </article>
        """
        for row in rows
    ) or '<div class="empty">No peptides or inventory yet.</div>'
    summary = "".join(
        f'<span class="health-pill {state}">{count} {health_label(state).lower()}</span>'
        for state, count in states.items()
    )
    body = f"""
    <section class="panel hero-panel">
      <div>
        <p class="eyebrow">At a glance</p>
        <h2>{len(rows)} tracked peptides</h2>
        <p class="meta">{stock_summary} physical stock on hand. Dose logs are used only for forecasting.</p>
        <div class="health-summary">{summary}</div>
      </div>
      <a class="button" href="/inventory">Manage inventory</a>
    </section>
    <section class="panel">
      <div class="panel-head"><h2>Current inventory</h2></div>
      <div class="card-list">{cards}</div>
    </section>
    """
    return layout(ctx, "Inventory", body, "/")


def render_inventory(ctx: RequestContext, conn: sqlite3.Connection) -> bytes:
    lots = query(
        conn,
        """
        SELECT *,
          CASE WHEN vial_count > COALESCE(vials_used, 0) THEN vial_count - COALESCE(vials_used, 0) ELSE 0 END AS vials_available
        FROM inventory_lots
        ORDER BY added_at DESC, id DESC
        LIMIT 100
        """,
    )
    adjustments = query(conn, "SELECT * FROM inventory_adjustments ORDER BY created_at DESC, id DESC LIMIT 100")
    lot_html = "".join(
        f"""
        <article class="item">
          <div class="item-title">
            <div><h3>{h(row['peptide_name'])}</h3><p class="meta">{fmt_num(row['vials_available'])} of {fmt_num(row['vial_count'])} vials on hand · {fmt_num(row['mg_per_vial'])} {unit_label(row['unit'])}/vial{f" · vendor {h(row['vendor_name'])}" if row['vendor_name'] else ""}{f" · batch {h(row['batch_date'])}" if row['batch_date'] else ""}{f" · expires {h(row['expiry_date'])}" if row['expiry_date'] else ""} · added {h(row['added_at'])}</p></div>
          </div>
          <p class="meta">{h(row['notes'])}</p>
          <div class="button-row">
            <form method="post" action="/lots/use" onsubmit="return confirm('Mark one vial from this lot as used/reconstituted?');">
              <input type="hidden" name="lot_id" value="{row['id']}">
              <button class="secondary" type="submit">Mark 1 vial used</button>
            </form>
            <form method="post" action="/lots/restore">
              <input type="hidden" name="lot_id" value="{row['id']}">
              <button class="secondary" type="submit">Restore 1 vial</button>
            </form>
            <form method="post" action="/lots/delete" onsubmit="return confirm('Delete this inventory lot?');">
              <input type="hidden" name="lot_id" value="{row['id']}">
              <button class="danger" type="submit">Delete lot</button>
            </form>
          </div>
        </article>
        """
        for row in lots
    ) or '<div class="empty">No inventory lots yet.</div>'
    adjustment_html = "".join(
        f"""
        <article class="item">
          <h3>{h(row['peptide_name'])}: {fmt_amount(float(row['amount_mg']), row['unit'])}</h3>
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
          <label>Vendor name <input name="vendor_name" placeholder="WanShun, Peptide Lab..."></label>
          <label>Vials on hand <input name="vial_count" type="number" step="0.01" min="0" required></label>
          <label>Strength per vial <input name="mg_per_vial" type="number" step="0.01" min="0" required></label>
          <label>Unit
            <select name="unit"><option value="mg">mg</option><option value="iu">IU</option></select>
          </label>
          <label>Batch date <input name="batch_date" type="date"></label>
          <label>Expiry date <input name="expiry_date" type="date"></label>
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
          <label>Amount <input name="amount_mg" type="number" step="0.01" placeholder="-5 or 5" required></label>
          <label>Unit
            <select name="unit"><option value="mg">mg</option><option value="iu">IU</option></select>
          </label>
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
    return layout(ctx, "Manage Inventory", body, "/inventory")


def vial_markers(row: dict[str, Any]) -> str:
    full_vials = int(row["remaining_vials"])
    partial_vial = row["remaining_vials"] - full_vials
    color = h(row["color"])
    markers = "".join(
        f'<span class="vial-marker" style="--vial-color: {color}" aria-hidden="true"></span>'
        for _ in range(full_vials)
    )
    if partial_vial >= 0.01:
        fill = max(1, min(99, round(partial_vial * 100)))
        markers += (
            f'<span class="vial-marker partial" style="--vial-color: {color}; --vial-fill: {fill}%" '
            'aria-hidden="true"></span>'
        )
    return markers


def render_vial_view(ctx: RequestContext, conn: sqlite3.Connection) -> bytes:
    stock_rows = [row for row in inventory_rows(conn) if row["remaining_vials"] > 0]
    total_vials = sum(row["remaining_vials"] for row in stock_rows)

    groups = "".join(
        f"""
        <article class="vial-group">
          <div class="vial-group-head">
            <div class="vial-name">
              <span class="color-swatch" style="--swatch-color: {h(row['color'])}" aria-hidden="true"></span>
              <div>
                <h3>{h(row['name'])}</h3>
                <p class="meta">{fmt_amount(row['remaining_mg'], row['unit'])} on hand</p>
              </div>
            </div>
            <strong class="vial-count">{fmt_num(row['remaining_vials'])} vial{'s' if row['remaining_vials'] != 1 else ''}</strong>
          </div>
          <div class="vial-grid" aria-label="{h(row['name'])}: {fmt_num(row['remaining_vials'])} vials on hand">
            {vial_markers(row)}
          </div>
        </article>
        """
        for row in stock_rows
    ) or '<div class="empty">No physical vial stock is currently on hand.</div>'
    body = f"""
    <section class="panel vial-hero">
      <div>
        <p class="eyebrow">Physical stock</p>
        <h2>{fmt_num(total_vials)} vials on hand</h2>
        <p class="meta">Each marker represents one vial currently on hand. Used/reconstituted vials are excluded.</p>
      </div>
      <a class="button secondary" href="/inventory">Manage inventory</a>
    </section>
    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>Vial view</h2>
          <p class="meta">A visual count of your physical stock, grouped by peptide.</p>
        </div>
      </div>
      <div class="vial-list">{groups}</div>
    </section>
    """
    return layout(ctx, "Vial View", body, "/vials")


def render_one_k_view(ctx: RequestContext, conn: sqlite3.Connection) -> bytes:
    stock_rows = sorted(
        (row for row in inventory_rows(conn) if row["remaining_vials"] > 0),
        key=lambda row: row["name"].lower(),
    )
    total_vials = sum(row["remaining_vials"] for row in stock_rows)
    all_markers = "".join(vial_markers(row) for row in stock_rows)
    legend = "".join(
        f"""
        <div class="vial-legend-item">
          <span class="color-swatch" style="--swatch-color: {h(row['color'])}" aria-hidden="true"></span>
          <span>{h(row['name'])}</span>
          <strong>{fmt_num(row['remaining_vials'])}</strong>
        </div>
        """
        for row in stock_rows
    ) or '<div class="empty">No physical vial stock is currently on hand.</div>'
    body = f"""
    <section class="panel one-k-hero">
      <div>
        <p class="eyebrow">Thousand-foot view</p>
        <h2>{fmt_num(total_vials)} vials on hand</h2>
        <p class="meta">Every colored marker is one physical vial. Colors change as one peptide runs into the next.</p>
      </div>
      <a class="button secondary" href="/inventory">Manage inventory</a>
    </section>
    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>All physical stock</h2>
          <p class="meta">{len(stock_rows)} peptides currently have vials on hand.</p>
        </div>
      </div>
      <div class="vial-mosaic" aria-label="{fmt_num(total_vials)} vials currently on hand across {len(stock_rows)} peptides">
        {all_markers}
      </div>
      <div class="vial-legend" aria-label="Vial color legend">{legend}</div>
    </section>
    """
    return layout(ctx, "1K Foot View", body, "/one-k")


def event_label(event_type: str) -> str:
    return {
        "lot_added": "Lot added",
        "vial_used": "Vial used",
        "vial_restored": "Vial restored",
        "lot_deleted": "Lot deleted",
        "adjustment_added": "Adjustment added",
    }.get(event_type, event_type.replace("_", " ").title())


def render_log(ctx: RequestContext, conn: sqlite3.Connection) -> bytes:
    events = query(
        conn,
        """
        SELECT e.*, u.display_name
        FROM inventory_events e
        LEFT JOIN users u ON u.id = e.created_by
        ORDER BY e.created_at DESC, e.id DESC
        LIMIT 200
        """,
    )
    event_html = "".join(
        f"""
        <article class="item event-item">
          <div class="item-title">
            <div>
              <h3>{h(event_label(row['event_type']))}: {h(row['peptide_name'])}</h3>
              <p class="meta">{h(row['created_at'])} · {h(row['display_name'] or 'Unknown admin')}</p>
            </div>
            <span class="badge">{h(row['event_type'].replace('_', ' '))}</span>
          </div>
          <div class="event-grid">
            <div class="metric"><span>Lot</span><strong>{h(row['lot_id'] or 'n/a')}</strong></div>
            <div class="metric"><span>Vials</span><strong>{fmt_num(float(row['quantity_vials'] or 0))}</strong></div>
            <div class="metric"><span>{unit_heading(row['unit'])} / vial</span><strong>{fmt_amount(float(row['mg_per_vial'] or 0), row['unit'])}</strong></div>
            <div class="metric"><span>Total {unit_heading(row['unit'])}</span><strong>{fmt_amount(float(row['amount_mg'] or 0), row['unit'])}</strong></div>
          </div>
          <p class="meta">{h(row['reason'] or '')}</p>
          <p class="meta">{h(row['notes'] or '')}</p>
        </article>
        """
        for row in events
    ) or '<div class="empty">No inventory events yet. New vial use, restore, lot, delete, and adjustment actions will appear here.</div>'
    body = f"""
    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>Inventory log</h2>
          <p class="meta">Timestamped audit trail for physical inventory actions.</p>
        </div>
      </div>
      <div class="card-list">{event_html}</div>
    </section>
    """
    return layout(ctx, "Inventory Log", body, "/log")


class App(BaseHTTPRequestHandler):
    server_version = "PeptideInventory/0.1"

    def do_HEAD(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        self.send_response(HTTPStatus.OK if parsed.path in {"/", "/inventory", "/vials", "/one-k", "/log", "/login", "/healthz"} else HTTPStatus.NOT_FOUND)
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
            if parsed.path == "/vials":
                return self.html(render_vial_view(ctx, conn))
            if parsed.path == "/one-k":
                return self.html(render_one_k_view(ctx, conn))
            if parsed.path == "/log":
                return self.html(render_log(ctx, conn))
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
            if parsed.path == "/lots/use":
                self.mark_lot_vial(conn, ctx.user["id"], int(data["lot_id"]), 1)
                return self.redirect(with_flash("/inventory", "Vial marked used"))
            if parsed.path == "/lots/restore":
                self.mark_lot_vial(conn, ctx.user["id"], int(data["lot_id"]), -1)
                return self.redirect(with_flash("/inventory", "Vial restored"))
            if parsed.path == "/lots/delete":
                self.delete_lot(conn, ctx.user["id"], int(data["lot_id"]))
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
        unit = normalize_unit(code_data.get("unit", "mg") if code_data else data.get("unit", "mg"))
        ensure_peptide_unit(conn, peptide_name, unit)
        vial_count = safe_number(data.get("vial_count", ""), "Vials on hand")
        mg_per_vial = float(code_data["mg_per_vial"]) if code_data else safe_number(data.get("mg_per_vial", ""), "Strength per vial")
        added_at = (data.get("added_at") or datetime.now(app_timezone()).date().isoformat()).strip()
        vendor_name = data.get("vendor_name", "").strip()
        batch_date = data.get("batch_date", "").strip()
        expiry_date = data.get("expiry_date", "").strip()
        notes = data.get("notes", "").strip()
        if code_data:
            code_note = f"Supplier code {code_data['code']}"
            notes = f"{code_note}. {notes}" if notes else code_note
        cursor = conn.execute(
            """
            INSERT INTO inventory_lots
              (peptide_name, vial_count, mg_per_vial, unit, vendor_name, batch_date, expiry_date, added_at, notes, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (peptide_name, vial_count, mg_per_vial, unit, vendor_name, batch_date, expiry_date, added_at, notes, user_id, now_iso()),
        )
        self.record_event(
            conn,
            user_id,
            "lot_added",
            cursor.lastrowid,
            peptide_name,
            vial_count,
            mg_per_vial,
            vial_count * mg_per_vial,
            unit,
            "Inventory lot added",
            notes,
        )

    def add_adjustment(self, conn: sqlite3.Connection, user_id: int, data: dict[str, str]) -> None:
        peptide_name = data.get("peptide_name", "").strip()
        if not peptide_name:
            raise ValueError("Peptide is required.")
        amount_mg = float(data.get("amount_mg", ""))
        unit = normalize_unit(data.get("unit", "mg"))
        ensure_peptide_unit(conn, peptide_name, unit)
        reason = data.get("reason", "").strip()
        if not reason:
            raise ValueError("Reason is required.")
        conn.execute(
            """
            INSERT INTO inventory_adjustments (peptide_name, amount_mg, unit, reason, notes, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (peptide_name, amount_mg, unit, reason, data.get("notes", "").strip(), user_id, now_iso()),
        )
        self.record_event(
            conn,
            user_id,
            "adjustment_added",
            None,
            peptide_name,
            0,
            0,
            amount_mg,
            unit,
            reason,
            data.get("notes", "").strip(),
        )

    def mark_lot_vial(self, conn: sqlite3.Connection, user_id: int, lot_id: int, delta: int) -> None:
        lot = one(
            conn,
            """
            SELECT id, peptide_name, vial_count, mg_per_vial, unit, COALESCE(vials_used, 0) AS vials_used, notes
            FROM inventory_lots
            WHERE id = ?
            """,
            (lot_id,),
        )
        if not lot:
            raise ValueError("Inventory lot not found.")
        vial_count = float(lot["vial_count"])
        vials_used = float(lot["vials_used"])
        next_used = vials_used + delta
        if next_used < 0:
            raise ValueError("No used vials to restore for this lot.")
        if next_used > vial_count:
            raise ValueError("All vials in this lot are already marked used.")
        conn.execute("UPDATE inventory_lots SET vials_used = ? WHERE id = ?", (next_used, lot_id))
        event_type = "vial_used" if delta > 0 else "vial_restored"
        reason = "Vial marked used/reconstituted" if delta > 0 else "Vial restored to on-hand stock"
        mg_per_vial = float(lot["mg_per_vial"])
        self.record_event(
            conn,
            user_id,
            event_type,
            lot_id,
            lot["peptide_name"],
            abs(delta),
            mg_per_vial,
            abs(delta) * mg_per_vial,
            normalize_unit(lot["unit"]),
            reason,
            lot["notes"] or "",
        )

    def delete_lot(self, conn: sqlite3.Connection, user_id: int, lot_id: int) -> None:
        lot = one(
            conn,
            """
            SELECT id, peptide_name, vial_count, mg_per_vial, unit, COALESCE(vials_used, 0) AS vials_used, notes
            FROM inventory_lots
            WHERE id = ?
            """,
            (lot_id,),
        )
        if not lot:
            raise ValueError("Inventory lot not found.")
        remaining_vials = max(float(lot["vial_count"]) - float(lot["vials_used"]), 0)
        mg_per_vial = float(lot["mg_per_vial"])
        self.record_event(
            conn,
            user_id,
            "lot_deleted",
            lot_id,
            lot["peptide_name"],
            remaining_vials,
            mg_per_vial,
            remaining_vials * mg_per_vial,
            normalize_unit(lot["unit"]),
            "Inventory lot deleted",
            lot["notes"] or "",
        )
        conn.execute("DELETE FROM inventory_lots WHERE id = ?", (lot_id,))

    def record_event(
        self,
        conn: sqlite3.Connection,
        user_id: int,
        event_type: str,
        lot_id: int | None,
        peptide_name: str,
        quantity_vials: float,
        mg_per_vial: float,
        amount_mg: float,
        unit: str,
        reason: str,
        notes: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO inventory_events
              (event_type, lot_id, peptide_name, quantity_vials, mg_per_vial, amount_mg, unit, reason, notes, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (event_type, lot_id, peptide_name, quantity_vials, mg_per_vial, amount_mg, unit, reason, notes, user_id, now_iso()),
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
