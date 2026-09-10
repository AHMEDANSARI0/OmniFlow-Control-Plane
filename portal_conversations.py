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
import re
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
        "needs_reply": bool(row.get("needs_reply")),
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


MAX_TAGS_PER_CONVERSATION = 6
MAX_TAG_LENGTH = 24
MAX_TAG_SUMMARY = 30


def _clean_tag(value: Any) -> str:
    """Collapse whitespace and cap the length; empty string when unusable."""
    if not isinstance(value, str):
        return ""
    cleaned = re.sub(r"\s+", " ", value).strip()
    if not cleaned:
        return ""
    return cleaned[:MAX_TAG_LENGTH]


def _tags_map(cur, client_id, conversation_ids) -> dict:
    """Return {conversation_id: [tag, ...]} for the given conversations."""
    ids = [int(cid) for cid in conversation_ids if cid is not None]
    if not ids:
        return {}
    cur.execute(
        "SELECT id, conversation_id, tag FROM " + portal_db._q(portal_db.CONV_TAGS_TABLE) +
        " WHERE client_id = %s AND conversation_id = ANY(%s) ORDER BY id",
        (client_id, ids),
    )
    mapping = {}
    for row in portal_db.rows(cur):
        mapping.setdefault(row.get("conversation_id"), []).append(
            str(row.get("tag") or "")
        )
    return mapping


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
        " ) AS unread, EXISTS ("
        " SELECT 1 FROM " + portal_db._q(portal_db.MSGS_TABLE) +
        " lr WHERE lr.conversation_id = c.id"
        " GROUP BY lr.conversation_id"
        " HAVING COALESCE(MAX(CASE WHEN lr.direction = 'in' THEN lr.id END), 0)"
        " > COALESCE(MAX(CASE WHEN lr.direction = 'out' THEN lr.id END), 0)"
        " ) AS needs_reply FROM "
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
    channel_filter = (request.args.get("channel") or "").strip().lower()
    if channel_filter in ("whatsapp", "website"):
        sql += " AND c.channel = %s"
        params.append(channel_filter)
    tag_filter = (request.args.get("tag") or "").strip()[:MAX_TAG_LENGTH]
    if tag_filter:
        sql += (
            " AND EXISTS (SELECT 1 FROM " + portal_db._q(portal_db.CONV_TAGS_TABLE) +
            " ct WHERE ct.conversation_id = c.id AND ct.client_id = c.client_id"
            " AND LOWER(ct.tag) = LOWER(%s))"
        )
        params.append(tag_filter)
    needs_reply_filter = (request.args.get("needs_reply") or "").strip()
    if needs_reply_filter == "1":
        sql += (
            " AND EXISTS (SELECT 1 FROM " + portal_db._q(portal_db.MSGS_TABLE) +
            " nr WHERE nr.conversation_id = c.id"
            " GROUP BY nr.conversation_id"
            " HAVING COALESCE(MAX(CASE WHEN nr.direction = 'in' THEN nr.id END), 0)"
            " > COALESCE(MAX(CASE WHEN nr.direction = 'out' THEN nr.id END), 0))"
        )
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

    tags_map = {}
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                found = portal_db.rows(cur)
                tags_map = _tags_map(cur, principal["client_id"],
                                     [row.get("id") for row in found])
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "conversations read")[0]), 503

    conversations = []
    for row in found:
        item = _conversation_public(row)
        item["tags"] = tags_map.get(row.get("id"), [])
        conversations.append(item)
    return jsonify({"conversations": conversations}), 200


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
                detail_tags = _tags_map(
                    cur, principal["client_id"], [conversation_id]
                ).get(conversation_id, [])
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

    conversation_public = _conversation_public(found[0])
    conversation_public["tags"] = detail_tags
    return jsonify({
        "conversation": conversation_public,
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



# ---------------------------------------------------------------------------
# Conversation tags (labels) — organize and filter the shared inbox
# ---------------------------------------------------------------------------

@bp.get("/conversations/tags/summary")
def conversation_tags_summary():
    principal, error = _principal_or_error()
    if error is not None:
        return error
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT tag, COUNT(*) AS uses FROM "
                    + portal_db._q(portal_db.CONV_TAGS_TABLE) +
                    " WHERE client_id = %s GROUP BY tag"
                    " ORDER BY uses DESC, tag LIMIT %s",
                    (principal["client_id"], MAX_TAG_SUMMARY),
                )
                rows = portal_db.rows(cur)
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "tag summary")[0]), 503
    return jsonify({"tags": [
        {"tag": str(row.get("tag") or ""), "count": int(row.get("uses") or 0)}
        for row in rows
    ]}), 200


@bp.post("/conversations/<int:conversation_id>/tags")
def add_conversation_tag(conversation_id: int):
    principal, error = _principal_or_error()
    if error is not None:
        return error
    forbidden = ensure_human_principal(principal)
    if forbidden is not None:
        return forbidden
    payload = request.get_json(silent=True) or {}
    tag = _clean_tag(payload.get("tag"))
    if not tag:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Label text is required."}}), 400
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
                    "SELECT id, tag FROM " + portal_db._q(portal_db.CONV_TAGS_TABLE) +
                    " WHERE client_id = %s AND conversation_id = %s ORDER BY id",
                    (principal["client_id"], conversation_id),
                )
                existing = portal_db.rows(cur)
                if any(str(row.get("tag") or "").lower() == tag.lower()
                       for row in existing):
                    return jsonify({"ok": True, "duplicate": True, "tag": tag}), 200
                if len(existing) >= MAX_TAGS_PER_CONVERSATION:
                    return jsonify({"error": {"code": "limit_reached",
                                              "message": "Up to 6 labels per conversation."}}), 409
                cur.execute(
                    "INSERT INTO " + portal_db._q(portal_db.CONV_TAGS_TABLE) +
                    " (client_id, conversation_id, tag, created_at)"
                    " VALUES (%s, %s, %s, NOW()) RETURNING id",
                    (principal["client_id"], conversation_id, tag),
                )
                inserted = portal_db.rows(cur)
                portal_db.log_action(
                    cur,
                    principal["client_id"],
                    "tag.added",
                    "customer_user",
                    principal.get("user_id"),
                    conversation_id,
                    ("Label added: " + tag)[:200],
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "label add")[0]), 503
    return jsonify({"ok": True, "duplicate": False, "tag": tag,
                    "id": inserted[0].get("id") if inserted else None}), 200


@bp.delete("/conversations/<int:conversation_id>/tags")
def remove_conversation_tag(conversation_id: int):
    principal, error = _principal_or_error()
    if error is not None:
        return error
    forbidden = ensure_human_principal(principal)
    if forbidden is not None:
        return forbidden
    tag = _clean_tag(request.args.get("tag"))
    if not tag:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Label text is required."}}), 400
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM " + portal_db._q(portal_db.CONV_TAGS_TABLE) +
                    " WHERE conversation_id = %s AND client_id = %s"
                    " AND LOWER(tag) = LOWER(%s) RETURNING id, tag",
                    (conversation_id, principal["client_id"], tag),
                )
                deleted = portal_db.rows(cur)
                if not deleted:
                    return jsonify({"error": {"code": "not_found",
                                              "message": "Label not found."}}), 404
                portal_db.log_action(
                    cur,
                    principal["client_id"],
                    "tag.removed",
                    "customer_user",
                    principal.get("user_id"),
                    conversation_id,
                    ("Label removed: " + str(deleted[0].get("tag") or ""))[:200],
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "label remove")[0]), 503
    return jsonify({"ok": True}), 200



# ---------------------------------------------------------------------------
# Saved replies (canned response templates for the shared inbox)
# ---------------------------------------------------------------------------

MAX_SAVED_REPLIES = 30
MAX_REPLY_BODY = 1000
SHORTCUT_RE = re.compile(r"^[a-z0-9_-]{1,24}$")


def _clean_shortcut(value: Any) -> str:
    """Normalize a shortcut: trim, drop a leading slash, lowercase, cap 24."""
    if not isinstance(value, str):
        return ""
    shortcut = value.strip()
    while shortcut.startswith("/"):
        shortcut = shortcut[1:]
    shortcut = re.sub(r"\s+", "-", shortcut.strip().lower())
    shortcut = shortcut[:24]
    if not SHORTCUT_RE.match(shortcut):
        return ""
    return shortcut


@bp.get("/saved-replies")
def list_saved_replies():
    principal, error = _principal_or_error()
    if error is not None:
        return error
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, shortcut, body, created_at FROM "
                    + portal_db._q(portal_db.SAVED_REPLIES_TABLE) +
                    " WHERE client_id = %s ORDER BY id DESC LIMIT %s",
                    (principal["client_id"], MAX_SAVED_REPLIES),
                )
                rows = portal_db.rows(cur)
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "saved replies read")[0]), 503
    return jsonify({"replies": [
        {
            "id": int(row.get("id") or 0),
            "shortcut": str(row.get("shortcut") or ""),
            "body": str(row.get("body") or ""),
            "created_at": _iso(row.get("created_at")),
        }
        for row in rows
    ]}), 200


@bp.post("/saved-replies")
def create_saved_reply():
    principal, error = _principal_or_error()
    if error is not None:
        return error
    forbidden = ensure_human_principal(principal)
    if forbidden is not None:
        return forbidden
    payload = request.get_json(silent=True) or {}
    shortcut = _clean_shortcut(payload.get("shortcut"))
    if not shortcut:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Shortcut can use letters, numbers, dashes and underscores (max 24)."}}), 400
    body_text = payload.get("body")
    body_text = body_text.strip() if isinstance(body_text, str) else ""
    if not body_text:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Message text is required."}}), 400
    if len(body_text) > MAX_REPLY_BODY:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Message must be 1000 characters or fewer."}}), 400
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS total FROM "
                    + portal_db._q(portal_db.SAVED_REPLIES_TABLE) +
                    " WHERE client_id = %s",
                    (principal["client_id"],),
                )
                counts = portal_db.rows(cur)
                if counts and int(counts[0].get("total") or 0) >= MAX_SAVED_REPLIES:
                    return jsonify({"error": {"code": "limit_reached",
                                              "message": "Up to 30 saved replies per workspace."}}), 409
                cur.execute(
                    "SELECT id FROM " + portal_db._q(portal_db.SAVED_REPLIES_TABLE) +
                    " WHERE client_id = %s AND LOWER(shortcut) = LOWER(%s) LIMIT 1",
                    (principal["client_id"], shortcut),
                )
                if portal_db.rows(cur):
                    return jsonify({"error": {"code": "duplicate",
                                              "message": "A reply with this shortcut already exists."}}), 409
                cur.execute(
                    "INSERT INTO " + portal_db._q(portal_db.SAVED_REPLIES_TABLE) +
                    " (client_id, shortcut, body, created_at)"
                    " VALUES (%s, %s, %s, NOW()) RETURNING id, shortcut, body, created_at",
                    (principal["client_id"], shortcut, body_text),
                )
                inserted = portal_db.rows(cur)
                portal_db.log_action(
                    cur,
                    principal["client_id"],
                    "saved_reply.created",
                    "customer_user",
                    principal.get("user_id"),
                    None,
                    ("Saved reply added: /" + shortcut)[:200],
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "saved reply create")[0]), 503
    created = inserted[0] if inserted else {}
    return jsonify({"ok": True, "reply": {
        "id": int(created.get("id") or 0),
        "shortcut": str(created.get("shortcut") or shortcut),
        "body": str(created.get("body") or body_text),
        "created_at": _iso(created.get("created_at")),
    }}), 200


@bp.delete("/saved-replies/<int:reply_id>")
def delete_saved_reply(reply_id: int):
    principal, error = _principal_or_error()
    if error is not None:
        return error
    forbidden = ensure_human_principal(principal)
    if forbidden is not None:
        return forbidden
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM " + portal_db._q(portal_db.SAVED_REPLIES_TABLE) +
                    " WHERE id = %s AND client_id = %s RETURNING shortcut",
                    (reply_id, principal["client_id"]),
                )
                deleted = portal_db.rows(cur)
                if not deleted:
                    return jsonify({"error": {"code": "not_found",
                                              "message": "Saved reply not found."}}), 404
                portal_db.log_action(
                    cur,
                    principal["client_id"],
                    "saved_reply.deleted",
                    "customer_user",
                    principal.get("user_id"),
                    None,
                    ("Saved reply deleted: /"
                     + str(deleted[0].get("shortcut") or ""))[:200],
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "saved reply delete")[0]), 503
    return jsonify({"ok": True}), 200



# ---------------------------------------------------------------------------
# Conversation export (CSV download source — same filters as the list)
# ---------------------------------------------------------------------------

EXPORT_LIMIT = 500


@bp.get("/conversations/export")
def export_conversations():
    principal, error = _principal_or_error()
    if error is not None:
        return error
    status = (request.args.get("status") or "all").strip().lower()
    if status not in ALLOWED_STATUS:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "status must be all, open, or closed."}}), 400
    search = (request.args.get("q") or "").strip()[:100]
    intent_filter = (request.args.get("intent") or "").strip().lower()
    channel_filter = (request.args.get("channel") or "").strip().lower()
    tag_filter = (request.args.get("tag") or "").strip()[:MAX_TAG_LENGTH]

    sql = (
        "SELECT c.id, c.channel, c.contact_id, c.contact_name, c.status,"
        " c.last_message_at, c.last_message_preview, c.created_at, c.last_intent,"
        " c.lead_score, c.lead_temp, c.assigned_to, tm.name AS assignee_name"
        " FROM " + portal_db._q(portal_db.CONV_TABLE) + " c"
        " LEFT JOIN " + portal_db._q(portal_db.TEAM_TABLE) + " tm"
        " ON tm.client_id = c.client_id AND tm.email = c.assigned_to"
        " WHERE c.client_id = %s"
    )
    params = [principal["client_id"]]
    if status != "all":
        sql += " AND c.status = %s"
        params.append(status)
    if intent_filter and intent_filter != "all":
        sql += " AND c.last_intent = %s"
        params.append(intent_filter[:40])
    if channel_filter in ("whatsapp", "website"):
        sql += " AND c.channel = %s"
        params.append(channel_filter)
    if tag_filter:
        sql += (
            " AND EXISTS (SELECT 1 FROM " + portal_db._q(portal_db.CONV_TAGS_TABLE) +
            " ct WHERE ct.conversation_id = c.id AND ct.client_id = c.client_id"
            " AND LOWER(ct.tag) = LOWER(%s))"
        )
        params.append(tag_filter)
    if (request.args.get("needs_reply") or "").strip() == "1":
        sql += (
            " AND EXISTS (SELECT 1 FROM " + portal_db._q(portal_db.MSGS_TABLE) +
            " nr WHERE nr.conversation_id = c.id"
            " GROUP BY nr.conversation_id"
            " HAVING COALESCE(MAX(CASE WHEN nr.direction = 'in' THEN nr.id END), 0)"
            " > COALESCE(MAX(CASE WHEN nr.direction = 'out' THEN nr.id END), 0))"
        )
    if search:
        like = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        sql += (
            " AND (c.contact_name ILIKE %s OR c.contact_id ILIKE %s"
            " OR EXISTS (SELECT 1 FROM " + portal_db._q(portal_db.MSGS_TABLE) +
            " sm WHERE sm.conversation_id = c.id AND sm.body ILIKE %s))"
        )
        params.extend([like, like, like])
    sql += " ORDER BY c.last_message_at DESC NULLS LAST, c.id DESC LIMIT %s"
    params.append(EXPORT_LIMIT)

    tags_map = {}
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                found = portal_db.rows(cur)
                tags_map = _tags_map(cur, principal["client_id"],
                                     [row.get("id") for row in found])
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "conversations export")[0]), 503

    rows = []
    for row in found:
        item = _conversation_public(row)
        item["tags"] = tags_map.get(row.get("id"), [])
        rows.append(item)
    return jsonify({"conversations": rows, "count": len(rows)}), 200



# ---------------------------------------------------------------------------
# Customers (contacts aggregated across all their conversations)
# ---------------------------------------------------------------------------

CUSTOMERS_LIMIT = 100
WARM_LEAD_SCORE = 35


@bp.get("/customers")
def list_customers():
    principal, error = _principal_or_error()
    if error is not None:
        return error
    search = (request.args.get("q") or "").strip()[:100]
    channel_filter = (request.args.get("channel") or "").strip().lower()

    sql = (
        "SELECT c.contact_id,"
        " COALESCE(MAX(c.contact_name), '') AS contact_name,"
        " COUNT(*) AS conversation_count,"
        " COUNT(*) FILTER (WHERE c.status = 'open') AS open_count,"
        " MAX(c.last_message_at) AS last_message_at,"
        " MAX(c.last_message_preview) AS last_message_preview,"
        " STRING_AGG(DISTINCT c.channel, ',') AS channels,"
        " BOOL_OR(c.lead_temp = 'hot') AS has_hot,"
        " MAX(c.lead_score) AS max_lead_score"
        " FROM " + portal_db._q(portal_db.CONV_TABLE) + " c"
        " WHERE c.client_id = %s"
    )
    params = [principal["client_id"]]
    if channel_filter in ("whatsapp", "website"):
        sql += " AND c.channel = %s"
        params.append(channel_filter)
    if search:
        like = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        sql += (
            " GROUP BY c.contact_id"
            " HAVING (COALESCE(MAX(c.contact_name), '') ILIKE %s"
            " OR c.contact_id ILIKE %s)"
        )
        params.extend([like, like])
    else:
        sql += " GROUP BY c.contact_id"
    sql += (
        " ORDER BY MAX(c.last_message_at) DESC NULLS LAST, c.contact_id"
        " LIMIT %s"
    )
    params.append(CUSTOMERS_LIMIT)

    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                found = portal_db.rows(cur)
                contact_ids = [row.get("contact_id") for row in found]
                tags_map = {}
                if contact_ids:
                    cur.execute(
                        "SELECT conv.contact_id, ct.tag FROM "
                        + portal_db._q(portal_db.CONV_TAGS_TABLE) + " ct"
                        " JOIN " + portal_db._q(portal_db.CONV_TABLE) + " conv"
                        " ON conv.id = ct.conversation_id"
                        " WHERE ct.client_id = %s AND conv.client_id = %s"
                        " AND conv.contact_id = ANY(%s) ORDER BY ct.id",
                        (principal["client_id"], principal["client_id"],
                         contact_ids),
                    )
                    for row in portal_db.rows(cur):
                        key = row.get("contact_id")
                        tag = str(row.get("tag") or "")
                        if key is None or not tag:
                            continue
                        bucket = tags_map.setdefault(key, [])
                        if tag not in bucket:
                            bucket.append(tag)
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "customers read")[0]), 503

    customers = []
    for row in found:
        max_score = int(row.get("max_lead_score") or 0)
        if row.get("has_hot"):
            lead_temp = "hot"
        elif max_score >= WARM_LEAD_SCORE:
            lead_temp = "warm"
        else:
            lead_temp = "cold"
        customers.append({
            "contact_id": row.get("contact_id"),
            "name": str(row.get("contact_name") or ""),
            "channels": [
                part for part in str(row.get("channels") or "").split(",") if part
            ],
            "conversation_count": int(row.get("conversation_count") or 0),
            "open_count": int(row.get("open_count") or 0),
            "last_message_at": _iso(row.get("last_message_at")),
            "last_message_preview": row.get("last_message_preview"),
            "lead_temp": lead_temp,
            "tags": tags_map.get(row.get("contact_id"), []),
        })
    return jsonify({"customers": customers}), 200



# ---------------------------------------------------------------------------
# Automations v0 — deterministic keyword rules on inbound messages
# ---------------------------------------------------------------------------

MAX_AUTOMATIONS = 20
MAX_KEYWORD_LENGTH = 40
AUTOMATION_ACTION_TYPES = ("add_tag", "assign")
AUTOMATIONS_TABLE = portal_db.AUTOMATIONS_TABLE
WELCOME_SETTINGS_TABLE = portal_db.AUTOMATION_SETTINGS_TABLE


def _normalize_keyword(value) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip().lower()[:MAX_KEYWORD_LENGTH]


def _automation_public(row: Any) -> dict:
    return {
        "id": int(row.get("id") or 0),
        "keyword": str(row.get("keyword") or ""),
        "action_type": row.get("action_type") or "add_tag",
        "action_value": str(row.get("action_value") or ""),
        "is_active": row.get("is_active") is True,
        "times_triggered": int(row.get("times_triggered") or 0),
        "created_at": _iso(row.get("created_at")),
    }


def apply_automation_rules(client_id, conversation_id, contact_id,
                           message_text, conn):
    """Deterministic keyword rules for one inbound message. Tags respect the
    6-label cap (ON CONFLICT DO NOTHING); assignment only fills unassigned
    chats. Runs inside the caller's transaction; callers wrap in try/except."""
    text = re.sub(r"\s+", " ", str(message_text or "")).strip().lower()
    if not text:
        return
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, keyword, action_type, action_value FROM "
            + portal_db._q(AUTOMATIONS_TABLE) +
            " WHERE client_id = %s AND is_active IS TRUE"
            " ORDER BY id LIMIT %s",
            (client_id, MAX_AUTOMATIONS),
        )
        rules = portal_db.rows(cur)
        for rule in rules:
            keyword = str(rule.get("keyword") or "")
            if not keyword or keyword not in text:
                continue
            action_type = rule.get("action_type")
            action_value = str(rule.get("action_value") or "")
            if action_type == "add_tag" and action_value:
                cur.execute(
                    "SELECT COUNT(*) AS total FROM "
                    + portal_db._q(portal_db.CONV_TAGS_TABLE) +
                    " WHERE client_id = %s AND conversation_id = %s",
                    (client_id, conversation_id),
                )
                counts = portal_db.rows(cur)
                total = int(counts[0].get("total") or 0) if counts else 0
                if total < MAX_TAGS_PER_CONVERSATION:
                    cur.execute(
                        "INSERT INTO " + portal_db._q(portal_db.CONV_TAGS_TABLE) +
                        " (client_id, conversation_id, tag, created_at)"
                        " VALUES (%s, %s, %s, NOW())"
                        " ON CONFLICT (client_id, conversation_id, tag)"
                        " DO NOTHING",
                        (client_id, conversation_id, action_value),
                    )
            elif action_type == "assign" and action_value:
                cur.execute(
                    "UPDATE " + portal_db._q(portal_db.CONV_TABLE) +
                    " SET assigned_to = %s, updated_at = NOW()"
                    " WHERE id = %s AND client_id = %s AND assigned_to IS NULL",
                    (action_value, conversation_id, client_id),
                )
            else:
                continue
            cur.execute(
                "UPDATE " + portal_db._q(AUTOMATIONS_TABLE) +
                " SET times_triggered = times_triggered + 1 WHERE id = %s",
                (rule.get("id"),),
            )
        if rules:
            portal_db.log_action(
                cur,
                client_id,
                "automation.checked",
                "system",
                None,
                conversation_id,
                ("Rules evaluated: %d" % len(rules))[:200],
            )


@bp.get("/automations")
def list_automations():
    principal, error = _principal_or_error()
    if error is not None:
        return error
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, keyword, action_type, action_value, is_active,"
                    " times_triggered, created_at FROM "
                    + portal_db._q(AUTOMATIONS_TABLE) +
                    " WHERE client_id = %s ORDER BY id DESC LIMIT %s",
                    (principal["client_id"], MAX_AUTOMATIONS),
                )
                rows = portal_db.rows(cur)
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "automations read")[0]), 503
    return jsonify({"automations": [_automation_public(row) for row in rows]}), 200


@bp.post("/automations")
def create_automation():
    principal, error = _principal_or_error()
    if error is not None:
        return error
    forbidden = ensure_human_principal(principal)
    if forbidden is not None:
        return forbidden
    payload = request.get_json(silent=True) or {}
    keyword = _normalize_keyword(payload.get("keyword"))
    if len(keyword) < 2:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Keyword must be 2-40 characters."}}), 400
    action_type = payload.get("action_type", payload.get("actionType"))
    if action_type not in AUTOMATION_ACTION_TYPES:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Action must be add_tag or assign."}}), 400
    if action_type == "add_tag":
        action_value = _clean_tag(payload.get("action_value",
                                              payload.get("actionValue")))
        if not action_value:
            return jsonify({"error": {"code": "bad_request",
                                      "message": "Label value is required."}}), 400
    else:
        action_value = str(payload.get("action_value",
                                       payload.get("actionValue")) or "").strip().lower()[:120]
        if not action_value:
            return jsonify({"error": {"code": "bad_request",
                                      "message": "Assignee email is required."}}), 400
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                if action_type == "assign":
                    cur.execute(
                        "SELECT 1 FROM " + portal_db._q(portal_db.TEAM_TABLE) +
                        " WHERE client_id = %s AND email = %s"
                        " AND status = 'active' LIMIT 1",
                        (principal["client_id"], action_value),
                    )
                    if not portal_db.rows(cur):
                        return jsonify({"error": {"code": "assignee_not_found",
                                                  "message": "Assignee must be an active team member."}}), 400
                cur.execute(
                    "SELECT COUNT(*) AS total FROM "
                    + portal_db._q(AUTOMATIONS_TABLE) +
                    " WHERE client_id = %s",
                    (principal["client_id"],),
                )
                counts = portal_db.rows(cur)
                if counts and int(counts[0].get("total") or 0) >= MAX_AUTOMATIONS:
                    return jsonify({"error": {"code": "limit_reached",
                                              "message": "Up to 20 automations."}}), 409
                cur.execute(
                    "SELECT 1 FROM " + portal_db._q(AUTOMATIONS_TABLE) +
                    " WHERE client_id = %s AND keyword = %s"
                    " AND action_type = %s AND LOWER(action_value) = LOWER(%s)"
                    " LIMIT 1",
                    (principal["client_id"], keyword, action_type, action_value),
                )
                if portal_db.rows(cur):
                    return jsonify({"error": {"code": "duplicate",
                                              "message": "A rule with this keyword and action already exists."}}), 409
                cur.execute(
                    "INSERT INTO " + portal_db._q(AUTOMATIONS_TABLE) +
                    " (client_id, keyword, action_type, action_value,"
                    " is_active, times_triggered, created_at, updated_at)"
                    " VALUES (%s, %s, %s, %s, TRUE, 0, NOW(), NOW())"
                    " RETURNING id, keyword, action_type, action_value,"
                    " is_active, times_triggered, created_at",
                    (principal["client_id"], keyword, action_type, action_value),
                )
                inserted = portal_db.rows(cur)
                portal_db.log_action(
                    cur,
                    principal["client_id"],
                    "automation.created",
                    "customer_user",
                    principal.get("user_id"),
                    None,
                    ("Automation added: " + keyword
                     + " -> " + action_type + " " + action_value)[:200],
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "automation create")[0]), 503
    if not inserted:
        return jsonify({"error": {"code": "portal_unavailable",
                                  "message": "Try again shortly."}}), 503
    return jsonify({"ok": True, "automation": _automation_public(inserted[0])}), 200


@bp.patch("/automations/<int:rule_id>")
def update_automation(rule_id: int):
    principal, error = _principal_or_error()
    if error is not None:
        return error
    forbidden = ensure_human_principal(principal)
    if forbidden is not None:
        return forbidden
    payload = request.get_json(silent=True) or {}
    is_active = payload.get("is_active", payload.get("isActive"))
    if not isinstance(is_active, bool):
        return jsonify({"error": {"code": "bad_request",
                                  "message": "is_active must be true or false."}}), 400
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE " + portal_db._q(AUTOMATIONS_TABLE) +
                    " SET is_active = %s, updated_at = NOW()"
                    " WHERE id = %s AND client_id = %s"
                    " RETURNING keyword",
                    (is_active, rule_id, principal["client_id"]),
                )
                updated = portal_db.rows(cur)
                if not updated:
                    return jsonify({"error": {"code": "not_found",
                                              "message": "Automation not found."}}), 404
                portal_db.log_action(
                    cur,
                    principal["client_id"],
                    "automation.updated",
                    "customer_user",
                    principal.get("user_id"),
                    None,
                    "Automation " + ("enabled: " if is_active else "disabled: ")
                    + str(updated[0].get("keyword") or ""),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "automation update")[0]), 503
    return jsonify({"ok": True}), 200


@bp.delete("/automations/<int:rule_id>")
def delete_automation(rule_id: int):
    principal, error = _principal_or_error()
    if error is not None:
        return error
    forbidden = ensure_human_principal(principal)
    if forbidden is not None:
        return forbidden
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM " + portal_db._q(AUTOMATIONS_TABLE) +
                    " WHERE id = %s AND client_id = %s RETURNING keyword",
                    (rule_id, principal["client_id"]),
                )
                deleted = portal_db.rows(cur)
                if not deleted:
                    return jsonify({"error": {"code": "not_found",
                                              "message": "Automation not found."}}), 404
                portal_db.log_action(
                    cur,
                    principal["client_id"],
                    "automation.deleted",
                    "customer_user",
                    principal.get("user_id"),
                    None,
                    "Automation deleted: " + str(deleted[0].get("keyword") or ""),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "automation delete")[0]), 503
    return jsonify({"ok": True}), 200


@bp.get("/automations/welcome")
def get_welcome_settings():
    """Current welcome-automation settings (defaults when never configured)."""
    principal, error = _principal_or_error()
    if error:
        return error
    forbidden = ensure_human_principal(principal)
    if forbidden:
        return forbidden
    client_id = principal["client_id"]
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT welcome_enabled, welcome_text FROM "
                    + portal_db._q(WELCOME_SETTINGS_TABLE) +
                    " WHERE client_id = %s LIMIT 1",
                    (client_id,),
                )
                rows = portal_db.rows(cur)
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "welcome settings read")[0]), 503
    row = rows[0] if rows else {}
    return jsonify({
        "ok": True,
        "enabled": bool(row.get("welcome_enabled")),
        "text": str(row.get("welcome_text") or ""),
    }), 200


@bp.put("/automations/welcome")
def save_welcome_settings():
    principal, error = _principal_or_error()
    if error:
        return error
    forbidden = ensure_human_principal(principal)
    if forbidden:
        return forbidden
    payload = request.get_json(silent=True) or {}
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Enabled must be true or false."}}), 400
    text = payload.get("text")
    text = text.strip() if isinstance(text, str) else ""
    if enabled and not text:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Write the welcome message first."}}), 400
    if len(text) > 500:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Welcome message must be 500 characters or fewer."}}), 400
    client_id = principal["client_id"]
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO " + portal_db._q(WELCOME_SETTINGS_TABLE) +
                    " (client_id, welcome_enabled, welcome_text, updated_at)"
                    " VALUES (%s, %s, %s, NOW())"
                    " ON CONFLICT (client_id) DO UPDATE SET"
                    " welcome_enabled = EXCLUDED.welcome_enabled,"
                    " welcome_text = EXCLUDED.welcome_text,"
                    " updated_at = NOW()",
                    (client_id, enabled, text),
                )
                portal_db.log_action(
                    cur,
                    client_id,
                    "automation.welcome_updated",
                    "customer_user",
                    principal.get("user_id"),
                    None,
                    "Welcome automation " + ("enabled." if enabled else "disabled."),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "welcome settings save")[0]), 503
    return jsonify({"ok": True, "enabled": enabled, "text": text}), 200


def maybe_send_welcome(client_id, conversation_id, contact_id, contact_name, conn):
    """Queue the welcome message for a brand-new WhatsApp conversation.

    Called from the ingest path only when the conversation row was created by
    the current message (the upsert's (xmax = 0) flag). Deterministic: one
    settings read, at most one queued send_message command. Returns True when
    a welcome was queued. The caller wraps this in try/except.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT welcome_enabled, welcome_text FROM "
            + portal_db._q(WELCOME_SETTINGS_TABLE) +
            " WHERE client_id = %s LIMIT 1",
            (client_id,),
        )
        rows = portal_db.rows(cur)
        if not rows or not rows[0].get("welcome_enabled"):
            return False
        text = str(rows[0].get("welcome_text") or "").strip()
        if not text:
            return False
        display = str(contact_name or "").strip()
        first_name = display.split(" ")[0] if display else ""
        greeting = text.replace("{{name}}", first_name or "there")
        greeting_payload = {
            "external_user_id": contact_id,
            "body": greeting[:1000],
            "source": "welcome",
        }
        if display:
            greeting_payload["target_display_name"] = display
        cur.execute(
            "INSERT INTO " + portal_db._q(portal_db.CMD_TABLE) +
            " (client_id, channel, action, payload, status, requested_by,"
            " created_at, updated_at) "
            "VALUES (%s, 'whatsapp', 'send_message', CAST(%s AS JSONB),"
            " 'pending', NULL, NOW(), NOW()) "
            "RETURNING id",
            (client_id, json.dumps(greeting_payload)),
        )
        portal_db.log_action(
            cur,
            client_id,
            "automation.welcome_sent",
            "system",
            None,
            conversation_id,
            "Welcome message queued for a new conversation.",
        )
    return True


@bp.get("/automations/autoclose")
def get_autoclose_settings():
    """Current auto-close settings (defaults when never configured)."""
    principal, error = _principal_or_error()
    if error:
        return error
    forbidden = ensure_human_principal(principal)
    if forbidden:
        return forbidden
    client_id = principal["client_id"]
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT close_enabled, close_after_hours FROM "
                    + portal_db._q(WELCOME_SETTINGS_TABLE) +
                    " WHERE client_id = %s LIMIT 1",
                    (client_id,),
                )
                rows = portal_db.rows(cur)
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "autoclose settings read")[0]), 503
    row = rows[0] if rows else {}
    try:
        hours = int(row.get("close_after_hours") or 48)
    except (TypeError, ValueError):
        hours = 48
    return jsonify({
        "ok": True,
        "enabled": bool(row.get("close_enabled")),
        "hours": hours,
    }), 200


@bp.put("/automations/autoclose")
def save_autoclose_settings():
    principal, error = _principal_or_error()
    if error:
        return error
    forbidden = ensure_human_principal(principal)
    if forbidden:
        return forbidden
    payload = request.get_json(silent=True) or {}
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Enabled must be true or false."}}), 400
    hours = payload.get("hours", 48)
    if isinstance(hours, bool) or not isinstance(hours, int)             or hours < 1 or hours > 720:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Hours must be a whole number between 1 and 720."}}), 400
    client_id = principal["client_id"]
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO " + portal_db._q(WELCOME_SETTINGS_TABLE) +
                    " (client_id, close_enabled, close_after_hours, updated_at)"
                    " VALUES (%s, %s, %s, NOW())"
                    " ON CONFLICT (client_id) DO UPDATE SET"
                    " close_enabled = EXCLUDED.close_enabled,"
                    " close_after_hours = EXCLUDED.close_after_hours,"
                    " updated_at = NOW()",
                    (client_id, enabled, hours),
                )
                portal_db.log_action(
                    cur,
                    client_id,
                    "automation.autoclose_updated",
                    "customer_user",
                    principal.get("user_id"),
                    None,
                    "Auto-close enabled (idle over " + str(hours) + " hours)."
                    if enabled else "Auto-close disabled.",
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "autoclose settings save")[0]), 503
    return jsonify({"ok": True, "enabled": enabled, "hours": hours}), 200


def sweep_auto_close(client_id, conn):
    """Close idle open chats when auto-close is enabled.

    Deterministic: one settings read, at most one UPDATE. Returns the number
    of conversations closed. The caller owns the transaction/commit and
    wraps this in try/except.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT close_enabled, close_after_hours FROM "
            + portal_db._q(WELCOME_SETTINGS_TABLE) +
            " WHERE client_id = %s LIMIT 1",
            (client_id,),
        )
        rows = portal_db.rows(cur)
        if not rows or not rows[0].get("close_enabled"):
            return 0
        try:
            hours = int(rows[0].get("close_after_hours") or 48)
        except (TypeError, ValueError):
            hours = 48
        if hours < 1:
            return 0
        cur.execute(
            "UPDATE " + portal_db._q(portal_db.CONV_TABLE) +
            " SET status = 'closed', updated_at = NOW()"
            " WHERE client_id = %s AND status = 'open'"
            " AND last_message_at < NOW() - make_interval(hours => %s)"
            " RETURNING id",
            (client_id, hours),
        )
        closed = portal_db.rows(cur)
        if closed:
            portal_db.log_action(
                cur,
                client_id,
                "conversations.auto_closed",
                "system",
                None,
                None,
                "Auto-closed " + str(len(closed)) + " idle chat(s).",
            )
        return len(closed)





