"""Courier connector (V2 B10) - booking -> tracking -> RTO signal.

Provider-swappable connector over the owner's own courier account.
Credentials are pasted by the owner in the dashboard (Courier page),
stored per tenant in portal_courier_settings and NEVER returned in
full (masked, last 4 only) nor written to the audit log. While no
credentials are saved every endpoint answers 409 "not_configured" -
the moment the owner saves working credentials and enables the
connector, the same endpoints go live. Nothing is hardcoded per
shop: provider endpoints live in the PROVIDERS registry below
(base URL overridable in settings), and new couriers are one
adapter entry away.

Providers:
* leopards  - Leopards Courier merchant API (bookParcel /
  trackShipperApi / getAllCities). api_key + api_password.
* tcs       - TCS eComm API frame (Bearer key + optional secret).
  TCS grants real endpoint paths per subscription, so the paths are
  module constants (TCS_BOOK_PATH / TCS_TRACK_PATH) and the base URL
  is owner-editable - adjust once after onboarding, no code churn.

Parcel status is normalized to a fixed vocabulary
(booked / in_transit / delivered / returned / undelivered / cancelled)
so the bookings list and its delivered-vs-returned summary work the
same for every provider. Booking validates the address with the B9
address intelligence (normalize_address) and reports what is missing
plus the ready Roman-Urdu questions to ask.

All endpoints are human-only (API keys get 403). Booking, tracking
and settings changes are audited without secrets.
"""

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, request

import portal_db
from portal_auth import (
    PortalAuthUnavailable,
    authenticate_portal_request,
)
from portal_risk import normalize_address

logger = logging.getLogger("omniflow.portal-courier")

bp = Blueprint("portal_courier", __name__, url_prefix="/api/v1/portal")

SETTINGS_TABLE = "portal_courier_settings"
BOOKINGS_TABLE = "portal_courier_bookings"

HTTP_TIMEOUT = 25

# ---------------------------------------------------------------------------
# Provider registry (add a courier = add an entry here + two functions)
# ---------------------------------------------------------------------------

LEOPARDS_DEFAULT_BASE = "https://merchant.leopardscourier.com/api"
TCS_DEFAULT_BASE = "https://apis.tcs.com.pk"
TCS_BOOK_PATH = "/ecom/api/order-booking"
TCS_TRACK_PATH = "/ecom/api/track/"

PROVIDERS: Dict[str, Dict[str, Any]] = {
    "leopards": {
        "label": "Leopards Courier",
        "default_base": LEOPARDS_DEFAULT_BASE,
        "secret_label": "API password",
    },
    "tcs": {
        "label": "TCS",
        "default_base": TCS_DEFAULT_BASE,
        "secret_label": "API secret (optional)",
    },
}


def _http_json(url: str, payload: Optional[Dict[str, Any]],
               headers: Optional[Dict[str, str]] = None,
               method: Optional[str] = None
               ) -> Tuple[int, Dict[str, Any]]:
    """One JSON round-trip. Returns (http_status, body-dict-or-{})."""
    data = json.dumps(payload).encode("utf-8") if payload is not None \
        else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", "replace")
            code = int(getattr(resp, "status", 0) or 0)
    except urllib.error.HTTPError as error:
        try:
            body = error.read().decode("utf-8", "replace")
        except OSError:
            body = ""
        code = int(error.code or 0)
    except (urllib.error.URLError, OSError) as error:
        return 0, {"_error": str(error)}
    try:
        parsed = json.loads(body) if body else {}
    except ValueError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {"_value": parsed}
    return code, parsed


def leopards_headers(cfg: Dict[str, str]) -> Dict[str, str]:
    return {}


def leopards_book(cfg: Dict[str, str], data: Dict[str, Any]) \
        -> Dict[str, Any]:
    url = cfg["base_url"].rstrip("/") + "/bookParcel/"
    payload = {
        "api_key": cfg["api_key"],
        "api_password": cfg["api_password"],
        "shipment_type": str(data.get("service") or "Overnight"),
        "booked_packet_weight": int(data.get("weight") or 1000),
        "pieces": int(data.get("pieces") or 1),
        "cod_amount": str(int(round(float(data.get("cod_amount") or 0)))),
        "shipment_description": str(data.get("description") or "")[:200],
        "shipment_name": str(data.get("customer_name")
                             or "Customer")[:100],
        "shipment_email": "",
        "shipment_phone": str(data.get("phone") or ""),
        "shipment_address": str(data.get("address") or "")[:250],
        "shipment_city": str(data.get("city") or "")[:60],
    }
    code, resp = _http_json(url, payload)
    ok = str(resp.get("status")) in ("1", "true", "True", "SUCCESS")
    tracking = str(resp.get("track_number")
                   or resp.get("tracking_number") or "")
    if code == 0:
        return {"ok": False, "message": "Courier API not reachable."}
    if not ok or not tracking:
        return {"ok": False,
                "message": str(resp.get("message")
                               or ("Booking refused (HTTP "
                                   + str(code) + ")."))}
    return {"ok": True, "tracking_number": tracking,
            "message": str(resp.get("message") or "Booked.")}


def leopards_track(cfg: Dict[str, str], tracking_number: str) \
        -> Dict[str, Any]:
    url = cfg["base_url"].rstrip("/") + "/trackShipperApi/"
    payload = {"api_key": cfg["api_key"],
               "api_password": cfg["api_password"],
               "track_numbers": tracking_number}
    code, resp = _http_json(url, payload)
    packets = resp.get("packet_list")
    if code == 0:
        return {"ok": False, "message": "Courier API not reachable."}
    if not isinstance(packets, list) or not packets:
        return {"ok": False,
                "message": str(resp.get("message")
                               or ("No tracking data (HTTP "
                                   + str(code) + ")."))}
    packet = packets[0] if isinstance(packets[0], dict) else {}
    events = []
    for act in (packet.get("activity") or []):
        if not isinstance(act, dict):
            continue
        events.append({
            "when": " ".join(str(act.get(k) or "")
                             for k in ("date", "time")).strip(),
            "status": str(act.get("status") or ""),
            "detail": str(act.get("detail")
                          or act.get("activity") or ""),
        })
    status = str(packet.get("status") or packet.get("booking_status")
                 or "")
    return {"ok": True, "status": status, "events": events}


def leopards_test(cfg: Dict[str, str]) -> Dict[str, Any]:
    url = cfg["base_url"].rstrip("/") + "/getAllCities/"
    code, resp = _http_json(url, {"api_key": cfg["api_key"],
                                  "api_password": cfg["api_password"]})
    if code == 0:
        return {"ok": False,
                "message": "Courier API not reachable."}
    ok = str(resp.get("status")) in ("1", "true", "True") or \
        isinstance(resp.get("city_list"), list)
    if ok:
        return {"ok": True, "message": "Credentials accepted."}
    return {"ok": False,
            "message": str(resp.get("message")
                           or ("Credentials refused (HTTP "
                               + str(code) + ")."))}


def tcs_headers(cfg: Dict[str, str]) -> Dict[str, str]:
    headers = {"X-Api-Key": cfg["api_key"]}
    if cfg.get("api_password"):
        headers["Authorization"] = "Bearer " + cfg["api_password"]
    return headers


def tcs_book(cfg: Dict[str, str], data: Dict[str, Any]) -> Dict[str, Any]:
    url = cfg["base_url"].rstrip("/") + TCS_BOOK_PATH
    payload = {
        "reference": str(data.get("description") or "")[:100],
        "consignee": {
            "name": str(data.get("customer_name")
                        or "Customer")[:100],
            "phone": str(data.get("phone") or ""),
            "address": str(data.get("address") or "")[:250],
            "city": str(data.get("city") or "")[:60],
        },
        "pieces": int(data.get("pieces") or 1),
        "weight_grams": int(data.get("weight") or 1000),
        "cod_amount": round(float(data.get("cod_amount") or 0), 2),
        "service": str(data.get("service") or "OVERNIGHT"),
    }
    code, resp = _http_json(url, payload, headers=tcs_headers(cfg))
    if code == 0:
        return {"ok": False, "message": "Courier API not reachable."}
    tracking = str(resp.get("cn") or resp.get("trackingNumber")
                   or resp.get("tracking_number") or "")
    if 200 <= code < 300 and tracking:
        return {"ok": True, "tracking_number": tracking,
                "message": "Booked."}
    return {"ok": False,
            "message": str(resp.get("message")
                           or resp.get("error")
                           or ("Booking refused (HTTP "
                               + str(code) + ")."))}


def tcs_track(cfg: Dict[str, str], tracking_number: str) \
        -> Dict[str, Any]:
    url = cfg["base_url"].rstrip("/") + TCS_TRACK_PATH \
        + tracking_number
    code, resp = _http_json(url, None, headers=tcs_headers(cfg),
                            method="GET")
    if code == 0:
        return {"ok": False, "message": "Courier API not reachable."}
    if not (200 <= code < 300):
        return {"ok": False,
                "message": "No tracking data (HTTP " + str(code)
                           + ")."}
    raw_events = resp.get("events") or resp.get("history") or []
    events = []
    if isinstance(raw_events, list):
        for act in raw_events:
            if not isinstance(act, dict):
                continue
            events.append({
                "when": str(act.get("datetime") or act.get("date")
                            or ""),
                "status": str(act.get("status") or ""),
                "detail": str(act.get("description")
                              or act.get("detail") or ""),
            })
    status = str(resp.get("status") or resp.get("shipmentStatus") or "")
    return {"ok": True, "status": status, "events": events}


def tcs_test(cfg: Dict[str, str]) -> Dict[str, Any]:
    url = cfg["base_url"].rstrip("/") + "/"
    code, _ = _http_json(url, None, headers=tcs_headers(cfg),
                         method="GET")
    if code == 0:
        return {"ok": False, "message": "Courier API not reachable."}
    if code < 500:
        return {"ok": True,
                "message": "Reachable - book a test parcel to confirm"
                           " the credentials end to end."}
    return {"ok": False, "message": "Courier API error (HTTP "
                                    + str(code) + ")."}


PROVIDERS["leopards"]["book"] = leopards_book
PROVIDERS["leopards"]["track"] = leopards_track
PROVIDERS["leopards"]["test"] = leopards_test
PROVIDERS["tcs"]["book"] = tcs_book
PROVIDERS["tcs"]["track"] = tcs_track
PROVIDERS["tcs"]["test"] = tcs_test


def normalize_status(raw: str) -> str:
    """Map any provider status string to the fixed vocabulary."""
    text = (raw or "").strip().lower()
    if not text:
        return "in_transit"
    if "return" in text or "rto" in text:
        return "returned"
    if "undeliver" in text:
        return "undelivered"
    if "deliver" in text:
        return "delivered"
    if "cancel" in text:
        return "cancelled"
    if "book" in text or "create" in text:
        return "booked"
    return "in_transit"


# ---------------------------------------------------------------------------
# Settings + helpers
# ---------------------------------------------------------------------------

_DDL_READY = False

_DDL = """
CREATE TABLE IF NOT EXISTS portal_courier_settings (
  client_id BIGINT PRIMARY KEY,
  provider TEXT NOT NULL DEFAULT 'leopards',
  base_url TEXT NOT NULL DEFAULT '',
  api_key TEXT NOT NULL DEFAULT '',
  api_password TEXT NOT NULL DEFAULT '',
  enabled BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS portal_courier_bookings (
  id BIGSERIAL PRIMARY KEY,
  client_id BIGINT NOT NULL,
  contact_id TEXT NOT NULL DEFAULT '',
  tracking_number TEXT NOT NULL DEFAULT '',
  provider TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'booked',
  cod_amount NUMERIC(12,2) NOT NULL DEFAULT 0,
  city TEXT NOT NULL DEFAULT '',
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  track_json JSONB NOT NULL DEFAULT '[]'::jsonb,
  tracked_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_portal_courier_bookings
  ON portal_courier_bookings (client_id, id DESC);
"""


def _ensure_ddl(cur) -> None:
    global _DDL_READY
    if _DDL_READY:
        return
    cur.execute(_DDL)
    _DDL_READY = True


def _principal_or_error():
    try:
        principal = authenticate_portal_request()
    except PortalAuthUnavailable as error:
        return None, (jsonify({"error": {"code": "portal_unavailable",
                                         "message": str(error)}}), 503)
    if principal is None:
        return None, (jsonify({"error": {"code": "unauthorized",
                                         "message": "Sign in required."}}),
                      401)
    if principal.get("via_api_key"):
        return None, (jsonify({"error": {"code": "forbidden",
                                         "message": "Owner sign-in required."}}),
                      403)
    return principal, None


def mask_secret(value: str) -> str:
    value = value or ""
    if not value:
        return ""
    return "\u2022\u2022\u2022\u2022" + value[-4:] if len(value) > 4 \
        else "\u2022\u2022\u2022\u2022"


def _load_settings(cur, client_id: int) -> Dict[str, Any]:
    cur.execute(
        "SELECT provider, base_url, api_key, api_password, enabled"
        " FROM " + portal_db._q(SETTINGS_TABLE) +
        " WHERE client_id = %s LIMIT 1",
        (client_id,),
    )
    rows = portal_db.rows(cur)
    if not rows:
        return {"provider": "leopards", "base_url": "",
                "api_key": "", "api_password": "", "enabled": False}
    row = rows[0]
    return {"provider": str(row.get("provider") or "leopards"),
            "base_url": str(row.get("base_url") or ""),
            "api_key": str(row.get("api_key") or ""),
            "api_password": str(row.get("api_password") or ""),
            "enabled": bool(row.get("enabled"))}


def _effective_cfg(settings: Dict[str, Any]) -> Dict[str, str]:
    """Settings + provider defaults merged into an adapter config."""
    provider = settings["provider"]
    entry = PROVIDERS.get(provider) or PROVIDERS["leopards"]
    base = settings["base_url"].strip() or entry["default_base"]
    return {"provider": provider, "base_url": base,
            "api_key": settings["api_key"],
            "api_password": settings["api_password"]}


def _configured(settings: Dict[str, Any]) -> bool:
    if not settings["api_key"]:
        return False
    if settings["provider"] == "leopards" \
            and not settings["api_password"]:
        return False
    return True


# ---------------------------------------------------------------------------
# Owner API
# ---------------------------------------------------------------------------

@bp.get("/courier/settings")
def get_courier_settings():
    principal, auth_error = _principal_or_error()
    if auth_error:
        return auth_error
    conn = portal_db._conn()
    try:
        with conn.cursor() as cur:
            _ensure_ddl(cur)
            settings = _load_settings(cur, int(principal["client_id"]))
        conn.commit()
    finally:
        conn.close()
    entry = PROVIDERS.get(settings["provider"]) or PROVIDERS["leopards"]
    return jsonify({
        "configured": _configured(settings),
        "enabled": settings["enabled"],
        "provider": settings["provider"],
        "base_url": settings["base_url"],
        "api_key_masked": mask_secret(settings["api_key"]),
        "api_password_masked": mask_secret(settings["api_password"]),
        "secret_label": entry["secret_label"],
        "providers": [{"id": pid, "label": p["label"],
                       "default_base": p["default_base"]}
                      for pid, p in PROVIDERS.items()],
    }), 200


@bp.put("/courier/settings")
def put_courier_settings():
    principal, auth_error = _principal_or_error()
    if auth_error:
        return auth_error
    client_id = int(principal["client_id"])
    payload = request.get_json(silent=True) or {}
    provider = str(payload.get("provider") or "").strip()
    if provider not in PROVIDERS:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "provider must be one of: "
                                             + ", ".join(sorted(PROVIDERS))
                                             + "."}}), 400
    conn = portal_db._conn()
    try:
        with conn.cursor() as cur:
            _ensure_ddl(cur)
            current = _load_settings(cur, client_id)
            api_key = str(payload.get("api_key") or "").strip()
            api_password = str(payload.get("api_password") or "").strip()
            next_settings = {
                "provider": provider,
                "base_url": str(payload.get("base_url")
                                or "").strip()[:200],
                "api_key": api_key or current["api_key"],
                "api_password": api_password
                or current["api_password"],
                "enabled": bool(payload.get("enabled", True)),
            }
            cur.execute(
                "INSERT INTO " + portal_db._q(SETTINGS_TABLE) +
                " (client_id, provider, base_url, api_key,"
                " api_password, enabled, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, NOW())"
                " ON CONFLICT (client_id) DO UPDATE SET"
                " provider = EXCLUDED.provider,"
                " base_url = EXCLUDED.base_url,"
                " api_key = EXCLUDED.api_key,"
                " api_password = EXCLUDED.api_password,"
                " enabled = EXCLUDED.enabled,"
                " updated_at = NOW()",
                (client_id, next_settings["provider"],
                 next_settings["base_url"], next_settings["api_key"],
                 next_settings["api_password"],
                 next_settings["enabled"]),
            )
            portal_db.log_action(
                cur, client_id, "courier.settings", "human", None, None,
                ("provider=" + provider
                 + " creds=" + ("updated" if (api_key or api_password)
                                else "kept")
                 + " enabled=" + ("on" if next_settings["enabled"]
                                  else "off")),
            )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True,
                    "configured": _configured(next_settings)}), 200


@bp.post("/courier/test")
def test_courier():
    principal, auth_error = _principal_or_error()
    if auth_error:
        return auth_error
    conn = portal_db._conn()
    try:
        with conn.cursor() as cur:
            _ensure_ddl(cur)
            settings = _load_settings(cur, int(principal["client_id"]))
        conn.commit()
    finally:
        conn.close()
    if not _configured(settings):
        return jsonify({"error": {"code": "not_configured",
                                  "message": "Save the API credentials"
                                             " first."}}), 409
    entry = PROVIDERS.get(settings["provider"]) or PROVIDERS["leopards"]
    result = entry["test"](_effective_cfg(settings))
    return jsonify(result), (200 if result.get("ok") else 502)


@bp.post("/courier/book")
def book_courier_parcel():
    principal, auth_error = _principal_or_error()
    if auth_error:
        return auth_error
    client_id = int(principal["client_id"])
    payload = request.get_json(silent=True) or {}
    data = {
        "contact_id": str(payload.get("contact_id") or "").strip()[:100],
        "customer_name": str(payload.get("customer_name")
                             or "").strip()[:100],
        "phone": str(payload.get("phone") or "").strip()[:40],
        "city": str(payload.get("city") or "").strip()[:60],
        "address": str(payload.get("address") or "").strip()[:250],
        "cod_amount": payload.get("cod_amount") or 0,
        "pieces": payload.get("pieces") or 1,
        "weight": payload.get("weight") or 1000,
        "description": str(payload.get("description")
                           or "").strip()[:200],
    }
    if not data["address"] or not data["city"]:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "city and address are"
                                             " required."}}), 400
    try:
        data["cod_amount"] = round(float(data["cod_amount"]), 2)
        data["pieces"] = max(1, int(data["pieces"]))
        data["weight"] = max(1, int(data["weight"]))
    except (TypeError, ValueError):
        return jsonify({"error": {"code": "bad_request",
                                  "message": "cod_amount, pieces and"
                                             " weight must be"
                                             " numbers."}}), 400
    intel = normalize_address(data["address"] + " " + data["city"])
    conn = portal_db._conn()
    try:
        with conn.cursor() as cur:
            _ensure_ddl(cur)
            settings = _load_settings(cur, client_id)
        conn.commit()
    finally:
        conn.close()
    if not settings["enabled"] or not _configured(settings):
        return jsonify({"error": {"code": "not_configured",
                                  "message": "Save the credentials and"
                                             " enable the connector"
                                             " first."}}), 409
    entry = PROVIDERS.get(settings["provider"]) or PROVIDERS["leopards"]
    result = entry["book"](_effective_cfg(settings), data)
    if not result.get("ok"):
        return jsonify({"error": {"code": "courier_error",
                                  "message": result.get("message",
                                                        "Booking failed.")}}), 502
    booking_id = None
    conn = portal_db._conn()
    try:
        with conn.cursor() as cur:
            _ensure_ddl(cur)
            cur.execute(
                "INSERT INTO " + portal_db._q(BOOKINGS_TABLE) +
                " (client_id, contact_id, tracking_number, provider,"
                " status, cod_amount, city, payload)"
                " VALUES (%s, %s, %s, %s, 'booked', %s, %s,"
                " CAST(%s AS JSONB)) RETURNING id",
                (client_id, data["contact_id"],
                 result["tracking_number"], settings["provider"],
                 data["cod_amount"], data["city"],
                 json.dumps({k: v for k, v in data.items()})),
            )
            rows = portal_db.rows(cur)
            booking_id = int(rows[0].get("id") or 0) if rows else None
            portal_db.log_action(
                cur, client_id, "courier.book", "human",
                data["contact_id"] or None, None,
                ("Parcel " + result["tracking_number"] + " via "
                 + settings["provider"]),
            )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "id": booking_id,
                    "tracking_number": result["tracking_number"],
                    "address_issues": intel["issues"],
                    "ask_prompts": intel["ask_prompts"]}), 200


@bp.get("/courier/bookings")
def list_courier_bookings():
    principal, auth_error = _principal_or_error()
    if auth_error:
        return auth_error
    client_id = int(principal["client_id"])
    conn = portal_db._conn()
    try:
        with conn.cursor() as cur:
            _ensure_ddl(cur)
            cur.execute(
                "SELECT id, contact_id, tracking_number, provider,"
                " status, cod_amount, city, created_at, tracked_at"
                " FROM " + portal_db._q(BOOKINGS_TABLE) +
                " WHERE client_id = %s ORDER BY id DESC LIMIT 20",
                (client_id,),
            )
            rows = portal_db.rows(cur)
        conn.commit()
    finally:
        conn.close()
    bookings = []
    summary: Dict[str, int] = {}
    for r in rows:
        status = normalize_status(str(r.get("status") or ""))
        summary[status] = summary.get(status, 0) + 1
        bookings.append({
            "id": int(r.get("id") or 0),
            "contact_id": str(r.get("contact_id") or ""),
            "tracking_number": str(r.get("tracking_number") or ""),
            "provider": str(r.get("provider") or ""),
            "status": status,
            "raw_status": str(r.get("status") or ""),
            "cod_amount": float(r.get("cod_amount") or 0),
            "city": str(r.get("city") or ""),
            "created_at": None if r.get("created_at") is None
            else str(r.get("created_at")),
            "tracked_at": None if r.get("tracked_at") is None
            else str(r.get("tracked_at")),
        })
    return jsonify({"bookings": bookings, "summary": summary}), 200


@bp.post("/courier/track")
def track_courier_parcel():
    principal, auth_error = _principal_or_error()
    if auth_error:
        return auth_error
    client_id = int(principal["client_id"])
    payload = request.get_json(silent=True) or {}
    try:
        booking_id = int(payload.get("id") or 0)
    except (TypeError, ValueError):
        booking_id = 0
    if booking_id <= 0:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "id is required."}}), 400
    conn = portal_db._conn()
    try:
        with conn.cursor() as cur:
            _ensure_ddl(cur)
            settings = _load_settings(cur, client_id)
            cur.execute(
                "SELECT id, tracking_number, provider FROM " +
                portal_db._q(BOOKINGS_TABLE) +
                " WHERE id = %s AND client_id = %s LIMIT 1",
                (booking_id, client_id),
            )
            rows = portal_db.rows(cur)
            if not rows:
                return jsonify({"error": {"code": "not_found",
                                          "message": "No such"
                                                     " booking."}}), 404
            booking = rows[0]
            tracking_number = str(booking.get("tracking_number") or "")
        conn.commit()
    finally:
        conn.close()
    if not settings["enabled"] or not _configured(settings):
        return jsonify({"error": {"code": "not_configured",
                                  "message": "Save the credentials and"
                                             " enable the connector"
                                             " first."}}), 409
    entry = PROVIDERS.get(settings["provider"]) or PROVIDERS["leopards"]
    result = entry["track"](_effective_cfg(settings), tracking_number)
    if not result.get("ok"):
        return jsonify({"error": {"code": "courier_error",
                                  "message": result.get("message",
                                                        "Tracking failed.")}}), 502
    new_status = normalize_status(result.get("status") or "")
    events = result.get("events") or []
    conn = portal_db._conn()
    try:
        with conn.cursor() as cur:
            _ensure_ddl(cur)
            cur.execute(
                "UPDATE " + portal_db._q(BOOKINGS_TABLE) +
                " SET status = %s, track_json = CAST(%s AS JSONB),"
                " tracked_at = NOW(), updated_at = NOW()"
                " WHERE id = %s AND client_id = %s RETURNING id",
                (new_status, json.dumps(events), booking_id, client_id),
            )
            portal_db.rows(cur)
            portal_db.log_action(
                cur, client_id, "courier.track", "human", None, None,
                ("Parcel " + tracking_number + " -> " + new_status),
            )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "id": booking_id,
                    "status": new_status,
                    "raw_status": result.get("status") or "",
                    "events": events}), 200
