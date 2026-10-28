#!/usr/bin/env python3
"""
Проверка штрихкодов (GTIN) и поиск NTIN: сначала в локальной базе, потом в каталоге НКТ.

Установка:   pip install requests pandas openpyxl
Файлы рядом: check_ntin.py и base_ntin.csv (база готовых NTIN)

Примеры:
  python check_ntin.py товары.xlsx --col "Штрихкод" --out результат.xlsx
  python check_ntin.py товары.xlsx --col "Штрихкод" --debug      # показать сырой ответ каталога
  python check_ntin.py товары.xlsx --col "Штрихкод" --offline    # только база, без сети
  python check_ntin.py --enrich-base                             # дописать штрихкоды в базе по NTIN

Как работает:
  1. Штрихкод есть в базе  -> берём NTIN оттуда и проверяем его в каталоге (не деактивирован ли).
  2. Штрихкода нет в базе  -> ищем в каталоге по штрихкоду.
  Каталог (по 100 кодов за запрос) считается главным источником. Если он недоступен,
  результат из базы помечается FOUND_UNVERIFIED.

Статусы:
  FOUND             один активный NTIN, подтверждён каталогом
  FOUND_UNVERIFIED  NTIN есть в базе, но каталог его не подтвердил или недоступен
  MULTIPLE          несколько активных NTIN, нужен выбор вручную (в каталоге бывают дубли карточек)
  DEACTIVATED       карточка деактивирована (в комментарии NTIN-дубликат, если он указан)
  NOT_FOUND         ни в базе, ни в каталоге: нужна заявка на регистрацию
  NOT_CHECKED       каталог был недоступен, в базе кода нет
  INTERNAL_CODE     внутренний штрихкод магазина (префиксы 02, 04, 20-29): по нему искать нельзя,
                    он может принадлежать чужому товару. Сверяйте по названию.
  BAD_BARCODE       штрихкод пустой или некорректный
"""
import argparse
import os
import re
import sys
import time

import pandas as pd
import requests

URL = "https://nct.gov.kz/api/integration/ofd/search_ofd/"
BATCH = 100          # максимум кодов в одном запросе по документации
PAUSE = 0.6          # пауза между запросами, сек
RETRIES = 4
TIMEOUT = 30          # таймаут одного запроса, сек (бот ставит меньше)
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BASE = os.path.join(HERE, "base_ntin.csv")


def digits(x):
    s = "" if x is None else str(x)
    return "" if s == "nan" else re.sub(r"\D", "", s)


def norm(x):
    """Ключ для сравнения кодов: только цифры без ведущих нулей (Excel часто их теряет)."""
    return digits(x).lstrip("0")


def clean_gtin(code):
    d = digits(code)
    return d if len(d) in (8, 12, 13, 14) else ""


def is_internal(g):
    """Внутренние/ограниченные штрихкоды (префиксы 02, 04, 20-29): не уникальны в мире."""
    if len(g) == 12:          # UPC-A: система нумерации 2 (товары на вес) считается внутренней
        return g[0] == "2"
    if len(g) == 13:          # EAN-13: 200-299, 02x, 04x
        return g[0] == "2" or g[:2] in ("02", "04")
    return False


def dup_of(item):
    return digits(item.get("ntin_duplicateofproduct") or item.get("ntin_duulateofproduct"))


# ---------------------------------------------------------------- база
def load_base(path):
    if not os.path.exists(path):
        print(f"База не найдена: {path}. Работаю только с каталогом.", file=sys.stderr)
        return pd.DataFrame(columns=["ntin", "gtin", "name", "oktru", "source"])
    return pd.read_csv(path, dtype=str).fillna("")


def base_index(base):
    idx, names = {}, {}
    for r in base.itertuples():
        if r.gtin:
            k = norm(r.gtin)
            lst = idx.setdefault(k, [])
            if r.ntin not in lst:
                lst.append(r.ntin)
            names.setdefault(k, r.name)
    return idx, names


# ---------------------------------------------------------------- каталог
def fetch(codes, debug=False):
    """Один запрос к каталогу: список карточек или None при ошибке."""
    params = {"tin": ",".join(codes)}
    for attempt in range(RETRIES):
        try:
            r = requests.get(URL, params=params, timeout=TIMEOUT)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            data = r.json()
            if debug:
                print(str(data)[:1500])
            if isinstance(data, dict):
                data = data.get("result") or data.get("results") or []
            return data
        except (requests.RequestException, ValueError) as e:
            print(f"  ошибка запроса ({attempt + 1}/{RETRIES}): {e}", file=sys.stderr)
            time.sleep(2 ** attempt)
    return None


def query_catalog(codes, debug=False):
    """Возвращает (карточки, множество успешно проверенных кодов)."""
    items, checked = [], set()
    codes = list(dict.fromkeys(codes))
    for i in range(0, len(codes), BATCH):
        batch = codes[i:i + BATCH]
        data = fetch(batch, debug=debug)
        if debug:
            return items, checked
        if data is None:
            print(f"Пачка {i // BATCH + 1} не загрузилась, пропускаю", file=sys.stderr)
        else:
            items.extend(data)
            checked.update(batch)
        print(f"Каталог: {min(i + BATCH, len(codes))}/{len(codes)}")
        time.sleep(PAUSE)
    return items, checked


def index_items(items):
    by_g, by_n = {}, {}
    for it in items:
        g, n = norm(it.get("gtin")), norm(it.get("ntin_code"))
        if g:
            by_g.setdefault(g, []).append(it)
        if n:
            by_n.setdefault(n, []).append(it)
    return by_g, by_n


def cat_name(item):
    return (item.get("name_ru") or item.get("name_kk") or "").strip()


# ---------------------------------------------------------------- логика статусов
def classify(g, base_ntins, by_g, by_n, api_ok, base_name="", known=frozenset()):
    """Возвращает (ntin, статус, источник, комментарий, название в каталоге)."""
    items = by_g.get(norm(g), [])
    base_keys = {norm(n) for n in base_ntins}
    active_items = [i for i in items if not i.get("ntin_isdeactivated")]
    active = sorted({digits(i.get("ntin_code")) for i in active_items})
    if active:
        src = "база+каталог" if {norm(a) for a in active} & base_keys else "каталог"
        names = " | ".join(dict.fromkeys(cat_name(i) for i in active_items))
        if len(active) == 1:
            return active[0], "FOUND", src, "", names
        # дубли карточек: если ровно одна из них есть в нашей базе (прайс или выгрузка производителя), берём её
        trusted = [a for a in active if norm(a) in base_keys or norm(a) in known]
        if len(trusted) == 1:
            t = trusted[0]
            tname = next((cat_name(i) for i in active_items if digits(i.get("ntin_code")) == t), "")
            return t, "FOUND", "база+каталог", f"выбрана карточка из базы, других активных на этот штрихкод: {len(active) - 1}", tname
        return ",".join(active), "MULTIPLE", src, "несколько активных карточек на один штрихкод, нужен выбор", names
    if items:
        dup = next((dup_of(i) for i in items if dup_of(i)), "")
        return (dup or None), "DEACTIVATED", "каталог", "карточка деактивирована" + (f", дубликат {dup}" if dup else ""), cat_name(items[0])
    # каталог по штрихкоду ничего не вернул: смотрим карточки по кодам из базы
    for n in base_ntins:
        its = by_n.get(norm(n), [])
        if its and not its[0].get("ntin_isdeactivated"):
            return n, "FOUND", "база+каталог", "", cat_name(its[0])
        if its:
            dup = dup_of(its[0])
            return (dup or None), "DEACTIVATED", "база+каталог", "код из базы деактивирован" + (f", дубликат {dup}" if dup else ""), cat_name(its[0])
    if base_ntins:
        n = ",".join(base_ntins)
        if len(base_ntins) > 1:
            return n, "MULTIPLE", "база", "в базе несколько кодов на один штрихкод" + ("" if api_ok else ", каталог недоступен"), base_name
        return n, "FOUND_UNVERIFIED", "база", "каталог не подтвердил код" if api_ok else "каталог недоступен", base_name
    return None, ("NOT_FOUND" if api_ok else "NOT_CHECKED"), "", "", ""


# ---------------------------------------------------------------- режимы работы
def run_check(a):
    if a.file.lower().endswith(".csv"):
        df = pd.read_csv(a.file, dtype=str)
    else:
        df = pd.read_excel(a.file, dtype=str)
    if a.col not in df.columns:
        sys.exit(f"Колонки «{a.col}» нет в файле. Есть: {', '.join(map(str, df.columns))}")

    df["_gtin"] = df[a.col].map(clean_gtin)
    all_g = sorted({g for g in df["_gtin"] if g})
    internal = {g for g in all_g if is_internal(g)}
    gtins = [g for g in all_g if g not in internal]
    base_df = load_base(a.base)
    base, base_names = base_index(base_df)
    known = {norm(n) for n in base_df["ntin"]}
    print(f"Строк: {len(df)}, уникальных штрихкодов: {len(all_g)}, из них внутренних: {len(internal)}, "
          f"есть в базе: {sum(norm(g) in base for g in gtins)}")

    # что спрашиваем у каталога: коды из базы (для проверки) и штрихкоды, которых в базе нет
    codes, code_of = [], {}
    for g in gtins:
        asked = base[norm(g)] if norm(g) in base else [g]
        code_of[g] = asked
        codes.extend(asked)

    if a.offline:
        items, checked = [], set()
    else:
        items, checked = query_catalog(codes, debug=a.debug)
        if a.debug:
            return
    by_g, by_n = index_items(items)

    out = {}
    for g in internal:
        out[g] = (None, "INTERNAL_CODE", "", "внутренний штрихкод (префикс 02, 04 или 2): искать по нему нельзя, "
                  "в каталоге он может относиться к чужому товару. Сверьте по названию", "")
    for g in gtins:
        api_ok = all(c in checked for c in code_of[g])
        out[g] = classify(g, base.get(norm(g), []), by_g, by_n, api_ok, base_names.get(norm(g), ""), known)

    res = [out.get(g, (None, "BAD_BARCODE", "", "", "")) for g in df["_gtin"]]
    df["ntin"] = [r[0] for r in res]
    df["статус"] = [r[1] for r in res]
    df["источник"] = [r[2] for r in res]
    df["комментарий"] = [r[3] for r in res]
    df["название в каталоге"] = [r[4] for r in res]
    df.drop(columns="_gtin").to_excel(a.out, index=False)
    print()
    print(df[[a.col, "ntin", "статус", "источник", "название в каталоге"]].to_string(index=False, max_colwidth=38))
    print()
    print(df["статус"].value_counts().to_string())
    print(f"Сохранено: {a.out}")


def run_enrich(a):
    """Дописывает штрихкоды в базе для строк, где известен только NTIN (например, 1С Байерсдорф)."""
    base = load_base(a.base)
    need = sorted(set(base.loc[base["gtin"] == "", "ntin"]))
    print(f"Строк без штрихкода: {len(need)}")
    items, _ = query_catalog(need)
    gtin_of = {}
    for it in items:
        n, g = norm(it.get("ntin_code")), digits(it.get("gtin"))
        if n and g and not it.get("ntin_isdeactivated"):
            gtin_of[n] = g
    mask = base["gtin"] == ""
    base.loc[mask, "gtin"] = base.loc[mask, "ntin"].map(lambda n: gtin_of.get(norm(n), ""))
    filled = int((base.loc[mask, "gtin"] != "").sum())
    base.to_csv(a.base_out, index=False, encoding="utf-8")
    print(f"Штрихкод найден для {filled} из {mask.sum()} строк. Сохранено: {a.base_out}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", help="Excel или CSV со списком товаров")
    ap.add_argument("--col", help="название колонки со штрихкодом")
    ap.add_argument("--out", default="результат.xlsx")
    ap.add_argument("--base", default=DEFAULT_BASE, help="путь к base_ntin.csv")
    ap.add_argument("--offline", action="store_true", help="не обращаться к каталогу, использовать только базу")
    ap.add_argument("--debug", action="store_true", help="напечатать сырой ответ первой пачки и остановиться")
    ap.add_argument("--enrich-base", action="store_true", help="дописать штрихкоды в базе по NTIN")
    ap.add_argument("--base-out", default=os.path.join(HERE, "base_ntin_enriched.csv"))
    a = ap.parse_args(argv)
    if a.enrich_base:
        return run_enrich(a)
    if not a.file or not a.col:
        ap.error("укажите файл и --col (или используйте --enrich-base)")
    run_check(a)


if __name__ == "__main__":
    main()
