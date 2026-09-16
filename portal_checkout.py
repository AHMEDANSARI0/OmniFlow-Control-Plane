"""Smart checkout v0: shareable order-summary links the merchant can send to
a customer. Tokenized public view (no auth), server-computed totals,
merchant-marked statuses. No payments provider — the link closes the loop
over chat."""

import json
import logging
import secrets
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, request

from portal_auth import (
    PortalAuthUnavailable,
    authenticate_portal_request,
    ensure_human_principal,
)
import portal_db
import portal_growth

bp = Blueprint("portal_checkout", __name__, url_prefix="/api/v1/portal")
public_bp = Blueprint("portal_checkout_public", __name__,
                      url_prefix="/api/v1/public")


@public_bp.get("/checkout/<token>")
def public_checkout_route(token: str):
    payload, status = public_checkout(token)
    return jsonify(payload), status

logger = logging.getLogger(__name__)

LINKS_TABLE = "portal_checkout_links"

STATUSES = ("open", "paid", "cancelled", "shipped", "delivered")
SETTINGS_TABLE = "portal_checkout_settings"
NOTIFY_STATUSES = ("paid", "shipped", "delivered")
DEFAULT_TEMPLATES = {
    "paid": "Payment received for '{title}'. Thank you! Your order is"
            " being prepared.",
    "shipped": "Good news - your order '{title}' has been shipped and is on"
               " its way.",
    "delivered": "Your order '{title}' has been delivered. Thank you for"
                 " shopping with us!",
}
_SETTINGS_DDL_READY = False
CART_TABLE = "portal_cart_reminders"
_CART_DDL_READY = False
CART_SEND_CAP = 5
CART_GAP_DEFAULTS = {1: 2, 2: 24, 3: 48}
DEFAULT_CART_TEMPLATES = {
    1: "Assalam o alaikum! You left '{title}' ({total}) in your cart."
       " Complete your order before it runs out of stock.",
    2: "Still thinking about '{title}'? It is reserved for you - reply"
       " here and we will confirm your order right away.",
    3: "Last reminder: your cart '{title}' ({total}) expires soon. Pay"
       " via the checkout link or reply here to place your order.",
}
MAX_ITEMS = 20
MAX_QTY = 99
MAX_TITLE = 120

_CHECKOUT_DDL_READY = False


def _principal_or_error():
    try:
        principal = authenticate_portal_request()
    except PortalAuthUnavailable as error:
        return None, (jsonify({"error": {"code": "portal_unavailable",
                                         "message": str(error)}}), 503)
    if principal is None:
        return None, (jsonify({"error": {"code": "unauthorized",
                                         "message": "Sign in required."}}), 401)
    return principal, None


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return value.isoformat()
    except AttributeError:
        return None


def _ensure_checkout_tables(conn) -> None:
    global _CHECKOUT_DDL_READY
    if _CHECKOUT_DDL_READY:
        return
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS " + portal_db._q(LINKS_TABLE) +
            " (id BIGSERIAL PRIMARY KEY,"
            " client_id BIGINT NOT NULL,"
            " contact_id TEXT NOT NULL,"
            " token TEXT NOT NULL UNIQUE,"
            " title TEXT NOT NULL DEFAULT '',"
            " items JSONB NOT NULL DEFAULT '[]'::jsonb,"
            " total NUMERIC(12,2) NOT NULL DEFAULT 0,"
            " status TEXT NOT NULL DEFAULT 'open',"
            " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            " updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS portal_checkout_links_client_idx ON "
            + portal_db._q(LINKS_TABLE) + " (client_id, id DESC)"
        )
    conn.commit()
    _CHECKOUT_DDL_READY = True


def _ensure_settings_table(conn) -> None:
    global _SETTINGS_DDL_READY
    if _SETTINGS_DDL_READY:
        return
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS " + portal_db._q(SETTINGS_TABLE) +
            " (client_id BIGINT PRIMARY KEY,"
            " notify_enabled BOOLEAN NOT NULL DEFAULT TRUE,"
            " tpl_paid TEXT NOT NULL DEFAULT '',"
            " tpl_shipped TEXT NOT NULL DEFAULT '',"
            " tpl_delivered TEXT NOT NULL DEFAULT '',"
            " updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
        )
        for column in (
            "cart_enabled BOOLEAN NOT NULL DEFAULT FALSE",
            "cart_gap_1 INTEGER NOT NULL DEFAULT 2",
            "cart_gap_2 INTEGER NOT NULL DEFAULT 24",
            "cart_gap_3 INTEGER NOT NULL DEFAULT 48",
            "cart_tpl_1 TEXT NOT NULL DEFAULT ''",
            "cart_tpl_2 TEXT NOT NULL DEFAULT ''",
            "cart_tpl_3 TEXT NOT NULL DEFAULT ''",
        ):
            cur.execute(
                "ALTER TABLE " + portal_db._q(SETTINGS_TABLE) +
                " ADD COLUMN IF NOT EXISTS " + column
            )
    conn.commit()
    _SETTINGS_DDL_READY = True


def _load_settings(cur, client_id):
    """Notify settings for the client. Absent row or unreadable table ->
    defaults (enabled, blank templates)."""
    try:
        cur.execute(
            "SELECT notify_enabled, tpl_paid, tpl_shipped, tpl_delivered"
            " FROM " + portal_db._q(SETTINGS_TABLE) +
            " WHERE client_id = %s",
            (client_id,),
        )
        rows = portal_db.rows(cur)
    except Exception:
        rows = []
    row = rows[0] if rows else {}
    settings = {"notify_enabled": bool(row.get("notify_enabled", True))}
    for key in ("paid", "shipped", "delivered"):
        settings["tpl_" + key] = str(row.get("tpl_" + key) or "").strip()[:500]
    return settings


def _contact_name(cur, client_id, contact_id):
    """Best-effort display name from the newest conversation with this
    contact; any failure -> empty string (never blocks the update)."""
    try:
        cur.execute(
            "SELECT contact_name FROM " + portal_db._q(portal_db.CONV_TABLE) +
            " WHERE client_id = %s AND contact_id = %s"
            " ORDER BY id DESC LIMIT 1",
            (client_id, contact_id),
        )
        rows = portal_db.rows(cur)
    except Exception:
        return ""
    if not rows:
        return ""
    return str(rows[0].get("contact_name") or "").strip()


def _render_body(settings, status, contact_name, title, total):
    """Fill {name}/{title}/{total}; blank custom template -> default."""
    template = str(settings.get("tpl_" + str(status)) or "").strip()
    if not template:
        template = DEFAULT_TEMPLATES.get(str(status), "")
    clean_name = str(contact_name or "").strip()
    first_name = clean_name.split(" ")[0] if clean_name else ""
    body = (template
            .replace("{name}", first_name)
            .replace("{title}", str(title or ""))
            .replace("{total}", str(total if total is not None else "")))
    return body[:1000]


def _ensure_cart_table(conn) -> None:
    global _CART_DDL_READY
    if _CART_DDL_READY:
        return
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS " + portal_db._q(CART_TABLE) +
            " (id BIGSERIAL PRIMARY KEY,"
            " client_id BIGINT NOT NULL,"
            " link_id BIGINT NOT NULL,"
            " step INTEGER NOT NULL,"
            " sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            " UNIQUE (client_id, link_id, step))"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS portal_cart_reminders_client_idx ON "
            + portal_db._q(CART_TABLE) + " (client_id, link_id, step)"
        )
    conn.commit()
    _CART_DDL_READY = True


def _load_cart_settings(cur, client_id):
    """Cart recovery settings. Absent row or unreadable table -> defaults
    (recovery OFF until the merchant turns it on)."""
    try:
        cur.execute(
            "SELECT cart_enabled, cart_gap_1, cart_gap_2, cart_gap_3,"
            " cart_tpl_1, cart_tpl_2, cart_tpl_3 FROM "
            + portal_db._q(SETTINGS_TABLE) + " WHERE client_id = %s",
            (client_id,),
        )
        rows = portal_db.rows(cur)
    except Exception:
        rows = []
    row = rows[0] if rows else {}

    def _gap(key):
        try:
            value = int(row.get(key))
        except (TypeError, ValueError):
            return None
        return value if 1 <= value <= 168 else None

    settings = {
        "cart_enabled": bool(row.get("cart_enabled", False)) if rows else False,
        "cart_gap_1": _gap("cart_gap_1") or CART_GAP_DEFAULTS[1],
        "cart_gap_2": _gap("cart_gap_2") or CART_GAP_DEFAULTS[2],
        "cart_gap_3": _gap("cart_gap_3") or CART_GAP_DEFAULTS[3],
    }
    for key in ("cart_tpl_1", "cart_tpl_2", "cart_tpl_3"):
        settings[key] = str(row.get(key) or "").strip()[:500]
    return settings


def _render_cart_body(settings, step, title, total, name=""):
    """Fill {name}/{title}/{total}; blank custom template -> default."""
    template = str(settings.get("cart_tpl_" + str(step)) or "").strip()
    if not template:
        template = DEFAULT_CART_TEMPLATES.get(int(step), "")
    clean_name = str(name or "").strip()
    first_name = clean_name.split(" ")[0] if clean_name else ""
    return (template
            .replace("{name}", first_name)
            .replace("{title}", str(title or ""))
            .replace("{total}", str(total if total is not None else "")))[:1000]


def materialize_cart_reminders(cur, client_id, conn) -> int:
    """Time-based cart recovery: up to 3 WhatsApp reminders per open
    checkout link, riding the connector tick (like scheduled broadcasts).
    Returns the number of reminders queued. Never raises for missing
    tables - recovery silently stays off on fresh workspaces."""
    settings = _load_cart_settings(cur, client_id)
    if not settings["cart_enabled"]:
        return 0
    _ensure_cart_table(conn)
    sent = 0
    for step in (1, 2, 3):
        if sent >= CART_SEND_CAP:
            break
        gap = settings["cart_gap_" + str(step)]
        cur.execute(
            "SELECT l.id, l.contact_id, l.title, l.total"
            " FROM " + portal_db._q(LINKS_TABLE) + " l"
            " WHERE l.client_id = %s AND l.status = 'open'"
            " AND l.created_at <= NOW() - (%s || ' hours')::interval"
            " AND NOT EXISTS (SELECT 1 FROM " + portal_db._q(CART_TABLE) +
            " r WHERE r.client_id = l.client_id AND r.link_id = l.id"
            " AND r.step = %s)"
            " ORDER BY l.id ASC LIMIT %s",
            (client_id, str(gap), step, CART_SEND_CAP - sent),
        )
        due_rows = portal_db.rows(cur)
        for row in due_rows:
            if sent >= CART_SEND_CAP:
                break
            contact_id = str(row.get("contact_id") or "").strip()
            if not contact_id:
                continue
            name = _contact_name(cur, client_id, contact_id)
            body = _render_cart_body(
                settings, step, row.get("title"), row.get("total"), name)
            portal_growth._send_command(
                cur, client_id, contact_id, name, body, "cart_recovery",
            )
            cur.execute(
                "INSERT INTO " + portal_db._q(CART_TABLE) +
                " (client_id, link_id, step) VALUES (%s, %s, %s)"
                " ON CONFLICT (client_id, link_id, step) DO NOTHING",
                (client_id, int(row.get("id") or 0), step),
            )
            portal_db.log_action(
                cur,
                client_id,
                "cart.recovery_step_" + str(step),
                "system",
                None,
                None,
                ("Cart reminder " + str(step) + " queued")[:200],
            )
            sent += 1
    if sent:
        conn.commit()
    return sent


def _normalize_items(raw: Any) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Returns (items, error)."""
    if not isinstance(raw, list) or not raw:
        return None, "items (non-empty list) is required."
    if len(raw) > MAX_ITEMS:
        return None, "Max " + str(MAX_ITEMS) + " items per link."
    items: List[Dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            return None, "Each item must be an object."
        name = str(entry.get("name") or "").strip()[:100]
        qty = entry.get("qty", 1)
        price = entry.get("price")
        if not name:
            return None, "Every item needs a name."
        if isinstance(qty, bool) or not isinstance(qty, int) or not (
                1 <= qty <= MAX_QTY):
            return None, "qty must be 1-" + str(MAX_QTY) + "."
        if isinstance(price, bool) or not isinstance(price, (int, float)) or price < 0:
            return None, "price must be 0 or more."
        items.append({"name": name, "qty": qty, "price": round(float(price), 2)})
    return items, None


def _public(row: Dict[str, Any]) -> Dict[str, Any]:
    items = row.get("items")
    if not isinstance(items, list):
        items = []
    return {
        "id": int(row.get("id") or 0),
        "token": str(row.get("token") or ""),
        "contact_id": row.get("contact_id"),
        "title": str(row.get("title") or ""),
        "items": [
            {
                "name": str(item.get("name") or ""),
                "qty": int(item.get("qty") or 1),
                "price": float(item.get("price") or 0),
            }
            for item in items if isinstance(item, dict)
        ],
        "total": float(row.get("total") or 0),
        "status": row.get("status") or "open",
        "created_at": _iso(row.get("created_at")),
    }


@bp.post("/checkout/links")
def create_checkout_link():
    principal, error = _principal_or_error()
    if error:
        return error
    forbidden = ensure_human_principal(principal)
    if forbidden is not None:
        return forbidden
    payload = request.get_json(silent=True) or {}
    contact = str(payload.get("contact_id") or "").strip()[:100]
    title = str(payload.get("title") or "").strip()[:MAX_TITLE]
    items, err = _normalize_items(payload.get("items"))
    if err:
        return jsonify({"error": {"code": "bad_request",
                                  "message": err}}), 400
    if not contact:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "contact_id is required."}}), 400
    total = round(sum(item["qty"] * item["price"] for item in items), 2)
    token = secrets.token_urlsafe(16)
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            _ensure_checkout_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO " + portal_db._q(LINKS_TABLE) +
                    " (client_id, contact_id, token, title, items, total)"
                    " VALUES (%s, %s, %s, %s, CAST(%s AS JSONB), %s)"
                    " RETURNING id, token, contact_id, title, items, total,"
                    " status, created_at",
                    (principal["client_id"], contact, token, title,
                     json.dumps(items), total),
                )
                created = portal_db.rows(cur)
                portal_db.log_action(
                    cur,
                    principal["client_id"],
                    "checkout.created",
                    "customer_user",
                    principal.get("user_id"),
                    None,
                    ("Checkout link " + token[:8] + " (" + str(total) + ")")[:200],
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        logger.warning("checkout create failed: %s", error)
        return jsonify(portal_db.portal_unavailable(error, "checkout create")[0]), 503
    return jsonify({"ok": True, "link": _public(created[0] if created else {})}), 200


@bp.get("/checkout/links")
def list_checkout_links():
    principal, error = _principal_or_error()
    if error:
        return error
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            _ensure_checkout_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, token, contact_id, title, items, total,"
                    " status, created_at FROM " + portal_db._q(LINKS_TABLE) +
                    " WHERE client_id = %s ORDER BY id DESC LIMIT 30",
                    (principal["client_id"],),
                )
                rows = portal_db.rows(cur)
        finally:
            conn.close()
    except Exception as error:
        logger.warning("checkout list failed: %s", error)
        return jsonify(portal_db.portal_unavailable(error, "checkout list")[0]), 503
    return jsonify({"links": [_public(row) for row in rows]}), 200


@bp.patch("/checkout/links/<int:link_id>")
def update_checkout_link(link_id: int):
    principal, error = _principal_or_error()
    if error:
        return error
    forbidden = ensure_human_principal(principal)
    if forbidden is not None:
        return forbidden
    payload = request.get_json(silent=True) or {}
    status = payload.get("status")
    if status not in STATUSES:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Pick open, paid, cancelled,"
                                             " shipped or delivered."}}), 400
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            _ensure_checkout_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE " + portal_db._q(LINKS_TABLE) +
                    " SET status = %s, updated_at = NOW()"
                    " WHERE id = %s AND client_id = %s"
                    " RETURNING token, contact_id, title, total",
                    (status, link_id, principal["client_id"]),
                )
                updated = portal_db.rows(cur)
                if not updated:
                    return jsonify({"error": {"code": "not_found",
                                              "message": "Link not"
                                                         " found."}}), 404
                portal_db.log_action(
                    cur,
                    principal["client_id"],
                    "checkout." + str(status),
                    "customer_user",
                    principal.get("user_id"),
                    None,
                    ("Checkout " + str(updated[0].get("token"))[:8]
                     + " -> " + str(status))[:200],
                )
                notified = False
                if status in NOTIFY_STATUSES:
                    try:
                        settings = _load_settings(cur, principal["client_id"])
                        if settings["notify_enabled"]:
                            name = _contact_name(
                                cur, principal["client_id"],
                                updated[0].get("contact_id"))
                            body = _render_body(
                                settings, str(status), name,
                                updated[0].get("title"),
                                updated[0].get("total"))
                            portal_growth._send_command(
                                cur, principal["client_id"],
                                updated[0].get("contact_id"), name, body,
                                "checkout_update",
                            )
                            portal_db.log_action(
                                cur,
                                principal["client_id"],
                                "checkout.notified",
                                "customer_user",
                                principal.get("user_id"),
                                None,
                                ("Order update queued: "
                                 + str(status))[:200],
                            )
                            notified = True
                    except Exception as notify_error:
                        logger.warning(
                            "checkout notify failed: %s", notify_error)
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        logger.warning("checkout update failed: %s", error)
        return jsonify(portal_db.portal_unavailable(error, "checkout update")[0]), 503
    return jsonify({"ok": True, "status": status,
                    "notified": bool(notified)}), 200


@bp.get("/checkout/settings")
def get_checkout_settings():
    principal, error = _principal_or_error()
    if error:
        return error
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            _ensure_settings_table(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT notify_enabled, tpl_paid, tpl_shipped,"
                    " tpl_delivered, cart_enabled, cart_gap_1, cart_gap_2,"
                    " cart_gap_3, cart_tpl_1, cart_tpl_2, cart_tpl_3 FROM "
                    + portal_db._q(SETTINGS_TABLE) +
                    " WHERE client_id = %s",
                    (principal["client_id"],),
                )
                rows = portal_db.rows(cur)
        finally:
            conn.close()
    except Exception as error:
        logger.warning("checkout settings read failed: %s", error)
        return jsonify(portal_db.portal_unavailable(error, "checkout settings")[0]), 503
    row = rows[0] if rows else {}

    def _gap_out(key):
        try:
            value = int(row.get(key))
        except (TypeError, ValueError):
            return None
        return value if 1 <= value <= 168 else None

    cart = {
        "enabled": bool(row.get("cart_enabled", False)),
        "gap_1": _gap_out("cart_gap_1") or CART_GAP_DEFAULTS[1],
        "gap_2": _gap_out("cart_gap_2") or CART_GAP_DEFAULTS[2],
        "gap_3": _gap_out("cart_gap_3") or CART_GAP_DEFAULTS[3],
        "tpl_1": str(row.get("cart_tpl_1") or "").strip()[:500],
        "tpl_2": str(row.get("cart_tpl_2") or "").strip()[:500],
        "tpl_3": str(row.get("cart_tpl_3") or "").strip()[:500],
    }
    return jsonify({
        "ok": True,
        "settings": {
            "notify_enabled": bool(row.get("notify_enabled", True))
                              if rows else True,
            "tpl_paid": str(row.get("tpl_paid") or "").strip()[:500],
            "tpl_shipped": str(row.get("tpl_shipped") or "").strip()[:500],
            "tpl_delivered": str(row.get("tpl_delivered") or "").strip()[:500],
            "cart": cart if rows else {
                "enabled": False,
                "gap_1": CART_GAP_DEFAULTS[1],
                "gap_2": CART_GAP_DEFAULTS[2],
                "gap_3": CART_GAP_DEFAULTS[3],
                "tpl_1": "", "tpl_2": "", "tpl_3": "",
            },
        },
    }), 200


@bp.put("/checkout/settings")
def save_checkout_settings():
    principal, error = _principal_or_error()
    if error:
        return error
    forbidden = ensure_human_principal(principal)
    if forbidden is not None:
        return forbidden
    payload = request.get_json(silent=True) or {}
    settings_in = payload.get("settings")
    settings_in = settings_in if isinstance(settings_in, dict) else {}
    notify_enabled = settings_in.get("notify_enabled")
    if not isinstance(notify_enabled, bool):
        return jsonify({"error": {"code": "bad_request",
                                  "message": "notify_enabled must be true"
                                             " or false."}}), 400
    templates = {}
    for key in ("paid", "shipped", "delivered"):
        value = str(settings_in.get("tpl_" + key) or "").strip()
        if len(value) > 500:
            return jsonify({"error": {"code": "bad_request",
                                      "message": "Templates are capped at"
                                                 " 500 characters."}}), 400
        templates[key] = value
    cart_in = settings_in.get("cart")
    cart_provided = isinstance(cart_in, dict)
    cart_in = cart_in if cart_provided else {}
    cart_enabled = cart_in.get("enabled") is True
    gaps = {}
    for key in (1, 2, 3):
        if not cart_provided:
            gaps[key] = CART_GAP_DEFAULTS[key]
            continue
        try:
            gap = int(cart_in.get("gap_" + str(key)))
        except (TypeError, ValueError):
            gap = 0
        if not 1 <= gap <= 168:
            return jsonify({"error": {"code": "bad_request",
                                      "message": "Reminder gaps are 1 to"
                                                 " 168 hours."}}), 400
        gaps[key] = gap
    cart_templates = {}
    for key in (1, 2, 3):
        value = str(cart_in.get("tpl_" + str(key)) or "").strip()
        if len(value) > 500:
            return jsonify({"error": {"code": "bad_request",
                                      "message": "Templates are capped at"
                                                 " 500 characters."}}), 400
        cart_templates[key] = value
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            _ensure_settings_table(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO " + portal_db._q(SETTINGS_TABLE) +
                    " (client_id, notify_enabled, tpl_paid, tpl_shipped,"
                    " tpl_delivered, cart_enabled, cart_gap_1, cart_gap_2,"
                    " cart_gap_3, cart_tpl_1, cart_tpl_2, cart_tpl_3,"
                    " updated_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                    " %s, NOW())"
                    " ON CONFLICT (client_id) DO UPDATE SET"
                    " notify_enabled = EXCLUDED.notify_enabled,"
                    " tpl_paid = EXCLUDED.tpl_paid,"
                    " tpl_shipped = EXCLUDED.tpl_shipped,"
                    " tpl_delivered = EXCLUDED.tpl_delivered,"
                    " cart_enabled = EXCLUDED.cart_enabled,"
                    " cart_gap_1 = EXCLUDED.cart_gap_1,"
                    " cart_gap_2 = EXCLUDED.cart_gap_2,"
                    " cart_gap_3 = EXCLUDED.cart_gap_3,"
                    " cart_tpl_1 = EXCLUDED.cart_tpl_1,"
                    " cart_tpl_2 = EXCLUDED.cart_tpl_2,"
                    " cart_tpl_3 = EXCLUDED.cart_tpl_3,"
                    " updated_at = NOW()",
                    (principal["client_id"], notify_enabled,
                     templates["paid"], templates["shipped"],
                     templates["delivered"], cart_enabled,
                     gaps[1], gaps[2], gaps[3],
                     cart_templates[1], cart_templates[2],
                     cart_templates[3]),
                )
                portal_db.log_action(
                    cur,
                    principal["client_id"],
                    "checkout.settings",
                    "customer_user",
                    principal.get("user_id"),
                    None,
                    ("Order update settings: notify="
                     + ("on" if notify_enabled else "off"))[:200],
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        logger.warning("checkout settings save failed: %s", error)
        return jsonify(portal_db.portal_unavailable(error, "checkout settings")[0]), 503
    return jsonify({"ok": True}), 200


def public_checkout(token: str) -> Tuple[Optional[Dict[str, Any]], Optional[int]]:
    """Public (unauthenticated) token lookup. Returns (payload, status)."""
    token = str(token or "").strip()[:64]
    if not token:
        return {"error": {"code": "bad_request",
                          "message": "token is required."}}, 400
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            _ensure_checkout_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, token, contact_id, title, items, total,"
                    " status, created_at FROM " + portal_db._q(LINKS_TABLE) +
                    " WHERE token = %s LIMIT 1",
                    (token,),
                )
                rows = portal_db.rows(cur)
        finally:
            conn.close()
    except Exception as error:
        logger.warning("checkout public read failed: %s", error)
        return {"error": {"code": "portal_unavailable",
                          "message": "Try again shortly."}}, 503
    if not rows:
        return {"error": {"code": "not_found",
                          "message": "This order link is not valid."}}, 404
    view = _public(rows[0])
    view.pop("contact_id", None)
    view.pop("id", None)
    return view, 200
