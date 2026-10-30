#!/usr/bin/env python3
"""
Справочники НКТ в локальной базе nkt.db: COUNTRY (страны), MEASURE_UNIT (единицы),
TNVED (ТН ВЭД ЕАЭС), PACKAGE (пакеты) и любые другие по названию.

  python dicts.py download COUNTRY MEASURE_UNIT PACKAGE TNVED   скачать (докачивается при обрыве)
  python dicts.py search COUNTRY китай                          найти код страны
  python dicts.py search MEASURE_UNIT штука                     найти единицу измерения
  python dicts.py search TNVED "заколки для волос"              найти код ТН ВЭД по словам
  python dicts.py search TNVED 9615                             найти код ТН ВЭД по началу цифр
  python dicts.py show PACKAGE                                  показать весь справочник
"""
import argparse
import re
import sqlite3
import sys

import oktru
from nkt_client import NktClient

SCHEMA = """
CREATE TABLE IF NOT EXISTS dict_items(
    dict TEXT, id INTEGER, parent_id INTEGER, code TEXT, level INTEGER, has_child INTEGER,
    name_ru TEXT, name_kk TEXT, is_deleted INTEGER, search TEXT, PRIMARY KEY(dict, id));
CREATE INDEX IF NOT EXISTS ix_dict_code ON dict_items(dict, code);
"""


def connect(path=oktru.DB):
    db = oktru.connect(path)
    db.executescript(SCHEMA)
    return db


def norm_text(s):
    return " " + re.sub(r"[^\w]+", " ", (s or "").lower()) + " "


def row_of(name, it):
    p = it.get("properties") or {}
    return (name, it["id"], it.get("parentId"), it.get("code") or p.get("code"), it.get("level"),
            int(bool(it.get("hasChild"))), p.get("nameRu"), p.get("nameKk"),
            int(bool(it.get("isDeleted"))), norm_text(p.get("nameRu")))


def download(client, db, name, size=100, force=False):
    if force:
        db.execute("DELETE FROM dict_items WHERE dict=?", (name,))
        oktru.meta_set(db, f"dict_{name}_next_page", 1)
        oktru.meta_set(db, f"dict_{name}_done", "")
        db.commit()
    if oktru.meta_get(db, f"dict_{name}_done"):
        print(f"{name}: уже скачан (для повторной загрузки добавьте --force)")
        return
    page = int(oktru.meta_get(db, f"dict_{name}_next_page", 1))
    while True:
        data = client.dictionary_items(name, page=page, size=size)
        total = int(data.get("totalPages", 0))
        db.executemany("INSERT OR REPLACE INTO dict_items VALUES(?,?,?,?,?,?,?,?,?,?)",
                       [row_of(name, i) for i in data.get("content", [])])
        oktru.meta_set(db, f"dict_{name}_next_page", page + 1)
        db.commit()
        if page == 1 or page % 20 == 0 or page >= total:
            print(f"{name}: страница {page}/{total} (всего записей {data.get('totalElements')})")
        if page >= total:
            break
        page += 1
    oktru.meta_set(db, f"dict_{name}_done", 1)
    db.commit()


def get(db, name, code):
    """Строка справочника по коду или None."""
    return db.execute("SELECT code, name_ru FROM dict_items WHERE dict=? AND code=? AND is_deleted=0 LIMIT 1",
                      (name, str(code))).fetchone()


def is_loaded(db, name):
    return bool(db.execute("SELECT 1 FROM dict_items WHERE dict=? LIMIT 1", (name,)).fetchone())


def search(db, name, query, limit=10):
    """[(код, название)]. Цифры ищутся по началу кода, слова по началу слов (с основами)."""
    q = query.strip()
    if re.fullmatch(r"\d+", q):
        rows = db.execute("SELECT code, name_ru FROM dict_items WHERE dict=? AND is_deleted=0 AND has_child=0 "
                          "AND code LIKE ? ORDER BY code LIMIT ?", (name, q + "%", limit * 3)).fetchall()
        return list(dict(rows).items())[:limit] if rows else []
    parts = []
    for w in re.findall(r"[\w-]+", q.lower()):
        if w in oktru.STOP:
            continue
        base, whole = oktru.stem(w)
        parts.append(f"% {base} %" if whole else f"% {base}%")
    if not parts:
        return []
    score = " + ".join("(CASE WHEN (' ' || search) LIKE ? THEN 1 ELSE 0 END)" for _ in parts)
    sql = (f"SELECT code, name_ru, score FROM (SELECT code, name_ru, {score} AS score FROM dict_items "
           "WHERE dict=? AND is_deleted=0 AND has_child=0) WHERE score>=? ORDER BY score DESC, length(name_ru) LIMIT ?")
    for need in ([len(parts)] + ([len(parts) - 1] if len(parts) > 1 else [])):
        rows = db.execute(sql, parts + [name, need, limit * 3]).fetchall()
        if rows:
            seen, out = set(), []
            for code, nm, _ in rows:
                if code not in seen:
                    seen.add(code)
                    out.append((code, nm))
            return out[:limit]
    return []


def leaves_with_paths(db, name):
    """[(код, [названия от группы до кода])] для всех конечных кодов справочника.
    У кодов ТН ВЭД названия без контекста («прочие»), смысл лежит в названиях верхних уровней, поэтому путь собираем по родителям."""
    rows = db.execute("SELECT id, parent_id, code, has_child, name_ru FROM dict_items WHERE dict=? AND is_deleted=0", (name,)).fetchall()
    by_id = {r[0]: r for r in rows}
    out = []
    for _id, parent, code, has_child, nm in rows:
        if has_child:
            continue
        names, cur = [nm or ""], parent
        for _ in range(8):
            if cur not in by_id:
                break
            names.append(by_id[cur][4] or "")
            cur = by_id[cur][1]
        out.append((code, list(reversed(names))))
    return out


def search_paths(db, name, query, limit=10, leaves=None):
    """Поиск конечных кодов по словам из ВСЕГО пути (группа > позиция > код). Возвращает [(код, последние три уровня пути)]."""
    parts = []
    for w in re.findall(r"[\w-]+", query.lower()):
        if w in oktru.STOP:
            continue
        base, whole = oktru.stem(w)
        parts.append(re.compile(r"(?<!\w)" + re.escape(base) + (r"(?!\w)" if whole else "")))
    if not parts:
        return []
    leaves = leaves if leaves is not None else leaves_with_paths(db, name)
    scored = []
    for code, names in leaves:
        text = norm_text(" ".join(names))
        score = sum(1 for rx in parts if rx.search(text))
        scored.append((score, len(text), code, names))
    for need in ([len(parts)] + ([len(parts) - 1] if len(parts) > 1 else [])):
        hits = sorted((x for x in scored if x[0] >= need), key=lambda x: (-x[0], x[1]))
        if hits:
            return [(code, " > ".join(names[-3:])[:220]) for _, _, code, names in hits[:limit]]
    return []


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("download")
    d.add_argument("names", nargs="+")
    d.add_argument("--force", action="store_true")
    s = sub.add_parser("search")
    s.add_argument("name")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=10)
    sh = sub.add_parser("show")
    sh.add_argument("name")
    a = ap.parse_args(argv)
    db = connect()
    if a.cmd == "download":
        client = NktClient()
        for n in a.names:
            download(client, db, n.upper(), force=a.force)
        print("Готово.")
    elif a.cmd == "search":
        if not is_loaded(db, a.name.upper()):
            sys.exit(f"Справочник {a.name.upper()} ещё не скачан: python dicts.py download {a.name.upper()}")
        rows = search(db, a.name.upper(), a.query, a.limit)
        for code, nm in rows:
            print(f"{code}  {nm}")
        if not rows:
            print("Ничего не найдено, попробуйте другие слова.")
    else:
        for code, nm in db.execute("SELECT code, name_ru FROM dict_items WHERE dict=? AND is_deleted=0 ORDER BY code",
                                   (a.name.upper(),)):
            print(f"{code}  {nm}")


if __name__ == "__main__":
    main()
