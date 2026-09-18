"""Общий объект шаблонизатора: глобальные значения и фильтры."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.config import settings
from app.models import EntryStatus
from app.services.queue import format_date, format_date_full, now_local, to_local

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _status_label(value) -> str:
    try:
        return EntryStatus(value).label
    except ValueError:
        return str(value)


def _people(count: int) -> str:
    """«1 человек», «2 человека», «5 человек» — без этого фраза выглядит роботом."""
    count = abs(int(count))
    tail, hundred = count % 10, count % 100
    if tail == 1 and hundred != 11:
        return f"{count} человек"
    if 2 <= tail <= 4 and not 12 <= hundred <= 14:
        return f"{count} человека"
    return f"{count} человек"


def _time(value: dt.datetime) -> str:
    return f"{to_local(value):%H:%M}"


def _datetime(value: dt.datetime) -> str:
    return f"{to_local(value):%d.%m.%Y %H:%M}"


templates.env.globals.update(
    settings=settings,
    teacher_name=settings.teacher_name,
    teacher_title=settings.teacher_title,
    site_title=settings.site_title,
    group_placeholder=settings.group_placeholder,
    now_local=now_local,
    current_year=lambda: now_local().year,
)

templates.env.filters.update(
    dfull=format_date_full,
    dshort=format_date,
    hm=_time,
    dtime=_datetime,
    status_label=_status_label,
    people=_people,
)
