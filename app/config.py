"""Конфигурация из переменных окружения."""
from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "да"}


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "DATABASE_URL", "postgresql+psycopg://au_queue:au_queue@db:5432/au_queue"
    )
    secret_key: str = os.getenv("SECRET_KEY", "dev-insecure-secret-key")
    admin_username: str = os.getenv("ADMIN_USERNAME", "uymin")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "admin")

    teacher_name: str = os.getenv("TEACHER_NAME", "Уймин Антон Григорьевич")
    teacher_title: str = os.getenv("TEACHER_TITLE", "Преподаватель")
    site_title: str = os.getenv("SITE_TITLE", "Запись на приём")

    tz_name: str = os.getenv("TZ", "Asia/Yekaterinburg")

    # Ставить только при работе за HTTPS: с этим флагом браузер не отправит
    # куку по обычному http, и в локальной сети вход в админку «сломается».
    cookie_secure: bool = _bool("COOKIE_SECURE", False)

    booking_lead_minutes: int = _int("BOOKING_LEAD_MINUTES", 5)
    max_active_bookings: int = _int("MAX_ACTIVE_BOOKINGS", 1)
    days_ahead: int = _int("DAYS_AHEAD", 30)

    captcha_ttl_seconds: int = _int("CAPTCHA_TTL_SECONDS", 600)
    captcha_max_attempts: int = _int("CAPTCHA_MAX_ATTEMPTS", 4)

    group_pattern: str = os.getenv("GROUP_PATTERN", r"^[А-ЯЁ]{2}-[0-9]{2}-[0-9]{2}$")
    group_placeholder: str = os.getenv("GROUP_PLACEHOLDER", "КТ-24-04")

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.tz_name)
        except Exception:  # noqa: BLE001 — падать из-за кривой TZ не хочется
            return ZoneInfo("UTC")


settings = Settings()
