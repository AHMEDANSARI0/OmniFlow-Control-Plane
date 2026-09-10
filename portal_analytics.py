"""Portal analytics (customer Bearer, read-only aggregates).

Read-only rollups over conversations, messages, the action audit log and
knowledge-base usage. Everything is computed with SQL aggregates for the
requested window (7, 14 or 30 days); per-day buckets are zero-filled in UTC
so the UI always renders a continuous chart. Deterministic: zero AI cost.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import logging

from flask import Blueprint, jsonify, request

from portal_auth import PortalAuthUnavailable, authenticate_portal_request
import portal_db


logger = logging.getLogger("omniflow.portal-analytics")

bp = Blueprint("portal_analytics", __name__, url_prefix="/api/v1")

ALLOWED_WINDOWS = (7, 14, 30)

AUTOMATION_ACTIONS = ("kb.auto_reply", "message.enqueued", "followup.ack")
FOLLOWUP_DELIVERED_PREFIX = "Follow-up delivered%"


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


def _direction_counts(rows) -> Dict[str, int]:
    counts = {"in": 0, "out": 0}
    for row in rows:
        direction = str(row.get("direction") or "")
        if direction in counts:
            counts[direction] = int(row.get("c") or 0)
    return counts


@bp.get("/portal/analytics")
def get_analytics():
    principal, error = _principal_or_error()
    if error is not None:
        return error
    raw_days = (request.args.get("days") or "7").strip()
    try:
        days = int(raw_days)
    except (TypeError, ValueError):
        days = 7
    if days not in ALLOWED_WINDOWS:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "days must be 7, 14 or 30."}}), 400
    client_id = principal["client_id"]

    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS conversations,"
                    " COUNT(*) FILTER (WHERE status = 'closed')"
                    " AS conversations_closed,"
                    " COUNT(DISTINCT contact_id) AS customers"
                    " FROM " + portal_db._q(portal_db.CONV_TABLE) +
                    " WHERE client_id = %s",
                    (client_id,),
                )
                totals_rows = portal_db.rows(cur)

                cur.execute(
                    "SELECT direction, COUNT(*) AS c FROM "
                    + portal_db._q(portal_db.MSGS_TABLE) +
                    " WHERE client_id = %s GROUP BY direction",
                    (client_id,),
                )
                direction_rows = portal_db.rows(cur)

                cur.execute(
                    "SELECT COUNT(*) AS c FROM "
                    + portal_db._q(portal_db.CONV_TABLE) +
                    " WHERE client_id = %s"
                    " AND created_at > NOW() - make_interval(days => %s)",
                    (client_id, days),
                )
                window_conv_rows = portal_db.rows(cur)

                cur.execute(
                    "SELECT created_at::date AS day, direction, COUNT(*) AS c"
                    " FROM " + portal_db._q(portal_db.MSGS_TABLE) +
                    " WHERE client_id = %s"
                    " AND created_at > NOW() - make_interval(days => %s)"
                    " GROUP BY 1, 2",
                    (client_id, days),
                )
                per_day_rows = portal_db.rows(cur)

                cur.execute(
                    "SELECT intent, COUNT(*) AS c FROM "
                    + portal_db._q(portal_db.MSGS_TABLE) +
                    " WHERE client_id = %s AND direction = 'in'"
                    " AND intent IS NOT NULL"
                    " AND created_at > NOW() - make_interval(days => %s)"
                    " GROUP BY intent ORDER BY COUNT(*) DESC LIMIT 8",
                    (client_id, days),
                )
                intent_rows = portal_db.rows(cur)

                cur.execute(
                    "SELECT action, COUNT(*) AS c FROM "
                    + portal_db._q("portal_action_log") +
                    " WHERE client_id = %s"
                    " AND created_at > NOW() - make_interval(days => %s)"
                    " AND action IN %s"
                    " GROUP BY action",
                    (client_id, days, AUTOMATION_ACTIONS),
                )
                automation_rows = portal_db.rows(cur)

                cur.execute(
                    "SELECT COUNT(*) AS c FROM "
                    + portal_db._q("portal_action_log") +
                    " WHERE client_id = %s AND action = 'followup.ack'"
                    " AND note LIKE %s"
                    " AND created_at > NOW() - make_interval(days => %s)",
                    (client_id, FOLLOWUP_DELIVERED_PREFIX, days),
                )
                delivered_rows = portal_db.rows(cur)

                cur.execute(
                    "SELECT title, usage_count FROM "
                    + portal_db._q("portal_kb_entries") +
                    " WHERE client_id = %s AND usage_count > 0"
                    " ORDER BY usage_count DESC, updated_at DESC LIMIT 5",
                    (client_id,),
                )
                entry_rows = portal_db.rows(cur)

                cur.execute(
                    "SELECT COUNT(*) AS created,"
                    " COUNT(*) FILTER (WHERE status = 'closed') AS closed"
                    " FROM " + portal_db._q(portal_db.CONV_TABLE) +
                    " WHERE client_id = %s"
                    " AND created_at > NOW() - make_interval(days => %s)",
                    (client_id, days),
                )
                service_rows = portal_db.rows(cur)

                cur.execute(
                    "SELECT AVG(EXTRACT(EPOCH FROM (t.out_ts - t.first_in)))"
                    " AS avg_seconds,"
                    " PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY"
                    " EXTRACT(EPOCH FROM (t.out_ts - t.first_in)))"
                    " AS median_seconds,"
                    " COUNT(*) AS answered"
                    " FROM ("
                    " SELECT i.conversation_id, i.first_in,"
                    " (SELECT MIN(o.created_at) FROM "
                    + portal_db._q(portal_db.MSGS_TABLE) +
                    " o WHERE o.conversation_id = i.conversation_id"
                    " AND o.direction = 'out'"
                    " AND o.created_at > i.first_in) AS out_ts"
                    " FROM (SELECT conversation_id, MIN(created_at) AS first_in"
                    " FROM " + portal_db._q(portal_db.MSGS_TABLE) +
                    " WHERE client_id = %s AND direction = 'in'"
                    " AND created_at > NOW() - make_interval(days => %s)"
                    " GROUP BY conversation_id) i"
                    " ) t"
                    " WHERE t.out_ts IS NOT NULL",
                    (client_id, days),
                )
                frt_rows = portal_db.rows(cur)
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "portal analytics")[0]), 503

    totals_row = totals_rows[0] if totals_rows else {}
    all_time = _direction_counts(direction_rows)
    window_conversations = int(
        (window_conv_rows[0].get("c") if window_conv_rows else 0) or 0
    )

    # Zero-filled UTC day buckets so the chart is continuous.
    today = datetime.now(timezone.utc).date()
    buckets: Dict[str, Dict[str, int]] = {}
    day_order: List[str] = []
    for offset in range(days - 1, -1, -1):
        key = (today - timedelta(days=offset)).isoformat()
        buckets[key] = {"inbound": 0, "outbound": 0}
        day_order.append(key)
    for row in per_day_rows:
        key = str(row.get("day") or "")
        if key in buckets:
            direction = str(row.get("direction") or "in")
            field = "outbound" if direction == "out" else "inbound"
            buckets[key][field] = int(row.get("c") or 0)
    per_day = [
        {"day": key, "inbound": buckets[key]["inbound"],
         "outbound": buckets[key]["outbound"]}
        for key in day_order
    ]
    window_in = sum(item["inbound"] for item in per_day)
    window_out = sum(item["outbound"] for item in per_day)

    automation = {
        str(row.get("action") or ""): int(row.get("c") or 0)
        for row in automation_rows
    }
    delivered = int((delivered_rows[0].get("c") if delivered_rows else 0) or 0)

    service_row = service_rows[0] if service_rows else {}
    window_created = int(service_row.get("created") or 0)
    window_closed = int(service_row.get("closed") or 0)
    resolution_rate = (
        round(window_closed / window_created * 100, 1)
        if window_created > 0 else None
    )
    frt_row = frt_rows[0] if frt_rows else {}
    frt_answered = int(frt_row.get("answered") or 0)

    def _round_seconds(value):
        if value is None:
            return None
        try:
            return round(float(value), 1)
        except (TypeError, ValueError):
            return None

    frt_avg = _round_seconds(frt_row.get("avg_seconds")) if frt_answered else None
    frt_median = _round_seconds(frt_row.get("median_seconds")) if frt_answered else None

    intents = [
        {"intent": str(row.get("intent") or ""), "count": int(row.get("c") or 0)}
        for row in intent_rows
    ]
    top_entries = [
        {"title": str(row.get("title") or ""),
         "usage_count": int(row.get("usage_count") or 0)}
        for row in entry_rows
    ]

    return jsonify({
        "days": days,
        "totals": {
            "conversations": int(totals_row.get("conversations") or 0),
            "conversations_closed": int(
                totals_row.get("conversations_closed") or 0
            ),
            "customers": int(totals_row.get("customers") or 0),
            "messages_in": all_time["in"],
            "messages_out": all_time["out"],
        },
        "window": {
            "conversations": window_conversations,
            "messages_in": window_in,
            "messages_out": window_out,
            "instant_answers": automation.get("kb.auto_reply", 0),
            "followups_delivered": delivered,
            "replies_queued": automation.get("message.enqueued", 0),
        },
        "service": {
            "resolution_rate": resolution_rate,
            "frt_avg_seconds": frt_avg,
            "frt_median_seconds": frt_median,
            "answered_conversations": frt_answered,
        },
        "per_day": per_day,
        "intents": intents,
        "top_entries": top_entries,
    }), 200
