#!/usr/bin/env python3
"""
Очистка базы кодов по результату проверки audit_base.py (в каталог НЕ обращается, работает по base_audit.csv).

  python clean_base.py            создать base_ntin_clean.csv (рабочая база) и base_ntin_inactive.csv (убранные коды)

Что делает:
  - оставляет только активные в каталоге коды (отключённые и неизвестные каталогу убирает: их нельзя ставить в чек);
  - дописывает штрихкоды из каталога там, где в базе их не было;
  - убранные коды складывает в base_ntin_inactive.csv вместе со штрихкодами из каталога
    (по ним можно найти действующие коды-заменители: python check_ntin.py base_ntin_inactive.csv --col gtin_in_catalog);
  - исходный base_ntin.csv НЕ меняет: вы сами подменяете его, когда убедитесь, что всё в порядке.
"""
import argparse
import os

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
COLS = ["ntin", "gtin", "name", "oktru", "source"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=os.path.join(HERE, "base_ntin.csv"))
    ap.add_argument("--audit", default=os.path.join(HERE, "base_audit.csv"))
    ap.add_argument("--out", default=os.path.join(HERE, "base_ntin_clean.csv"))
    ap.add_argument("--inactive-out", default=os.path.join(HERE, "base_ntin_inactive.csv"))
    a = ap.parse_args(argv)

    base = pd.read_csv(a.base, dtype=str).fillna("")
    for c in COLS:
        if c not in base.columns:
            base[c] = ""
    audit = pd.read_csv(a.audit, dtype=str).fillna("")
    status = dict(zip(audit["ntin"], audit["status"]))
    gcat = dict(zip(audit["ntin"], audit["gtin_in_catalog"]))

    # дубли кода в базе: оставляем строку со штрихкодом
    base = base.assign(_g=(base["gtin"] != "")).sort_values("_g", ascending=False, kind="stable").drop(columns="_g")
    base = base.drop_duplicates("ntin")

    st = base["ntin"].map(status).fillna("не проверен")
    keep = base[st.isin(["активен", "не проверен"])].copy()      # непроверенные (нет в аудите) не трогаем
    gone = base[~st.isin(["активен", "не проверен"])].copy()

    need = keep["gtin"] == ""
    keep.loc[need, "gtin"] = keep.loc[need, "ntin"].map(lambda n: gcat.get(n, ""))
    filled = int((keep.loc[need, "gtin"] != "").sum())

    gone["status"] = gone["ntin"].map(status)
    gone["gtin_in_catalog"] = gone["ntin"].map(lambda n: gcat.get(n, ""))
    gone[["ntin", "gtin_in_catalog", "name", "source", "status"]].to_csv(a.inactive_out, index=False, encoding="utf-8")
    keep[COLS].to_csv(a.out, index=False, encoding="utf-8")

    print(f"Было кодов: {len(base)}")
    print(f"Оставлено (активные в каталоге): {len(keep)}; из них со штрихкодом: {int((keep['gtin'] != '').sum())} (дописано из каталога: {filled})")
    print(f"Убрано: {len(gone)} (отключены: {int((gone['status'] == 'отключён').sum())}, нет в каталоге: {int((gone['status'] == 'нет в каталоге').sum())})")
    print(f"  из убранных со штрихкодом в каталоге: {int((gone['gtin_in_catalog'] != '').sum())}")
    print("\nПо источникам (оставлено / убрано):")
    s1 = keep["source"].str.replace(r"\s*\(\d\d\.\d\d\.\d{4}\)", "", regex=True).value_counts()
    s2 = gone["source"].str.replace(r"\s*\(\d\d\.\d\d\.\d{4}\)", "", regex=True).value_counts()
    for src in sorted(set(s1.index) | set(s2.index)):
        print(f"  {src}: {int(s1.get(src, 0))} / {int(s2.get(src, 0))}")
    print(f"\nГотово: {a.out}\nУбранные коды (для поиска замен): {a.inactive_out}")


if __name__ == "__main__":
    main()
