#!/usr/bin/env python3
"""
Отправка заявок в НКТ. ПО УМОЛЧАНИЮ НИЧЕГО НЕ ОТПРАВЛЯЕТ: любое действие, меняющее данные,
выполняется только с флагом --go.

  python submit.py create payloads.json              показать, что будет создано (без --go ничего не делает)
  python submit.py create payloads.json --go         создать черновик заявки (и загрузить фото)
  python submit.py create payloads.json --go --moderate   ... и сразу отправить на модерацию
  python submit.py moderate 4125 --go                отправить черновик на модерацию
  python submit.py publish 4125 --go                 опубликовать одобренную заявку (статус readyToPublish), получить NTIN
  python submit.py publish-ready --go                опубликовать ВСЕ одобренные заявки сразу и показать их NTIN
  python submit.py status 4125                       статус, замечания модератора, NTIN
  python submit.py details 4125                      что именно записано в заявке (категория и все поля)
  python submit.py list                              заявки, созданные этой программой
  python submit.py export --out my_ntin.csv          таблица готовых кодов (NTIN, название) для Excel и кассы
  python submit.py duplicates 4125                   похожие карточки (если система нашла дубли)
  python submit.py duplicates-all                    похожие карточки для ВСЕХ заявок «на доработке»
  python submit.py decide 4125 --use-existing 0200... --go   взять готовую карточку
  python submit.py decide 4125 --continue --go       всё равно создавать новую
  python submit.py delete 4125 --go                  удалить заявку (можно только в статусе «новая»)

Защиты: по умолчанию обрабатывается одна заявка за запуск (--limit N, чтобы больше);
повторный запуск не создаёт дублей (заявки запоминаются в nkt.db).
"""
import argparse
import csv
import hashlib
import json
import os
import sys
import time
from datetime import datetime

import oktru
from nkt_client import NktClient, NktError

REQ = "/portal/api/v1/products/requests"
STORAGE = "/portal/api/v1/storage"
IMAGE_TYPES = {"front", "back", "left", "right", "top", "bottom"}
MAX_BYTES = 5 * 1024 * 1024
FINAL = {"completed", "rejected", "cancelled"}


def connect():
    db = oktru.connect()
    db.execute("""CREATE TABLE IF NOT EXISTS requests(
        key TEXT PRIMARY KEY, request_id INTEGER, status TEXT, name_ru TEXT, ntin TEXT, created_at TEXT)""")
    return db


def name_of(body):
    return next((a["value"] for a in body["attributes"] if a["code"] == "name_ru"), "")


def check_photos(photos):
    errs = []
    for kind, path in photos.items():
        if kind not in IMAGE_TYPES:
            errs.append(f"тип фото «{kind}» не из списка {sorted(IMAGE_TYPES)}")
        elif not os.path.isfile(path):
            errs.append(f"файл не найден: {path}")
        elif os.path.getsize(path) > MAX_BYTES:
            errs.append(f"файл больше 5 МБ: {path}")
        elif not path.lower().endswith((".jpg", ".jpeg", ".png")):
            errs.append(f"нужен jpg или png: {path}")
    return errs


def create_one(client, db, body, go):
    body = dict(body)
    photos = body.pop("_photos", None) or {}
    key = hashlib.sha1(json.dumps([body, photos], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    row = db.execute("SELECT request_id FROM requests WHERE key=?", (key,)).fetchone()
    title = name_of(body)
    if row:
        print(f"  уже создана ранее: заявка {row[0]} ({title}), пропускаю")
        return row[0], False
    errs = check_photos(photos)
    if errs:
        for e in errs:
            print("  ОШИБКА фото:", e)
        return None, False
    if not go:
        print(f"  [пробный показ] создал бы заявку: {title}")
        print(f"                 категория {body['oktru']}, полей {len(body['attributes'])}, фото {len(photos)}")
        return None, True
    r = client.post_json(REQ, body)
    rid = client.body(r).get("id")
    if not rid:
        print("  ОШИБКА: сервер не вернул номер заявки:", r.text[:300])
        return None, False
    db.execute("INSERT INTO requests VALUES(?,?,?,?,?,?)",
               (key, rid, "new", title, None, datetime.now().isoformat(timespec="seconds")))
    db.commit()
    print(f"  создан черновик: заявка {rid} ({title})")
    if photos:
        files = []
        for kind, path in photos.items():
            up = client.body(client.upload(f"{STORAGE}/{rid}/upload", path, imageType=kind))
            fname = up.get("filename") or up.get("fileName")
            if not fname:
                print(f"  ОШИБКА: сервер не вернул имя файла для {path}: {up}")
                return rid, True
            files.append({"fileName": fname, "imageType": kind})
            print(f"  фото загружено: {kind} ({os.path.basename(path)})")
        full = dict(body, imageFiles=files, documents=[])
        client.put_json(f"{REQ}/{rid}", full)
        print("  фото прикреплены к заявке")
    return rid, True


def moderate(client, rid, go):
    if not go:
        print(f"  [пробный показ] отправил бы заявку {rid} на модерацию")
        return
    client.put_json(f"{REQ}/{rid}/moderation")
    print(f"  заявка {rid} отправлена на модерацию")


def show_status(client, db, rid, raw=False):
    s = client.get_json(f"{REQ}/{rid}/status")
    if raw:
        print("сырой ответ:", json.dumps(s, ensure_ascii=False)[:2000])
    code = s.get("code")
    print(f"заявка {rid}: {code} ({s.get('value')})")
    if s.get("ntinCode"):
        print(f"  NTIN: {s['ntinCode']}   карточка: {s.get('productUrl')}")
    if s.get("comment"):
        print("  комментарий модератора:", s["comment"])
    for d in s.get("revisionDetails") or []:
        print(f"  замечание по полю {d.get('attributeCode')}: {d.get('comment')} (сейчас: {d.get('value')})")
    if s.get("reason"):
        print("  причина:", s["reason"])
    na = s.get("nextAction")
    if na:
        print(f"  что делать дальше: {na.get('description')} ({na.get('method')} {na.get('endpoint')})")
    pm = s.get("photoModeration")
    if pm:
        print(f"  проверка фото ИИ: {pm.get('score')}% {pm.get('comment') or ''}")
    db.execute("UPDATE requests SET status=?, ntin=COALESCE(?, ntin) WHERE request_id=?",
               (code, s.get("ntinCode"), rid))
    db.commit()
    return s


def print_duplicates(items, top=5):
    if isinstance(items, dict):
        items = items.get("content") or items.get("duplicates") or []
    if not items:
        print("  (список похожих пуст)")
    for x in items[:top]:
        if x.get("ntin") is None and x.get("name") is None:
            print("  ", json.dumps(x, ensure_ascii=False)[:400])
            continue
        print(f"  {x.get('orderNumber')}. {x.get('name')} | бренд {x.get('brand')} | GTIN {x.get('gtin')} | "
              f"NTIN {x.get('ntin')} | совпадение {x.get('similarity')}%")


def print_attr(at, indent=2):
    val = at.get("value")
    if val in (None, ""):
        val = at.get("values") or at.get("multiValues") or ""
    print(" " * indent + f"{at.get('code')} | {at.get('nameRu') or ''}: {val}")
    for sub in at.get("attributes") or []:
        print_attr(sub, indent + 4)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("file")
    c.add_argument("--index", type=int, help="только заявка с этим номером из файла (с 1)")
    c.add_argument("--limit", type=int, default=1, help="сколько заявок обработать за запуск (по умолчанию 1)")
    c.add_argument("--go", action="store_true")
    c.add_argument("--moderate", action="store_true")
    m = sub.add_parser("moderate")
    m.add_argument("id", type=int)
    m.add_argument("--go", action="store_true")
    st = sub.add_parser("status")
    st.add_argument("id", type=int, nargs="?")
    st.add_argument("--raw", action="store_true", help="показать сырой ответ сервера")
    pr = sub.add_parser("publish-ready")
    pr.add_argument("--go", action="store_true")
    pb = sub.add_parser("publish")
    pb.add_argument("id", type=int)
    pb.add_argument("--go", action="store_true")
    dt = sub.add_parser("details")
    dt.add_argument("id", type=int)
    dt.add_argument("--raw", action="store_true", help="дополнительно показать сырой ответ сервера")
    sub.add_parser("list")
    ex = sub.add_parser("export")
    ex.add_argument("--out", default="my_ntin.csv")
    d = sub.add_parser("duplicates")
    d.add_argument("id", type=int)
    d.add_argument("--raw", action="store_true", help="показать сырой ответ сервера")
    da = sub.add_parser("duplicates-all")
    da.add_argument("--top", type=int, default=5)
    dc = sub.add_parser("decide")
    dc.add_argument("id", type=int)
    dc.add_argument("--use-existing", metavar="NTIN")
    dc.add_argument("--continue", dest="cont", action="store_true")
    dc.add_argument("--go", action="store_true")
    de = sub.add_parser("delete")
    de.add_argument("id", type=int)
    de.add_argument("--go", action="store_true")
    a = ap.parse_args(argv)

    db = connect()
    if a.cmd == "export":
        rows = db.execute("SELECT ntin, name_ru, request_id, status FROM requests "
                          "WHERE ntin IS NOT NULL AND status IN ('completed','existingProductSelected') ORDER BY created_at").fetchall()
        with open(a.out, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["NTIN", "Название", "Номер заявки", "Статус"])
            w.writerows(rows)
        print(f"Сохранено {len(rows)} кодов: {a.out} (открывается в Excel)")
        return
    if a.cmd == "list":
        rows = db.execute("SELECT request_id, status, ntin, name_ru, created_at FROM requests ORDER BY created_at").fetchall()
        for rid, status, ntin, name, ts in rows:
            print(f"{rid}  {status or '?':<14} {ntin or '-':<14} {name}  ({ts})")
        print(f"Всего: {len(rows)}")
        return
    client = NktClient()
    if a.cmd == "create":
        items = json.load(open(a.file, encoding="utf-8"))
        items = [(i, b) for i, b in enumerate(items, 1) if not a.index or i == a.index]
        done = 0
        for i, body in items:
            if done >= a.limit:
                print(f"Остановился на лимите {a.limit}. Добавьте --limit N, чтобы обработать больше.")
                break
            print(f"\nзаявка {i} из файла:")
            rid, created = create_one(client, db, body, a.go)
            if rid and a.go and a.moderate and created:
                moderate(client, rid, True)
                time.sleep(1)
                show_status(client, db, rid)
            if created:
                done += 1
        if not a.go:
            print("\nЭто был пробный показ. Для настоящей отправки добавьте --go.")
    elif a.cmd == "moderate":
        moderate(client, a.id, a.go)
    elif a.cmd == "status":
        ids = [a.id] if a.id else [r[0] for r in db.execute("SELECT request_id FROM requests WHERE status IS NULL OR status NOT IN ('completed','rejected','cancelled')")]
        for rid in ids:
            show_status(client, db, rid)
    elif a.cmd == "publish-ready":
        ids = [r[0] for r in db.execute("SELECT request_id FROM requests WHERE status IS NULL OR status NOT IN ('completed','rejected','cancelled')")]
        ready = []
        for rid in ids:
            code = client.get_json(f"{REQ}/{rid}/status").get("code")
            db.execute("UPDATE requests SET status=? WHERE request_id=?", (code, rid))
            if code == "readyToPublish":
                ready.append(rid)
        db.commit()
        print(f"Проверено заявок: {len(ids)}, готовых к публикации: {len(ready)}")
        for rid in ready:
            if a.go:
                client.put_json(f"{REQ}/{rid}/publish")
                print(f"  заявка {rid}: запрос на публикацию отправлен")
            else:
                print(f"  [пробный показ] опубликовал бы заявку {rid}")
        if a.go and ready:
            time.sleep(2)
            for rid in ready:
                show_status(client, db, rid)
    elif a.cmd == "publish":
        if a.go:
            client.put_json(f"{REQ}/{a.id}/publish")
            print(f"заявка {a.id}: запрос на публикацию отправлен")
            time.sleep(1)
            show_status(client, db, a.id)
        else:
            print(f"[пробный показ] опубликовал бы заявку {a.id}")
    elif a.cmd == "details":
        d = client.get_json(f"{REQ}/{a.id}/details")
        path = " > ".join(x.get("nameRu", "") for x in d.get("categoryAncestors") or [])
        print(f"заявка {a.id}: статус {(d.get('status') or {}).get('code')}, тип {(d.get('requestType') or {}).get('code')}")
        print("категория:", path or "-")
        for at in d.get("attributes") or []:
            print_attr(at)
        if a.raw:
            print("\nсырой ответ (первые 4000 знаков):")
            print(json.dumps(d, ensure_ascii=False)[:4000])
        print(f"фото: {len((d.get('imageData') or {}).get('imageFiles') or [])}, документы: {len((d.get('documents') or {}).get('documents') or [])}")
    elif a.cmd == "duplicates":
        data = client.get_json(f"{REQ}/{a.id}/duplicates")
        if a.raw:
            print("сырой ответ (первые 3000 знаков):")
            print(json.dumps(data, ensure_ascii=False)[:3000])
        print_duplicates(data, 20)
    elif a.cmd == "duplicates-all":
        rows = db.execute("SELECT request_id, name_ru FROM requests WHERE status='underRevision' ORDER BY request_id").fetchall()
        for rid, name in rows:
            print(f"\n=== заявка {rid}: {name}")
            try:
                print_duplicates(client.get_json(f"{REQ}/{rid}/duplicates"), a.top)
            except NktError as e:
                print("  не удалось получить список:", e)
        print(f"\nЗаявок на доработке: {len(rows)}")
    elif a.cmd == "decide":
        if bool(a.use_existing) == bool(a.cont):
            sys.exit("Укажите либо --use-existing NTIN, либо --continue")
        body = {"decision": "USE_EXISTING", "ntin": a.use_existing} if a.use_existing else {"decision": "CONTINUE"}
        if a.go:
            client.post_json(f"{REQ}/{a.id}/duplicate-decision", body)
            print("Решение отправлено:", body)
        else:
            print("[пробный показ] отправил бы решение:", body)
    elif a.cmd == "delete":
        if a.go:
            client.delete(f"{REQ}/{a.id}")
            db.execute("DELETE FROM requests WHERE request_id=?", (a.id,))
            db.commit()
            print(f"Заявка {a.id} удалена")
        else:
            print(f"[пробный показ] удалил бы заявку {a.id}")


if __name__ == "__main__":
    try:
        main()
    except NktError as e:
        sys.exit(f"Ошибка: {e}")
