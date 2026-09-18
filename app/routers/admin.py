"""Панель #au_team: приём, очередь, история, справочники, QR-код."""
from __future__ import annotations

import datetime as dt
import re
from urllib.parse import quote, urlsplit

import segno
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.captcha import KINDS, issue_challenge
from app.config import settings
from app.db import get_db
from app.models import EntryStatus, Purpose, QueueEntry, QueueSession, Setting
from app.security import (
    ADMIN_COOKIE,
    ADMIN_SESSION_MAX_AGE,
    check_admin_credentials,
    clean_full_name,
    clean_group,
    clean_text,
    make_admin_token,
    name_key,
    new_entry_token,
    require_admin,
    validate_full_name,
    validate_group,
)
from app.services import export as export_service
from app.services import queue as queue_service
from app.templating import templates

router = APIRouter(prefix="/admin")
guard = Depends(require_admin)

PUBLIC_URL_KEY = "public_url"


def _back(url: str, ok: str = "", err: str = "") -> RedirectResponse:
    parts = []
    if ok:
        parts.append(f"ok={quote(ok)}")
    if err:
        parts.append(f"err={quote(err)}")
    suffix = ("&" if "?" in url else "?") + "&".join(parts) if parts else ""
    return RedirectResponse(url + suffix, status_code=303)


#: Только 24-часовая запись: «09:00», «14:30», «23:59». Ни «2:30 PM», ни «25:00».
_TIME_RE = re.compile(r"^([01]?[0-9]|2[0-3]):([0-5][0-9])$")

TIME_FORMAT_ERROR = "Время пишем в 24-часовом формате: 09:00, 14:30, 18:00."


def _parse_window(
    time_from: str, time_to: str
) -> tuple[dt.time | None, dt.time | None, str]:
    """Границы приёма из формы: начало, конец и текст ошибки.

    Пустое поле — это «не указано», а вот непонятное время раньше молча
    превращалось в пустое, и приём открывался без часов вовсе.
    """
    bounds: list[dt.time | None] = []
    for raw in (time_from, time_to):
        raw = (raw or "").strip()
        if not raw:
            bounds.append(None)
            continue
        match = _TIME_RE.match(raw)
        if match is None:
            return None, None, TIME_FORMAT_ERROR
        bounds.append(dt.time(int(match.group(1)), int(match.group(2))))

    start, end = bounds
    if start and end and end <= start:
        return None, None, "Конец приёма должен быть позже начала."
    return start, end, ""


def _active_purposes(db: Session) -> list[Purpose]:
    return list(
        db.scalars(
            select(Purpose)
            .where(Purpose.is_active.is_(True))
            .order_by(Purpose.sort_order, Purpose.id)
        ).all()
    )


def _public_url(request: Request, db: Session) -> str:
    """Адрес для QR: сохранённый в админке, затем из env, затем из запроса."""
    stored = db.get(Setting, PUBLIC_URL_KEY)
    if stored and stored.value.strip():
        return stored.value.strip().rstrip("/")
    if settings.public_url.strip():
        return settings.public_url.strip().rstrip("/")
    return str(request.base_url).rstrip("/")


# ── Вход ────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "admin/login.html", {"error": ""})


@router.post("/login")
def login_submit(request: Request, username: str = Form(""), password: str = Form("")):
    if not check_admin_credentials(username, password):
        return templates.TemplateResponse(
            request,
            "admin/login.html",
            {"error": "Не тот логин или пароль. Проверьте раскладку и Caps Lock."},
            status_code=401,
        )
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        ADMIN_COOKIE,
        make_admin_token(),
        max_age=ADMIN_SESSION_MAX_AGE,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(ADMIN_COOKIE, path="/")
    return response


# ── Приём и очередь ─────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse, dependencies=[guard])
@router.get("/", response_class=HTMLResponse, dependencies=[guard])
def dashboard(request: Request, db: Session = Depends(get_db)):
    session = queue_service.current_session(db)
    entries = queue_service.entries_of(db, session.id) if session else []

    # Подставляем ближайшую «круглую» пятиминутку и два часа приёма:
    # поправить проще, чем набирать время с нуля.
    start = queue_service.now_local().replace(second=0, microsecond=0)
    start = start.replace(minute=start.minute - start.minute % 5)

    recent = [
        row
        for row in queue_service.past_sessions(db, limit=6)
        if session is None or row[0].id != session.id
    ]

    return templates.TemplateResponse(
        request,
        "admin/dashboard.html",
        {
            "session": session,
            "entries": entries,
            "counters": queue_service.counters(entries),
            "purposes": _active_purposes(db),
            "recent": recent[:5],
            "default_room": settings.default_room,
            "default_from": f"{start:%H:%M}",
            "default_to": f"{start + dt.timedelta(hours=2):%H:%M}",
            "ok": request.query_params.get("ok", ""),
            "err": request.query_params.get("err", ""),
        },
    )


@router.post("/session/open", dependencies=[guard])
def session_open(
    db: Session = Depends(get_db),
    room: str = Form(""),
    time_from: str = Form(""),
    time_to: str = Form(""),
    note: str = Form(""),
):
    start, end, error = _parse_window(time_from, time_to)
    if error:
        return _back("/admin", err=error)
    try:
        queue_service.open_session(
            db,
            room=room or settings.default_room,
            time_from=start,
            time_to=end,
            note=note,
        )
    except queue_service.QueueError as exc:
        return _back("/admin", err=str(exc))
    return _back("/admin", ok="Приём открыт. Студенты уже видят очередь.")


@router.post("/session/update", dependencies=[guard])
def session_update(
    db: Session = Depends(get_db),
    room: str = Form(""),
    time_from: str = Form(""),
    time_to: str = Form(""),
    note: str = Form(""),
):
    session = queue_service.current_session(db)
    if session is None:
        return _back("/admin", err="Приём не открыт.")

    start, end, error = _parse_window(time_from, time_to)
    if error:
        return _back("/admin", err=error)

    session.room = room.strip()[:64]
    session.time_from = start
    session.time_to = end
    session.note = note.strip()
    db.commit()
    return _back("/admin", ok="Сохранено.")


@router.post("/session/close", dependencies=[guard])
def session_close(db: Session = Depends(get_db)):
    session = queue_service.current_session(db)
    if session is None:
        return _back("/admin", err="Приём и так закрыт.")

    session_id = session.id
    try:
        left = queue_service.close_session(db, session)
    except queue_service.QueueError as exc:
        return _back("/admin", err=str(exc))
    message = "Приём закрыт, список ушёл в историю."
    if left:
        message += f" Не дождались: {left}."
    return _back(f"/admin/sessions/{session_id}", ok=message)


@router.post("/entries", dependencies=[guard])
def entry_add(
    db: Session = Depends(get_db),
    full_name: str = Form(""),
    group_name: str = Form(""),
    purpose_id: str = Form(""),
    comment: str = Form(""),
    namesake: str = Form(""),
):
    """Поставить в очередь руками — для тех, кто пришёл без телефона.

    Галочка «однофамилец» — единственный способ завести в приёме второе
    такое же ФИО: решает живой человек, который видит очередь целиком.
    """
    session = queue_service.current_session(db)
    if session is None:
        return _back("/admin", err="Приём не открыт — вставать некуда.")

    name = clean_full_name(full_name)
    group = clean_group(group_name)
    if error := validate_full_name(name):
        return _back("/admin", err=error)
    if error := validate_group(group):
        return _back("/admin", err=error)

    purpose = next((p for p in _active_purposes(db) if str(p.id) == purpose_id), None)
    try:
        entry = queue_service.join_queue(
            db,
            session=session,
            full_name=name,
            full_name_key=name_key(name),
            group_name=group,
            purpose_id=purpose.id if purpose else None,
            comment=clean_text(comment).strip()[:200],
            token=new_entry_token(),
            added_by_admin=True,
            allow_namesake=namesake == "on",
        )
    except queue_service.QueueError as exc:
        return _back("/admin", err=str(exc))
    return _back("/admin", ok=f"№{entry.number} — {entry.full_name}, добавлен вручную.")


@router.post("/entries/{entry_id}/status", dependencies=[guard])
def entry_status(
    entry_id: int,
    db: Session = Depends(get_db),
    status: str = Form(""),
    back: str = Form("/admin"),
):
    entry = db.get(QueueEntry, entry_id)
    if entry is None:
        return _back(back, err="Запись не найдена.")
    if status not in {s.value for s in EntryStatus}:
        return _back(back, err="Неизвестный статус.")

    queue_service.set_status(db, entry, EntryStatus(status))
    return _back(
        back,
        ok=f"№{entry.number} {entry.full_name}: {EntryStatus(status).label.lower()}.",
    )


@router.post("/entries/{entry_id}/delete", dependencies=[guard])
def entry_delete(
    entry_id: int,
    db: Session = Depends(get_db),
    back: str = Form("/admin"),
):
    entry = db.get(QueueEntry, entry_id)
    if entry is None:
        return _back(back, err="Запись не найдена.")
    name = entry.full_name
    db.delete(entry)
    db.commit()
    return _back(back, ok=f"«{name}» убран из очереди без следа.")


# ── История приёмов ─────────────────────────────────────────────────────

@router.get("/sessions", response_class=HTMLResponse, dependencies=[guard])
def sessions_list(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "admin/sessions.html",
        {
            "rows": queue_service.past_sessions(db),
            "ok": request.query_params.get("ok", ""),
            "err": request.query_params.get("err", ""),
        },
    )


@router.get("/sessions/{session_id}", response_class=HTMLResponse, dependencies=[guard])
def session_detail(session_id: int, request: Request, db: Session = Depends(get_db)):
    session = queue_service.session_by_id(db, session_id)
    if session is None:
        return _back("/admin/sessions", err="Такого приёма не было.")
    entries = queue_service.entries_of(db, session.id)
    return templates.TemplateResponse(
        request,
        "admin/session.html",
        {
            "session": session,
            "entries": entries,
            "counters": queue_service.counters(entries),
            "title": queue_service.session_title(session),
            "ok": request.query_params.get("ok", ""),
            "err": request.query_params.get("err", ""),
        },
    )


@router.post("/sessions/{session_id}/delete", dependencies=[guard])
def session_delete(session_id: int, db: Session = Depends(get_db)):
    session = db.get(QueueSession, session_id)
    if session is None:
        return _back("/admin/sessions", err="Такого приёма не было.")
    if session.is_open:
        return _back("/admin/sessions", err="Сначала закройте приём.")
    label = queue_service.session_title(session)
    db.delete(session)
    db.commit()
    return _back("/admin/sessions", ok=f"{label} — удалён вместе со списком.")


@router.get("/sessions/{session_id}/export.csv", dependencies=[guard])
def export_csv(session_id: int, db: Session = Depends(get_db)):
    session = queue_service.session_by_id(db, session_id)
    if session is None:
        return _back("/admin/sessions", err="Такого приёма не было.")
    data = export_service.to_csv(
        queue_service.entries_of(db, session.id), queue_service.session_title(session)
    )
    stamp = queue_service.to_local(session.opened_at).date()
    return Response(
        data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="ochered-{stamp}.csv"'},
    )


@router.get("/sessions/{session_id}/export.xlsx", dependencies=[guard])
def export_xlsx(session_id: int, db: Session = Depends(get_db)):
    session = queue_service.session_by_id(db, session_id)
    if session is None:
        return _back("/admin/sessions", err="Такого приёма не было.")
    data = export_service.to_xlsx(
        queue_service.entries_of(db, session.id), queue_service.session_title(session)
    )
    stamp = queue_service.to_local(session.opened_at).date()
    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="ochered-{stamp}.xlsx"'},
    )


@router.get("/sessions/{session_id}/print", response_class=HTMLResponse, dependencies=[guard])
def print_list(session_id: int, request: Request, db: Session = Depends(get_db)):
    session = queue_service.session_by_id(db, session_id)
    if session is None:
        return _back("/admin/sessions", err="Такого приёма не было.")
    return templates.TemplateResponse(
        request,
        "admin/print.html",
        {
            "session": session,
            "entries": queue_service.entries_of(db, session.id),
            "title": queue_service.session_title(session),
        },
    )


# ── Поиск по всем записям ───────────────────────────────────────────────

@router.get("/entries", response_class=HTMLResponse, dependencies=[guard])
def entries_search(
    request: Request,
    db: Session = Depends(get_db),
    group: str = "",
    status: str = "",
    date: str = "",
    q: str = "",
):
    query = (
        select(QueueEntry)
        .options(selectinload(QueueEntry.purpose), selectinload(QueueEntry.session))
        .join(QueueSession, QueueEntry.session_id == QueueSession.id)
        .order_by(QueueEntry.created_at.desc())
    )
    if group:
        query = query.where(QueueEntry.group_name == clean_group(group))
    if status in {s.value for s in EntryStatus}:
        query = query.where(QueueEntry.status == EntryStatus(status))
    if date:
        try:
            day = dt.date.fromisoformat(date)
        except ValueError:
            pass
        else:
            start = dt.datetime.combine(day, dt.time.min, tzinfo=settings.tz)
            query = query.where(
                QueueSession.opened_at >= start,
                QueueSession.opened_at < start + dt.timedelta(days=1),
            )
    if q:
        query = query.where(QueueEntry.full_name.ilike(f"%{q.strip()}%"))

    return templates.TemplateResponse(
        request,
        "admin/entries.html",
        {
            "entries": list(db.scalars(query.limit(500)).all()),
            "groups": queue_service.known_groups(db),
            "statuses": list(EntryStatus),
            "filters": {"group": group, "status": status, "date": date, "q": q},
            "ok": request.query_params.get("ok", ""),
            "err": request.query_params.get("err", ""),
        },
    )


# ── QR-код на дверь ─────────────────────────────────────────────────────

@router.get("/qr", response_class=HTMLResponse, dependencies=[guard])
def qr_page(request: Request, db: Session = Depends(get_db)):
    base = _public_url(request, db)
    target = f"{base}/q"
    # error="m" — код читается, даже если лист затёрли или оторвали угол.
    # omitsize=True добавляет viewBox: без него svg не масштабируется,
    # и код прижимается к углу рамки, вместо того чтобы её заполнить.
    svg = segno.make(target, error="m").svg_inline(
        scale=10, border=2, dark="#000000", light="#FFFFFF", omitsize=True
    )
    return templates.TemplateResponse(
        request,
        "admin/qr.html",
        {
            "qr_svg": svg,
            "target": target,
            "base": base,
            "host": urlsplit(target).netloc,
            "ok": request.query_params.get("ok", ""),
            "err": request.query_params.get("err", ""),
        },
    )


@router.post("/qr", dependencies=[guard])
def qr_save(db: Session = Depends(get_db), base: str = Form("")):
    value = base.strip().rstrip("/")
    if value and not value.startswith(("http://", "https://")):
        return _back("/admin/qr", err="Адрес должен начинаться с http:// или https://")

    stored = db.get(Setting, PUBLIC_URL_KEY)
    if stored is None:
        db.add(Setting(key=PUBLIC_URL_KEY, value=value))
    else:
        stored.value = value
    db.commit()
    if not value:
        return _back("/admin/qr", ok="Адрес сброшен — берём его из текущего запроса.")
    return _back("/admin/qr", ok="Адрес сохранён, QR перерисован.")


# ── Цели визита ─────────────────────────────────────────────────────────

@router.get("/purposes", response_class=HTMLResponse, dependencies=[guard])
def purposes_list(request: Request, db: Session = Depends(get_db)):
    purposes = list(
        db.scalars(select(Purpose).order_by(Purpose.sort_order, Purpose.id)).all()
    )
    return templates.TemplateResponse(
        request,
        "admin/purposes.html",
        {
            "purposes": purposes,
            "ok": request.query_params.get("ok", ""),
            "err": request.query_params.get("err", ""),
        },
    )


@router.post("/purposes", dependencies=[guard])
def purpose_create(
    db: Session = Depends(get_db),
    title: str = Form(""),
    hint: str = Form(""),
    needs_comment: str = Form(""),
    sort_order: int = Form(100),
):
    title = title.strip()[:160]
    if not title:
        return _back("/admin/purposes", err="Название не может быть пустым.")
    db.add(
        Purpose(
            title=title,
            hint=hint.strip()[:240],
            needs_comment=needs_comment == "on",
            sort_order=int(sort_order),
        )
    )
    db.commit()
    return _back("/admin/purposes", ok=f"Добавлено: «{title}».")


@router.post("/purposes/{purpose_id}/update", dependencies=[guard])
def purpose_update(
    purpose_id: int,
    db: Session = Depends(get_db),
    title: str = Form(""),
    hint: str = Form(""),
    needs_comment: str = Form(""),
    is_active: str = Form(""),
    sort_order: int = Form(100),
):
    purpose = db.get(Purpose, purpose_id)
    if purpose is None:
        return _back("/admin/purposes", err="Пункт не найден.")
    purpose.title = title.strip()[:160] or purpose.title
    purpose.hint = hint.strip()[:240]
    purpose.needs_comment = needs_comment == "on"
    purpose.is_active = is_active == "on"
    purpose.sort_order = int(sort_order)
    db.commit()
    return _back("/admin/purposes", ok="Сохранено.")


@router.post("/purposes/{purpose_id}/delete", dependencies=[guard])
def purpose_delete(purpose_id: int, db: Session = Depends(get_db)):
    purpose = db.get(Purpose, purpose_id)
    if purpose is None:
        return _back("/admin/purposes", err="Пункт не найден.")
    db.delete(purpose)
    db.commit()
    return _back("/admin/purposes", ok="Пункт удалён, у прежних записей он просто опустеет.")


# ── Просмотр капчи ──────────────────────────────────────────────────────

@router.get("/captcha", response_class=HTMLResponse, dependencies=[guard])
def captcha_preview(request: Request, kind: str = "", db: Session = Depends(get_db)):
    challenge = issue_challenge(db, kind if kind in KINDS else None)
    return templates.TemplateResponse(
        request,
        "admin/captcha.html",
        {"challenge": challenge, "kinds": KINDS, "current_kind": challenge.kind},
    )
