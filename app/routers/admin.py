"""Админ-панель преподавателя: дни, окна, брони, справочники, выгрузки."""
from __future__ import annotations

import datetime as dt
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, selectinload

from app.captcha import KINDS, issue_challenge
from app.db import get_db
from app.models import (
    Booking,
    BookingStatus,
    Purpose,
    ReceptionDay,
    ReceptionWindow,
    Slot,
)
from app.security import (
    ADMIN_COOKIE,
    ADMIN_SESSION_MAX_AGE,
    check_admin_credentials,
    make_admin_token,
    require_admin,
)
from app.services import export as export_service
from app.services import slots as slot_service
from app.templating import templates

router = APIRouter(prefix="/admin")
guard = Depends(require_admin)


def _back(url: str, ok: str = "", err: str = "") -> RedirectResponse:
    parts = []
    if ok:
        parts.append(f"ok={quote(ok)}")
    if err:
        parts.append(f"err={quote(err)}")
    suffix = ("&" if "?" in url else "?") + "&".join(parts) if parts else ""
    return RedirectResponse(url + suffix, status_code=303)


def _parse_time(raw: str) -> dt.time | None:
    try:
        return dt.time.fromisoformat((raw or "").strip())
    except ValueError:
        return None


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
        samesite="lax",
        path="/",
    )
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(ADMIN_COOKIE, path="/")
    return response


# ── Сводка ──────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse, dependencies=[guard])
@router.get("/", response_class=HTMLResponse, dependencies=[guard])
def dashboard(request: Request, db: Session = Depends(get_db)):
    today = slot_service.today_local()
    summaries = slot_service.upcoming_days(db, include_unpublished=True)

    today_bookings = list(
        db.scalars(
            select(Booking)
            .join(Slot, Booking.slot_id == Slot.id)
            .join(ReceptionDay, Slot.day_id == ReceptionDay.id)
            .options(
                selectinload(Booking.slot).selectinload(Slot.day),
                selectinload(Booking.purpose),
            )
            .where(ReceptionDay.date == today, Booking.status != BookingStatus.cancelled)
            .order_by(Slot.starts_at)
        ).all()
    )

    totals = db.execute(
        select(Booking.status, func.count(Booking.id)).group_by(Booking.status)
    ).all()

    return templates.TemplateResponse(
        request,
        "admin/dashboard.html",
        {
            "summaries": summaries,
            "today": today,
            "today_bookings": today_bookings,
            "totals": {BookingStatus(s).label: n for s, n in totals},
            "purposes_count": db.scalar(select(func.count(Purpose.id))) or 0,
        },
    )


# ── Дни приёма ──────────────────────────────────────────────────────────

@router.get("/days", response_class=HTMLResponse, dependencies=[guard])
def days_list(request: Request, db: Session = Depends(get_db)):
    days = list(
        db.scalars(
            select(ReceptionDay)
            .options(selectinload(ReceptionDay.windows))
            .order_by(ReceptionDay.date.desc())
        ).all()
    )
    counts = dict(
        db.execute(
            select(Slot.day_id, func.count(Slot.id)).group_by(Slot.day_id)
        ).all()
    )
    booked = dict(
        db.execute(
            select(Slot.day_id, func.count(Booking.id))
            .join(Booking, Booking.slot_id == Slot.id)
            .where(Booking.status != BookingStatus.cancelled)
            .group_by(Slot.day_id)
        ).all()
    )
    return templates.TemplateResponse(
        request,
        "admin/days.html",
        {
            "days": days,
            "counts": counts,
            "booked": booked,
            "today": slot_service.today_local(),
            "ok": request.query_params.get("ok", ""),
            "err": request.query_params.get("err", ""),
            "default_date": slot_service.today_local().isoformat(),
        },
    )


@router.post("/days", dependencies=[guard])
def day_create(
    db: Session = Depends(get_db),
    date: str = Form(""),
    room: str = Form(""),
    note: str = Form(""),
    start_time: str = Form(""),
    end_time: str = Form(""),
    slot_minutes: int = Form(5),
):
    try:
        parsed = dt.date.fromisoformat(date.strip())
    except ValueError:
        return _back("/admin/days", err="Дата указана неверно.")

    if slot_service.get_day(db, parsed) is not None:
        return _back("/admin/days", err=f"День {parsed:%d.%m.%Y} уже заведён.")

    day = ReceptionDay(date=parsed, room=room.strip()[:64], note=note.strip())
    db.add(day)
    db.flush()

    start = _parse_time(start_time)
    end = _parse_time(end_time)
    if start and end:
        window = ReceptionWindow(
            day_id=day.id, start_time=start, end_time=end, slot_minutes=max(slot_minutes, 1)
        )
        window.day = day
        db.add(window)
        db.flush()
        try:
            slot_service.rebuild_window_slots(db, window)
        except slot_service.WindowError as exc:
            db.rollback()
            return _back("/admin/days", err=str(exc))

    db.commit()
    return _back(f"/admin/days/{day.id}", ok=f"День {parsed:%d.%m.%Y} создан.")


@router.get("/days/{day_id}", response_class=HTMLResponse, dependencies=[guard])
def day_detail(day_id: int, request: Request, db: Session = Depends(get_db)):
    day = db.get(ReceptionDay, day_id)
    if day is None:
        return _back("/admin/days", err="День не найден.")

    views = slot_service.day_slot_views(db, day)
    bookings = [v.slot.active_booking for v in views if v.slot.active_booking is not None]

    return templates.TemplateResponse(
        request,
        "admin/day.html",
        {
            "day": day,
            "views": views,
            "bookings": bookings,
            "free_count": sum(1 for v in views if v.is_free),
            "ok": request.query_params.get("ok", ""),
            "err": request.query_params.get("err", ""),
        },
    )


@router.post("/days/{day_id}/update", dependencies=[guard])
def day_update(
    day_id: int,
    db: Session = Depends(get_db),
    room: str = Form(""),
    note: str = Form(""),
    is_published: str = Form(""),
):
    day = db.get(ReceptionDay, day_id)
    if day is None:
        return _back("/admin/days", err="День не найден.")
    day.room = room.strip()[:64]
    day.note = note.strip()
    day.is_published = is_published == "on"
    db.commit()
    return _back(f"/admin/days/{day_id}", ok="Сохранено.")


@router.post("/days/{day_id}/delete", dependencies=[guard])
def day_delete(day_id: int, db: Session = Depends(get_db)):
    day = db.get(ReceptionDay, day_id)
    if day is None:
        return _back("/admin/days", err="День не найден.")

    active = db.scalar(
        select(func.count(Booking.id))
        .join(Slot, Booking.slot_id == Slot.id)
        .where(Slot.day_id == day_id, Booking.status != BookingStatus.cancelled)
    )
    if active:
        return _back(
            f"/admin/days/{day_id}",
            err=f"В этом дне {active} активных броней. Снимите день с публикации "
            "или отмените брони — так студенты не окажутся с талоном в никуда.",
        )

    label = f"{day.date:%d.%m.%Y}"
    db.delete(day)
    db.commit()
    return _back("/admin/days", ok=f"День {label} удалён.")


# ── Окна приёма ─────────────────────────────────────────────────────────

@router.post("/days/{day_id}/windows", dependencies=[guard])
def window_create(
    day_id: int,
    db: Session = Depends(get_db),
    start_time: str = Form(""),
    end_time: str = Form(""),
    slot_minutes: int = Form(5),
    title: str = Form(""),
):
    day = db.get(ReceptionDay, day_id)
    if day is None:
        return _back("/admin/days", err="День не найден.")

    start, end = _parse_time(start_time), _parse_time(end_time)
    if start is None or end is None:
        return _back(f"/admin/days/{day_id}", err="Время окна указано неверно.")

    window = ReceptionWindow(
        day_id=day.id,
        start_time=start,
        end_time=end,
        slot_minutes=max(int(slot_minutes), 1),
        title=title.strip()[:120],
    )
    window.day = day
    db.add(window)
    db.flush()
    try:
        added, _ = slot_service.rebuild_window_slots(db, window)
    except slot_service.WindowError as exc:
        db.rollback()
        return _back(f"/admin/days/{day_id}", err=str(exc))

    db.commit()
    return _back(f"/admin/days/{day_id}", ok=f"Окно добавлено, слотов: {added}.")


@router.post("/windows/{window_id}/update", dependencies=[guard])
def window_update(
    window_id: int,
    db: Session = Depends(get_db),
    start_time: str = Form(""),
    end_time: str = Form(""),
    slot_minutes: int = Form(5),
    title: str = Form(""),
):
    window = db.get(ReceptionWindow, window_id)
    if window is None:
        return _back("/admin/days", err="Окно не найдено.")

    start, end = _parse_time(start_time), _parse_time(end_time)
    if start is None or end is None:
        return _back(f"/admin/days/{window.day_id}", err="Время окна указано неверно.")

    window.start_time = start
    window.end_time = end
    window.slot_minutes = max(int(slot_minutes), 1)
    window.title = title.strip()[:120]
    db.flush()

    try:
        added, removed = slot_service.rebuild_window_slots(db, window)
    except slot_service.WindowError as exc:
        db.rollback()
        return _back(f"/admin/days/{window.day_id}", err=str(exc))

    db.commit()
    return _back(
        f"/admin/days/{window.day_id}",
        ok=f"Окно обновлено: добавлено {added}, убрано {removed}. "
        "Слоты с бронями сохранены.",
    )


@router.post("/windows/{window_id}/delete", dependencies=[guard])
def window_delete(window_id: int, db: Session = Depends(get_db)):
    window = db.get(ReceptionWindow, window_id)
    if window is None:
        return _back("/admin/days", err="Окно не найдено.")
    day_id = window.day_id

    active = db.scalar(
        select(func.count(Booking.id))
        .join(Slot, Booking.slot_id == Slot.id)
        .where(Slot.window_id == window_id, Booking.status != BookingStatus.cancelled)
    )
    if active:
        return _back(
            f"/admin/days/{day_id}",
            err=f"В окне {active} активных броней — сначала разберитесь с ними.",
        )

    db.delete(window)
    db.commit()
    return _back(f"/admin/days/{day_id}", ok="Окно удалено.")


@router.post("/slots/{slot_id}/toggle", dependencies=[guard])
def slot_toggle(slot_id: int, db: Session = Depends(get_db)):
    slot = db.scalar(
        select(Slot).options(selectinload(Slot.bookings)).where(Slot.id == slot_id)
    )
    if slot is None:
        return _back("/admin/days", err="Слот не найден.")
    if slot.active_booking is not None:
        return _back(f"/admin/days/{slot.day_id}", err="Слот занят — сначала отмените бронь.")
    slot.is_blocked = not slot.is_blocked
    db.commit()
    state = "закрыт" if slot.is_blocked else "открыт"
    return _back(f"/admin/days/{slot.day_id}", ok=f"Слот {state}.")


# ── Брони ───────────────────────────────────────────────────────────────

@router.get("/bookings", response_class=HTMLResponse, dependencies=[guard])
def bookings_list(
    request: Request,
    db: Session = Depends(get_db),
    group: str = "",
    status: str = "",
    day: str = "",
    q: str = "",
):
    query = (
        select(Booking)
        .join(Slot, Booking.slot_id == Slot.id)
        .join(ReceptionDay, Slot.day_id == ReceptionDay.id)
        .options(
            selectinload(Booking.slot).selectinload(Slot.day),
            selectinload(Booking.purpose),
        )
        .order_by(Slot.starts_at.desc())
    )
    if group:
        query = query.where(Booking.group_name == group.strip().upper())
    if status in {s.value for s in BookingStatus}:
        query = query.where(Booking.status == BookingStatus(status))
    if day:
        try:
            query = query.where(ReceptionDay.date == dt.date.fromisoformat(day))
        except ValueError:
            pass
    if q:
        query = query.where(Booking.full_name.ilike(f"%{q.strip()}%"))

    bookings = list(db.scalars(query.limit(500)).all())
    return templates.TemplateResponse(
        request,
        "admin/bookings.html",
        {
            "bookings": bookings,
            "groups": slot_service.known_groups(db),
            "statuses": list(BookingStatus),
            "filters": {"group": group, "status": status, "day": day, "q": q},
            "ok": request.query_params.get("ok", ""),
            "err": request.query_params.get("err", ""),
        },
    )


@router.post("/bookings/{booking_id}/status", dependencies=[guard])
def booking_status(
    booking_id: int,
    request: Request,
    db: Session = Depends(get_db),
    status: str = Form(""),
    back: str = Form("/admin/bookings"),
):
    booking = db.get(Booking, booking_id)
    if booking is None:
        return _back(back, err="Бронь не найдена.")
    if status not in {s.value for s in BookingStatus}:
        return _back(back, err="Неизвестный статус.")

    booking.status = BookingStatus(status)
    if booking.status == BookingStatus.cancelled:
        booking.cancelled_at = dt.datetime.now(dt.timezone.utc)
    db.commit()
    return _back(back, ok=f"{booking.full_name}: {BookingStatus(status).label.lower()}.")


@router.post("/bookings/{booking_id}/delete", dependencies=[guard])
def booking_delete(
    booking_id: int,
    db: Session = Depends(get_db),
    back: str = Form("/admin/bookings"),
):
    booking = db.get(Booking, booking_id)
    if booking is None:
        return _back(back, err="Бронь не найдена.")
    name = booking.full_name
    db.delete(booking)
    db.commit()
    return _back(back, ok=f"Запись «{name}» удалена без следа.")


# ── Выгрузки и печать ───────────────────────────────────────────────────

def _day_bookings(db: Session, day: ReceptionDay) -> list[Booking]:
    return list(
        db.scalars(
            select(Booking)
            .join(Slot, Booking.slot_id == Slot.id)
            .options(selectinload(Booking.slot), selectinload(Booking.purpose))
            .where(Slot.day_id == day.id, Booking.status != BookingStatus.cancelled)
            .order_by(Slot.starts_at)
        ).all()
    )


@router.get("/days/{day_id}/export.csv", dependencies=[guard])
def export_csv(day_id: int, db: Session = Depends(get_db)):
    day = db.get(ReceptionDay, day_id)
    if day is None:
        return _back("/admin/days", err="День не найден.")
    title = f"Приём {slot_service.format_date_full(day.date)}"
    data = export_service.to_csv(_day_bookings(db, day), title)
    return Response(
        data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="priem-{day.date}.csv"'},
    )


@router.get("/days/{day_id}/export.xlsx", dependencies=[guard])
def export_xlsx(day_id: int, db: Session = Depends(get_db)):
    day = db.get(ReceptionDay, day_id)
    if day is None:
        return _back("/admin/days", err="День не найден.")
    title = f"Приём {slot_service.format_date_full(day.date)}"
    data = export_service.to_xlsx(_day_bookings(db, day), title)
    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="priem-{day.date}.xlsx"'},
    )


@router.get("/days/{day_id}/print", response_class=HTMLResponse, dependencies=[guard])
def print_list(day_id: int, request: Request, db: Session = Depends(get_db)):
    day = db.get(ReceptionDay, day_id)
    if day is None:
        return _back("/admin/days", err="День не найден.")
    return templates.TemplateResponse(
        request,
        "admin/print.html",
        {"day": day, "bookings": _day_bookings(db, day)},
    )


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
    db.execute(
        delete(Purpose).where(Purpose.id == purpose_id)
    )
    db.commit()
    return _back("/admin/purposes", ok="Пункт удалён, у прежних броней он просто опустеет.")


# ── Просмотр капчи ──────────────────────────────────────────────────────

@router.get("/captcha", response_class=HTMLResponse, dependencies=[guard])
def captcha_preview(request: Request, kind: str = "", db: Session = Depends(get_db)):
    challenge = issue_challenge(db, kind if kind in KINDS else None)
    return templates.TemplateResponse(
        request,
        "admin/captcha.html",
        {"challenge": challenge, "kinds": KINDS, "current_kind": challenge.kind},
    )
