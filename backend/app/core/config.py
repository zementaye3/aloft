from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_JWT_DEFAULT = "change-me-in-production-use-secrets-token-hex-32"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = "development"
    log_level: str = "INFO"
    app_contact_email: str = "CHANGE_ME@example.com"
    host: str = "0.0.0.0"
    port: int = 8000
    reload: bool = False
    # MongoDB connection - configure via MONGODB_URI environment variable
    # Development default: mongodb://localhost:27017/?directConnection=true
    # Production example: mongodb+srv://user:pass@cluster.mongodb.net/aloft
    mongodb_uri: str = "mongodb://localhost:27017/?directConnection=true"
    mongodb_db_name: str = "aloft"

    # CORS configuration
    # In production, set this to specific origins like ["https://aloft.app", "https://www.aloft.app"]
    # Use ["*"] for development to allow all origins
    cors_allowed_origins: list[str] = ["*"]

    # CSP configuration
    # In production, set this to specific API domains your frontend needs to connect to
    # Example: ["https://api.groq.com", "https://api.elevenlabs.io"]
    # Leave empty for development defaults
    csp_allowed_connect_origins: list[str] = []
    # CSP report-only mode for testing (logs violations without blocking)
    # Set to true in development/staging to test CSP without breaking the app
    csp_report_only: bool = False
    # None means "no Redis configured" -- rate limiting fails open (allows
    # the request) rather than blocking the whole app from working if
    # Redis isn't set up yet. See core/redis_client.py and
    # services/rate_limiter.py. Sessions (core/redis.py) also use this;
    # connect_to_redis() in that module becomes a no-op when it's None.
    redis_url: str | None = None

    # Per-IP request caps for the genuinely expensive/quota-limited
    # endpoints. These numbers exist specifically because of the tight
    # free-tier ceilings documented in the zero-cost plan -- AviationStack
    # caps around 100/month total, Groq caps around 30/min total. One
    # client retrying in a loop, or one bad actor, could otherwise burn
    # through a month's AviationStack quota in seconds.
    rate_limit_flight_lookups_per_hour: int = 10
    # Live tracking has its own higher limit since it's user-triggered (not
    # background polling) and shares no bucket with recommendations.
    rate_limit_live_tracking_per_hour: int = 60
    rate_limit_content_generation_per_hour: int = 20
    # Position updates are called every few seconds from the mobile app.
    # 600/min = 10 calls/sec — generous enough for any real polling interval.
    rate_limit_position_updates_per_minute: int = 600

    # Additional rate limits for expensive operations
    rate_limit_poi_discovery_per_hour: int = 30
    rate_limit_story_generation_per_hour: int = 50
    rate_limit_audio_synthesis_per_hour: int = 30
    rate_limit_downloads_per_hour: int = 10
    rate_limit_session_creation_per_hour: int = 20
    rate_limit_mixed_audio_per_hour: int = 15
    rate_limit_image_retrieval_per_hour: int = 40
    # Public spectator view (GET /sessions/shared/{token}) has no auth to key
    # by, so it's IP-based and per-minute rather than per-hour like the
    # authenticated session endpoints -- someone actually watching a flight
    # will poll it every few seconds.
    rate_limit_spectator_view_per_minute: int = 120

    # Account lockout settings
    max_failed_login_attempts: int = 5
    account_lockout_duration_minutes: int = 30

    # Rate limiting algorithm selection (fixed, sliding, or token_bucket)
    # - fixed: Simple window counter, allows ~2x the limit at window boundaries
    # - sliding: More accurate, uses Redis sorted sets (default)
    # - token_bucket: Allows burst traffic, smoother rate limiting
    rate_limit_algorithm: str = "sliding"

    # Maximum concurrent Groq + ElevenLabs calls during batch content
    # generation (POST /routes/{route_key}/content).
    # 3 is a safe default for free-tier accounts:
    #   - Groq free tier: ~30 req/min total → 3 concurrent keeps us well under
    #   - ElevenLabs free tier: ~2 concurrent requests
    # Raise this if you're on a paid plan for faster batch generation.
    # Must be >= 1: asyncio.Semaphore(0) would permanently block all generation.
    content_generation_max_concurrent: int = Field(default=3, ge=1)

    # Content generation throttling -- tunable without code changes.
    # Groq free tier: ~30 req/min = 1 req/2s minimum.
    # Wikipedia free tier: generous but parallel bursts trigger 429s.
    content_inter_poi_delay_seconds: float = 3.0
    content_inter_image_delay_seconds: float = 1.0
    content_rate_limit_backoff_seconds: float = 60.0
    content_max_poi_retries: int = 3

    # How long a flight session lives in Redis before auto-expiring.
    # 12 hours covers any realistic flight duration with margin.
    session_ttl_seconds: int = 43200
    corridor_width_km: float = 100.0

    # GDPR Article 17 (Right to Erasure) grace period. Deletion is scheduled,
    # not immediate, so a user who deletes by mistake (or is coerced/hacked)
    # has a window to cancel via POST /v1/user/data/cancel-deletion.
    # The gdpr_worker actually purges data once this window elapses.
    gdpr_deletion_grace_period_hours: int = 48
    # How often the deletion worker polls for due deletions.
    gdpr_deletion_worker_interval_seconds: int = 900  # 15 minutes
    groq_api_key: SecretStr | None = None
    groq_model: str = "llama-3.3-70b-versatile"
    elevenlabs_api_key: SecretStr | None = None
    # Default voice: "Bella" -- free tier voice for ElevenLabs.
    # Find other voice IDs at elevenlabs.io/voice-lab or GET /v1/voices.
    elevenlabs_voice_id: str = "EXAVITQu4vr4xnSDxMaL"
    # ElevenLabs output format. mp3_22050_64 is the recommended default:
    # 22kHz / 64kbps is transparent quality for speech narration and uses
    # half the bandwidth/storage of mp3_44100_128. Switch to mp3_44100_128
    # or mp3_44100_192 if you need broadcast-quality output.
    # Valid values: mp3_22050_32, mp3_22050_64, mp3_44100_64, mp3_44100_128, mp3_44100_192
    elevenlabs_output_format: str = "mp3_22050_64"
    audio_storage_dir: str = "./audio_storage"
    aviationstack_api_key: str | None = None
    aerodatabox_api_key: str | None = None

    # ---------------------------------------------------------------------------
    # OpenRouter (optional — OpenAI-compatible gateway to 100+ LLMs)
    # ---------------------------------------------------------------------------
    # Drop-in fallback/alternative to Groq. Supports GPT-4, Claude, Llama,
    # Mistral, and 100+ other models through one OpenAI-compatible endpoint.
    # Free tier available. Get your key at: https://openrouter.ai/keys
    openrouter_api_key: SecretStr | None = None

    @property
    def groq_api_keys(self) -> list[str]:
        """Parse comma-separated Groq API keys into a list."""
        if not self.groq_api_key:
            return []
        keys = self.groq_api_key.get_secret_value().split(",")
        return [k.strip() for k in keys if k.strip()]

    @property
    def elevenlabs_api_keys(self) -> list[str]:
        """Parse comma-separated ElevenLabs API keys into a list."""
        if not self.elevenlabs_api_key:
            return []
        keys = self.elevenlabs_api_key.get_secret_value().split(",")
        return [k.strip() for k in keys if k.strip()]

    @property
    def aviationstack_api_keys(self) -> list[str]:
        """Parse comma-separated AviationStack API keys into a list."""
        if not self.aviationstack_api_key:
            return []
        keys = self.aviationstack_api_key.split(",")
        return [k.strip() for k in keys if k.strip()]

    @property
    def aerodatabox_api_keys(self) -> list[str]:
        """Parse comma-separated AeroDataBox API keys into a list."""
        if not self.aerodatabox_api_key:
            return []
        keys = self.aerodatabox_api_key.split(",")
        return [k.strip() for k in keys if k.strip()]

    # ---------------------------------------------------------------------------
    # Cloudflare R2 audio storage (optional — falls back to local disk when unset)
    # ---------------------------------------------------------------------------
    # R2 is S3-compatible. Get these values from:
    #   Cloudflare dashboard → R2 → Manage R2 API tokens
    #   Account ID: Cloudflare dashboard → right sidebar
    #
    # When all four R2 fields are set, audio files are stored in R2 and served
    # directly from there. When any field is missing, audio falls back to local
    # disk (audio_storage_dir). Local disk is fine for dev; R2 is required for
    # any deployment on ephemeral infrastructure (Render free tier, etc.).
    r2_account_id: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: SecretStr | None = None
    r2_bucket_name: str | None = None

    @property
    def r2_configured(self) -> bool:
        """True when all four R2 credentials are present."""
        return all(
            [
                self.r2_account_id,
                self.r2_access_key_id,
                self.r2_secret_access_key,
                self.r2_bucket_name,
            ]
        )

    # ---------------------------------------------------------------------------
    # JWT authentication
    # ---------------------------------------------------------------------------
    # Generate a strong secret with: python -c "import secrets; print(secrets.token_hex(32))"
    # Must be set in production. The default is intentionally weak so local
    # dev works out of the box, but will raise an error at startup if
    # ENVIRONMENT=production and the default is still in place.
    jwt_secret_key: SecretStr = SecretStr(_JWT_DEFAULT)

    # ---------------------------------------------------------------------------
    # Dev-only bypass flags
    # ---------------------------------------------------------------------------
    # Set SKIP_EMAIL_VERIFICATION=true in your .env to let unverified accounts
    # access protected endpoints. NEVER enable this in production — the
    # _check_production_secrets validator will raise an error if it's set.
    skip_email_verification: bool = False
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 30
    jwt_refresh_token_expire_days: int = 30

    @model_validator(mode="after")
    def _check_production_secrets(self) -> "Settings":
        """Refuse to start in production with the default JWT secret, an
        unconfigured database, or dev bypass flags.

        This turns silent security holes into loud startup crashes so they
        can never slip through to a real deployment unnoticed.
        """
        if self.environment.lower() == "production":
            missing_required = []

            jwt_value = self.jwt_secret_key.get_secret_value() if self.jwt_secret_key else ""
            if not jwt_value or jwt_value == _JWT_DEFAULT:
                missing_required.append("JWT_SECRET_KEY")

            if not self.mongodb_uri or "localhost" in self.mongodb_uri:
                missing_required.append("MONGODB_URI")

            if missing_required:
                raise ValueError(
                    f"Required secrets not configured: {', '.join(missing_required)}. "
                    "Set them via environment variables (JWT_SECRET_KEY, MONGODB_URI)."
                )

            if self.skip_email_verification:
                raise ValueError(
                    "SKIP_EMAIL_VERIFICATION must not be enabled in production. "
                    "Remove it from your environment or set it to false."
                )
        return self

    @model_validator(mode="after")
    def _check_production_cors(self) -> "Settings":
        """Refuse to start in production with wildcard CORS origins.

        Wildcard CORS allows any origin to access your API, which is a security risk.
        In production, specify your actual frontend domain(s).
        """
        if self.environment.lower() == "production" and self.cors_allowed_origins == ["*"]:
            raise ValueError(
                "CORS_ALLOWED_ORIGINS cannot be ['*'] in production. "
                "Set it to your actual frontend domain(s), e.g., ['https://aloft.app', 'https://www.aloft.app']"
            )
        return self

    # ---------------------------------------------------------------------------
    # POI source feature flags
    # ---------------------------------------------------------------------------
    # Wikipedia is the only source enabled by default. Wikidata and GeoNames
    # clients (app/clients/wikidata.py and app/clients/geonames.py) are fully
    # implemented and wired into poi_service.py -- they're just off by default
    # to keep discovery fast and avoid extra API calls until you want the
    # denser coverage they add. Flip the flags below to enable them.
    #
    # Wikidata: structured metadata (coordinates, type classifications,
    #   description), pulls from the Wikidata Query Service (SPARQL).
    #   No API key required -- public endpoint.
    #   See: https://query.wikidata.org/
    #
    # GeoNames: populated-place and geographic feature names in 11 languages.
    #   Requires a free GeoNames account (username, not a key).
    #   See: https://www.geonames.org/export/web-services.html
    #   Free tier: 1,000 credits/hour, 30,000/day.
    poi_source_wikidata_enabled: bool = False
    poi_source_geonames_enabled: bool = False
    geonames_username: str | None = None
    # OSM Overpass: named physical features (peaks, ruins, airports, etc.)
    # No API key required. Public endpoint: overpass-api.de
    poi_source_overpass_enabled: bool = False

    # ---------------------------------------------------------------------------
    # Openverse image fallback
    # ---------------------------------------------------------------------------
    # When Wikipedia has no images for a POI, Openverse is queried as a fallback.
    # Anonymous access works (100 req/min) but registering a free client gets
    # 500 req/min. Register at: https://api.openverse.org/v1/auth_tokens/register/
    openverse_client_id: str | None = None
    openverse_client_secret: SecretStr | None = None

    @property
    def openverse_client_ids(self) -> list[str]:
        """Parse comma-separated Openverse client IDs into a list."""
        if not self.openverse_client_id:
            return []
        keys = self.openverse_client_id.split(",")
        return [k.strip() for k in keys if k.strip()]

    @property
    def openverse_client_secrets(self) -> list[str]:
        """Parse comma-separated Openverse client secrets into a list."""
        if not self.openverse_client_secret:
            return []
        keys = self.openverse_client_secret.get_secret_value().split(",")
        return [k.strip() for k in keys if k.strip()]

    # ---------------------------------------------------------------------------
    # OpenSky Network (optional — live aircraft position)
    # ---------------------------------------------------------------------------
    # OpenSky now requires OAuth2 client credentials (basic auth was retired).
    # Create an API client at https://opensky-network.org → Account → API Client.
    # Set both fields to get ~4,000 req/day. Leave unset for anonymous (~100 req/day).
    opensky_client_id: str | None = None
    opensky_client_secret: SecretStr | None = None

    @property
    def opensky_client_ids(self) -> list[str]:
        """Parse comma-separated OpenSky client IDs into a list."""
        if not self.opensky_client_id:
            return []
        keys = self.opensky_client_id.split(",")
        return [k.strip() for k in keys if k.strip()]

    @property
    def opensky_client_secrets(self) -> list[str]:
        """Parse comma-separated OpenSky client secrets into a list."""
        if not self.opensky_client_secret:
            return []
        keys = self.opensky_client_secret.get_secret_value().split(",")
        return [k.strip() for k in keys if k.strip()]

    # ---------------------------------------------------------------------------
    # POI curation and destination tour settings
    # ---------------------------------------------------------------------------
    # Maximum number of curated POIs to keep after discovery (quality over quantity)
    max_curated_pois_per_route: int = 30
    # Number of destination highlights to generate for the destination tour
    destination_highlights_count: int = 20
    # Minutes between destination tour narrations during ocean crossings
    destination_tour_interval_minutes: int = 8
    # Minimum spacing in km between curated POIs (prevents clustering in one city)
    poi_min_spacing_km: float = 150.0

    # ---------------------------------------------------------------------------
    # Email service for password reset
    # ---------------------------------------------------------------------------
    # Resend (recommended): https://resend.com/ - Free tier: 3,000 emails/month
    # SendGrid (alternative): https://sendgrid.com/ - Free tier: 100 emails/day
    # FROM_EMAIL must be verified in your email service dashboard
    resend_api_key: SecretStr | None = None
    sendgrid_api_key: SecretStr | None = None
    from_email: str = "noreply@aloft.app"
    # Frontend base URL for password reset links and email verification
    # Configure via FRONTEND_BASE_URL environment variable
    # Development default: http://localhost:5500 (aloft-frontend/ static server)
    # Production example: https://aloft.app
    frontend_base_url: str = "http://localhost:5500"

    # ---------------------------------------------------------------------------
    # OneSignal push notifications
    # ---------------------------------------------------------------------------
    # OneSignal free tier: 10,000 notifications/month
    # Get credentials from: https://onesignal.com/
    onesignal_app_id: str | None = None
    onesignal_api_key: SecretStr | None = None


@lru_cache
def get_settings() -> Settings:
    """Return the application settings, cached after the first call.

    The @lru_cache decorator means Settings() is only constructed once per
    process. Subsequent calls return the same instance without re-reading
    environment variables or the .env file.

    UNIT TESTING:
    Because the result is cached, tests that need different settings values
    must clear the cache after patching the environment, otherwise the
    pre-patch values remain in effect:

        import os
        from app.core.config import get_settings

        def test_something(monkeypatch):
            monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32-chars-xxxxxxxxxxxxxxxx")
            get_settings.cache_clear()        # ← required
            settings = get_settings()
            assert settings.jwt_secret_key.get_secret_value() == "test-secret-..."
            get_settings.cache_clear()        # ← clean up for subsequent tests

    Alternatively, use the override_settings fixture if your conftest.py
    provides one.
    """
    return Settings()
