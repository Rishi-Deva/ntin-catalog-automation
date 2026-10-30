#!/usr/bin/env python3
"""
Выгрузка черновиков ИИ-помощника в файл для card.py (карточки, которые сотрудники подтвердили кнопкой «Верно»).

  python export_drafts.py 12                 черновик №12
  python export_drafts.py 12 13              несколько черновиков
  python export_drafts.py --all-ready        все полные черновики (статус «готов»)

Печатает JSON в окно (ничего не меняет). Обычно запускают на ноутбуке через ssh и сохраняют в файл:
  ssh user@server "cd ~/nkt && ./venv/bin/python export_drafts.py 12" > товар.json
Дальше: python card.py товар.json --out payloads.json
"""
import argparse
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def export(db, ids=None, all_ready=False):
    """Возвращает (список описаний товаров для card.py, список замечаний для человека)."""
    if ids:
        marks = ",".join("?" * len(ids))
        rows = db.execute(f"SELECT id, status, spec FROM ai_drafts WHERE id IN ({marks}) ORDER BY id", list(ids)).fetchall()
    elif all_ready:
        rows = db.execute("SELECT id, status, spec FROM ai_drafts WHERE status='ready' ORDER BY id").fetchall()
    else:
        rows = []
    specs, notes = [], []
    found = {r[0] for r in rows}
    for i in ids or []:
        if i not in found:
            notes.append(f"Черновика №{i} нет.")
    for did, status, spec in rows:
        if status not in ("ready", "incomplete"):
            notes.append(f"Черновик №{did} в статусе «{status}»: это не подтверждённая карточка, пропускаю.")
            continue
        if status == "incomplete":
            notes.append(f"Черновик №{did} неполный (не хватает данных): card.py покажет, чего именно.")
        specs.append(json.loads(spec or "{}"))
    return specs, notes


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="*", type=int)
    ap.add_argument("--all-ready", action="store_true")
    ap.add_argument("--db", default=os.path.join(HERE, "nkt.db"))
    a = ap.parse_args(argv)
    if not a.ids and not a.all_ready:
        ap.error("укажите номера черновиков или --all-ready")
    db = sqlite3.connect(a.db)
    specs, notes = export(db, a.ids, a.all_ready)
    for n in notes:
        print(n, file=sys.stderr)
    print(json.dumps(specs, ensure_ascii=False, indent=1))
    print(f"Выгружено карточек: {len(specs)}", file=sys.stderr)
    return 0 if specs else 1


if __name__ == "__main__":
    sys.exit(main())
