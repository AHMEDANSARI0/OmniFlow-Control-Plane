"""Deeper commerce analytics on checkout links: per-customer LTV
(new / repeat / VIP tiers) and product-wise sales aggregated from PAID
links' item JSON. Deterministic Python-side aggregation over the recent
paid window - no SQL JSON wizardry, fully testable."""
import logging
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, request

from portal_auth import (
    PortalAuthUnavailable,
    authenticate_portal_request,
    ensure_human_principal,
)
import portal_checkout
import portal_db

bp = Blueprint("portal_analytics", __name__, url_prefix="/api/v1/portal")
logger = logging.getLogger(__name__)

PAID_STATUSES = ("paid", "shipped", "delivered")
MAX_SCAN = 500
VIP_SPEND = 25000
REPEAT_MIN = 2
VIP_ORDERS = 4


def _principal_or_error():
    try:
        principal = authenticate_portal_request()
    except PortalAuthUnavailable as error:
        return None, (jsonify({"error": {"code": "portal_unavailable",
                                         "message": "Try again shortly."}}),
                      503)
    if principal is None:
        return None, (jsonify({"error": {"code": "unauthorized",
                                         "message": "Sign in required."}}),
                      401)
    return principal, None


def _parse_days(raw: Any) -> int:
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return 30
    return min(max(days, 1), 365)


def _fetch_paid_links(cur, client_id: int, days: int) -> List[Dict[str, Any]]:
    cur.execute(
        "SELECT id, contact_id, title, items, total, paid_amount, discount,"
        " status, created_at FROM " + portal_db._q(portal_checkout.LINKS_TABLE)
        + " WHERE client_id = %s AND status IN ('paid', 'shipped',"
        " 'delivered')"
        " AND created_at >= NOW() - (%s || ' days')::interval"
        " ORDER BY id DESC LIMIT %s",
        (client_id, str(days), MAX_SCAN),
    )
    return portal_db.rows(cur)


def _customer_tier(orders: int, spend: float) -> str:
    if orders >= VIP_ORDERS or spend >= VIP_SPEND:
        return "vip"
    if orders >= REPEAT_MIN:
        return "repeat"
    return "new"


@bp.get("/insights/customer-analytics")
def customer_analytics():
    principal, error = _principal_or_error()
    if error:
        return error
    days = _parse_days(request.args.get("days"))
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            portal_checkout._ensure_checkout_tables(conn)
            with conn.cursor() as cur:
                rows = _fetch_paid_links(cur, principal["client_id"], days)
        finally:
            conn.close()
    except Exception as error:
        logger.warning("customer analytics failed: %s", error)
        return jsonify(portal_db.portal_unavailable(
            error, "customer analytics")[0]), 503
    by_contact: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        contact = str(row.get("contact_id") or "").strip()
        if not contact:
            continue
        total = round(float(row.get("total") or 0), 2)
        entry = by_contact.setdefault(contact, {
            "contact_id": contact, "orders": 0, "spend": 0.0,
            "last_order": None,
        })
        entry["orders"] += 1
        entry["spend"] = round(entry["spend"] + total, 2)
        created = portal_checkout._iso(row.get("created_at"))
        if created and (entry["last_order"] is None
                        or created > entry["last_order"]):
            entry["last_order"] = created
    customers = []
    for entry in by_contact.values():
        entry["avg_order"] = round(
            entry["spend"] / entry["orders"], 2) if entry["orders"] else 0
        entry["tier"] = _customer_tier(entry["orders"], entry["spend"])
        customers.append(entry)
    customers.sort(key=lambda e: (-e["spend"], e["contact_id"]))
    tiers = {"new": 0, "repeat": 0, "vip": 0}
    for entry in customers:
        tiers[entry["tier"]] += 1
    return jsonify({"days": days, "customers": customers[:50],
                    "tiers": tiers,
                    "repeat_share": round(
                        (tiers["repeat"] + tiers["vip"])
                        / len(customers), 2) if customers else 0}), 200


@bp.get("/insights/product-analytics")
def product_analytics():
    principal, error = _principal_or_error()
    if error:
        return error
    days = _parse_days(request.args.get("days"))
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            portal_checkout._ensure_checkout_tables(conn)
            with conn.cursor() as cur:
                rows = _fetch_paid_links(cur, principal["client_id"], days)
        finally:
            conn.close()
    except Exception as error:
        logger.warning("product analytics failed: %s", error)
        return jsonify(portal_db.portal_unavailable(
            error, "product analytics")[0]), 503
    by_product: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        items = row.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            qty = int(item.get("qty") or 0)
            price = float(item.get("price") or 0)
            entry = by_product.setdefault(name, {
                "name": name, "qty_sold": 0, "revenue": 0.0, "orders": 0,
            })
            entry["qty_sold"] += qty
            entry["revenue"] = round(entry["revenue"] + qty * price, 2)
            entry["orders"] += 1
    products = sorted(by_product.values(),
                      key=lambda e: (-e["revenue"], e["name"]))[:50]
    return jsonify({"days": days, "products": products}), 200
