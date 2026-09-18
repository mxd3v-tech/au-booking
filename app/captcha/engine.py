"""Генерация и серверная проверка капчи.

Правильный ответ никогда не уезжает в браузер: в payload попадает только то,
что нужно нарисовать, а разбор ошибки приходит уже после ответа.
"""
from __future__ import annotations

import datetime as dt
import random
import uuid

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.captcha import bank
from app.config import settings
from app.models import CaptchaChallenge

KINDS = (
    "truth_myth", "net_scheme", "subnet", "quiz",
    "ports", "order_steps", "permissions", "logs",
)

TRUTH_MYTH_CARDS = 3
SUBNET_MIN_PREFIX = 24
SUBNET_MAX_PREFIX = 30


def _hosts_for(prefix: int) -> int:
    """Сколько адресов можно раздать хостам в сети с таким префиксом."""
    return max(2 ** (32 - prefix) - 2, 0)


def _best_prefix(hosts: int) -> int:
    """Самая экономная маска, в которую ещё влезает нужное число хостов."""
    for prefix in range(SUBNET_MAX_PREFIX, SUBNET_MIN_PREFIX - 1, -1):
        if _hosts_for(prefix) >= hosts:
            return prefix
    return SUBNET_MIN_PREFIX


# ── Сборка заданий ──────────────────────────────────────────────────────

def _build_truth_myth() -> tuple[dict, dict]:
    cards = random.sample(bank.TRUTH_MYTH, TRUTH_MYTH_CARDS)
    payload = {
        "kind": "truth_myth",
        "title": "Правда или миф",
        "intro": "Три утверждения из жизни администратора. Решите по каждому.",
        "cards": [{"text": text} for text, _, _ in cards],
    }
    answer = {
        "values": [is_true for _, is_true, _ in cards],
        "explains": [explain for _, _, explain in cards],
    }
    return payload, answer


def _build_net_scheme() -> tuple[dict, dict]:
    symptom, node_id, explain = random.choice(bank.SCHEME_CASES)
    payload = {
        "kind": "net_scheme",
        "title": "Где искать причину",
        "intro": symptom,
        "nodes": bank.SCHEME_NODES,
    }
    return payload, {"node": node_id, "explain": explain}


def _build_subnet() -> tuple[dict, dict]:
    hosts, case = random.choice(bank.SUBNET_CASES)
    base = random.choice(bank.SUBNET_BASES)
    prefix = _best_prefix(hosts)
    tighter = prefix + 1
    explain = (
        f"Нужно {hosts} адресов. /{prefix} даёт {_hosts_for(prefix)} — ближайшая подходящая. "
        + (
            f"В /{tighter} поместилось бы только {_hosts_for(tighter)}."
            if tighter <= SUBNET_MAX_PREFIX
            else "Меньше уже некуда: /30 — это стык на два адреса."
        )
    )
    payload = {
        "kind": "subnet",
        "title": "Выберите маску",
        "intro": f"Нужно выдать адреса: {case}. Хостов: {hosts}.",
        "hosts": hosts,
        "base": base,
        "min_prefix": SUBNET_MIN_PREFIX,
        "max_prefix": SUBNET_MAX_PREFIX,
        "start_prefix": SUBNET_MIN_PREFIX,
    }
    return payload, {"prefix": prefix, "explain": explain}


def _build_quiz() -> tuple[dict, dict]:
    return _build_choice("quiz", "Один верный ответ", random.choice(bank.QUIZ))


def _build_choice(kind: str, title: str, case: tuple) -> tuple[dict, dict]:
    question, options, correct_index, explain = case
    order = list(range(len(options)))
    random.shuffle(order)
    payload = {
        "kind": kind,
        "title": title,
        "intro": question,
        "options": [options[i] for i in order],
    }
    return payload, {"index": order.index(correct_index), "explain": explain}


def _build_ports() -> tuple[dict, dict]:
    services = random.sample(bank.PORT_SERVICES, 3)
    ports = [port for _, port in services]
    random.shuffle(ports)
    return {
        "kind": "ports",
        "title": "Сервис ищет порт",
        "intro": "Сопоставьте три сервиса с портами сервера по умолчанию.",
        "services": [name for name, _ in services],
        "ports": ports,
    }, {
        "values": [port for _, port in services],
        "explain": "Стандартные порты: " + "; ".join(f"{name} — {port}" for name, port in services) + ".",
    }


def _build_order_steps() -> tuple[dict, dict]:
    question, steps, explain = random.choice(bank.ORDER_CASES)
    order = list(range(len(steps)))
    random.shuffle(order)
    # Не выдаём уже собранную последовательность.
    if order == list(range(len(steps))):
        order = order[1:] + order[:1]
    return {
        "kind": "order_steps",
        "title": "Что за чем",
        "intro": question,
        "steps": [steps[i] for i in order],
    }, {"values": [order.index(i) for i in range(len(steps))], "explain": explain}


def _build_permissions() -> tuple[dict, dict]:
    return _build_choice("permissions", "Права без 777", random.choice(bank.PERMISSION_CASES))


def _build_logs() -> tuple[dict, dict]:
    log, question, options, index, explain = random.choice(bank.LOG_CASES)
    payload, answer = _build_choice("logs", "Читаем журнал", (question, options, index, explain))
    payload["log"] = log
    return payload, answer


_BUILDERS = {
    "truth_myth": _build_truth_myth,
    "net_scheme": _build_net_scheme,
    "subnet": _build_subnet,
    "quiz": _build_quiz,
    "ports": _build_ports,
    "order_steps": _build_order_steps,
    "permissions": _build_permissions,
    "logs": _build_logs,
}


def build_payload(kind: str | None = None, *, exclude_kind: str | None = None) -> tuple[str, dict, dict]:
    kind = kind if kind in _BUILDERS and kind != exclude_kind else random.choice(
        [candidate for candidate in KINDS if candidate != exclude_kind]
    )
    payload, answer = _BUILDERS[kind]()
    return kind, payload, answer


# ── Жизненный цикл задания ──────────────────────────────────────────────

def issue_challenge(db: Session, kind: str | None = None, *, exclude_kind: str | None = None) -> CaptchaChallenge:
    """Выдать новое задание и попутно прибрать протухшие."""
    now = dt.datetime.now(dt.timezone.utc)
    db.execute(
        delete(CaptchaChallenge).where(
            CaptchaChallenge.expires_at < now - dt.timedelta(hours=1)
        )
    )
    kind, payload, answer = build_payload(kind, exclude_kind=exclude_kind)
    challenge = CaptchaChallenge(
        id=str(uuid.uuid4()),
        kind=kind,
        payload=payload,
        answer=answer,
        expires_at=now + dt.timedelta(seconds=settings.captcha_ttl_seconds),
    )
    db.add(challenge)
    db.commit()
    return challenge


def _check(challenge: CaptchaChallenge, submitted: str) -> tuple[bool, str]:
    """Сверить ответ. Возвращает (верно, пояснение)."""
    answer = challenge.answer
    submitted = (submitted or "").strip()

    if challenge.kind == "truth_myth":
        expected = answer["values"]
        parts = submitted.split(",")
        if len(parts) != len(expected) or any(p not in {"0", "1"} for p in parts):
            return False, "Нужно ответить на все три утверждения."
        given = [p == "1" for p in parts]
        wrong = [i for i, (g, e) in enumerate(zip(given, expected)) if g != e]
        if not wrong:
            return True, "Все три разобраны верно."
        return False, " ".join(answer["explains"][i] for i in wrong)

    if challenge.kind == "net_scheme":
        ok = submitted == answer["node"]
        return ok, answer["explain"]

    if challenge.kind == "subnet":
        try:
            prefix = int(submitted)
        except ValueError:
            return False, "Маска не распознана."
        ok = prefix == answer["prefix"]
        return ok, answer["explain"]

    if challenge.kind in {"ports", "order_steps"}:
        try:
            values = [int(part) for part in submitted.split(",")]
        except ValueError:
            return False, "Ответьте на задание полностью."
        return values == answer["values"], answer["explain"]

    if challenge.kind in {"quiz", "permissions", "logs"}:
        try:
            index = int(submitted)
        except ValueError:
            return False, "Вариант не выбран."
        ok = index == answer["index"]
        return ok, answer["explain"]

    return False, "Неизвестный тип задания."


def verify_challenge(db: Session, challenge_id: str, submitted: str) -> dict:
    """Проверить ответ студента. Ответ всегда сверяется на сервере."""
    # Проверка ответа и счётчик попыток сериализованы с погашением задания.
    challenge = db.scalar(
        select(CaptchaChallenge)
        .where(CaptchaChallenge.id == (challenge_id or ""))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = dt.datetime.now(dt.timezone.utc)

    if challenge is None or challenge.consumed or challenge.expires_at < now:
        db.rollback()
        return {
            "ok": False,
            "expired": True,
            "title": "Задание устарело",
            "text": "Возьмите новое — оно уже загружается.",
        }

    if challenge.solved:
        db.rollback()
        return {"ok": True, "title": "Уже принято", "text": "Можно отправлять форму."}

    challenge.attempts += 1
    ok, explain = _check(challenge, submitted)

    if ok:
        challenge.solved = True
        db.commit()
        return {
            "ok": True,
            "title": random.choice(bank.RIGHT_TITLES),
            "text": explain,
        }

    # После первой ошибки задание больше не принимается, даже прямым запросом.
    challenge.consumed = True
    db.commit()
    return {
        "ok": False,
        "expired": True,
        "title": random.choice(bank.WRONG_TITLES),
        "text": explain,
    }


def consume_challenge(db: Session, challenge_id: str) -> bool:
    """Погасить решённое задание при отправке формы. Одно задание — одна запись."""
    # Условие перепроверяется PostgreSQL после ожидания конкурентного UPDATE.
    # Даже если оба запроса пришли одновременно, строку получит только один.
    consumed_id = db.scalar(
        update(CaptchaChallenge)
        .where(
            CaptchaChallenge.id == (challenge_id or ""),
            CaptchaChallenge.solved.is_(True),
            CaptchaChallenge.consumed.is_(False),
            CaptchaChallenge.expires_at >= func.clock_timestamp(),
        )
        .values(consumed=True)
        .returning(CaptchaChallenge.id)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return consumed_id is not None
