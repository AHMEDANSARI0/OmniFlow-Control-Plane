"""Portal revenue ops (COD confirmation, order tracking, lead scoring).

COD: the business sends a confirmation request from a conversation thread;
the customer replies YES/NO (English + Roman Urdu keywords) and OmniFlow
updates the order state, sends the follow-up message automatically and
records the outcome. Orders: the business imports an orders list (code,
status, note) and order_tracking messages are answered instantly with the
stored status. Lead scoring: a deterministic 0-100 score + temperature is
recomputed on every inbound message. All deterministic: zero AI cost.
"""

import logging
import re
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, request

from portal_auth import PortalAuthUnavailable, authenticate_portal_request
import portal_db


logger = logging.getLogger("omniflow.portal-revenue")

bp = Blueprint("portal_revenue", __name__, url_prefix="/api/v1")

COD_TABLE = "portal_cod_orders"
ORDERS_TABLE = "portal_orders"
ORDERS_MAX_ROWS = 500
AUTO_REPLY_COOLDOWN_SECONDS = 120

YES_TOKENS = frozenset({
    "yes", "y", "haan", "han", "haa", "confirm", "confirmed", "ok", "okay",
    "theek", "thik", "done", "deal", "kardo", "krdo", "ji",
})
NO_TOKENS = frozenset({
    "no", "n", "nahi", "nahin", "nai", "na", "cancel", "cancelled",
    "canceled", "radd", "mat", "stop",
})
YES_PHRASES = (
    "confirm kar do", "kar do", "kr do", "order confirm", "confirm karo",
    "pakka", "final kar do", "no problem",
)
NO_PHRASES = (
    "cancel kar do", "cancel karo", "cancel my order", "order cancel",
    "radd karo", "nahi chahiye", "nh chahiye",
)

LEAD_BASE_SCORES = {
    "purchase_intent": 50,
    "pricing": 35,
    "availability": 30,
    "order_tracking": 20,
    "appointment": 20,
    "shipping": 12,
    "general": 10,
    "complaint": 4,
    "refund_return": 4,
    "human_request": 4,
}

COD_CONFIRM_TEMPLATE = (
    "Hi {name}! Please confirm your cash-on-delivery order{details}. "
    "Reply YES to confirm or NO to cancel."
)
COD_CONFIRMED_REPLY = (
    "Thank you {name}! Your order is confirmed. We will share delivery "
    "updates right here."
)
COD_CANCELLED_REPLY = (
    "Your order request has been cancelled as requested. Message us anytime "
    "if you change your mind."
)
ORDER_FOUND_REPLY = "Hi {name}! Order {code} status: {status}{note}"
ORDER_MISSING_REPLY = (
    "Hi {name}! We could not find order {code}. Please double-check the "
    "number or message us here and we will help right away."
)


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


def _normalize_text(value) -> str:
    cleaned = re.sub(r"[^\w\s]", " ", str(value or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _first_name(contact_name) -> str:
    display = str(contact_name or "").strip()
    return display.split(" ")[0] if display else "there"


def _iso(value) -> Optional[str]:
    """ISO-8601 string for timestamps coming out of the driver, else None."""
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _cod_reply_kind(message_text):
    """Classify a COD reply as "yes", "no" or None (undecidable)."""
    normalized = _normalize_text(message_text)
    if not normalized:
        return None
    tokens = set(normalized.split(" "))
    yes_hits = sum(1 for token in tokens if token in YES_TOKENS)
    no_hits = sum(1 for token in tokens if token in NO_TOKENS)
    # Phrases are deliberate statements -> stronger signal than bare tokens.
    yes_hits += 2 * sum(1 for phrase in YES_PHRASES if phrase in normalized)
    no_hits += 2 * sum(1 for phrase in NO_PHRASES if phrase in normalized)
    if no_hits > 0 and no_hits >= yes_hits:
        return "no"
    if yes_hits > 0:
        return "yes"
    return None


def _extract_order_codes(message_text) -> List[str]:
    """Pull order-code candidates (3+ digits, optional prefix/dash) out."""
    matches = re.findall(r"#?\b([A-Z]{0,3}-?\d{3,10})\b",
                         str(message_text or "").upper())
    codes: List[str] = []
    for match in matches[:3]:
        code = match.strip("-").strip()
        if code and code not in codes:
            codes.append(code)
    return codes


def _send_command(cur, client_id, external_user_id, display_name, body,
                  source):
    """Queue a WhatsApp send through the existing connector command queue."""
    payload: Dict[str, Any] = {
        "external_user_id": external_user_id,
        "body": str(body or "")[:1000],
        "source": source,
    }
    display = str(display_name or "").strip()
    if display:
        payload["target_display_name"] = display
    import json

    cur.execute(
        "INSERT INTO " + portal_db._q(portal_db.CMD_TABLE) +
        " (client_id, channel, action, payload, status, requested_by,"
        " created_at, updated_at) "
        "VALUES (%s, 'whatsapp', 'send_message', CAST(%s AS JSONB),"
        " 'pending', NULL, NOW(), NOW()) "
        "RETURNING id",
        (client_id, json.dumps(payload)),
    )


def _recent_outbound(cur, conversation_id, seconds):
    cur.execute(
        "SELECT 1 FROM " + portal_db._q(portal_db.MSGS_TABLE) +
        " WHERE conversation_id = %s AND direction = 'out'"
        " AND created_at > NOW() - make_interval(secs => %s)"
        " LIMIT 1",
        (conversation_id, seconds),
    )
    return bool(portal_db.rows(cur))


def _cod_public(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": int(row.get("id") or 0),
        "status": str(row.get("status") or "pending"),
        "details": str(row.get("details") or ""),
        "attempts": int(row.get("attempts") or 1),
        "created_at": _iso(row.get("created_at")),
        "answered_at": _iso(row.get("answered_at")),
    }


def recompute_lead_score(cur, client_id, conversation_id, intent):
    """Deterministic 0-100 lead score + temperature for one conversation."""
    base = LEAD_BASE_SCORES.get(str(intent or ""), 8)
    cur.execute(
        "SELECT COUNT(*) AS c FROM " + portal_db._q(portal_db.MSGS_TABLE) +
        " WHERE conversation_id = %s AND direction = 'in'"
        " AND created_at > NOW() - make_interval(days => 7)",
        (conversation_id,),
    )
    count_rows = portal_db.rows(cur)
    inbound_week = int((count_rows[0].get("c") if count_rows else 0) or 0)
    score = min(100, base + min(20, 3 * inbound_week))
    if score >= 65:
        temp = "hot"
    elif score >= 35:
        temp = "warm"
    else:
        temp = "cold"
    cur.execute(
        "UPDATE " + portal_db._q(portal_db.CONV_TABLE) +
        " SET lead_score = %s, lead_temp = %s"
        " WHERE id = %s AND client_id = %s",
        (score, temp, conversation_id, client_id),
    )


def _handle_cod_reply(cur, client_id, conversation_id, external_user_id,
                      contact_name, message_text, intent) -> bool:
    """Transition a pending COD order on YES/NO. True = handled."""
    if intent in ("human_request", "complaint", "refund_return"):
        return False
    cur.execute(
        "SELECT id, details FROM " + portal_db._q(COD_TABLE) +
        " WHERE client_id = %s AND conversation_id = %s"
        " AND status = 'pending' ORDER BY id DESC LIMIT 1",
        (client_id, conversation_id),
    )
    pending = portal_db.rows(cur)
    if not pending:
        return False
    kind = _cod_reply_kind(message_text)
    if kind is None:
        return False
    new_status = "confirmed" if kind == "yes" else "cancelled"
    cur.execute(
        "UPDATE " + portal_db._q(COD_TABLE) +
        " SET status = %s, answered_at = NOW(), updated_at = NOW()"
        " WHERE id = %s AND status = 'pending'"
        " RETURNING id",
        (new_status, pending[0].get("id")),
    )
    if not portal_db.rows(cur):
        return False
    portal_db.log_action(
        cur,
        client_id,
        "cod." + new_status,
        "system",
        None,
        conversation_id,
        ("COD order confirmed by customer." if kind == "yes"
         else "COD order cancelled by customer.")[:200],
    )
    reply = COD_CONFIRMED_REPLY if kind == "yes" else COD_CANCELLED_REPLY
    _send_command(cur, client_id, external_user_id, contact_name,
                  reply.replace("{name}", _first_name(contact_name)),
                  "cod_confirmation")
    return True


def _handle_order_lookup(cur, client_id, conversation_id, external_user_id,
                         contact_name, message_text) -> None:
    codes = _extract_order_codes(message_text)
    if not codes:
        return
    if _recent_outbound(cur, conversation_id, AUTO_REPLY_COOLDOWN_SECONDS):
        return
    cur.execute(
        "SELECT id, code, status_text, note FROM " + portal_db._q(ORDERS_TABLE) +
        " WHERE client_id = %s AND code = ANY(%s) LIMIT 1",
        (client_id, codes),
    )
    found = portal_db.rows(cur)
    display = _first_name(contact_name)
    if found:
        order = found[0]
        note = str(order.get("note") or "").strip()
        body = ORDER_FOUND_REPLY.format(
            name=display,
            code=str(order.get("code") or ""),
            status=str(order.get("status_text") or "pending"),
            note=(" — " + note) if note else "",
        )
    else:
        body = ORDER_MISSING_REPLY.format(name=display, code=codes[0])
    _send_command(cur, client_id, external_user_id, contact_name, body,
                  "order_tracking")
    portal_db.log_action(
        cur,
        client_id,
        "order.lookup",
        "system",
        None,
        conversation_id,
        ("Order " + str(found[0].get("code")) + ": "
         + str(found[0].get("status_text")) if found
         else "Order " + codes[0] + " not found")[:200],
    )


def handle_inbound(client_id, conversation_id, contact_id, contact_name,
                   message_text, intent, conn):
    """COD reply transition, order lookup and lead scoring, in the ingest
    transaction. Never raises into the caller: wrap in try/except there."""
    external_user_id = str(contact_id or "").strip()
    if not external_user_id:
        return
    with conn.cursor() as cur:
        handled_cod = _handle_cod_reply(
            cur, client_id, conversation_id, external_user_id,
            contact_name, message_text, intent,
        )
        if not handled_cod and intent == "order_tracking":
            _handle_order_lookup(
                cur, client_id, conversation_id, external_user_id,
                contact_name, message_text,
            )
        recompute_lead_score(cur, client_id, conversation_id, intent)


@bp.post("/portal/conversations/<int:conversation_id>/cod")
def request_cod_confirmation(conversation_id: int):
    principal, error = _principal_or_error()
    if error is not None:
        return error
    payload = request.get_json(silent=True) or {}
    details = str(payload.get("details") or "").strip()
    if len(details) > 200:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Details must be 200 characters or fewer."}}), 400
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT contact_id, contact_name FROM "
                    + portal_db._q(portal_db.CONV_TABLE) +
                    " WHERE id = %s AND client_id = %s LIMIT 1",
                    (conversation_id, principal["client_id"]),
                )
                conv_rows = portal_db.rows(cur)
                if not conv_rows:
                    return jsonify({"error": {"code": "not_found",
                                              "message": "Conversation not found."}}), 404
                external_user_id = str(conv_rows[0].get("contact_id") or "").strip()
                if not external_user_id:
                    return jsonify({"error": {"code": "bad_request",
                                              "message": "This conversation has no deliverable contact."}}), 400
                display_name = str(conv_rows[0].get("contact_name") or "").strip()
                details_suffix = " (" + details + ")" if details else ""
                body = COD_CONFIRM_TEMPLATE.format(
                    name=_first_name(display_name),
                    details=details_suffix,
                )
                _send_command(cur, principal["client_id"], external_user_id,
                              display_name, body, "cod_confirmation")
                cur.execute(
                    "INSERT INTO " + portal_db._q(COD_TABLE) +
                    " (client_id, conversation_id, contact_id, contact_name,"
                    " status, details, attempts)"
                    " VALUES (%s, %s, %s, %s, 'pending', %s, 1)"
                    " ON CONFLICT (conversation_id) DO UPDATE SET"
                    " status = 'pending', details = EXCLUDED.details,"
                    " attempts = " + portal_db._q(COD_TABLE) + ".attempts + 1,"
                    " answered_at = NULL, updated_at = NOW()"
                    " RETURNING id, status, details, attempts, created_at,"
                    " answered_at",
                    (principal["client_id"], conversation_id,
                     external_user_id, display_name or None, details),
                )
                cod_rows = portal_db.rows(cur)
                portal_db.log_action(
                    cur,
                    principal["client_id"],
                    "cod.requested",
                    "customer_user",
                    principal.get("user_id"),
                    conversation_id,
                    ("COD confirmation sent: " + details) if details
                    else "COD confirmation sent.",
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "portal cod request")[0]), 503
    return jsonify({"ok": True, "cod": _cod_public(cod_rows[0] if cod_rows else {})}), 200


@bp.get("/portal/conversations/<int:conversation_id>/cod")
def get_cod_confirmation(conversation_id: int):
    principal, error = _principal_or_error()
    if error is not None:
        return error
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM " + portal_db._q(portal_db.CONV_TABLE) +
                    " WHERE id = %s AND client_id = %s LIMIT 1",
                    (conversation_id, principal["client_id"]),
                )
                if not portal_db.rows(cur):
                    return jsonify({"error": {"code": "not_found",
                                              "message": "Conversation not found."}}), 404
                cur.execute(
                    "SELECT id, status, details, attempts, created_at,"
                    " answered_at FROM " + portal_db._q(COD_TABLE) +
                    " WHERE client_id = %s AND conversation_id = %s"
                    " ORDER BY id DESC LIMIT 1",
                    (principal["client_id"], conversation_id),
                )
                cod_rows = portal_db.rows(cur)
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "portal cod read")[0]), 503
    return jsonify({
        "cod": _cod_public(cod_rows[0]) if cod_rows else None,
    }), 200


@bp.get("/portal/orders")
def list_orders():
    principal, error = _principal_or_error()
    if error is not None:
        return error
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, code, status_text, note, updated_at FROM "
                    + portal_db._q(ORDERS_TABLE) +
                    " WHERE client_id = %s ORDER BY updated_at DESC, id DESC"
                    " LIMIT %s",
                    (principal["client_id"], ORDERS_MAX_ROWS),
                )
                order_rows = portal_db.rows(cur)
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "portal orders")[0]), 503
    items = [
        {
            "id": int(row.get("id") or 0),
            "code": str(row.get("code") or ""),
            "status_text": str(row.get("status_text") or ""),
            "note": str(row.get("note") or ""),
            "updated_at": _iso(row.get("updated_at")),
        }
        for row in order_rows
    ]
    return jsonify({"items": items}), 200


@bp.post("/portal/orders/import")
def import_orders():
    principal, error = _principal_or_error()
    if error is not None:
        return error
    payload = request.get_json(silent=True) or {}
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "items list is required."}}), 400
    if len(raw_items) > ORDERS_MAX_ROWS:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Import at most " + str(ORDERS_MAX_ROWS) + " orders at a time."}}), 400
    cleaned: List[Dict[str, str]] = []
    seen = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            return jsonify({"error": {"code": "bad_request",
                                      "message": "Each item must be an object."}}), 400
        code = str(raw.get("code") or "").strip().upper()
        status_text = str(raw.get("status") or "").strip() or "pending"
        note = str(raw.get("note") or "").strip()
        if not code or len(code) > 40:
            return jsonify({"error": {"code": "bad_request",
                                      "message": "Every order needs a code (max 40 characters)."}}), 400
        if len(status_text) > 60:
            return jsonify({"error": {"code": "bad_request",
                                      "message": "Status must be 60 characters or fewer."}}), 400
        if len(note) > 200:
            return jsonify({"error": {"code": "bad_request",
                                      "message": "Notes must be 200 characters or fewer."}}), 400
        if code in seen:
            continue
        seen.add(code)
        cleaned.append({"code": code, "status_text": status_text, "note": note})
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                for item in cleaned:
                    cur.execute(
                        "INSERT INTO " + portal_db._q(ORDERS_TABLE) +
                        " (client_id, code, status_text, note)"
                        " VALUES (%s, %s, %s, %s)"
                        " ON CONFLICT (client_id, code) DO UPDATE SET"
                        " status_text = EXCLUDED.status_text,"
                        " note = EXCLUDED.note, updated_at = NOW()",
                        (principal["client_id"], item["code"],
                         item["status_text"], item["note"]),
                    )
                portal_db.log_action(
                    cur,
                    principal["client_id"],
                    "orders.imported",
                    "customer_user",
                    principal.get("user_id"),
                    None,
                    "Imported " + str(len(cleaned)) + " orders.",
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "portal orders import")[0]), 503
    return jsonify({"ok": True, "imported": len(cleaned)}), 200


@bp.delete("/portal/orders/<int:order_id>")
def delete_order(order_id: int):
    principal, error = _principal_or_error()
    if error is not None:
        return error
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM " + portal_db._q(ORDERS_TABLE) +
                    " WHERE id = %s AND client_id = %s RETURNING id",
                    (order_id, principal["client_id"]),
                )
                deleted = portal_db.rows(cur)
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "portal orders delete")[0]), 503
    if not deleted:
        return jsonify({"error": {"code": "not_found",
                                  "message": "Order not found."}}), 404
    return jsonify({"ok": True}), 200
