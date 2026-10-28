#!/usr/bin/env python3
"""
Проверка базы кодов по Национальному каталогу: сколько кодов активны, сколько отключены,
сколько каталог не знает и у скольких в каталоге есть штрихкод. Базу НЕ меняет (только читает каталог).

  python audit_base.py                      проверить base_ntin.csv, итог по источникам
  python audit_base.py --out base_audit.csv подробный файл по каждому коду (по умолчанию base_audit.csv)
"""
import argparse
import os
import re

import pandas as pd

import check_ntin as ck

HERE = os.path.dirname(os.path.abspath(__file__))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=os.path.join(HERE, "base_ntin.csv"))
    ap.add_argument("--out", default=os.path.join(HERE, "base_audit.csv"))
    a = ap.parse_args(argv)

    base = pd.read_csv(a.base, dtype=str).fillna("")
    ntins = sorted(set(base["ntin"]))
    print(f"Кодов в базе: {len(ntins)}. Запрашиваю каталог (по 100 за раз, это займёт несколько минут)...")
    items, _ = ck.query_catalog(ntins)
    items = items or []
    if not items:
        raise SystemExit("Каталог ничего не вернул (нет связи или ключ не работает). База не тронута, попробуйте позже.")

    by_n = {}
    for it in items:
        n = ck.norm(it.get("ntin_code"))
        if n:
            by_n[n] = it

    rows = []
    for r in base.itertuples():
        it = by_n.get(ck.norm(r.ntin))
        if it is None:
            status, g = "нет в каталоге", ""
        else:
            status = "отключён" if it.get("ntin_isdeactivated") else "активен"
            g = ck.digits(it.get("gtin")) or ""
        rows.append({"ntin": r.ntin, "name": r.name, "source": re.sub(r"\s*\(\d\d\.\d\d\.\d{4}\)", "", r.source),
                     "status": status, "gtin_in_catalog": g, "gtin_in_base": r.gtin})
    df = pd.DataFrame(rows).drop_duplicates("ntin")
    df.to_csv(a.out, index=False, encoding="utf-8")

    df["штрихкод в каталоге"] = df["gtin_in_catalog"] != ""
    t = df.groupby("source").apply(lambda x: pd.Series({
        "кодов": len(x),
        "активны": int((x.status == "активен").sum()),
        "отключены": int((x.status == "отключён").sum()),
        "нет в каталоге": int((x.status == "нет в каталоге").sum()),
        "активны со штрихкодом": int(((x.status == "активен") & x["штрихкод в каталоге"]).sum()),
    }))
    print()
    print(t.to_string())
    print()
    print(f"ИТОГО кодов: {len(df)}; активны: {int((df.status == 'активен').sum())}; "
          f"отключены: {int((df.status == 'отключён').sum())}; нет в каталоге: {int((df.status == 'нет в каталоге').sum())}")
    print(f"Подробности по каждому коду: {a.out}")


if __name__ == "__main__":
    main()
