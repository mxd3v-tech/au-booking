"""Выгрузка очереди: CSV для Excel и настоящий XLSX."""
from __future__ import annotations

import csv
import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.models import EntryStatus, QueueEntry
from app.security import clean_text
from app.services.queue import to_local

# Согласие — последним столбцом: это не про очередь, а про то, чем
# подтверждается право хранить эти строки.
HEADERS = [
    "№", "Фамилия и имя", "Группа", "Цель визита", "Комментарий", "Статус",
    "Встал в очередь", "Согласие на обработку данных",
]

BRAND = "015D1E"


def _rows(entries: list[QueueEntry]) -> list[list[str]]:
    return [
        [
            str(entry.number),
            entry.full_name,
            entry.group_name,
            entry.purpose.title if entry.purpose else "",
            entry.comment,
            EntryStatus(entry.status).label,
            f"{to_local(entry.created_at):%d.%m.%Y %H:%M}",
            (
                f"{entry.consent_label}, {to_local(entry.consent_at):%d.%m.%Y %H:%M}"
                if entry.consent_at
                else "нет отметки"
            ),
        ]
        for entry in entries
    ]


def to_csv(entries: list[QueueEntry], title: str) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([_csv_text(title)])
    writer.writerow(HEADERS)
    writer.writerows([_csv_text(value) for value in row] for row in _rows(entries))
    # BOM — чтобы Excel не превратил кириллицу в кракозябры
    return "﻿".encode("utf-8") + buffer.getvalue().encode("utf-8")


def _csv_text(value: str) -> str:
    value = clean_text(value)
    # Кавычки CSV экранируют разделитель, но не запрещают Excel вычислять
    # формулу. Учитываем и пробелы/переводы строк перед знаком формулы.
    if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def _set_text(cell, value: str) -> None:
    cell.value = clean_text(value)
    # openpyxl сам распознаёт '=...' как формулу и '#N/A' как ошибку;
    # все поля выгрузки — текст, в том числе из старых записей.
    cell.data_type = "s"


def to_xlsx(entries: list[QueueEntry], title: str) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Очередь"

    _set_text(sheet["A1"], title)
    sheet["A1"].font = Font(bold=True, size=13, color=BRAND)
    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(HEADERS))

    header_fill = PatternFill("solid", fgColor="E8F1EA")
    thin = Side(style="thin", color="D9DFDB")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for column, header in enumerate(HEADERS, start=1):
        cell = sheet.cell(row=3, column=column, value=header)
        cell.font = Font(bold=True, color="0B0F0C")
        cell.fill = header_fill
        cell.border = border
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for row_index, row in enumerate(_rows(entries), start=4):
        for column, value in enumerate(row, start=1):
            cell = sheet.cell(row=row_index, column=column)
            _set_text(cell, value)
            cell.border = border
            cell.alignment = Alignment(vertical="center", wrap_text=column in (4, 5))

    widths = [5, 30, 12, 26, 30, 14, 18, 26]
    for column, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(column)].width = width

    sheet.freeze_panes = "A4"

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
