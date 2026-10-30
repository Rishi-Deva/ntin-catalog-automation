#!/usr/bin/env python3
"""
Диалог «➕ Добавить товар» в боте.

  фото упаковки (до 3) -> ИИ читает упаковку -> штрихкод есть в каталоге? -> по названию есть похожие карточки?
  -> если нигде нет: черновик новой карточки с кнопками «Верно» / «Исправить».

Правила:
  - подтверждает человек (кнопки), ИИ сам ничего в каталог не отправляет;
  - сначала поиск по штрихкоду, потом по названию во всём каталоге, новую карточку готовим только если нигде нет;
  - не больше AI_DAILY_LIMIT товаров в день на человека (по умолчанию 50) и месячный бюджет AI_MONTHLY_BUDGET_USD
    (по умолчанию 5 долларов), чтобы не потратить общий баланс;
  - фото хранятся AI_PHOTO_DAYS дней (по умолчанию 30), потом удаляются;
  - каждый расход ИИ записывается в таблицу ai_usage, каждый черновик в ai_drafts (по ним видно качество).
"""
import csv
import io
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import ai_add
import card
import check_ntin as ck

log = logging.getLogger("ntinbot")
SESSION_TTL = 1800          # сколько секунд ждём сотрудника, потом диалог сбрасывается
MAX_PHOTOS = 3
MAX_FIXES = 3
AI_COMMANDS = ("/add", "/done", "/cancel", "/pick", "/none", "/ok", "/fix", "/ai", "/codes")
FIELD_NAMES = {"country": "страна", "oktru": "категория", "tnved": "код ТН ВЭД", "manufacturer": "производитель",
               "kind": "вид товара", "unit": "единица", "quantity": "количество"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_usage(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, day TEXT, user_id INTEGER, kind TEXT,
    tokens_in INTEGER, tokens_out INTEGER, cost REAL);
CREATE INDEX IF NOT EXISTS ix_ai_usage_day ON ai_usage(day, user_id);
CREATE TABLE IF NOT EXISTS ai_drafts(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user_id INTEGER, user_name TEXT, status TEXT, ntin TEXT,
    product TEXT, spec TEXT, photos TEXT, cost REAL, fixes INTEGER DEFAULT 0, chosen_name TEXT, chosen_gtin TEXT);
"""


def csv_safe(v):
    """Значения, начинающиеся с = + - @, Excel принимает за формулу: ставим апостроф."""
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


STATUS_NAMES = {"found_barcode": "Найден по штрихкоду", "found_choice": "Код выбран ранее", "submitted": "Отправлен на модерацию", "published": "Опубликован, код получен", "matched": "Выбрана готовая карточка",
                "ready": "Черновик новой карточки", "incomplete": "Черновик новой карточки (неполный)"}


def human_error(e):
    return re.sub(r"«(\w+)»", lambda m: FIELD_NAMES.get(m.group(1), m.group(1)), e)


class AiFlow:
    def __init__(self, brain, env, decode_barcode, gtin_ok, photos_dir):
        self.b, self.env = brain, env or {}
        self.decode, self.gtin_ok, self.dir = decode_barcode, gtin_ok, photos_dir
        self.daily_limit = int(self.env.get("AI_DAILY_LIMIT") or 50)
        self.budget = float(self.env.get("AI_MONTHLY_BUDGET_USD") or 5)
        self.photo_days = int(self.env.get("AI_PHOTO_DAYS") or 30)
        self.sessions = {}
        self._last_purge = 0.0
        import dicts
        self.b.db.executescript(SCHEMA + dicts.SCHEMA)           # таблицы ИИ и справочников (если ещё нет)
        for col in ("chosen_name", "chosen_gtin"):                # у старых баз этих колонок ещё нет
            try:
                self.b.db.execute(f"ALTER TABLE ai_drafts ADD COLUMN {col} TEXT")
            except Exception:
                pass
        self.b.db.commit()
        os.makedirs(self.dir, exist_ok=True)

    # ------------------------------------------------------------ служебное
    def enabled(self):
        return bool(self.env.get("ANTHROPIC_API_KEY"))

    def active(self, uid):
        s = self.sessions.get(uid)
        if s and time.time() - s["t"] > SESSION_TTL:
            self.sessions.pop(uid, None)
            return False
        return s is not None

    def markup(self, uid):
        s = self.sessions.get(uid)
        if not s:
            return None
        mode = s["mode"]
        if mode == "collect":
            rows = [["✅ Готово"], ["✖️ Отмена"]]
        elif mode == "pick":
            rows = [[f"✅ №{i}" for i in range(1, len(s["cands"]) + 1)], ["❌ Нет подходящего"], ["✖️ Отмена"]]
        elif mode == "draft":
            rows = [["✅ Верно"], ["✏️ Исправить", "✖️ Отмена"]]
        else:
            rows = [["✖️ Отмена"]]
        return {"keyboard": [[{"text": t} for t in r] for r in rows], "resize_keyboard": True, "is_persistent": True}

    def check_limits(self, uid):
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        n = self.b.db.execute("SELECT COUNT(*) FROM ai_usage WHERE day=? AND user_id=? AND kind='extract'", (day, uid)).fetchone()[0]
        if n >= self.daily_limit:
            return False, f"Сегодня вы уже обработали {n} товаров, это дневной лимит ({self.daily_limit}). Продолжите завтра."
        month = day[:7]
        spent = self.b.db.execute("SELECT COALESCE(SUM(cost),0) FROM ai_usage WHERE day LIKE ?", (month + "%",)).fetchone()[0]
        if spent >= self.budget:
            return False, "ИИ-помощник остановлен: достигнут месячный лимит расходов. Сообщите администратору."
        return True, ""

    def log_usage(self, uid, kind, usage, s=None):
        if not usage:
            return
        now = datetime.now(timezone.utc)
        self.b.db.execute("INSERT INTO ai_usage(ts, day, user_id, kind, tokens_in, tokens_out, cost) VALUES(?,?,?,?,?,?,?)",
                          (now.isoformat(timespec="seconds"), now.strftime("%Y-%m-%d"), uid, kind,
                           usage["in"], usage["out"], usage["cost"]))
        self.b.db.commit()
        if s is not None:
            s["cost"] = s.get("cost", 0.0) + usage["cost"]

    def save_draft(self, uid, uname, s, status, ntin="", name="", gtin=""):
        vals = (status, ntin, json.dumps(s.get("product") or {}, ensure_ascii=False), json.dumps(s.get("spec") or {}, ensure_ascii=False),
                json.dumps(s["photos"], ensure_ascii=False), s.get("cost", 0.0), s.get("fixes", 0), name, gtin)
        if s.get("draft_id"):
            self.b.db.execute("UPDATE ai_drafts SET status=?, ntin=?, product=?, spec=?, photos=?, cost=?, fixes=?, "
                              "chosen_name=?, chosen_gtin=? WHERE id=?", vals + (s["draft_id"],))
        else:
            cur = self.b.db.execute("INSERT INTO ai_drafts(ts, user_id, user_name, status, ntin, product, spec, photos, cost, fixes, "
                                    "chosen_name, chosen_gtin) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                    (datetime.now(timezone.utc).isoformat(timespec="seconds"), uid, uname) + vals)
            s["draft_id"] = cur.lastrowid
        self.b.db.commit()
        return s["draft_id"]

    def purge_photos(self):
        """Удаляет фото старше AI_PHOTO_DAYS дней. Возвращает число удалённых файлов."""
        n, limit = 0, time.time() - self.photo_days * 86400
        for name in os.listdir(self.dir):
            p = os.path.join(self.dir, name)
            try:
                if os.path.isfile(p) and os.path.getmtime(p) < limit:
                    os.remove(p)
                    n += 1
            except OSError:
                pass
        return n

    def tick(self):
        """Раз в сутки чистит старые фото (вызывается из главного цикла)."""
        if time.time() - self._last_purge > 86400:
            self._last_purge = time.time()
            n = self.purge_photos()
            if n:
                log.info("удалено старых фото: %s", n)

    def codes_file(self):
        """Таблица всего, что сотрудники подобрали через «Добавить товар»: для кассы и товарной базы."""
        rows = self.b.db.execute("SELECT id, ts, user_name, status, ntin, chosen_name, chosen_gtin, spec FROM ai_drafts "
                                 "WHERE status IN ('found_barcode','found_choice','matched','ready','incomplete','submitted','published') ORDER BY id").fetchall()
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["Дата", "Кто", "Что произошло", "Код NTIN", "Название", "Штрихкод", "Из нашей базы", "Номер записи"])
        for did, ts, who, status, ntin, name, gtin, spec in rows:
            if status in ("ready", "incomplete"):
                try:
                    name = card.compose_names(json.loads(spec or "{}"))[0]
                except Exception:
                    name = name or ""
                gtin = gtin or (json.loads(spec or "{}").get("gtin") or "")
            w.writerow([csv_safe(x) for x in (ts[:16].replace("T", " "), who, STATUS_NAMES.get(status, status), ntin or "",
                                                name or "", gtin or "", ("да" if ntin and self.b.in_base(ntin) else ("нет" if ntin else "")), did)])
        return ("file", "codes_ai.csv", ("\ufeff" + buf.getvalue()).encode("utf-8"), f"Записей: {len(rows)}")

    def stats(self):
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today = self.b.db.execute("SELECT COUNT(*) FROM ai_usage WHERE day=? AND kind='extract'", (day,)).fetchone()[0]
        spent = self.b.db.execute("SELECT COALESCE(SUM(cost),0) FROM ai_usage WHERE day LIKE ?", (day[:7] + "%",)).fetchone()[0]
        rows = self.b.db.execute("SELECT status, COUNT(*), SUM(CASE WHEN fixes=0 THEN 1 ELSE 0 END) FROM ai_drafts GROUP BY status").fetchall()
        names = {"found_barcode": "найден по штрихкоду", "matched": "выбрана готовая карточка", "ready": "черновик готов",
                 "incomplete": "черновик неполный", "cancelled": "отменено", "draft": "не завершено"}
        lines = [f"🤖 ИИ-помощник: сегодня обработано товаров {today}, за месяц потрачено ${spent:.4f} из бюджета ${self.budget:.2f}.",
                 f"Лимиты: {self.daily_limit} товаров в день на человека. Ключ: {'есть' if self.enabled() else 'НЕТ'}."]
        total = sum(r[1] for r in rows)
        lines.append(f"Всего обработано: {total}")
        for st, c, no_fix in rows:
            lines.append(f"  {names.get(st, st)}: {c}" + (f" (без исправлений {no_fix})" if st in ("ready", "incomplete") else ""))
        return "\n".join(lines)

    # ------------------------------------------------------------ диалог
    @staticmethod
    def _file_id(msg):
        if msg.get("photo"):
            return msg["photo"][-1]["file_id"]
        d = msg.get("document")
        if d and str(d.get("mime_type", "")).startswith("image/") and d.get("file_size", 0) <= 8_000_000:
            return d["file_id"]
        return None

    def start(self, uid):
        if not self.enabled():
            return ["ИИ-помощник пока не подключён. Попросите администратора добавить ключ."]
        ok, why = self.check_limits(uid)
        if not ok:
            return [why]
        self.sessions[uid] = {"mode": "collect", "t": time.time(), "photos": [], "barcode": "", "note": "", "fixes": 0, "cost": 0.0}
        return ["➕ Добавляем товар.\n"
                "1) Пришлите фото упаковки спереди.\n"
                "2) Пришлите фото стороны с производителем (где страна и изготовитель).\n"
                "3) Если есть штрихкод, сфотографируйте его или напишите цифры.\n"
                "Можно до 3 фото. Любой текст (например «цвет красный») я передам ИИ как подсказку.\n"
                "Когда всё отправили, нажмите «✅ Готово»."]

    def handle(self, uid, uname, msg, text, cmd, photo_loader=None, notify=None):
        """Возвращает список ответов или None, если сообщение не относится к добавлению товара."""
        notify = notify or (lambda t: None)
        if cmd == "/add":
            return self.start(uid)
        if cmd == "/ai":
            return [self.stats()] if uid in self.b.admins else ["Эта команда только для администратора."]
        if cmd == "/codes":
            return [self.codes_file()] if uid in self.b.admins else ["Эта команда только для администратора."]
        if not self.active(uid):
            if cmd in ("/done", "/ok", "/fix", "/pick", "/none", "/cancel"):
                return ["Сейчас нет начатого добавления товара. Нажмите «➕ Добавить товар»."]
            return None
        s = self.sessions[uid]
        if cmd == "/cancel":
            self.sessions.pop(uid, None)
            if s.get("draft_id"):
                self.save_draft(uid, uname, s, "cancelled")
            return ["Отменил. Фото и данные не сохраняются в каталоге."]
        if cmd and cmd not in AI_COMMANDS:
            self.sessions.pop(uid, None)             # любая другая команда сбрасывает диалог
            return None
        s["t"] = time.time()
        mode = s["mode"]
        if mode == "collect":
            return self._collect(uid, uname, s, msg, text, cmd, photo_loader, notify)
        if mode == "pick":
            return self._pick(uid, uname, s, text, cmd, notify)
        if mode == "draft":
            return self._draft(uid, uname, s, cmd, notify)
        return self._fix(uid, uname, s, text, cmd, notify)

    def _collect(self, uid, uname, s, msg, text, cmd, photo_loader, notify):
        fid = self._file_id(msg)
        if fid:
            if len(s["photos"]) >= MAX_PHOTOS:
                return [f"Уже принял {MAX_PHOTOS} фото. Нажмите «✅ Готово» или «✖️ Отмена»."]
            if photo_loader is None:
                return ["Фото сейчас принять не получается."]
            try:
                data = photo_loader(fid)
            except Exception:
                return ["Не удалось загрузить фото. Отправьте ещё раз."]
            path = os.path.join(self.dir, f"{uid}_{int(time.time())}_{len(s['photos']) + 1}.jpg")
            with open(path, "wb") as f:
                f.write(data)
            s["photos"].append(path)
            out = f"Принял фото {len(s['photos'])} из {MAX_PHOTOS}."
            if not s["barcode"]:
                try:
                    code = self.decode(data)
                except Exception:
                    code = None
                if code:
                    s["barcode"] = code
                    out += f" Прочитал штрихкод: {code}."
            return [out + " Ещё фото или «✅ Готово»."]
        if cmd == "/done":
            if not s["photos"] and not s["barcode"]:
                return ["Пришлите хотя бы одно фото упаковки (или цифры штрихкода)."]
            return self._process(uid, uname, s, notify)
        if cmd in ("/ok", "/fix", "/pick", "/none"):
            return ["Сначала пришлите фото и нажмите «✅ Готово»."]
        if text and re.fullmatch(r"[\d\s-]{8,}", text):
            digits = re.sub(r"\D", "", text)
            if self.gtin_ok(digits):
                s["barcode"] = digits
                return [f"Штрихкод записал: {digits}. Ещё фото или «✅ Готово»."]
            return ["Эти цифры не прошли проверку штрихкода. Проверьте и отправьте ещё раз."]
        if text:
            s["note"] = (s["note"] + " " + text).strip()[:300]
            return ["Записал подсказку для ИИ. Ещё фото или «✅ Готово»."]
        return ["Пришлите фото упаковки или нажмите «✅ Готово»."]

    # ------------------------------------------------------------ обработка
    def _process(self, uid, uname, s, notify):
        ok, why = self.check_limits(uid)
        if not ok:
            self.sessions.pop(uid, None)
            return [why]
        notify("🔍 Читаю упаковку, это займёт до минуты…")
        images = []
        for p in s["photos"]:
            with open(p, "rb") as f:
                images.append(f.read())
        try:
            if images:
                product, usage = ai_add.extract_product(self.env, images, note=s["note"] or None)
            else:
                product, usage = ai_add.normalize_product({}), None
        except ai_add.AiError as e:
            log.warning("ИИ: %s", e)
            self.log_usage(uid, "extract_failed", e.usage, s)          # деньги потрачены, но товар не засчитан в дневной лимит
            hint = f"\n\n[для администратора] начало ответа ИИ: {e.raw!r}" if uid in self.b.admins and e.raw is not None else ""
            return [str(e) + "\nФото сохранены, можно нажать «✅ Готово» ещё раз." + hint]
        self.log_usage(uid, "extract", usage, s)
        if usage is None:                                   # без фото считаем запрос, чтобы лимит работал
            self.log_usage(uid, "extract", {"in": 0, "out": 0, "cost": 0.0}, s)
        s["product"] = product
        self.save_draft(uid, uname, s, "draft")

        g = re.sub(r"\D", "", s["barcode"] or product.get("barcode") or "")
        if not (self.gtin_ok(g) and not ck.is_internal(g)):
            g = ""
        s["gtin"] = g
        if g:
            items = ck.fetch([g])
            active = [it for it in (items or []) if it.get("ntin_code") and not it.get("ntin_isdeactivated")]
            ch = self.b.chosen_use(g)                        # раньше этот штрихкод уже сопоставили с кодом
            if ch and (not active or ch["ntin"] in {re.sub(r"\D", "", str(it["ntin_code"])) for it in active}) and len(active) != 1:
                self.sessions.pop(uid, None)
                self.save_draft(uid, uname, s, "found_choice", ch["ntin"], ch["name"], g)
                return [self.b.chosen_text(ch, uid in self.b.admins)]
            if len(active) == 1:
                c = active[0]
                self.sessions.pop(uid, None)
                self.save_draft(uid, uname, s, "found_barcode", re.sub(r"\D", "", str(c["ntin_code"])), c.get("name_ru") or "", g)
                return [f"✅ По штрихкоду {g} в каталоге уже есть код: {re.sub(r'[^0-9]', '', str(c['ntin_code']))}\n{c.get('name_ru') or ''}\n"
                        "Новую карточку заводить не нужно."]
            if len(active) > 1:
                s["cands"] = [{"ntin": re.sub(r"\D", "", str(c["ntin_code"])), "name": c.get("name_ru") or "", "gtin": g, "score": 1.0}
                              for c in active[:5]]
                return self._show_candidates(s, f"По штрихкоду {g} в каталоге несколько карточек:")
        cands = ai_add.find_candidates(product, lambda q: ck.search_by_name(q, limit=20))
        if cands:
            s["cands"] = cands
            return self._show_candidates(s, "🔎 Нашёл в каталоге похожие карточки:")
        return self._make_draft(uid, uname, s, notify, intro="В каталоге по штрихкоду и названию ничего подходящего не нашёл.")

    def _show_candidates(self, s, head):
        s["mode"] = "pick"
        for c in s["cands"]:
            c["ours"] = self.b.in_base(c["ntin"])
        s["cands"].sort(key=lambda c: not c["ours"])                     # карточки из нашей базы первыми (порядок остальных сохраняется)
        lines = [head]
        for i, c in enumerate(s["cands"], 1):
            lines.append(f"{i}) {c['ntin']}  {c['name']}" + (f"  [штрихкод {c['gtin']}]" if c["gtin"] else "")
                         + ("  (из нашей базы)" if c["ours"] else ""))
        lines.append("Если это ваш товар (то же название, объём, оттенок), нажмите его номер. Если нет, «❌ Нет подходящего».")
        if any(c["ours"] for c in s["cands"]):
            lines.append("Если подходят несколько, лучше брать ту, что «из нашей базы»: она проверена.")
        return ["\n".join(lines)]

    def _pick(self, uid, uname, s, text, cmd, notify):
        if cmd == "/pick":
            m = re.search(r"\d+", text)
            n = int(m.group()) if m else 0
            if not (1 <= n <= len(s["cands"])):
                return ["Нажмите номер из списка или «❌ Нет подходящего»."]
            c = s["cands"][n - 1]
            self.sessions.pop(uid, None)
            self.save_draft(uid, uname, s, "matched", c["ntin"], c["name"], c["gtin"])
            g = s.get("gtin") or ""
            if g:
                self.b.remember(g, c["ntin"], c["name"], uid, uname)             # в следующий раз этот штрихкод сразу покажет этот код
            return [f"✅ Берите готовый код: {c['ntin']}\n{c['name']}\nНовую карточку заводить не нужно."
                    + ("\nЗапомнил: этот штрихкод теперь сразу покажет этот код." if g else "")]
        if cmd == "/none":
            return self._make_draft(uid, uname, s, notify, intro="Хорошо, готовлю черновик новой карточки.")
        return ["Нажмите номер из списка, «❌ Нет подходящего» или «✖️ Отмена»."]

    # ------------------------------------------------------------ черновик
    def _rebuild(self, uid, s):
        """Подбирает коды и собирает черновик по текущему описанию товара. Возвращает текст черновика."""
        product = s["product"]
        try:
            codes, notes, usages = ai_add.resolve_codes(self.env, self.b.db, product)
        except ai_add.AiError as e:
            codes, notes, usages = {}, [str(e)], []
        for u in usages:
            self.log_usage(uid, "codes", u, s)
        spec = ai_add.build_spec(product, codes, s.get("gtin", ""))
        body, errors, warns = card.build(dict(spec), self.b.db)
        s.update(spec=spec, codes=codes, notes=notes, errors=errors, warns=warns, body=body)
        name_ru, _ = card.compose_names(spec)
        lines = ["📝 Черновик новой карточки:", f"Название: {name_ru}",
                 f"Бренд: {spec['brand'] or 'СТМ (бренда нет)'}", f"Производитель: {spec['manufacturer']}",
                 f"Страна: {codes.get('country_name') or 'не определена'}" + (f" ({codes['country']})" if codes.get("country") else ""),
                 f"Категория: {codes.get('oktru_path') or codes.get('oktru_name') or 'не определена'}",
                 f"Код ТН ВЭД: {codes.get('tnved', 'не определён')}" + (f" ({codes['tnved_name']})" if codes.get("tnved_name") else ""),
                 f"Штрихкод: {s.get('gtin') or 'нет'}",
                 "Название по-казахски: " + ("с упаковки" if spec.get("name_kk") else "будет таким же, как русское")]
        if errors:
            lines.append("❗ Не хватает или неверно: " + "; ".join(human_error(e) for e in errors))
        own = [x for x in product.get("problems", []) if "казах" not in x.lower()]          # казахское название решаем мы, замечания ИИ о нём не нужны
        quiet_warns = [human_error(w) for w in warns if "казах" not in w.lower()]            # про казахское название уже сказано строкой выше
        extra = [n for n in notes + quiet_warns + own if n]
        if extra:
            lines.append("⚠️ Замечания: " + "; ".join(extra))
        return "\n".join(lines)

    def _make_draft(self, uid, uname, s, notify, intro=""):
        notify("📝 Готовлю черновик карточки…")
        text = self._rebuild(uid, s)
        s["mode"] = "draft"
        self.save_draft(uid, uname, s, "draft")
        return [(intro + "\n\n" if intro else "") + text + "\n\nВсё верно? Нажмите «✅ Верно» или «✏️ Исправить»."]

    def _draft(self, uid, uname, s, cmd, notify=None):
        if cmd == "/ok":
            status = "incomplete" if s.get("errors") else "ready"
            did = self.save_draft(uid, uname, s, status)
            self.sessions.pop(uid, None)
            name_ru, _ = card.compose_names(s["spec"])
            for a in self.b.admins:
                self.b.outbox.append((a, f"🆕 Черновик #{did} от {uname or uid} ({'неполный' if status == 'incomplete' else 'готов'}):\n{name_ru}\n"
                                         f"Описание для card.py:\n{json.dumps(s['spec'], ensure_ascii=False)}"))
            sub = getattr(self.b, "submitter", None)
            if status == "ready" and sub is not None and sub.enabled():
                if notify:
                    notify("📤 Отправляю карточку в каталог…")
                ok, info = sub.submit_draft(did)
                if ok:
                    return [f"✅ Принял, черновик №{did}. Карточка отправлена на модерацию в каталог ({info}). "
                            "Когда её одобрят и опубликуют, бот сам пришлёт вам код NTIN."]
                return [f"✅ Принял, черновик №{did}. Сразу отправить в каталог не получилось ({info}): администратор отправит позже и сообщит код."]
            if status == "ready":
                return [f"✅ Принял, черновик №{did}. Администратор проверит и отправит карточку в каталог "
                        "(отправка прямо из бота будет включена позже). Когда карточка пройдёт модерацию, администратор сообщит вам код."]
            return [f"Черновик №{did} сохранён, но в нём не хватает данных. Администратор дополнит и отправит."]
        if cmd == "/fix":
            if s["fixes"] >= MAX_FIXES:
                return ["Исправлений уже достаточно. Нажмите «✅ Верно» (администратор поправит) или «✖️ Отмена»."]
            s["mode"] = "fix"
            return ["Что исправить? Напишите одним сообщением, например:\nбренд Baseus, страна Китай, объём 100 мл"]
        return ["Нажмите «✅ Верно», «✏️ Исправить» или «✖️ Отмена»."]

    def _fix(self, uid, uname, s, text, cmd, notify):
        if cmd or not text:
            return ["Напишите, что исправить, одним сообщением, или нажмите «✖️ Отмена»."]
        notify("✏️ Вношу исправление…")
        try:
            product, usage = ai_add.apply_correction(self.env, s["product"], text)
        except ai_add.AiError as e:
            s["mode"] = "draft"
            return [str(e) + "\nМожно нажать «✏️ Исправить» ещё раз."]
        self.log_usage(uid, "fix", usage, s)
        s["product"], s["fixes"] = product, s["fixes"] + 1
        draft = self._rebuild(uid, s)
        s["mode"] = "draft"
        self.save_draft(uid, uname, s, "draft")
        return [draft + "\n\nВсё верно? Нажмите «✅ Верно» или «✏️ Исправить»."]
