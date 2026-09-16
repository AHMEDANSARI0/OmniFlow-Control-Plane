"""Smart checkout v0: shareable order-summary links the merchant can send to
a customer. Tokenized public view (no auth), server-computed totals,
merchant-marked statuses. No payments provider — the link closes the loop
over chat."""

import json
import logging
import secrets
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, request

from portal_auth import (
    PortalAuthUnavailable,
    authenticate_portal_request,
    ensure_human_principal,
)
import portal_db

bp = Blueprint("portal_checkout", __name__, url_prefix="/api/v1/portal")
public_bp = Blueprint("portal_checkout_public", __name__,
                      url_prefix="/api/v1/public")


@public_bp.get("/checkout/<token>")
def public_checkout_route(token: str):
    payload, status = public_checkout(token)
    return jsonify(payload), status

logger = logging.getLogger(__name__)

LINKS_TABLE = "portal_checkout_links"

STATUSES = ("open", "paid", "cancelled")
MAX_ITEMS = 20
MAX_QTY = 99
MAX_TITLE = 120

_CHECKOUT_DDL_READY = False


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


def _ensure_checkout_tables(conn) -> None:
    global _CHECKOUT_DDL_READY
    if _CHECKOUT_DDL_READY:
        return
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS " + portal_db._q(LINKS_TABLE) +
            " (id BIGSERIAL PRIMARY KEY,"
            " client_id BIGINT NOT NULL,"
            " contact_id TEXT NOT NULL,"
            " token TEXT NOT NULL UNIQUE,"
            " title TEXT NOT NULL DEFAULT '',"
            " items JSONB NOT NULL DEFAULT '[]'::jsonb,"
            " total NUMERIC(12,2) NOT NULL DEFAULT 0,"
            " status TEXT NOT NULL DEFAULT 'open',"
            " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            " updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS portal_checkout_links_client_idx ON "
            + portal_db._q(LINKS_TABLE) + " (client_id, id DESC)"
        )
    conn.commit()
    _CHECKOUT_DDL_READY = True


def _normalize_items(raw: Any) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Returns (items, error)."""
    if not isinstance(raw, list) or not raw:
        return None, "items (non-empty list) is required."
    if len(raw) > MAX_ITEMS:
        return None, "Max " + str(MAX_ITEMS) + " items per link."
    items: List[Dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            return None, "Each item must be an object."
        name = str(entry.get("name") or "").strip()[:100]
        qty = entry.get("qty", 1)
        price = entry.get("price")
        if not name:
            return None, "Every item needs a name."
        if isinstance(qty, bool) or not isinstance(qty, int) or not (
                1 <= qty <= MAX_QTY):
            return None, "qty must be 1-" + str(MAX_QTY) + "."
        if isinstance(price, bool) or not isinstance(price, (int, float)) or price < 0:
            return None, "price must be 0 or more."
        items.append({"name": name, "qty": qty, "price": round(float(price), 2)})
    return items, None


def _public(row: Dict[str, Any]) -> Dict[str, Any]:
    items = row.get("items")
    if not isinstance(items, list):
        items = []
    return {
        "id": int(row.get("id") or 0),
        "token": str(row.get("token") or ""),
        "contact_id": row.get("contact_id"),
        "title": str(row.get("title") or ""),
        "items": [
            {
                "name": str(item.get("name") or ""),
                "qty": int(item.get("qty") or 1),
                "price": float(item.get("price") or 0),
            }
            for item in items if isinstance(item, dict)
        ],
        "total": float(row.get("total") or 0),
        "status": row.get("status") or "open",
        "created_at": _iso(row.get("created_at")),
    }


@bp.post("/checkout/links")
def create_checkout_link():
    principal, error = _principal_or_error()
    if error:
        return error
    forbidden = ensure_human_principal(principal)
    if forbidden is not None:
        return forbidden
    payload = request.get_json(silent=True) or {}
    contact = str(payload.get("contact_id") or "").strip()[:100]
    title = str(payload.get("title") or "").strip()[:MAX_TITLE]
    items, err = _normalize_items(payload.get("items"))
    if err:
        return jsonify({"error": {"code": "bad_request",
                                  "message": err}}), 400
    if not contact:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "contact_id is required."}}), 400
    total = round(sum(item["qty"] * item["price"] for item in items), 2)
    token = secrets.token_urlsafe(16)
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            _ensure_checkout_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO " + portal_db._q(LINKS_TABLE) +
                    " (client_id, contact_id, token, title, items, total)"
                    " VALUES (%s, %s, %s, %s, CAST(%s AS JSONB), %s)"
                    " RETURNING id, token, contact_id, title, items, total,"
                    " status, created_at",
                    (principal["client_id"], contact, token, title,
                     json.dumps(items), total),
                )
                created = portal_db.rows(cur)
                portal_db.log_action(
                    cur,
                    principal["client_id"],
                    "checkout.created",
                    "customer_user",
                    principal.get("user_id"),
                    None,
                    ("Checkout link " + token[:8] + " (" + str(total) + ")")[:200],
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        logger.warning("checkout create failed: %s", error)
        return jsonify(portal_db.portal_unavailable(error, "checkout create")[0]), 503
    return jsonify({"ok": True, "link": _public(created[0] if created else {})}), 200


@bp.get("/checkout/links")
def list_checkout_links():
    principal, error = _principal_or_error()
    if error:
        return error
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            _ensure_checkout_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, token, contact_id, title, items, total,"
                    " status, created_at FROM " + portal_db._q(LINKS_TABLE) +
                    " WHERE client_id = %s ORDER BY id DESC LIMIT 30",
                    (principal["client_id"],),
                )
                rows = portal_db.rows(cur)
        finally:
            conn.close()
    except Exception as error:
        logger.warning("checkout list failed: %s", error)
        return jsonify(portal_db.portal_unavailable(error, "checkout list")[0]), 503
    return jsonify({"links": [_public(row) for row in rows]}), 200


@bp.patch("/checkout/links/<int:link_id>")
def update_checkout_link(link_id: int):
    principal, error = _principal_or_error()
    if error:
        return error
    forbidden = ensure_human_principal(principal)
    if forbidden is not None:
        return forbidden
    payload = request.get_json(silent=True) or {}
    status = payload.get("status")
    if status not in STATUSES:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Pick open, paid or"
                                             " cancelled."}}), 400
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            _ensure_checkout_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE " + portal_db._q(LINKS_TABLE) +
                    " SET status = %s, updated_at = NOW()"
                    " WHERE id = %s AND client_id = %s RETURNING token",
                    (status, link_id, principal["client_id"]),
                )
                updated = portal_db.rows(cur)
                if not updated:
                    return jsonify({"error": {"code": "not_found",
                                              "message": "Link not"
                                                         " found."}}), 404
                portal_db.log_action(
                    cur,
                    principal["client_id"],
                    "checkout." + str(status),
                    "customer_user",
                    principal.get("user_id"),
                    None,
                    ("Checkout " + str(updated[0].get("token"))[:8]
                     + " -> " + str(status))[:200],
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        logger.warning("checkout update failed: %s", error)
        return jsonify(portal_db.portal_unavailable(error, "checkout update")[0]), 503
    return jsonify({"ok": True, "status": status}), 200


def public_checkout(token: str) -> Tuple[Optional[Dict[str, Any]], Optional[int]]:
    """Public (unauthenticated) token lookup. Returns (payload, status)."""
    token = str(token or "").strip()[:64]
    if not token:
        return {"error": {"code": "bad_request",
                          "message": "token is required."}}, 400
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            _ensure_checkout_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, token, contact_id, title, items, total,"
                    " status, created_at FROM " + portal_db._q(LINKS_TABLE) +
                    " WHERE token = %s LIMIT 1",
                    (token,),
                )
                rows = portal_db.rows(cur)
        finally:
            conn.close()
    except Exception as error:
        logger.warning("checkout public read failed: %s", error)
        return {"error": {"code": "portal_unavailable",
                          "message": "Try again shortly."}}, 503
    if not rows:
        return {"error": {"code": "not_found",
                          "message": "This order link is not valid."}}, 404
    view = _public(rows[0])
    view.pop("contact_id", None)
    view.pop("id", None)
    return view, 200
