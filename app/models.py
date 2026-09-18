"""Модели данных.

Ключевая гарантия от двойной брони — частичный UNIQUE-индекс
`uq_booking_active_slot`: в БД физически не может существовать двух
неотменённых броней на один слот, как бы одновременно ни нажали кнопку.
"""
from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class BookingStatus(str, enum.Enum):
    booked = "booked"        # ожидает
    done = "done"            # принят
    no_show = "no_show"      # неявка
    cancelled = "cancelled"  # отменена

    @property
    def label(self) -> str:
        return {
            "booked": "Ожидает",
            "done": "Принят",
            "no_show": "Неявка",
            "cancelled": "Отменена",
        }[self.value]


ACTIVE_STATUSES = (BookingStatus.booked, BookingStatus.done, BookingStatus.no_show)

_status_type = Enum(
    BookingStatus,
    name="booking_status",
    native_enum=False,
    length=16,
    values_callable=lambda e: [m.value for m in e],
)


class ReceptionDay(Base):
    """День приёма."""

    __tablename__ = "reception_day"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, unique=True, nullable=False)
    room: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    is_published: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    windows: Mapped[list["ReceptionWindow"]] = relationship(
        back_populates="day",
        cascade="all, delete-orphan",
        order_by="ReceptionWindow.start_time",
    )
    slots: Mapped[list["Slot"]] = relationship(
        back_populates="day", cascade="all, delete-orphan", order_by="Slot.starts_at"
    )


class ReceptionWindow(Base):
    """Окно приёма внутри дня: например 14:00–16:00 по 5 минут."""

    __tablename__ = "reception_window"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day_id: Mapped[int] = mapped_column(
        ForeignKey("reception_day.id", ondelete="CASCADE"), nullable=False, index=True
    )
    start_time: Mapped[dt.time] = mapped_column(Time, nullable=False)
    end_time: Mapped[dt.time] = mapped_column(Time, nullable=False)
    slot_minutes: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    title: Mapped[str] = mapped_column(String(120), default="", nullable=False)

    day: Mapped[ReceptionDay] = relationship(back_populates="windows")
    slots: Mapped[list["Slot"]] = relationship(
        back_populates="window", cascade="all, delete-orphan", order_by="Slot.starts_at"
    )


class Slot(Base):
    """Конкретное окошко приёма. Материализуется при сохранении окна."""

    __tablename__ = "slot"
    __table_args__ = (
        UniqueConstraint("day_id", "starts_at", name="uq_slot_day_start"),
        Index("ix_slot_starts_at", "starts_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day_id: Mapped[int] = mapped_column(
        ForeignKey("reception_day.id", ondelete="CASCADE"), nullable=False, index=True
    )
    window_id: Mapped[int] = mapped_column(
        ForeignKey("reception_window.id", ondelete="CASCADE"), nullable=False, index=True
    )
    starts_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    day: Mapped[ReceptionDay] = relationship(back_populates="slots")
    window: Mapped[ReceptionWindow] = relationship(back_populates="slots")
    bookings: Mapped[list["Booking"]] = relationship(back_populates="slot")

    @property
    def active_booking(self) -> "Booking | None":
        for booking in self.bookings:
            if booking.status is not BookingStatus.cancelled:
                return booking
        return None


class Purpose(Base):
    """Справочник целей визита — редактируется в админке."""

    __tablename__ = "purpose"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    hint: Mapped[str] = mapped_column(String(240), default="", nullable=False)
    needs_comment: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)


class Booking(Base):
    """Бронь студента."""

    __tablename__ = "booking"
    __table_args__ = (
        Index(
            "uq_booking_active_slot",
            "slot_id",
            unique=True,
            postgresql_where=text("status <> 'cancelled'"),
        ),
        Index("ix_booking_name_key", "full_name_key"),
        Index("ix_booking_group", "group_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slot_id: Mapped[int] = mapped_column(
        ForeignKey("slot.id", ondelete="CASCADE"), nullable=False, index=True
    )
    full_name: Mapped[str] = mapped_column(String(160), nullable=False)
    full_name_key: Mapped[str] = mapped_column(String(160), nullable=False)
    group_name: Mapped[str] = mapped_column(String(32), nullable=False)
    purpose_id: Mapped[int | None] = mapped_column(
        ForeignKey("purpose.id", ondelete="SET NULL"), nullable=True
    )
    comment: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    status: Mapped[BookingStatus] = mapped_column(
        _status_type, default=BookingStatus.booked, nullable=False, index=True
    )
    cancel_token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    cancelled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ip_hash: Mapped[str] = mapped_column(String(32), default="", nullable=False)

    slot: Mapped[Slot] = relationship(back_populates="bookings")
    purpose: Mapped[Purpose | None] = relationship()

    @property
    def is_active(self) -> bool:
        return self.status is not BookingStatus.cancelled


class CaptchaChallenge(Base):
    """Выданное задание капчи. Правильный ответ живёт только на сервере."""

    __tablename__ = "captcha_challenge"
    __table_args__ = (CheckConstraint("attempts >= 0", name="ck_captcha_attempts"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    answer: Mapped[dict] = mapped_column(JSONB, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    solved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Setting(Base):
    """Небольшие настройки, редактируемые из админки."""

    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="", nullable=False)
