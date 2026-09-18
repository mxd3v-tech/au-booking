"""Студенческая часть: очередь, постановка в неё, капча."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.captcha import consume_challenge, issue_challenge, verify_challenge
from app.config import settings
from app.db import get_db
from app.models import EntryStatus, Purpose, QueueEntry
from app.security import (
    ENTRY_COOKIE,
    ENTRY_COOKIE_MAX_AGE,
    clean_full_name,
    clean_group,
    hash_ip,
    name_key,
    new_entry_token,
    read_entry_token,
    validate_full_name,
    validate_group,
    write_entry_token,
)
from app.services import queue as queue_service
from app.templating import templates

router = APIRouter()


def _active_purposes(db: Session) -> list[Purpose]:
    return list(
        db.scalars(
            select(Purpose)
            .where(Purpose.is_active.is_(True))
            .order_by(Purpose.sort_order, Purpose.id)
        ).all()
    )


def _my_entry(request: Request, db: Session, session_id: int | None) -> QueueEntry | None:
    """Запись, которую поставил этот телефон, — только в текущем сеансе."""
    entry = queue_service.entry_by_token(db, read_entry_token(request))
    if entry is None or session_id is None or entry.session_id != session_id:
        return None
    return entry


def _remember(response: Response, token: str) -> None:
    response.set_cookie(
        ENTRY_COOKIE,
        write_entry_token(token),
        max_age=ENTRY_COOKIE_MAX_AGE,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def _queue_context(request: Request, db: Session) -> dict:
    session = queue_service.current_session(db)
    entries = queue_service.entries_of(db, session.id) if session else []
    mine = _my_entry(request, db, session.id if session else None)
    return {
        "session": session,
        "entries": entries,
        "mine": mine,
        "ahead": queue_service.waiting_before(entries, mine) if mine else 0,
        "counters": queue_service.counters(entries),
    }


# ── Очередь ─────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    context = _queue_context(request, db)
    return templates.TemplateResponse(request, "index.html", context)


@router.get("/q", include_in_schema=False)
def qr_target():
    """Куда ведёт QR-код на двери. Отдельный адрес — он короче и не меняется."""
    return RedirectResponse("/", status_code=307)


@router.get("/api/queue")
def queue_fragment(request: Request, db: Session = Depends(get_db)):
    """Свежий список для автообновления: рисуем тем же шаблоном, что и страницу."""
    context = _queue_context(request, db)
    session = context["session"]
    html = templates.get_template("partials/queue_list.html").render(
        {"request": request, **context}
    )
    return JSONResponse(
        {
            "open": session is not None,
            "session_id": session.id if session else None,
            "waiting": context["counters"]["waiting"],
            "ahead": context["ahead"],
            "mine": context["mine"].number if context["mine"] else None,
            "mine_status": context["mine"].status.value if context["mine"] else "",
            "html": html,
        }
    )


# ── Постановка в очередь ────────────────────────────────────────────────

def _join_form(
    request: Request,
    db: Session,
    session,
    *,
    errors: dict[str, str] | None = None,
    values: dict[str, str] | None = None,
    status_code: int = 200,
):
    return templates.TemplateResponse(
        request,
        "join.html",
        {
            "session": session,
            "purposes": _active_purposes(db),
            "groups": queue_service.known_groups(db),
            "challenge": issue_challenge(db),
            "errors": errors or {},
            "values": values or {},
        },
        status_code=status_code,
    )


@router.get("/join", response_class=HTMLResponse)
def join_form(request: Request, db: Session = Depends(get_db)):
    session = queue_service.current_session(db)
    if session is None:
        return RedirectResponse("/", status_code=303)
    if _my_entry(request, db, session.id) is not None:
        return RedirectResponse("/#my", status_code=303)
    return _join_form(request, db, session)


@router.post("/join")
def join_submit(
    request: Request,
    db: Session = Depends(get_db),
    full_name: str = Form(""),
    group_name: str = Form(""),
    purpose_id: str = Form(""),
    comment: str = Form(""),
    captcha_id: str = Form(""),
):
    session = queue_service.current_session(db)
    if session is None:
        return RedirectResponse("/", status_code=303)

    # Записаться можно один раз за приём — неважно, ждёт человек или его уже
    # приняли. Форму с этого телефона показывать больше нечего.
    if _my_entry(request, db, session.id) is not None:
        return RedirectResponse("/#my", status_code=303)

    name = clean_full_name(full_name)
    group = clean_group(group_name)
    comment = (comment or "").strip()[:200]
    purposes = _active_purposes(db)

    errors: dict[str, str] = {}
    if error := validate_full_name(name):
        errors["full_name"] = error
    if error := validate_group(group):
        errors["group_name"] = error

    purpose: Purpose | None = None
    if purposes:
        try:
            purpose = next(p for p in purposes if str(p.id) == purpose_id)
        except StopIteration:
            errors["purpose_id"] = "Выберите, с чем идёте."
        else:
            if purpose.needs_comment and not comment:
                errors["comment"] = "Для этого пункта нужно короткое пояснение."

    if not consume_challenge(db, captcha_id):
        errors["captcha"] = "Проверка не пройдена или устарела — решите задание заново."

    if not errors:
        token = new_entry_token()
        try:
            entry = queue_service.join_queue(
                db,
                session=session,
                full_name=name,
                full_name_key=name_key(name),
                group_name=group,
                purpose_id=purpose.id if purpose else None,
                comment=comment,
                token=token,
                ip_hash=hash_ip(request),
            )
        except queue_service.QueueError as exc:
            errors["queue"] = str(exc)
        else:
            response = RedirectResponse(f"/?new={entry.number}#my", status_code=303)
            _remember(response, token)
            return response

    return _join_form(
        request,
        db,
        session,
        errors=errors,
        values={
            "full_name": name,
            "group_name": group,
            "purpose_id": purpose_id,
            "comment": comment,
        },
        status_code=422,
    )


@router.post("/leave")
def leave_queue(request: Request, db: Session = Depends(get_db)):
    entry = queue_service.entry_by_token(db, read_entry_token(request))
    if entry is not None and entry.is_waiting:
        queue_service.set_status(db, entry, EntryStatus.left)
    return RedirectResponse("/", status_code=303)


# ── Капча ───────────────────────────────────────────────────────────────

@router.get("/api/captcha")
def captcha_new(request: Request, db: Session = Depends(get_db)):
    challenge = issue_challenge(db)
    html = templates.get_template("partials/captcha.html").render(
        {"request": request, "challenge": challenge}
    )
    return JSONResponse({"id": challenge.id, "kind": challenge.kind, "html": html})


@router.post("/api/captcha/{challenge_id}")
def captcha_check(
    challenge_id: str,
    db: Session = Depends(get_db),
    answer: str = Form(""),
):
    return JSONResponse(verify_challenge(db, challenge_id, answer))
