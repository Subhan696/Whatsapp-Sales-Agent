from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Meta WhatsApp Cloud API
    META_VERIFY_TOKEN: str
    META_APP_SECRET: str
    META_ACCESS_TOKEN: str
    META_PHONE_NUMBER_ID: str
    META_GRAPH_API_VERSION: str = "v21.0"
    ADMIN_WHATSAPP_NUMBER: str

    # Channel provider — official Meta Cloud API, or the whatsapp-web.js bridge.
    # "wa_web" routes all inbound/outbound through the local Node bridge (wa-bridge/).
    CHANNEL_PROVIDER: Literal["meta", "wa_web"] = "meta"
    WA_BRIDGE_URL: str = "http://localhost:3000"
    WA_BRIDGE_TOKEN: str = ""  # shared secret; if set, required on /wa-bridge/inbound

    # Commerce
    DEFAULT_COMMERCE_MODE: Literal["website", "whatsapp_only"] = "whatsapp_only"
    SHOPIFY_STORE_DOMAIN: str = ""
    SHOPIFY_ADMIN_API_TOKEN: str = ""

    # LLM
    LLM_PROVIDER: Literal["anthropic", "openai"] = "anthropic"
    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    LLM_MODEL: str = ""
    VISION_MODEL: str = ""

    # Compliance / ops
    SERVICE_WINDOW_HOURS: int = 24
    OPT_OUT_KEYWORDS: str = "STOP,UNSUBSCRIBE"
    ENABLE_METRICS: bool = True
    LOG_LEVEL: str = "INFO"

    # Public base URL (used to convert /static/uploads/… paths to full URLs for WhatsApp)
    # Set to your ngrok URL while developing, e.g. https://abc123.ngrok-free.app
    BASE_URL: str = "http://localhost:8000"

    # Comma-separated list of origins allowed to call this API cross-origin
    # (e.g. a separately-hosted admin frontend). Empty = no cross-origin JS
    # callers allowed at all. The bundled CRM dashboard is served same-origin
    # and is unaffected either way.
    CORS_ALLOWED_ORIGINS: str = ""

    # Multi-tenancy — Phase 1 uses a single static tenant.
    # Phase 2 (multi-session manager) will resolve per-message instead.
    DEFAULT_TENANT_ID: int = 1

    # Platform-level secret gating tenant management (create/list/rotate-key).
    # Distinct from per-tenant admin_api_key — this protects the ability to
    # provision tenants at all. Empty by default; unset means tenant
    # management is locked (fail closed, not open).
    SUPERADMIN_KEY: str = ""

    # JWT secret for signing session tokens
    JWT_SECRET: str = "change_me_in_production"

    # Master key for encrypting sensitive secrets at rest (e.g. meta_access_token
    # in AppSetting). Fernet key — generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # Unset means encryption is disabled — sensitive values are stored as
    # plaintext with a startup warning, not a hard failure (don't brick existing
    # deployments that haven't set this yet).
    SECRETS_ENCRYPTION_KEY: str = ""

    # Database
    DATABASE_URL: str

    @field_validator("LLM_MODEL", mode="before")
    @classmethod
    def default_llm_model(cls, v: str, info) -> str:
        if not v:
            provider = (info.data or {}).get("LLM_PROVIDER", "anthropic")
            return "claude-sonnet-4-6" if provider == "anthropic" else "gpt-4o"
        return v

    @field_validator("VISION_MODEL", mode="before")
    @classmethod
    def default_vision_model(cls, v: str, info) -> str:
        if not v:
            provider = (info.data or {}).get("LLM_PROVIDER", "anthropic")
            return "claude-sonnet-4-6" if provider == "anthropic" else "gpt-4o"
        return v

    @property
    def opt_out_keywords_list(self) -> list[str]:
        return [k.strip().upper() for k in self.OPT_OUT_KEYWORDS.split(",") if k.strip()]

    @property
    def cors_allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ALLOWED_ORIGINS.split(",") if o.strip()]

    @property
    def meta_graph_base_url(self) -> str:
        return f"https://graph.facebook.com/{self.META_GRAPH_API_VERSION}"

    @model_validator(mode="after")
    def validate_llm_keys(self) -> "Settings":
        if self.LLM_PROVIDER == "anthropic" and not self.ANTHROPIC_API_KEY:
            raise ValueError("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic")
        if self.LLM_PROVIDER == "openai" and not self.OPENAI_API_KEY:
            raise ValueError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
        return self

    @model_validator(mode="after")
    def validate_shopify_config(self) -> "Settings":
        if self.DEFAULT_COMMERCE_MODE == "website":
            if not self.SHOPIFY_STORE_DOMAIN or not self.SHOPIFY_ADMIN_API_TOKEN:
                raise ValueError(
                    "SHOPIFY_STORE_DOMAIN and SHOPIFY_ADMIN_API_TOKEN are required "
                    "when DEFAULT_COMMERCE_MODE=website"
                )
        return self


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
