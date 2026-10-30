"""Логика бота без Telegram и без интернета (bot.py, класс Brain)."""
import logging

import pytest

import bot
from conftest import AVON, INTERNAL, NIVEA, OTHER

ADMIN, STAFF, STRANGER = 1, 2, 3


def msg(uid, text="", **extra):
    m = {"chat": {"type": "private"}, "from": {"id": uid, "username": f"user{uid}"}, "text": text}
    m.update(extra)
    return m


@pytest.fixture
def brain(db, base_csv, catalog):
    return bot.Brain(_bot_tables(db), {ADMIN}, {STAFF}, base_path=base_csv, invite="Секрет-2026")


def _bot_tables(db):
    """Таблицы бота в той же временной базе."""
    db.execute("CREATE TABLE IF NOT EXISTS scans(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user_id INTEGER,"
               " user_name TEXT, barcode TEXT, status TEXT, ntin TEXT, name TEXT, comment TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS members(user_id INTEGER PRIMARY KEY, name TEXT, joined_at TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS choices(gtin TEXT PRIMARY KEY, ntin TEXT, name TEXT, user_id INTEGER,"
               " user_name TEXT, ts TEXT)")
    return db


# ------------------------------------------------------------------ штрихкоды
def test_known_barcodes_are_valid():
    for code in (NIVEA, AVON, OTHER, INTERNAL):
        assert bot.gtin_ok(code), code


@pytest.mark.parametrize("code", ["4005808588795", "12345", "４００５８０８５８８７９４", ""])
def test_gtin_ok_rejects(code):
    assert not bot.gtin_ok(code)


def test_ascii_digits_converts_phone_keyboards():
    assert bot.ascii_digits("４００５٨٠٨") == "4005808"


def test_extract_codes_single_code_with_spaces():
    assert bot.extract_codes("4 005808 588794") == [NIVEA]


def test_extract_codes_several_codes():
    assert bot.extract_codes(f"{NIVEA}\n{AVON}, {OTHER}; {NIVEA}") == [NIVEA, AVON, OTHER]


def test_extract_codes_long_line_split_by_spaces():
    assert bot.extract_codes(f"{NIVEA} {AVON}") == [NIVEA, AVON]


def test_parse_ids():
    assert bot.parse_ids("123, 456 ;789") == {123, 456, 789}
    assert bot.parse_ids(None) == set()


def test_csv_safe_blocks_formulas():
    assert bot.csv_safe("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert bot.csv_safe("-5") == "'-5"
    assert bot.csv_safe("Помада") == "Помада"
    assert bot.csv_safe(None) == ""


def test_split_text_respects_limit():
    parts = bot.split_text("строка\n" * 1000 + "x" * 9000, n=4000)
    assert all(len(p) <= 4000 for p in parts)
    assert "".join(p.replace("\n", "") for p in parts).count("x") == 9000
    assert bot.split_text("") == [""]


def test_redact_secret_hides_token_in_messages_and_errors():
    rec = logging.LogRecord("t", logging.ERROR, "", 0, "url https://api/bot%s/send", ("TOKEN123",), None)
    try:
        raise RuntimeError("упало на TOKEN123")
    except RuntimeError:
        import sys
        rec.exc_info = sys.exc_info()
    bot.RedactSecret("TOKEN123").filter(rec)
    assert "TOKEN123" not in rec.getMessage() and "TOKEN123" not in rec.exc_text


# ------------------------------------------------------------------ доступ
def test_stranger_gets_his_id_once_per_minute(brain):
    first = brain.handle(msg(STRANGER, "привет"))
    assert "Нет доступа" in first[0] and str(STRANGER) in first[0] and "/join" in first[0]
    assert brain.handle(msg(STRANGER, "привет")) == []


def test_group_chats_are_ignored(brain):
    m = msg(ADMIN, "/start")
    m["chat"]["type"] = "group"
    assert brain.handle(m) == []


def test_id_works_for_everyone(brain):
    assert str(STRANGER) in brain.handle(msg(STRANGER, "🆔 Мой номер"))[0]


def test_join_with_right_code_opens_access_and_notifies_admin(brain):
    assert "Доступ открыт" in brain.handle(msg(STRANGER, "/join секрет-2026"))[0]
    assert brain.has_access(STRANGER)
    assert brain.outbox and brain.outbox[0][0] == ADMIN


def test_join_wrong_code_is_limited(brain):
    for _ in range(5):
        assert "не подошёл" in brain.join(STRANGER, "x", "/join неверно")
    assert "Слишком много" in brain.join(STRANGER, "x", "/join Секрет-2026")


def test_join_disabled_without_invite(db, base_csv, catalog):
    b = bot.Brain(_bot_tables(db), {ADMIN}, set(), base_path=base_csv, invite="")
    assert "не включён" in b.join(STRANGER, "x", "/join что-угодно")


def test_admin_commands_are_closed_for_staff(brain):
    assert brain.handle(msg(STAFF, "/stats")) == ["Эта команда только для администратора."]


def test_markup_differs_for_stranger_staff_and_admin(brain):
    assert brain.markup_for(STRANGER) == {"remove_keyboard": True}
    staff = [b["text"] for row in brain.markup_for(STAFF)["keyboard"] for b in row]
    admin = [b["text"] for row in brain.markup_for(ADMIN)["keyboard"] for b in row]
    assert bot.BTN_FIND in staff and "📊 Статистика" not in staff
    assert "📊 Статистика" in admin


# ------------------------------------------------------------------ проверка штрихкода
def card(ntin, gtin, name="Товар", off=False):
    return {"ntin_code": ntin, "gtin": gtin, "name_ru": name, "ntin_isdeactivated": off}


def test_found_in_catalog(brain, catalog):
    catalog.cards = [card("0200212724569", NIVEA, "Лосьон после бритья NIVEA")]
    reply = brain.handle(msg(STAFF, NIVEA))[0]
    assert "✅ Код есть: 0200212724569" in reply
    row = brain.db.execute("SELECT status, ntin FROM scans").fetchone()
    assert row == ("FOUND", "0200212724569")


def test_not_found_goes_to_todo_list(brain, catalog):
    reply = brain.handle(msg(STAFF, AVON))[0]
    assert "кода нет" in reply
    assert brain.db.execute("SELECT status FROM scans").fetchone() == ("NOT_FOUND",)


def test_catalog_down_with_code_in_base(brain, catalog):
    catalog.down = True
    assert "Код из нашей базы: 0200212724569" in brain.handle(msg(STAFF, NIVEA))[0]


def test_catalog_down_without_code(brain, catalog):
    catalog.down = True
    assert "не отвечает" in brain.handle(msg(STAFF, AVON))[0]


def test_internal_barcode_is_not_searched(brain, catalog):
    reply = brain.handle(msg(STAFF, INTERNAL))[0]
    assert "внутренний" in reply and catalog.calls == 0


def test_wrong_check_digit(brain):
    assert "не прошли проверку" in brain.handle(msg(STAFF, "4005808588795"))[0]


def test_several_codes_in_one_message_are_limited(brain, catalog, monkeypatch):
    monkeypatch.setattr(bot, "MAX_CODES", 2)
    replies = brain.handle(msg(STAFF, f"{NIVEA}\n{AVON}\n{OTHER}"))
    assert len(replies) == 3 and "первые 2 из 3" in replies[-1]


def test_remembered_choice_is_used_for_unknown_barcode(brain, catalog):
    brain.remember(AVON, "0200999999990", "Бальзам Avon", ADMIN, "admin")
    reply = brain.handle(msg(STAFF, AVON))[0]
    assert "0200999999990" in reply and "уже сопоставляли" in reply and "/forget" not in reply
    assert "/forget" in brain.handle(msg(ADMIN, AVON))[0]
    assert brain.forget(AVON) == 1


def test_remembered_choice_ignored_if_card_was_switched_off(brain, catalog):
    brain.remember(AVON, "0200999999990", "Бальзам Avon", ADMIN, "admin")
    catalog.cards = [card("0200999999990", "", off=True)]
    assert "кода нет" in brain.handle(msg(STAFF, AVON))[0]


def test_photo_without_barcode_reader(brain):
    m = msg(STAFF, photo=[{"file_id": "f1"}])
    assert "введите цифры" in brain.handle(m)[0]


def test_photo_decoded(brain, catalog, monkeypatch):
    catalog.cards = [card("0200212724569", NIVEA)]
    monkeypatch.setattr(bot, "decode_barcode", lambda data: NIVEA)
    replies = brain.handle(msg(STAFF, photo=[{"file_id": "small"}, {"file_id": "big"}]), photo_loader=lambda fid: b"img")
    assert replies[0] == f"Прочитал штрихкод: {NIVEA}" and "0200212724569" in replies[1]


def test_too_big_document_is_refused(brain):
    m = msg(STAFF, document={"file_id": "d", "mime_type": "image/png", "file_size": 9_000_000})
    assert "слишком большой" in brain.handle(m)[0]


# ------------------------------------------------------------------ поиск по названию
def test_find_button_waits_for_words(brain, monkeypatch):
    seen = []
    monkeypatch.setattr(brain, "find_by_name", lambda text: seen.append(text) or ["результат"])
    assert "Что искать" in brain.handle(msg(STAFF, bot.BTN_FIND))[0]
    assert brain.handle(msg(STAFF, "помада avon")) == ["результат"]
    assert seen == ["/find помада avon"]


def test_find_without_words_explains(brain):
    assert "Напишите, что искать" in brain.find_by_name("/find")[0]
