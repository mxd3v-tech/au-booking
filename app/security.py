"""Сессии администратора, токены броней и нормализация ввода."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets

from fastapi import Request, status
from fastapi.exceptions import HTTPException
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings

ADMIN_COOKIE = "au_admin"
BOOKINGS_COOKIE = "au_bookings"
ADMIN_SESSION_MAX_AGE = 12 * 60 * 60  # 12 часов
BOOKINGS_COOKIE_MAX_AGE = 60 * 60 * 24 * 120
MAX_REMEMBERED_BOOKINGS = 12

_serializer = URLSafeTimedSerializer(settings.secret_key, salt="au-queue")

_SPACES = re.compile(r"\s+")
_NAME_ALLOWED = re.compile(r"^[А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z\-'` .]{4,159}$")
_GROUP_RE = re.compile(settings.group_pattern)


# ── Администратор ───────────────────────────────────────────────────────

def check_admin_credentials(username: str, password: str) -> bool:
    # Сравниваем байты: compare_digest на строках падает, если в них
    # есть не-ASCII — а пароль с кириллицей ввести никто не запретит.
    ok_user = hmac.compare_digest(
        username.strip().encode("utf-8"), settings.admin_username.encode("utf-8")
    )
    ok_pass = hmac.compare_digest(
        password.encode("utf-8"), settings.admin_password.encode("utf-8")
    )
    return ok_user and ok_pass


def make_admin_token() -> str:
    return _serializer.dumps({"u": settings.admin_username})


def is_admin(request: Request) -> bool:
    raw = request.cookies.get(ADMIN_COOKIE)
    if not raw:
        return False
    try:
        data = _serializer.loads(raw, max_age=ADMIN_SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return False
    return data.get("u") == settings.admin_username


def require_admin(request: Request) -> bool:
    if not is_admin(request):
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            detail="Требуется вход",
            headers={"Location": "/admin/login"},
        )
    return True


# ── Брони студента в этом браузере ──────────────────────────────────────

def read_remembered(request: Request) -> list[str]:
    raw = request.cookies.get(BOOKINGS_COOKIE)
    if not raw:
        return []
    try:
        data = _serializer.loads(raw, max_age=BOOKINGS_COOKIE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return []
    return [str(t) for t in data][:MAX_REMEMBERED_BOOKINGS] if isinstance(data, list) else []


def write_remembered(tokens: list[str]) -> str:
    return _serializer.dumps(tokens[-MAX_REMEMBERED_BOOKINGS:])


def new_cancel_token() -> str:
    return secrets.token_urlsafe(24)


def hash_ip(request: Request) -> str:
    """Короткий необратимый отпечаток адреса — только чтобы ловить флуд."""
    client = request.headers.get("x-forwarded-for", "") or (
        request.client.host if request.client else ""
    )
    client = client.split(",")[0].strip()
    if not client:
        return ""
    digest = hashlib.blake2s(
        client.encode("utf-8"), key=settings.secret_key.encode("utf-8")[:32], digest_size=8
    )
    return digest.hexdigest()


# ── Нормализация и проверка ввода ───────────────────────────────────────

def clean_full_name(raw: str) -> str:
    name = _SPACES.sub(" ", (raw or "").strip())
    return " ".join(part[:1].upper() + part[1:] for part in name.split(" ") if part)


def name_key(raw: str) -> str:
    """Ключ для сравнения ФИО: регистр, лишние пробелы и ё не должны мешать."""
    return _SPACES.sub(" ", (raw or "").strip().lower()).replace("ё", "е")


def validate_full_name(name: str) -> str | None:
    if len(name) < 5:
        return "Укажите фамилию, имя и отчество полностью."
    if len(name.split(" ")) < 2:
        return "Нужно как минимум фамилия и имя."
    if not _NAME_ALLOWED.match(name):
        return "В ФИО допустимы только буквы, пробел и дефис."
    return None


def clean_group(raw: str) -> str:
    return _SPACES.sub("", (raw or "").strip().upper()).replace("—", "-").replace("–", "-")


def validate_group(group: str) -> str | None:
    if not _GROUP_RE.match(group):
        return f"Формат номера группы: {settings.group_placeholder}"
    return None


def dumps_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)
