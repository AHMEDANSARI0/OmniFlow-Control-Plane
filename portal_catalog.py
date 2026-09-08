"""Portal business core (catalog, industry presets, activity feed).

Businesses list their products and services (name, price, notes) as
structured catalog items. Industry starter presets seed the knowledge base
with proven starter answers in one click. Every meaningful action across
the platform (instant answers, queued replies, status changes, follow-up
deliveries, preset/catalog changes) is appended to the action audit log and
surfaced as a recent-activity feed. Deterministic and zero AI cost.
"""

import logging
from typing import Any, Dict

from flask import Blueprint, jsonify, request

from portal_auth import PortalAuthUnavailable, authenticate_portal_request
import portal_db


logger = logging.getLogger("omniflow.portal-catalog")

bp = Blueprint("portal_catalog", __name__, url_prefix="/api/v1")

CATALOG_TABLE = "portal_catalog"
KB_TABLE = "portal_kb_entries"
ACTIVITY_TABLE = "portal_action_log"
MAX_LIST_ITEMS = 200

# Starter knowledge-base answers per industry. Keywords mix English and
# Roman Urdu; {name} becomes the customer's first name. Applied entries are
# marked category "starter" and can be edited or deleted like any entry.
INDUSTRY_PRESETS = {
    "clothing_fashion": {
        "label": "Clothing & fashion",
        "description": "Sizing, exchange policy, delivery and cash on delivery.",
        "entries": [
            {
                "title": "Delivery time",
                "keywords": "delivery, delivery time, kitne din, shipping",
                "content": (
                    "Hi {name}! We deliver in 2-3 working days across major "
                    "cities. You will receive a tracking message once your "
                    "order is dispatched."
                ),
            },
            {
                "title": "Cash on delivery",
                "keywords": "cod, cash on delivery, payment, advance",
                "content": (
                    "Hi {name}! Yes, cash on delivery is available. You pay "
                    "the rider when your parcel arrives. Advance payment is "
                    "not required."
                ),
            },
            {
                "title": "Exchange policy",
                "keywords": "exchange, return, wapas, refund, size change",
                "content": (
                    "Hi {name}! We offer a 7-day exchange. If the size does "
                    "not fit, message us here and we will arrange the "
                    "exchange — the item must be unworn with tags attached."
                ),
            },
            {
                "title": "Sizes available",
                "keywords": "size, sizes, fitting, measurement",
                "content": (
                    "Hi {name}! We stock sizes S to XXL. Share your usual "
                    "size or measurements and we will confirm availability "
                    "right away."
                ),
            },
            {
                "title": "New arrivals",
                "keywords": "new arrival, latest, stock, available",
                "content": (
                    "Hi {name}! Fresh stock arrives every week. Tell us what "
                    "you are looking for and we will share the latest "
                    "available designs and prices."
                ),
            },
        ],
    },
    "food_restaurant": {
        "label": "Food & restaurant",
        "description": "Menu, timings, delivery areas and deals.",
        "entries": [
            {
                "title": "Delivery time",
                "keywords": "delivery, delivery time, kitna time, kitne din",
                "content": (
                    "Hi {name}! Orders are delivered hot within 40-60 "
                    "minutes inside our delivery area."
                ),
            },
            {
                "title": "Opening hours",
                "keywords": "timing, timings, open, band, khula, hours",
                "content": (
                    "Hi {name}! We are open daily from 12:00 PM to 12:00 "
                    "AM. Late-night orders are welcome until midnight."
                ),
            },
            {
                "title": "Delivery areas",
                "keywords": "area, deliver, address, location, pohnchenge",
                "content": (
                    "Hi {name}! We deliver across the main city area. Share "
                    "your location and we will confirm right away."
                ),
            },
            {
                "title": "Today's deals",
                "keywords": "deal, deals, discount, offer, price",
                "content": (
                    "Hi {name}! Today's deal gives you 20% off on family "
                    "combos. Want us to send the full menu with prices?"
                ),
            },
            {
                "title": "Ordering steps",
                "keywords": "order, kaise, how, book",
                "content": (
                    "Hi {name}! Ordering is simple: send us the items you "
                    "want with your address, and we confirm the total and "
                    "delivery time right here on WhatsApp."
                ),
            },
        ],
    },
    "beauty_salon": {
        "label": "Beauty & salon",
        "description": "Services, rates, appointments and timings.",
        "entries": [
            {
                "title": "Book an appointment",
                "keywords": "appointment, booking, book, time, slot",
                "content": (
                    "Hi {name}! We would love to book you in. Share your "
                    "preferred day and time and we will confirm the slot "
                    "right away."
                ),
            },
            {
                "title": "Services and rates",
                "keywords": "rate, rates, price, charges, service, package",
                "content": (
                    "Hi {name}! Our most-loved packages start from basic "
                    "grooming to full bridal. Tell us which service you are "
                    "interested in and we will share the rates."
                ),
            },
            {
                "title": "Opening hours",
                "keywords": "timing, timings, open, closed, hours, khula",
                "content": (
                    "Hi {name}! We are open Monday to Sunday, 10:00 AM to "
                    "9:00 PM. Walk-ins are welcome; booked slots get "
                    "priority."
                ),
            },
            {
                "title": "Location",
                "keywords": "location, address, where, kahan",
                "content": (
                    "Hi {name}! We are located centrally — share your area "
                    "and we will send exact directions on WhatsApp."
                ),
            },
            {
                "title": "Home service",
                "keywords": "home service, ghar, home, visit",
                "content": (
                    "Hi {name}! Yes, we offer home service for bridal and "
                    "party bookings. Share your date and location for a "
                    "quote."
                ),
            },
        ],
    },
    "electronics_mobiles": {
        "label": "Electronics & mobiles",
        "description": "Prices, warranty, delivery and stock checks.",
        "entries": [
            {
                "title": "Warranty details",
                "keywords": "warranty, guarantee, claim, zimmedari",
                "content": (
                    "Hi {name}! All our products come with an official "
                    "warranty. Keep your receipt and message us here for "
                    "any warranty claim — we handle it end to end."
                ),
            },
            {
                "title": "Delivery time",
                "keywords": "delivery, delivery time, kitne din, shipping",
                "content": (
                    "Hi {name}! We deliver in 2-4 working days with "
                    "cash-on-delivery available nationwide."
                ),
            },
            {
                "title": "Price list",
                "keywords": "price, rate, price list, kitna, cost",
                "content": (
                    "Hi {name}! Prices change with stock, so we share the "
                    "latest confirmed price for each model. Which product "
                    "are you interested in?"
                ),
            },
            {
                "title": "Original products",
                "keywords": "original, copy, first copy, pta, fake",
                "content": (
                    "Hi {name}! We deal in 100% original, PTA-approved "
                    "products only. Every order includes a check-on-delivery "
                    "option for your peace of mind."
                ),
            },
            {
                "title": "Return policy",
                "keywords": "return, exchange, wapas, fault, fault, refund",
                "content": (
                    "Hi {name}! If a product has a fault, message us within "
                    "7 days and we will replace it or fix it under warranty "
                    "— no hassle."
                ),
            },
        ],
    },
    "general_retail": {
        "label": "General retail",
        "description": "Delivery, payment, availability and support basics.",
        "entries": [
            {
                "title": "Delivery time",
                "keywords": "delivery, delivery time, kitne din, shipping",
                "content": (
                    "Hi {name}! We deliver in 2-3 working days. You will get "
                    "a confirmation message as soon as your order ships."
                ),
            },
            {
                "title": "Cash on delivery",
                "keywords": "cod, cash on delivery, payment, advance",
                "content": (
                    "Hi {name}! Cash on delivery is available. You only pay "
                    "when your order reaches you."
                ),
            },
            {
                "title": "Stock availability",
                "keywords": "stock, available, available, mojood",
                "content": (
                    "Hi {name}! Tell us exactly which item you need and we "
                    "will confirm availability and the latest price right "
                    "away."
                ),
            },
            {
                "title": "Support hours",
                "keywords": "timing, hours, open, reply, jawab",
                "content": (
                    "Hi {name}! Our team replies daily from 10:00 AM to "
                    "10:00 PM — leave a message anytime and we will get "
                    "back to you first thing."
                ),
            },
            {
                "title": "Exchange policy",
                "keywords": "exchange, return, wapas, refund",
                "content": (
                    "Hi {name}! We accept exchanges within 7 days if the "
                    "item is unused and in original condition. Just message "
                    "us here to start one."
                ),
            },
        ],
    },
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


def _item_json(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": int(row.get("id") or 0),
        "kind": str(row.get("kind") or "product"),
        "name": str(row.get("name") or ""),
        "price_text": str(row.get("price_text") or ""),
        "notes": str(row.get("notes") or ""),
        "is_active": row.get("is_active") is True,
    }


def _clean_item_payload(raw):
    """Validate a catalog item payload. Returns (fields, error)."""
    if not isinstance(raw, dict):
        return None, (jsonify({"error": {"code": "bad_request",
                                         "message": "item object is required."}}), 400)
    kind = str(raw.get("kind") or "product").strip()
    name = str(raw.get("name") or "").strip()
    price_text = str(raw.get("price_text") or "").strip()
    notes = str(raw.get("notes") or "").strip()
    is_active_raw = raw.get("is_active")
    if is_active_raw is None:
        is_active = True
    else:
        is_active = is_active_raw is True or str(is_active_raw).lower() == "true"
    if kind not in ("product", "service"):
        return None, (jsonify({"error": {"code": "bad_request",
                                         "message": "kind must be product or service."}}), 400)
    if not name or len(name) > 200:
        return None, (jsonify({"error": {"code": "bad_request",
                                         "message": "Name is required (max 200 characters)."}}), 400)
    if len(price_text) > 100:
        return None, (jsonify({"error": {"code": "bad_request",
                                         "message": "Price must be 100 characters or fewer."}}), 400)
    if len(notes) > 1000:
        return None, (jsonify({"error": {"code": "bad_request",
                                         "message": "Notes must be 1000 characters or fewer."}}), 400)
    return {
        "kind": kind,
        "name": name,
        "price_text": price_text,
        "notes": notes,
        "is_active": is_active,
    }, None


@bp.get("/portal/catalog")
def get_catalog():
    principal, error = _principal_or_error()
    if error is not None:
        return error
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, kind, name, price_text, notes, is_active"
                    " FROM " + portal_db._q(CATALOG_TABLE) +
                    " WHERE client_id = %s"
                    " ORDER BY kind, name LIMIT %s",
                    (principal["client_id"], MAX_LIST_ITEMS),
                )
                item_rows = portal_db.rows(cur)
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "portal catalog")[0]), 503
    return jsonify({"items": [_item_json(row) for row in item_rows]}), 200


@bp.post("/portal/catalog")
def create_catalog_item():
    principal, error = _principal_or_error()
    if error is not None:
        return error
    payload = request.get_json(silent=True) or {}
    fields, item_error = _clean_item_payload(payload.get("item"))
    if item_error is not None:
        return item_error
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO " + portal_db._q(CATALOG_TABLE) +
                    " (client_id, kind, name, price_text, notes, is_active)"
                    " VALUES (%s, %s, %s, %s, %s, %s)"
                    " RETURNING id, kind, name, price_text, notes, is_active",
                    (principal["client_id"], fields["kind"], fields["name"],
                     fields["price_text"], fields["notes"], fields["is_active"]),
                )
                created = portal_db.rows(cur)
                if created:
                    portal_db.log_action(
                        cur,
                        principal["client_id"],
                        "catalog.changed",
                        "customer_user",
                        principal.get("user_id"),
                        None,
                        "Added " + fields["kind"] + ": " + fields["name"][:120],
                    )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "portal catalog create")[0]), 503
    return jsonify({"ok": True, "item": _item_json(created[0] if created else {})}), 200


@bp.put("/portal/catalog/<int:item_id>")
def update_catalog_item(item_id):
    principal, error = _principal_or_error()
    if error is not None:
        return error
    payload = request.get_json(silent=True) or {}
    fields, item_error = _clean_item_payload(payload.get("item"))
    if item_error is not None:
        return item_error
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE " + portal_db._q(CATALOG_TABLE) +
                    " SET kind = %s, name = %s, price_text = %s, notes = %s,"
                    " is_active = %s, updated_at = NOW()"
                    " WHERE id = %s AND client_id = %s"
                    " RETURNING id, kind, name, price_text, notes, is_active",
                    (fields["kind"], fields["name"], fields["price_text"],
                     fields["notes"], fields["is_active"],
                     item_id, principal["client_id"]),
                )
                updated = portal_db.rows(cur)
                if updated:
                    portal_db.log_action(
                        cur,
                        principal["client_id"],
                        "catalog.changed",
                        "customer_user",
                        principal.get("user_id"),
                        None,
                        "Updated " + fields["kind"] + ": " + fields["name"][:120],
                    )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "portal catalog update")[0]), 503
    if not updated:
        return jsonify({"error": {"code": "not_found",
                                  "message": "Catalog item not found."}}), 404
    return jsonify({"ok": True, "item": _item_json(updated[0])}), 200


@bp.delete("/portal/catalog/<int:item_id>")
def delete_catalog_item(item_id):
    principal, error = _principal_or_error()
    if error is not None:
        return error
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM " + portal_db._q(CATALOG_TABLE) +
                    " WHERE id = %s AND client_id = %s RETURNING id",
                    (item_id, principal["client_id"]),
                )
                deleted = portal_db.rows(cur)
                if deleted:
                    portal_db.log_action(
                        cur,
                        principal["client_id"],
                        "catalog.changed",
                        "customer_user",
                        principal.get("user_id"),
                        None,
                        "Removed a catalog item.",
                    )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "portal catalog delete")[0]), 503
    if not deleted:
        return jsonify({"error": {"code": "not_found",
                                  "message": "Catalog item not found."}}), 404
    return jsonify({"ok": True}), 200


@bp.get("/portal/presets")
def get_presets():
    principal, error = _principal_or_error()
    if error is not None:
        return error
    presets = [
        {
            "id": key,
            "label": value["label"],
            "description": value["description"],
            "entryCount": len(value["entries"]),
        }
        for key, value in INDUSTRY_PRESETS.items()
    ]
    return jsonify({"presets": presets}), 200


@bp.post("/portal/presets/apply")
def apply_preset():
    principal, error = _principal_or_error()
    if error is not None:
        return error
    payload = request.get_json(silent=True) or {}
    industry = str(payload.get("industry") or "").strip()
    preset = INDUSTRY_PRESETS.get(industry)
    if preset is None:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Unknown industry."}}), 400
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                applied = 0
                skipped = 0
                for entry in preset["entries"]:
                    cur.execute(
                        "SELECT 1 FROM " + portal_db._q(KB_TABLE) +
                        " WHERE client_id = %s AND title = %s LIMIT 1",
                        (principal["client_id"], entry["title"]),
                    )
                    if portal_db.rows(cur):
                        skipped += 1
                        continue
                    cur.execute(
                        "INSERT INTO " + portal_db._q(KB_TABLE) +
                        " (client_id, title, category, keywords, content,"
                        " is_active) VALUES (%s, %s, 'starter', %s, %s, TRUE)",
                        (principal["client_id"], entry["title"],
                         entry["keywords"], entry["content"]),
                    )
                    applied += 1
                portal_db.log_action(
                    cur,
                    principal["client_id"],
                    "preset.applied",
                    "customer_user",
                    principal.get("user_id"),
                    None,
                    preset["label"] + " (added " + str(applied) + ")",
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "portal preset apply")[0]), 503
    return jsonify({"ok": True, "applied": applied, "skipped": skipped}), 200


@bp.get("/portal/activity")
def get_activity():
    principal, error = _principal_or_error()
    if error is not None:
        return error
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, action, actor_kind, note, conversation_id,"
                    " created_at FROM " + portal_db._q(ACTIVITY_TABLE) +
                    " WHERE client_id = %s ORDER BY id DESC LIMIT 50",
                    (principal["client_id"],),
                )
                activity_rows = portal_db.rows(cur)
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(error, "portal activity")[0]), 503
    items = [
        {
            "id": int(row.get("id") or 0),
            "action": str(row.get("action") or ""),
            "actor_kind": str(row.get("actor_kind") or ""),
            "note": str(row.get("note") or ""),
            "conversation_id": row.get("conversation_id"),
            "created_at": row.get("created_at"),
        }
        for row in activity_rows
    ]
    return jsonify({"items": items}), 200
