"""Brands (V2 B18) - multi-brand prep.

A workspace can define the businesses/brands it sells under. Everything
else (KB entries, catalog items, checkout links) gains an optional
brand tag; untagged rows behave exactly as before, so this is pure
prep - nothing changes until the owner actually creates brands.

Brands are owner-only, plan-capped (the "brands" limit key) and fully
audited. Deleting a brand leaves its data: tagged rows simply fall
back to untagged (brand_id NULL).
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
import portal_plans

bp = Blueprint("portal_brands", __name__, url_prefix="/api/v1/portal")
logger = logging.getLogger(__name__)

TABLE = "portal_brands"

NAME_MAX = 60
MAX_BRANDS = 50

_DDL_READY = False

_DDL = """
CREATE TABLE IF NOT EXISTS portal_brands (
  id BIGSERIAL PRIMARY KEY,
  client_id BIGINT NOT NULL,
  name TEXT NOT NULL,
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS portal_brands_client_idx
  ON portal_brands (client_id, is_active, id DESC);
"""


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
    if principal.get("via_api_key"):
        return None, (jsonify({"error": {"code": "forbidden",
                                         "message": "Owner sign-in"
                                                    " required."}}),
                      403)
    return principal, None


def _ensure_ddl(cur) -> None:
    global _DDL_READY
    if _DDL_READY:
        return
    cur.execute(_DDL)
    _DDL_READY = True


def _public(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": int(row.get("id") or 0),
        "name": str(row.get("name") or ""),
        "is_active": row.get("is_active") is True,
        "created_at": str(row.get("created_at") or "") or None,
    }


def _clean_name(raw: Any) -> str:
    return str(raw or "").strip()[:NAME_MAX]


def resolve_brand(cur, client_id: int, raw: Any) -> Optional[int]:
    """Map a payload brand_id onto this client's brands; junk -> None.

    Never raises and never 4xxs - an unknown or foreign id simply reads
    as untagged so a stale form can never block a save."""
    if raw is None or raw == "":
        return None
    try:
        brand_id = int(raw)
    except (TypeError, ValueError):
        return None
    if brand_id <= 0:
        return None
    try:
        cur.execute(
            "SELECT 1 FROM " + portal_db._q(TABLE) +
            " WHERE id = %s AND client_id = %s LIMIT 1",
            (brand_id, client_id),
        )
    except Exception:
        return None
    return brand_id if portal_db.rows(cur) else None


def _dup_exists(cur, client_id: int, name: str, exclude_id=None) -> bool:
    sql = ("SELECT 1 FROM " + portal_db._q(TABLE) +
           " WHERE client_id = %s AND lower(name) = lower(%s)")
    args = [client_id, name]
    if exclude_id is not None:
        sql += " AND id <> %s"
        args.append(exclude_id)
    sql += " LIMIT 1"
    cur.execute(sql, tuple(args))
    return bool(portal_db.rows(cur))


@bp.get("/brands")
def list_brands():
    principal, error = _principal_or_error()
    if error:
        return error
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                _ensure_ddl(cur)
                cur.execute(
                    "SELECT id, name, is_active, created_at FROM "
                    + portal_db._q(TABLE) +
                    " WHERE client_id = %s"
                    " ORDER BY is_active DESC, id DESC LIMIT "
                    + str(MAX_BRANDS),
                    (principal["client_id"],),
                )
                rows = portal_db.rows(cur)
        finally:
            conn.close()
    except Exception as error:
        logger.warning("brands list failed: %s", error)
        return jsonify(portal_db.portal_unavailable(error, "brands read")[0]), 503
    return jsonify({"brands": [_public(row) for row in rows]}), 200


@bp.post("/brands")
def create_brand():
    principal, error = _principal_or_error()
    if error:
        return error
    forbidden = ensure_human_principal(principal)
    if forbidden is not None:
        return forbidden
    payload = request.get_json(silent=True) or {}
    name = _clean_name(payload.get("name"))
    if not name:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Brand name is required."}}), 400
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                _ensure_ddl(cur)
                blocked = portal_plans.enforce(cur, principal["client_id"],
                                               "brands", "Brand")
                if blocked is not None:
                    return blocked
                if _dup_exists(cur, principal["client_id"], name):
                    return jsonify({"error": {"code": "conflict",
                                              "message": "A brand with this"
                                                         " name already"
                                                         " exists."}}), 409
                cur.execute(
                    "INSERT INTO " + portal_db._q(TABLE) +
                    " (client_id, name) VALUES (%s, %s)"
                    " RETURNING id, name, is_active, created_at",
                    (principal["client_id"], name),
                )
                created = portal_db.rows(cur)
                portal_db.log_action(
                    cur, principal["client_id"], "brands.created",
                    "human", principal.get("user_id"), None,
                    "Brand " + name,
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        logger.warning("brand create failed: %s", error)
        return jsonify(portal_db.portal_unavailable(error, "brand create")[0]), 503
    return jsonify({"ok": True,
                    "brand": _public(created[0] if created else {})}), 200


@bp.patch("/brands/<int:brand_id>")
def update_brand(brand_id: int):
    principal, error = _principal_or_error()
    if error:
        return error
    forbidden = ensure_human_principal(principal)
    if forbidden is not None:
        return forbidden
    payload = request.get_json(silent=True) or {}
    raw_name = payload.get("name")
    name = _clean_name(raw_name) if raw_name is not None else None
    if name == "":
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Brand name is required."}}), 400
    is_active = payload.get("is_active")
    if is_active is not None and not isinstance(is_active, bool):
        return jsonify({"error": {"code": "bad_request",
                                  "message": "is_active must be true or"
                                             " false."}}), 400
    if name is None and is_active is None:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Nothing to update."}}), 400
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                _ensure_ddl(cur)
                sets = ["updated_at = NOW()"]
                args: list = []
                if name is not None:
                    if _dup_exists(cur, principal["client_id"], name,
                                   exclude_id=brand_id):
                        return jsonify({"error": {"code": "conflict",
                                                  "message": "A brand with"
                                                             " this name"
                                                             " already"
                                                             " exists."}}), 409
                    sets.append("name = %s")
                    args.append(name)
                if is_active is not None:
                    sets.append("is_active = %s")
                    args.append(is_active)
                args.extend([brand_id, principal["client_id"]])
                cur.execute(
                    "UPDATE " + portal_db._q(TABLE) + " SET " +
                    ", ".join(sets) +
                    " WHERE id = %s AND client_id = %s"
                    " RETURNING id, name, is_active, created_at",
                    tuple(args),
                )
                updated = portal_db.rows(cur)
                if not updated:
                    return jsonify({"error": {"code": "not_found",
                                              "message": "Brand not"
                                                         " found."}}), 404
                portal_db.log_action(
                    cur, principal["client_id"], "brands.updated",
                    "human", principal.get("user_id"), None,
                    ("Brand " + str(updated[0].get("name") or ""))[:200],
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        logger.warning("brand update failed: %s", error)
        return jsonify(portal_db.portal_unavailable(error, "brand update")[0]), 503
    return jsonify({"ok": True,
                    "brand": _public(updated[0] if updated else {})}), 200


@bp.delete("/brands/<int:brand_id>")
def delete_brand(brand_id: int):
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
            with conn.cursor() as cur:
                _ensure_ddl(cur)
                cur.execute(
                    "DELETE FROM " + portal_db._q(TABLE) +
                    " WHERE id = %s AND client_id = %s RETURNING name",
                    (brand_id, principal["client_id"]),
                )
                deleted = portal_db.rows(cur)
                if not deleted:
                    return jsonify({"error": {"code": "not_found",
                                              "message": "Brand not"
                                                         " found."}}), 404
                # Prep keeps data: tagged rows fall back to untagged.
                for table in ("portal_kb_entries", "portal_catalog",
                              "portal_checkout_links"):
                    try:
                        cur.execute(
                            "UPDATE " + portal_db._q(table) +
                            " SET brand_id = NULL"
                            " WHERE client_id = %s AND brand_id = %s",
                            (principal["client_id"], brand_id),
                        )
                    except Exception:
                        pass
                portal_db.log_action(
                    cur, principal["client_id"], "brands.deleted",
                    "human", principal.get("user_id"), None,
                    ("Brand " + str(deleted[0].get("name") or "")
                     + " removed")[:200],
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        logger.warning("brand delete failed: %s", error)
        return jsonify(portal_db.portal_unavailable(error, "brand delete")[0]), 503
    return jsonify({"ok": True}), 200
