"""
Startup secret-configuration reporting.

This used to advertise AWS Secrets Manager / Azure Key Vault / Google
Secret Manager support, but that code was never actually wired into
Settings resolution (see app/core/config.py) -- Settings only ever reads
from environment variables and .env files via pydantic-settings' normal
mechanism. So if anyone had configured AWS_REGION etc. expecting their
JWT_SECRET_KEY to be pulled from AWS Secrets Manager, it silently
wouldn't have been: config.py's fields would still be reading the raw
env var (or the insecure default) the whole time.

This file now does exactly what the app actually does: reports which of
the important settings are configured, based on the real Settings object
that was actually constructed -- not a second, independent lookup that
can disagree with it.

If you do want real multi-cloud secret manager support later, the right
place to add it is a custom `settings_customise_sources` classmethod on
the Settings class in app/core/config.py, so secrets actually flow into
the fields pydantic-settings resolves -- not a parallel system like this
one used to be.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("aloft.secrets")

_REQUIRED_SETTINGS = ["jwt_secret_key", "mongodb_uri"]
_OPTIONAL_SETTINGS = [
    "groq_api_key",
    "elevenlabs_api_key",
    "aviationstack_api_key",
    "redis_url",
    "resend_api_key",
    "sendgrid_api_key",
]


def log_secret_validation_results() -> None:
    """Log which important settings are actually configured on the real
    Settings object, for visibility at startup. Called from main.py.
    """
    from app.core.config import get_settings

    settings = get_settings()

    configured: list[str] = []
    missing: list[str] = []

    for name in _REQUIRED_SETTINGS + _OPTIONAL_SETTINGS:
        value = getattr(settings, name, None)
        if hasattr(value, "get_secret_value"):
            value = value.get_secret_value()
        if value:
            configured.append(name.upper())
        else:
            missing.append(name.upper())

    if configured:
        logger.info("Configured settings: %s", ", ".join(configured))
    if missing:
        logger.warning("Not configured: %s", ", ".join(missing))