#!/usr/bin/env python3
"""
Telegram-бот «штрихкод -> код NTIN» (этап 1).

Что делает:
  - принимает штрихкод цифрами или ФОТО штрихкода;
  - проверяет цифры (контрольная цифра), ищет код в нашей базе и в Национальном каталоге;
  - отвечает: «код есть, ничего делать не нужно» или «кода нет, записал в список»;
  - всё, что не нашлось, копится в списке для следующего этапа (регистрация карточек).

Настройки в файле .env рядом с программой (в чат не присылайте):
  TELEGRAM_BOT_TOKEN=токен от @BotFather
  BOT_ADMIN_USERS=123456789                   (ваш Telegram-номер; можно несколько через запятую)
  BOT_ALLOWED_USERS=111111111,222222222       (мама и девочки, вручную)
  BOT_INVITE_CODE=слово-и-цифры-2026          (необязательно: вход по коду; человек пишет боту /join КОД)
Свой номер каждый видит, написав боту /id.

Кнопки под полем ввода дублируют команды. Команды: /start /help /id /join /find для всех; /stats /notfound /export /users /remove только для администраторов.
Запуск: python bot.py
"""
import csv
import hmac
import io
import json
import logging
import os
import re
import signal
import sqlite3
import sys
import time
import traceback
import unicodedata
from datetime import datetime
from logging.handlers import RotatingFileHandler

import requests

import check_ntin as ck
import oktru
from nkt_client import _read_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("ntinbot")
ck.RETRIES, ck.TIMEOUT = 2, 12     # бот однопоточный: долгий ответ каталога не должен надолго «замораживать» всех


class ImageTooBig(Exception):
    """Картинка слишком велика для безопасной распаковки."""


class PhotoError(Exception):
    """Не удалось скачать фото из Telegram (текст без адреса и токена)."""

# статусы, по которым нужна дальнейшая работа человека или следующего этапа бота
LATEST = ("SELECT barcode, status, ts FROM (SELECT barcode, status, ts, ROW_NUMBER() OVER ("
          "PARTITION BY barcode ORDER BY (status = 'NOT_CHECKED'), id DESC) AS rn FROM scans) WHERE rn = 1")   # последний ЗНАЧИМЫЙ статус; NOT_CHECKED только если другого нет
TODO_STATUSES = ("NOT_FOUND", "INTERNAL_CODE", "MULTIPLE", "DEACTIVATED")
MAX_CODES = 5            # штрихкодов за одно сообщение (бот однопоточный)
BUDGET_SEC = 90          # сколько секунд на проверку одного сообщения
MAX_DOC_BYTES = 8_000_000
FIND_LIMIT = 8           # сколько найденных товаров показывать в ответ на /find
BTN_FIND = "🔎 Найти по названию"
# надпись на кнопке -> команда (кнопки лежат под полем ввода, нажатие приходит боту обычным сообщением)
BUTTONS = {BTN_FIND: "/find", "❓ Помощь": "/help", "🆔 Мой номер": "/id",
           "📋 Список": "/notfound", "📊 Статистика": "/stats", "📥 Экспорт": "/export", "👥 Люди": "/users"}
MENU_COMMANDS = [{"command": "find", "description": "Найти код по названию"},
                 {"command": "help", "description": "Как пользоваться"},
                 {"command": "id", "description": "Мой номер в Telegram"}]
STATE = {"stop": False, "busy": False, "last_alert": 0.0}


# ------------------------------------------------------------------ настройки
def parse_ids(s):
    return {int(x) for x in re.findall(r"\d+", s or "")}


def load_env():
    env = _read_dotenv(os.path.join(HERE, ".env"))
    env.update({k: v for k, v in os.environ.items() if k.startswith(("TELEGRAM_", "BOT_"))})
    return env


# ------------------------------------------------------------------ штрихкоды
def ascii_digits(text):
    """Полноширинные и арабские цифры -> обычные 0-9 (клавиатуры на телефонах бывают разные)."""
    return "".join(str(unicodedata.decimal(ch)) if ch.isdecimal() and not ch.isascii() else ch for ch in text)


def norm_text(s):
    """Для поиска по названию: нижний регистр, «ё» как «е», лишние пробелы убраны."""
    return re.sub(r"\s+", " ", str(s).lower().replace("ё", "е")).strip()


def gtin_ok(code):
    """Контрольная цифра GTIN-8/12/13/14."""
    if not (code.isascii() and code.isdigit()) or len(code) not in (8, 12, 13, 14):
        return False
    d = [int(x) for x in code]
    s = sum(d[-2::-2]) * 3 + sum(d[-3::-2])
    return (10 - s % 10) % 10 == d[-1]


def extract_codes(text):
    """Штрихкоды из текста: один код (можно с пробелами) или несколько, по одному в строке/через запятую/пробел."""
    whole = re.sub(r"[^0-9]", "", text)
    if gtin_ok(whole):
        return [whole]
    out = []
    for part in re.split(r"[\n,;]+", text):
        d = re.sub(r"[^0-9]", "", part)
        cands = [d] if 8 <= len(d) <= 14 else ([re.sub(r"[^0-9]", "", t) for t in part.split()] if len(d) > 14 else [])
        for c in cands:
            if 8 <= len(c) <= 14 and c not in out:
                out.append(c)
    return out[:50]


def image_pixels(data):
    """Число пикселей по заголовку PNG или JPEG (без распаковки), иначе None. Защита от «картинок-бомб»."""
    import struct
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return w * h
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker == 0xFF:                          # байт заполнения
                i += 1
                continue
            if marker in (0x00, 0x01) or 0xD0 <= marker <= 0xD9:      # маркеры без длины
                i += 2
                continue
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w * h
            i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    return None


def decode_barcode(data):
    """Достаёт штрихкод из фото (байты). Возвращает цифры или None. Нужен пакет opencv-python-headless."""
    code, resized = _decode_once(data, full=False)
    if code or not resized:
        return code
    px = image_pixels(data)                     # мелкий штрихкод на большом кадре: пробуем без уменьшения
    if px and px <= 16_000_000:
        return _decode_once(data, full=True)[0]
    return None


def _decode_once(data, full=False):
    """Один проход чтения. Возвращает (код или None, был ли кадр уменьшен)."""
    import cv2
    import numpy as np

    px = image_pixels(data)
    if px and px > 40_000_000:                           # больше ~40 мегапикселей не распаковываем
        raise ImageTooBig()
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None, False
    m = max(img.shape[:2])
    resized = m > 2000 and not full
    if resized:                                       # огромные кадры уменьшаем, иначе копии съедят память
        img = cv2.resize(img, None, fx=2000 / m, fy=2000 / m, interpolation=cv2.INTER_AREA)
    det = cv2.barcode.BarcodeDetector()
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    variants = [img, gray]
    for scale in (0.5, 0.35, 0.25, 1.5, 2.0):          # то уменьшаем (крупный штрихкод), то увеличиваем (мелкий)
        variants.append(cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC))
    h, w = gray.shape
    pad = int(0.3 * max(h, w))                           # поля вокруг: детектору нужен «фон», иначе плотный кадр не находится
    padded = cv2.copyMakeBorder(gray, pad, pad, pad, pad, cv2.BORDER_REPLICATE)
    variants.append(padded)
    variants.append(cv2.resize(padded, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA))
    variants.append(cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE))
    variants.append(cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray))
    for v in variants:
        try:
            ok, infos, _types, _pts = det.detectAndDecodeWithType(v)
        except cv2.error:
            continue
        if not ok:
            continue
        for info in infos:
            code = re.sub(r"[^0-9]", "", info or "")
            if code and gtin_ok(code):
                return code, resized
    return None, resized


# ------------------------------------------------------------------ база данных
def connect(path=None):
    db = oktru.connect(path) if path else oktru.connect()
    db.execute("""CREATE TABLE IF NOT EXISTS scans(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user_id INTEGER, user_name TEXT,
        barcode TEXT, status TEXT, ntin TEXT, name TEXT, comment TEXT)""")
    db.execute("CREATE TABLE IF NOT EXISTS members(user_id INTEGER PRIMARY KEY, name TEXT, joined_at TEXT)")
    db.execute("CREATE INDEX IF NOT EXISTS ix_scans_barcode ON scans(barcode, id)")
    db.execute("CREATE INDEX IF NOT EXISTS ix_scans_ts ON scans(ts)")
    db.commit()
    return db


class Brain:
    """Вся логика ответов, без обращений к Telegram (так её легко проверять)."""

    def __init__(self, db, admins, allowed, base_path=ck.DEFAULT_BASE, invite=""):
        self.db, self.admins, self.allowed = db, set(admins), set(allowed) | set(admins)
        self.invite = (invite or "").strip().lower()
        self.fails = {}          # неудачные попытки ввода кода: {номер: [время, ...]}
        self.outbox = []         # сообщения администраторам: [(chat_id, текст)]
        self.stranger_last = {}  # когда последний раз отвечали постороннему: {номер: время}
        self.join_alert = 0.0    # когда последний раз предупреждали админов о переборе кода
        self.waiting_find = {}   # кто нажал «Найти по названию» и ждёт, что искать: {номер: время}
        base = ck.load_base(base_path)
        self.idx, self.names = ck.base_index(base)
        self.known = {ck.norm(n) for n in base["ntin"]}
        self.rows = [(str(r.ntin), str(r.gtin), str(r.name)) for r in base.itertuples()]   # для /find
        self.rows_low = [norm_text(r[2]) for r in self.rows]

    # -- поиск кода
    def lookup(self, code):
        if ck.is_internal(code):
            return None, "INTERNAL_CODE", "", "", ""
        base_n = self.idx.get(ck.norm(code), [])
        items = ck.fetch([code])
        api_ok = items is not None
        items = list(items or [])
        if api_ok and base_n:
            more = ck.fetch(base_n)
            if more is None:
                api_ok = False          # каталог ответил не полностью, подтвердить код нельзя
            else:
                items += more
        by_g, by_n = ck.index_items(items)
        ntin, status, src, comment, name = ck.classify(
            code, base_n, by_g, by_n, api_ok, self.names.get(ck.norm(code), ""), self.known)
        return ntin, status, src, comment, name

    def has_access(self, uid):
        if uid in self.allowed:
            return True
        return self.db.execute("SELECT 1 FROM members WHERE user_id=?", (uid,)).fetchone() is not None

    def join(self, uid, uname, text):
        if self.has_access(uid):
            return "У вас уже есть доступ. Пришлите фото штрихкода."
        if not self.invite:
            return "Вход по коду не включён. Попросите администратора добавить вас (ваш номер: %s)." % uid
        now = time.time()
        self.fails = {u: [t for t in ts if now - t < 3600] for u, ts in self.fails.items()}
        self.fails = {u: ts for u, ts in self.fails.items() if ts}
        if sum(len(ts) for ts in self.fails.values()) >= 30:      # перебор кода с разных аккаунтов
            if now - self.join_alert > 3600:
                self.join_alert = now
                for a in self.admins:
                    self.outbox.append((a, "⚠️ Много неверных кодов входа за час. Вход по коду закрыт на время, смените BOT_INVITE_CODE."))
            return "Вход по коду временно закрыт. Обратитесь к администратору."
        tries = self.fails.get(uid, [])
        if len(tries) >= 5:
            return "Слишком много неверных попыток. Попробуйте через час."
        parts = text.split(maxsplit=1)
        code = parts[1].strip().lower() if len(parts) > 1 else ""
        if not hmac.compare_digest(code.encode("utf-8"), self.invite.encode("utf-8")):
            tries.append(now)
            self.fails[uid] = tries
            return "Код не подошёл. Проверьте и отправьте ещё раз: /join КОД"
        self.db.execute("INSERT OR REPLACE INTO members VALUES(?,?,?)", (uid, uname, datetime.now().isoformat(timespec="seconds")))
        self.db.commit()
        self.fails.pop(uid, None)
        for a in self.admins:
            self.outbox.append((a, f"➕ Новый пользователь по коду: {uname or '?'} (номер {uid})"))
        return "✅ Доступ открыт. Пришлите фото штрихкода (цифры должны быть видны) или введите цифры."

    def record(self, uid, uname, code, status, ntin, name, comment=""):
        self.db.execute("INSERT INTO scans(ts,user_id,user_name,barcode,status,ntin,name,comment) VALUES(?,?,?,?,?,?,?,?)",
                        (datetime.now().isoformat(timespec="seconds"), uid, uname, code, status, ntin or "", name or "", comment))
        self.db.commit()

    def answer_code(self, uid, uname, raw):
        code = re.sub(r"[^0-9]", "", raw)
        if not gtin_ok(code):
            return ("Цифры не прошли проверку: вероятно, ошибка при чтении. "
                    "Введите цифры ещё раз или сфотографируйте штрихкод крупнее и ровнее.")
        ntin, status, _src, comment, name = self.lookup(code)
        self.record(uid, uname, code, status, ntin, name, comment)
        nm = f"\n{name}" if name else ""
        if status == "FOUND":
            return f"✅ Код есть: {ntin}{nm}\nНичего делать не нужно."
        if status == "FOUND_UNVERIFIED":
            return f"✅ Код из нашей базы: {ntin}{nm}\n(Каталог его не подтвердил, возможно временно не отвечает.) Ничего делать не нужно."
        if status == "MULTIPLE":
            return (f"⚠️ На этот штрихкод несколько кодов: {ntin}{nm}\n"
                    "Выбор сделает администратор, запись сохранена.")
        if status == "DEACTIVATED":
            extra = f" Замена: {ntin}." if ntin else ""
            return f"⚠️ Карточка отключена.{extra}{nm}\nЗапись сохранена, разберёмся."
        if status == "INTERNAL_CODE":
            return ("ℹ️ Это внутренний магазинный штрихкод (начинается с 2), по нему нельзя искать в каталоге.\n"
                    "Запись сохранена. На следующем этапе бот попросит фото упаковки.")
        if status == "NOT_CHECKED":
            return "⏳ Каталог сейчас не отвечает. Запись сохранена, повторите через несколько минут."
        return ("❌ В каталоге кода нет. Записал в список «нужно зарегистрировать».\n"
                "Можно поискать по названию: нажмите кнопку «🔎 Найти по названию» внизу.\n"
                "На следующем этапе бот попросит фото упаковки.")

    # -- кнопки под полем ввода
    def markup_for(self, uid):
        """Постоянные кнопки: у своих сотрудников меню, у чужих убираем старое меню."""
        if not self.has_access(uid):
            return {"remove_keyboard": True}
        rows = [[BTN_FIND], ["❓ Помощь", "🆔 Мой номер"]]
        if uid in self.admins:
            rows += [["📋 Список", "📊 Статистика"], ["📥 Экспорт", "👥 Люди"]]
        return {"keyboard": [[{"text": t} for t in row] for row in rows], "resize_keyboard": True, "is_persistent": True}

    # -- поиск по названию
    def find_by_name(self, text):
        """/find слова: ищет готовые коды по словам из названия (нужны все слова), актуальность проверяет по каталогу."""
        parts = text.split(None, 1)
        words = norm_text(parts[1]).split() if len(parts) > 1 else []
        if not words:
            return ["Напишите, что искать, например:\n/find бальзам после бритья\n"
                    "Ищу по названию среди готовых кодов. Чем точнее слова (бренд, вид товара, объём), тем лучше."]
        hits = [r for r, low in zip(self.rows, self.rows_low) if all(w in low for w in words)]
        if not hits:
            return ["Ничего не нашёл. Попробуйте меньше слов или другое слово (например, только бренд и вид товара). "
                    "Если и по штрихкоду кода нет, пришлите фото упаковки администратору."]
        hits.sort(key=lambda r: (len(r[2]), r[2]))        # короче название обычно точнее
        shown = hits[:FIND_LIMIT]
        items = ck.fetch([h[0] for h in shown])
        note = ""
        if not items:                                      # каталог не ответил: показываем, но предупреждаем
            note = "\n(Каталог не ответил, действует ли код, не проверено.)"
        else:
            by_n = {ck.norm(it.get("ntin_code")): it for it in items}
            ok = [h for h in shown if ck.norm(h[0]) in by_n and not by_n[ck.norm(h[0])].get("ntin_isdeactivated")]
            if len(ok) < len(shown):
                note = f"\n(Ещё {len(shown) - len(ok)} подходящих кодов в каталоге отключены, их не показываю.)"
            shown = ok
        if not shown:
            return ["Подходящие коды в каталоге отключены, использовать их нельзя. Пришлите фото упаковки администратору." + note]
        lines = [f"{h[0]}  {h[2]}" for h in shown]
        head = f"Нашёл по названию: {len(hits)}. Показываю {len(shown)}:" if len(hits) > len(shown) else f"Нашёл по названию: {len(shown)}:"
        tail = "\nДобавьте ещё слово, чтобы сузить поиск." if len(hits) > FIND_LIMIT else ""
        return [head + "\n" + "\n".join(lines) + tail + note + "\nПроверьте, что это тот же товар (название, объём, оттенок)."]

    # -- сообщения
    def handle(self, msg, photo_loader=None):
        """msg: объект сообщения Telegram. Возвращает список ответов (строки) или ('file', имя, байты, подпись)."""
        chat = msg.get("chat", {})
        if chat.get("type") != "private":
            return []
        user = msg.get("from") or {}
        uid, uname = user.get("id"), (user.get("username") or user.get("first_name") or "")
        if uid is None:
            return []
        text = ascii_digits((msg.get("text") or "").strip())
        text = BUTTONS.get(text, text)          # нажатие кнопки = команда
        cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""

        if cmd == "/id":
            return [f"Ваш номер в Telegram: {uid}\nПередайте его администратору, чтобы вам открыли доступ."]
        if cmd == "/join":
            return [self.join(uid, uname, text)]
        if not self.has_access(uid):
            now = time.time()
            if now - self.stranger_last.get(uid, 0) < 60:        # постороннему отвечаем не чаще раза в минуту
                return []
            if len(self.stranger_last) > 1000:
                self.stranger_last.clear()
            self.stranger_last[uid] = now
            log.info("нет доступа: %s %s", uid, uname)
            hint = "\nЕсли у вас есть код приглашения, отправьте: /join КОД" if self.invite else ""
            return [f"Нет доступа. Ваш номер: {uid}. Сообщите его администратору.{hint}"]
        waiting = self.waiting_find.pop(uid, None)       # любое сообщение сбрасывает режим «жду, что искать»
        if (waiting and time.time() - waiting < 600 and text and not cmd and not msg.get("photo")
                and not msg.get("document") and not re.search(r"[0-9]{6,}", text)):
            return self.find_by_name("/find " + text)
        if cmd == "/find":
            if len(text.split(None, 1)) < 2:
                self.waiting_find[uid] = time.time()
                return ["🔎 Что искать? Напишите слова из названия одним сообщением, например:\nпомада губная avon\n"
                        "Чем точнее слова (бренд, вид товара, объём), тем лучше."]
            return self.find_by_name(text)
        if cmd in ("/start", "/help"):
            return ["Привет! Пришлите фото штрихкода (цифры должны быть видны) или введите цифры.\n"
                    "Я отвечу, есть ли на этот товар код NTIN. Если код есть, больше ничего делать не нужно.\n"
                    "Если штрихкод не находится, нажмите кнопку «🔎 Найти по названию» внизу и напишите слова из названия, например: бальзам после бритья"]
        if cmd in ("/stats", "/notfound", "/export", "/users", "/remove"):
            if uid not in self.admins:
                return ["Эта команда только для администратора."]
            return self.admin_command(cmd, text)
        file_id = None
        if msg.get("photo"):
            file_id = msg["photo"][-1]["file_id"]
        elif msg.get("document") and str(msg["document"].get("mime_type", "")).startswith("image/"):
            if msg["document"].get("file_size", 0) > MAX_DOC_BYTES:
                return ["Файл слишком большой (больше 8 МБ). Отправьте обычное фото штрихкода."]
            file_id = msg["document"]["file_id"]
        if file_id:
            if photo_loader is None:
                return ["Фото сейчас читать не получается, введите цифры штрихкода."]
            try:
                code = decode_barcode(photo_loader(file_id))
            except ImportError:
                return ["Чтение фото не настроено на сервере (нет opencv). Введите цифры штрихкода."]
            except ImageTooBig:
                return ["Картинка слишком большая. Отправьте обычное фото штрихкода (можно снять ближе)."]
            except PhotoError as e:
                log.warning("фото не загружено: %s", e)
                return ["Не удалось загрузить фото. Отправьте ещё раз или введите цифры штрихкода."]
            if not code:
                return ["Не удалось прочитать штрихкод на фото. Сфотографируйте ближе и ровнее, без бликов, "
                        "или введите цифры вручную."]
            return [f"Прочитал штрихкод: {code}", self.answer_code(uid, uname, code)]
        codes = extract_codes(text)
        if len(codes) > 1:
            total, replies, start, done, timed_out = len(codes), [], time.time(), 0, False
            for c in codes[:MAX_CODES]:
                if time.time() - start > BUDGET_SEC:
                    timed_out = True
                    break
                replies.append(f"{c}\n{self.answer_code(uid, uname, c)}")
                done += 1
            if done < total:
                why = "время проверки вышло. " if timed_out else ""
                replies.append(f"Обработаны первые {done} из {total} ({why}лимит сообщения {MAX_CODES}). Остальные отправьте отдельным сообщением.")
            return replies
        if codes:
            return [self.answer_code(uid, uname, codes[0])]
        if re.search(r"[0-9]{8}", text):
            return [self.answer_code(uid, uname, text)]
        return ["Пришлите фото штрихкода или цифры штрихкода (8, 12, 13 или 14 цифр).\nИскать по названию: кнопка «🔎 Найти по названию» внизу."]

    # -- администратор
    def admin_command(self, cmd, text=""):
        if cmd == "/users":
            rows = self.db.execute("SELECT user_id, name, joined_at FROM members ORDER BY joined_at").fetchall()
            lines = ["Администраторы: " + (", ".join(map(str, sorted(self.admins))) or "нет"),
                     "Добавлены вручную (.env): " + (", ".join(map(str, sorted(self.allowed - self.admins))) or "нет"),
                     f"Вошли по коду: {len(rows)}"]
            lines += [f"  {n or '?'} ({i}), {t}" for i, n, t in rows]
            return ["\n".join(lines)]
        if cmd == "/remove":
            m = re.search(r"[0-9]{1,15}", text)
            if not m:
                return ["Укажите номер: /remove 123456789"]
            uid = int(m.group())
            n = self.db.execute("DELETE FROM members WHERE user_id=?", (uid,)).rowcount
            self.db.commit()
            if n:
                return [f"Доступ {uid} закрыт. Чтобы он не вернулся по коду, смените BOT_INVITE_CODE в .env и перезапустите бота."]
            return [f"{uid} среди вошедших по коду не найден (если он в .env, уберите его оттуда)."]
        if cmd == "/stats":
            today = datetime.now().strftime("%Y-%m-%d")
            rows = self.db.execute("SELECT status, COUNT(*) FROM scans WHERE ts LIKE ? GROUP BY status", (today + "%",)).fetchall()
            users = self.db.execute("SELECT user_name, COUNT(*) FROM scans WHERE ts LIKE ? GROUP BY user_id", (today + "%",)).fetchall()
            ph = ",".join("?" * len(TODO_STATUSES))
            total_todo = self.db.execute(f"SELECT COUNT(*) FROM ({LATEST}) WHERE status IN ({ph})", TODO_STATUSES).fetchone()[0]
            total_nc = self.db.execute(f"SELECT COUNT(*) FROM ({LATEST}) WHERE status='NOT_CHECKED'").fetchone()[0]
            lines = [f"Сегодня проверок: {sum(c for _, c in rows)}"] + [f"  {s}: {c}" for s, c in rows]
            lines += ["По людям: " + ", ".join(f"{u or '?'} {c}" for u, c in users)] if users else []
            lines.append(f"В списке «нужно зарегистрировать»: {total_todo}; не удалось проверить (каталог не отвечал): {total_nc}")
            return ["\n".join(lines)]
        if cmd == "/notfound":
            ph = ",".join("?" * len(TODO_STATUSES))
            todo = self.db.execute(f"SELECT barcode, status FROM ({LATEST}) WHERE status IN ({ph}) ORDER BY ts DESC LIMIT 40", TODO_STATUSES).fetchall()
            nc = self.db.execute(f"SELECT barcode FROM ({LATEST}) WHERE status='NOT_CHECKED' ORDER BY ts DESC LIMIT 20").fetchall()
            if not todo and not nc:
                return ["Список пуст."]
            parts = []
            if todo:
                parts.append("Нужно зарегистрировать (последние 40):\n" + "\n".join(f"{b}  {st}" for b, st in todo))
            if nc:
                parts.append("Не удалось проверить (каталог не отвечал), отправьте ещё раз:\n" + "\n".join(b for (b,) in nc))
            return ["\n\n".join(parts)]
        # /export
        rows = self.db.execute("SELECT ts, barcode, status, ntin, name, user_name FROM scans ORDER BY id").fetchall()
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["Время", "Штрихкод", "Статус", "NTIN", "Название", "Кто"])
        w.writerows([[csv_safe(x) for x in r] for r in rows])
        return [("file", "scans.csv", ("\ufeff" + buf.getvalue()).encode("utf-8"), f"Всего записей: {len(rows)}")]


# ------------------------------------------------------------------ Telegram
def csv_safe(v):
    """Значения, начинающиеся с = + - @, Excel принимает за формулу: ставим апостроф."""
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def split_text(text, n=4000):
    """Режет длинный ответ на части (у Telegram лимит 4096 знаков)."""
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > n:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:n])
            line = line[n:]
        if cur and len(cur) + len(line) + 1 > n:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out or [""]


class RedactSecret(logging.Filter):
    """Заменяет секрет на *** во всех записях журнала, включая текст ошибок."""

    def __init__(self, secret):
        super().__init__()
        self.secret = secret

    def filter(self, record):
        if self.secret:
            record.msg, record.args = str(record.getMessage()).replace(self.secret, "***"), ()
            if record.exc_info:
                record.exc_text = "".join(traceback.format_exception(*record.exc_info)).replace(self.secret, "***")
                record.exc_info = None
        return True


class Tg:
    def __init__(self, token):
        self.api = f"https://api.telegram.org/bot{token}"
        self.files = f"https://api.telegram.org/file/bot{token}"

    last_status = 0

    def call(self, method, files=None, **params):
        attempt, waited = 0, 0
        while attempt < 4:
            try:
                self.last_status = 0
                r = requests.post(f"{self.api}/{method}", data=params, files=files, timeout=60)
                self.last_status = r.status_code
                if r.status_code == 429:
                    try:
                        wait = r.json().get("parameters", {}).get("retry_after", 2)
                    except ValueError:
                        wait = 2
                    if waited + wait > 60:
                        log.warning("Telegram %s: слишком долгое ограничение частоты (%s с), пропускаю", method, wait)
                        return None
                    waited += wait
                    time.sleep(wait)
                    continue                      # ожидание по просьбе Telegram не считаем попыткой
                if 400 <= r.status_code < 500:    # повторять бесполезно (бот заблокирован, неверный запрос, конфликт копий)
                    log.warning("Telegram %s: HTTP %s %s", method, r.status_code, r.text[:150])
                    return None
                r.raise_for_status()
                return r.json().get("result")
            except requests.RequestException as e:
                # токен в тексте ошибки не печатаем
                attempt += 1
                log.warning("Telegram %s: ошибка сети (%s) попытка %s", method, type(e).__name__, attempt)
                time.sleep(min(2 ** attempt, 20))
        return None

    def send(self, chat_id, text, markup=None):
        res = None
        extra = {"reply_markup": json.dumps(markup, ensure_ascii=False)} if markup else {}
        for chunk in split_text(text):
            res = self.call("sendMessage", chat_id=chat_id, text=chunk, **extra)
        return res

    def send_file(self, chat_id, name, data, caption=""):
        return self.call("sendDocument", files={"document": (name, data)}, chat_id=chat_id, caption=caption)

    def load_photo(self, file_id):
        info = self.call("getFile", file_id=file_id)
        if not info or not info.get("file_path"):
            raise PhotoError("Telegram не вернул путь к файлу")
        try:
            r = requests.get(f"{self.files}/{info['file_path']}", timeout=60)
            r.raise_for_status()
        except requests.RequestException as e:
            # у ошибки requests в тексте адрес с токеном, поэтому цепочку исключений не сохраняем
            raise PhotoError(f"скачивание не удалось ({type(e).__name__})") from None
        return r.content


def process_update(tg, brain, upd):
    msg = upd.get("message")
    if not msg:
        return
    chat_id = (msg.get("chat") or {}).get("id")
    uid = (msg.get("from") or {}).get("id")
    if chat_id is None or uid is None:
        return
    try:
        if brain.has_access(uid) and (msg.get("photo") or msg.get("document") or msg.get("text")):
            tg.call("sendChatAction", chat_id=chat_id, action="typing")
        for reply in brain.handle(msg, photo_loader=tg.load_photo):
            if isinstance(reply, tuple):
                tg.send_file(chat_id, reply[1], reply[2], reply[3])
            else:
                tg.send(chat_id, reply, markup=brain.markup_for(uid))
        while brain.outbox:
            chat, text = brain.outbox.pop(0)
            tg.send(chat, text)
    except Exception:
        log.exception("ошибка при обработке сообщения")
        tg.send(chat_id, "Что-то пошло не так, попробуйте ещё раз.")
        if time.time() - STATE["last_alert"] > 600:          # администраторов предупреждаем не чаще раза в 10 минут
            STATE["last_alert"] = time.time()
            for a in brain.admins:
                tg.send(a, "⚠️ Ошибка в боте при обработке сообщения, подробности в bot.log")


def poll_once(tg, brain, db, offset):
    """Один круг: получить сообщения и обработать. Возвращает новый offset."""
    updates = tg.call("getUpdates", offset=offset, timeout=30, allowed_updates='["message"]')
    if updates is None:
        if tg.last_status == 401:
            log.critical("Telegram отклонил токен (401). Проверьте TELEGRAM_BOT_TOKEN в .env")
            sys.exit(1)
        if tg.last_status == 409:
            log.error("Конфликт (409): запущена ещё одна копия бота с этим токеном. Жду минуту")
            time.sleep(60)
        else:
            time.sleep(5)
        return offset
    for upd in updates:
        offset = upd["update_id"] + 1
        oktru.meta_set(db, "tg_offset", offset)      # сначала запоминаем, потом обрабатываем:
        db.commit()                                  # «ядовитое» сообщение не зациклит перезапуски бота
        STATE["busy"] = True
        try:
            process_update(tg, brain, upd)
        finally:
            STATE["busy"] = False
        if STATE["stop"]:
            break
    return offset


def _on_sigterm(signum, frame):
    STATE["stop"] = True
    if not STATE["busy"]:        # ничего не обрабатываем: можно выйти сразу, иначе доделаем сообщение и выйдем
        sys.exit(0)


def run():
    env = load_env()
    token = env.get("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("Нет TELEGRAM_BOT_TOKEN в файле .env")
    handler = RotatingFileHandler(os.path.join(HERE, "bot.log"), maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handlers = [handler, logging.StreamHandler()]
    for h in handlers:
        h.addFilter(RedactSecret(token))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    db = connect()
    brain = Brain(db, parse_ids(env.get("BOT_ADMIN_USERS")), parse_ids(env.get("BOT_ALLOWED_USERS")),
                  invite=env.get("BOT_INVITE_CODE", ""))
    if not brain.admins:
        log.warning("BOT_ADMIN_USERS не задан: напишите боту /id и впишите номер в .env")
    tg = Tg(token)
    offset = int(oktru.meta_get(db, "tg_offset", 0))
    log.info("Бот запущен. Доступ: администраторов %s, пользователей %s", len(brain.admins), len(brain.allowed))
    try:
        tg.call("setMyCommands", commands=json.dumps(MENU_COMMANDS, ensure_ascii=False))   # меню «/» слева от поля ввода
    except Exception:
        log.warning("не удалось задать меню команд")
    signal.signal(signal.SIGTERM, _on_sigterm)
    while not STATE["stop"]:
        try:
            offset = poll_once(tg, brain, db, offset)
        except SystemExit:
            raise
        except Exception:
            log.exception("ошибка в главном цикле")
            time.sleep(5)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("Остановлено")
