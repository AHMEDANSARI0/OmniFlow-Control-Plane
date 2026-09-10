"""Portal team ops (multi-user team, roles, assignment, internal notes).

Team: the workspace owner is seeded automatically from the signed-in
principal on first use; owner/admin manage members (add, change role,
enable/disable, remove). Roles: owner > admin > agent - team management is
owner/admin only; every active member can be assigned conversations and
write internal notes. Notes are strictly internal: they never reach the
customer and require no connector involvement. All deterministic: zero AI
cost, no bridge changes, no bot restart.
"""

import logging
import os
import re
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, request

from portal_auth import (
    PortalAuthUnavailable,
    authenticate_portal_request,
    ensure_human_principal,
)
import portal_db


logger = logging.getLogger("omniflow.portal-team")

bp = Blueprint("portal_team", __name__, url_prefix="/api/v1")

TEAM_TABLE = portal_db.TEAM_TABLE
NOTES_TABLE = portal_db.NOTES_TABLE

MANAGE_ROLES = ("owner", "admin")
ASSIGNABLE_ROLES = ("admin", "agent")
MAX_NOTE_BODY = 2000
MAX_NAME_LEN = 80
MAX_EMAIL_LEN = 120
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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


def _iso(value) -> Optional[str]:
    """ISO-8601 string for timestamps coming out of the driver, else None."""
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _clean_email(value) -> str:
    return str(value or "").strip().lower()[:MAX_EMAIL_LEN]


def _clean_name(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())[:MAX_NAME_LEN]


def _member_public(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": int(row.get("id") or 0),
        "email": row.get("email") or "",
        "name": row.get("name") or "",
        "role": row.get("role") or "agent",
        "status": row.get("status") or "active",
        "created_at": _iso(row.get("created_at")),
    }


def _note_public(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": int(row.get("id") or 0),
        "author_email": row.get("author_email") or "",
        "author_name": row.get("author_name") or "",
        "body": row.get("body") or "",
        "created_at": _iso(row.get("created_at")),
    }


def _fetch_member_by_id(cur, client_id, member_id) -> Optional[Dict[str, Any]]:
    cur.execute(
        "SELECT id, email, name, role, status, created_at FROM "
        + portal_db._q(TEAM_TABLE) +
        " WHERE id = %s AND client_id = %s LIMIT 1",
        (member_id, client_id),
    )
    rows = portal_db.rows(cur)
    return rows[0] if rows else None


def _fetch_member_by_email(cur, client_id, email) -> Optional[Dict[str, Any]]:
    cur.execute(
        "SELECT id, email, name, role, status, created_at FROM "
        + portal_db._q(TEAM_TABLE) +
        " WHERE client_id = %s AND email = %s LIMIT 1",
        (client_id, email),
    )
    rows = portal_db.rows(cur)
    return rows[0] if rows else None


def _my_role(cur, client_id, principal) -> Optional[str]:
    email = _clean_email(principal.get("email"))
    if not email:
        return None
    row = _fetch_member_by_email(cur, client_id, email)
    if not row or (row.get("status") or "active") != "active":
        return None
    return row.get("role") or "agent"


def _seed_owner_if_empty(cur, client_id, principal) -> None:
    """First team use seeds the signed-in human as the workspace owner."""
    email = _clean_email(principal.get("email"))
    if not email:
        return
    cur.execute(
        "SELECT 1 FROM " + portal_db._q(TEAM_TABLE) +
        " WHERE client_id = %s LIMIT 1",
        (client_id,),
    )
    if portal_db.rows(cur):
        return
    cur.execute(
        "INSERT INTO " + portal_db._q(TEAM_TABLE) +
        " (client_id, email, name, role, status)"
        " VALUES (%s, %s, %s, 'owner', 'active')"
        " ON CONFLICT (client_id, email) DO NOTHING",
        (client_id, email, _clean_name(principal.get("display_name"))),
    )


def _conversation_exists(cur, client_id, conversation_id) -> bool:
    cur.execute(
        "SELECT 1 FROM " + portal_db._q(portal_db.CONV_TABLE) +
        " WHERE id = %s AND client_id = %s LIMIT 1",
        (conversation_id, client_id),
    )
    return bool(portal_db.rows(cur))


@bp.get("/portal/team")
def list_team():
    principal, error = _principal_or_error()
    if error is not None:
        return error
    client_id = principal["client_id"]
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                _seed_owner_if_empty(cur, client_id, principal)
                cur.execute(
                    "SELECT id, email, name, role, status, created_at FROM "
                    + portal_db._q(TEAM_TABLE) +
                    " WHERE client_id = %s"
                    " ORDER BY CASE WHEN role = 'owner' THEN 0"
                    " WHEN role = 'admin' THEN 1 ELSE 2 END, id ASC",
                    (client_id,),
                )
                members = portal_db.rows(cur)
                my_role = _my_role(cur, client_id, principal)
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "portal team list")[0]), 503
    return jsonify({
        "members": [_member_public(m) for m in members],
        "my_role": my_role,
    }), 200


@bp.post("/portal/team")
def add_team_member():
    principal, error = _principal_or_error()
    if error is not None:
        return error
    forbidden = ensure_human_principal(principal)
    if forbidden is not None:
        return forbidden
    client_id = principal["client_id"]
    payload = request.get_json(silent=True) or {}
    email = _clean_email(payload.get("email"))
    name = _clean_name(payload.get("name"))
    role = str(payload.get("role") or "agent").strip().lower()
    if not email or not EMAIL_RE.match(email):
        return jsonify({"error": {"code": "bad_request",
                                  "message": "A valid email is required."}}), 400
    if role not in ASSIGNABLE_ROLES:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "role must be admin or agent."}}), 400
    inserted = None
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                _seed_owner_if_empty(cur, client_id, principal)
                my_role = _my_role(cur, client_id, principal)
                if my_role not in MANAGE_ROLES:
                    return jsonify({"error": {"code": "forbidden_role",
                                              "message": "Only the owner or an admin can manage the team."}}), 403
                cur.execute(
                    "INSERT INTO " + portal_db._q(TEAM_TABLE) +
                    " (client_id, email, name, role, status)"
                    " VALUES (%s, %s, %s, %s, 'active')"
                    " ON CONFLICT (client_id, email) DO NOTHING"
                    " RETURNING id, email, name, role, status, created_at",
                    (client_id, email, name, role),
                )
                rows = portal_db.rows(cur)
                if not rows:
                    return jsonify({"error": {"code": "email_exists",
                                              "message": "This email is already on the team."}}), 409
                inserted = rows[0]
                portal_db.log_action(
                    cur,
                    client_id,
                    "team.added",
                    "customer_user",
                    principal.get("user_id"),
                    None,
                    "Team member added: " + email + " (" + role + ")",
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "portal team add")[0]), 503
    return jsonify({"ok": True, "member": _member_public(inserted)}), 200


@bp.patch("/portal/team/<int:member_id>")
def update_team_member(member_id: int):
    principal, error = _principal_or_error()
    if error is not None:
        return error
    forbidden = ensure_human_principal(principal)
    if forbidden is not None:
        return forbidden
    client_id = principal["client_id"]
    payload = request.get_json(silent=True) or {}
    new_role = payload.get("role")
    new_status = payload.get("status")
    if new_role is None and new_status is None:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Nothing to update."}}), 400
    if new_role is not None and str(new_role).strip().lower() not in ASSIGNABLE_ROLES:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "role must be admin or agent."}}), 400
    if new_status is not None and str(new_status).strip().lower() not in ("active", "disabled"):
        return jsonify({"error": {"code": "bad_request",
                                  "message": "status must be active or disabled."}}), 400
    updated = None
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                my_role = _my_role(cur, client_id, principal)
                if my_role not in MANAGE_ROLES:
                    return jsonify({"error": {"code": "forbidden_role",
                                              "message": "Only the owner or an admin can manage the team."}}), 403
                target = _fetch_member_by_id(cur, client_id, member_id)
                if not target:
                    return jsonify({"error": {"code": "not_found",
                                              "message": "Team member not found."}}), 404
                if (target.get("role") or "agent") == "owner":
                    return jsonify({"error": {"code": "owner_locked",
                                              "message": "The workspace owner cannot be changed here."}}), 403
                my_email = _clean_email(principal.get("email"))
                if my_email and str(target.get("email") or "").lower() == my_email:
                    return jsonify({"error": {"code": "self_change_blocked",
                                              "message": "You cannot change your own role or status."}}), 400
                sets = ["updated_at = NOW()"]
                params: List[Any] = []
                if new_role is not None:
                    sets.append("role = %s")
                    params.append(str(new_role).strip().lower())
                if new_status is not None:
                    sets.append("status = %s")
                    params.append(str(new_status).strip().lower())
                params.append(member_id)
                params.append(client_id)
                cur.execute(
                    "UPDATE " + portal_db._q(TEAM_TABLE) +
                    " SET " + ", ".join(sets) +
                    " WHERE id = %s AND client_id = %s"
                    " RETURNING id, email, name, role, status, created_at",
                    tuple(params),
                )
                rows = portal_db.rows(cur)
                if not rows:
                    return jsonify({"error": {"code": "not_found",
                                              "message": "Team member not found."}}), 404
                updated = rows[0]
                portal_db.log_action(
                    cur,
                    client_id,
                    "team.updated",
                    "customer_user",
                    principal.get("user_id"),
                    None,
                    "Team member updated: " + str(updated.get("email") or ""),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "portal team update")[0]), 503
    return jsonify({"ok": True, "member": _member_public(updated)}), 200


@bp.delete("/portal/team/<int:member_id>")
def remove_team_member(member_id: int):
    principal, error = _principal_or_error()
    if error is not None:
        return error
    forbidden = ensure_human_principal(principal)
    if forbidden is not None:
        return forbidden
    client_id = principal["client_id"]
    removed_email = ""
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                my_role = _my_role(cur, client_id, principal)
                if my_role not in MANAGE_ROLES:
                    return jsonify({"error": {"code": "forbidden_role",
                                              "message": "Only the owner or an admin can manage the team."}}), 403
                target = _fetch_member_by_id(cur, client_id, member_id)
                if not target:
                    return jsonify({"error": {"code": "not_found",
                                              "message": "Team member not found."}}), 404
                if (target.get("role") or "agent") == "owner":
                    return jsonify({"error": {"code": "owner_locked",
                                              "message": "The workspace owner cannot be removed."}}), 403
                my_email = _clean_email(principal.get("email"))
                if my_email and str(target.get("email") or "").lower() == my_email:
                    return jsonify({"error": {"code": "self_change_blocked",
                                              "message": "You cannot remove yourself."}}), 400
                cur.execute(
                    "DELETE FROM " + portal_db._q(TEAM_TABLE) +
                    " WHERE id = %s AND client_id = %s RETURNING email",
                    (member_id, client_id),
                )
                rows = portal_db.rows(cur)
                removed_email = str(rows[0].get("email") or "") if rows else ""
                portal_db.log_action(
                    cur,
                    client_id,
                    "team.removed",
                    "customer_user",
                    principal.get("user_id"),
                    None,
                    "Team member removed: " + removed_email,
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "portal team remove")[0]), 503
    return jsonify({"ok": True, "removed_email": removed_email}), 200


@bp.post("/portal/conversations/<int:conversation_id>/assign")
def assign_conversation(conversation_id: int):
    principal, error = _principal_or_error()
    if error is not None:
        return error
    forbidden = ensure_human_principal(principal)
    if forbidden is not None:
        return forbidden
    client_id = principal["client_id"]
    payload = request.get_json(silent=True) or {}
    raw = payload.get("assignee_email")
    assignee = _clean_email(raw) if raw is not None else ""
    assignee_name = None
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                if not _conversation_exists(cur, client_id, conversation_id):
                    return jsonify({"error": {"code": "not_found",
                                              "message": "Conversation not found."}}), 404
                if assignee:
                    _seed_owner_if_empty(cur, client_id, principal)
                    member = _fetch_member_by_email(cur, client_id, assignee)
                    if not member or (member.get("status") or "active") != "active":
                        return jsonify({"error": {"code": "assignee_not_found",
                                                  "message": "That teammate is not an active member."}}), 404
                cur.execute(
                    "UPDATE " + portal_db._q(portal_db.CONV_TABLE) +
                    " SET assigned_to = %s WHERE id = %s AND client_id = %s",
                    (assignee or None, conversation_id, client_id),
                )
                if assignee:
                    cur.execute(
                        "SELECT name FROM " + portal_db._q(TEAM_TABLE) +
                        " WHERE client_id = %s AND email = %s LIMIT 1",
                        (client_id, assignee),
                    )
                    rows = portal_db.rows(cur)
                    assignee_name = str(rows[0].get("name") or "") if rows else None
                portal_db.log_action(
                    cur,
                    client_id,
                    "conversation.assigned" if assignee else "conversation.unassigned",
                    "customer_user",
                    principal.get("user_id"),
                    conversation_id,
                    ("Assigned to " + assignee) if assignee else "Assignment cleared.",
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "conversation assign")[0]), 503
    return jsonify({
        "ok": True,
        "assigned_to": assignee or None,
        "assignee_name": assignee_name,
    }), 200


@bp.get("/portal/conversations/<int:conversation_id>/notes")
def list_notes(conversation_id: int):
    principal, error = _principal_or_error()
    if error is not None:
        return error
    client_id = principal["client_id"]
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                if not _conversation_exists(cur, client_id, conversation_id):
                    return jsonify({"error": {"code": "not_found",
                                              "message": "Conversation not found."}}), 404
                cur.execute(
                    "SELECT id, author_email, author_name, body, created_at FROM "
                    + portal_db._q(NOTES_TABLE) +
                    " WHERE conversation_id = %s AND client_id = %s"
                    " ORDER BY id DESC LIMIT 100",
                    (conversation_id, client_id),
                )
                notes = portal_db.rows(cur)
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "conversation notes")[0]), 503
    return jsonify({"notes": [_note_public(n) for n in notes]}), 200


@bp.post("/portal/conversations/<int:conversation_id>/notes")
def add_note(conversation_id: int):
    principal, error = _principal_or_error()
    if error is not None:
        return error
    forbidden = ensure_human_principal(principal)
    if forbidden is not None:
        return forbidden
    client_id = principal["client_id"]
    payload = request.get_json(silent=True) or {}
    body = str(payload.get("body") or "").strip()
    if not body:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Note text is required."}}), 400
    if len(body) > MAX_NOTE_BODY:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Notes must be 2000 characters or fewer."}}), 400
    note_row = None
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                if not _conversation_exists(cur, client_id, conversation_id):
                    return jsonify({"error": {"code": "not_found",
                                              "message": "Conversation not found."}}), 404
                cur.execute(
                    "INSERT INTO " + portal_db._q(NOTES_TABLE) +
                    " (client_id, conversation_id, author_email, author_name, body)"
                    " VALUES (%s, %s, %s, %s, %s)"
                    " RETURNING id, author_email, author_name, body, created_at",
                    (client_id, conversation_id,
                     _clean_email(principal.get("email")),
                     _clean_name(principal.get("display_name")),
                     body),
                )
                rows = portal_db.rows(cur)
                note_row = rows[0] if rows else {}
                portal_db.log_action(
                    cur,
                    client_id,
                    "note.added",
                    "customer_user",
                    principal.get("user_id"),
                    conversation_id,
                    "Internal note added.",
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "conversation note add")[0]), 503
    return jsonify({"ok": True, "note": _note_public(note_row or {})}), 200


# ---------------------------------------------------------------------------
# Agent performance (last N days, deterministic SQL — zero AI)
# ---------------------------------------------------------------------------

try:
    PERF_WINDOW_DAYS = max(
        1, min(30, int(os.environ.get("OF_PERF_WINDOW_DAYS", "7") or 7))
    )
except ValueError:
    PERF_WINDOW_DAYS = 7


@bp.get("/portal/team/performance")
def team_performance():
    """Per-member activity for the last PERF_WINDOW_DAYS + live board counters.

    Deterministic only: replies are queued send_message commands attributed
    through platform_users.email, notes by author_email, plus the current
    open-assignment snapshot. No bridge involvement, no AI cost.
    """
    principal, error = _principal_or_error()
    if error is not None:
        return error
    client_id = principal["client_id"]
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, email, name, role, status FROM "
                    + portal_db._q(TEAM_TABLE) +
                    " WHERE client_id = %s ORDER BY id ASC",
                    (client_id,),
                )
                members = portal_db.rows(cur)
                emails = []
                for member in members:
                    email = str(member.get("email") or "").strip().lower()
                    if email and email not in emails:
                        emails.append(email)
                reply_by_email: Dict[str, int] = {}
                touched_by_email: Dict[str, int] = {}
                if emails:
                    cur.execute(
                        "SELECT LOWER(pu.email) AS email, COUNT(*) AS replies_sent,"
                        " COUNT(DISTINCT cmd.payload->>'conversation_id')"
                        " AS conversations_touched"
                        " FROM " + portal_db._q(portal_db.CMD_TABLE) + " cmd"
                        " JOIN " + portal_db._q(portal_db.USERS_TABLE) + " pu"
                        " ON pu.id = cmd.requested_by"
                        " WHERE cmd.client_id = %s AND cmd.action = 'send_message'"
                        " AND cmd.created_at >= NOW()"
                        " - CAST(%s AS INT) * INTERVAL '1 day'"
                        " AND LOWER(pu.email) = ANY(%s)"
                        " GROUP BY LOWER(pu.email)",
                        (client_id, PERF_WINDOW_DAYS, emails),
                    )
                    for row in portal_db.rows(cur):
                        email = str(row.get("email") or "").strip().lower()
                        reply_by_email[email] = int(row.get("replies_sent") or 0)
                        touched_by_email[email] = int(
                            row.get("conversations_touched") or 0
                        )
                notes_by_email: Dict[str, int] = {}
                cur.execute(
                    "SELECT LOWER(author_email) AS email, COUNT(*) AS notes_added"
                    " FROM " + portal_db._q(NOTES_TABLE) +
                    " WHERE client_id = %s"
                    " AND created_at >= NOW() - CAST(%s AS INT) * INTERVAL '1 day'"
                    " GROUP BY LOWER(author_email)",
                    (client_id, PERF_WINDOW_DAYS),
                )
                for row in portal_db.rows(cur):
                    email = str(row.get("email") or "").strip().lower()
                    notes_by_email[email] = int(row.get("notes_added") or 0)
                assigned_by_email: Dict[str, int] = {}
                cur.execute(
                    "SELECT LOWER(assigned_to) AS email, COUNT(*) AS open_count"
                    " FROM " + portal_db._q(portal_db.CONV_TABLE) +
                    " WHERE client_id = %s AND status = 'open'"
                    " AND assigned_to IS NOT NULL"
                    " GROUP BY LOWER(assigned_to)",
                    (client_id,),
                )
                for row in portal_db.rows(cur):
                    email = str(row.get("email") or "").strip().lower()
                    assigned_by_email[email] = int(row.get("open_count") or 0)
                cur.execute(
                    "SELECT COUNT(*) FILTER (WHERE status = 'open') AS open_total,"
                    " COUNT(*) FILTER (WHERE status = 'open'"
                    " AND assigned_to IS NULL) AS unassigned_open"
                    " FROM " + portal_db._q(portal_db.CONV_TABLE) +
                    " WHERE client_id = %s",
                    (client_id,),
                )
                board_rows = portal_db.rows(cur)
                board_row = board_rows[0] if board_rows else {}
                cur.execute(
                    "SELECT score, COUNT(*) AS total, MAX(answered_at) AS latest"
                    " FROM " + portal_db._q(portal_db.CSAT_TABLE) +
                    " WHERE client_id = %s AND score IS NOT NULL"
                    " AND answered_at >= NOW()"
                    " - CAST(%s AS INT) * INTERVAL '1 day'"
                    " GROUP BY score"
                    " ORDER BY score ASC",
                    (client_id, PERF_WINDOW_DAYS),
                )
                csat_rows = portal_db.rows(cur)
                csat_total = sum(int(row.get("total") or 0) for row in csat_rows)
                csat_weighted = sum(
                    int(row.get("score") or 0) * int(row.get("total") or 0)
                    for row in csat_rows
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "team performance")[0]), 503
    performance = []
    total_replies = 0
    for member in members:
        email = str(member.get("email") or "").strip().lower()
        replies_sent = reply_by_email.get(email, 0)
        total_replies += replies_sent
        role = member.get("role")
        performance.append({
            "id": member.get("id"),
            "email": member.get("email"),
            "name": member.get("name"),
            "role": role if role in ("owner", "admin", "agent") else "agent",
            "status": "disabled" if member.get("status") == "disabled" else "active",
            "replies_sent": replies_sent,
            "conversations_touched": touched_by_email.get(email, 0),
            "notes_added": notes_by_email.get(email, 0),
            "assigned_open": assigned_by_email.get(email, 0),
        })
    performance.sort(
        key=lambda item: (
            -item["replies_sent"],
            str(item.get("name") or item.get("email") or "").lower(),
        )
    )
    dist = [0, 0, 0, 0, 0]
    recent = []
    for row in csat_rows:
        score = int(row.get("score") or 0)
        total = int(row.get("total") or 0)
        if 1 <= score <= 5:
            dist[score - 1] = total
        latest = _iso(row.get("latest"))
        if latest:
            recent.append({"score": score, "at": latest})
    recent.sort(key=lambda item: item["at"], reverse=True)
    recent = recent[:3]
    csat_answered = sum(dist)
    csat_avg = (
        round(csat_weighted / csat_answered, 1) if csat_answered else None
    )
    return jsonify({
        "ok": True,
        "window_days": PERF_WINDOW_DAYS,
        "members": performance,
        "board": {
            "open_conversations": int(board_row.get("open_total") or 0),
            "unassigned_open": int(board_row.get("unassigned_open") or 0),
            "replies_sent": total_replies,
            "csat_avg": csat_avg,
            "csat_answered": csat_answered,
            "csat_dist": dist,
            "csat_low": [
                {"score": score, "count": total}
                for score, total in [(i + 1, dist[i]) for i in range(2)]
                if total
            ],
            "csat_recent": recent,
        },
    }), 200
