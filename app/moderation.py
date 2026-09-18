"""Фильтр ФИО: мат, брань и подставные имена.

Очередь висит на экране у кабинета, и фамилия из неё видна всем, кто стоит
рядом. Поэтому ФИО проверяется не только на формат, но и на содержание —
иначе первый же шутник украшает собой список приёма.

Как это устроено:

  * Слово «складывается» — приводится к тому, что человек читает глазами:
    похожие на буквы латиница и цифры становятся кириллицей, регистр и «ё»
    снимаются, повторы букв сжимаются («хуууй» → «хуй»), всё, кроме букв,
    выбрасывается. Ровно так и обходят простые списки запрещённых слов.
  * Сложенное слово сверяется с корнями. Корни намеренно узкие, а на
    настоящие фамилии, которые в них попадают («Херсонский», «Мудрецов»,
    «Сукачёв», «Мандарин»), стоят исключения: ложное срабатывание обиднее
    пропуска — человеку отказывают в его собственной фамилии.
  * Проверяются и отдельные слова, и ФИО целиком без пробелов: брань любят
    разбивать на части («Ху Йов»).

Список — защита от шуток, а не цензура. Настоящую фамилию, которая всё же
попала под фильтр, преподаватель вписывает вручную из админки: там фильтра
нет, потому что решает живой человек.
"""
from __future__ import annotations

import logging
import re

from app.config import settings

logger = logging.getLogger("au-queue.names")

BANNED_MESSAGE = (
    "Так записаться нельзя: в фамилии или имени запрещённое слово. "
    "Если это ваши настоящие фамилия и имя — подойдите к преподавателю, "
    "он впишет вас в очередь вручную."
)
NONSENSE_MESSAGE = (
    "Это не похоже на фамилию и имя. Напишите их так, как в зачётке."
)

# Латиница и цифры, которые на экране читаются как кириллица: именно ими
# заменяют буквы, чтобы слово прошло проверку. Всё остальное отбрасывается.
_HOMOGLYPHS = str.maketrans(
    {
        "a": "а", "b": "в", "c": "с", "e": "е", "h": "н", "k": "к", "m": "м",
        "o": "о", "p": "р", "t": "т", "x": "х", "y": "у",
        "0": "о", "1": "и", "3": "з", "4": "ч", "6": "б",
    }
)
_CYRILLIC = re.compile(r"[^а-я]")
_LATIN = re.compile(r"[^a-z]")
_DOUBLED = re.compile(r"(.)\1+")

VOWELS = frozenset("аеёиоуыэюяaeiouy")

# Корни брани в сложенном слове. Записаны без приставок и окончаний;
# «^» — начало слова, отрицательные заглядывания вперёд — исключения для
# настоящих фамилий, которые иначе попадали бы под запрет.
_BANNED_RU = (
    r"ху[йеяёи]",
    r"п[ие]?зд",
    r"бля[дтш]",
    # Корень «еб» с любой приставкой. Без приставок его нельзя искать как
    # подстроку: тогда под запрет уходят Лебедев, Требухов и Объедков.
    r"^(?:вы|за|на|по|от|отъ|под|подъ|у|до|раз|при|пере|об|съ)?[ъь]?еб",
    r"(?:долб|муд|дур|жоп|гни|черт)[оа]?[её]б",
    r"мудак|мудил|мудоз",
    r"пид[оа]р|п[ие]драс|педрил",
    r"г[ао]ндон",
    r"залуп",
    r"дроч",
    r"манда(?![рт])",
    r"сука(?![чнрелс])|сучар",
    r"^хер(?!сон|аск|ув)",
    r"говн|срак|дерьм",
    r"шлюх|проститут|сперм|минет|анальн|онанис",
    # Брань помягче, но в списке приёма ей тоже нечего делать.
    r"дебил|^идиот|^урод|ничтожеств",
)

# Латиница отдельно: сложенное в кириллицу «fuck» превращается в безобидный
# мусор, поэтому транслит и английскую брань ищем по исходным буквам.
_BANNED_LAT = (
    r"^hu[yi]",
    r"^pizd",
    r"^blya",
    r"^[a-z]{0,3}eban",
    r"^suka$|^suchka",
    r"^mudak",
    r"^pidor",
    r"fuck|shit(?!ov)|bitch|cunt|nigg",
)

_RU_PATTERNS = tuple(re.compile(pattern) for pattern in _BANNED_RU)
_LAT_PATTERNS = tuple(re.compile(pattern) for pattern in _BANNED_LAT)


def _fold_ru(word: str) -> str:
    """Слово так, как его видит глаз: кириллица, без повторов и разделителей."""
    folded = word.lower().replace("ё", "е").translate(_HOMOGLYPHS)
    return _DOUBLED.sub(r"\1", _CYRILLIC.sub("", folded))


def _fold_lat(word: str) -> str:
    return _DOUBLED.sub(r"\1", _LATIN.sub("", word.lower()))


def _extra_stems() -> tuple[str, ...]:
    """Слова из BANNED_NAMES_EXTRA — по той же нормализации, что и свои."""
    raw = (settings.banned_names_extra or "").replace(";", ",").split(",")
    return tuple(stem for stem in (_fold_ru(word) for word in raw) if len(stem) > 2)


def find_banned_stem(name: str) -> str | None:
    """Первый запрещённый корень в ФИО или None.

    Возвращаем корень, а не само ФИО: в журнал попадает причина отказа,
    а не персональные данные.
    """
    words = [part for part in re.split(r"[-'\s]+", name or "") if part]
    russian = [_fold_ru(word) for word in words]
    latin = [_fold_lat(word) for word in words]
    # Отдельные слова — чтобы «^» в корне означал начало слова. Плюс ФИО
    # целиком, без пробелов: брань любят разбивать на части («Ху Йов»).
    russian.append("".join(russian))
    latin.append("".join(latin))

    for patterns, candidates in ((_RU_PATTERNS, russian), (_LAT_PATTERNS, latin)):
        for pattern in patterns:
            if any(pattern.search(candidate) for candidate in candidates):
                return pattern.pattern
    for stem in _extra_stems():
        if any(stem in candidate for candidate in russian):
            return stem
    return None


def looks_like_name(name: str) -> bool:
    """Похоже ли это вообще на фамилию и имя.

    Двух проверок хватает, чтобы отсеять «Ааа Ббб» и «Ыыы Ъъъ», не мешая
    настоящим именам: в каждом слове есть гласная, и после сжатия повторов в
    нём остаётся не меньше двух букв.
    """
    for part in re.split(r"[-'\s]+", name or ""):
        if not part:
            continue
        if len(_fold_ru(part)) < 2 and len(_fold_lat(part)) < 2:
            return False
        if not (set(part.lower()) & VOWELS):
            return False
    return True


def check_name(name: str) -> str | None:
    """Сообщение об отказе для формы записи или None, если ФИО годится."""
    stem = find_banned_stem(name)
    if stem is not None:
        logger.info("ФИО не прошло фильтр: сработал корень %s", stem)
        return BANNED_MESSAGE
    if not looks_like_name(name):
        return NONSENSE_MESSAGE
    return None
