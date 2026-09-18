"""Модели данных живой очереди.

Сеанс приёма — это «приём идёт»: открыли, к вам подходят, закрыли.
Записи живут внутри сеанса и нумеруются с единицы.

Две вещи гарантирует сама база, а не аккуратность кода:

  * `uq_session_open` — открытым может быть только один сеанс. Индекс по
    выражению `(closed_at IS NULL)` с условием на то же выражение: у всех
    открытых сеансов ключ индекса одинаковый, поэтому второй не вставится.
  * `uq_entry_person` — один человек может записаться на приём только один раз.
"""
from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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


class EntryStatus(str, enum.Enum):
    waiting = "waiting"    # стоит в очереди
    done = "done"          # принят
    no_show = "no_show"    # не подошёл, когда дошла очередь
    left = "left"          # ушёл сам

    @property
    def label(self) -> str:
        return {
            "waiting": "Ожидает",
            "done": "Принят",
            "no_show": "Не подошёл",
            "left": "Ушёл",
        }[self.value]


_status_type = Enum(
    EntryStatus,
    name="entry_status",
    native_enum=False,
    length=16,
    values_callable=lambda e: [m.value for m in e],
)


class QueueSession(Base):
    """Один приём: открыли — люди записываются, закрыли — список в историю."""

    __tablename__ = "queue_session"
    __table_args__ = (
        Index(
            "uq_session_open",
            text("(closed_at IS NULL)"),
            unique=True,
            postgresql_where=text("closed_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    room: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    time_from: Mapped[dt.time | None] = mapped_column(Time, nullable=True)
    time_to: Mapped[dt.time | None] = mapped_column(Time, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    opened_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    closed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    entries: Mapped[list["QueueEntry"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="QueueEntry.number",
    )

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    @property
    def time_label(self) -> str:
        """«14:00–16:00», «с 14:00», «до 16:00» — смотря что заполнили."""
        if self.time_from and self.time_to:
            return f"{self.time_from:%H:%M}–{self.time_to:%H:%M}"
        if self.time_from:
            return f"с {self.time_from:%H:%M}"
        if self.time_to:
            return f"до {self.time_to:%H:%M}"
        return ""


class QueueEntry(Base):
    """Человек в очереди."""

    __tablename__ = "queue_entry"
    __table_args__ = (
        UniqueConstraint("session_id", "number", name="uq_entry_number"),
        # Одна запись на весь приём. Повторно не встать ни с другого телефона,
        # ни почистив куки, ни после отметки «Принят» — вернуть человека в
        # очередь может только преподаватель из админки.
        #
        # В ключе только ФИО: группу студент пишет сам, и «поправить» её,
        # чтобы записаться второй раз, ничего не стоит. Настоящий однофамилец
        # добавляется вручную из админки — его ключу дописывается группа.
        UniqueConstraint("session_id", "full_name_key", name="uq_entry_person"),
        Index("ix_entry_name_key", "full_name_key"),
        Index("ix_entry_group", "group_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("queue_session.id", ondelete="CASCADE"), nullable=False, index=True
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    full_name: Mapped[str] = mapped_column(String(160), nullable=False)
    full_name_key: Mapped[str] = mapped_column(String(160), nullable=False)
    group_name: Mapped[str] = mapped_column(String(32), nullable=False)
    purpose_id: Mapped[int | None] = mapped_column(
        ForeignKey("purpose.id", ondelete="SET NULL"), nullable=True
    )
    comment: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    status: Mapped[EntryStatus] = mapped_column(
        _status_type, default=EntryStatus.waiting, nullable=False, index=True
    )
    token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    closed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    added_by_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ip_hash: Mapped[str] = mapped_column(String(32), default="", nullable=False)

    session: Mapped[QueueSession] = relationship(back_populates="entries")
    purpose: Mapped["Purpose | None"] = relationship()

    @property
    def is_waiting(self) -> bool:
        return self.status is EntryStatus.waiting


class Purpose(Base):
    """Справочник целей визита — редактируется в админке."""

    __tablename__ = "purpose"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    hint: Mapped[str] = mapped_column(String(240), default="", nullable=False)
    needs_comment: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)


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
    """Небольшие настройки, редактируемые из админки (например, адрес для QR)."""

    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="", nullable=False)
