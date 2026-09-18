"""Точка входа: сборка приложения, инициализация БД, начальные данные."""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import UniqueConstraint, func, inspect, select
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.schema import AddConstraint

from app import models  # noqa: F401 — регистрация таблиц в метаданных
from app.config import settings
from app.db import Base, SessionLocal, engine
from app.models import Purpose, QueueEntry
from app.routers import admin, public
from app.security import name_key
from app.services import queue as queue_service
from app.templating import templates

logger = logging.getLogger("au-queue")

STATIC_DIR = Path(__file__).resolve().parent / "static"

DEFAULT_PURPOSES = [
    ("Сдача или защита лабораторной работы", "Номер работы укажите в комментарии", False, 10),
    ("Пересдача, отработка пропуска", "", False, 20),
    ("Консультация по проекту, курсовой или диплому", "", False, 30),
    ("Организационные вопросы, документы, подписи", "", False, 40),
    ("Другое", "Коротко опишите вопрос — так приём пройдёт быстрее", True, 90),
]


def _wait_for_db(attempts: int = 30, delay: float = 2.0) -> None:
    for attempt in range(1, attempts + 1):
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql("SELECT 1")
            return
        except OperationalError:
            logger.warning("База ещё не готова (попытка %s/%s)", attempt, attempts)
            time.sleep(delay)
    raise RuntimeError("Не удалось подключиться к базе данных")


def _existing_names(inspector, table_name: str) -> set[str]:
    """Как в базе уже называются индексы и уникальные ограничения таблицы."""
    return {index["name"] for index in inspector.get_indexes(table_name)} | {
        unique["name"] for unique in inspector.get_unique_constraints(table_name)
    }


def _rebuild_name_keys() -> None:
    """Пересчитать ключи ФИО по нынешним правилам сравнения.

    Дубли, накопившиеся, пока уникальности не было, удалять нельзя — это
    история приёмов. Разводим им ключи, иначе ограничение просто не встанет.
    """
    with SessionLocal() as db:
        entries = list(
            db.scalars(
                select(QueueEntry).order_by(QueueEntry.session_id, QueueEntry.number)
            ).all()
        )
        taken: set[tuple[int, str]] = set()
        duplicates = 0
        for entry in entries:
            key = unique = name_key(entry.full_name)
            attempt = 1
            while (entry.session_id, unique) in taken:
                attempt += 1
                duplicates += 1
                unique = f"{key}|{entry.group_name.lower()}|{attempt}"[:160]
            taken.add((entry.session_id, unique))
            entry.full_name_key = unique
        db.commit()
    if entries:
        logger.info(
            "Ключи ФИО пересчитаны: записей %s, прежних дублей %s",
            len(entries),
            duplicates,
        )


def _sql_literal(value: object) -> str:
    """Значение по умолчанию для DDL: параметры в ALTER TABLE не подставить."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _add_missing_columns(inspector, table) -> None:
    """Досоздать колонки, появившиеся в модели после первого запуска.

    Та же беда, что и с индексами: `create_all` существующую таблицу не
    трогает. Так согласие на обработку данных («когда» и «по какой редакции»)
    без этого шага до рабочей базы не доехало бы вовсе.

    NOT NULL-колонке нужен DEFAULT: в таблице уже лежат записи, и без него
    PostgreSQL ALTER не пропустит. Значение берём из модели — у прежних
    записей оно и означает «согласия в базе нет».
    """
    existing = {column["name"] for column in inspector.get_columns(table.name)}
    for column in table.columns:
        if column.name in existing:
            continue
        spec = f"{column.name} {column.type.compile(engine.dialect)}"
        default = getattr(column.default, "arg", None)
        if not column.nullable:
            if default is None or callable(default):
                logger.error(
                    "Колонку %s.%s нельзя добавить автоматически: она NOT NULL, "
                    "а значения по умолчанию у неё нет",
                    table.name,
                    column.name,
                )
                continue
            spec += f" NOT NULL DEFAULT {_sql_literal(default)}"
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    f"ALTER TABLE {table.name} ADD COLUMN IF NOT EXISTS {spec}"
                )
        except SQLAlchemyError:
            logger.exception("Не удалось добавить колонку %s.%s", table.name, column.name)
        else:
            logger.info("Добавлена колонка %s.%s", table.name, column.name)


def _upgrade_schema() -> None:
    """Досоздать в уже существующих таблицах то, чего в них нет.

    `create_all` создаёт таблицу целиком либо обходит её стороной: всё, что
    появилось в моделях после первого запуска, само до базы не доезжает —
    так и вышло, что уникальности записей в рабочей базе не было вовсе.
    """
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    # Старое правило «нельзя стоять в очереди дважды» уступило место
    # «нельзя записаться дважды»; ключ и условие у них разные.
    if "queue_entry" in tables:
        if "uq_entry_active_person" in _existing_names(inspector, "queue_entry"):
            with engine.begin() as connection:
                connection.exec_driver_sql("DROP INDEX IF EXISTS uq_entry_active_person")
            logger.info("Снят прежний индекс uq_entry_active_person")
        if "uq_entry_person" not in _existing_names(inspector, "queue_entry"):
            _rebuild_name_keys()

    for table in Base.metadata.sorted_tables:
        if table.name not in tables:
            continue  # таблицы нет — create_all создаст её вместе с индексами
        _add_missing_columns(inspector, table)
        known = _existing_names(inspector, table.name)

        missing = [index for index in table.indexes if index.name not in known]
        # Только именованные: безымянная уникальность — это `unique=True` у
        # колонки, она уходит в базу прямо в объявлении столбца. Сверить её
        # с тем, что уже есть, не по чему, и добавится она вторым индексом.
        unique_constraints = [
            constraint
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
            and isinstance(constraint.name, str)
            and constraint.name not in known
        ]

        for index in missing:
            try:
                index.create(engine, checkfirst=True)
            except SQLAlchemyError:
                logger.exception("Не удалось создать индекс %s", index.name)
            else:
                logger.info("Создан индекс %s", index.name)

        for constraint in unique_constraints:
            try:
                with engine.begin() as connection:
                    connection.execute(AddConstraint(constraint))
            except SQLAlchemyError:
                # Единственная реальная причина — данные, которые ограничению
                # противоречат. Падать из-за этого нельзя: приём должен идти.
                logger.exception(
                    "Не удалось добавить ограничение %s — в таблице есть "
                    "записи, которые ему не отвечают",
                    constraint.name,
                )
            else:
                logger.info("Добавлено ограничение %s", constraint.name)


def _seed() -> None:
    with SessionLocal() as db:
        if (db.scalar(select(func.count(Purpose.id))) or 0) == 0:
            for title, hint, needs_comment, order in DEFAULT_PURPOSES:
                db.add(
                    Purpose(
                        title=title, hint=hint, needs_comment=needs_comment, sort_order=order
                    )
                )
            db.commit()
            logger.info("Справочник целей визита заполнен значениями по умолчанию")


def _purge_expired() -> None:
    """Убрать приёмы, у которых вышел срок хранения из политики.

    Планировщика в сервисе нет: чистим при старте и при закрытии приёма.
    Для сервиса, который поднимают к началу семестра и перезапускают после
    обновлений, этого достаточно — обещанный срок не растягивается.
    """
    with SessionLocal() as db:
        removed = queue_service.purge_expired(db)
    if removed:
        logger.info(
            "Срок хранения (%s дн.) вышел: удалено приёмов %s",
            settings.retention_days,
            removed,
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    _wait_for_db()
    Base.metadata.create_all(engine)
    _upgrade_schema()
    _seed()
    _purge_expired()
    if settings.secret_key == "dev-insecure-secret-key":
        logger.warning("SECRET_KEY не задан — сессии подписаны отладочным ключом")
    if not settings.operator_known:
        logger.warning(
            "ORG_NAME не задан: в согласии на обработку данных не будет "
            "оператора, и юридической силы у такого согласия нет"
        )
    yield


class RevalidatedStatic(StaticFiles):
    """Статика с обязательной перепроверкой у сервера.

    Без Cache-Control браузер вправе несколько часов рисовать страницу
    скриптом из кэша: после обновления контейнера разметка приезжает новая,
    а app.js остаётся старым — и часть капчи перестаёт отвечать на тапы.
    С no-cache файл по-прежнему лежит в кэше, но сверяется по ETag и почти
    всегда отдаётся как 304 — трафика это не добавляет.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app = FastAPI(
    title=f"{settings.site_title} — {settings.teacher_name}",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

app.mount("/static", RevalidatedStatic(directory=str(STATIC_DIR)), name="static")
app.include_router(public.router)
app.include_router(admin.router)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


@app.exception_handler(404)
async def not_found(request: Request, _exc) -> HTMLResponse:
    return templates.TemplateResponse(request, "404.html", {}, status_code=404)
