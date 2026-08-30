"""Gunicorn entry point for the OmniFlow Control Plane API v1."""

import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
SRC_DIR = PROJECT_ROOT / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Refuse plaintext PostgreSQL transport even if a provider URL omits sslmode.
os.environ.setdefault("PGSSLMODE", "require")

from control_plane.http_api import create_control_plane_api  # noqa: E402
from auth_password_reset import bp as auth_password_reset_bp
from admin_users import bp as admin_users_bp


application = create_control_plane_api()
application.register_blueprint(auth_password_reset_bp)
application.register_blueprint(admin_users_bp)

# Vercel discovers Flask applications through a top-level variable named
# ``app``.  Keep ``application`` for the existing Gunicorn entry point.
app = application
