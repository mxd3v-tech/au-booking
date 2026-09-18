"""Генерация и серверная проверка капчи.

Правильный ответ никогда не уезжает в браузер: в payload попадает только то,
что нужно нарисовать, а разбор ошибки приходит уже после ответа.
"""
from __future__ import annotations

import datetime as dt
import random
import uuid

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.captcha import bank
from app.config import settings
from app.models import CaptchaChallenge

KINDS = ("truth_myth", "net_scheme", "subnet", "quiz")

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
    question, options, correct_index, explain = random.choice(bank.QUIZ)
    order = list(range(len(options)))
    random.shuffle(order)
    payload = {
        "kind": "quiz",
        "title": "Один верный ответ",
        "intro": question,
        "options": [options[i] for i in order],
    }
    return payload, {"index": order.index(correct_index), "explain": explain}


_BUILDERS = {
    "truth_myth": _build_truth_myth,
    "net_scheme": _build_net_scheme,
    "subnet": _build_subnet,
    "quiz": _build_quiz,
}


def build_payload(kind: str | None = None) -> tuple[str, dict, dict]:
    kind = kind if kind in _BUILDERS else random.choice(KINDS)
    payload, answer = _BUILDERS[kind]()
    return kind, payload, answer


# ── Жизненный цикл задания ──────────────────────────────────────────────

def issue_challenge(db: Session, kind: str | None = None) -> CaptchaChallenge:
    """Выдать новое задание и попутно прибрать протухшие."""
    now = dt.datetime.now(dt.timezone.utc)
    db.execute(
        delete(CaptchaChallenge).where(
            CaptchaChallenge.expires_at < now - dt.timedelta(hours=1)
        )
    )
    kind, payload, answer = build_payload(kind)
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
        parts = [p for p in submitted.split(",") if p != ""]
        if len(parts) != len(expected):
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

    if challenge.kind == "quiz":
        try:
            index = int(submitted)
        except ValueError:
            return False, "Вариант не выбран."
        ok = index == answer["index"]
        return ok, answer["explain"]

    return False, "Неизвестный тип задания."


def verify_challenge(db: Session, challenge_id: str, submitted: str) -> dict:
    """Проверить ответ студента. Ответ всегда сверяется на сервере."""
    challenge = db.get(CaptchaChallenge, challenge_id or "")
    now = dt.datetime.now(dt.timezone.utc)

    if challenge is None or challenge.consumed or challenge.expires_at < now:
        return {
            "ok": False,
            "expired": True,
            "title": "Задание устарело",
            "text": "Возьмите новое — оно уже загружается.",
        }

    if challenge.solved:
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

    exhausted = challenge.attempts >= settings.captcha_max_attempts
    if exhausted:
        # Гасим задание, иначе лимит попыток был бы просто надписью:
        # подобрать ответ перебором можно было бы и после него.
        challenge.consumed = True
    db.commit()
    return {
        "ok": False,
        "expired": exhausted,
        "title": random.choice(bank.WRONG_TITLES),
        "text": explain,
        "attempts_left": max(settings.captcha_max_attempts - challenge.attempts, 0),
    }


def consume_challenge(db: Session, challenge_id: str) -> bool:
    """Погасить решённое задание при отправке формы. Одно задание — одна запись."""
    challenge = db.get(CaptchaChallenge, challenge_id or "")
    now = dt.datetime.now(dt.timezone.utc)
    if (
        challenge is None
        or not challenge.solved
        or challenge.consumed
        or challenge.expires_at < now
    ):
        return False
    challenge.consumed = True
    db.commit()
    return True
