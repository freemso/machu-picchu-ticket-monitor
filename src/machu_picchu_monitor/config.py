from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import AlertRule
from .route_matching import normalize_route_code


def _csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    target_dates: str = "2026-08-24,2026-08-25"
    target_routes: str = "2A,2B,3A,3B,1C"
    preferred_notification: str = "telegram"
    backup_notifications: str = ""

    poll_interval_seconds: int = Field(default=1800, ge=60)
    jitter_seconds: int = Field(default=300, ge=0)
    retry_attempts: int = Field(default=3, ge=1)
    retry_base_seconds: float = Field(default=2.0, ge=0.1)
    retry_max_seconds: float = Field(default=300.0, ge=1.0)
    request_timeout_seconds: float = Field(default=30.0, ge=1.0)

    sqlite_path: Path = Path("data/availability.sqlite3")
    log_level: str = "INFO"
    provider_mode: str = "auto"
    run_monitor_in_web: bool = True
    alert_on_first_seen: bool = False

    # Declarative alert rules. Edit rules.json to add a rule (see README), or set
    # ALERT_RULES to a JSON string to override the file (handy on Railway).
    alert_rules: str = ""
    alert_rules_file: Path = Path("rules.json")

    # Route catalog (IDs) changes rarely; cache it on disk next to the SQLite file.
    route_catalog_ttl_seconds: int = Field(default=7 * 24 * 3600, ge=60)

    app_host: str = "0.0.0.0"
    app_port: int = 8000

    official_site_base_url: str = "https://tuboleto.cultura.pe"
    official_api_base_url: str = "https://api-tuboleto.cultura.pe"
    official_place_slug: str = "llaqta_machupicchu"
    official_place_id: int = 1
    official_point_of_sale: int = 5
    official_api_secret: str = "5t4jPtv4LpmGgWU7ZYk8FhZf5LNTpk"
    encryption_password: str = "Km4pDqgVZdLNXYdde5jypBysh9MzkL"
    decrypt_security_salt: str = "In5iAIxnHwMTLg9ldHFUb3"
    encryption_iterations: int = 65536
    encryption_key_length_words: int = 8

    playwright_headless: bool = True
    playwright_timeout_ms: int = 45000

    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_to: str | None = None
    smtp_use_tls: bool = True

    slack_webhook_url: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def target_date_values(self) -> list[date]:
        return [date.fromisoformat(item) for item in _csv(self.target_dates)]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def target_route_values(self) -> list[str]:
        return [normalize_route_code(item) for item in _csv(self.target_routes)]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def backup_notification_values(self) -> list[str]:
        return [item.lower() for item in _csv(self.backup_notifications)]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def availability_url(self) -> str:
        return f"{self.official_site_base_url}/disponibilidad/{self.official_place_slug}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def place_url(self) -> str:
        return f"{self.official_site_base_url}/{self.official_place_slug}"

    def load_alert_rules(self) -> list[AlertRule]:
        """Load alert rules from rules.json / ALERT_RULES, falling back to deriving
        'increase' rules from TARGET_DATES x TARGET_ROUTES for backward compatibility."""
        from .rules import load_rules

        rules = load_rules(inline_json=self.alert_rules, rules_file=self.alert_rules_file)
        if rules:
            return rules
        return [
            AlertRule(
                name=f"increase:{route}:{visit_date}",
                type="increase",
                visit_date=visit_date,
                route=route,
            )
            for visit_date in self.target_date_values
            for route in self.target_route_values
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
