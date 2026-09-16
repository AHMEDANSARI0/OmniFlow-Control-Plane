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
from portal_channels import bp as portal_channels_bp  # noqa: E402
from portal_bot import bp as portal_bot_bp  # noqa: E402
from portal_profile import bp as portal_profile_bp  # noqa: E402
from portal_apikeys import bp as portal_apikeys_bp  # noqa: E402
from portal_conversations import bp as portal_conversations_bp  # noqa: E402
from connector_api import bp as connector_api_bp  # noqa: E402
from portal_followups import bp as portal_followups_bp  # noqa: E402
from portal_kb import bp as portal_kb_bp  # noqa: E402
from portal_catalog import bp as portal_catalog_bp  # noqa: E402
from portal_analytics import bp as portal_analytics_bp  # noqa: E402
from portal_revenue import bp as portal_revenue_bp  # noqa: E402
from portal_team import bp as portal_team_bp  # noqa: E402
from portal_growth import bp as portal_growth_bp  # noqa: E402
from portal_cod import bp as portal_cod_bp  # noqa: E402
from portal_segments import bp as portal_segments_bp  # noqa: E402
from portal_pipeline import bp as portal_pipeline_bp  # noqa: E402
from portal_insights import bp as portal_insights_bp  # noqa: E402
from portal_contacts import bp as portal_contacts_bp  # noqa: E402
from portal_fraud import bp as portal_fraud_bp  # noqa: E402
from portal_compliance import bp as portal_compliance_bp  # noqa: E402
from portal_setup import bp as portal_setup_bp  # noqa: E402
from portal_webhooks import bp as portal_webhooks_bp  # noqa: E402
from portal_sequences import bp as portal_sequences_bp  # noqa: E402
from portal_widget import bp as portal_widget_bp  # noqa: E402
from portal_auth import bind_root_application  # noqa: E402


# --- Extension endpoints (password reset + admin + portal + connector) ------
# create_control_plane_api() returns the ControlPlaneAPI WSGI wrapper, which
# does not expose the Flask instance, so the extensions live in their own
# small Flask app and requests are dispatched by explicit URL prefixes.
aux_app = Flask("omniflow_extensions")
aux_app.register_blueprint(auth_password_reset_bp)
aux_app.register_blueprint(admin_users_bp)
aux_app.register_blueprint(portal_channels_bp)
aux_app.register_blueprint(portal_bot_bp)
aux_app.register_blueprint(portal_profile_bp)
aux_app.register_blueprint(portal_apikeys_bp)
aux_app.register_blueprint(portal_conversations_bp)
aux_app.register_blueprint(connector_api_bp)
aux_app.register_blueprint(portal_followups_bp)
aux_app.register_blueprint(portal_kb_bp)
aux_app.register_blueprint(portal_catalog_bp)
aux_app.register_blueprint(portal_analytics_bp)
aux_app.register_blueprint(portal_revenue_bp)
aux_app.register_blueprint(portal_team_bp)
aux_app.register_blueprint(portal_growth_bp)
aux_app.register_blueprint(portal_cod_bp)
aux_app.register_blueprint(portal_segments_bp)
aux_app.register_blueprint(portal_pipeline_bp)
aux_app.register_blueprint(portal_insights_bp)
aux_app.register_blueprint(portal_contacts_bp)
aux_app.register_blueprint(portal_fraud_bp)
aux_app.register_blueprint(portal_compliance_bp)
aux_app.register_blueprint(portal_setup_bp)
aux_app.register_blueprint(portal_webhooks_bp)
aux_app.register_blueprint(portal_sequences_bp)
aux_app.register_blueprint(portal_widget_bp)

_EXTENSION_PREFIXES = (
    "/api/v1/auth/forgot-password",
    "/api/v1/auth/reset-password",
    "/api/v1/admin/users",
    "/api/v1/admin/reset-codes",
    "/api/v1/portal/",
    "/api/v1/connector/",
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

# Portal extensions validate customer Bearer tokens by asking the MAIN app
# itself (in-process GET /api/v1/auth/me) — bind the composite root here.
bind_root_application(application)

# Vercel discovers the entry point through a top-level ``app`` variable.
# Keep ``application`` for the existing Gunicorn entry point.
app = application
