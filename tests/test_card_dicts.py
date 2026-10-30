"""Справочники (oktru.py, dicts.py) и сборка заявки на карточку (card.py)."""
import pytest

import card
import dicts
import oktru


# ------------------------------------------------------------------ ОКТРУ
@pytest.mark.parametrize("word, expected", [("резинка", ("резинк", False)), ("гель", ("гел", False)),
                                            ("шампунь", ("шампун", False)), ("usb", ("usb", True)),
                                            ("интерфейсные", ("интерфейсн", False))])
def test_stem(word, expected):
    assert oktru.stem(word) == expected


def test_build_paths_joins_levels(db):
    path = db.execute("SELECT path_ru FROM oktru WHERE code='1088-0001-0001'").fetchone()[0]
    assert path == "Товары для красоты и личной гигиены > Косметика > Бальзамы для тела"


def test_oktru_search_finds_leaf_by_word_start(db):
    rows = oktru.search(db, "бальзам")
    assert rows[0][0] == "1088-0001-0001"
    assert rows[0][2:] == (1, 1)


def test_oktru_search_ignores_parents_and_deleted(db):
    codes = [r[0] for r in oktru.search(db, "косметика", limit=20)]
    assert "1088-0001" not in codes                 # не конечная категория
    assert "1088-0001-0099" not in codes            # удалена


def test_oktru_search_allows_one_missing_word(db):
    rows = oktru.search(db, "накладные ногти пластиковые")
    assert rows and rows[0][0] == "1088-0001-0002"
    assert rows[0][2:] == (2, 3)


def test_oktru_search_with_prefix(db):
    assert {r[0] for r in oktru.search(db, "кабельные", prefix="3000")} == set()
    assert {r[0] for r in oktru.search(db, "кабели", prefix="3000")} == {"3000-0001"}


def test_oktru_search_stop_words_only(db):
    assert oktru.search(db, "для и с") == []


def test_meta_roundtrip(db):
    oktru.meta_set(db, "k", 5)
    assert oktru.meta_get(db, "k") == "5"
    assert oktru.meta_get(db, "нет", "x") == "x"


class FakeClient:
    """Отдаёт справочник постранично, как настоящий каталог."""

    def __init__(self, pages):
        self.pages, self.asked = pages, []

    def dictionary_items(self, code, page=1, size=100):
        self.asked.append(page)
        return {"content": self.pages[page - 1], "totalPages": len(self.pages), "totalElements": 3}


def test_dicts_download_resumes_and_marks_done(db):
    item = lambda i: {"id": i, "code": f"P{i}", "hasChild": False, "properties": {"nameRu": f"Упаковка {i}"}}
    client = FakeClient([[item(1)], [item(2)], [item(3)]])
    oktru.meta_set(db, "dict_PACKAGE_next_page", 2)          # первая страница уже была загружена
    dicts.download(client, db, "PACKAGE")
    assert client.asked == [2, 3]
    assert dicts.get(db, "PACKAGE", "P3") == ("P3", "Упаковка 3")
    dicts.download(client, db, "PACKAGE")                    # второй раз ничего не качает
    assert client.asked == [2, 3]


# ------------------------------------------------------------------ справочники
def test_dicts_get_and_loaded(db):
    assert dicts.get(db, "COUNTRY", "CN") == ("CN", "Китай")
    assert dicts.get(db, "COUNTRY", "XX") is None
    assert dicts.is_loaded(db, "TNVED") and not dicts.is_loaded(db, "PACKAGE")


def test_dicts_search_by_word_and_by_digits(db):
    assert dicts.search(db, "COUNTRY", "россия") == [("RU", "Россия")]
    assert dicts.search(db, "TNVED", "3304") == [("3304990000", "прочие")]


def test_tnved_leaf_names_have_no_context_but_paths_do(db):
    assert dicts.search(db, "TNVED", "косметические средства") == []
    found = dicts.search_paths(db, "TNVED", "косметические средства")
    assert found[0][0] == "3304990000"
    assert "Косметические средства для ухода за кожей > прочие" in found[0][1]


def test_search_paths_tolerates_one_missing_word(db):
    found = dicts.search_paths(db, "TNVED", "провода кабели медные")
    assert [c for c, _ in found] == ["8544429000"]


# ------------------------------------------------------------------ карточка
def good_product(**kw):
    p = {"kind": "Бальзам для тела", "brand": "Avon", "article": "The Sleep Ritual", "features": "согревающий",
         "pack": "200 мл", "oktru": "1088-0001-0001", "country": "PL", "manufacturer": "Avon Operations Polska",
         "tnved": "3304990000", "unit": "796", "quantity": "1", "gtin": "5059018202727"}
    p.update(kw)
    return p


def test_compose_names_follows_catalog_format():
    name, short = card.compose_names(good_product())
    assert name == "Бальзам для тела Avon The Sleep Ritual / согревающий 200 мл"
    assert short == "Бальзам для тела Avon"


def test_compose_names_without_brand_uses_stm():
    name, short = card.compose_names({"kind": "Заколка", "features": "пластик"})
    assert (name, short) == ("Заколка СТМ / пластик", "Заколка СТМ")


def test_build_good_card(db):
    body, errors, warns = card.build(good_product(), db)
    assert errors == []
    attrs = {a["code"]: a["value"] for a in body["attributes"]}
    assert body["oktru"] == "1088-0001-0001" and body["autoPublication"] is False
    assert attrs["gtin"] == "5059018202727" and attrs["quantity"] == "1" and attrs["brand"] == "Avon"
    assert any("казахском" in w for w in warns)          # казахского названия нет: копия русского


def test_build_reports_missing_fields(db):
    body, errors, _ = card.build(good_product(manufacturer="", tnved=None), db)
    assert body is None
    assert "не заполнено поле «manufacturer»" in errors and "не заполнено поле «tnved»" in errors


@pytest.mark.parametrize("change, message", [
    ({"quantity": "0"}, "quantity"),
    ({"gtin": "12345"}, "gtin"),
    ({"country": "XX"}, "COUNTRY"),
    ({"tnved": "33049900"}, "ТН ВЭД"),
    ({"oktru": "1088-0001"}, "не конечная"),
    ({"oktru": "1088-0001-0099"}, "удалена"),
    ({"oktru": "9999"}, "нет в базе"),
    ({"article": "x" * 501}, "article"),
])
def test_build_catches_errors(db, change, message):
    body, errors, _ = card.build(good_product(**change), db)
    assert body is None
    assert any(message in e for e in errors), errors


def test_build_picks_category_by_words(db):
    p = good_product(oktru=None, oktru_search="накладные ногти")
    body, errors, warns = card.build(p, db)
    assert errors == [] and body["oktru"] == "1088-0001-0002"
    assert any("выбрана автоматически" in w for w in warns)


def test_build_warns_about_store_barcode(db):
    _, _, warns = card.build(good_product(gtin="2000000000015"), db)
    assert any("внутренний" in w for w in warns)


def test_build_keeps_photos_out_of_attributes(db):
    body, _, _ = card.build(good_product(photos={"front": "a.jpg"}), db)
    assert body["_photos"] == {"front": "a.jpg"}
    assert all(a["code"] != "_photos" for a in body["attributes"])
