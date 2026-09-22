"""Saved catalog: reusable product/service entries in one place.

The catalog feeds checkout links (pick a saved item instead of retyping
name/price) and the recommendation engine (portal_reco reads the same
table). Items are per-client, capped, and archive instead of disappearing
so past recommendations and pickers stay stable. API contract matches the
existing web clients exactly: GET returns {items:[...]}; writes take
{item: {kind, name, price_text, notes, is_active}}.
"""

import logging
from typing import Any, Dict, Optional, Tuple

from flask import Blueprint, jsonify, request

from portal_auth import (
    PortalAuthUnavailable,
    authenticate_portal_request,
    ensure_human_principal,
)
import portal_db

bp = Blueprint("portal_catalog", __name__, url_prefix="/api/v1/portal")

logger = logging.getLogger(__name__)

CATALOG_TABLE = "portal_catalog"

MAX_ITEMS = 200
NAME_MAX = 120
PRICE_TEXT_MAX = 40
NOTES_MAX = 160
KINDS = ("product", "service")

_DDL_READY = False


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


def _ensure_catalog_tables(conn) -> None:
    global _DDL_READY
    if _DDL_READY:
        return
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS " + portal_db._q(CATALOG_TABLE) +
            " (id BIGSERIAL PRIMARY KEY,"
            " client_id BIGINT NOT NULL,"
            " name TEXT NOT NULL,"
            " kind TEXT NOT NULL DEFAULT 'product',"
            " price_text TEXT NOT NULL DEFAULT '',"
            " notes TEXT NOT NULL DEFAULT '',"
            " is_active BOOLEAN NOT NULL DEFAULT TRUE,"
            " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            " updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS portal_catalog_client_idx ON "
            + portal_db._q(CATALOG_TABLE) + " (client_id, is_active, id DESC)"
        )
    conn.commit()
    _DDL_READY = True


def validate_catalog_item(payload: Any) -> Tuple[Optional[Dict[str, Any]],
                                                 Optional[str]]:
    """Returns (clean, error)."""
    if not isinstance(payload, dict):
        return None, "item (object) is required."
    name = str(payload.get("name") or "").strip()
    if not name:
        return None, "Item name is required."
    if len(name) > NAME_MAX:
        return None, "Item name must be " + str(NAME_MAX) + " characters or fewer."
    kind = str(payload.get("kind") or "product").strip().lower()
    if kind not in KINDS:
        return None, "kind must be product or service."
    price_text = str(payload.get("price_text") or "").strip()
    if len(price_text) > PRICE_TEXT_MAX:
        return None, ("Price text must be " + str(PRICE_TEXT_MAX)
                      + " characters or fewer.")
    notes = str(payload.get("notes") or "").strip()
    if len(notes) > NOTES_MAX:
        return None, "Notes must be " + str(NOTES_MAX) + " characters or fewer."
    is_active = payload.get("is_active", True)
    if not isinstance(is_active, bool):
        return None, "is_active must be true or false."
    return ({"name": name, "kind": kind, "price_text": price_text,
             "notes": notes, "is_active": is_active}), None


def _public(row: Dict[str, Any]) -> Dict[str, Any]:
    created = row.get("created_at")
    return {
        "id": int(row.get("id") or 0),
        "kind": str(row.get("kind") or "product"),
        "name": str(row.get("name") or ""),
        "price_text": str(row.get("price_text") or ""),
        "notes": str(row.get("notes") or ""),
        "is_active": row.get("is_active") is True,
        "created_at": created.isoformat() if hasattr(created, "isoformat")
        else None,
    }


def _load_row(cur, client_id: int, item_id: int) -> Optional[Dict[str, Any]]:
    cur.execute(
        "SELECT id, kind, name, price_text, notes, is_active, created_at"
        " FROM " + portal_db._q(CATALOG_TABLE) +
        " WHERE id = %s AND client_id = %s LIMIT 1",
        (item_id, client_id),
    )
    rows = portal_db.rows(cur)
    return rows[0] if rows else None


@bp.get("/catalog")
def list_catalog():
    principal, error = _principal_or_error()
    if error:
        return error
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            _ensure_catalog_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, kind, name, price_text, notes, is_active,"
                    " created_at FROM " + portal_db._q(CATALOG_TABLE) +
                    " WHERE client_id = %s"
                    " ORDER BY is_active DESC, id DESC LIMIT "
                    + str(MAX_ITEMS),
                    (principal["client_id"],),
                )
                rows = portal_db.rows(cur)
        finally:
            conn.close()
    except Exception as error:
        logger.warning("catalog list failed: %s", error)
        return jsonify(portal_db.portal_unavailable(error, "catalog read")[0]), 503
    return jsonify({"items": [_public(row) for row in rows]}), 200


@bp.post("/catalog")
def create_catalog_item():
    principal, error = _principal_or_error()
    if error:
        return error
    forbidden = ensure_human_principal(principal)
    if forbidden is not None:
        return forbidden
    payload = request.get_json(silent=True) or {}
    raw = payload.get("item") if isinstance(payload.get("item"), dict) else payload
    clean, err = validate_catalog_item(raw)
    if err:
        return jsonify({"error": {"code": "bad_request", "message": err}}), 400
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            _ensure_catalog_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM " + portal_db._q(CATALOG_TABLE) +
                    " WHERE client_id = %s AND lower(name) = lower(%s)"
                    " LIMIT 1",
                    (principal["client_id"], clean["name"]),
                )
                if portal_db.rows(cur):
                    return jsonify({"error": {"code": "conflict",
                                              "message": "An item with this"
                                                         " name already"
                                                         " exists."}}), 409
                cur.execute(
                    "SELECT COUNT(*) AS n FROM " + portal_db._q(CATALOG_TABLE) +
                    " WHERE client_id = %s AND is_active",
                    (principal["client_id"],),
                )
                counts = portal_db.rows(cur)
                if counts and int(counts[0].get("n") or 0) >= MAX_ITEMS:
                    return jsonify({"error": {"code": "bad_request",
                                              "message": "Up to "
                                                         + str(MAX_ITEMS)
                                                         + " active catalog"
                                                         " items are"
                                                         " supported."}}), 400
                cur.execute(
                    "INSERT INTO " + portal_db._q(CATALOG_TABLE) +
                    " (client_id, name, kind, price_text, notes, is_active)"
                    " VALUES (%s, %s, %s, %s, %s, %s)"
                    " RETURNING id, kind, name, price_text, notes, is_active,"
                    " created_at",
                    (principal["client_id"], clean["name"], clean["kind"],
                     clean["price_text"], clean["notes"], clean["is_active"]),
                )
                created = portal_db.rows(cur)
                portal_db.log_action(
                    cur,
                    principal["client_id"],
                    "catalog.created",
                    "customer_user",
                    principal.get("user_id"),
                    None,
                    ("Catalog item " + clean["name"])[:200],
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        logger.warning("catalog create failed: %s", error)
        return jsonify(portal_db.portal_unavailable(error, "catalog create")[0]), 503
    return jsonify({"ok": True,
                    "item": _public(created[0] if created else {})}), 200


@bp.put("/catalog/<int:item_id>")
def update_catalog_item(item_id: int):
    principal, error = _principal_or_error()
    if error:
        return error
    forbidden = ensure_human_principal(principal)
    if forbidden is not None:
        return forbidden
    payload = request.get_json(silent=True) or {}
    raw = payload.get("item") if isinstance(payload.get("item"), dict) else payload
    clean, err = validate_catalog_item(raw)
    if err:
        return jsonify({"error": {"code": "bad_request", "message": err}}), 400
    updated: list = []
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            _ensure_catalog_tables(conn)
            with conn.cursor() as cur:
                if _load_row(cur, principal["client_id"], item_id) is None:
                    return jsonify({"error": {"code": "not_found",
                                              "message": "Catalog item not"
                                                         " found."}}), 404
                cur.execute(
                    "SELECT id FROM " + portal_db._q(CATALOG_TABLE) +
                    " WHERE client_id = %s AND lower(name) = lower(%s)"
                    " AND id <> %s LIMIT 1",
                    (principal["client_id"], clean["name"], item_id),
                )
                if portal_db.rows(cur):
                    return jsonify({"error": {"code": "conflict",
                                              "message": "An item with this"
                                                         " name already"
                                                         " exists."}}), 409
                cur.execute(
                    "UPDATE " + portal_db._q(CATALOG_TABLE) +
                    " SET name = %s, kind = %s, price_text = %s, notes = %s,"
                    " is_active = %s, updated_at = NOW()"
                    " WHERE id = %s AND client_id = %s"
                    " RETURNING id, kind, name, price_text, notes, is_active,"
                    " created_at",
                    (clean["name"], clean["kind"], clean["price_text"],
                     clean["notes"], clean["is_active"], item_id,
                     principal["client_id"]),
                )
                updated = portal_db.rows(cur)
                portal_db.log_action(
                    cur,
                    principal["client_id"],
                    "catalog.updated",
                    "customer_user",
                    principal.get("user_id"),
                    None,
                    ("Catalog item " + clean["name"])[:200],
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        logger.warning("catalog update failed: %s", error)
        return jsonify(portal_db.portal_unavailable(error, "catalog update")[0]), 503
    if not updated:
        return jsonify({"error": {"code": "not_found",
                                  "message": "Catalog item not found."}}), 404
    return jsonify({"ok": True, "item": _public(updated[0])}), 200


@bp.delete("/catalog/<int:item_id>")
def delete_catalog_item(item_id: int):
    principal, error = _principal_or_error()
    if error:
        return error
    forbidden = ensure_human_principal(principal)
    if forbidden is not None:
        return forbidden
    removed: list = []
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            _ensure_catalog_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM " + portal_db._q(CATALOG_TABLE) +
                    " WHERE id = %s AND client_id = %s RETURNING id",
                    (item_id, principal["client_id"]),
                )
                removed = portal_db.rows(cur)
                if removed:
                    portal_db.log_action(
                        cur,
                        principal["client_id"],
                        "catalog.removed",
                        "customer_user",
                        principal.get("user_id"),
                        None,
                        ("Catalog item removed #" + str(item_id))[:200],
                    )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        logger.warning("catalog delete failed: %s", error)
        return jsonify(portal_db.portal_unavailable(error, "catalog delete")[0]), 503
    if not removed:
        return jsonify({"error": {"code": "not_found",
                                  "message": "Catalog item not found."}}), 404
    return jsonify({"ok": True}), 200
