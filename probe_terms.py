#!/usr/bin/env python3
"""
Быстрая оценка: есть ли в открытом каталоге (nct.gov.kz) карточки на группу товаров. Ключ не нужен, ничего не меняет.

  python probe_terms.py                                   # рыболовные товары (список по умолчанию)
  python probe_terms.py "крючок рыболовный" поплавок      # свои слова (фразы в кавычках)

По каждому запросу печатает: сколько карточек найдено всего, сколько из первой выдачи действующих и сколько из них содержат
ВСЕ слова запроса, плюс два примера названий. Это оценка «есть ли вообще такие карточки», а не гарантия, что есть именно ваш товар.
"""
import re
import sys
import time

import requests

URL = "https://nct.gov.kz/api/integration/ofd/search_ofd/"
DEFAULT = ["крючок рыболовный", "поплавок", "грузило", "леска рыболовная", "катушка рыболовная", "блесна", "воблер",
           "удилище", "мормышка", "силиконовая приманка", "спиннинг", "садок рыболовный"]


def has_all_words(name, query):
    low = (name or "").lower().replace("ё", "е")
    for w in re.findall(r"[\w-]+", query.lower().replace("ё", "е")):
        if w[:max(4, len(w) - 2)] not in low:
            return False
    return True


def check(term, get=requests.get):
    """Возвращает (всего найдено, действующие карточки из первой выдачи) или None, если каталог не ответил."""
    try:
        r = get(URL, params={"q": term, "limit": 30, "offset": 0}, timeout=30)
        if r.status_code != 200:
            return None
        data = r.json()
    except (requests.RequestException, ValueError):
        return None
    items = data.get("result") if isinstance(data, dict) else data
    total = data.get("count") if isinstance(data, dict) else None
    active = [i for i in (items or []) if not i.get("ntin_isdeactivated") and i.get("ntin_code")]
    return total, active


def main(argv=None, get=requests.get, sleep=time.sleep):
    terms = list(argv if argv is not None else sys.argv[1:]) or DEFAULT
    for i, term in enumerate(terms):
        if i:
            sleep(1)
        res = check(term, get)
        if res is None:
            print(f"{term}: каталог не ответил, повторите позже")
            continue
        total, active = res
        exact = [a for a in active if has_all_words(a.get("name_ru"), term)]
        print(f"{term}: найдено всего {total}; действующих в первой выдаче {len(active)}; с полным совпадением слов {len(exact)}")
        for a in (exact or active)[:2]:
            print(f"    {a.get('ntin_code')}  {str(a.get('name_ru'))[:90]}")


if __name__ == "__main__":
    main()
