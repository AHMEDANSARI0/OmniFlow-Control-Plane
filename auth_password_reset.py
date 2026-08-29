"""
OmniFlow Control Plane — password reset endpoints (v2, schema: platform_users +
user_password_credentials). Registers as a Flask blueprint.

Endpoints:
  POST /api/v1/auth/forgot-password   {email}
  POST /api/v1/auth/reset-password    {code, new_password}
"""

import hashlib
import logging
import os
import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from flask import Blueprint, jsonify, request
from werkzeug.security import generate_password_hash

logger = logging.getLogger("omniflow.auth-reset")

bp = Blueprint("auth_password_reset", __name__, url_prefix="/api/v1/auth")

# --- Schema (matches verified Neon tables; override via env if they change) ---
USERS_TABLE = os.environ.get("OF_USERS_TABLE", "platform_users")
USER_ID_COL = "id"
USER_EMAIL_COL = "email"
CREDS_TABLE = os.environ.get("OF_CREDS_TABLE", "user_password_credentials")
CREDS_USER_COL = "user_id"
CREDS_PASSWORD_COL = "password_hash"
# ----------------------------------------------------------------------------

RESET_TTL_MINUTES = int(os.environ.get("OF_RESET_TTL_MINUTES", "10"))
MAX_ATTEMPTS = 3


def _q(ident: str) -> str:
    """Quote a dynamic identifier (config values, not user input)."""
    return '"' + ident.replace('"', '""') + '"'


def _conn():
    import psycopg2

    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "5432")),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        sslmode=os.environ.get("PGSSLMODE", "require"),
        connect_timeout=15,
    )


def _send_code_email(to_email: str, code: str, ttl_minutes: int) -> bool:
    host = os.environ.get("SMTP_HOST")
    if not host:
        return False
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("SMTP_FROM", user)

    msg = EmailMessage()
    msg["Subject"] = f"OmniFlow password reset code: {code}"
    msg["From"] = sender
    msg["To"] = to_email
    msg.set_content(
        f"Your OmniFlow password reset code is: {code}\n"
        f"It expires in {ttl_minutes} minutes. Enter it at "
        f"https://omniflow-bice.vercel.app/dashboard/reset-password"
    )

    try:
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=15) as smtp:
            smtp.starttls()
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
        return True
    except Exception:
        logger.exception("SMTP send failed; falling back to log delivery")
        return False


@bp.post("/forgot-password")
def forgot_password():
    payload = request.get_json(silent=True) or {}
    email = str(payload.get("email", "")).strip().lower()
    created = False

    if email and len(email) <= 320 and "@" in email:
        conn = None
        try:
            code = f"{secrets.randbelow(1_000_000):06d}"
            token_hash = hashlib.sha256(code.encode()).hexdigest()
            expires = datetime.now(timezone.utc) + timedelta(minutes=RESET_TTL_MINUTES)

            conn = _conn()
            cur = conn.cursor()
            cur.execute(
                f"SELECT {_q(USER_ID_COL)} FROM {_q(USERS_TABLE)} "
                f"WHERE lower({_q(USER_EMAIL_COL)}) = %s",
                (email,),
            )
            row = cur.fetchone()
            if row:
                user_id = row[0]
                cur.execute(
                    "DELETE FROM password_reset_tokens "
                    "WHERE user_id = %s AND used_at IS NULL",
                    (user_id,),
                )
                cur.execute(
                    "INSERT INTO password_reset_tokens (user_id, token_hash, expires_at) "
                    "VALUES (%s, %s, %s)",
                    (user_id, token_hash, expires),
                )
                conn.commit()
                created = True

                if not _send_code_email(email, code, RESET_TTL_MINUTES):
                    # TEST-PHASE delivery: admin reads the code from Vercel logs.
                    logger.warning(
                        "password reset code user_id=%s email=%s code=%s expires=%s",
                        user_id, email, code, expires.isoformat(),
                    )
        except Exception:
            logger.exception("forgot-password failed")
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            return jsonify({"error": {"code": "reset_unavailable",
                                      "message": "Try again later."}}), 503
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    # Same response whether or not the account exists (anti-enumeration).
    return jsonify({"ok": True, "delivered": created}), 200


@bp.post("/reset-password")
def reset_password():
    payload = request.get_json(silent=True) or {}
    code = str(payload.get("code", "")).strip()
    new_password = str(payload.get("new_password", ""))

    if not (len(code) == 6 and code.isdigit()):
        return jsonify({"error": {"code": "invalid_code",
                                  "message": "Code must be 6 digits."}}), 400
    if len(new_password) < 8 or len(new_password) > 1024:
        return jsonify({"error": {"code": "weak_password",
                                  "message": "Password length out of range."}}), 400

    conn = None
    try:
        token_hash = hashlib.sha256(code.encode()).hexdigest()
        now = datetime.now(timezone.utc)

        conn = _conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, user_id, expires_at, attempts, used_at "
            "FROM password_reset_tokens "
            "WHERE token_hash = %s ORDER BY id DESC LIMIT 1",
            (token_hash,),
        )
        row = cur.fetchone()
        if not row:
            return jsonify({"error": {"code": "invalid_code",
                                      "message": "Invalid reset code."}}), 400

        token_id, user_id, expires_at, attempts, used_at = row
        if used_at is not None:
            return jsonify({"error": {"code": "invalid_code",
                                      "message": "Code already used."}}), 400

        expires = expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= now:
            return jsonify({"error": {"code": "invalid_code",
                                      "message": "Code expired."}}), 410

        if int(attempts) >= MAX_ATTEMPTS:
            return jsonify({"error": {"code": "invalid_code",
                                      "message": "Too many attempts. Request a new code."}}), 429

        new_hash = generate_password_hash(new_password)
        cur.execute(
            f"UPDATE {_q(CREDS_TABLE)} SET {_q(CREDS_PASSWORD_COL)} = %s, "
            f"password_changed_at = now(), failed_attempt_count = 0, "
            f"locked_until = NULL, updated_at = now() "
            f"WHERE {_q(CREDS_USER_COL)} = %s",
            (new_hash, user_id),
        )
        cur.execute(
            "UPDATE password_reset_tokens SET used_at = %s WHERE id = %s",
            (now, token_id),
        )
        cur.execute(
            "DELETE FROM password_reset_tokens WHERE user_id = %s AND used_at IS NULL",
            (user_id,),
        )
        conn.commit()
        logger.info("password reset applied user_id=%s", user_id)
        return jsonify({"ok": True}), 200
    except Exception:
        logger.exception("reset-password failed")
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return jsonify({"error": {"code": "reset_unavailable",
                                  "message": "Try again later."}}), 503
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
