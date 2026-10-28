"""
Автотесты без сети и без токенов: каталог подменяется заглушкой.

Запуск из корня репозитория:   python -m unittest discover -s tests -v
"""
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


if __name__ == "__main__":
    unittest.main()
