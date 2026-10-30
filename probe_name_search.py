#!/usr/bin/env python3
"""
Проверка поиска товаров по названию в открытом каталоге (nct.gov.kz). Ключ не нужен, ничего не меняет.

  python probe_name_search.py "кабель type-c"
  python probe_name_search.py "помада губная avon"

Печатает: ответил ли каталог, сколько нашёл и первые карточки (код NTIN, штрихкод, отключена ли, название).
"""
import sys

import requests

URL = "https://nct.gov.kz/api/integration/ofd/search_ofd/"


def main():
    query = " ".join(sys.argv[1:]).strip() or "кабель type-c"
    print(f"Ищу: «{query}»")
    for name in ("q", "name", "search"):
        try:
            r = requests.get(URL, params={name: query, "limit": 10, "offset": 0}, timeout=30)
        except requests.RequestException as e:
            print(f"  параметр {name}: нет связи ({type(e).__name__})")
            continue
        print(f"  параметр {name}: HTTP {r.status_code}")
        if r.status_code != 200:
            print("    ответ:", r.text[:200].replace("\n", " "))
            continue
        try:
            data = r.json()
        except ValueError:
            print("    ответ не JSON:", r.text[:200].replace("\n", " "))
            continue
        items = data.get("result") if isinstance(data, dict) else data
        total = data.get("count") if isinstance(data, dict) else None
        print(f"    всего найдено: {total}; в ответе карточек: {len(items or [])}")
        for it in (items or [])[:8]:
            print("    ", it.get("ntin_code"), "| штрихкод:", it.get("gtin"), "| отключена:", it.get("ntin_isdeactivated"),
                  "|", str(it.get("name_ru"))[:70])
        if items:
            print(f"\nИТОГ: поиск по названию работает через параметр «{name}».")
            return
    print("\nИТОГ: поиск по названию не сработал ни с одним из параметров. Пришлите этот текст мне.")


if __name__ == "__main__":
    main()
