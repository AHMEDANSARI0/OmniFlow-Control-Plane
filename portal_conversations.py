"""
OmniFlow Control Plane — portal conversations (customer-scoped inbox).

GET /api/v1/portal/conversations            ?status=all|open|closed&limit=50&q=search
    -> {conversations: [{id, channel, contact_id, contact_name, status,
                         last_message_at, last_message_preview, created_at,
                         unread}]}
       newest conversation first. `q` searches contact name, contact id and
       message bodies (case-insensitive). Opening a conversation marks it read.

GET /api/v1/portal/conversations/<id>
    -> {conversation: {...}, messages: [{id, direction, body, status,
                                         created_at}]}  oldest-first, last 200.
       404 when the conversation does not belong to this tenant.

Read-only for now: messages enter through the CONNECTOR ingest endpoint
(POST /api/v1/connector/whatsapp/messages, service key). Listing/detail work
for both human sessions and portal API keys (ofk_ — read-only anyway).
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from flask import Blueprint, jsonify, request

from portal_auth import (
    PortalAuthUnavailable,
    authenticate_portal_request,
    ensure_human_principal,
)
import portal_db


logger = logging.getLogger("omniflow.portal-conversations")

bp = Blueprint("portal_conversations", __name__, url_prefix="/api/v1/portal")

ALLOWED_STATUS = ("all", "open", "closed")
MESSAGE_PAGE_SIZE = 200


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return None


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


def _conversation_public(row: Any) -> dict:
    return {
        "id": row.get("id"),
        "channel": row.get("channel") or "whatsapp",
        "contact_id": row.get("contact_id"),
        "contact_name": row.get("contact_name"),
        "status": row.get("status") or "open",
        "last_message_at": _iso(row.get("last_message_at")),
        "last_message_preview": row.get("last_message_preview"),
        "created_at": _iso(row.get("created_at")),
        "unread": bool(row.get("unread")),
        "last_intent": row.get("last_intent"),
        "lead_score": int(row.get("lead_score") or 0),
        "lead_temp": row.get("lead_temp") or "cold",
        "assigned_to": row.get("assigned_to"),
        "assignee_name": row.get("assignee_name"),
    }


def _message_public(row: Any) -> dict:
    return {
        "id": row.get("id"),
        "direction": row.get("direction") or "in",
        "body": row.get("body") or "",
        "status": row.get("status") or "received",
        "intent": row.get("intent"),
        "created_at": _iso(row.get("created_at")),
    }


@bp.get("/conversations")
def list_conversations():
    principal, error = _principal_or_error()
    if error:
        return error

    status = (request.args.get("status") or "all").strip().lower()
    if status not in ALLOWED_STATUS:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "status must be all, open, or closed."}}), 400
    raw_limit = request.args.get("limit", "")
    try:
        limit = int(raw_limit) if raw_limit else 50
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(50, limit))

    search = (request.args.get("q") or "").strip()
    if len(search) > 100:
        search = search[:100]

    sql = (
        "SELECT c.id, c.channel, c.contact_id, c.contact_name, c.status,"
        " c.last_message_at, c.last_message_preview, c.created_at, c.last_intent,"
        " c.lead_score, c.lead_temp, c.assigned_to, tm.name AS assignee_name,"
        " EXISTS ("
        " SELECT 1 FROM " + portal_db._q(portal_db.MSGS_TABLE) +
        " m WHERE m.conversation_id = c.id AND m.direction = 'in'"
        " AND m.created_at > COALESCE(c.last_read_at, TO_TIMESTAMP(0))"
        " ) AS unread FROM "
        + portal_db._q(portal_db.CONV_TABLE) + " c"
        " LEFT JOIN " + portal_db._q(portal_db.TEAM_TABLE) + " tm"
        " ON tm.client_id = c.client_id AND tm.email = c.assigned_to"
        " WHERE c.client_id = %s"
    )
    params = [principal["client_id"]]
    if status != "all":
        sql += " AND c.status = %s"
        params.append(status)
    intent_filter = (request.args.get("intent") or "").strip().lower()
    if intent_filter and intent_filter != "all":
        sql += " AND c.last_intent = %s"
        params.append(intent_filter[:40])
    if search:
        like = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        sql += (
            " AND (c.contact_name ILIKE %s OR c.contact_id ILIKE %s"
            " OR EXISTS (SELECT 1 FROM " + portal_db._q(portal_db.MSGS_TABLE) +
            " sm WHERE sm.conversation_id = c.id AND sm.body ILIKE %s))"
        )
        params.extend([like, like, like])
    sql += " ORDER BY c.last_message_at DESC NULLS LAST, c.id DESC LIMIT %s"
    params.append(limit)

    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                found = portal_db.rows(cur)
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "conversations read")[0]), 503

    return jsonify({"conversations": [_conversation_public(r) for r in found]}), 200


@bp.get("/conversations/<int:conversation_id>")
def conversation_detail(conversation_id: int):
    principal, error = _principal_or_error()
    if error:
        return error

    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, channel, contact_id, contact_name, status,"
                    " last_message_at, last_message_preview, created_at,"
                    " c.lead_score, c.lead_temp, c.assigned_to,"
                    " tm.name AS assignee_name FROM "
                    + portal_db._q(portal_db.CONV_TABLE) + " c"
                    " LEFT JOIN " + portal_db._q(portal_db.TEAM_TABLE) + " tm"
                    " ON tm.client_id = c.client_id AND tm.email = c.assigned_to"
                    " WHERE c.id = %s AND c.client_id = %s LIMIT 1",
                    (conversation_id, principal["client_id"]),
                )
                found = portal_db.rows(cur)
                if not found:
                    # Other tenant's (or unknown) conversation -> plain 404.
                    return jsonify({"error": {"code": "not_found",
                                              "message": "Conversation not found."}}), 404
                cur.execute(
                    "SELECT id, direction, body, status, intent, created_at FROM "
                    + portal_db._q(portal_db.MSGS_TABLE) +
                    " WHERE conversation_id = %s ORDER BY id DESC LIMIT %s",
                    (conversation_id, MESSAGE_PAGE_SIZE),
                )
                msgs = portal_db.rows(cur)
                cur.execute(
                    "UPDATE " + portal_db._q(portal_db.CONV_TABLE) +
                    " SET last_read_at = NOW()"
                    " WHERE id = %s AND client_id = %s",
                    (conversation_id, principal["client_id"]),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "conversation detail")[0]), 503

    return jsonify({
        "conversation": _conversation_public(found[0]),
        "messages": [_message_public(m) for m in reversed(msgs)],
    }), 200


@bp.get("/conversations/intents/summary")
def conversation_intent_summary():
    principal, error = _principal_or_error()
    if error:
        return error

    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT c.last_intent AS intent, COUNT(*) AS conversations"
                    " FROM " + portal_db._q(portal_db.CONV_TABLE) +
                    " c WHERE c.client_id = %s AND c.last_intent IS NOT NULL"
                    " GROUP BY c.last_intent ORDER BY COUNT(*) DESC",
                    (principal["client_id"],),
                )
                summary = portal_db.rows(cur)
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "intent summary")[0]), 503

    return jsonify({"intents": summary}), 200


@bp.patch("/conversations/<int:conversation_id>")
def update_conversation(conversation_id: int):
    principal, error = _principal_or_error()
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    status = payload.get("status")
    if status not in ("open", "closed"):
        return jsonify({"error": {"code": "bad_request",
                                  "message": "status must be open or closed."}}), 400

    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE " + portal_db._q(portal_db.CONV_TABLE) +
                    " SET status = %s, updated_at = NOW()"
                    " WHERE id = %s AND client_id = %s"
                    " RETURNING id, channel, contact_id, contact_name, status,"
                    " last_message_at, last_message_preview, created_at,"
                    " lead_score, lead_temp",
                    (status, conversation_id, principal["client_id"]),
                )
                updated = portal_db.rows(cur)
                if updated:
                    portal_db.log_action(
                        cur,
                        principal["client_id"],
                        "conversation.status_changed",
                        "customer_user",
                        principal.get("user_id"),
                        conversation_id,
                        "Status set to " + str(status) + ".",
                    )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "conversation update")[0]), 503

    if not updated:
        # Other tenant's (or unknown) conversation -> plain 404.
        return jsonify({"error": {"code": "not_found",
                                  "message": "Conversation not found."}}), 404
    return jsonify({"conversation": _conversation_public(updated[0])}), 200


@bp.post("/conversations/<int:conversation_id>/messages")
def enqueue_conversation_message(conversation_id: int):
    principal, error = _principal_or_error()
    if error:
        return error

    forbidden = ensure_human_principal(principal)
    if forbidden:
        return forbidden

    payload = request.get_json(silent=True) or {}
    body = payload.get("body")
    if not isinstance(body, str) or not body.strip():
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Message body is required."}}), 400
    body = body.strip()
    if len(body) > 4096:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Message must be under 4096 characters."}}), 400

    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, channel, contact_id, contact_name FROM "
                    + portal_db._q(portal_db.CONV_TABLE) +
                    " WHERE id = %s AND client_id = %s LIMIT 1",
                    (conversation_id, principal["client_id"]),
                )
                found = portal_db.rows(cur)
                if not found:
                    # Other tenant's (or unknown) conversation -> plain 404.
                    return jsonify({"error": {"code": "not_found",
                                              "message": "Conversation not found."}}), 404
                contact_id = str(found[0].get("contact_id") or "").strip()
                if not contact_id:
                    return jsonify({"error": {"code": "bad_request",
                                              "message": "This conversation has no deliverable contact."}}), 400
                if (found[0].get("channel") or "") == "website":
                    # Website chats deliver inside the widget itself: store
                    # the reply directly instead of queueing a WhatsApp
                    # command (the bridge is WhatsApp-only).
                    cur.execute(
                        "INSERT INTO " + portal_db._q(portal_db.MSGS_TABLE) +
                        " (conversation_id, client_id, direction, body,"
                        " sender_name, status, created_at)"
                        " VALUES (%s, %s, 'out', %s, NULL, 'sent', NOW())",
                        (conversation_id, principal["client_id"], body),
                    )
                    cur.execute(
                        "UPDATE " + portal_db._q(portal_db.CONV_TABLE) +
                        " SET last_message_at = NOW(),"
                        " last_message_preview = %s, updated_at = NOW()"
                        " WHERE id = %s AND client_id = %s",
                        (body[:120], conversation_id, principal["client_id"]),
                    )
                    portal_db.log_action(
                        cur,
                        principal["client_id"],
                        "message.sent",
                        "customer_user",
                        principal.get("user_id"),
                        conversation_id,
                        "Website reply delivered in widget.",
                    )
                    conn.commit()
                    return jsonify({"ok": True, "queued": False,
                                    "command_id": None}), 200
                command_payload = {
                    "external_user_id": contact_id,
                    "body": body,
                    "conversation_id": conversation_id,
                }
                display_name = found[0].get("contact_name")
                if display_name:
                    command_payload["target_display_name"] = display_name
                cur.execute(
                    "INSERT INTO " + portal_db._q(portal_db.CMD_TABLE) +
                    " (client_id, channel, action, payload, status, requested_by,"
                    " created_at, updated_at) "
                    "VALUES (%s, 'whatsapp', 'send_message', CAST(%s AS JSONB),"
                    " 'pending', %s, NOW(), NOW()) "
                    "RETURNING id",
                    (principal["client_id"], json.dumps(command_payload),
                     principal["user_id"]),
                )
                inserted = portal_db.rows(cur)
                if inserted:
                    portal_db.log_action(
                        cur,
                        principal["client_id"],
                        "message.enqueued",
                        "customer_user",
                        principal.get("user_id"),
                        conversation_id,
                        "Reply queued for delivery.",
                    )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "portal message enqueue")[0]), 503

    logger.info(
        "portal send_message queued id=%s conversation=%s client=%s",
        inserted[0].get("id") if inserted else "?",
        conversation_id,
        principal["client_id"],
    )
    return jsonify({"ok": True, "queued": True,
                    "command_id": inserted[0].get("id") if inserted else None}), 200
