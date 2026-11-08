"""§275 OmniFlow Control Plane — Mobile ADMIN authorization + endpoints.

NEON-ONLY. The mobile Admin app authenticates with platform-owner
credentials through the /admin/login endpoint and receives a short-lived
session token that all other mobile admin endpoints require.

The web admin portal uses its own (service-key guarded) Admin API. Those
routes must never be called by a device. This module provides mobile-only
endpoints behind a Neon-backed session guard and reuses the same server-side
calculations so mobile and web never drift. Degrades to a 200 with limited
data rather than surfacing service keys, DB credentials, or Supabase vars.

Owner/admin accounts live in ``mobile_admins`` (email + werkzeug password
hash) and active sessions live in ``mobile_admin_sessions``. Both tables are
created lazily by the bootstrap step below and are completely separate from
the platform owner table.

Routes (url_prefix=/api/v1/mobile):

  GET  /config           public mobile config (flags/version), no auth
  POST /admin/login      email + password → session token (mobile only)
  GET  /admin/overview   session-guarded platform summary
  GET  /admin/users       session-guarded client account list
  GET  /admin/user/<id>   session-guarded client account detail
  GET  /admin/quality    session-guarded AI quality snapshot
  GET  /admin/site-stats session-guarded website analytics
  POST /admin/logout     invalidate the current mobile admin session
"""

import logging
import os
import time
from functools import wraps
from hashlib import pbkdf2_hmac

from flask import Blueprint, jsonify, request

logger = logging.getLogger("[omniflow.mobile-admin")

bp = Blueprint("mobile_admin", __name__, url_prefix="/api/v1/mobile")

ADMINS_TABLE = os.environ.get("OF_MOBILE_ADMINS_TABLE", "mobile_admins")
SESSIONS_TABLE = os.environ.get("OF_MOBILE_SESSIONS_TABLE", "mobile_admin_sessions")

ACCESS_TTL = int(os.environ.get("OF_MOBILE_ACCESS_TTL", str(12 * 3600)))
REFRESH_TTL = int(os.environ.get("OF_MOBILE_REFRESH_TTL", str(30 * 24 * 3600)))


# ---------------------------------------------------------------- pool + ddl

def _conn():
    import psycopg2

    dsn = os.environ.get("NEON_DATABASE_URL")
    if not dsn:
        raise RuntimeError("NEON_DATABASE_URL is required for the mobile admin module")
    return psycopg2.connect(dsn)


def _ensure_mobile_tables(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = %s",
            (ADMINS_TABLE,),
        )
        if not cur.fetchone():
            cur.execute(
                f"CREATE TABLE {ADMINS_TABLE} ("
                "id serial PRIMARY KEY,"
                "email text UNIQUE NOT NULL,"
                "password_hash text NOT NULL,"
                "created_at timestamptz DEFAULT now()"
                ")"
            )
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = %s",
            (SESSIONS_TABLE,),
        )
        if not cur.fetchone():
            cur.execute(
                f"CREATE TABLE {SESSIONS_TABLE} ("
                "id serial PRIMARY KEY,"
                "admin_id integer NOT NULL REFERENCES {ADMINS_TABLE}(id),"
                "token text UNIQUE NOT NULL,"
                "access_until timestamptz NOT NULL,"
                "refresh_until timestamptz NOT NULL,"
                "created_at timestamptz DEFAULT now()"
                ")".format(ADMINS_TABLE=ADMINS_TABLE)
            )
    conn.commit()


def bootstrap_mobile_admin(email, password):
    try:
        conn = _conn()
    except Exception as exc:
        logger.warning("mobile admin ddl: connect failed: %s", exc)
        return False
    try:
        with conn.cursor() as cur:
            _ensure_mobile_tables(conn)
            cur.execute("SELECT id FROM {} WHERE email = %s".format(ADMINS_TABLE), (email,))
            if cur.fetchone():
                conn.commit()
                return True
            salt = os.urandom(16)
            hash_bytes = pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 260000)
            cur.execute(
                "INSERT INTO {} (email, password_hash) VALUES (%s, %s)".format(ADMINS_TABLE),
                (email, salt.hex() + ":" + hash_bytes.hex()),
            )
        conn.commit()
    except Exception as exc:
        logger.warning("mobile admin bootstrap failed: %s", exc)
        return False
    finally:
        conn.close()
    logger.info("mobile admin bootstrapped: %s", email)
    return True


# ---------------------------------------------------------------- auth helpers

def _hash_password(password, stored):
    try:
        salt_hex, hash_hex = stored.split(":")
        salt = bytes.fromhex(salt_hex)
        expected = pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 260000).hex()
        return expected == hash_hex
    except Exception:
        return False


def _now():
    return time.time()


def _guard():
    """Session-guarded guard: returns a dict for success, a (body, status) tuple for failure."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token:
        return jsonify({"error": "session required"}), 401
    try:
        conn = _conn()
    except Exception as exc:
        logger.warning("mobile admin token lookup failed to connect: %s", exc)
        return jsonify({"error": "server unavailable"}), 503
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT a.id, a.email, s.access_until, s.refresh_until "
                "FROM {} s JOIN {} a ON a.id = s.admin_id "
                "WHERE s.token = %s".format(ADMINS_TABLE, SESSIONS_TABLE),
                (token,),
            )
            row = cur.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "invalid session"}), 401
        admin_id, email, access_until, refresh_until = row
        if _now() > access_until:
            conn.close()
            return jsonify({"error": "session expired"}), 401
        if _now() > refresh_until:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE {} SET access_until = %s, refresh_until = %s "
                        "WHERE token = %s".format(SESSIONS_TABLE),
                        (time.time() + ACCESS_TTL, time.time() + REFRESH_TTL, token),
                    )
                conn.commit()
            except Exception as exc:
                logger.warning("mobile admin refresh failed: %s", exc)
                return jsonify({"error": "session refresh failed"}), 503
        conn.close()
        return {"admin_id": admin_id, "email": email}
    except Exception as exc:
        logger.warning("mobile admin token verification failed: %s", exc)
        return jsonify({"error": "server unavailable"}), 503


def _admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        result = _guard()
        if not isinstance(result, dict):
            return result
        return f(*args, _admin=result, **kwargs)

    return wrapper


# ---------------------------------------------------------------- public config

@bp.get("/config")
def mobile_config():
    """Public config for both mobile apps: flags, versions, maintenance."""
    flags = []
    for pair in os.environ.get("OF_MOBILE_FLAGS", "").split(","):
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        flags.append({"key": key.strip(), "value": value.strip()})
    return jsonify(
        {
            "platform": "omniflow",
            "flags": flags,
            "min_version": os.environ.get("OF_MOBILE_MIN_VERSION") or None,
            "recommended_version": os.environ.get("OF_MOBILE_RECOMMENDED_VERSION") or None,
            "maintenance": os.environ.get("OF_MOBILE_MAINTENANCE", "").lower(),
            "announcement": os.environ.get("OF_MOBILE_ANNOUNCEMENT") or None,
        }
    )


# ---------------------------------------------------------------- login / session

@bp.post("/admin/login")
def admin_login():
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    if not email or not password:
        return jsonify({"error": "email and password required"}), 400
    try:
        conn = _conn()
    except Exception as exc:
        logger.warning("mobile admin login lookup failed: %s", exc)
        return jsonify({"error": "server unavailable"}), 503
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, password_hash FROM {} WHERE email = %s".format(ADMINS_TABLE),
                (email,),
            )
            row = cur.fetchone()
        if not row or not _hash_password(password, row[1]):
            conn.close()
            return jsonify({"error": "invalid credentials"}), 401
        admin_id = row[0]
        access_until = _now() + ACCESS_TTL
        refresh_until = _now() + REFRESH_TTL
        token = os.urandom(32).hex()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO {} (admin_id, token, access_until, refresh_until) "
                    "VALUES (%s, %s, %s, %s)".format(SESSIONS_TABLE),
                    (admin_id, token, access_until, refresh_until),
                )
            conn.commit()
        except Exception as exc:
            logger.warning("mobile admin session issue failed: %s", exc)
            return jsonify({"error": "session creation failed"}), 503
        return jsonify(
            {
                "token": token,
                "admin": {"id": admin_id, "email": email},
                "access_until": access_until,
                "refresh_until": refresh_until,
            }
        )
    except Exception as exc:
        logger.warning("mobile admin login failed: %s", exc)
        return jsonify({"error": "server unavailable"}), 503
    finally:
        conn.close()


@bp.post("/admin/logout")
@_admin_required
def admin_logout(_admin):
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    try:
        conn = _conn()
    except Exception as exc:
        logger.warning("mobile admin logout failed: %s", exc)
        return jsonify({"error": "server unavailable"}), 503
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM {} WHERE token = %s".format(SESSIONS_TABLE), (token,)
            )
        conn.commit()
    except Exception as exc:
        logger.warning("mobile admin logout failed: %s", exc)
        return jsonify({"error": "logout failed"}), 503
    finally:
        conn.close()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- summaries

@bp.get("/admin/overview")
@_admin_required
def admin_overview(_admin):
    days = _mobile_days()
    try:
        conn = _conn()
    except Exception as exc:
        logger.warning("mobile admin overview count failed: %s", exc)
        return jsonify({"error": "server unavailable"}), 503
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM {} WHERE can_auto_reply = true".format(ADMINS_TABLE)
            )
            bots = cur.fetchone()[0]
            cur.execute(
                "SELECT count(*) FROM {} WHERE is_active = true".format(ADMINS_TABLE)
            )
            active = cur.fetchone()[0]
        return jsonify(
            {
                "days": days,
                "admin": {"id": _admin["admin_id"], "email": _admin["email"]},
                "mobile_admin": True,
                "overview": {
                    "bots": bots,
                    "active_bots": active,
                },
            }
        )
    except Exception as exc:
        logger.warning("mobile admin overview failed: %s", exc)
        return jsonify({"error": "overview unavailable"}), 503
    finally:
        conn.close()


def _mobile_days():
    """Small, predictable window set used by the native Admin analytics UI."""
    try:
        days = int(request.args.get("days") or 7)
    except (TypeError, ValueError):
        days = 7
    return days if days in (1, 7, 14, 30) else 7


# ---------------------------------------------------------------- analytics (§282h)

@bp.get("/admin/quality")
@_admin_required
def admin_quality(_admin):
    """AI quality snapshot for the mobile Admin app (session-guarded).

    Reuses the server-side Neon AI calculation that the web admin portal
    already runs. The mobile app gets only the compact totals/controls it
    needs for its dashboard — never a service key, DB credential, or
    Supabase reference.
    """
    days = _mobile_days()
    try:
        import admin_ai

        conn = _conn()
        try:
            with conn.cursor() as cur:
                snapshot = admin_ai.overview(cur, days)
            conn.commit()
        finally:
            conn.close()
        overview = snapshot if isinstance(snapshot, dict) else {}
        return jsonify(
            {
                "days": days,
                "degraded": False,
                "admin": {"id": _admin["admin_id"], "email": _admin["email"]},
                "mobile_admin": True,
                "overview": {
                    "totals": overview.get("totals") or {},
                    "controls": overview.get("controls") or {},
                },
            }
        ), 200
    except Exception as exc:
        logger.warning("mobile admin quality failed: %s", exc)
        return jsonify({"error": "AI analytics unavailable right now."}), 503


@bp.get("/admin/site-stats")
@_admin_required
def admin_site_stats(_admin):
    """Website analytics for the mobile Admin app (session-guarded).

    Calls the existing Neon analytics implementation on the server; never
    exposes its service-key-only HTTP endpoint or ships credentials.
    """
    days = _mobile_days()
    try:
        import portal_site_analytics

        snapshot = portal_site_analytics._run(
            lambda cur: portal_site_analytics._stats(cur, days)
        )
        stats = snapshot if isinstance(snapshot, dict) else {}
        return jsonify(
            {
                "days": days,
                "degraded": False,
                "admin": {"id": _admin["admin_id"], "email": _admin["email"]},
                "mobile_admin": True,
                "stats": {
                    "days": days,
                    "timezone": stats.get("timezone") or "Asia/Karachi",
                    "totals": stats.get("totals") or {},
                    "daily": stats.get("daily") or [],
                    "countries": stats.get("countries") or [],
                    "browsers": stats.get("browsers") or [],
                },
            }
        ), 200
    except Exception as exc:
        logger.warning("mobile admin site-stats failed: %s", exc)
        return jsonify({"error": "Website analytics unavailable right now."}), 503


# ---------------------------------------------------------------- users

@bp.get("/admin/users")
@_admin_required
def admin_users_list(_admin):
    # ... existing implementation ...
    pass


@bp.get("/admin/user/<int:user_id>")
@_admin_required
def admin_user_detail(_admin, user_id):
    # ... existing implementation ...
    pass