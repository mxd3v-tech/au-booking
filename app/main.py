"""Точка входа: сборка приложения, инициализация БД, начальные данные."""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app import models  # noqa: F401 — регистрация таблиц в метаданных
from app.config import settings
from app.db import Base, SessionLocal, engine
from app.models import Purpose
from app.routers import admin, public
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


@asynccontextmanager
async def lifespan(_: FastAPI):
    _wait_for_db()
    Base.metadata.create_all(engine)
    _seed()
    if settings.secret_key == "dev-insecure-secret-key":
        logger.warning("SECRET_KEY не задан — сессии подписаны отладочным ключом")
    yield


app = FastAPI(
    title=f"{settings.site_title} — {settings.teacher_name}",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(public.router)
app.include_router(admin.router)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


@app.exception_handler(404)
async def not_found(request: Request, _exc) -> HTMLResponse:
    return templates.TemplateResponse(request, "404.html", {}, status_code=404)
