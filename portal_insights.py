"""Workspace insights: weekly summary and the broadcast calendar."""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from flask import Blueprint, jsonify, request

from portal_auth import (
    PortalAuthUnavailable,
    authenticate_portal_request,
)
import portal_db

bp = Blueprint("portal_insights", __name__, url_prefix="/api/v1/portal")

logger = logging.getLogger(__name__)

WINDOW_DAYS = 7
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

INTENT_RULES = (
    ("refund", re.compile(r"refund|money back|paisa wapas|wapsi|return kar|cancel kar", re.I)),
    ("order_status", re.compile(r"order|parcel|shipment|tracking|kahan|kahaan|kab aay|kab mile|deliver", re.I)),
    ("complaint", re.compile(r"complaint|shikayat|ghatiya|bewakoof|fraud|scam|hangama|kharab|report kar", re.I)),
    ("pricing", re.compile(r"price|rate|kitna|qimat|cost|discount|sale", re.I)),
    ("delivery", re.compile(r"delivery|shipping|charger|courier|tcs|post", re.I)),
    ("greeting", re.compile(r"^(hi|hello|hey|salam|assalam)\b", re.I)),
)
PHONE_RE = re.compile(r"(\+?92|0)?[\s-]?3\d{2}[\s-]?\d{7}\b")
ORDER_RE = re.compile(r"#(\d{3,})")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def classify_intent(text: str) -> str:
    value = str(text or "")
    for intent, pattern in INTENT_RULES:
        if pattern.search(value):
            return intent
    return "other"


def extract_entities(text: str) -> dict:
    value = str(text or "")
    return {
        "phones": [match.group(0).strip() for match in PHONE_RE.finditer(value)][:5],
        "order_ids": [match.group(1) for match in ORDER_RE.finditer(value)][:5],
        "emails": [match.group(0) for match in EMAIL_RE.finditer(value)][:5],
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


def _rows(cur, sql: str, params: tuple) -> list:
    cur.execute(sql, params)
    return portal_db.rows(cur)


def _day_key(when: Any) -> str:
    """YYYY-MM-DD from a datetime or an ISO string; blank when unknown."""
    if when is None:
        return ""
    if isinstance(when, str):
        return when[:10]
    try:
        return when.strftime("%Y-%m-%d")
    except AttributeError:
        return ""


@bp.get("/insights/intent")
def intent_endpoint():
    principal, error = _principal_or_error()
    if error:
        return error
    text = (request.args.get("text") or "").strip()
    if not text:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "text is required."}}), 400
    return jsonify({"intent": classify_intent(text[:1000]),
                    "entities": extract_entities(text[:1000])}), 200


@bp.get("/insights/weekly")
def weekly_summary():
    principal, error = _principal_or_error()
    if error:
        return error
    client_id = principal["client_id"]
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                chats = _rows(
                    cur,
                    "SELECT COUNT(*) AS total FROM " + portal_db._q(portal_db.CONV_TABLE) +
                    " WHERE client_id = %s"
                    " AND created_at >= NOW() - INTERVAL '" + str(WINDOW_DAYS) + " days'",
                    (client_id,),
                )
                messages = _rows(
                    cur,
                    "SELECT m.direction, COUNT(*) AS total"
                    " FROM " + portal_db._q(portal_db.MSGS_TABLE) + " m"
                    " JOIN " + portal_db._q(portal_db.CONV_TABLE) + " c"
                    " ON c.id = m.conversation_id"
                    " WHERE c.client_id = %s"
                    " AND m.created_at >= NOW() - INTERVAL '" + str(WINDOW_DAYS) + " days'"
                    " GROUP BY m.direction",
                    (client_id,),
                )
                actions = _rows(
                    cur,
                    "SELECT action, COUNT(*) AS total FROM " +
                    portal_db._q("portal_action_log") +
                    " WHERE client_id = %s"
                    " AND created_at >= NOW() - INTERVAL '" + str(WINDOW_DAYS) + " days'"
                    " GROUP BY action",
                    (client_id,),
                )
                broadcasts = _rows(
                    cur,
                    "SELECT COUNT(*) AS total FROM " + portal_db._q(portal_db.BROADCASTS_TABLE) +
                    " WHERE client_id = %s"
                    " AND created_at >= NOW() - INTERVAL '" + str(WINDOW_DAYS) + " days'",
                    (client_id,),
                )
                csat = _rows(
                    cur,
                    "SELECT COUNT(*) AS asked, AVG(score) AS avg_score FROM " +
                    portal_db._q(portal_db.CSAT_TABLE) +
                    " WHERE client_id = %s"
                    " AND requested_at >= NOW() - INTERVAL '" + str(WINDOW_DAYS) + " days'",
                    (client_id,),
                )
                daily_chats = _rows(
                    cur,
                    "SELECT to_char(date_trunc('day', created_at), 'YYYY-MM-DD') AS day,"
                    " COUNT(*) AS total"
                    " FROM " + portal_db._q(portal_db.CONV_TABLE) +
                    " WHERE client_id = %s"
                    " AND created_at >= NOW() - INTERVAL '" + str(WINDOW_DAYS) + " days'"
                    " GROUP BY 1 ORDER BY 1",
                    (client_id,),
                )
                daily_in = _rows(
                    cur,
                    "SELECT to_char(date_trunc('day', m.created_at), 'YYYY-MM-DD') AS day,"
                    " COUNT(*) AS total"
                    " FROM " + portal_db._q(portal_db.MSGS_TABLE) + " m"
                    " JOIN " + portal_db._q(portal_db.CONV_TABLE) + " c"
                    " ON c.id = m.conversation_id"
                    " WHERE c.client_id = %s AND m.direction = 'in'"
                    " AND m.created_at >= NOW() - INTERVAL '" + str(WINDOW_DAYS) + " days'"
                    " GROUP BY 1 ORDER BY 1",
                    (client_id,),
                )
        finally:
            conn.close()
    except Exception as error:
        logger.warning("weekly summary failed: %s", error)
        return jsonify(portal_db.portal_unavailable(error, "weekly summary")[0]), 503

    messages_in = messages_out = 0
    for row in messages:
        total = int(row.get("total") or 0)
        if row.get("direction") == "out":
            messages_out = total
        else:
            messages_in += total
    action_map = {row.get("action"): int(row.get("total") or 0) for row in actions}
    chat_map = {row.get("day"): int(row.get("total") or 0) for row in daily_chats}
    in_map = {row.get("day"): int(row.get("total") or 0) for row in daily_in}

    today = datetime.now(timezone.utc).date()
    days = []
    for offset in range(WINDOW_DAYS - 1, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        days.append({
            "day": day,
            "chats": chat_map.get(day, 0),
            "inbound": in_map.get(day, 0),
        })

    avg_score = None
    if csat and csat[0].get("avg_score") is not None:
        try:
            avg_score = round(float(csat[0]["avg_score"]), 1)
        except (TypeError, ValueError):
            avg_score = None
    return jsonify({
        "chats": int(chats[0].get("total") or 0) if chats else 0,
        "messages_in": messages_in,
        "messages_out": messages_out,
        "cod_confirmed": action_map.get("cod.confirmed", 0),
        "cod_declined": action_map.get("cod.declined", 0),
        "broadcasts": int(broadcasts[0].get("total") or 0) if broadcasts else 0,
        "csat_asked": int(csat[0].get("asked") or 0) if csat else 0,
        "csat_avg": avg_score,
        "days": days,
    }), 200


@bp.get("/insights/calendar")
def broadcast_calendar():
    principal, error = _principal_or_error()
    if error:
        return error
    month = (request.args.get("month") or "").strip()
    if not month:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
    if not MONTH_RE.match(month):
        return jsonify({"error": {"code": "bad_request",
                                  "message": "month must look like 2026-09."}}), 400
    year, mon = int(month[:4]), int(month[5:7])
    if year < 2020 or year > 2100:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "month is out of range."}}), 400
    start = datetime(year, mon, 1, tzinfo=timezone.utc)
    end = datetime(year + (mon == 12), (mon % 12) + 1, 1, tzinfo=timezone.utc)
    client_id = principal["client_id"]
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                rows = _rows(
                    cur,
                    "SELECT id, audience, body, recipient_count,"
                    " send_at, created_at, materialized_at"
                    " FROM " + portal_db._q(portal_db.BROADCASTS_TABLE) +
                    " WHERE client_id = %s"
                    " AND COALESCE(send_at, created_at) >= %s"
                    " AND COALESCE(send_at, created_at) < %s"
                    " ORDER BY COALESCE(send_at, created_at), id",
                    (client_id, start, end),
                )
        finally:
            conn.close()
    except Exception as error:
        logger.warning("broadcast calendar failed: %s", error)
        return jsonify(portal_db.portal_unavailable(error, "broadcast calendar")[0]), 503

    day_map: Dict[str, Dict[str, int]] = {}
    items = []
    for row in rows:
        when = row.get("send_at") or row.get("created_at")
        day = _day_key(when)
        scheduled = row.get("send_at") is not None and row.get("materialized_at") is None
        bucket = day_map.setdefault(day, {"sent": 0, "scheduled": 0})
        if scheduled:
            bucket["scheduled"] += 1
        else:
            bucket["sent"] += 1
        body = str(row.get("body") or "")
        items.append({
            "id": row.get("id"),
            "day": day,
            "audience": row.get("audience") or "all",
            "body": body[:120],
            "recipients": int(row.get("recipient_count") or 0),
            "scheduled": scheduled,
        })
    days = [{"date": key, "sent": value["sent"], "scheduled": value["scheduled"]}
            for key, value in sorted(day_map.items())]
    return jsonify({"month": month, "days": days, "items": items}), 200
