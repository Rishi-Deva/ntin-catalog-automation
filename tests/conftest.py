"""Общие заготовки для тестов: временная база со справочниками, своя база кодов, подменный каталог.

Тесты не ходят в интернет: каталог, ИИ и Telegram подменяются заглушками.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import check_ntin as ck  # noqa: E402
import dicts  # noqa: E402
import oktru  # noqa: E402


def gtin_with_check(body):
    """Добавляет к цифрам правильную контрольную цифру GTIN."""
    d = [int(x) for x in body]
    s = sum(d[-1::-2]) * 3 + sum(d[-2::-2])
    return body + str((10 - s % 10) % 10)


# Настоящие штрихкоды с правильной контрольной цифрой
NIVEA = "4005808588794"
AVON = "5059018202727"
OTHER = gtin_with_check("460123456789")      # 13 цифр
INTERNAL = gtin_with_check("200000000001")    # магазинный (начинается с 2)


@pytest.fixture
def db(tmp_path):
    """База SQLite с маленькими справочниками ОКТРУ, стран, единиц и ТН ВЭД."""
    con = dicts.connect(str(tmp_path / "nkt.db"))
    oktru_rows = [
        # id, parent, code, level, has_child, name_ru, name_kk, is_deleted, updated
        (1, None, "1088", 1, 1, "Товары для красоты и личной гигиены", None, 0, None),
        (2, 1, "1088-0001", 2, 1, "Косметика", None, 0, None),
        (3, 2, "1088-0001-0001", 3, 0, "Бальзамы для тела", None, 0, None),
        (4, 2, "1088-0001-0002", 3, 0, "Накладные ногти", None, 0, None),
        (5, 2, "1088-0001-0003", 3, 0, "Прочие масла косметические", None, 0, None),
        (6, None, "2000", 1, 1, "Строительство", None, 0, None),
        (7, 6, "2000-0001", 2, 0, "Эстакады открытые кабельные", None, 0, None),
        (8, None, "3000", 1, 1, "Электроника", None, 0, None),
        (9, 8, "3000-0001", 2, 0, "Кабели интерфейсные USB", None, 0, None),
        (10, 2, "1088-0001-0099", 3, 0, "Удалённая категория", None, 1, None),
    ]
    con.executemany("INSERT INTO oktru(id,parent_id,code,level,has_child,name_ru,name_kk,is_deleted,updated)"
                    " VALUES(?,?,?,?,?,?,?,?,?)", oktru_rows)
    oktru.build_paths(con)

    def item(i, parent, code, has_child, name, deleted=False):
        return {"id": i, "parentId": parent, "code": code, "level": 1, "hasChild": has_child,
                "isDeleted": deleted, "properties": {"nameRu": name, "nameKk": None}}

    country = [item(1, None, "CN", False, "Китай"), item(2, None, "RU", False, "Россия"),
               item(3, None, "PL", False, "Польша")]
    unit = [item(1, None, "796", False, "Штука")]
    tnved = [item(1, None, "33", True, "Эфирные масла и косметические средства"),
             item(2, 1, "3304", True, "Косметические средства для ухода за кожей"),
             item(3, 2, "3304990000", False, "прочие"),
             item(4, None, "85", True, "Электрические машины"),
             item(5, 4, "8544", True, "Провода и кабели изолированные"),
             item(6, 5, "8544429000", False, "прочие с соединителями")]
    for name, items in (("COUNTRY", country), ("MEASURE_UNIT", unit), ("TNVED", tnved)):
        con.executemany("INSERT OR REPLACE INTO dict_items VALUES(?,?,?,?,?,?,?,?,?,?)",
                        [dicts.row_of(name, it) for it in items])
    con.commit()
    yield con
    con.close()


@pytest.fixture
def base_csv(tmp_path):
    """Наша база готовых кодов (как base_ntin.csv)."""
    p = tmp_path / "base_ntin.csv"
    p.write_text("ntin,gtin,name,oktru,source\n"
                 f"0200212724569,{NIVEA},Лосьон после бритья NIVEA 100 мл,,прайс\n"
                 "0200000000017,,Помада губная Avon красная,,avon\n"
                 f"0200000000024,{OTHER},Кабель USB Type-C 1 м,,прайс\n", encoding="utf-8")
    return str(p)


class FakeCatalog:
    """Подмена check_ntin.fetch: карточки по штрихкоду и по коду NTIN, счётчик запросов."""

    def __init__(self, cards=None, down=False):
        self.cards, self.down, self.calls = list(cards or []), down, 0

    def __call__(self, codes, debug=False):
        self.calls += 1
        if self.down:
            return None
        keys = {ck.norm(c) for c in codes}
        return [c for c in self.cards if ck.norm(c.get("gtin")) in keys or ck.norm(c.get("ntin_code")) in keys]


@pytest.fixture
def catalog(monkeypatch):
    cat = FakeCatalog()
    monkeypatch.setattr(ck, "fetch", cat)
    return cat
