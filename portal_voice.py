"""Voice channel slice one: owner click-to-call via Twilio.

The platform voice keys live in the admin panel (Integrations > Voice:
Twilio account SID, auth token, from number) or the OF_TWILIO_*
environment variables. With keys saved, the owner can call any
customer right from the conversation: Twilio places the call and reads
the short message aloud (TwiML <Say>), the call lands in a small log,
and Twilio's status webhook keeps the status column fresh.

Standard library only (urllib + base64), fail-soft like every other
provider adapter: a missing config is a clean 409, a Twilio error a
502, and nothing here ever blocks the ingest loop.
"""

import base64
import logging
import os
import urllib.parse
import urllib.request
from typing import Any, Dict

from flask import Blueprint, jsonify, request

import platform_settings
import portal_auth
import portal_db
from portal_auth import authenticate_portal_request
from portal_auth import ensure_human_principal

logger = logging.getLogger("omniflow.portal-voice")

bp = Blueprint("portal_voice", __name__, url_prefix="/api/v1/portal")
public_bp = Blueprint("portal_voice_public", __name__,
                      url_prefix="/api/v1/public")

CALLS_TABLE = "portal_voice_calls"
NAME_MAX = 300
LOG_LIMIT = 20

_DDL_READY = False

_DDL = (
    "CREATE TABLE IF NOT EXISTS " + portal_db._q(CALLS_TABLE) + " ("
    " id BIGSERIAL PRIMARY KEY,"
    " client_id BIGINT NOT NULL,"
    " contact_id TEXT NOT NULL DEFAULT '',"
    " phone TEXT NOT NULL,"
    " sid TEXT NOT NULL DEFAULT '',"
    " status TEXT NOT NULL DEFAULT 'queued',"
    " message TEXT NOT NULL DEFAULT '',"
    " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
    ")"
)
_DDL_INDEX = (
    "CREATE INDEX IF NOT EXISTS portal_voice_calls_idx ON "
    + portal_db._q(CALLS_TABLE) + " (client_id, id DESC)"
)


def _ensure_ddl(cur) -> None:
    global _DDL_READY
    if _DDL_READY:
        return
    cur.execute(_DDL)
    cur.execute(_DDL_INDEX)
    _DDL_READY = True


def _principal_or_error():
    """Module-level indirection so the test rig can stub auth."""
    return authenticate_portal_request(), None


def _voice_keys() -> Dict[str, str]:
    stored = {}
    try:
        stored = platform_settings.get_group("voice")
    except Exception:
        stored = {}
    def pick(name, env):
        return str(stored.get(name) or "").strip() or \
            os.environ.get(env, "").strip()
    return {
        "account_sid": pick("account_sid", "OF_TWILIO_ACCOUNT_SID"),
        "auth_token": str(stored.get("auth_token") or "")
                      or os.environ.get("OF_TWILIO_AUTH_TOKEN", ""),
        "from_number": pick("from_number", "OF_TWILIO_FROM_NUMBER"),
    }


def voice_configured(keys: Dict[str, str]) -> bool:
    return bool(keys.get("account_sid") and keys.get("auth_token")
                and keys.get("from_number"))


def _twilio_create_call(keys: Dict[str, str], to_number: str,
                        message: str) -> Dict[str, Any]:
    """POST the create-call request; dict response or {'_error': ...}."""
    try:
        endpoint = ("https://api.twilio.com/2010-04-01/Accounts/"
                    + keys["account_sid"] + "/Calls.json")
        twiml = ("<Response><Say>" + _xml_escape(message)
                 + "</Say></Response>")
        body = urllib.parse.urlencode({
            "To": to_number,
            "From": keys["from_number"],
            "Twiml": twiml,
        }).encode("utf8")
        token = base64.b64encode(
            (keys["account_sid"] + ":" + keys["auth_token"])
            .encode("utf8")).decode("ascii")
        req = urllib.request.Request(
            endpoint, data=body, method="POST",
            headers={
                "Authorization": "Basic " + token,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            import json
            return {"_status": resp.status,
                    **json.loads(resp.read().decode("utf8"))}
    except Exception as error:
        return {"_error": str(error) or "twilio request failed"}


def _xml_escape(text: str) -> str:
    return (str(text or "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _row_public(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": int(row.get("id") or 0),
        "contact_id": str(row.get("contact_id") or ""),
        "phone": str(row.get("phone") or ""),
        "sid": str(row.get("sid") or ""),
        "status": str(row.get("status") or "queued"),
        "message": str(row.get("message") or ""),
        "created_at": str(row.get("created_at") or ""),
    }


@bp.get("/voice/calls")
def list_voice_calls():
    principal, _ = _principal_or_error()
    if principal is None:
        return jsonify({"error": {"code": "unauthorized",
                                  "message": "Sign in required."}}), 401
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                _ensure_ddl(cur)
                cur.execute(
                    "SELECT id, contact_id, phone, sid, status,"
                    " message, created_at FROM "
                    + portal_db._q(CALLS_TABLE) +
                    " WHERE client_id = %s ORDER BY id DESC LIMIT %s",
                    (principal["client_id"], LOG_LIMIT),
                )
                rows = portal_db.rows(cur)
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(
            error, "voice calls")[0]), 503
    return jsonify({"calls": [_row_public(r) for r in rows]}), 200


@bp.post("/voice/calls")
def create_voice_call():
    principal, _ = _principal_or_error()
    if principal is None:
        return jsonify({"error": {"code": "unauthorized",
                                  "message": "Sign in required."}}), 401
    forbidden = ensure_human_principal(principal)
    if forbidden is not None:
        return forbidden
    payload = request.get_json(silent=True) or {}
    phone = str(payload.get("phone") or "").strip()
    message = str(payload.get("message") or "").strip()[:NAME_MAX]
    contact_id = str(payload.get("contact_id") or "").strip()[:100]
    if not phone.startswith("+") or not phone[1:].isdigit():
        return jsonify({"error": {"code": "bad_request",
                                  "message": "phone must be E.164, e.g."
                                             " +923001234567."}}), 400
    if not message:
        return jsonify({"error": {"code": "bad_request",
                                  "message": "Write the short message the"
                                             " call should read."}}), 400
    keys = _voice_keys()
    if not voice_configured(keys):
        return jsonify({"error": {"code": "not_configured",
                                  "message": "Voice is not configured -"
                                             " add the Twilio keys in the"
                                             " admin panel Integrations"
                                             " page."}}), 409
    result = _twilio_create_call(keys, phone, message)
    if "_error" in result or "_status" not in result \
            or int(result["_status"]) >= 300:
        logger.warning("twilio call failed: %s", result.get("_error"))
        return jsonify({"error": {"code": "twilio_error",
                                  "message": "Twilio rejected the call -"
                                             " check the saved keys and"
                                             " the from number."}}), 502
    try:
        portal_db.ensure_tables()
        conn = portal_db._conn()
        try:
            with conn.cursor() as cur:
                _ensure_ddl(cur)
                cur.execute(
                    "INSERT INTO " + portal_db._q(CALLS_TABLE) +
                    " (client_id, contact_id, phone, sid, status,"
                    " message) VALUES (%s, %s, %s, %s, %s, %s)"
                    " RETURNING id, contact_id, phone, sid, status,"
                    " message, created_at",
                    (principal["client_id"], contact_id, phone,
                     str(result.get("sid") or ""),
                     str(result.get("status") or "queued"), message),
                )
                created = portal_db.rows(cur)
                portal_db.log_action(
                    cur, principal["client_id"], "voice.called", "human",
                    principal.get("user_id"), None,
                    ("Twilio call to " + phone[-4:])[:200],
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        return jsonify(portal_db.portal_unavailable(
            error, "voice call")[0]), 503
    return jsonify({"ok": True, "call": _row_public(created[0])}), 200


@public_bp.post("/voice/webhook")
def voice_webhook():
    """Twilio status callback: update the call status by CallSid."""
    sid = str(request.form.get("CallSid") or "").strip()[:64]
    status = str(request.form.get("CallStatus") or "").strip()[:32]
    if sid and status:
        try:
            portal_db.ensure_tables()
            conn = portal_db._conn()
            try:
                with conn.cursor() as cur:
                    _ensure_ddl(cur)
                    cur.execute(
                        "UPDATE " + portal_db._q(CALLS_TABLE) +
                        " SET status = %s WHERE sid = %s",
                        (status, sid),
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            logger.warning("voice webhook update failed", exc_info=True)
    return "", 204
