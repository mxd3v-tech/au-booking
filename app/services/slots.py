"""Расписание: нарезка окон на слоты, доступность, создание брони."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.models import (
    Booking,
    BookingStatus,
    ReceptionDay,
    ReceptionWindow,
    Slot,
)

MAX_SLOTS_PER_WINDOW = 400

WEEKDAYS = [
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
]
MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def now_local() -> dt.datetime:
    return dt.datetime.now(settings.tz)


def today_local() -> dt.date:
    return now_local().date()


def format_date(value: dt.date) -> str:
    return f"{value.day} {MONTHS_GEN[value.month - 1]}"


def format_date_full(value: dt.date) -> str:
    return f"{WEEKDAYS[value.weekday()]}, {value.day} {MONTHS_GEN[value.month - 1]} {value.year}"


def to_local(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(settings.tz)


# ── Нарезка окна на слоты ───────────────────────────────────────────────

class WindowError(ValueError):
    """Окно задано так, что нарезать его нельзя."""


def planned_slot_starts(day: ReceptionDay, window: ReceptionWindow) -> list[dt.datetime]:
    if window.slot_minutes < 1:
        raise WindowError("Длительность приёма должна быть хотя бы одну минуту.")
    if window.end_time <= window.start_time:
        raise WindowError("Конец окна должен быть позже начала.")

    start = dt.datetime.combine(day.date, window.start_time, tzinfo=settings.tz)
    end = dt.datetime.combine(day.date, window.end_time, tzinfo=settings.tz)
    step = dt.timedelta(minutes=window.slot_minutes)

    starts: list[dt.datetime] = []
    cursor = start
    while cursor + step <= end:
        starts.append(cursor)
        cursor += step
        if len(starts) > MAX_SLOTS_PER_WINDOW:
            raise WindowError(
                f"Окно даёт больше {MAX_SLOTS_PER_WINDOW} слотов — "
                "увеличьте длительность приёма."
            )
    if not starts:
        raise WindowError("Окно короче одного слота — записывать некуда.")
    return starts


def rebuild_window_slots(db: Session, window: ReceptionWindow) -> tuple[int, int]:
    """Пересобрать слоты окна.

    Слоты с активной бронью не трогаются: студенту, который уже записан,
    время менять нельзя. Возвращает (добавлено, удалено).
    """
    day = window.day
    planned = planned_slot_starts(day, window)
    planned_set = {s for s in planned}

    existing = db.scalars(
        select(Slot).options(selectinload(Slot.bookings)).where(Slot.window_id == window.id)
    ).all()
    existing_by_start = {to_local(s.starts_at): s for s in existing}

    removed = 0
    for start, slot in existing_by_start.items():
        if start in planned_set:
            continue
        if slot.active_booking is not None:
            continue  # занятый слот сохраняем, даже если окно сдвинули
        db.delete(slot)
        removed += 1

    added = 0
    step = dt.timedelta(minutes=window.slot_minutes)
    for start in planned:
        if start in existing_by_start:
            continue
        db.add(
            Slot(
                day_id=day.id,
                window_id=window.id,
                starts_at=start,
                ends_at=start + step,
            )
        )
        added += 1

    db.flush()
    return added, removed


# ── Представление расписания ────────────────────────────────────────────

@dataclass
class SlotView:
    slot: Slot
    state: str  # free | taken | blocked | closed
    starts_at: dt.datetime
    ends_at: dt.datetime

    @property
    def time_label(self) -> str:
        return self.starts_at.strftime("%H:%M")

    @property
    def range_label(self) -> str:
        return f"{self.starts_at:%H:%M}–{self.ends_at:%H:%M}"

    @property
    def is_free(self) -> bool:
        return self.state == "free"

    @property
    def state_label(self) -> str:
        return {
            "free": "Свободно",
            "taken": "Занято",
            "blocked": "Закрыто",
            "closed": "Время прошло",
        }[self.state]


def booking_deadline(slot_starts_at: dt.datetime) -> dt.datetime:
    return slot_starts_at - dt.timedelta(minutes=settings.booking_lead_minutes)


def slot_state(slot: Slot, now: dt.datetime) -> str:
    if slot.active_booking is not None:
        return "taken"
    if slot.is_blocked:
        return "blocked"
    if booking_deadline(to_local(slot.starts_at)) <= now:
        return "closed"
    return "free"


def day_slot_views(db: Session, day: ReceptionDay, now: dt.datetime | None = None) -> list[SlotView]:
    now = now or now_local()
    slots = db.scalars(
        select(Slot)
        .options(selectinload(Slot.bookings))
        .where(Slot.day_id == day.id)
        .order_by(Slot.starts_at)
    ).all()
    return [
        SlotView(
            slot=slot,
            state=slot_state(slot, now),
            starts_at=to_local(slot.starts_at),
            ends_at=to_local(slot.ends_at),
        )
        for slot in slots
    ]


@dataclass
class DaySummary:
    day: ReceptionDay
    total: int
    free: int

    @property
    def taken(self) -> int:
        return self.total - self.free

    @property
    def has_free(self) -> bool:
        return self.free > 0


def upcoming_days(
    db: Session, *, include_unpublished: bool = False, limit_days: int | None = None
) -> list[DaySummary]:
    now = now_local()
    horizon = now.date() + dt.timedelta(days=limit_days or settings.days_ahead)

    query = (
        select(ReceptionDay)
        .where(ReceptionDay.date >= now.date(), ReceptionDay.date <= horizon)
        .order_by(ReceptionDay.date)
    )
    if not include_unpublished:
        query = query.where(ReceptionDay.is_published.is_(True))

    days = db.scalars(query).all()
    summaries: list[DaySummary] = []
    for day in days:
        views = day_slot_views(db, day, now)
        if not views:
            continue
        summaries.append(
            DaySummary(day=day, total=len(views), free=sum(1 for v in views if v.is_free))
        )
    return summaries


def get_day(db: Session, day_date: dt.date) -> ReceptionDay | None:
    return db.scalar(select(ReceptionDay).where(ReceptionDay.date == day_date))


# ── Брони ───────────────────────────────────────────────────────────────

class BookingError(Exception):
    """Не получилось создать бронь — с человеческим объяснением."""


def active_bookings_for(db: Session, key: str, group: str) -> list[Booking]:
    now = now_local()
    return list(
        db.scalars(
            select(Booking)
            .join(Slot, Booking.slot_id == Slot.id)
            .options(selectinload(Booking.slot).selectinload(Slot.day))
            .where(
                Booking.full_name_key == key,
                Booking.group_name == group,
                Booking.status == BookingStatus.booked,
                Slot.ends_at > now,
            )
            .order_by(Slot.starts_at)
        ).all()
    )


def create_booking(
    db: Session,
    *,
    slot: Slot,
    full_name: str,
    full_name_key: str,
    group_name: str,
    purpose_id: int | None,
    comment: str,
    cancel_token: str,
    ip_hash: str,
) -> Booking:
    booking = Booking(
        slot_id=slot.id,
        full_name=full_name,
        full_name_key=full_name_key,
        group_name=group_name,
        purpose_id=purpose_id,
        comment=comment,
        status=BookingStatus.booked,
        cancel_token=cancel_token,
        ip_hash=ip_hash,
    )
    db.add(booking)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise BookingError(
            "Этот слот заняли за секунду до вас. Выберите другое время — "
            "свободные уже подсвечены."
        ) from exc
    db.refresh(booking)
    return booking


def cancel_booking(db: Session, booking: Booking) -> None:
    booking.status = BookingStatus.cancelled
    booking.cancelled_at = dt.datetime.now(dt.timezone.utc)
    db.commit()


def find_by_token(db: Session, token: str) -> Booking | None:
    if not token:
        return None
    return db.scalar(
        select(Booking)
        .options(
            selectinload(Booking.slot).selectinload(Slot.day),
            selectinload(Booking.purpose),
        )
        .where(Booking.cancel_token == token)
    )


def known_groups(db: Session, limit: int = 60) -> list[str]:
    rows = db.execute(
        select(Booking.group_name, func.count(Booking.id).label("n"))
        .group_by(Booking.group_name)
        .order_by(func.count(Booking.id).desc())
        .limit(limit)
    ).all()
    return [row[0] for row in rows]
