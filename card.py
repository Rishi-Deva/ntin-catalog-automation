#!/usr/bin/env python3
"""
Сборка заявки на базовую карточку товара (пока только ПОКАЗ, ничего не отправляется).

  python card.py товары.json            показать собранные заявки и найденные проблемы
  python card.py товары.json --out payloads.json   сохранить готовые тела запросов

Файл товаров: один объект или список. Поля товара:
  oktru        код КОНЕЧНОЙ категории ОКТРУ (python oktru.py search ...)
  oktru_search слова для автоподбора категории вместо кода (например «накладные ногти»)
  oktru_prefix раздел для автоподбора, например 1088 (красота)
  kind         вид товара: «Заколка для волос»
  brand        бренд (если нет, будет «СТМ» = собственная торговая марка)
  article      модель или артикул (необязательно)
  features     ключевые характеристики: «пластик, 5 см, чёрный»
  pack         фасовка: «12 шт.» (необязательно)
  name_kk      название на казахском (если не задано, копируется русское, с предупреждением)
  country      код страны из справочника COUNTRY (например CN)
  manufacturer наименование производителя (обязательно в НКТ)
  tnved        код ТН ВЭД (10 цифр)
  unit         код единицы измерения из MEASURE_UNIT (штука = 796)
  quantity     количество в этой единице (например 1)
  gtin         штрихкод (необязательно)
  pack_qty     количество в упаковке (необязательно)
  package      код из справочника PACKAGE (необязательно)
  auto_publish true, чтобы одобренная заявка публиковалась сама (по умолчанию false)
  photos       {"front": "путь.jpg", "back": "путь.jpg"} фото упаковки (необязательно, до 5 МБ)

Формат названия по требованию НКТ:
  [Вид товара] [Бренд] [Модель/Артикул] / [Ключевые характеристики] [Фасовка]
"""
import argparse
import json
import re
import sys

import dicts
import oktru

LIMITS = {"name_ru": 2000, "name_kk": 2000, "short_name_ru": 255, "a4282e5d": 200, "article": 500}
GTIN_RE = re.compile(r"^(\d{8}|\d{12}|\d{13}|\d{14})$")
REQUIRED = ["kind", "oktru", "country", "manufacturer", "tnved", "unit", "quantity"]


def clean(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def compose_names(p):
    kind, brand = clean(p.get("kind")), clean(p.get("brand")) or "СТМ"
    article, feats, pack = clean(p.get("article")), clean(p.get("features")), clean(p.get("pack"))
    head = " ".join(x for x in (kind, brand, article) if x)
    tail = " ".join(x for x in (feats, pack) if x)
    name_ru = f"{head} / {tail}" if tail else head
    short = " ".join(x for x in (kind, brand) if x)
    return name_ru, short


def resolve_oktru(p, db):
    """Если код категории не задан, ищет его по словам oktru_search (и необязательно oktru_prefix)."""
    if p.get("oktru") or not p.get("oktru_search"):
        return None
    rows = oktru.search(db, p["oktru_search"], limit=6, prefix=p.get("oktru_prefix"))
    full = [r for r in rows if r[2] == r[3]]
    if len(full) > 1:
        want = " ".join(p["oktru_search"].lower().split())
        exact = [r for r in full if " ".join(r[1].rsplit(" > ", 1)[-1].lower().split()) == want]
        if len(exact) == 1:
            full = exact
    if len(full) == 1:
        p["oktru"] = full[0][0]
        return f"категория выбрана автоматически: {full[0][0]}  {full[0][1].rsplit(' > ', 1)[-1]}"
    if not full:
        return "ОШИБКА: категория по словам «%s» не найдена" % p["oktru_search"]
    opts = "; ".join(f"{c} {pth.rsplit(' > ', 1)[-1]}" for c, pth, _, _ in full)
    return f"ОШИБКА: по словам «{p['oktru_search']}» подходит несколько категорий, впишите нужный код в поле oktru: {opts}"


def build(p, db):
    """Возвращает (тело запроса, ошибки, предупреждения)."""
    errors, warns = [], []
    note = resolve_oktru(p, db)
    if note and note.startswith("ОШИБКА"):
        return None, [note[len("ОШИБКА: "):]], warns
    if note:
        warns.append(note)
    for k in REQUIRED:
        if p.get(k) in (None, ""):
            errors.append(f"не заполнено поле «{k}»")
    if errors:
        return None, errors, warns

    name_ru, short = compose_names(p)
    name_kk = clean(p.get("name_kk"))
    if not name_kk:
        name_kk = name_ru
        warns.append("название на казахском скопировано с русского (так делают другие продавцы, но лучше перевести)")
    if not clean(p.get("brand")):
        warns.append("бренда нет: в названии и поле бренда указано «СТМ»")

    # количество
    try:
        qty = float(str(p["quantity"]).replace(",", "."))
        if qty <= 0:
            raise ValueError
    except ValueError:
        errors.append("quantity должно быть числом больше нуля")
        qty = None

    # длины
    for field, text in (("name_ru", name_ru), ("name_kk", name_kk), ("short_name_ru", short),
                        ("a4282e5d", clean(p.get("manufacturer"))), ("article", clean(p.get("article")))):
        if len(text) > LIMITS[field]:
            errors.append(f"{field}: {len(text)} знаков при максимуме {LIMITS[field]}")

    # штрихкод
    gtin = re.sub(r"\D", "", str(p.get("gtin") or ""))
    if p.get("gtin") and not GTIN_RE.match(gtin):
        errors.append(f"gtin «{p.get('gtin')}»: допустимо 8, 12, 13 или 14 цифр")
    if gtin and dicts and gtin.zfill(13)[0] == "2" and len(gtin) in (12, 13):
        warns.append("штрихкод похож на внутренний магазинный (начинается с 2): не используйте его для поиска чужих карточек")

    # коды справочников
    for field, dname in (("country", "COUNTRY"), ("unit", "MEASURE_UNIT"), ("tnved", "TNVED"), ("package", "PACKAGE")):
        if not p.get(field):
            continue
        if not dicts.is_loaded(db, dname):
            warns.append(f"справочник {dname} не скачан, код «{p[field]}» не проверен")
        elif not dicts.get(db, dname, p[field]):
            errors.append(f"кода «{p[field]}» нет в справочнике {dname}")
    if re.sub(r"\D", "", str(p["tnved"])) != str(p["tnved"]) or len(str(p["tnved"])) != 10:
        errors.append("код ТН ВЭД должен состоять из 10 цифр")

    # категория
    row = db.execute("SELECT has_child, is_deleted FROM oktru WHERE code=?", (p["oktru"],)).fetchone()
    if row is None:
        errors.append(f"категории ОКТРУ «{p['oktru']}» нет в базе (python oktru.py download)")
    elif row[0]:
        errors.append("категория не конечная: нужна категория последнего уровня")
    elif row[1]:
        errors.append("категория удалена из справочника")

    if errors:
        return None, errors, warns

    attrs = [
        ("name_ru", name_ru), ("name_kk", name_kk), ("short_name_ru", short),
        ("country", p["country"]), ("measure_unit", p["unit"]),
        ("quantity", f"{qty:g}"), ("tnved", str(p["tnved"])),
        ("a4282e5d", clean(p["manufacturer"])), ("brand", clean(p.get("brand")) or "СТМ"),
    ]
    if clean(p.get("article")):
        attrs.append(("article", clean(p["article"])))
    if gtin:
        attrs.append(("gtin", gtin))
    if p.get("pack_qty"):
        attrs.append(("a0238c5b", str(p["pack_qty"])))
    if p.get("package"):
        attrs.append(("paket", str(p["package"])))
    body = {"oktru": p["oktru"], "autoPublication": bool(p.get("auto_publish", False)),
            "attributes": [{"code": c, "value": v} for c, v in attrs]}
    if p.get("photos"):
        body["_photos"] = p["photos"]       # служебное поле, на сервер не уходит
    return body, errors, warns


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    data = json.load(open(a.file, encoding="utf-8"))
    items = data if isinstance(data, list) else [data]
    db = dicts.connect()
    ready = []
    for n, p in enumerate(items, 1):
        body, errors, warns = build(p, db)
        print(f"\n=== товар {n}: {clean(p.get('kind'))} {clean(p.get('brand'))} {clean(p.get('article'))}")
        for e in errors:
            print("  ОШИБКА:", e)
        for w in warns:
            print("  внимание:", w)
        if body:
            print("  название:", next(x["value"] for x in body["attributes"] if x["code"] == "name_ru"))
            print("  коротко: ", next(x["value"] for x in body["attributes"] if x["code"] == "short_name_ru"))
            print("  готово к отправке (отправка пока отключена)")
            ready.append(body)
    print(f"\nИтог: готово {len(ready)} из {len(items)}")
    if a.out:
        json.dump(ready, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("Сохранено:", a.out)


if __name__ == "__main__":
    main()
