"""
Comprehensive startup configuration validation.

Validates all configuration settings before the application starts,
ensuring required values are present and valid.
"""

import re
from typing import Any


class ConfigurationError(Exception):
    """Raised when configuration validation fails."""

    pass


class ConfigValidator:
    """Validates application configuration."""

    def __init__(self, settings: Any):
        self.settings = settings
        self.errors = []
        self.warnings = []

    def validate_all(self) -> None:
        """Run all configuration validations."""
        self.validate_environment()
        self.validate_database()
        self.validate_redis()
        self.validate_secrets()
        self.validate_api_keys()
        self.validate_cors()
        self.validate_csp()
        self.validate_rate_limits()
        self.validate_audio_storage()
        self.validate_email_service()
        self.validate_external_apis()

        if self.errors:
            error_message = "Configuration validation failed:\n" + "\n".join(self.errors)
            if self.warnings:
                error_message += "\n\nWarnings:\n" + "\n".join(self.warnings)
            raise ConfigurationError(error_message)

        if self.warnings:
            print("Configuration warnings:")
            for warning in self.warnings:
                print(f"  WARNING: {warning}")

    def validate_environment(self) -> None:
        """Validate environment configuration."""
        valid_environments = ["development", "staging", "production"]
        if self.settings.environment.lower() not in valid_environments:
            self.errors.append(
                f"Invalid ENVIRONMENT: {self.settings.environment}. "
                f"Must be one of: {', '.join(valid_environments)}"
            )

        valid_log_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if self.settings.log_level.upper() not in valid_log_levels:
            self.errors.append(
                f"Invalid LOG_LEVEL: {self.settings.log_level}. "
                f"Must be one of: {', '.join(valid_log_levels)}"
            )

    def validate_database(self) -> None:
        """Validate MongoDB configuration."""
        if not self.settings.mongodb_uri:
            self.errors.append("MONGODB_URI is required")

        if (
            "localhost" in self.settings.mongodb_uri
            and self.settings.environment.lower() == "production"
        ):
            self.warnings.append(
                "MONGODB_URI contains 'localhost' in production environment. "
                "This is likely a configuration error."
            )

        if not self.settings.mongodb_db_name:
            self.errors.append("MONGODB_DB_NAME is required")

    def validate_redis(self) -> None:
        """Validate Redis configuration."""
        if self.settings.redis_url:
            if (
                "localhost" in self.settings.redis_url
                and self.settings.environment.lower() == "production"
            ):
                self.warnings.append(
                    "REDIS_URL contains 'localhost' in production environment. "
                    "This is likely a configuration error."
                )
        else:
            self.warnings.append(
                "REDIS_URL is not configured. Rate limiting and sessions will be disabled."
            )

    def validate_secrets(self) -> None:
        """Validate secret configuration."""
        # JWT secret key validation.
        # The list of weak defaults must stay in sync with the _JWT_DEFAULT
        # constant in app/core/config.py.
        jwt_secret = self.settings.jwt_secret_key.get_secret_value()
        weak_defaults = [
            # Exact default from config.py — keep this in sync!
            "change-me-in-production-use-secrets-token-hex-32",
            # Common weak values that are not the exact default but still bad
            "secret",
            "password",
            "123456",
            "default",
        ]

        if any(jwt_secret == d or jwt_secret.startswith(d) for d in weak_defaults):
            if self.settings.environment.lower() == "production":
                self.errors.append(
                    "JWT_SECRET_KEY appears to be a weak default value. "
                    'Generate a strong secret with: python -c "import secrets; print(secrets.token_hex(32))"'
                )
            else:
                self.warnings.append(
                    "JWT_SECRET_KEY appears to be a weak default value. "
                    "This is acceptable for development but not production."
                )

        if len(jwt_secret) < 32:
            self.errors.append(
                f"JWT_SECRET_KEY is too short ({len(jwt_secret)} characters). "
                "Minimum 32 characters recommended."
            )

    def validate_api_keys(self) -> None:
        """Validate API key configuration."""
        # Groq and ElevenLabs are always required -- they power the core experience
        required_apis = {
            "GROQ_API_KEY": self.settings.groq_api_key,
            "ELEVENLABS_API_KEY": self.settings.elevenlabs_api_key,
        }

        for api_name, api_key in required_apis.items():
            if not api_key:
                if self.settings.environment.lower() == "production":
                    self.errors.append(f"{api_name} is required in production")
                else:
                    self.warnings.append(
                        f"{api_name} is not configured. Some features may not work."
                    )

        # Flight resolution: at least one of AeroDataBox or AviationStack must be configured.
        # AeroDataBox is preferred (1 API call vs 3, same free quota).
        has_aerodatabox = bool(self.settings.aerodatabox_api_key)
        has_aviationstack = bool(self.settings.aviationstack_api_key)

        if not has_aerodatabox and not has_aviationstack:
            if self.settings.environment.lower() == "production":
                self.errors.append(
                    "At least one flight resolution API key is required in production: "
                    "AERODATABOX_API_KEY (recommended) or AVIATIONSTACK_API_KEY."
                )
            else:
                self.warnings.append(
                    "Neither AERODATABOX_API_KEY nor AVIATIONSTACK_API_KEY is configured. "
                    "Flight number resolution will not work."
                )
        elif not has_aerodatabox:
            self.warnings.append(
                "AERODATABOX_API_KEY is not configured. "
                "Flight resolution will use AviationStack (3 API calls per flight vs 1 for AeroDataBox). "
                "Sign up free at rapidapi.com/aedbx-aedbx/api/aerodatabox"
            )

    def validate_cors(self) -> None:
        """Validate CORS configuration."""
        if not self.settings.cors_allowed_origins:
            self.errors.append("CORS_ALLOWED_ORIGINS is required")

        if (
            self.settings.environment.lower() == "production"
            and self.settings.cors_allowed_origins == ["*"]
        ):
            self.errors.append(
                "CORS_ALLOWED_ORIGINS cannot be ['*'] in production. "
                "Set it to your actual frontend domain(s)."
            )

        # Validate origin format
        for origin in self.settings.cors_allowed_origins:
            if origin != "*" and not origin.startswith(("http://", "https://")):
                self.errors.append(
                    f"Invalid CORS origin: {origin}. Origins must start with http:// or https://"
                )

    def validate_csp(self) -> None:
        """Validate CSP configuration."""
        if self.settings.csp_report_only and self.settings.environment.lower() == "production":
            self.warnings.append(
                "CSP_REPORT_ONLY is enabled in production. "
                "CSP violations will be logged but not blocked. "
                "Disable this after testing is complete."
            )

    def validate_rate_limits(self) -> None:
        """Validate rate limit configuration."""
        rate_limits = {
            "rate_limit_flight_lookups_per_hour": self.settings.rate_limit_flight_lookups_per_hour,
            "rate_limit_content_generation_per_hour": self.settings.rate_limit_content_generation_per_hour,
            "rate_limit_position_updates_per_minute": self.settings.rate_limit_position_updates_per_minute,
        }

        for limit_name, limit_value in rate_limits.items():
            if limit_value <= 0:
                self.errors.append(f"{limit_name} must be greater than 0")
            if limit_value > 10000:
                self.warnings.append(
                    f"{limit_name} is very high ({limit_value}). This may lead to quota exhaustion."
                )

    def validate_audio_storage(self) -> None:
        """Validate audio storage configuration."""
        if self.settings.r2_configured:
            # R2 is configured, validate settings
            if not self.settings.r2_account_id:
                self.errors.append("R2_ACCOUNT_ID is required when using R2")
            if not self.settings.r2_access_key_id:
                self.errors.append("R2_ACCESS_KEY_ID is required when using R2")
            if not self.settings.r2_secret_access_key:
                self.errors.append("R2_SECRET_ACCESS_KEY is required when using R2")
            if not self.settings.r2_bucket_name:
                self.errors.append("R2_BUCKET_NAME is required when using R2")
        else:
            if self.settings.environment.lower() == "production":
                # This used to be a warning. It's now a hard error because R2
                # is no longer optional in practice: (1) without it, narration
                # audio written to local disk is lost on every restart/redeploy
                # on ephemeral hosting (Render free tier has no persistent
                # disk), and (2) background music tracks are no longer bundled
                # in the Docker image (see Dockerfile) and are fetched from R2
                # on first use -- without R2, POST /pois/{id}/audio/mixed will
                # 404 for every request.
                self.errors.append(
                    "R2 is not configured but ENVIRONMENT=production. R2_ACCOUNT_ID, "
                    "R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, and R2_BUCKET_NAME are all "
                    "required in production: generated narration audio is lost on every "
                    "restart without it (no persistent disk on most free-tier hosting), "
                    "and background music tracks are fetched from R2 at runtime rather "
                    "than bundled in the image."
                )

    def validate_email_service(self) -> None:
        """Validate email service configuration."""
        has_resend = self.settings.resend_api_key is not None
        has_sendgrid = self.settings.sendgrid_api_key is not None

        if (
            not has_resend
            and not has_sendgrid
            and self.settings.environment.lower() == "production"
        ):
            self.warnings.append(
                "No email service configured (RESEND_API_KEY or SENDGRID_API_KEY). "
                "Password reset and email verification will not work."
            )

        if has_resend and has_sendgrid:
            self.warnings.append(
                "Both RESEND_API_KEY and SENDGRID_API_KEY are configured. "
                "Resend will be used by default."
            )

        # Validate FROM_EMAIL format
        email_pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        if not re.match(email_pattern, self.settings.from_email):
            self.errors.append(f"Invalid FROM_EMAIL format: {self.settings.from_email}")

    def validate_external_apis(self) -> None:
        """Validate external API configuration."""
        # OpenSky API
        if self.settings.opensky_client_id and not self.settings.opensky_client_secret:
            self.errors.append("OPENSKY_CLIENT_SECRET is required when OPENSKY_CLIENT_ID is set")

        # Openverse API
        if self.settings.openverse_client_id and not self.settings.openverse_client_secret:
            self.errors.append(
                "OPENVERSE_CLIENT_SECRET is required when OPENVERSE_CLIENT_ID is set"
            )

        # GeoNames
        if self.settings.poi_source_geonames_enabled and not self.settings.geonames_username:
            self.errors.append(
                "GEONAMES_USERNAME is required when POI_SOURCE_GEONAMES_ENABLED is true"
            )


def validate_configuration(settings: Any) -> None:
    """Validate application configuration on startup.

    Args:
        settings: Application settings object

    Raises:
        ConfigurationError: If validation fails
    """
    validator = ConfigValidator(settings)
    validator.validate_all()


def validate_for_development(settings: Any) -> None:
    """Validate configuration for development environment.

    Less strict validation for development, with warnings for production-like issues.

    Args:
        settings: Application settings object
    """
    validator = ConfigValidator(settings)

    # Run validations but downgrade errors to warnings
    validator.validate_all()

    # In development, treat errors as warnings
    if validator.errors:
        print("Development configuration warnings:")
        for error in validator.errors:
            print(f"  WARNING: {error}")
