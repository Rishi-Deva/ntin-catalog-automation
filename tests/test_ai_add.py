"""ИИ-помощник: разбор ответов модели, сопоставление с карточками, выбор кодов (ai_add.py).

Модель не вызывается: call_ai и requests подменяются.
"""
import pytest

import ai_add
from ai_add import AiError


# ------------------------------------------------------------------ разбор JSON
@pytest.mark.parametrize("text", [
    '{"kind": "Бальзам"}',
    '```json\n{"kind": "Бальзам"}\n```',
    'Вот ответ: {"kind": "Бальзам"} Надеюсь, помог.',
    '{сломано {"kind": "Бальзам"}',
])
def test_parse_json_finds_first_object(text):
    assert ai_add.parse_json(text) == {"kind": "Бальзам"}


@pytest.mark.parametrize("text", ["", "нет json", "[1, 2]", None])
def test_parse_json_raises_with_raw_text(text):
    with pytest.raises(AiError) as e:
        ai_add.parse_json(text)
    assert e.value.raw == (text or "")[:300]


def test_normalize_product_cleans_values_and_lists():
    p = ai_add.normalize_product({"kind": "  Бальзам   для тела ", "brand": "null", "pack": "нет",
                                  "search_queries": "один запрос", "oktru_queries": ["a", "", None, "b", "c", "d"],
                                  "confidence": "1.7", "problems": "плохо видно"})
    assert p["kind"] == "Бальзам для тела"
    assert p["brand"] is None and p["pack"] is None
    assert p["search_queries"] == ["один запрос"]
    assert p["oktru_queries"] == ["a", "b", "c"]
    assert p["oktru_query"] == "a"                  # одиночный запрос берётся из списка
    assert p["confidence"] == 1.0
    assert p["problems"] == ["плохо видно"]


def test_normalize_product_bad_confidence():
    assert ai_add.normalize_product({"confidence": "высокая"})["confidence"] == 0.0


def test_cost_is_tiny_for_typical_request():
    assert ai_add.cost_usd(1000, 300) < 0.001


# ------------------------------------------------------------------ сопоставление с карточками
PRODUCT = {"kind": "Лосьон после бритья", "brand": "NIVEA", "model_or_article": None, "pack": "100 мл",
           "search_queries": ["лосьон после бритья nivea", "nivea men лосьон"]}


def test_key_tokens_skips_short_words_but_keeps_numbers():
    assert ai_add.key_tokens(PRODUCT) == ["лосьон", "после", "бритья", "nivea", "100"]


def test_score_card_full_and_partial():
    assert ai_add.score_card("Лосьон после бритья NIVEA Men 100 мл", PRODUCT) == 1.0
    partial = ai_add.score_card("Лосьон после бритья 100 мл", PRODUCT)      # бренда нет: штраф вдвое
    assert abs(partial - 0.4) < 1e-9


def test_score_card_empty_product():
    assert ai_add.score_card("что угодно", {}) == 0.0


def test_find_candidates_filters_sorts_and_pauses():
    answers = {
        "лосьон после бритья nivea": [
            {"ntin_code": "0200089141810", "name_ru": "Лосьон после бритья NIVEA Men 100 мл", "gtin": "4005808588794"},
            {"ntin_code": "0200000000099", "name_ru": "Крем для рук", "gtin": ""},
            {"ntin_code": "0200000000088", "name_ru": "Лосьон после бритья NIVEA 100 мл", "ntin_isdeactivated": True},
        ],
        "nivea men лосьон": [
            {"ntin_code": "0200089141810", "name_ru": "Лосьон после бритья NIVEA Men 100 мл"},   # повтор
            {"ntin_code": "0200212724569", "name_ru": "Лосьон после бритья NIVEA 100 мл охлаждающий"},
        ],
    }
    pauses = []
    found = ai_add.find_candidates(PRODUCT, answers.get, pause=0.5, sleep=pauses.append)
    assert [c["ntin"] for c in found] == ["0200089141810", "0200212724569"]
    assert found[0]["gtin"] == "4005808588794" and found[0]["score"] == 1.0
    assert pauses == [0.5]


def test_find_candidates_adds_stm_query_for_unbranded():
    asked = []
    ai_add.find_candidates({"kind": "Серьга", "search_queries": ["серьга кольцо"]}, lambda q: asked.append(q) or [],
                           sleep=lambda s: None)
    assert asked == ["серьга кольцо", "Серьга СТМ"]


def test_find_candidates_survives_catalog_down():
    assert ai_add.find_candidates(PRODUCT, lambda q: None, sleep=lambda s: None) == []


# ------------------------------------------------------------------ выбор варианта
@pytest.mark.parametrize("text, expected", [('{"number": 2}', 2), ('{"number": null}', None), ("3", 3),
                                            ('обрыв {"number": 4', 4), ("null", None)])
def test_pick_number(text, expected):
    assert ai_add._pick_number(text) == expected


@pytest.mark.parametrize("text", ['{"другое": 1}', "не знаю", '{"number": "два"}'])
def test_pick_number_bad(text):
    assert ai_add._pick_number(text) is ai_add._BAD


def fake_ai(monkeypatch, replies):
    """Подменяет call_ai: отдаёт ответы по очереди и запоминает запросы."""
    asked = []

    def call(env, system, content, max_tokens=1200):
        asked.append(content)
        return replies.pop(0), {"in": 100, "out": 10, "cost": 0.00001, "stop": "end_turn"}

    monkeypatch.setattr(ai_add, "call_ai", call)
    return asked


OPTIONS = [("A", "Бальзамы для тела"), ("B", "Эстакады открытые кабельные")]


def test_choose_code_trivial_cases_do_not_call_ai(monkeypatch):
    asked = fake_ai(monkeypatch, [])
    assert ai_add.choose_code({}, PRODUCT, [], "категорию") == (None, None)
    assert ai_add.choose_code({}, PRODUCT, OPTIONS[:1], "категорию") == ("A", None)
    assert asked == []


def test_choose_code_picks_number(monkeypatch):
    fake_ai(monkeypatch, ['{"number": 1}'])
    code, usage = ai_add.choose_code({}, PRODUCT, OPTIONS, "категорию")
    assert code == "A" and usage["in"] == 100


def test_choose_code_retries_once_then_counts_both(monkeypatch):
    asked = fake_ai(monkeypatch, ["не понял", '{"number": 2}'])
    code, usage = ai_add.choose_code({}, PRODUCT, OPTIONS, "категорию")
    assert code == "B" and len(asked) == 2 and usage["in"] == 200


def test_choose_code_out_of_range_means_nothing(monkeypatch):
    fake_ai(monkeypatch, ['{"number": 7}'])
    assert ai_add.choose_code({}, PRODUCT, OPTIONS, "категорию")[0] is None


def test_choose_code_gives_up_with_usage(monkeypatch):
    fake_ai(monkeypatch, ["мусор", "снова мусор"])
    with pytest.raises(AiError) as e:
        ai_add.choose_code({}, PRODUCT, OPTIONS, "категорию")
    assert e.value.usage["in"] == 200


def test_extract_product_retries_after_bad_json(monkeypatch):
    asked = fake_ai(monkeypatch, ["вот товар", '{"kind": "Бальзам", "brand": "Avon", "confidence": 0.9}'])
    monkeypatch.setattr(ai_add, "prepare_image", lambda b: b)
    product, usage = ai_add.extract_product({}, [b"jpeg"], note="бальзам")
    assert product["brand"] == "Avon" and usage["in"] == 200
    assert "ВАЖНО" in asked[1][-1]["text"] and "Подсказка сотрудника: бальзам" in asked[0][-1]["text"]


def test_extract_product_cut_off_answer(monkeypatch):
    monkeypatch.setattr(ai_add, "prepare_image", lambda b: b)
    monkeypatch.setattr(ai_add, "call_ai", lambda *a, **k: ('{"kind": "Ба', {"in": 1, "out": 1, "cost": 0, "stop": "max_tokens"}))
    with pytest.raises(AiError, match="оборвался"):
        ai_add.extract_product({}, [b"jpeg"])


def test_resolve_codes_end_to_end(monkeypatch, db):
    def smart_ai(env, system, content, max_tokens=1200):
        """Отвечает номером строки, где есть «Бальзам» (как выбрала бы модель по смыслу)."""
        lines = [l for l in content[-1]["text"].splitlines() if l[:1].isdigit() and ". " in l]
        n = next(int(l.split(".")[0]) for l in lines if "Бальзам" in l)
        return '{"number": %d}' % n, {"in": 100, "out": 5, "cost": 0.0, "stop": "end_turn"}

    monkeypatch.setattr(ai_add, "call_ai", smart_ai)
    product = ai_add.normalize_product({"kind": "Бальзам для тела", "brand": "Avon", "country_name": "Польша",
                                        "oktru_queries": ["бальзамы", "кабельные эстакады"],
                                        "tnved_queries": ["косметические средства для кожи"]})
    codes, notes, usages = ai_add.resolve_codes({}, db, product)
    assert codes["country"] == "PL"
    assert codes["oktru"] == "1088-0001-0001" and codes["oktru_name"] == "Бальзамы для тела"
    assert codes["tnved"] == "3304990000"
    assert notes == [] and len(usages) == 1        # ТН ВЭД один вариант: ИИ не спрашивали


def test_resolve_codes_reports_unknown_country(monkeypatch, db):
    fake_ai(monkeypatch, [])
    product = ai_add.normalize_product({"kind": "Неизвестное", "country_name": "Атлантида"})
    codes, notes, _ = ai_add.resolve_codes({}, db, product)
    assert "country" not in codes
    assert any("Атлантида" in n for n in notes)


def test_build_spec_and_kazakh_name_only_from_pack():
    product = ai_add.normalize_product({"kind": "Бальзам", "brand": "Avon", "name_kk_on_pack": "Бальзам денеге"})
    spec = ai_add.build_spec(product, {"country": "PL", "oktru": "X", "tnved": "3304990000"}, gtin="5059018202727")
    assert spec["name_kk"] == "Бальзам денеге" and spec["gtin"] == "5059018202727"
    assert spec["manufacturer"] == "не указан" and spec["unit"] == "796"
    assert "name_kk" not in ai_add.build_spec(ai_add.normalize_product({"kind": "Бальзам"}), {})


def test_apply_correction_changes_only_requested_fields(monkeypatch):
    fake_ai(monkeypatch, ['{"brand": "Baseus", "oktru_query": "кабели"}'])
    product = ai_add.normalize_product({"kind": "Кабель", "brand": "Noname", "oktru_queries": ["провода"]})
    new, _ = ai_add.apply_correction({}, product, "бренд Baseus")
    assert new["brand"] == "Baseus" and new["kind"] == "Кабель"
    assert new["oktru_query"] == "кабели" and new["oktru_queries"] == []
    assert product["brand"] == "Noname"             # исходное описание не испорчено


# ------------------------------------------------------------------ запрос к модели
class Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        return self._data


def test_call_ai_without_key():
    with pytest.raises(AiError, match="не подключён"):
        ai_add.call_ai({}, "s", [])


@pytest.mark.parametrize("status, data, message", [
    (401, {}, "Ключ ИИ не принят"),
    (429, {}, "Слишком много"),
    (400, {"error": {"message": "Your credit balance is too low"}}, "закончились деньги"),
    (529, {}, "временно недоступен"),
    (418, {}, "код 418"),
])
def test_call_ai_human_errors(monkeypatch, status, data, message):
    monkeypatch.setattr(ai_add.requests, "post", lambda *a, **k: Resp(status, data))
    with pytest.raises(AiError, match=message):
        ai_add.call_ai({"ANTHROPIC_API_KEY": "k"}, "s", [])


def test_call_ai_success_counts_tokens(monkeypatch):
    sent = {}

    def post(url, headers, json, timeout):
        sent.update(json)
        return Resp(200, {"content": [{"type": "text", "text": "при"}, {"type": "text", "text": "вет"}],
                          "usage": {"input_tokens": 1000, "output_tokens": 200}, "stop_reason": "end_turn"})

    monkeypatch.setattr(ai_add.requests, "post", post)
    text, usage = ai_add.call_ai({"ANTHROPIC_API_KEY": "k", "AI_MODEL": "claude-test"}, "s", [{"type": "text", "text": "x"}])
    assert text == "привет" and usage["in"] == 1000 and usage["out"] == 200 and usage["stop"] == "end_turn"
    assert sent["model"] == "claude-test"


def test_call_ai_refusal(monkeypatch):
    monkeypatch.setattr(ai_add.requests, "post", lambda *a, **k: Resp(200, {"stop_reason": "refusal", "content": []}))
    with pytest.raises(AiError, match="отказался"):
        ai_add.call_ai({"ANTHROPIC_API_KEY": "k"}, "s", [])


def test_call_ai_no_connection(monkeypatch):
    def boom(*a, **k):
        raise ai_add.requests.ConnectionError("нет сети")

    monkeypatch.setattr(ai_add.requests, "post", boom)
    with pytest.raises(AiError, match="Нет связи"):
        ai_add.call_ai({"ANTHROPIC_API_KEY": "k"}, "s", [])
