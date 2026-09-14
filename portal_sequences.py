"""Deterministic multi-step sequences for brand-new contacts."""

import csv
import io
import logging
from typing import Any, Dict, Optional

from flask import Blueprint, Response, jsonify, request

from portal_auth import (
    PortalAuthUnavailable,
    authenticate_portal_request,
    ensure_human_principal,
)
import portal_db
import portal_growth

bp = Blueprint("portal_sequences", __name__, url_prefix="/api/v1/portal")

logger = logging.getLogger(__name__)

SEQUENCES_TABLE = "portal_sequences"
STEPS_TABLE = "portal_sequence_steps"
ENROLLMENTS_TABLE = "portal_sequence_enrollments"
MAX_STEPS = 5
MAX_BODY = 1000
MAX_DELAY_HOURS = 168

_SEQ_DDL_READY = False


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


def _normalize_keyword(raw: Any) -> Optional[str]:
    """Lowercase, collapse spaces, cap at 32 chars; None when blank."""
    text = " ".join(str(raw or "").split()).lower()
    if not text:
        return None
    return text[:32]


def _ensure_seq_tables(conn) -> None:
    global _SEQ_DDL_READY
    if _SEQ_DDL_READY:
        return
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS " + portal_db._q(SEQUENCES_TABLE) +
            " (id BIGSERIAL PRIMARY KEY,"
            " client_id BIGINT NOT NULL,"
            " name TEXT NOT NULL,"
            " enabled BOOLEAN NOT NULL DEFAULT FALSE,"
            " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
        )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS " + portal_db._q(STEPS_TABLE) +
            " (id BIGSERIAL PRIMARY KEY,"
            " client_id BIGINT NOT NULL,"
            " sequence_id BIGINT NOT NULL,"
            " step_no INT NOT NULL,"
            " delay_hours INT NOT NULL DEFAULT 0,"
            " body TEXT NOT NULL)"
        )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS " + portal_db._q(ENROLLMENTS_TABLE) +
            " (id BIGSERIAL PRIMARY KEY,"
            " client_id BIGINT NOT NULL,"
            " sequence_id BIGINT NOT NULL,"
            " conversation_id BIGINT NOT NULL,"
            " contact_id TEXT NOT NULL,"
            " contact_name TEXT,"
            " current_step INT NOT NULL DEFAULT 0,"
            " next_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            " status TEXT NOT NULL DEFAULT 'active',"
            " enrolled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            " CONSTRAINT uq_sequence_enrollment"
            " UNIQUE (sequence_id, conversation_id))"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_sequence_enrollments_due"
            " ON " + portal_db._q(ENROLLMENTS_TABLE) +
            " (client_id, status, next_at)"
        )
        cur.execute(
            "ALTER TABLE " + portal_db._q(SEQUENCES_TABLE) +
            " ADD COLUMN IF NOT EXISTS trigger_keyword TEXT"
        )
    conn.commit()
    _SEQ_DDL_READY = True


def _parse_steps(raw: Any, client_id: int, sequence_id: int, cur) -> Optional[int]:
    """Validate and persist steps; returns count or None when invalid."""
    if not isinstance(raw, list) or not (1 <= len(raw) <= MAX_STEPS):
        return None
    cleaned = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return None
        body = str(item.get("body") or "").strip()
        if not body or len(body) > MAX_BODY:
            return None
        try:
            delay = int(item.get("delay_hours", 0))
        except (TypeError, ValueError):
            return None
        if delay < 0 or delay > MAX_DELAY_HOURS:
            return None
        if index == 0 and delay < 0:
            return None
        cleaned.append((index + 1, delay, body))
    for step_no, delay, body in cleaned:
        cur.execute(
            "INSERT INTO " + portal_db._q(STEPS_TABLE) +
            " (client_id, sequence_id, step_no, delay_hours, body)"
            " VALUES (%s, %s, %s, %s, %s)",
            (client_id, sequence_id, step_no, delay, body),
        )
    return len(cleaned)


def _steps_public(rows) -> list:
    return [
        {
            "step_no": int(row.get("step_no") or 0),
            "delay_hours": int(row.get("delay_hours") or 0),
            "body": row.get("body") or "",
        }
        for row in rows
    ]


def _sequence_public(row: Dict[str, Any], steps, active_count: int,
                     completed_count: int = 0) -> dict:
    return {
        "id": row.get("id"),
        "name": row.get("name") or "",
        "enabled": row.get("enabled") is True,
        "created_at": _iso(row.get("created_at")),
        "steps": steps,
        "active_enrollments": active_count,
        "completed_enrollments": completed_count,
        "trigger_keyword": row.get("trigger_keyword") or None,
    }


@bp.get("/sequences")
def list_sequences():
    principal, error = _principal_or_error()
    if error:
        return error
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            _ensure_seq_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, name, enabled, created_at FROM " + portal_db._q(SEQUENCES_TABLE) +
                    " WHERE client_id = %s ORDER BY id DESC LIMIT 20",
                    (principal["client_id"],),
                )
                sequences = portal_db.rows(cur)
                result = []
                for row in sequences:
                    cur.execute(
                        "SELECT step_no, delay_hours, body FROM " + portal_db._q(STEPS_TABLE) +
                        " WHERE client_id = %s AND sequence_id = %s ORDER BY step_no",
                        (principal["client_id"], row.get("id")),
                    )
                    steps = _steps_public(portal_db.rows(cur))
                    cur.execute(
                        "SELECT COUNT(*) AS total FROM " + portal_db._q(ENROLLMENTS_TABLE) +
                        " WHERE client_id = %s AND sequence_id = %s AND status = 'active'",
                        (principal["client_id"], row.get("id")),
                    )
                    counts = portal_db.rows(cur)
                    active_count = int(counts[0].get("total") or 0) if counts else 0
                    cur.execute(
                        "SELECT COUNT(*) AS total FROM " + portal_db._q(ENROLLMENTS_TABLE) +
                        " WHERE client_id = %s AND sequence_id = %s AND status = 'completed'",
                        (principal["client_id"], row.get("id")),
                    )
                    dones = portal_db.rows(cur)
                    completed_count = int(dones[0].get("total") or 0) if dones else 0
                    result.append(_sequence_public(row, steps, active_count,
                                                   completed_count))
        finally:
            conn.close()
    except Exception as error:
        logger.warning("sequences read failed: %s", error)
        return jsonify(portal_db.portal_unavailable(error, "sequences read")[0]), 503
    return jsonify({"sequences": result}), 200


@bp.post("/sequences")
def create_sequence():
    principal, error = _principal_or_error()
    if error:
        return error
    forbidden = ensure_human_principal(principal)
    if forbidden is not None:
        return forbidden
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()[:120]
    if not name:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "A name is required."}}), 400
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            _ensure_seq_tables(conn)
            with conn.cursor() as cur:
                keyword = _normalize_keyword(payload.get("trigger_keyword"))
                cur.execute(
                    "INSERT INTO " + portal_db._q(SEQUENCES_TABLE) +
                    " (client_id, name, enabled, trigger_keyword)"
                    " VALUES (%s, %s, FALSE, %s)"
                    " RETURNING id, name, enabled, created_at, trigger_keyword",
                    (principal["client_id"], name, keyword),
                )
                rows = portal_db.rows(cur)
                created = rows[0] if rows else {}
                sequence_id = int(created.get("id") or 0)
                count = _parse_steps(payload.get("steps"), principal["client_id"],
                                     sequence_id, cur)
                if count is None:
                    conn.rollback()
                    return jsonify({"error": {"code": "bad_request",
                                              "message": "Steps: 1 to 5, each with text and a 0-168h delay."}}), 400
                portal_db.log_action(
                    cur,
                    principal["client_id"],
                    "sequence.created",
                    "customer_user",
                    principal.get("user_id"),
                    None,
                    "Sequence '" + name + "' with " + str(count) + " steps.",
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "sequence create")[0]), 503
    return jsonify({"ok": True,
                    "sequence": _sequence_public(created, [], 0)}), 200


@bp.put("/sequences/<int:sequence_id>")
def update_sequence(sequence_id: int):
    principal, error = _principal_or_error()
    if error:
        return error
    forbidden = ensure_human_principal(principal)
    if forbidden is not None:
        return forbidden
    payload = request.get_json(silent=True) or {}
    changes = []
    params: list = []
    if "name" in payload:
        name = str(payload.get("name") or "").strip()[:120]
        if not name:
            return jsonify({"error": {"code": "bad_request",
                                      "message": "Name cannot be empty."}}), 400
        changes.append("name = %s")
        params.append(name)
    if "trigger_keyword" in payload:
        changes.append("trigger_keyword = %s")
        params.append(_normalize_keyword(payload.get("trigger_keyword")))
    if "enabled" in payload:
        if not isinstance(payload.get("enabled"), bool):
            return jsonify({"error": {"code": "bad_request",
                                      "message": "enabled must be true or false."}}), 400
        changes.append("enabled = %s")
        params.append(payload.get("enabled"))
    if not changes:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Nothing to update."}}), 400
    params.extend([sequence_id, principal["client_id"]])
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            _ensure_seq_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE " + portal_db._q(SEQUENCES_TABLE) +
                    " SET " + ", ".join(changes) +
                    " WHERE id = %s AND client_id = %s"
                    " RETURNING id",
                    tuple(params),
                )
                rows = portal_db.rows(cur)
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "sequence update")[0]), 503
    if not rows:
        return jsonify({"error": {"code": "not_found",
                                  "message": "Sequence not found."}}), 404
    return jsonify({"ok": True}), 200


@bp.put("/sequences/<int:sequence_id>/steps")
def replace_sequence_steps(sequence_id: int):
    principal, error = _principal_or_error()
    if error:
        return error
    forbidden = ensure_human_principal(principal)
    if forbidden is not None:
        return forbidden
    payload = request.get_json(silent=True) or {}
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            _ensure_seq_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM " + portal_db._q(SEQUENCES_TABLE) +
                    " WHERE id = %s AND client_id = %s",
                    (sequence_id, principal["client_id"]),
                )
                found = portal_db.rows(cur)
                if not found:
                    return jsonify({"error": {"code": "not_found",
                                              "message": "Sequence not found."}}), 404
                cur.execute(
                    "DELETE FROM " + portal_db._q(STEPS_TABLE) +
                    " WHERE client_id = %s AND sequence_id = %s",
                    (principal["client_id"], sequence_id),
                )
                count = _parse_steps(payload.get("steps"), principal["client_id"],
                                     sequence_id, cur)
                if count is None:
                    conn.rollback()
                    return jsonify({"error": {"code": "bad_request",
                                              "message": "Steps: 1 to 5, each with text and a 0-168h delay."}}), 400
                portal_db.log_action(
                    cur,
                    principal["client_id"],
                    "sequence.steps_updated",
                    "customer_user",
                    principal.get("user_id"),
                    None,
                    "Sequence #%d now has %d step(s)." % (sequence_id, count),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "sequence steps update")[0]), 503
    return jsonify({"ok": True}), 200


@bp.delete("/sequences/<int:sequence_id>")
def delete_sequence(sequence_id: int):
    principal, error = _principal_or_error()
    if error:
        return error
    forbidden = ensure_human_principal(principal)
    if forbidden is not None:
        return forbidden
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            _ensure_seq_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM " + portal_db._q(ENROLLMENTS_TABLE) +
                    " WHERE client_id = %s AND sequence_id = %s",
                    (principal["client_id"], sequence_id),
                )
                cur.execute(
                    "DELETE FROM " + portal_db._q(STEPS_TABLE) +
                    " WHERE client_id = %s AND sequence_id = %s",
                    (principal["client_id"], sequence_id),
                )
                cur.execute(
                    "DELETE FROM " + portal_db._q(SEQUENCES_TABLE) +
                    " WHERE id = %s AND client_id = %s RETURNING id",
                    (sequence_id, principal["client_id"]),
                )
                rows = portal_db.rows(cur)
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "sequence delete")[0]), 503
    if not rows:
        return jsonify({"error": {"code": "not_found",
                                  "message": "Sequence not found."}}), 404
    return jsonify({"ok": True}), 200


@bp.get("/sequences/<int:sequence_id>/enrollments")
def list_enrollments(sequence_id: int):
    principal, error = _principal_or_error()
    if error:
        return error
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            _ensure_seq_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM " + portal_db._q(SEQUENCES_TABLE) +
                    " WHERE id = %s AND client_id = %s",
                    (sequence_id, principal["client_id"]),
                )
                if not portal_db.rows(cur):
                    return jsonify({"error": {"code": "not_found",
                                              "message": "Sequence not found."}}), 404
                cur.execute(
                    "SELECT id, contact_name, contact_id, current_step, status,"
                    " next_at, enrolled_at FROM " + portal_db._q(ENROLLMENTS_TABLE) +
                    " WHERE client_id = %s AND sequence_id = %s"
                    " ORDER BY id DESC LIMIT 20",
                    (principal["client_id"], sequence_id),
                )
                found = portal_db.rows(cur)
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "enrollments read")[0]), 503
    return jsonify({"enrollments": [
        {
            "id": row.get("id"),
            "contact_name": row.get("contact_name"),
            "contact_id": row.get("contact_id"),
            "current_step": int(row.get("current_step") or 0),
            "status": row.get("status") or "active",
            "next_at": _iso(row.get("next_at")),
            "enrolled_at": _iso(row.get("enrolled_at")),
        }
        for row in found
    ]}), 200


@bp.get("/sequences/<int:sequence_id>/enrollments/export")
def export_sequence_enrollments(sequence_id: int):
    principal, error = _principal_or_error()
    if error:
        return error
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            _ensure_seq_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, name FROM " + portal_db._q(SEQUENCES_TABLE) +
                    " WHERE id = %s AND client_id = %s",
                    (sequence_id, principal["client_id"]),
                )
                if not portal_db.rows(cur):
                    return jsonify({"error": {"code": "not_found",
                                              "message": "Sequence not found."}}), 404
                cur.execute(
                    "SELECT contact_name, contact_id, status, current_step, enrolled_at"
                    " FROM " + portal_db._q(ENROLLMENTS_TABLE) +
                    " WHERE client_id = %s AND sequence_id = %s"
                    " ORDER BY id DESC LIMIT 2000",
                    (principal["client_id"], sequence_id),
                )
                found = portal_db.rows(cur)
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "enrollments export")[0]), 503
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["contact_name", "contact_id", "status", "current_step",
                     "enrolled_at"])
    for row in found:
        writer.writerow([
            row.get("contact_name") or "",
            row.get("contact_id") or "",
            row.get("status") or "active",
            int(row.get("current_step") or 0),
            _iso(row.get("enrolled_at")) or "",
        ])
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition":
                 "attachment; filename=sequence-%d-enrollments.csv" % sequence_id},
    )


def _normalize_contact(value: Any) -> str:
    """Normalize a pasted phone number or id to the WhatsApp jid form."""
    text = str(value or "").strip()
    for char in (" ", "-", "(", ")"):
        text = text.replace(char, "")
    if not text:
        return ""
    if "@" not in text:
        text = text.lstrip("+") + "@c.us"
    return text


@bp.post("/sequences/<int:sequence_id>/enrollments")
def enroll_contacts(sequence_id: int):
    """Manually enroll pasted contacts into a sequence."""
    principal, error = _principal_or_error()
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    raw = payload.get("contacts")
    if not isinstance(raw, list):
        return jsonify({"error": {"code": "bad_request",
                                  "message": "contacts must be a list."}}), 400
    seen: list = []
    for item in raw[:50]:
        contact = _normalize_contact(item)
        if contact and contact not in seen:
            seen.append(contact)
    if not seen:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "No valid contacts."}}), 400
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            _ensure_seq_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM " + portal_db._q(SEQUENCES_TABLE) +
                    " WHERE id = %s AND client_id = %s",
                    (sequence_id, principal["client_id"]),
                )
                if not portal_db.rows(cur):
                    return jsonify({"error": {"code": "not_found",
                                              "message": "Sequence not found."}}), 404
                cur.execute(
                    "INSERT INTO " + portal_db._q(ENROLLMENTS_TABLE) +
                    " (client_id, sequence_id, conversation_id, contact_id,"
                    " contact_name, current_step, next_at)"
                    " SELECT %s, %s, c.id, c.contact_id, c.contact_name, 0,"
                    " NOW() + make_interval(hours => COALESCE(MIN(st.delay_hours), 0))"
                    " FROM " + portal_db._q(portal_db.CONV_TABLE) + " c"
                    " LEFT JOIN " + portal_db._q(STEPS_TABLE) +
                    " st ON st.sequence_id = %s AND st.step_no = 1"
                    " WHERE c.client_id = %s AND c.contact_id = ANY(%s)"
                    " AND NOT EXISTS (SELECT 1 FROM " + portal_db._q(ENROLLMENTS_TABLE) +
                    " e WHERE e.sequence_id = %s AND e.conversation_id = c.id)"
                    " GROUP BY c.id, c.contact_id, c.contact_name"
                    " RETURNING contact_id",
                    (principal["client_id"], sequence_id, sequence_id,
                     principal["client_id"], seen, sequence_id),
                )
                enrolled_rows = portal_db.rows(cur)
                enrolled_ids = {row.get("contact_id") for row in enrolled_rows}
                skipped = [c for c in seen if c not in enrolled_ids]
                portal_db.log_action(
                    cur,
                    principal["client_id"],
                    "sequence.enrolled_manually",
                    "customer_user",
                    principal.get("user_id"),
                    None,
                    "Enrolled " + str(len(enrolled_rows)) + " contact(s) into"
                    " sequence " + str(sequence_id) + ".",
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "sequence enroll")[0]), 503
    return jsonify({"enrolled": len(enrolled_rows), "skipped": skipped}), 200


@bp.delete("/sequences/<int:sequence_id>/enrollments/<int:enrollment_id>")
def cancel_enrollment(sequence_id: int, enrollment_id: int):
    """Stop an active enrollment."""
    principal, error = _principal_or_error()
    if error:
        return error
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            _ensure_seq_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM " + portal_db._q(SEQUENCES_TABLE) +
                    " WHERE id = %s AND client_id = %s",
                    (sequence_id, principal["client_id"]),
                )
                if not portal_db.rows(cur):
                    return jsonify({"error": {"code": "not_found",
                                              "message": "Sequence not found."}}), 404
                cur.execute(
                    "UPDATE " + portal_db._q(ENROLLMENTS_TABLE) +
                    " SET status = 'cancelled'"
                    " WHERE id = %s AND sequence_id = %s AND client_id = %s"
                    " AND status = 'active' RETURNING id",
                    (enrollment_id, sequence_id, principal["client_id"]),
                )
                if not portal_db.rows(cur):
                    return jsonify({"error": {"code": "not_found",
                                              "message": "Enrollment not found or not active."}}), 404
                portal_db.log_action(
                    cur,
                    principal["client_id"],
                    "sequence.enrollment_cancelled",
                    "customer_user",
                    principal.get("user_id"),
                    None,
                    "Cancelled enrollment " + str(enrollment_id) + ".",
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "enrollment cancel")[0]), 503
    return jsonify({"ok": True}), 200

def maybe_enroll_new_contact(client_id, conversation_id, contact_id,
                             contact_name, conn) -> None:
    """Ingest hook: enroll contacts whose conversation has one message."""
    portal_db.ensure_tables()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO " + portal_db._q(ENROLLMENTS_TABLE) +
            " (client_id, sequence_id, conversation_id, contact_id, contact_name,"
            " current_step, next_at)"
            " SELECT %s, s.id, %s, %s, %s, 0,"
            " NOW() + make_interval(hours => COALESCE(MIN(st.delay_hours), 0))"
            " FROM " + portal_db._q(SEQUENCES_TABLE) + " s"
            " LEFT JOIN " + portal_db._q(STEPS_TABLE) +
            " st ON st.sequence_id = s.id AND st.step_no = 1"
            " WHERE s.client_id = %s AND s.enabled IS TRUE"
            " AND NOT EXISTS (SELECT 1 FROM " + portal_db._q(ENROLLMENTS_TABLE) +
            " e WHERE e.sequence_id = s.id AND e.conversation_id = %s)"
            " AND (SELECT COUNT(*) FROM " + portal_db._q(portal_db.MSGS_TABLE) +
            " m WHERE m.conversation_id = %s) <= 1"
            " GROUP BY s.id"
            " RETURNING id",
            (client_id, conversation_id, str(contact_id or ""),
             str(contact_name or ""), client_id, conversation_id, conversation_id),
        )
        enrolled = portal_db.rows(cur)
        if enrolled:
            portal_db.log_action(
                cur,
                client_id,
                "sequence.enrolled",
                "automation",
                None,
                conversation_id,
                "Enrolled " + str(len(enrolled)) + " sequence(s).",
            )


def maybe_enroll_keyword(client_id, conversation_id, contact_id,
                         contact_name, body, conn) -> None:
    """Ingest hook: enroll a contact when their message matches a trigger."""
    keyword = _normalize_keyword(body)
    if not keyword:
        return
    portal_db.ensure_tables()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO " + portal_db._q(ENROLLMENTS_TABLE) +
            " (client_id, sequence_id, conversation_id, contact_id,"
            " contact_name, current_step, next_at)"
            " SELECT %s, s.id, %s, %s, %s, 0,"
            " NOW() + make_interval(hours => COALESCE(MIN(st.delay_hours), 0))"
            " FROM " + portal_db._q(SEQUENCES_TABLE) + " s"
            " LEFT JOIN " + portal_db._q(STEPS_TABLE) +
            " st ON st.sequence_id = s.id AND st.step_no = 1"
            " WHERE s.client_id = %s AND s.enabled IS TRUE"
            " AND s.trigger_keyword = %s"
            " AND NOT EXISTS (SELECT 1 FROM " + portal_db._q(ENROLLMENTS_TABLE) +
            " e WHERE e.sequence_id = s.id AND e.conversation_id = %s)"
            " GROUP BY s.id"
            " RETURNING id",
            (client_id, conversation_id, str(contact_id or ""),
             str(contact_name or ""), client_id, keyword, conversation_id),
        )
        enrolled = portal_db.rows(cur)
        if enrolled:
            portal_db.log_action(
                cur,
                client_id,
                "sequence.enrolled_keyword",
                "automation",
                None,
                conversation_id,
                "Keyword '" + keyword + "' enrolled " + str(len(enrolled))
                + " sequence(s).",
            )


def deliver_due_sequence_steps(cur, client_id: int, conn) -> int:
    """Send due steps for active enrollments (max 5 per poll)."""
    try:
        portal_db.ensure_tables()
        _ensure_seq_tables(conn)
    except Exception:
        return 0
    try:
        cur.execute(
            "SELECT e.id, e.sequence_id, e.current_step, e.contact_id, e.contact_name"
            " FROM " + portal_db._q(ENROLLMENTS_TABLE) + " e"
            " WHERE e.client_id = %s AND e.status = 'active'"
            " AND e.next_at <= NOW()"
            " ORDER BY e.next_at LIMIT 5",
            (client_id,),
        )
        due = portal_db.rows(cur)
        sent = 0
        for row in due:
            current_step = int(row.get("current_step") or 0)
            cur.execute(
                "SELECT step_no, delay_hours, body FROM " + portal_db._q(STEPS_TABLE) +
                " WHERE client_id = %s AND sequence_id = %s AND step_no > %s"
                " ORDER BY step_no LIMIT 2",
                (client_id, row.get("sequence_id"), current_step),
            )
            upcoming = portal_db.rows(cur)
            if not upcoming:
                cur.execute(
                    "UPDATE " + portal_db._q(ENROLLMENTS_TABLE) +
                    " SET status = 'completed' WHERE id = %s",
                    (row.get("id"),),
                )
                continue
            step = upcoming[0]
            display = str(row.get("contact_name") or "").strip()
            body = str(step.get("body") or "").replace(
                "{name}", (display.split(" ")[0] if display else "there")
            )
            portal_growth._send_command(
                cur, client_id, str(row.get("contact_id") or ""), display,
                body, "sequence", broadcast_id=None,
            )
            finished = len(upcoming) == 1
            if finished:
                cur.execute(
                    "UPDATE " + portal_db._q(ENROLLMENTS_TABLE) +
                    " SET current_step = %s, status = 'completed' WHERE id = %s",
                    (int(step.get("step_no") or 0), row.get("id")),
                )
            else:
                cur.execute(
                    "UPDATE " + portal_db._q(ENROLLMENTS_TABLE) +
                    " SET current_step = %s,"
                    " next_at = NOW() + make_interval(hours => %s)"
                    " WHERE id = %s",
                    (int(step.get("step_no") or 0),
                     int(upcoming[1].get("delay_hours") or 0), row.get("id")),
                )
            sent += 1
        if sent:
            conn.commit()
        return sent
    except Exception as error:
        logger.warning("sequence delivery pass failed: %s", error)
        return 0
