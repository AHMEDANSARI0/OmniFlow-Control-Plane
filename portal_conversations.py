"""
OmniFlow Control Plane — portal conversations (customer-scoped inbox).

GET /api/v1/portal/conversations            ?status=all|open|closed&limit=50
    -> {conversations: [{id, channel, contact_id, contact_name, status,
                         last_message_at, last_message_preview, created_at}]}
       newest conversation first.

GET /api/v1/portal/conversations/<id>
    -> {conversation: {...}, messages: [{id, direction, body, status,
                                         created_at}]}  oldest-first, last 200.
       404 when the conversation does not belong to this tenant.

Read-only for now: messages enter through the CONNECTOR ingest endpoint
(POST /api/v1/connector/whatsapp/messages, service key). Listing/detail work
for both human sessions and portal API keys (ofk_ — read-only anyway).
"""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from flask import Blueprint, jsonify, request

from portal_auth import PortalAuthUnavailable, authenticate_portal_request
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
    }


def _message_public(row: Any) -> dict:
    return {
        "id": row.get("id"),
        "direction": row.get("direction") or "in",
        "body": row.get("body") or "",
        "status": row.get("status") or "received",
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

    sql = (
        "SELECT id, channel, contact_id, contact_name, status,"
        " last_message_at, last_message_preview, created_at FROM "
        + portal_db._q(portal_db.CONV_TABLE) +
        " WHERE client_id = %s"
    )
    params = [principal["client_id"]]
    if status != "all":
        sql += " AND status = %s"
        params.append(status)
    sql += " ORDER BY last_message_at DESC NULLS LAST, id DESC LIMIT %s"
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
                    " last_message_at, last_message_preview, created_at FROM "
                    + portal_db._q(portal_db.CONV_TABLE) +
                    " WHERE id = %s AND client_id = %s LIMIT 1",
                    (conversation_id, principal["client_id"]),
                )
                found = portal_db.rows(cur)
                if not found:
                    # Other tenant's (or unknown) conversation -> plain 404.
                    return jsonify({"error": {"code": "not_found",
                                              "message": "Conversation not found."}}), 404
                cur.execute(
                    "SELECT id, direction, body, status, created_at FROM "
                    + portal_db._q(portal_db.MSGS_TABLE) +
                    " WHERE conversation_id = %s ORDER BY id DESC LIMIT %s",
                    (conversation_id, MESSAGE_PAGE_SIZE),
                )
                msgs = portal_db.rows(cur)
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "conversation detail")[0]), 503

    return jsonify({
        "conversation": _conversation_public(found[0]),
        "messages": [_message_public(m) for m in reversed(msgs)],
    }), 200
