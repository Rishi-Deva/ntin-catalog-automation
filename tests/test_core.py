"""
Автотесты без сети и без токенов: каталог подменяется заглушкой.

Запуск из корня репозитория:   python -m unittest discover -s tests -v
"""
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pandas as pd  # noqa: E402

import bot  # noqa: E402
import check_ntin as ck  # noqa: E402


def make_gtin(first12):
    """Достраивает контрольную цифру EAN-13."""
    s = sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(first12))
    return first12 + str((10 - s % 10) % 10)


GOOD = "4005808588794"


class BarcodeTests(unittest.TestCase):
    def test_valid_and_invalid_checksum(self):
        self.assertTrue(bot.gtin_ok(GOOD))
        self.assertFalse(bot.gtin_ok("4005808588795"))

    def test_wrong_length_and_letters(self):
        self.assertFalse(bot.gtin_ok("12345"))
        self.assertFalse(bot.gtin_ok("40058085887ab"))

    def test_fullwidth_digits_are_converted(self):
        self.assertEqual(bot.ascii_digits("４００５８０８５８８７９４"), GOOD)

    def test_extract_one_code_with_spaces(self):
        self.assertEqual(bot.extract_codes("4 005808 588794"), [GOOD])

    def test_extract_several_codes(self):
        other = make_gtin("890000000001")
        self.assertEqual(bot.extract_codes(f"{GOOD}\n{other}"), [GOOD, other])
        self.assertEqual(bot.extract_codes(f"{GOOD} {other}"), [GOOD, other])

    def test_extract_nothing(self):
        self.assertEqual(bot.extract_codes("привет"), [])


class HelperTests(unittest.TestCase):
    def test_csv_formula_is_escaped(self):
        self.assertEqual(bot.csv_safe("=HYPERLINK(1)"), "'=HYPERLINK(1)")
        self.assertEqual(bot.csv_safe("+cmd"), "'+cmd")
        self.assertEqual(bot.csv_safe("обычный текст"), "обычный текст")
        self.assertEqual(bot.csv_safe(None), "")

    def test_split_text_has_no_empty_or_oversized_parts(self):
        for text in ("x" * 4000, "x" * 9000, "a\n" + "x" * 4000, "\n".join("строка %d" % i for i in range(2000))):
            parts = bot.split_text(text)
            self.assertTrue(all(parts), "пустой кусок")
            self.assertTrue(all(len(p) <= 4000 for p in parts))

    def test_image_pixels_from_png_header(self):
        png = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 30000, 30000) + b"\x08" + b"\x00" * 4
        self.assertEqual(bot.image_pixels(png), 900_000_000)

    def test_image_pixels_from_jpeg_with_fill_bytes(self):
        jpg = (b"\xff\xd8" + b"\xff\xff\xff\xe0\x00\x04\x00\x00" + b"\xff\xd0"
               + b"\xff\xc0\x00\x11\x08" + struct.pack(">HH", 3000, 4000) + b"\x00" * 20)
        self.assertEqual(bot.image_pixels(jpg), 12_000_000)


class BotLogicTests(unittest.TestCase):
    """Логика ответов бота без Telegram: доступ по коду, статусы, списки."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.catalog = {GOOD: [{"gtin": GOOD, "ntin_code": "0200212724569", "ntin_isdeactivated": False,
                                "name_ru": "ЛОСЬОН ПОСЛЕ БРИТЬЯ NIVEA 100МЛ"}]}
        self.down = False
        ck.fetch = lambda codes, debug=False: None if self.down else [i for c in codes for i in self.catalog.get(c, [])]
        self.db = bot.connect(os.path.join(self.tmp, "t.db"))
        self.brain = bot.Brain(self.db, admins={1}, allowed={2}, base_path=os.path.join(self.tmp, "нет_базы.csv"),
                               invite="Секрет-1")

    @staticmethod
    def msg(uid, text):
        return {"chat": {"type": "private", "id": uid}, "from": {"id": uid, "first_name": "u%d" % uid}, "text": text}

    def say(self, uid, text):
        return self.brain.handle(self.msg(uid, text))

    def test_found_code_is_returned(self):
        self.assertIn("0200212724569", self.say(2, GOOD)[0])

    def test_stranger_is_refused_and_throttled(self):
        self.assertIn("Нет доступа", self.say(50, "привет")[0])
        self.assertEqual(self.say(50, "ещё раз"), [])          # второй раз подряд не отвечаем

    def test_join_by_code_is_case_insensitive(self):
        self.assertIn("Код не подошёл", self.say(60, "/join неверно")[0])
        self.assertIn("Доступ открыт", self.say(60, "/join секрет-1")[0])
        self.assertIn("0200212724569", self.say(60, GOOD)[0])

    def test_too_many_wrong_codes_close_joining(self):
        for uid in range(100, 135):
            self.say(uid, "/join неверно")
        self.assertIn("временно закрыт", self.say(500, "/join секрет-1")[0])

    def test_notfound_keeps_latest_status_per_barcode(self):
        missing = make_gtin("890000000001")
        self.say(2, missing)                                    # нет в каталоге -> в список
        self.assertIn(missing, self.say(1, "/notfound")[0])
        self.catalog[missing] = [{"gtin": missing, "ntin_code": "0200000000001", "ntin_isdeactivated": False,
                                  "name_ru": "Нашёлся позже"}]
        self.say(2, missing)                                    # теперь код есть -> из списка уходит
        self.assertEqual(self.say(1, "/notfound"), ["Список пуст."])

    def test_unreachable_catalog_does_not_erase_known_status(self):
        missing = make_gtin("890000000002")
        self.say(2, missing)
        self.down = True
        self.say(2, missing)                                    # каталог не ответил
        out = self.say(1, "/notfound")[0]
        self.assertIn("Нужно зарегистрировать", out)
        self.assertIn(missing, out)

    def test_batch_limit_is_reported_honestly(self):
        codes = [make_gtin("89000000%04d" % i) for i in range(7)]
        replies = self.say(2, "\n".join(codes))
        self.assertEqual(len(replies), bot.MAX_CODES + 1)
        self.assertIn("первые 5 из 7", replies[-1])


class FindByNameTests(unittest.TestCase):
    """Команда /find: поиск готовых кодов по словам из названия."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        base = os.path.join(self.tmp, "base.csv")
        pd.DataFrame([
            ["0200000000011", "", "Бальзам после бритья Avon 100 мл", "", "t"],
            ["0200000000012", "", "Бальзам для волос Ёлочка 200 мл", "", "t"],
            ["0200000000013", "", "Гель после бритья Nivea", "", "t"],
            ["0200000000014", "", "Шампунь отключённый", "", "t"],
        ] + [["02000000001%02d" % i, "", "Крем для рук Avon №%d" % i, "", "t"] for i in range(20, 32)],
            columns=["ntin", "gtin", "name", "oktru", "source"]).to_csv(base, index=False)
        self.deact = {"0200000000014"}
        self.down = False

        def fetch(codes, debug=False):
            if self.down:
                return None
            return [{"ntin_code": c, "gtin": None, "ntin_isdeactivated": c in self.deact} for c in codes]
        ck.fetch = fetch
        self.brain = bot.Brain(bot.connect(os.path.join(self.tmp, "t.db")), admins={1}, allowed={2}, base_path=base)

    def find(self, text, uid=2):
        msg = {"chat": {"type": "private", "id": uid}, "from": {"id": uid, "first_name": "u"}, "text": text}
        return self.brain.handle(msg)[0]

    def test_all_words_required_and_case_and_yo_ignored(self):
        out = self.find("/find БАЛЬЗАМ бритья")
        self.assertIn("0200000000011", out)
        self.assertNotIn("0200000000012", out)
        self.assertIn("0200000000012", self.find("/find елочка"))       # «е» находит «Ё»

    def test_empty_query_asks_what_to_search(self):
        self.assertIn("Что искать", self.find("/find"))
        self.assertIn("Напишите, что искать", self.brain.find_by_name("/find")[0])

    def test_nothing_found(self):
        self.assertIn("Ничего не нашёл", self.find("/find помада"))

    def test_limit_and_hint_to_narrow(self):
        out = self.find("/find крем рук")
        self.assertIn("Показываю %d" % bot.FIND_LIMIT, out)
        self.assertIn("Добавьте ещё слово", out)
        self.assertEqual(sum(1 for line in out.split("\n") if line.startswith("02000")), bot.FIND_LIMIT)

    def test_deactivated_code_is_not_shown(self):
        out = self.find("/find шампунь")
        self.assertNotIn("0200000000014  ", out)
        self.assertIn("использовать их нельзя", out)

    def test_catalog_down_shows_codes_with_warning(self):
        self.down = True
        out = self.find("/find гель бритья")
        self.assertIn("0200000000013", out)
        self.assertIn("не проверено", out)

    def test_stranger_cannot_use_find(self):
        self.assertIn("Нет доступа", self.find("/find крем", uid=99))



class ButtonTests(unittest.TestCase):
    """Кнопки под полем ввода и диалог «Найти по названию»."""

    def setUp(self):
        FindByNameTests.setUp(self)
        self.brain.admins = {1}
        self.brain.allowed = {1, 2}

    def say(self, uid, text, **extra):
        msg = {"chat": {"type": "private", "id": uid}, "from": {"id": uid, "first_name": "u"}, "text": text}
        msg.update(extra)
        return self.brain.handle(msg)

    def test_keyboard_for_member_admin_and_stranger(self):
        member = self.brain.markup_for(2)["keyboard"]
        self.assertEqual(member[0][0]["text"], bot.BTN_FIND)
        texts_admin = [b["text"] for row in self.brain.markup_for(1)["keyboard"] for b in row]
        self.assertIn("📋 Список", texts_admin)
        self.assertNotIn("📋 Список", [b["text"] for row in member for b in row])
        self.assertEqual(self.brain.markup_for(99), {"remove_keyboard": True})

    def test_find_button_then_words(self):
        self.assertIn("Что искать", self.say(2, bot.BTN_FIND)[0])
        out = self.say(2, "крем рук")[0]
        self.assertIn("Нашёл по названию", out)
        self.assertNotIn(2, self.brain.waiting_find)

    def test_state_is_used_once(self):
        self.say(2, bot.BTN_FIND)
        self.say(2, "крем рук")
        self.assertIn("Пришлите фото штрихкода", self.say(2, "ещё слова")[0])

    def test_barcode_after_button_is_a_barcode_not_a_search(self):
        self.say(2, bot.BTN_FIND)
        out = self.say(2, GOOD)[0]
        self.assertNotIn("Нашёл по названию", out)
        self.assertNotIn("Что искать", out)

    def test_other_button_cancels_waiting(self):
        self.say(2, bot.BTN_FIND)
        self.assertIn("Привет", self.say(2, "❓ Помощь")[0])
        self.assertIn("Пришлите фото штрихкода", self.say(2, "крем рук")[0])

    def test_waiting_expires(self):
        self.say(2, bot.BTN_FIND)
        self.brain.waiting_find[2] = bot.time.time() - 1000
        self.assertIn("Пришлите фото штрихкода", self.say(2, "крем рук")[0])

    def test_admin_buttons(self):
        self.assertEqual(self.say(1, "📋 Список"), ["Список пуст."])
        self.assertIn("только для администратора", self.say(2, "📋 Список")[0])

    def test_replies_carry_the_keyboard(self):
        sent = []

        class FakeTg:
            def call(self, *a, **k): return {}
            def load_photo(self, file_id): return b""
            def send(self, chat_id, text, markup=None): sent.append((chat_id, markup))
            def send_file(self, *a, **k): pass
        upd = {"message": {"chat": {"type": "private", "id": 2}, "from": {"id": 2, "first_name": "u"}, "text": "/help"}}
        bot.process_update(FakeTg(), self.brain, upd)
        self.assertEqual(sent[-1][1]["keyboard"][0][0]["text"], bot.BTN_FIND)
        upd["message"]["from"]["id"] = upd["message"]["chat"]["id"] = 99
        bot.process_update(FakeTg(), self.brain, upd)
        self.assertEqual(sent[-1][1], {"remove_keyboard": True})

    def test_send_passes_markup_as_json(self):
        calls = []
        t = bot.Tg("T")
        t.call = lambda method, **p: calls.append(p) or {}
        t.send(1, "привет", markup={"remove_keyboard": True})
        self.assertEqual(json.loads(calls[0]["reply_markup"]), {"remove_keyboard": True})
        t.send(1, "без кнопок")
        self.assertNotIn("reply_markup", calls[1])



class ImportSupplierTests(unittest.TestCase):
    def test_leading_zero_restored_and_no_duplicates(self):
        import openpyxl
        tmp = tempfile.mkdtemp()
        xlsx = os.path.join(tmp, "supplier.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append([])
        ws.append(["№", "Номенклатура", "NTIN"])
        ws.append([1, "Товар один", 200219275439])            # Excel потерял ведущий ноль
        ws.append([2, "Товар два", 200219275590])
        wb.save(xlsx)
        base = os.path.join(tmp, "base.csv")
        pd.DataFrame(columns=["ntin", "gtin", "name", "oktru", "source"]).to_csv(base, index=False)
        cmd = [sys.executable, os.path.join(ROOT, "import_supplier.py"), xlsx, "--source", "Тест", "--base", base]
        subprocess.run(cmd, check=True, capture_output=True)
        df = pd.read_csv(base, dtype=str).fillna("")
        self.assertEqual(list(df["ntin"]), ["0200219275439", "0200219275590"])
        self.assertEqual(list(df["name"]), ["Товар один", "Товар два"])
        subprocess.run(cmd, check=True, capture_output=True)    # повторный запуск
        self.assertEqual(len(pd.read_csv(base, dtype=str)), 2)


class BaseToolsTests(unittest.TestCase):
    """Инструменты базы кодов: загрузка партии из CSV, проверка по каталогу, очистка."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.base = os.path.join(self.tmp, "base.csv")

    def write_base(self, rows):
        pd.DataFrame(rows, columns=["ntin", "gtin", "name", "oktru", "source"]).to_csv(self.base, index=False)

    def test_csv_batch_keeps_row_sources_and_is_idempotent(self):
        self.write_base([["0200000000001", "", "Старый", "", "Прайс"]])
        batch = os.path.join(self.tmp, "batch.csv")
        pd.DataFrame([["0200000000002", "4005808588794", "Новый один", "", "Источник А"],
                      ["0200000000003", "", "Новый два", "", "Источник Б"]],
                     columns=["ntin", "gtin", "name", "oktru", "source"]).to_csv(batch, index=False)
        cmd = [sys.executable, os.path.join(ROOT, "import_supplier.py"), batch, "--source", "Общая", "--base", self.base]
        subprocess.run(cmd, check=True, capture_output=True)
        df = pd.read_csv(self.base, dtype=str).fillna("")
        self.assertEqual(len(df), 3)
        row = df[df.ntin == "0200000000002"].iloc[0]
        self.assertEqual(row.gtin, "4005808588794")
        self.assertTrue(row.source.startswith("Источник А"))
        subprocess.run(cmd, check=True, capture_output=True)
        self.assertEqual(len(pd.read_csv(self.base, dtype=str)), 3)

    def test_audit_marks_active_deactivated_and_unknown(self):
        import audit_base
        self.write_base([["0200000000011", "", "Активный", "", "А"], ["0200000000012", "", "Отключённый", "", "А"],
                         ["0200000000013", "", "Неизвестный", "", "Б"]])
        cat = {"0200000000011": {"ntin_code": "0200000000011", "gtin": "4005808588794", "ntin_isdeactivated": False},
               "0200000000012": {"ntin_code": "0200000000012", "gtin": None, "ntin_isdeactivated": True}}
        ck.query_catalog = lambda ntins: ([cat[n] for n in ntins if n in cat], True)
        out = os.path.join(self.tmp, "audit.csv")
        audit_base.main(["--base", self.base, "--out", out])
        st = dict(zip(*[pd.read_csv(out, dtype=str).fillna("")[c] for c in ("ntin", "status")]))
        self.assertEqual(st, {"0200000000011": "активен", "0200000000012": "отключён", "0200000000013": "нет в каталоге"})

    def test_audit_stops_when_catalog_is_unreachable(self):
        import audit_base
        self.write_base([["0200000000011", "", "Активный", "", "А"]])
        ck.query_catalog = lambda ntins: (None, False)
        with self.assertRaises(SystemExit):
            audit_base.main(["--base", self.base, "--out", os.path.join(self.tmp, "audit.csv")])

    def test_clean_keeps_active_fills_barcodes_and_archives_the_rest(self):
        import clean_base
        self.write_base([["0200000000021", "", "Активный без штрихкода", "", "А"],
                         ["0200000000021", "", "Дубль строки", "", "А"],
                         ["0200000000022", "4005808588794", "Отключённый", "", "А"],
                         ["0200000000023", "", "Не было в проверке", "", "Б"]])
        audit = os.path.join(self.tmp, "audit.csv")
        pd.DataFrame([["0200000000021", "Активный", "А", "активен", "4600104010705", ""],
                      ["0200000000022", "Отключённый", "А", "отключён", "4005808588794", "4005808588794"]],
                     columns=["ntin", "name", "source", "status", "gtin_in_catalog", "gtin_in_base"]).to_csv(audit, index=False)
        out, gone = os.path.join(self.tmp, "clean.csv"), os.path.join(self.tmp, "gone.csv")
        clean_base.main(["--base", self.base, "--audit", audit, "--out", out, "--inactive-out", gone])
        clean = pd.read_csv(out, dtype=str).fillna("")
        self.assertEqual(sorted(clean.ntin), ["0200000000021", "0200000000023"])          # отключённый убран, непроверенный остался
        self.assertEqual(clean[clean.ntin == "0200000000021"].iloc[0].gtin, "4600104010705")  # штрихкод дописан из каталога
        self.assertEqual(list(pd.read_csv(gone, dtype=str).ntin), ["0200000000022"])
        self.assertEqual(len(pd.read_csv(self.base, dtype=str)), 4)                         # исходная база не тронута



if __name__ == "__main__":
    unittest.main()
