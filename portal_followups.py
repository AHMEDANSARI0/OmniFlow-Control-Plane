"""Portal follow-up agent (customer Bearer + connector service key).

Businesses configure: enabled, delay hours, max attempts, message template
({name} = customer first name). A follow-up is armed when an inbound message
classifies as pricing/availability/purchase_intent and cancelled when the
customer sends anything else (including sensitive intents). The laptop
connector polls due follow-ups, delivers them through the WhatsApp adapter
and acks; attempts and stage are tracked here.
"""

import logging
import os
import secrets
from typing import Any, Optional

from flask import Blueprint, jsonify, request

from portal_auth import PortalAuthUnavailable, authenticate_portal_request
import portal_db


logger = logging.getLogger("omniflow.portal-followups")

bp = Blueprint("portal_followups", __name__, url_prefix="/api/v1")

DEFAULT_TEMPLATE = (
    "Hi {name}! Following up on your question — if you need any more "
    "details or want to place an order, just let me know. Happy to help!"
)

QUALIFYING_INTENTS = ("pricing", "availability", "purchase_intent")


def _service_key_ok():
    provided = request.headers.get("X-Omniflow-Key", "")
    expected = (
        os.environ.get("OMNIFLOW_SERVICE_KEY", "")
        or os.environ.get("OMNIFLOW_ADMIN_API_KEY", "")
    ).strip()
    return bool(expected) and secrets.compare_digest(provided, expected)


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


def _load_settings(cur, client_id):
    cur.execute(
        "SELECT enabled, delay_hours, max_attempts, message_template"
        " FROM " + portal_db._q("portal_followup_settings") +
        " WHERE client_id = %s LIMIT 1",
        (client_id,),
    )
    found = portal_db.rows(cur)
    if not found:
        return {"enabled": False, "delay_hours": 24, "max_attempts": 2,
                "message_template": ""}
    row = found[0]
    return {
        "enabled": bool(row.get("enabled")),
        "delay_hours": int(row.get("delay_hours") or 24),
        "max_attempts": int(row.get("max_attempts") or 2),
        "message_template": row.get("message_template") or "",
    }


def _public(settings):
    return {
        "enabled": settings["enabled"],
        "delay_hours": settings["delay_hours"],
        "max_attempts": settings["max_attempts"],
        "message_template": settings["message_template"],
    }


@bp.get("/portal/followups")
def get_followup_settings():
    principal, error = _principal_or_error()
    if error:
        return error
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                settings = _load_settings(cur, principal["client_id"])
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "followups read")[0]), 503
    return jsonify({"settings": _public(settings)}), 200


@bp.put("/portal/followups")
def put_followup_settings():
    principal, error = _principal_or_error()
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    settings = payload.get("settings")
    if not isinstance(settings, dict):
        return jsonify({"error": {"code": "bad_request",
                                  "message": "settings object is required."}}), 400

    enabled = bool(settings.get("enabled"))
    raw_delay = settings.get("delay_hours")
    raw_attempts = settings.get("max_attempts")
    template = settings.get("message_template")
    template = template if isinstance(template, str) else ""

    try:
        delay_hours = int(raw_delay)
    except (TypeError, ValueError):
        return jsonify({"error": {"code": "bad_request",
                                  "message": "delay_hours must be a number."}}), 400
    try:
        max_attempts = int(raw_attempts)
    except (TypeError, ValueError):
        return jsonify({"error": {"code": "bad_request",
                                  "message": "max_attempts must be a number."}}), 400
    if not 1 <= delay_hours <= 168:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "delay_hours must be 1-168."}}), 400
    if not 1 <= max_attempts <= 3:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "max_attempts must be 1-3."}}), 400
    if len(template) > 1000:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Template must be under 1000 characters."}}), 400

    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO " + portal_db._q("portal_followup_settings") +
                    " (client_id, enabled, delay_hours, max_attempts,"
                    " message_template, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, NOW()) "
                    "ON CONFLICT (client_id) DO UPDATE SET "
                    " enabled = EXCLUDED.enabled,"
                    " delay_hours = EXCLUDED.delay_hours,"
                    " max_attempts = EXCLUDED.max_attempts,"
                    " message_template = EXCLUDED.message_template,"
                    " updated_at = NOW()",
                    (principal["client_id"], enabled, delay_hours,
                     max_attempts, template),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "followups write")[0]), 503
    return jsonify({"ok": True}), 200


def _render_template(template, contact_name):
    base = (template or "").strip() or DEFAULT_TEMPLATE
    clean_name = str(contact_name or "").strip()
    first_name = clean_name.split(" ")[0] if clean_name else "there"
    return base.replace("{name}", first_name)


@bp.get("/connector/followups")
def connector_due_followups():
    if not _service_key_ok():
        return jsonify({"error": {"code": "forbidden",
                                  "message": "Service key missing or invalid."}}), 403
    try:
        client_id = int(request.args.get("client_id", "1"))
    except (TypeError, ValueError):
        client_id = 1
    try:
        limit = int(request.args.get("limit", "5"))
    except (TypeError, ValueError):
        limit = 5
    limit = max(1, min(5, limit))

    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                settings = _load_settings(cur, client_id)
                if not settings["enabled"]:
                    return jsonify({"followups": []}), 200
                cur.execute(
                    "SELECT f.id, f.conversation_id, f.contact_id,"
                    " f.contact_name, f.attempts, s.message_template"
                    " FROM " + portal_db._q("portal_followups") + " f"
                    " JOIN " + portal_db._q("portal_followup_settings") +
                    " s ON s.client_id = f.client_id"
                    " WHERE f.client_id = %s AND f.stage = 'waiting'"
                    " AND f.attempts < s.max_attempts"
                    " AND f.next_due_at IS NOT NULL AND f.next_due_at <= NOW()"
                    " ORDER BY f.next_due_at LIMIT %s",
                    (client_id, limit),
                )
                due = portal_db.rows(cur)
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "followups due")[0]), 503

    items = []
    for row in due:
        items.append({
            "id": row.get("id"),
            "conversation_id": row.get("conversation_id"),
            "external_user_id": row.get("contact_id"),
            "contact_name": row.get("contact_name"),
            "attempts": row.get("attempts"),
            "body": _render_template(
                settings["message_template"], row.get("contact_name")
            ),
        })
    return jsonify({"followups": items}), 200


@bp.post("/connector/followups/ack")
def connector_followup_ack():
    if not _service_key_ok():
        return jsonify({"error": {"code": "forbidden",
                                  "message": "Service key missing or invalid."}}), 403
    payload = request.get_json(silent=True) or {}
    try:
        followup_id = int(payload.get("followup_id"))
    except (TypeError, ValueError):
        return jsonify({"error": {"code": "bad_request",
                                  "message": "followup_id is required."}}), 400
    ok = bool(payload.get("ok"))
    note = payload.get("note") if isinstance(payload.get("note"), str) else None

    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE " + portal_db._q("portal_followups") + " f"
                    " SET attempts = f.attempts + 1,"
                    " stage = CASE WHEN f.attempts + 1 >="
                    " COALESCE(s.max_attempts, 2) THEN 'done'"
                    " ELSE 'waiting' END,"
                    " next_due_at = NOW()"
                    " + make_interval(hours => COALESCE(s.delay_hours, 24)),"
                    " last_note = %s, updated_at = NOW()"
                    " FROM " + portal_db._q("portal_followup_settings") + " s"
                    " WHERE s.client_id = f.client_id AND f.id = %s"
                    " RETURNING f.stage, f.attempts",
                    (note, followup_id),
                )
                updated = portal_db.rows(cur)
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "followups ack")[0]), 503

    if not updated:
        return jsonify({"error": {"code": "not_found",
                                  "message": "Follow-up not found."}}), 404
    logger.info(
        "followup ack id=%s ok=%s stage=%s attempts=%s",
        followup_id, ok, updated[0].get("stage"), updated[0].get("attempts"),
    )
    return jsonify({"ok": True}), 200


def update_followup_state(client_id, conversation_id, contact_id,
                          contact_name, intent, conn):
    """Arm/cancel the conversation follow-up inside the ingest transaction.

    Qualifying inbound intents arm (or re-arm) the follow-up; any other
    inbound intent cancels a waiting one. Outbound messages (intent None)
    are ignored. Never raises into the caller: wrap in try/except there.
    """
    if intent is None:
        return
    with conn.cursor() as cur:
        settings = _load_settings(cur, client_id)
        if not settings["enabled"]:
            return
        delay_hours = settings["delay_hours"]
        if intent in QUALIFYING_INTENTS:
            cur.execute(
                "INSERT INTO " + portal_db._q("portal_followups") +
                " (client_id, conversation_id, contact_id, contact_name,"
                " stage, intent, attempts, next_due_at)"
                " VALUES (%s, %s, %s, %s, 'waiting', %s, 0,"
                " NOW() + make_interval(hours => %s))"
                " ON CONFLICT (conversation_id) DO UPDATE SET"
                " stage = 'waiting', intent = EXCLUDED.intent,"
                " contact_id = EXCLUDED.contact_id,"
                " contact_name = EXCLUDED.contact_name,"
                " next_due_at = NOW() + make_interval(hours => %s),"
                " updated_at = NOW()",
                (client_id, conversation_id, contact_id, contact_name,
                 intent, delay_hours, delay_hours),
            )
        else:
            cur.execute(
                "UPDATE " + portal_db._q("portal_followups") +
                " SET stage = 'cancelled', updated_at = NOW()"
                " WHERE conversation_id = %s AND stage = 'waiting'",
                (conversation_id,),
            )
