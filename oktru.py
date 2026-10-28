#!/usr/bin/env python3
"""
Справочник категорий ОКТРУ в локальной базе SQLite (nkt.db).

  python oktru.py download [--max-pages 3]   скачать справочник (можно прерывать и продолжать)
  python oktru.py stats                       сколько записей и до какого уровня
  python oktru.py search "гель для душа"      найти конечные категории по словам
  python oktru.py search "шампунь" --prefix 1088   искать только в разделе «красота и личная гигиена»
  python oktru.py reindex                     перестроить индекс поиска (после обновления программы)

Для заявки нужен код КОНЕЧНОЙ категории (hasChild = false, уровень 4),
например 1113-0012-0024-100100228.
"""
import argparse
import os
import re
import sqlite3
import sys

from nkt_client import NktClient

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "nkt.db")


def connect(path=DB):
    db = sqlite3.connect(path, timeout=30)       # ждать, если файл занят другой программой
    db.execute("PRAGMA journal_mode=WAL")          # чтение и запись не мешают друг другу
    db.executescript("""
        CREATE TABLE IF NOT EXISTS oktru(
            id INTEGER PRIMARY KEY, parent_id INTEGER, code TEXT, level INTEGER,
            has_child INTEGER, name_ru TEXT, name_kk TEXT, is_deleted INTEGER,
            updated TEXT, path_ru TEXT, search TEXT);
        CREATE INDEX IF NOT EXISTS ix_oktru_code ON oktru(code);
        CREATE INDEX IF NOT EXISTS ix_oktru_parent ON oktru(parent_id);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
    """)
    return db


def meta_get(db, key, default=None):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def meta_set(db, key, value):
    db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, str(value)))


def row_of(item):
    p = item.get("properties") or {}
    return (item["id"], item.get("parentId"), item.get("code") or p.get("code"), item.get("level"),
            int(bool(item.get("hasChild"))), p.get("nameRu"), p.get("nameKk"),
            int(bool(item.get("isDeleted"))), item.get("updatedDate"))


def download(client, db, max_pages=None, size=100):
    start = int(meta_get(db, "oktru_next_page", 1))
    first = client.dictionary_items("OKTRU", page=start, size=size)
    total_pages = int(first.get("totalPages", 0))
    print(f"Всего записей: {first.get('totalElements')}, страниц: {total_pages}. Начинаю со страницы {start}.")
    page, data, done = start, first, 0
    while True:
        db.executemany("INSERT OR REPLACE INTO oktru(id,parent_id,code,level,has_child,name_ru,name_kk,is_deleted,updated)"
                       " VALUES(?,?,?,?,?,?,?,?,?)", [row_of(i) for i in data.get("content", [])])
        meta_set(db, "oktru_next_page", page + 1)
        db.commit()
        done += 1
        if page % 20 == 0 or page == total_pages:
            print(f"  страница {page}/{total_pages}")
        if page >= total_pages or (max_pages and done >= max_pages):
            break
        page += 1
        data = client.dictionary_items("OKTRU", page=page, size=size)
    if page >= total_pages:
        meta_set(db, "oktru_done", 1)
        db.commit()
        build_paths(db)
        print("Готово.")
    else:
        print(f"Остановлено после {done} стр. Запустите ту же команду, чтобы продолжить.")


def build_paths(db):
    """Для каждой записи собирает путь «Раздел > Класс > ... > Название» и строку для поиска."""
    nodes = {r[0]: (r[1], r[2] or "") for r in db.execute("SELECT id, parent_id, name_ru FROM oktru")}
    cache = {}

    def path(i):
        if i in cache:
            return cache[i]
        parent, name = nodes[i]
        chain = [name]
        seen = {i}
        while parent in nodes and parent not in seen:
            seen.add(parent)
            chain.append(nodes[parent][1])
            parent = nodes[parent][0]
        cache[i] = " > ".join(reversed(chain))
        return cache[i]

    db.executemany("UPDATE oktru SET path_ru=?, search=? WHERE id=?",
                   [(path(i), " " + re.sub(r"[^\w]+", " ", path(i).lower()) + " ", i) for i in nodes])
    db.commit()


STOP = {"для", "и", "с", "со", "на", "из", "в", "по", "от", "без", "к", "при", "или", "не"}


def stem(w):
    """Основа слова: «резинка» -> «резинк», «гель» -> «гел», «шампунь» -> «шампун».
    Короткие слова (до 3 букв) ищутся только целиком."""
    if len(w) <= 3:
        return w, True
    base = w[:-1] if w[-1] in "аяыиеоуюьй" else w
    if len(base) > 8:
        base = base[:-1]
    return base, False


def search(db, query, limit=15, prefix=None):
    """Возвращает [(код, путь, совпало слов, всего слов)].
    Слово должно НАЧИНАТЬСЯ с основы (так «резин» не найдётся внутри «прорезиненные»).
    Сначала ищет по всем словам, если пусто, допускает пропуск одного слова.
    prefix: только коды, начинающиеся с этого значения (например 1088 = красота и личная гигиена)."""
    parts = []
    for w in re.findall(r"[\w-]+", query.lower()):
        if w in STOP:
            continue
        base, whole = stem(w)
        parts.append(f"% {base} %" if whole else f"% {base}%")
    if not parts:
        return []
    score = " + ".join("(CASE WHEN (' ' || search) LIKE ? THEN 1 ELSE 0 END)" for _ in parts)
    where = "has_child=0 AND is_deleted=0" + (" AND code LIKE ?" if prefix else "")
    sql = (f"SELECT code, path_ru, score FROM (SELECT code, path_ru, {score} AS score FROM oktru WHERE {where}) "
           "WHERE score>=? ORDER BY score DESC, length(path_ru) LIMIT ?")
    extra = [f"{prefix}%"] if prefix else []
    for need in ([len(parts)] + ([len(parts) - 1] if len(parts) > 1 else [])):
        rows = db.execute(sql, parts + extra + [need, limit]).fetchall()
        if rows:
            return [(c, pth, sc, len(parts)) for c, pth, sc in rows]
    return []


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("download")
    d.add_argument("--max-pages", type=int)
    sub.add_parser("stats")
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=15)
    s.add_argument("--prefix", help="только раздел, например 1088 (красота и личная гигиена)")
    sub.add_parser("reindex")
    a = ap.parse_args(argv)
    db = connect()
    if a.cmd == "download":
        download(NktClient(), db, a.max_pages)
    elif a.cmd == "stats":
        n = db.execute("SELECT COUNT(*) FROM oktru").fetchone()[0]
        leaves = db.execute("SELECT COUNT(*) FROM oktru WHERE has_child=0 AND is_deleted=0").fetchone()[0]
        print(f"Записей: {n}, конечных активных категорий: {leaves}, следующая страница: {meta_get(db, 'oktru_next_page', 1)}, "
              f"полностью скачано: {'да' if meta_get(db, 'oktru_done') else 'нет'}")
        for lvl, c in db.execute("SELECT level, COUNT(*) FROM oktru GROUP BY level ORDER BY level"):
            print(f"  уровень {lvl}: {c}")
    elif a.cmd == "reindex":
        build_paths(db)
        print("Индекс поиска перестроен.")
    else:
        rows = search(db, a.query, a.limit, a.prefix)
        if not rows:
            print("Ничего не найдено (категории скачаны? попробуйте другие слова).")
        for code, path, sc, total in rows:
            note = "" if sc == total else f"  [совпало {sc} из {total} слов]"
            print(f"{code}  {path}{note}")


if __name__ == "__main__":
    main()
