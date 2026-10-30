"""Проверка штрихкодов и логика статусов кода NTIN (check_ntin.py)."""
import pandas as pd
import pytest

import check_ntin as ck
from conftest import INTERNAL, NIVEA, OTHER


def card(ntin, gtin, name="Товар", off=False, dup=""):
    return {"ntin_code": ntin, "gtin": gtin, "name_ru": name, "ntin_isdeactivated": off,
            "ntin_duplicateofproduct": dup}


@pytest.mark.parametrize("raw, expected", [("4005808588794", "4005808588794"), (" 400-580 858 8794 ", "4005808588794"),
                                           (None, ""), ("nan", ""), (4005808588794, "4005808588794")])
def test_digits(raw, expected):
    assert ck.digits(raw) == expected


def test_norm_drops_leading_zeros_lost_by_excel():
    assert ck.norm("0200212724569") == ck.norm("200212724569")


@pytest.mark.parametrize("raw, ok", [("12345678", True), ("123456789012", True), ("4005808588794", True),
                                     ("12345678901234", True), ("1234567", False), ("123456789", False)])
def test_clean_gtin_lengths(raw, ok):
    assert (ck.clean_gtin(raw) != "") is ok


@pytest.mark.parametrize("code, internal", [(INTERNAL, True), ("0212345678903", True), ("0412345678901", True),
                                            ("212345678901", True), (NIVEA, False), ("12345678", False)])
def test_is_internal(code, internal):
    assert ck.is_internal(code) is internal


def test_dup_of_reads_both_spellings_of_field():
    assert ck.dup_of({"ntin_duplicateofproduct": "0200000000017"}) == "0200000000017"
    assert ck.dup_of({"ntin_duulateofproduct": "0200000000024"}) == "0200000000024"
    assert ck.dup_of({}) == ""


def test_load_base_missing_file_gives_empty_table(tmp_path):
    df = ck.load_base(str(tmp_path / "нет.csv"))
    assert list(df.columns) == ["ntin", "gtin", "name", "oktru", "source"]
    assert df.empty


def test_base_index_groups_codes_by_barcode():
    base = pd.DataFrame([{"ntin": "1", "gtin": NIVEA, "name": "A"}, {"ntin": "2", "gtin": NIVEA, "name": "B"},
                         {"ntin": "2", "gtin": NIVEA, "name": "B"}, {"ntin": "3", "gtin": "", "name": "C"}])
    idx, names = ck.base_index(base)
    assert idx == {ck.norm(NIVEA): ["1", "2"]}
    assert names[ck.norm(NIVEA)] == "A"


def classify(gtin, base_ntins, items, api_ok=True, known=frozenset()):
    by_g, by_n = ck.index_items(items)
    return ck.classify(gtin, base_ntins, by_g, by_n, api_ok, "из базы", known)


def test_classify_single_active_card():
    ntin, status, src, _, name = classify(NIVEA, [], [card("0200089141810", NIVEA, "Лосьон")])
    assert (ntin, status, src, name) == ("0200089141810", "FOUND", "каталог", "Лосьон")


def test_classify_marks_card_also_in_our_base():
    _, status, src, _, _ = classify(NIVEA, ["0200089141810"], [card("0200089141810", NIVEA)])
    assert (status, src) == ("FOUND", "база+каталог")


def test_classify_several_cards_picks_the_one_from_our_base():
    items = [card("0200089141810", NIVEA, "Чужая"), card("0200212724569", NIVEA, "Наша")]
    ntin, status, _, comment, name = classify(NIVEA, ["0200212724569"], items)
    assert (ntin, status, name) == ("0200212724569", "FOUND", "Наша")
    assert "других активных" in comment


def test_classify_several_cards_without_hint_needs_choice():
    items = [card("0200089141810", NIVEA), card("0200212724569", NIVEA)]
    ntin, status, _, _, _ = classify(NIVEA, [], items)
    assert status == "MULTIPLE"
    assert set(ntin.split(",")) == {"0200089141810", "0200212724569"}


def test_classify_known_codes_help_choose_between_duplicates():
    items = [card("0200089141810", NIVEA), card("0200212724569", NIVEA)]
    ntin, status, _, _, _ = classify(NIVEA, [], items, known={ck.norm("0200089141810")})
    assert (ntin, status) == ("0200089141810", "FOUND")


def test_classify_deactivated_card_points_to_duplicate():
    ntin, status, _, comment, _ = classify(NIVEA, [], [card("0200089141810", NIVEA, off=True, dup="0200212724569")])
    assert (ntin, status) == ("0200212724569", "DEACTIVATED")
    assert "дубликат 0200212724569" in comment


def test_classify_falls_back_to_code_from_base_found_by_ntin():
    ntin, status, src, _, _ = classify(OTHER, ["0200000000024"], [card("0200000000024", "")])
    assert (ntin, status, src) == ("0200000000024", "FOUND", "база+каталог")


def test_classify_code_only_in_base_is_unverified():
    ntin, status, _, comment, name = classify(OTHER, ["0200000000024"], [])
    assert (ntin, status, name) == ("0200000000024", "FOUND_UNVERIFIED", "из базы")
    assert comment == "каталог не подтвердил код"
    assert classify(OTHER, ["0200000000024"], [], api_ok=False)[3] == "каталог недоступен"


def test_classify_nothing_found_depends_on_catalog_availability():
    assert classify(OTHER, [], [])[1] == "NOT_FOUND"
    assert classify(OTHER, [], [], api_ok=False)[1] == "NOT_CHECKED"


def test_query_catalog_splits_into_batches_and_skips_failed(monkeypatch):
    calls = []

    def fake_fetch(batch, debug=False):
        calls.append(len(batch))
        return None if len(calls) == 2 else [card(c, c) for c in batch]

    monkeypatch.setattr(ck, "fetch", fake_fetch)
    monkeypatch.setattr(ck.time, "sleep", lambda s: None)
    codes = [str(10_000_000 + i) for i in range(250)] + ["10000000"]      # дубль убирается
    items, checked = ck.query_catalog(codes)
    assert calls == [100, 100, 50]
    assert len(items) == 150 and len(checked) == 150


class Resp:
    def __init__(self, status, data=None):
        self.status_code, self._data = status, data

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise ck.requests.HTTPError(str(self.status_code))


def test_fetch_retries_after_429_and_unwraps_result(monkeypatch):
    answers = [Resp(429), Resp(200, {"result": [card("1", NIVEA)]})]
    monkeypatch.setattr(ck.requests, "get", lambda *a, **k: answers.pop(0))
    monkeypatch.setattr(ck.time, "sleep", lambda s: None)
    assert ck.fetch([NIVEA]) == [card("1", NIVEA)]


def test_fetch_gives_none_when_catalog_keeps_failing(monkeypatch):
    monkeypatch.setattr(ck.requests, "get", lambda *a, **k: Resp(503))
    monkeypatch.setattr(ck.time, "sleep", lambda s: None)
    assert ck.fetch([NIVEA]) is None


def test_search_by_name_stops_on_client_error(monkeypatch):
    calls = []
    monkeypatch.setattr(ck.requests, "get", lambda *a, **k: calls.append(1) or Resp(400))
    assert ck.search_by_name("кабель") is None
    assert len(calls) == 1          # 400: повторять бесполезно


def test_search_by_name_returns_list(monkeypatch):
    monkeypatch.setattr(ck.requests, "get", lambda *a, **k: Resp(200, {"results": [card("1", "")]}))
    assert ck.search_by_name("кабель") == [card("1", "")]
    monkeypatch.setattr(ck.requests, "get", lambda *a, **k: Resp(200, {"results": "мусор"}))
    assert ck.search_by_name("кабель") == []
