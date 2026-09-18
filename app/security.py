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
ENTRY_COOKIE = "au_entry"
ADMIN_SESSION_MAX_AGE = 12 * 60 * 60  # 12 часов
ENTRY_COOKIE_MAX_AGE = 60 * 60 * 24 * 30

_serializer = URLSafeTimedSerializer(settings.secret_key, salt="au-queue")

_SPACES = re.compile(r"\s+")
_NAME_ALLOWED = re.compile(r"^[А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z\-' ]{2,159}$")
_NAME_PARTS = re.compile(r"[-'\s]+")
_GROUP_RE = re.compile(settings.group_pattern)
# XML 1.0: эти символы нельзя записывать в ячейки XLSX. Переводы строк
# и табуляцию сохраняем — они допустимы в комментариях и таблицах.
_INVALID_TEXT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


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


# ── Своя запись в этом браузере ─────────────────────────────────────────

def read_entry_token(request: Request) -> str:
    """Токен записи, которую этот телефон поставил в очередь."""
    raw = request.cookies.get(ENTRY_COOKIE)
    if not raw:
        return ""
    try:
        token = _serializer.loads(raw, max_age=ENTRY_COOKIE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return ""
    return str(token) if isinstance(token, str) else ""


def write_entry_token(token: str) -> str:
    return _serializer.dumps(token)


def new_entry_token() -> str:
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

def clean_text(raw: str) -> str:
    return _INVALID_TEXT.sub("", raw or "")


def clean_full_name(raw: str) -> str:
    name = _SPACES.sub(" ", (raw or "").strip())
    return " ".join(part[:1].upper() + part[1:] for part in name.split(" ") if part)


def name_key(raw: str) -> str:
    """Ключ, по которому человек считается тем же самым.

    Сравниваем не буквы, а человека. Мимо уникального индекса пролезала любая
    мелочь в написании, поэтому здесь снимается всё, что человека не меняет:
    регистр, лишние пробелы, ё, дефис в двойной фамилии — и порядок слов,
    иначе «Иванов Иван» и «Иван Иванов» дают два разных ключа.
    """
    parts = _NAME_PARTS.split((raw or "").strip().lower().replace("ё", "е"))
    return " ".join(sorted(part for part in parts if part))


def validate_full_name(name: str) -> str | None:
    parts = [p for p in name.split(" ") if p]
    if len(parts) < 2:
        return "Нужны фамилия и имя. Отчество писать не надо."
    # Ровно два слова — не придирка к оформлению: пока отчество было
    # необязательным, его дописывали, чтобы встать в очередь второй раз.
    if len(parts) > 2:
        return "Только фамилия и имя — отчество писать не надо."
    if any(len(part) < 2 for part in parts):
        return "Имя и фамилию пишем целиком, не инициалами."
    if not _NAME_ALLOWED.match(name):
        return "В имени допустимы только буквы, пробел и дефис."
    return None


def clean_group(raw: str) -> str:
    return _SPACES.sub("", (raw or "").strip().upper()).replace("—", "-").replace("–", "-")


def validate_group(group: str) -> str | None:
    if not _GROUP_RE.match(group):
        return f"Формат номера группы: {settings.group_placeholder}"
    return None


def dumps_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)
