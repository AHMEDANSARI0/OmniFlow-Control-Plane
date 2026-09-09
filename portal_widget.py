"""Public website chat widget backend.

Visitors chat from the marketing site without signing in. Messages become
portal conversations with channel "website" (contact "web_<visitor_id>")
and land in the same inbox as WhatsApp. When KB auto-reply is enabled and
an entry keyword-matches, the visitor receives an instant answer: the
reply is written directly into portal_messages and returned to the widget,
NEVER queued as a WhatsApp command. All deterministic: zero AI cost, no
bridge changes, no bot restart.
"""

import logging
import re
from typing import Any, Dict, Optional, Tuple

from flask import Blueprint, jsonify, request

import portal_db
import portal_kb
import portal_conversations
import intent_classifier


logger = logging.getLogger("omniflow.portal-widget")

bp = Blueprint("portal_widget", __name__, url_prefix="/api/v1")

WIDGET_TABLE = portal_db.WIDGET_TABLE

VISITOR_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
MAX_TEXT = 1000
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_MAX_INBOUND = 12
DEFAULT_WELCOME = "Hi! Message us here and we will reply right away."


def _iso(value) -> Optional[str]:
    """ISO-8601 string for timestamps coming out of the driver, else None."""
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _widget_client(cur) -> Optional[Dict[str, Any]]:
    """The widget settings singleton (row id = 1), seeded lazily from the
    first registered workspace so the widget works with zero setup."""
    cur.execute(
        "SELECT client_id, enabled, business_name, welcome_text,"
        " accent, position, launcher_label FROM "
        + portal_db._q(WIDGET_TABLE) + " WHERE id = 1 LIMIT 1"
    )
    rows = portal_db.rows(cur)
    if rows:
        return rows[0]
    cur.execute(
        "SELECT MIN(client_id) AS client_id FROM "
        + portal_db._q(portal_db.USERS_TABLE)
    )
    seed = portal_db.rows(cur)
    client_id = seed[0].get("client_id") if seed else None
    if client_id is None:
        return None
    cur.execute(
        "INSERT INTO " + portal_db._q(WIDGET_TABLE) +
        " (id, client_id, enabled, business_name, welcome_text,"
        " accent, position, launcher_label)"
        " VALUES (1, %s, TRUE, '', %s, %s, 'right', '')"
        " ON CONFLICT (id) DO NOTHING",
        (client_id, DEFAULT_WELCOME, DEFAULT_ACCENT),
    )
    cur.execute(
        "SELECT client_id, enabled, business_name, welcome_text,"
        " accent, position, launcher_label FROM "
        + portal_db._q(WIDGET_TABLE) + " WHERE id = 1 LIMIT 1"
    )
    rows = portal_db.rows(cur)
    return rows[0] if rows else None


def _widget_enabled(cur) -> Optional[Dict[str, Any]]:
    cfg = _widget_client(cur)
    if not cfg or not cfg.get("enabled") or cfg.get("client_id") is None:
        return None
    return cfg


def _message_public(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": int(row.get("id") or 0),
        "direction": row.get("direction") or "in",
        "body": row.get("body") or "",
        "created_at": _iso(row.get("created_at")),
    }


def _auto_answer(cur, client_id, conversation_id, message_text, intent) \
        -> Optional[Tuple[str, Any]]:
    """Instant KB answer for a website visitor: insert the outbound message
    directly (no WhatsApp command) and return (body, created_at)."""
    if intent not in portal_kb.KB_AUTO_INTENTS:
        return None
    cur.execute(
        "SELECT auto_reply FROM " + portal_db._q(portal_kb.KB_SETTINGS_TABLE) +
        " WHERE client_id = %s LIMIT 1",
        (client_id,),
    )
    settings_rows = portal_db.rows(cur)
    if not settings_rows or settings_rows[0].get("auto_reply") is not True:
        return None
    entry = portal_kb.match_knowledge_base(cur, client_id, message_text)
    if entry is None:
        return None
    rendered = str(entry.get("content") or "").replace("{name}", "there").strip()
    if not rendered:
        return None
    rendered = rendered[:1000]
    cur.execute(
        "INSERT INTO " + portal_db._q(portal_db.MSGS_TABLE) +
        " (conversation_id, client_id, direction, body, sender_name,"
        " status, intent, created_at)"
        " VALUES (%s, %s, 'out', %s, NULL, 'sent', NULL, NOW())"
        " RETURNING id, created_at",
        (conversation_id, client_id, rendered),
    )
    out_rows = portal_db.rows(cur)
    cur.execute(
        "UPDATE " + portal_db._q(portal_kb.KB_TABLE) +
        " SET usage_count = usage_count + 1 WHERE id = %s",
        (int(entry.get("id") or 0),),
    )
    cur.execute(
        "UPDATE " + portal_db._q(portal_db.CONV_TABLE) +
        " SET last_message_preview = %s, last_message_at = NOW(),"
        " updated_at = NOW() WHERE id = %s AND client_id = %s",
        (rendered[:120], conversation_id, client_id),
    )
    portal_db.log_action(
        cur,
        client_id,
        "kb.auto_reply",
        "system",
        None,
        conversation_id,
        "Website auto-answer: " + str(entry.get("title") or ""),
    )
    created = out_rows[0].get("created_at") if out_rows else None
    return rendered, created


@bp.get("/widget/config")
def widget_config():
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cfg = _widget_client(cur)
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "widget config")[0]), 503
    if not cfg or not cfg.get("enabled") or cfg.get("client_id") is None:
        return jsonify({"enabled": False}), 200
    return jsonify({
        "enabled": True,
        "business_name": str(cfg.get("business_name") or ""),
        "welcome_text": str(cfg.get("welcome_text") or "") or DEFAULT_WELCOME,
        "accent": str(cfg.get("accent") or DEFAULT_ACCENT),
        "position": cfg.get("position")
        if cfg.get("position") in WIDGET_POSITIONS
        else "right",
        "launcher_label": str(cfg.get("launcher_label") or ""),
    }), 200


@bp.post("/widget/chat")
def widget_send():
    payload = request.get_json(silent=True) or {}
    visitor = str(payload.get("visitor_id") or "")
    text = str(payload.get("text") or "").strip()
    if not VISITOR_RE.match(visitor):
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Invalid visitor."}}), 400
    if not text:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Message text is required."}}), 400
    if len(text) > MAX_TEXT:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Message too long."}}), 400
    intent = intent_classifier.classify_intent(text)
    reply_body = None
    reply_created = None
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cfg = _widget_enabled(cur)
                if not cfg:
                    return jsonify({"error": {"code": "widget_disabled",
                                              "message": "Chat is not available."}}), 403
                client_id = int(cfg["client_id"])
                cur.execute(
                    "INSERT INTO " + portal_db._q(portal_db.CONV_TABLE) +
                    " (client_id, channel, contact_id, contact_name,"
                    " status, last_message_preview, last_message_at,"
                    " last_intent, created_at, updated_at) "
                    "VALUES (%s, 'website', %s, 'Website visitor', 'open',"
                    " %s, NOW(), %s, NOW(), NOW()) "
                    "ON CONFLICT (client_id, channel, contact_id) DO UPDATE SET "
                    " last_message_preview = EXCLUDED.last_message_preview, "
                    " last_message_at = EXCLUDED.last_message_at, "
                    " last_intent = COALESCE(EXCLUDED.last_intent, "
                    " " + portal_db._q(portal_db.CONV_TABLE) + ".last_intent), "
                    " updated_at = NOW() "
                    "RETURNING id",
                    (client_id, "web_" + visitor, text[:120], intent),
                )
                conv_rows = portal_db.rows(cur)
                conversation_id = int(conv_rows[0]["id"]) if conv_rows else 0
                if not conversation_id:
                    return jsonify({"error": {"code": "widget_unavailable",
                                              "message": "Try again shortly."}}), 503
                cur.execute(
                    "SELECT COUNT(*) AS total FROM "
                    + portal_db._q(portal_db.MSGS_TABLE) +
                    " WHERE conversation_id = %s AND direction = 'in'"
                    " AND created_at > NOW() - make_interval(secs => %s)",
                    (conversation_id, RATE_LIMIT_WINDOW_SECONDS),
                )
                count_rows = portal_db.rows(cur)
                if count_rows and int(count_rows[0].get("total") or 0) >= RATE_LIMIT_MAX_INBOUND:
                    return jsonify({"error": {"code": "rate_limited",
                                              "message": "Too many messages — slow down a little."}}), 429
                cur.execute(
                    "INSERT INTO " + portal_db._q(portal_db.MSGS_TABLE) +
                    " (conversation_id, client_id, direction, body,"
                    " sender_name, status, intent, created_at)"
                    " VALUES (%s, %s, 'in', %s, NULL, 'received', %s, NOW())",
                    (conversation_id, client_id, text, intent),
                )
                auto = _auto_answer(cur, client_id, conversation_id, text, intent)
                if auto is not None:
                    reply_body, reply_created = auto
                try:
                    portal_conversations.apply_automation_rules(
                        client_id,
                        conversation_id,
                        "web_" + visitor,
                        text,
                        conn,
                    )
                except Exception:
                    pass
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "website widget chat")[0]), 503
    return jsonify({"ok": True, "reply": ({
        "body": reply_body,
        "created_at": _iso(reply_created),
    } if reply_body else None)}), 200


@bp.get("/widget/chat")
def widget_poll():
    visitor = request.args.get("visitor_id") or ""
    if not VISITOR_RE.match(visitor):
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Invalid visitor."}}), 400
    raw_since = request.args.get("since") or "0"
    try:
        since = int(raw_since)
    except (TypeError, ValueError):
        since = 0
    conversation_id = None
    messages: list = []
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cfg = _widget_enabled(cur)
                if not cfg:
                    return jsonify({"error": {"code": "widget_disabled",
                                              "message": "Chat is not available."}}), 403
                client_id = int(cfg["client_id"])
                cur.execute(
                    "SELECT id FROM " + portal_db._q(portal_db.CONV_TABLE) +
                    " WHERE client_id = %s AND channel = 'website'"
                    " AND contact_id = %s LIMIT 1",
                    (client_id, "web_" + visitor),
                )
                found = portal_db.rows(cur)
                if found:
                    conversation_id = int(found[0]["id"])
                    if since > 0:
                        cur.execute(
                            "SELECT id, direction, body, created_at FROM "
                            + portal_db._q(portal_db.MSGS_TABLE) +
                            " WHERE conversation_id = %s AND id > %s"
                            " ORDER BY id ASC LIMIT 100",
                            (conversation_id, since),
                        )
                        messages = portal_db.rows(cur)
                    else:
                        cur.execute(
                            "SELECT id, direction, body, created_at FROM "
                            + portal_db._q(portal_db.MSGS_TABLE) +
                            " WHERE conversation_id = %s"
                            " ORDER BY id DESC LIMIT 50",
                            (conversation_id,),
                        )
                        messages = list(reversed(portal_db.rows(cur)))
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "widget poll")[0]), 503
    return jsonify({
        "conversation_id": conversation_id,
        "messages": [_message_public(m) for m in messages],
    }), 200



# ---------------------------------------------------------------------------
# Portal-side widget settings (authenticated humans; API keys stay read-only)
# ---------------------------------------------------------------------------

from portal_auth import (
    PortalAuthUnavailable,
    authenticate_portal_request,
    ensure_human_principal,
)

MAX_BUSINESS_NAME = 80
MAX_WELCOME_TEXT = 200
DEFAULT_ACCENT = "#22d3ee"
WIDGET_POSITIONS = ("left", "right")
MAX_LAUNCHER_LABEL = 24
DEFAULT_LAUNCHER_LABEL = "Chat with us"


def _settings_public(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "enabled": row.get("enabled") is True,
        "business_name": str(row.get("business_name") or ""),
        "welcome_text": str(row.get("welcome_text") or ""),
        "accent": str(row.get("accent") or DEFAULT_ACCENT),
        "position": row.get("position")
        if row.get("position") in WIDGET_POSITIONS
        else "right",
        "launcher_label": str(row.get("launcher_label") or ""),
    }


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


@bp.get("/portal/widget/settings")
def portal_widget_settings_get():
    principal, error = _principal_or_error()
    if error is not None:
        return error
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cfg = _widget_client(cur)
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "widget settings")[0]), 503
    if not cfg or cfg.get("client_id") is None:
        return jsonify({"error": {"code": "no_workspace",
                                  "message": "No workspace found."}}), 400
    return jsonify({"settings": _settings_public(cfg)}), 200


@bp.put("/portal/widget/settings")
def portal_widget_settings_put():
    principal, error = _principal_or_error()
    if error is not None:
        return error
    forbidden = ensure_human_principal(principal)
    if forbidden is not None:
        return forbidden
    payload = request.get_json(silent=True) or {}
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        return jsonify({"error": {"code": "bad_request",
                                  "message": "enabled must be true or false."}}), 400
    business_name = re.sub(r"\s+", " ", str(payload.get("business_name") or "").strip())
    if len(business_name) > MAX_BUSINESS_NAME:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Business name must be 80 characters or fewer."}}), 400
    welcome_text = str(payload.get("welcome_text") or "").strip()
    if len(welcome_text) > MAX_WELCOME_TEXT:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Welcome text must be 200 characters or fewer."}}), 400
    accent = str(payload.get("accent") or "").strip().lower()
    if not accent:
        accent = DEFAULT_ACCENT
    if not re.match(r"^#[0-9a-f]{6}$", accent):
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Accent must be a hex colour like #22d3ee."}}), 400
    position = str(payload.get("position") or "right").strip().lower()
    if position not in WIDGET_POSITIONS:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Position must be left or right."}}), 400
    launcher_label = re.sub(r"\s+", " ", str(payload.get("launcher_label") or "").strip())
    if len(launcher_label) > MAX_LAUNCHER_LABEL:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Launcher label must be 24 characters or fewer."}}), 400
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cfg = _widget_client(cur)
                if not cfg or cfg.get("client_id") is None:
                    return jsonify({"error": {"code": "no_workspace",
                                              "message": "No workspace found."}}), 400
                cur.execute(
                    "UPDATE " + portal_db._q(WIDGET_TABLE) +
                    " SET enabled = %s, business_name = %s, welcome_text = %s,"
                    " accent = %s, position = %s, launcher_label = %s,"
                    " updated_at = NOW() WHERE id = 1",
                    (enabled, business_name, welcome_text, accent, position,
                     launcher_label),
                )
                portal_db.log_action(
                    cur,
                    cfg["client_id"],
                    "widget.updated",
                    "customer_user",
                    principal.get("user_id"),
                    None,
                    "Website widget " + ("enabled." if enabled else "disabled."),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "widget settings update")[0]), 503
    return jsonify({"ok": True, "settings": {
        "enabled": enabled,
        "business_name": business_name,
        "welcome_text": welcome_text,
        "accent": accent,
        "position": position,
        "launcher_label": launcher_label,
    }}), 200

