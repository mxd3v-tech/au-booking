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

    # ── Персональные данные ─────────────────────────────────────────────
    # Оператор — тот, кто отвечает за данные и к кому идут с отзывом
    # согласия. Без ORG_NAME согласие юридической силы не имеет, поэтому
    # при старте сервис об этом предупреждает.
    org_name: str = os.getenv("ORG_NAME", "")
    org_address: str = os.getenv("ORG_ADDRESS", "")
    # Куда писать про свои данные: почта, кабинет — что угодно читаемое.
    privacy_contact: str = os.getenv("PRIVACY_CONTACT", "")
    # Редакция текста согласия. Правите формулировки — меняйте и версию:
    # у каждой записи остаётся та, на которую согласился именно этот человек.
    consent_version: str = os.getenv("CONSENT_VERSION", "1")
    # Сколько дней держим закрытый приём со списком; 0 — не удалять само.
    retention_days: int = _int("RETENTION_DAYS", 180)

    # Свои слова к фильтру ФИО, через запятую. Сверяются по той же
    # нормализации, что и встроенный список.
    banned_names_extra: str = os.getenv("BANNED_NAMES_EXTRA", "")

    @property
    def group_input_mask(self) -> str:
        # Произвольный regex нельзя безопасно превратить в маску ввода.
        # Для своего формата оставляем свободный ввод и серверную проверку.
        return "aa-00-00" if self.group_pattern == DEFAULT_GROUP_PATTERN else ""

    @property
    def operator_known(self) -> bool:
        """Есть ли кому отвечать за данные — от этого зависит текст согласия."""
        return bool(self.org_name.strip())

    @property
    def retention_label(self) -> str:
        """Срок хранения по-русски: он попадает и в согласие, и в политику."""
        days = self.retention_days
        if days <= 0:
            return "до отзыва согласия"
        if days % 365 == 0:
            years = days // 365
            return f"{years} " + ("год" if years == 1 else "года" if years < 5 else "лет")
        if days % 30 == 0:
            months = days // 30
            return f"{months} " + ("месяц" if months == 1 else "месяца" if months < 5 else "месяцев")
        return f"{days} " + ("день" if days % 10 == 1 and days % 100 != 11 else
                             "дня" if days % 10 in (2, 3, 4) and days % 100 not in (12, 13, 14) else
                             "дней")

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.tz_name)
        except Exception:  # noqa: BLE001 — падать из-за кривой TZ не хочется
            return ZoneInfo("UTC")


settings = Settings()
