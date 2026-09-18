"""Живая очередь: сеанс приёма, номерки, статусы."""
from __future__ import annotations

import datetime as dt
import hashlib
import json

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.models import EntryStatus, QueueEntry, QueueSession

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


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def format_date(value: dt.date) -> str:
    return f"{value.day} {MONTHS_GEN[value.month - 1]}"


def format_date_full(value: dt.date) -> str:
    return f"{WEEKDAYS[value.weekday()]}, {value.day} {MONTHS_GEN[value.month - 1]} {value.year}"


def to_local(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(settings.tz)


class QueueError(Exception):
    """Действие не выполнено — с человеческим объяснением."""


def _violates(exc: IntegrityError, constraint: str) -> bool:
    """Какое именно ограничение не пустило запись."""
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    if diag is not None and getattr(diag, "constraint_name", None):
        return diag.constraint_name == constraint
    return constraint in str(exc)  # драйвер не дал подробностей — ищем в тексте


# ── Сеанс приёма ────────────────────────────────────────────────────────

def session_ends_at(session: QueueSession | None) -> dt.datetime | None:
    """Момент, когда заканчивается заявленное время приёма.

    Время «до» — это часы без даты, поэтому день берём от открытия приёма.
    Приём через полночь («с 23:50 до 00:30») заканчивается на следующий день.
    Не заполнили «до» — конца по часам нет, закроет только кнопка.
    """
    if session is None or session.time_to is None:
        return None
    opened = to_local(session.opened_at)
    ends_at = dt.datetime.combine(opened.date(), session.time_to, tzinfo=settings.tz)
    if ends_at <= opened:
        ends_at += dt.timedelta(days=1)
    return ends_at


def joining_closed(session: QueueSession | None, now: dt.datetime | None = None) -> bool:
    """Запись закрыта: заявленное время вышло, но приём ещё идёт.

    Те, кто уже в очереди, остаются с номерками — их дообслуживают.
    """
    ends_at = session_ends_at(session)
    return ends_at is not None and (now or now_local()) >= ends_at


def _waiting_count(db: Session, session_id: int) -> int:
    return db.scalar(
        select(func.count(QueueEntry.id)).where(
            QueueEntry.session_id == session_id,
            QueueEntry.status == EntryStatus.waiting,
        )
    ) or 0


def _autoclose_if_finished(db: Session, session: QueueSession) -> QueueSession | None:
    """Время вышло и ждать больше некому — приём уходит в историю сам.

    Планировщика в сервисе нет, поэтому проверяем на каждом чтении очереди:
    страницу всё равно открывают и студенты, и преподаватель. Решение
    принимается под той же блокировкой, что и запись, — пока считаем ждущих,
    никто не успеет встать в очередь.
    """
    if not joining_closed(session):
        return session
    try:
        locked = _lock_open_session(db, session.id)
    except QueueError:
        return None  # закрыли из соседнего запроса, пока мы читали
    if _waiting_count(db, session.id):
        db.rollback()
        return session
    locked.closed_at = utc_now()
    db.commit()
    return None


def current_session(db: Session, *, autoclose: bool = True) -> QueueSession | None:
    """Открытый сеанс. Он всегда один — за этим следит уникальный индекс.

    Заодно закрывает приём, у которого вышло время и опустела очередь.
    С autoclose=False сеанс только читают: так преподаватель успевает
    продлить время «до», а не теряет приём на кнопке «Сохранить».
    """
    session = db.scalar(select(QueueSession).where(QueueSession.closed_at.is_(None)))
    if session is None or not autoclose:
        return session
    return _autoclose_if_finished(db, session)


def _lock_open_session(db: Session, session_id: int) -> QueueSession:
    """Общий замок для записи, выхода и закрытия приёма до конца транзакции."""
    session = db.scalar(
        select(QueueSession)
        .where(QueueSession.id == session_id, QueueSession.closed_at.is_(None))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if session is None:
        db.rollback()
        raise QueueError("Этот приём уже закрыт. Вернитесь на страницу очереди.")
    return session


def session_revision(session: QueueSession | None) -> str:
    """Версия публичной шапки: меняется и при новом приёме, и при правке полей."""
    if session is None:
        return ""
    data = json.dumps(
        [session.id, session.room, session.time_label, session.note, joining_closed(session)]
    )
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


def open_session(
    db: Session,
    *,
    room: str,
    time_from: dt.time | None,
    time_to: dt.time | None,
    note: str,
) -> QueueSession:
    session = QueueSession(
        room=room.strip()[:64],
        time_from=time_from,
        time_to=time_to,
        note=note.strip(),
    )
    db.add(session)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise QueueError(
            "Приём уже открыт. Закройте текущий — и открывайте новый."
        ) from exc
    db.refresh(session)
    return session


def close_session(db: Session, session: QueueSession) -> int:
    """Закрыть приём. Возвращает, сколько человек так и не дождалось."""
    session = _lock_open_session(db, session.id)
    left_waiting = db.scalar(
        select(func.count(QueueEntry.id)).where(
            QueueEntry.session_id == session.id, QueueEntry.status == EntryStatus.waiting
        )
    ) or 0
    session.closed_at = utc_now()
    db.commit()
    return left_waiting


def session_by_id(db: Session, session_id: int) -> QueueSession | None:
    return db.scalar(
        select(QueueSession)
        .options(selectinload(QueueSession.entries).selectinload(QueueEntry.purpose))
        .where(QueueSession.id == session_id)
    )


def past_sessions(db: Session, limit: int = 200) -> list[tuple[QueueSession, int, int]]:
    """История: сеанс, сколько всего записалось, сколько принято."""
    rows = db.execute(
        select(
            QueueSession,
            func.count(QueueEntry.id),
            func.count(QueueEntry.id).filter(QueueEntry.status == EntryStatus.done),
        )
        .outerjoin(QueueEntry, QueueEntry.session_id == QueueSession.id)
        .group_by(QueueSession.id)
        .order_by(QueueSession.opened_at.desc())
        .limit(limit)
    ).all()
    return [(row[0], row[1], row[2]) for row in rows]


# ── Записи ──────────────────────────────────────────────────────────────

def entries_of(db: Session, session_id: int) -> list[QueueEntry]:
    return list(
        db.scalars(
            select(QueueEntry)
            .options(selectinload(QueueEntry.purpose))
            .where(QueueEntry.session_id == session_id)
            .order_by(QueueEntry.number)
        ).all()
    )


def waiting_before(entries: list[QueueEntry], entry: QueueEntry) -> int:
    """Сколько человек ещё ждёт впереди."""
    return sum(1 for e in entries if e.is_waiting and e.number < entry.number)


def _namesake_key(full_name_key: str, group_name: str, attempt: int) -> str:
    """Ключ для настоящего однофамильца — его подтвердил преподаватель."""
    suffix = f"|{group_name.lower()}" + (f"|{attempt}" if attempt > 1 else "")
    return (full_name_key + suffix)[:160]


def _already_joined_message(
    db: Session, session_id: int, full_name_key: str, full_name: str
) -> str:
    """Отказ по делу: человек должен понять, что именно ему делать дальше."""
    existing = db.scalar(
        select(QueueEntry).where(
            QueueEntry.session_id == session_id,
            QueueEntry.full_name_key == full_name_key,
        )
    )
    if existing is not None and existing.is_waiting:
        return (
            f"{full_name} уже стоит в этой очереди под №{existing.number}. "
            "Дважды занимать место нечестно по отношению к тем, кто ждёт."
        )
    was = f" — №{existing.number}, {existing.status.label.lower()}" if existing else ""
    return (
        f"{full_name} уже записывался на этот приём{was}. Второй раз за один "
        "приём встать нельзя — подойдите к преподавателю, он вернёт вас в очередь."
    )


def join_queue(
    db: Session,
    *,
    session: QueueSession,
    full_name: str,
    full_name_key: str,
    group_name: str,
    purpose_id: int | None,
    comment: str,
    token: str,
    ip_hash: str = "",
    added_by_admin: bool = False,
    allow_namesake: bool = False,
) -> QueueEntry:
    """Встать в очередь. Номер выдаётся следующим по порядку.

    Не пустить запись могут два ограничения базы, и ведём мы себя по-разному:

      * `uq_entry_number` — страховка от записи в обход блокировки сеанса.
      * `uq_entry_person` — этот человек в приёме уже есть. Студенту отказ,
        а подтверждённому в админке однофамильцу дописываем к ключу группу.
    """
    last_error: IntegrityError | None = None
    session_id = session.id
    key = full_name_key
    namesakes = 0

    for _ in range(8):
        # После rollback блокировка потеряна: на каждой попытке берём её
        # заново и читаем актуальный closed_at, а не объект из identity map.
        _lock_open_session(db, session_id)
        number = (
            db.scalar(
                select(func.max(QueueEntry.number)).where(
                    QueueEntry.session_id == session_id
                )
            )
            or 0
        ) + 1

        entry = QueueEntry(
            session_id=session_id,
            number=number,
            full_name=full_name,
            full_name_key=key,
            group_name=group_name,
            purpose_id=purpose_id,
            comment=comment,
            token=token,
            ip_hash=ip_hash,
            added_by_admin=added_by_admin,
        )
        db.add(entry)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            if _violates(exc, "uq_entry_person"):
                if not allow_namesake:
                    raise QueueError(
                        _already_joined_message(db, session_id, key, full_name)
                    ) from exc
                namesakes += 1
                key = _namesake_key(full_name_key, group_name, namesakes)
            last_error = exc
            continue
        db.refresh(entry)
        return entry

    raise QueueError(
        "Очередь в этот момент трогали сразу несколько человек. "
        "Нажмите «Встать в очередь» ещё раз."
    ) from last_error


def entry_by_token(db: Session, token: str) -> QueueEntry | None:
    if not token:
        return None
    return db.scalar(
        select(QueueEntry)
        .options(
            selectinload(QueueEntry.purpose),
            selectinload(QueueEntry.session),
        )
        .where(QueueEntry.token == token)
    )


def set_status(db: Session, entry: QueueEntry, status: EntryStatus) -> None:
    entry.status = status
    entry.closed_at = None if status is EntryStatus.waiting else utc_now()
    db.commit()


def leave_queue(db: Session, token: str) -> bool:
    """Студент может выйти только из открытого приёма и только пока ожидает."""
    entry = entry_by_token(db, token)
    if entry is None:
        return False
    try:
        _lock_open_session(db, entry.session_id)
    except QueueError:
        return False
    # Преподаватель мог уже отметить запись; не затираем его отметку.
    changed = db.scalar(
        update(QueueEntry)
        .where(QueueEntry.id == entry.id, QueueEntry.status == EntryStatus.waiting)
        .values(status=EntryStatus.left, closed_at=utc_now())
        .returning(QueueEntry.id)
    )
    db.commit()
    return changed is not None


def known_groups(db: Session, limit: int = 60) -> list[str]:
    rows = db.execute(
        select(QueueEntry.group_name, func.count(QueueEntry.id))
        .group_by(QueueEntry.group_name)
        .order_by(func.count(QueueEntry.id).desc())
        .limit(limit)
    ).all()
    return [row[0] for row in rows]


def counters(entries: list[QueueEntry]) -> dict[str, int]:
    return {
        "total": len(entries),
        "waiting": sum(1 for e in entries if e.status is EntryStatus.waiting),
        "done": sum(1 for e in entries if e.status is EntryStatus.done),
        "no_show": sum(1 for e in entries if e.status is EntryStatus.no_show),
        "left": sum(1 for e in entries if e.status is EntryStatus.left),
    }


def session_title(session: QueueSession) -> str:
    opened = to_local(session.opened_at)
    return f"Приём {format_date_full(opened.date())}, с {opened:%H:%M}"
