import os
import secrets
import string
import hashlib
import html
import re
import logging
from datetime import datetime, timezone, timedelta

import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
import uvicorn

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("keysystem")

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_SECRET = os.getenv("API_SECRET", "").strip()
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "").strip()

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")
if not API_SECRET:
    raise RuntimeError("API_SECRET is missing")

ADMIN_IDS = set()
for item in ADMIN_IDS_RAW.split(","):
    item = item.strip()
    if item.isdigit():
        ADMIN_IDS.add(int(item))

if not ADMIN_IDS:
    raise RuntimeError("ADMIN_IDS is missing or invalid")

app = FastAPI(title="DEVENDRAMODS X BYPASS Keysystem", version="4.0.0")
telegram_app: Application | None = None


def db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS licenses (
                id BIGSERIAL PRIMARY KEY,
                license_key TEXT UNIQUE NOT NULL,
                device_id TEXT,
                device_hash TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                activated_at TIMESTAMPTZ,
                expires_at TIMESTAMPTZ NOT NULL,
                banned BOOLEAN NOT NULL DEFAULT FALSE,
                ban_reason TEXT,
                last_seen_at TIMESTAMPTZ,
                last_ip TEXT,
                total_checks INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            ALTER TABLE licenses ADD COLUMN IF NOT EXISTS device_limit INTEGER DEFAULT 1
        """)
        conn.execute("ALTER TABLE licenses ADD COLUMN IF NOT EXISTS license_type TEXT NOT NULL DEFAULT 'FREE'")
        conn.execute("ALTER TABLE licenses ADD COLUMN IF NOT EXISTS created_by BIGINT")
        conn.execute("ALTER TABLE licenses ADD COLUMN IF NOT EXISTS note TEXT NOT NULL DEFAULT '-' ")
        conn.execute("ALTER TABLE licenses ADD COLUMN IF NOT EXISTS paused BOOLEAN NOT NULL DEFAULT FALSE")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS license_devices (
                id BIGSERIAL PRIMARY KEY,
                license_id BIGINT NOT NULL REFERENCES licenses(id) ON DELETE CASCADE,
                device_id TEXT NOT NULL,
                device_hash TEXT NOT NULL,
                first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (license_id, device_hash)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_license_devices_license
            ON license_devices(license_id)
        """)
        conn.execute("""
            INSERT INTO license_devices (license_id, device_id, device_hash)
            SELECT id, device_id, device_hash FROM licenses
            WHERE device_hash IS NOT NULL
            ON CONFLICT (license_id, device_hash) DO NOTHING
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_licenses_device_hash
            ON licenses(device_hash)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_licenses_expires_at
            ON licenses(expires_at)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_audit_logs (
                id BIGSERIAL PRIMARY KEY,
                admin_id BIGINT NOT NULL,
                action TEXT NOT NULL,
                target TEXT,
                details TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_admin_audit_logs_created
            ON admin_audit_logs(created_at DESC)
        """)
        conn.commit()


def utcnow():
    return datetime.now(timezone.utc)


def normalize_device(device_id: str) -> str:
    return device_id.strip()[:512]


def device_hash(device_id: str) -> str:
    return hashlib.sha256(device_id.encode("utf-8")).hexdigest()


def generate_key() -> str:
    alphabet = string.ascii_uppercase + string.digits
    parts = [
        "".join(secrets.choice(alphabet) for _ in range(5))
        for _ in range(4)
    ]
    return "DEVENDRAMODS-" + "-".join(parts)


def create_license(days: int):
    if days < 1 or days > 36500:
        raise ValueError("days must be between 1 and 36500")

    for _ in range(20):
        key = generate_key()
        try:
            with db() as conn:
                row = conn.execute(
                    """
                    INSERT INTO licenses (license_key, expires_at, license_type)
                    VALUES (%s, %s, 'FREE')
                    RETURNING license_key, expires_at, device_limit
                    """,
                    (key, utcnow() + timedelta(days=days)),
                ).fetchone()
                conn.commit()
                return row
        except psycopg.errors.UniqueViolation:
            continue

    raise RuntimeError("Could not generate a unique license key")


def create_custom_license(suffix: str, days: int, device_limit):
    if not 1 <= days <= 36500:
        raise ValueError("days must be between 1 and 36500")
    suffix = suffix.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{4,32}", suffix):
        raise ValueError("suffix must be 4-32 letters or digits")
    if device_limit is not None and not 1 <= device_limit <= 100000:
        raise ValueError("device limit must be 1-100000")
    key = f"DEVENDRA_{days}D_{suffix}"
    with db() as conn:
        row = conn.execute(
            """INSERT INTO licenses (license_key, expires_at, device_limit, license_type)
               VALUES (%s, %s, %s, 'CUSTOM')
               RETURNING license_key, expires_at, device_limit""",
            (key, utcnow() + timedelta(days=days), device_limit),
        ).fetchone()
        conn.commit()
        return row


def get_license(key: str):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM licenses WHERE license_key=%s",
            (key.strip().upper(),),
        ).fetchone()


def is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in ADMIN_IDS)


async def admin_only(update: Update) -> bool:
    if not is_admin(update):
        if update.effective_message:
            await update.effective_message.reply_text("⛔ Admin only.")
        return False
    return True


def license_display_data(key: str):
    with db() as conn:
        row = conn.execute(
            """SELECT l.*, COUNT(d.id) AS device_count
               FROM licenses l LEFT JOIN license_devices d ON d.license_id=l.id
               WHERE l.license_key=%s GROUP BY l.id""",
            (key.strip().upper(),),
        ).fetchone()
    return row


def format_key_card(row, *, generated: bool = False) -> str:
    """Compact, consistent license card used by /gen, /Customkey and /info."""
    now = utcnow()
    if row["banned"]:
        status = "🚫 BANNED"
    elif row.get("paused", False):
        status = "⏸ PAUSED"
    elif row["expires_at"] <= now:
        status = "❌ EXPIRED"
    else:
        status = "✅ ACTIVE"

    limit = "∞" if row["device_limit"] is None else str(row["device_limit"] or 1)
    created = row["created_at"].strftime("%d %b %Y")
    expires = row["expires_at"].strftime("%d %b %Y · %H:%M UTC")
    uid = row["created_by"] if row["created_by"] is not None else "-"
    key = html.escape(str(row["license_key"]))
    license_type = html.escape(str(row["license_type"] or "FREE"))
    note = html.escape(str(row["note"] or "-"))

    return (
        f"<b>{status}  ·  LICENSE</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🔑 <code>{key}</code>\n"
        f"🏷 Type: <b>{license_type}</b>  ·  📱 <b>{row['device_count']}/{limit}</b> devices\n"
        f"⏳ Expires: <code>{expires}</code>\n"
        f"👤 UID: <code>{uid}</code>\n"
        f"📅 Created: {created}\n"
        f"📝 Note: {note}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<i>Long-press the key above to copy it.</i>"
    )


def dashboard_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Generate", callback_data="dash:generate"),
         InlineKeyboardButton("🛠 Custom key", callback_data="dash:custom")],
        [InlineKeyboardButton("📋 All keys", callback_data="keys:0"),
         InlineKeyboardButton("🔎 Search", callback_data="dash:search")],
        [InlineKeyboardButton("📱 Devices", callback_data="dash:devices"),
         InlineKeyboardButton("📊 Statistics", callback_data="dash:stats")],
        [InlineKeyboardButton("⏳ Expiring", callback_data="dash:expiring"),
         InlineKeyboardButton("🧾 Activity", callback_data="dash:logs")],
        [InlineKeyboardButton("📖 Help & commands", callback_data="dash:help")],
    ])


def dashboard_text():
    with db() as conn:
        r = conn.execute("""SELECT COUNT(*) AS total,
            COUNT(*) FILTER (WHERE banned=FALSE AND paused=FALSE AND expires_at > NOW()) AS active,
            COUNT(*) FILTER (WHERE expires_at <= NOW()) AS expired,
            COUNT(*) FILTER (WHERE banned=TRUE) AS banned
            FROM licenses""").fetchone()
    return (
        "👑 <b>DEVENDRAMODS X</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🔐 <b>BYPASS KEYSYSTEM · ADMIN</b>\n\n"
        f"🔑 Total: <b>{r['total']}</b>   "
        f"✅ Active: <b>{r['active']}</b>\n"
        f"⏳ Expired: <b>{r['expired']}</b>   "
        f"🚫 Banned: <b>{r['banned']}</b>\n\n"
        "<i>Select a section to manage your licenses.</i>"
    )


def audit_log(admin_id: int, action: str, target: str = "-", details: str = "-"):
    with db() as conn:
        conn.execute(
            "INSERT INTO admin_audit_logs (admin_id, action, target, details) VALUES (%s,%s,%s,%s)",
            (admin_id, action[:80], target[:256], details[:1000]),
        )
        conn.commit()


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    await update.effective_message.reply_text(
        dashboard_text(),
        parse_mode="HTML",
        reply_markup=dashboard_keyboard(),
    )

async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /search <key-or-suffix>")
        return
    term = " ".join(context.args).strip().upper()
    with db() as conn:
        rows = conn.execute(
            """SELECT license_key FROM licenses
               WHERE UPPER(license_key) LIKE %s
               ORDER BY id DESC LIMIT 20""",
            (f"%{term}%",),
        ).fetchall()
    if not rows:
        await update.effective_message.reply_text("🔎 No matching keys found.")
        return
    await update.effective_message.reply_text(
        "🔎 <b>Search results</b>\n\n" +
        "\n".join(f"• <code>{r['license_key']}</code>" for r in rows),
        parse_mode="HTML",
    )


async def cmd_expiring(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    with db() as conn:
        rows = conn.execute(
            """SELECT license_key, expires_at FROM licenses
               WHERE banned=FALSE AND expires_at > NOW()
                 AND expires_at <= NOW() + INTERVAL '7 days'
               ORDER BY expires_at ASC LIMIT 30"""
        ).fetchall()
    if not rows:
        await update.effective_message.reply_text("✅ No keys expire in the next 7 days.")
        return
    lines = [f"• <code>{r['license_key']}</code> — {r['expires_at'].strftime('%Y-%m-%d %H:%M UTC')}" for r in rows]
    await update.effective_message.reply_text("⏳ <b>Expiring within 7 days</b>\n\n" + "\n".join(lines), parse_mode="HTML")


async def dashboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(update):
        await query.answer("Admin only.", show_alert=True)
        return
    data = query.data or ""
    if not data.startswith("bulk:"):
        await query.answer()
    if data == "dash:home":
        await query.edit_message_text(
            dashboard_text(), parse_mode="HTML", reply_markup=dashboard_keyboard()
        )
        return
    if data == "dash:generate":
        await query.edit_message_text("🔑 <b>Generate keys</b>\nUse: <code>/gen &lt;days&gt; [count]</code>\nExample: <code>/gen 30 5</code>", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Dashboard", callback_data="dash:home")]]))
        return
    if data == "dash:custom":
        await query.edit_message_text("🛠 <b>Custom key</b>\nUse: <code>/Customkey &lt;suffix&gt; &lt;days&gt; &lt;device_no|Unlimited&gt;</code>\nExample: <code>/Customkey ABCD1234 30 Unlimited</code>", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Dashboard", callback_data="dash:home")]]))
        return
    if data == "dash:help":
        text = ("👑 <b>DEVENDRAMODS X BYPASS Keysystem</b>\n\n"
                "/gen &lt;days&gt; [count]\n/Customkey &lt;suffix&gt; &lt;days&gt; &lt;device_no|Unlimited&gt;\n"
                "/reset &lt;key&gt;\n/ban &lt;key&gt; [reason]\n/unban &lt;key&gt;\n"
                "/extend &lt;key&gt; &lt;days&gt;\n/delete &lt;key&gt;\n/info &lt;key&gt;\n"
                "/search &lt;key-or-suffix&gt;\n/list\n/expiring\n/stats")
        await query.edit_message_text(text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Dashboard", callback_data="dash:home")]]))
        return
    if data == "dash:search":
        await query.edit_message_text(
            "🔎 <b>Search licenses</b>\nUse <code>/search KEY_OR_SUFFIX</code> to find a license.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Dashboard", callback_data="dash:home")]])
        )
        return
    if data == "dash:devices":
        with db() as conn:
            r = conn.execute("""SELECT COUNT(*) AS total,
                COUNT(DISTINCT license_id) AS licenses FROM license_devices""").fetchone()
        await query.edit_message_text(
            "📱 <b>Device overview</b>\n\n"
            f"Registered devices: <b>{r['total']}</b>\n"
            f"Licenses with devices: <b>{r['licenses']}</b>\n\n"
            "Use <code>/info KEY</code> to inspect a license and <code>/reset KEY</code> to clear its device bindings.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Dashboard", callback_data="dash:home")]])
        )
        return
    if data == "dash:stats":
        with db() as conn:
            r = conn.execute("""SELECT COUNT(*) AS total,
                COUNT(*) FILTER (WHERE banned) AS banned,
                COUNT(*) FILTER (WHERE expires_at > NOW() AND banned=FALSE AND paused=FALSE) AS active,
                COUNT(*) FILTER (WHERE expires_at <= NOW()) AS expired,
                COUNT(*) FILTER (WHERE expires_at > NOW() AND expires_at <= NOW()+INTERVAL '7 days' AND banned=FALSE AND paused=FALSE) AS expiring
                FROM licenses""").fetchone()
        text = (f"📊 <b>Keysystem statistics</b>\n\n"
                f"🔑 Total: <b>{r['total']}</b>\n✅ Active: <b>{r['active']}</b>\n"
                f"⏳ Expiring in 7 days: <b>{r['expiring']}</b>\n❌ Expired: <b>{r['expired']}</b>\n"
                f"🚫 Banned: <b>{r['banned']}</b>")
        await query.edit_message_text(text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Dashboard", callback_data="dash:home")]]))
        return
    if data == "dash:expiring":
        with db() as conn:
            rows = conn.execute("""SELECT license_key, expires_at FROM licenses
                WHERE banned=FALSE AND expires_at > NOW() AND expires_at <= NOW()+INTERVAL '7 days'
                ORDER BY expires_at ASC LIMIT 15""").fetchall()
        text = "⏳ <b>Expiring within 7 days</b>\n\n"
        text += "\n".join(f"• <code>{r['license_key']}</code> — {r['expires_at'].strftime('%m-%d %H:%M UTC')}" for r in rows) if rows else "No keys expiring soon."
        await query.edit_message_text(text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Dashboard", callback_data="dash:home")]]))
        return
    if data == "dash:logs":
        with db() as conn:
            rows = conn.execute("SELECT admin_id, action, target, created_at FROM admin_audit_logs ORDER BY id DESC LIMIT 10").fetchall()
        text = "🧾 <b>Recent admin activity</b>\n\n"
        text += "\n".join(f"• {r['action']} — <code>{r['target'] or '-'}</code>\n  {r['created_at'].strftime('%m-%d %H:%M UTC')} · UID {r['admin_id']}" for r in rows) if rows else "No activity recorded yet."
        await query.edit_message_text(text[:4000], parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Dashboard", callback_data="dash:home")]]))
        return
    if data.startswith("bulk:"):
        selected = context.user_data.setdefault("selected_license_ids", set())
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        page = 0
        try:
            if action == "single":
                operation, license_id = parts[2], int(parts[3])
                single_sql = {
                    "ban": "UPDATE licenses SET banned=TRUE, ban_reason='Banned by admin' WHERE id=%s",
                    "unban": "UPDATE licenses SET banned=FALSE, ban_reason=NULL WHERE id=%s",
                    "pause": "UPDATE licenses SET paused=TRUE WHERE id=%s AND banned=FALSE",
                    "resume": "UPDATE licenses SET paused=FALSE WHERE id=%s",
                }
                if operation not in single_sql:
                    await query.answer("Unknown action.", show_alert=True)
                    return
                with db() as conn:
                    cur = conn.execute(single_sql[operation], (license_id,))
                    conn.commit()
                audit_log(update.effective_user.id, f"SINGLE_{operation.upper()}", str(license_id))
                await query.answer("Updated." if cur.rowcount else "No change / license not found.", show_alert=True)
                with db() as conn:
                    found = conn.execute("SELECT license_key FROM licenses WHERE id=%s", (license_id,)).fetchone()
                if found:
                    row = license_display_data(found["license_key"])
                    await query.edit_message_text(format_key_card(row), parse_mode="HTML", reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🚫 Ban", callback_data=f"bulk:single:ban:{license_id}"), InlineKeyboardButton("✅ Unban", callback_data=f"bulk:single:unban:{license_id}")],
                        [InlineKeyboardButton("⏸ Pause", callback_data=f"bulk:single:pause:{license_id}"), InlineKeyboardButton("▶️ Resume", callback_data=f"bulk:single:resume:{license_id}")],
                        [InlineKeyboardButton("📋 Back to keys", callback_data="keys:0")],
                        [InlineKeyboardButton("🏠 Dashboard", callback_data="dash:home")],
                    ]))
                return
            if action == "toggle":
                license_id, page = int(parts[2]), int(parts[3])
                if license_id in selected:
                    selected.remove(license_id)
                else:
                    selected.add(license_id)
            elif action == "page":
                page = int(parts[2])
                with db() as conn:
                    page_ids = conn.execute(
                        "SELECT id FROM licenses ORDER BY id DESC LIMIT 8 OFFSET %s", (page * 8,)
                    ).fetchall()
                ids = {r["id"] for r in page_ids}
                if ids and ids.issubset(selected):
                    selected.difference_update(ids)
                else:
                    selected.update(ids)
            elif action == "clear":
                page = int(parts[2])
                selected.clear()
            elif action == "action":
                operation, page = parts[2], int(parts[3])
                ids = sorted(selected)
                if not ids:
                    await query.answer("Select at least one license first.", show_alert=True)
                    return
                if operation == "delete":
                    await query.answer("Confirm deletion in the next screen.")
                    keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton("🗑 Confirm delete", callback_data=f"bulk:delete_yes:{page}"),
                         InlineKeyboardButton("Cancel", callback_data=f"keys:{page}")],
                    ])
                    await query.edit_message_text(
                        f"⚠️ <b>Delete {len(ids)} selected licenses?</b>\nThis also removes their registered device records. This cannot be undone.",
                        parse_mode="HTML", reply_markup=keyboard)
                    return
                updates = {
                    "ban": ("UPDATE licenses SET banned=TRUE, ban_reason='Bulk action by admin' WHERE id=ANY(%s)", "BULK_BAN"),
                    "unban": ("UPDATE licenses SET banned=FALSE, ban_reason=NULL WHERE id=ANY(%s)", "BULK_UNBAN"),
                    "pause": ("UPDATE licenses SET paused=TRUE WHERE id=ANY(%s) AND banned=FALSE", "BULK_PAUSE"),
                    "resume": ("UPDATE licenses SET paused=FALSE WHERE id=ANY(%s)", "BULK_RESUME"),
                }
                if operation not in updates:
                    await query.answer("Unknown action.", show_alert=True)
                    return
                sql, log_action = updates[operation]
                with db() as conn:
                    cur = conn.execute(sql, (ids,))
                    conn.commit()
                audit_log(update.effective_user.id, log_action, f"{cur.rowcount} licenses", f"IDs: {ids[:100]}")
                selected.clear()
                await query.answer(f"Updated {cur.rowcount} license(s).", show_alert=True)
            elif action == "delete_yes":
                page = int(parts[2])
                ids = sorted(selected)
                if not ids:
                    await query.answer("Selection expired. Select licenses again.", show_alert=True)
                    return
                with db() as conn:
                    cur = conn.execute("DELETE FROM licenses WHERE id=ANY(%s)", (ids,))
                    conn.commit()
                audit_log(update.effective_user.id, "BULK_DELETE", f"{cur.rowcount} licenses", f"IDs: {ids[:100]}")
                selected.clear()
                await query.answer(f"Deleted {cur.rowcount} license(s).", show_alert=True)
            if action in {"toggle", "page", "clear"}:
                await query.answer("Selection updated.")
        except (ValueError, IndexError):
            await query.answer("Invalid bulk action.", show_alert=True)
            return
        # Refresh the license manager after selection or an action.
        data = f"keys:{page}"

    if data.startswith("keys:"):
        try:
            page = max(0, min(int(data.split(":", 1)[1]), 100000))
        except ValueError:
            page = 0
        per_page = 8
        offset = page * per_page
        selected = context.user_data.setdefault("selected_license_ids", set())
        with db() as conn:
            total = conn.execute("SELECT COUNT(*) AS n FROM licenses").fetchone()["n"]
            rows = conn.execute(
                """SELECT l.id, l.license_key, l.expires_at, l.banned, l.paused, l.device_limit,
                          COUNT(d.id) AS device_count
                   FROM licenses l LEFT JOIN license_devices d ON d.license_id=l.id
                   GROUP BY l.id ORDER BY l.id DESC LIMIT %s OFFSET %s""",
                (per_page, offset),
            ).fetchall()
        if not rows and page > 0:
            page -= 1
            offset = page * per_page
            with db() as conn:
                rows = conn.execute(
                    """SELECT l.id, l.license_key, l.expires_at, l.banned, l.paused, l.device_limit,
                              COUNT(d.id) AS device_count
                       FROM licenses l LEFT JOIN license_devices d ON d.license_id=l.id
                       GROUP BY l.id ORDER BY l.id DESC LIMIT %s OFFSET %s""",
                    (per_page, offset),
                ).fetchall()
        keyboard = []
        for r in rows:
            limit = "∞" if r["device_limit"] is None else str(r["device_limit"] or 1)
            status = "🚫" if r["banned"] else ("⏸" if r["paused"] else ("❌" if r["expires_at"] <= utcnow() else "✅"))
            short_key = r["license_key"] if len(r["license_key"]) <= 25 else r["license_key"][:22] + "…"
            checked = "☑️" if r["id"] in selected else "⬜"
            keyboard.append([InlineKeyboardButton(
                f"{checked} {status} {short_key} · {r['device_count']}/{limit}",
                callback_data=f"bulk:toggle:{r['id']}:{page}"
            )])
            keyboard.append([InlineKeyboardButton(
                f"🔎 View {short_key}", callback_data=f"keyview:{r['id']}"
            )])
        if rows:
            keyboard.append([InlineKeyboardButton("☑️ Select / deselect this page", callback_data=f"bulk:page:{page}"),
                             InlineKeyboardButton("✖ Clear selection", callback_data=f"bulk:clear:{page}")])
        if selected:
            keyboard.extend([
                [InlineKeyboardButton(f"🚫 Ban ({len(selected)})", callback_data=f"bulk:action:ban:{page}"),
                 InlineKeyboardButton(f"✅ Unban ({len(selected)})", callback_data=f"bulk:action:unban:{page}")],
                [InlineKeyboardButton(f"⏸ Pause ({len(selected)})", callback_data=f"bulk:action:pause:{page}"),
                 InlineKeyboardButton(f"▶️ Resume ({len(selected)})", callback_data=f"bulk:action:resume:{page}")],
                [InlineKeyboardButton(f"🗑 Delete ({len(selected)})", callback_data=f"bulk:action:delete:{page}")],
            ])
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"keys:{page-1}"))
        if offset + per_page < total:
            nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"keys:{page+1}"))
        if nav:
            keyboard.append(nav)
        keyboard.append([InlineKeyboardButton("🏠 Dashboard", callback_data="dash:home")])
        text = (
            "📋 <b>LICENSE MANAGER</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Page <b>{page + 1}</b> · <b>{total}</b> licenses\n"
            f"Selected: <b>{len(selected)}</b>\n\n"
            "Tap a license row to select it. Use View to inspect details."
            if rows else "📋 <b>LICENSE MANAGER</b>\n\nNo licenses found."
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data.startswith("keyview:"):
        try:
            license_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        with db() as conn:
            found = conn.execute("SELECT license_key FROM licenses WHERE id=%s", (license_id,)).fetchone()
        if not found:
            await query.edit_message_text("License no longer exists.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to keys", callback_data="keys:0")]]))
            return
        row = license_display_data(found["license_key"])
        state_buttons = [
            InlineKeyboardButton("🚫 Ban", callback_data=f"bulk:single:ban:{license_id}"),
            InlineKeyboardButton("✅ Unban", callback_data=f"bulk:single:unban:{license_id}"),
        ]
        pause_buttons = [
            InlineKeyboardButton("⏸ Pause", callback_data=f"bulk:single:pause:{license_id}"),
            InlineKeyboardButton("▶️ Resume", callback_data=f"bulk:single:resume:{license_id}"),
        ]
        keyboard = InlineKeyboardMarkup([
            state_buttons, pause_buttons,
            [InlineKeyboardButton("📋 Back to keys", callback_data="keys:0")],
            [InlineKeyboardButton("🏠 Dashboard", callback_data="dash:home")]
        ])
        await query.edit_message_text(format_key_card(row), parse_mode="HTML", reply_markup=keyboard)
        return



async def cmd_gen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    try:
        days = int(context.args[0])
        count = int(context.args[1]) if len(context.args) > 1 else 1
        if count < 1 or count > 100:
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /gen <days> [count]\nExample: /gen 30 5")
        return

    cards = []
    for _ in range(count):
        created = create_license(days)
        with db() as conn:
            conn.execute(
                "UPDATE licenses SET created_by=%s WHERE license_key=%s",
                (update.effective_user.id, created["license_key"]),
            )
            conn.commit()
        row = license_display_data(created["license_key"])
        cards.append(format_key_card(row, generated=True))

    await update.message.reply_text(
        "\n\n━━━━━━━━━━━━━━━━\n\n".join(cards),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 Dashboard", callback_data="dash:home"),
            InlineKeyboardButton("📋 All keys", callback_data="keys:0"),
        ]]),
    )


async def cmd_customkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    try:
        if len(context.args) != 3:
            raise ValueError
        suffix = context.args[0]
        days = int(context.args[1])
        device_arg = context.args[2]
        limit = None if device_arg.lower() == "unlimited" else int(device_arg)
        row = create_custom_license(suffix, days, limit)
    except psycopg.errors.UniqueViolation:
        await update.effective_message.reply_text("❌ That suffix already exists. Choose another.")
        return
    except ValueError:
        await update.effective_message.reply_text(
            "Usage: /Customkey <suffix> <days> <device_no|Unlimited>\n"
            "Example: /Customkey ABCD1234 30 3\n"
            "Example: /Customkey ABCD1234 30 Unlimited"
        )
        return
    with db() as conn:
        conn.execute(
            "UPDATE licenses SET created_by=%s WHERE license_key=%s",
            (update.effective_user.id, row["license_key"]),
        )
        conn.commit()
    full_row = license_display_data(row["license_key"])
    audit_log(update.effective_user.id, "CUSTOM_KEY", row["license_key"], f"{days} day(s), device_limit={limit if limit is not None else 'Unlimited'}")
    await update.effective_message.reply_text(
        format_key_card(full_row, generated=True), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 Dashboard", callback_data="dash:home"),
            InlineKeyboardButton("📋 All keys", callback_data="keys:0"),
        ]])
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /reset <key>")
        return

    key = context.args[0].strip().upper()
    with db() as conn:
        lic = conn.execute("SELECT id FROM licenses WHERE license_key=%s", (key,)).fetchone()
        if lic:
            conn.execute("DELETE FROM license_devices WHERE license_id=%s", (lic["id"],))
        cur = conn.execute(
            """
            UPDATE licenses
            SET device_id=NULL, device_hash=NULL, activated_at=NULL,
                last_seen_at=NULL, last_ip=NULL
            WHERE license_key=%s
            """,
            (key,),
        )
        conn.commit()

    if cur.rowcount:
        audit_log(update.effective_user.id, "RESET_DEVICES", key)
    await update.message.reply_text(
        "✅ Device binding reset." if cur.rowcount else "❌ Key not found."
    )


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /ban <key> [reason]")
        return

    key = context.args[0].strip().upper()
    reason = " ".join(context.args[1:]).strip() or "Banned by admin"

    with db() as conn:
        cur = conn.execute(
            "UPDATE licenses SET banned=TRUE, ban_reason=%s WHERE license_key=%s",
            (reason, key),
        )
        conn.commit()

    if cur.rowcount:
        audit_log(update.effective_user.id, "BAN", key, reason)
    await update.message.reply_text(
        "🚫 Key banned." if cur.rowcount else "❌ Key not found."
    )


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /unban <key>")
        return

    key = context.args[0].strip().upper()
    with db() as conn:
        cur = conn.execute(
            "UPDATE licenses SET banned=FALSE, ban_reason=NULL WHERE license_key=%s",
            (key,),
        )
        conn.commit()

    if cur.rowcount:
        audit_log(update.effective_user.id, "UNBAN", key)
    await update.message.reply_text(
        "✅ Key unbanned." if cur.rowcount else "❌ Key not found."
    )


async def cmd_extend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    try:
        key = context.args[0].strip().upper()
        days = int(context.args[1])
        if days < 1 or days > 36500:
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /extend <key> <days>")
        return

    with db() as conn:
        cur = conn.execute(
            """
            UPDATE licenses
            SET expires_at =
                CASE
                    WHEN expires_at > NOW()
                    THEN expires_at + (%s * INTERVAL '1 day')
                    ELSE NOW() + (%s * INTERVAL '1 day')
                END
            WHERE license_key=%s
            """,
            (days, days, key),
        )
        conn.commit()

    if cur.rowcount:
        audit_log(update.effective_user.id, "EXTEND", key, f"{days} day(s)")
    await update.message.reply_text(
        "✅ Expiry extended." if cur.rowcount else "❌ Key not found."
    )


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /delete <key>")
        return

    key = context.args[0].strip().upper()
    with db() as conn:
        cur = conn.execute(
            "DELETE FROM licenses WHERE license_key=%s",
            (key,),
        )
        conn.commit()

    if cur.rowcount:
        audit_log(update.effective_user.id, "DELETE", key)
    await update.message.reply_text(
        "🗑 Key deleted." if cur.rowcount else "❌ Key not found."
    )


async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /info <key>")
        return

    row = get_license(context.args[0].strip().upper())
    if not row:
        await update.message.reply_text("❌ Key not found.")
        return

    full_row = license_display_data(row["license_key"])
    await update.message.reply_text(
        format_key_card(full_row), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 Dashboard", callback_data="dash:home"),
            InlineKeyboardButton("📋 All keys", callback_data="keys:0"),
        ]])
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    with db() as conn:
        rows = conn.execute(
            """
            SELECT l.license_key, l.expires_at, l.banned, l.paused, l.device_limit,
                   COUNT(d.id) AS device_count
            FROM licenses l
            LEFT JOIN license_devices d ON d.license_id=l.id
            GROUP BY l.id
            ORDER BY l.id DESC
            LIMIT 50
            """
        ).fetchall()

    if not rows:
        await update.message.reply_text("No licenses.")
        return

    lines = []
    for r in rows:
        lim = "Unlimited" if r["device_limit"] is None else str(r["device_limit"])
        status = "BANNED" if r["banned"] else ("PAUSED" if r["paused"] else f'{r["device_count"]}/{lim} devices') 
        lines.append(
            f"{r['license_key']} | {status} | {r['expires_at'].date()}"
        )

    await update.message.reply_text("\n".join(lines))


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return

    with db() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE banned=TRUE) AS banned,
                (SELECT COUNT(DISTINCT license_id) FROM license_devices) AS bound,
                COUNT(*) FILTER (WHERE expires_at > NOW() AND banned=FALSE AND paused=FALSE) AS active,
                COUNT(*) FILTER (WHERE expires_at <= NOW()) AS expired
            FROM licenses
            """
        ).fetchone()

    await update.message.reply_text(
        "📊 Keysystem Stats\n\n"
        f"Total: {row['total']}\n"
        f"Active: {row['active']}\n"
        f"Expired: {row['expired']}\n"
        f"Bound: {row['bound']}\n"
        f"Banned: {row['banned']}"
    )


class VerifyRequest(BaseModel):
    key: str = Field(min_length=1, max_length=128)
    device_id: str = Field(min_length=1, max_length=512)


class VerifyResponse(BaseModel):
    ok: bool
    message: str
    expires_at: datetime | None = None
    first_activation: bool = False


def verify_secret(value: str | None):
    if not value or not secrets.compare_digest(value, API_SECRET):
        raise HTTPException(status_code=401, detail="Invalid API secret")


@app.get("/")
def root():
    return {"ok": True, "service": "DEVENDRAMODS X BYPASS Keysystem"}


@app.get("/health")
def health():
    try:
        with db() as conn:
            conn.execute("SELECT 1")
        return {"ok": True, "database": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/api/verify", response_model=VerifyResponse)
def verify_license(
    payload: VerifyRequest,
    x_api_secret: str | None = Header(default=None),
):
    verify_secret(x_api_secret)

    key = payload.key.strip().upper()
    device = normalize_device(payload.device_id)
    dhash = device_hash(device)
    now = utcnow()

    with db() as conn:
        row = conn.execute(
            "SELECT * FROM licenses WHERE license_key=%s FOR UPDATE",
            (key,),
        ).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Invalid key")

        if row["banned"]:
            raise HTTPException(
                status_code=403,
                detail=row["ban_reason"] or "Key is banned",
            )

        if row.get("paused", False):
            raise HTTPException(status_code=403, detail="Key is paused")

        if row["expires_at"] <= now:
            raise HTTPException(status_code=403, detail="Key expired")

        existing = conn.execute(
            "SELECT id FROM license_devices WHERE license_id=%s AND device_hash=%s",
            (row["id"], dhash),
        ).fetchone()
        first_activation = False
        if existing:
            conn.execute("UPDATE license_devices SET last_seen_at=NOW() WHERE id=%s", (existing["id"],))
        else:
            device_count = conn.execute(
                "SELECT COUNT(*) AS n FROM license_devices WHERE license_id=%s",
                (row["id"],),
            ).fetchone()["n"]
            device_limit = row["device_limit"]
            if device_limit is not None and device_count >= device_limit:
                raise HTTPException(status_code=403, detail=f"Device limit reached ({device_limit})")
            conn.execute(
                """INSERT INTO license_devices (license_id, device_id, device_hash)
                   VALUES (%s, %s, %s)""",
                (row["id"], device, dhash),
            )
            first_activation = True
        conn.execute(
            """UPDATE licenses
               SET device_id=COALESCE(device_id, %s),
                   device_hash=COALESCE(device_hash, %s),
                   activated_at=COALESCE(activated_at, NOW()),
                   last_seen_at=NOW(), total_checks=total_checks+1
               WHERE id=%s""",
            (device, dhash, row["id"]),
        )
        conn.commit()

    return VerifyResponse(
        ok=True,
        message="Activated successfully" if first_activation else "Verified",
        expires_at=row["expires_at"],
        first_activation=first_activation,
    )


@app.on_event("startup")
async def startup():
    global telegram_app
    init_db()

    telegram_app = Application.builder().token(BOT_TOKEN).build()

    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("help", cmd_start))
    telegram_app.add_handler(CommandHandler("gen", cmd_gen))
    telegram_app.add_handler(CommandHandler("customkey", cmd_customkey))
    telegram_app.add_handler(CommandHandler("reset", cmd_reset))
    telegram_app.add_handler(CommandHandler("ban", cmd_ban))
    telegram_app.add_handler(CommandHandler("unban", cmd_unban))
    telegram_app.add_handler(CommandHandler("extend", cmd_extend))
    telegram_app.add_handler(CommandHandler("delete", cmd_delete))
    telegram_app.add_handler(CommandHandler("info", cmd_info))
    telegram_app.add_handler(CommandHandler("list", cmd_list))
    telegram_app.add_handler(CommandHandler("stats", cmd_stats))
    telegram_app.add_handler(CommandHandler("search", cmd_search))
    telegram_app.add_handler(CommandHandler("expiring", cmd_expiring))
    telegram_app.add_handler(CallbackQueryHandler(dashboard_callback, pattern=r"^(dash:|keys:|keyview:|bulk:)"))

    await telegram_app.initialize()
    await telegram_app.start()
    if telegram_app.updater:
        await telegram_app.updater.start_polling(drop_pending_updates=True)

    log.info("Keysystem started")


@app.on_event("shutdown")
async def shutdown():
    global telegram_app
    if telegram_app:
        if telegram_app.updater:
            await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()
        telegram_app = None


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="info")
