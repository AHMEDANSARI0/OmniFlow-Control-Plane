"""Gunicorn + Vercel entry point for the OmniFlow Control Plane API v1."""
import os
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
# Refuse plaintext PostgreSQL transport even if a provider URL omits sslmode.
os.environ.setdefault("PGSSLMODE", "require")
from flask import Flask  # noqa: E402
from control_plane.http_api import create_control_plane_api  # noqa: E402
from auth_password_reset import bp as auth_password_reset_bp  # noqa: E402
from admin_users import bp as admin_users_bp  # noqa: E402
# --- Extension endpoints (password reset + admin user management) -----------
# create_control_plane_api() returns the ControlPlaneAPI WSGI wrapper, which
# does not expose the Flask instance, so the extensions live in their own
# small Flask app and requests are dispatched by URL prefix.
aux_app = Flask("omniflow_extensions")
aux_app.register_blueprint(auth_password_reset_bp)
aux_app.register_blueprint(admin_users_bp)
_EXTENSION_PREFIXES = (
    "/api/v1/auth/forgot-password",
    "/api/v1/auth/reset-password",
    "/api/v1/admin/users",
)
class _CompositeWsgi:
    """Dispatches extension paths to aux_app, everything else to the API."""
    def __init__(self, primary, aux):
        self._primary = primary
        self._aux = aux
    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if path.startswith(_EXTENSION_PREFIXES):
            return self._aux(environ, start_response)
        return self._primary(environ, start_response)
application = _CompositeWsgi(create_control_plane_api(), aux_app)
# Vercel discovers the entry point through a top-level ``app`` variable.
# Keep ``application`` for the existing Gunicorn entry point.
app = application

