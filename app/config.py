"""Конфигурация из переменных окружения."""
from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

DEFAULT_GROUP_PATTERN = r"^[А-ЯЁ]{2}-[0-9]{2}-[0-9]{2}$"


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
    admin_username: str = os.getenv("ADMIN_USERNAME", "admin")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "admin")

    teacher_name: str = os.getenv("TEACHER_NAME", "#au_team")
    teacher_title: str = os.getenv("TEACHER_TITLE", "Приём")
    site_title: str = os.getenv("SITE_TITLE", "Живая очередь")

    default_room: str = os.getenv("ROOM", "1215")

    # Адрес, который зашивается в QR-код. Пусто — берём из запроса;
    # за прокси это может оказаться внутренним адресом, тогда пропишите явно.
    public_url: str = os.getenv("PUBLIC_URL", "")

    tz_name: str = os.getenv("TZ", "Europe/Moscow")

    # Ставить только при работе за HTTPS: с этим флагом браузер не отправит
    # куку по обычному http, и в локальной сети вход в админку «сломается».
    cookie_secure: bool = _bool("COOKIE_SECURE", False)

    # Как часто страница очереди сама подтягивает свежий список, секунд.
    poll_seconds: int = _int("POLL_SECONDS", 15)

    captcha_ttl_seconds: int = _int("CAPTCHA_TTL_SECONDS", 600)

    group_pattern: str = os.getenv("GROUP_PATTERN", DEFAULT_GROUP_PATTERN)
    group_placeholder: str = os.getenv("GROUP_PLACEHOLDER", "КТ-24-04")

    @property
    def group_input_mask(self) -> str:
        # Произвольный regex нельзя безопасно превратить в маску ввода.
        # Для своего формата оставляем свободный ввод и серверную проверку.
        return "aa-00-00" if self.group_pattern == DEFAULT_GROUP_PATTERN else ""

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.tz_name)
        except Exception:  # noqa: BLE001 — падать из-за кривой TZ не хочется
            return ZoneInfo("UTC")


settings = Settings()
