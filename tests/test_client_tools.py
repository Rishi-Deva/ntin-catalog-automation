"""Клиент API каталога (nkt_client.py), пакетный подбор по названиям (match_names.py), импорт прайсов."""
import json

import pytest

import ai_add
import import_supplier
import match_names
import nkt_client
from conftest import INTERNAL, NIVEA
from nkt_client import AuthError, NktClient, NktError, ValidationError


# ------------------------------------------------------------------ .env и ключ
def test_read_dotenv(tmp_path):
    p = tmp_path / ".env"
    p.write_text('# комментарий\nNKT_API_KEY = "abc123"\nAI_MODEL=claude-haiku\nмусор\n', encoding="utf-8")
    assert nkt_client._read_dotenv(str(p)) == {"NKT_API_KEY": "abc123", "AI_MODEL": "claude-haiku"}
    assert nkt_client._read_dotenv(str(tmp_path / "нет")) == {}


def test_load_key_catches_doubled_paste(monkeypatch):
    monkeypatch.setenv("NKT_API_KEY", "abcdabcd")
    with pytest.raises(NktError, match="дважды"):
        nkt_client.load_key()


def test_load_key_strips_spaces(monkeypatch):
    monkeypatch.setenv("NKT_API_KEY", " ab cd ")
    assert nkt_client.load_key() == "abcd"


def test_client_repr_hides_key():
    assert "secret" not in repr(NktClient(api_key="secret"))


# ------------------------------------------------------------------ повторы и ошибки
class Resp:
    def __init__(self, status, text="", headers=None):
        self.status_code, self.text, self.headers = status, text, headers or {}

    def json(self):
        return json.loads(self.text or "{}")

    def raise_for_status(self):
        pass


def client_with(monkeypatch, answers):
    c = NktClient(api_key="k", min_interval=0, retries=4)
    sleeps = []
    monkeypatch.setattr(nkt_client.time, "sleep", sleeps.append)
    c.session.request = lambda method, url, **kw: answers.pop(0)
    return c, sleeps


def test_retry_after_429_respects_retry_after(monkeypatch):
    c, sleeps = client_with(monkeypatch, [Resp(429, headers={"Retry-After": "3"}), Resp(200, '{"ok": 1}')])
    assert c.get_json("/x") == {"ok": 1}
    assert 3.0 in sleeps


def test_server_errors_exhaust_retries(monkeypatch):
    c, _ = client_with(monkeypatch, [Resp(503)] * 4)
    with pytest.raises(NktError, match="после 4 попыток"):
        c.get_json("/x")


def test_auth_error(monkeypatch):
    c, _ = client_with(monkeypatch, [Resp(401)])
    with pytest.raises(AuthError):
        c.get_json("/x")


def test_validation_error_keeps_body(monkeypatch):
    c, _ = client_with(monkeypatch, [Resp(422, '{"message": "name_ru пустое"}')])
    with pytest.raises(ValidationError) as e:
        c.post_json("/requests", {"a": 1})
    assert e.value.status == 422 and "name_ru" in e.value.body


def test_body_of_empty_response():
    class Empty:
        def json(self):
            raise ValueError
    assert NktClient.body(Empty()) == {}


# ------------------------------------------------------------------ пакетный подбор
def test_gtin_valid_matches_bot():
    assert match_names.gtin_valid(NIVEA)
    assert not match_names.gtin_valid("4005808588795")


def test_pick_column_by_header_words():
    cols = ["№", "Наименование товара", "Штрихкод", "Бренд"]
    assert match_names.pick_column(cols, match_names.NAME_HDR) == "Наименование товара"
    assert match_names.pick_column(cols, ("нет такого",)) is None


def test_read_table_skips_example_and_empty_rows(tmp_path):
    p = tmp_path / "list.csv"
    p.write_text("Название;Штрихкод\nПример: помада;123\nЛосьон NIVEA;" + NIVEA + "\n;\n", encoding="utf-8")
    df, spec = match_names.read_table(str(p))
    assert list(df[spec["name"]]) == ["Лосьон NIVEA"]
    assert spec["gtin"] == "Штрихкод"


def test_searcher_caches_and_pauses(tmp_path):
    calls, pauses = [], []
    s = match_names.Searcher(cache_path=str(tmp_path / "c.json"), pause=0.6, sleep=pauses.append,
                             search=lambda q: calls.append(q) or [{"ntin_code": "1", "name_ru": "A", "extra": "x"}])
    assert s("Помада  Avon") == [{"ntin_code": "1", "name_ru": "A", "gtin": None, "ntin_isdeactivated": False}]
    s("помада avon")                       # тот же запрос другими буквами: из памяти
    s("кабель")
    assert calls == ["Помада  Avon", "кабель"] and pauses == [0.6]
    s.save()
    assert match_names.Searcher(cache_path=str(tmp_path / "c.json"), search=lambda q: 1 / 0)("помада avon")


def test_searcher_marks_failure():
    s = match_names.Searcher(search=lambda q: None)
    assert s("x") is None and s.failed


class StubSearch:
    def __init__(self, cards):
        self.cards, self.failed = cards, False

    def __call__(self, q):
        return self.cards


def test_match_row_by_barcode_prefers_catalog():
    product = match_names.heuristic_product("Лосьон после бритья", "NIVEA", "")
    fetch = lambda codes: [{"ntin_code": "0200212724569", "name_ru": "Лосьон NIVEA"}]
    res = match_names.match_row(product, NIVEA, StubSearch([]), fetch, {"0200212724569"}, 0.6)
    assert res["Статус"] == match_names.ST_BARCODE and res["Код NTIN"] == "0200212724569"
    assert res["Из нашей базы"] == "да" and res["Совпадение слов, %"] == 100


def test_match_row_by_name_when_no_barcode():
    product = match_names.heuristic_product("Лосьон после бритья", "NIVEA", "")
    cards = [{"ntin_code": "0200089141810", "name_ru": "Лосьон после бритья NIVEA Men"}]
    res = match_names.match_row(product, "", StubSearch(cards), lambda c: [], set(), 0.6)
    assert res["Статус"] == match_names.ST_NAME and res["Код NTIN"] == "0200089141810"


def test_match_row_internal_barcode_goes_to_name_search():
    asked = []
    product = match_names.heuristic_product("Серьга кольцо", "", "")
    match_names.match_row(product, INTERNAL, StubSearch([]), lambda c: asked.append(c) or [], set(), 0.6)
    assert asked == []


class DownSearch:
    """Каталог не отвечает: поиск возвращает None и ставит флаг."""
    failed = False

    def __call__(self, q):
        self.failed = True
        return None


def test_match_row_nothing_found():
    product = match_names.heuristic_product("Неизвестная вещь", "", "")
    res = match_names.match_row(product, "", StubSearch([]), lambda c: [], set(), 0.6)
    assert res["Статус"] == match_names.ST_NONE and res["Что делать"]


def test_match_row_catalog_down():
    product = match_names.heuristic_product("Неизвестная вещь", "", "")
    res = match_names.match_row(product, "", DownSearch(), lambda c: [], set(), 0.6)
    assert res["Статус"] == match_names.ST_DOWN


def test_heuristic_product_builds_queries():
    p = match_names.heuristic_product("Помада губная", "Avon", "красная")
    assert p["search_queries"] == ["Помада губная", "Avon Помада губная"]
    assert ai_add.key_tokens(p)


# ------------------------------------------------------------------ импорт прайсов
@pytest.mark.parametrize("raw, expected", [(200212724569, "0200212724569"), ("0200212724569", "0200212724569"),
                                           (200212724569.0, "0200212724569"), ("12", ""), (None, "")])
def test_clean_ntin_restores_zero(raw, expected):
    assert import_supplier.clean_ntin(raw) == expected


@pytest.mark.parametrize("raw, expected", [(4005808588794, NIVEA), ("40058085", "40058085"),
                                           (None, "")])
def test_clean_gtin(raw, expected):
    assert import_supplier.clean_gtin(raw) == expected


def test_col_index():
    assert import_supplier.col_index("A") == 0 and import_supplier.col_index("c") == 2
