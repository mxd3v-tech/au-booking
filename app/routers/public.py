"""Студенческая часть: расписание, бронь, талон, капча."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.captcha import consume_challenge, issue_challenge, verify_challenge
from app.config import settings
from app.db import get_db
from app.models import Booking, BookingStatus, Purpose, Slot
from app.security import (
    BOOKINGS_COOKIE,
    BOOKINGS_COOKIE_MAX_AGE,
    clean_full_name,
    clean_group,
    hash_ip,
    name_key,
    new_cancel_token,
    read_remembered,
    validate_full_name,
    validate_group,
    write_remembered,
)
from app.services import slots as slot_service
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


def _my_bookings(request: Request, db: Session) -> list[Booking]:
    tokens = read_remembered(request)
    if not tokens:
        return []
    found = db.scalars(
        select(Booking)
        .options(
            selectinload(Booking.slot).selectinload(Slot.day),
            selectinload(Booking.purpose),
        )
        .where(Booking.cancel_token.in_(tokens))
    ).all()
    now = slot_service.now_local()
    upcoming = [
        b
        for b in found
        if b.status == BookingStatus.booked and slot_service.to_local(b.slot.ends_at) > now
    ]
    upcoming.sort(key=lambda b: b.slot.starts_at)
    return upcoming


def _remember(response: Response, request: Request, token: str) -> None:
    tokens = read_remembered(request)
    if token not in tokens:
        tokens.append(token)
    response.set_cookie(
        BOOKINGS_COOKIE,
        write_remembered(tokens),
        max_age=BOOKINGS_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )


# ── Расписание ──────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    days = slot_service.upcoming_days(db)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "days": days,
            "my_bookings": _my_bookings(request, db),
            "purposes": _active_purposes(db),
        },
    )


@router.get("/d/{day_date}", response_class=HTMLResponse)
def day_page(day_date: str, request: Request, db: Session = Depends(get_db)):
    try:
        parsed = dt.date.fromisoformat(day_date)
    except ValueError:
        return RedirectResponse("/", status_code=303)

    day = slot_service.get_day(db, parsed)
    if day is None or not day.is_published:
        return templates.TemplateResponse(
            request, "day_missing.html", {"day_date": parsed}, status_code=404
        )

    views = slot_service.day_slot_views(db, day)
    return templates.TemplateResponse(
        request,
        "day.html",
        {
            "day": day,
            "views": views,
            "free_count": sum(1 for v in views if v.is_free),
            "my_bookings": _my_bookings(request, db),
        },
    )


# ── Бронь ───────────────────────────────────────────────────────────────

def _load_slot(db: Session, slot_id: int) -> Slot | None:
    return db.scalar(
        select(Slot)
        .options(selectinload(Slot.bookings), selectinload(Slot.day))
        .where(Slot.id == slot_id)
    )


def _booking_form(
    request: Request,
    db: Session,
    slot: Slot,
    *,
    errors: dict[str, str] | None = None,
    values: dict[str, str] | None = None,
    status_code: int = 200,
):
    challenge = issue_challenge(db)
    return templates.TemplateResponse(
        request,
        "book.html",
        {
            "slot": slot,
            "day": slot.day,
            "starts_at": slot_service.to_local(slot.starts_at),
            "ends_at": slot_service.to_local(slot.ends_at),
            "purposes": _active_purposes(db),
            "groups": slot_service.known_groups(db),
            "challenge": challenge,
            "errors": errors or {},
            "values": values or {},
        },
        status_code=status_code,
    )


@router.get("/book/{slot_id}", response_class=HTMLResponse)
def book_form(slot_id: int, request: Request, db: Session = Depends(get_db)):
    slot = _load_slot(db, slot_id)
    if slot is None or not slot.day.is_published:
        return RedirectResponse("/", status_code=303)

    state = slot_service.slot_state(slot, slot_service.now_local())
    if state != "free":
        return templates.TemplateResponse(
            request,
            "slot_unavailable.html",
            {
                "day": slot.day,
                "starts_at": slot_service.to_local(slot.starts_at),
                "state": state,
            },
            status_code=409,
        )
    return _booking_form(request, db, slot)


@router.post("/book/{slot_id}")
def book_submit(
    slot_id: int,
    request: Request,
    db: Session = Depends(get_db),
    full_name: str = Form(""),
    group_name: str = Form(""),
    purpose_id: str = Form(""),
    comment: str = Form(""),
    captcha_id: str = Form(""),
):
    slot = _load_slot(db, slot_id)
    if slot is None or not slot.day.is_published:
        return RedirectResponse("/", status_code=303)

    if slot_service.slot_state(slot, slot_service.now_local()) != "free":
        return templates.TemplateResponse(
            request,
            "slot_unavailable.html",
            {
                "day": slot.day,
                "starts_at": slot_service.to_local(slot.starts_at),
                "state": slot_service.slot_state(slot, slot_service.now_local()),
            },
            status_code=409,
        )

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
        key = name_key(name)
        active = slot_service.active_bookings_for(db, key, group)
        if len(active) >= settings.max_active_bookings:
            existing = active[0]
            return templates.TemplateResponse(
                request,
                "already_booked.html",
                {
                    "booking": existing,
                    "starts_at": slot_service.to_local(existing.slot.starts_at),
                    "day": existing.slot.day,
                },
                status_code=409,
            )

        try:
            booking = slot_service.create_booking(
                db,
                slot=slot,
                full_name=name,
                full_name_key=key,
                group_name=group,
                purpose_id=purpose.id if purpose else None,
                comment=comment,
                cancel_token=new_cancel_token(),
                ip_hash=hash_ip(request),
            )
        except slot_service.BookingError as exc:
            errors["slot"] = str(exc)
        else:
            response = RedirectResponse(f"/b/{booking.cancel_token}?new=1", status_code=303)
            _remember(response, request, booking.cancel_token)
            return response

    return _booking_form(
        request,
        db,
        slot,
        errors=errors,
        values={
            "full_name": name,
            "group_name": group,
            "purpose_id": purpose_id,
            "comment": comment,
        },
        status_code=422,
    )


# ── Талон ───────────────────────────────────────────────────────────────

@router.get("/b/{token}", response_class=HTMLResponse)
def ticket(token: str, request: Request, new: str = "", db: Session = Depends(get_db)):
    booking = slot_service.find_by_token(db, token)
    if booking is None:
        return templates.TemplateResponse(request, "ticket_missing.html", {}, status_code=404)

    starts_at = slot_service.to_local(booking.slot.starts_at)
    now = slot_service.now_local()
    response = templates.TemplateResponse(
        request,
        "ticket.html",
        {
            "booking": booking,
            "day": booking.slot.day,
            "starts_at": starts_at,
            "ends_at": slot_service.to_local(booking.slot.ends_at),
            "is_new": new == "1",
            "can_cancel": booking.status == BookingStatus.booked and starts_at > now,
            "is_past": starts_at <= now,
        },
    )
    if booking.status == BookingStatus.booked:
        _remember(response, request, booking.cancel_token)
    return response


@router.post("/b/{token}/cancel")
def ticket_cancel(token: str, request: Request, db: Session = Depends(get_db)):
    booking = slot_service.find_by_token(db, token)
    if booking is None:
        return RedirectResponse("/", status_code=303)
    if booking.status == BookingStatus.booked:
        slot_service.cancel_booking(db, booking)
    return RedirectResponse(f"/b/{token}", status_code=303)


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
