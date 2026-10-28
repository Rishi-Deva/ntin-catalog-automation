#!/usr/bin/env python3
"""
Добавление прайса поставщика с кодами NTIN в нашу базу base_ntin.csv.

  python import_supplier.py файл.xlsx --source "L'Oréal"            добавить в базу
  python import_supplier.py файл.xlsx --source "L'Oréal" --dry-run  только показать, что будет добавлено

Что делает:
  - сам находит строку заголовков и колонки (NTIN, штрихкод, название);
  - возвращает потерянный Excel-ом ведущий ноль в NTIN (12 цифр -> 13);
  - не добавляет то, что уже есть в базе (по NTIN), и сообщает о конфликтах по штрихкоду;
  - перед записью делает резервную копию базы рядом с ней.

Принимает Excel и CSV. Если в файле есть колонка source, подпись источника берётся из неё для каждой строки.
Если колонки найдены неверно, укажите их буквами: --ntin-col H --name-col B --gtin-col D
После добавления у новых строк обычно нет штрихкода. Чтобы бот находил их по штрихкоду:
  python check_ntin.py --enrich-base      (создаст base_ntin_enriched.csv со штрихкодами из каталога)
"""
import argparse
import os
import re
import shutil
import sys
from datetime import datetime

import openpyxl
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
COLS = ["ntin", "gtin", "name", "oktru", "source"]


class CsvSheet:
    """CSV читается так, будто это лист Excel (для поиска колонок и цикла по строкам)."""
    title = "CSV"

    def __init__(self, path):
        import csv
        with open(path, encoding="utf-8-sig", newline="") as f:
            self.rows = [tuple(r) for r in csv.reader(f)]

    def iter_rows(self, min_row=1, max_row=None, values_only=True):
        return iter([tuple(c if c != "" else None for c in r) for r in self.rows[min_row - 1:max_row]])


def col_index(letter):
    return openpyxl.utils.column_index_from_string(letter.upper()) - 1


def find_columns(ws, args):
    """Строка заголовков и номера колонок (с нуля). Возвращает (номер строки заголовков, ntin, gtin, name, запасное имя)."""
    header_row = None
    for i, row in enumerate(ws.iter_rows(min_row=1, max_row=40, values_only=True), 1):
        if any(isinstance(c, str) and "ntin" in c.lower() for c in row):
            header_row, headers = i, [str(c or "").strip().lower() for c in row]
            break
    if header_row is None:
        sys.exit("Не нашёл строку заголовков с колонкой NTIN. Укажите колонки: --ntin-col H --name-col B")

    def find(pred):
        for j, h in enumerate(headers):
            if pred(h):
                return j
        return None

    ntin = col_index(args.ntin_col) if args.ntin_col else find(lambda h: "ntin" in h)
    gtin = col_index(args.gtin_col) if args.gtin_col else find(lambda h: "gtin" in h or "штрих" in h or h in ("ean", "ean13"))
    name = col_index(args.name_col) if args.name_col else find(
        lambda h: h in ("номенклатура", "наименование", "название", "наименование товара", "товар", "name"))
    if name is None:
        name = find(lambda h: ("наименование" in h or "название" in h or "номенклатура" in h) and "каз" not in h)
    # запасной вариант названия (в некоторых строках основной колонки пусто, а название лежит в соседней)
    fallback = find(lambda h: ("наименование" in h or "название" in h) and j_ne(h, headers, name))
    return header_row, ntin, gtin, name, fallback


def j_ne(h, headers, name):
    return name is None or headers.index(h) != name


def clean_ntin(v):
    if v is None:
        return ""
    s = str(int(v)) if isinstance(v, (int, float)) and float(v).is_integer() else re.sub(r"\D", "", str(v))
    s = re.sub(r"\D", "", s)
    if len(s) == 12:
        s = "0" + s                       # Excel отрезает ведущий ноль
    return s if len(s) == 13 else ""


def clean_gtin(v):
    if v is None:
        return ""
    s = str(int(v)) if isinstance(v, (int, float)) and float(v).is_integer() else re.sub(r"\D", "", str(v))
    s = re.sub(r"\D", "", s)
    return s if len(s) in (8, 12, 13, 14) else ""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--source", help="как подписать источник в базе, например L'Oréal")
    ap.add_argument("--base", default=os.path.join(HERE, "base_ntin.csv"))
    ap.add_argument("--sheet")
    ap.add_argument("--ntin-col")
    ap.add_argument("--gtin-col")
    ap.add_argument("--name-col")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    if a.file.lower().endswith(".csv"):
        ws = CsvSheet(a.file)
    else:
        wb = openpyxl.load_workbook(a.file, data_only=True)
        ws = wb[a.sheet] if a.sheet else wb.active
    header_row, c_ntin, c_gtin, c_name, c_fallback = find_columns(ws, a)
    letter = openpyxl.utils.get_column_letter
    print(f"Лист «{ws.title}», заголовки в строке {header_row}. Колонки: NTIN={letter(c_ntin + 1)}"
          f", штрихкод={letter(c_gtin + 1) if c_gtin is not None else 'нет'}"
          f", название={letter(c_name + 1) if c_name is not None else 'нет'}"
          f"{', запасное название=' + letter(c_fallback + 1) if c_fallback is not None else ''}")

    source = a.source or os.path.splitext(os.path.basename(a.file))[0]
    source = f"{source} ({datetime.now().strftime('%d.%m.%Y')})"
    head = next(ws.iter_rows(min_row=header_row, max_row=header_row, values_only=True))
    c_source = next((j for j, h in enumerate(head) if str(h or "").strip().lower() == "source"), None)
    rows, bad = [], []
    for r in ws.iter_rows(min_row=header_row + 1, values_only=True):
        if all(c is None for c in r):
            continue
        ntin = clean_ntin(r[c_ntin] if c_ntin < len(r) else None)
        if not ntin:
            bad.append(r)
            continue
        name = str(r[c_name]).strip() if c_name is not None and c_name < len(r) and r[c_name] else ""
        if not name and c_fallback is not None and c_fallback < len(r) and r[c_fallback]:
            name = str(r[c_fallback]).strip()
        gtin = clean_gtin(r[c_gtin]) if c_gtin is not None and c_gtin < len(r) else ""
        rows.append({"ntin": ntin, "gtin": gtin, "name": re.sub(r"\s+", " ", name), "oktru": "", "source": (str(r[c_source]).strip() if c_source is not None and c_source < len(r) and r[c_source] else source)})

    new = pd.DataFrame(rows, columns=COLS)
    in_file_dups = int(new.duplicated("ntin").sum())
    new = new.drop_duplicates("ntin")

    if os.path.exists(a.base):
        base = pd.read_csv(a.base, dtype=str).fillna("")
        for c in COLS:
            if c not in base.columns:
                base[c] = ""
        base = base[COLS]
    else:
        base = pd.DataFrame(columns=COLS)

    known = set(base["ntin"])
    fresh = new[~new["ntin"].isin(known)]
    already = new[new["ntin"].isin(known)]

    # у уже известных кодов можно дописать штрихкод, если его не было
    filled = 0
    if not a.dry_run:
        gmap = dict(zip(already["ntin"], already["gtin"]))
        for i, row in base.iterrows():
            g = gmap.get(row["ntin"], "")
            if g and not row["gtin"]:
                base.at[i, "gtin"] = g
                filled += 1

    # конфликты: один штрихкод, разные NTIN
    gt = base[base["gtin"] != ""].groupby("gtin")["ntin"].apply(set).to_dict()
    conflicts = [(r.gtin, r.ntin, sorted(gt[r.gtin])) for r in fresh.itertuples() if r.gtin and r.gtin in gt and r.ntin not in gt[r.gtin]]

    print(f"\nПрочитано строк с кодом: {len(rows)}  (пропущено строк без корректного NTIN: {len(bad)}, повторов внутри файла: {in_file_dups})")
    print(f"Уже есть в базе: {len(already)}   Новых: {len(fresh)}   Дописано штрихкодов к старым кодам: {filled}")
    print(f"Конфликтов (один штрихкод, разные NTIN): {len(conflicts)}")
    for g, n, others in conflicts[:5]:
        print(f"   {g}: новый {n}, в базе {others}")
    print("Примеры новых:")
    for r in fresh.head(5).itertuples():
        print(f"   {r.ntin}  {r.name[:60]}")

    if a.dry_run:
        print("\nЭто был пробный показ, база не изменена. Уберите --dry-run, чтобы добавить.")
        return
    if os.path.exists(a.base):
        backup = a.base.replace(".csv", f"_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        shutil.copy2(a.base, backup)
        print(f"\nРезервная копия базы: {backup}")
    out = pd.concat([base, fresh], ignore_index=True)
    out.to_csv(a.base, index=False, encoding="utf-8")
    print(f"База обновлена: {len(base)} -> {len(out)} кодов ({a.base})")
    nog = int((fresh["gtin"] == "").sum())
    if nog:
        print(f"У {nog} новых кодов нет штрихкода. Следующий шаг: python check_ntin.py --enrich-base")


if __name__ == "__main__":
    main()
