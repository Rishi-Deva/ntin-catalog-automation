#!/usr/bin/env python3
"""
Автоотправка карточек в каталог: после кнопки «Верно» у сотрудника бот сам создаёт заявку, отправляет её на модерацию,
следит за статусом, при одобрении публикует и присылает код NTIN тому, кто подал товар.

Включается строкой AI_SUBMIT=1 в .env. Защиты:
  - отправляются только полные черновики (без ошибок card.build), каждый черновик не больше одного раза;
  - не больше AI_SUBMIT_DAILY заявок в сутки (по умолчанию 10);
  - администратор может мгновенно остановить новые отправки командой /submit off (и вернуть /submit on);
  - о каждой отправке и каждом изменении статуса бот пишет администраторам;
  - дубли и замечания модератора решает администратор: /use ЗАЯВКА КОД (взять готовую карточку) или /go ЗАЯВКА (продолжить свою).
Фото к заявке не прикладываются (при необходимости их можно добавить позже).
"""
import contextlib
import io
import json
import logging
import time
from datetime import datetime

import card
import oktru
import submit

log = logging.getLogger("ntinbot")
REQ = submit.REQ
FINAL = ("completed", "rejected", "cancelled")
DONE_WITH_CODE = ("completed", "existingProductSelected")


class Submitter:
    def __init__(self, brain, env, client=None):
        self.b, self.env, self._client = brain, env or {}, client
        self.daily = int(self.env.get("AI_SUBMIT_DAILY") or 10)
        self.interval = int(self.env.get("AI_SUBMIT_POLL") or 1800)
        self._last_poll = 0.0
        db = brain.db
        db.execute("""CREATE TABLE IF NOT EXISTS requests(
            key TEXT PRIMARY KEY, request_id INTEGER, status TEXT, name_ru TEXT, ntin TEXT, created_at TEXT)""")
        db.execute("""CREATE TABLE IF NOT EXISTS bot_requests(
            draft_id INTEGER PRIMARY KEY, request_id INTEGER, status TEXT, ntin TEXT, name TEXT, gtin TEXT,
            user_id INTEGER, user_name TEXT, created_at TEXT, notified TEXT)""")
        db.commit()

    # ------------------------------------------------------------ служебное
    def client(self):
        if self._client is None:
            from nkt_client import NktClient
            self._client = NktClient()
        return self._client

    def enabled(self):
        on = str(self.env.get("AI_SUBMIT", "0")).strip().lower() in ("1", "true", "yes", "on")
        return on and oktru.meta_get(self.b.db, "submit_paused", "0") != "1"

    def _tell(self, chat, text):
        self.b.outbox.append((chat, text))

    def _admins(self, text):
        for a in self.b.admins:
            self._tell(a, text)

    def _today_count(self):
        day = datetime.now().strftime("%Y-%m-%d")
        return self.b.db.execute("SELECT COUNT(*) FROM bot_requests WHERE created_at LIKE ?", (day + "%",)).fetchone()[0]

    # ------------------------------------------------------------ отправка
    def submit_draft(self, draft_id):
        """Создаёт заявку и отправляет на модерацию. Возвращает (получилось ли, пояснение для сотрудника)."""
        db = self.b.db
        if not self.enabled():
            return False, "автоотправка выключена"
        row = db.execute("SELECT status, spec, user_id, user_name FROM ai_drafts WHERE id=?", (draft_id,)).fetchone()
        if not row:
            return False, "такого черновика нет"
        status, spec_json, uid, uname = row
        if status in ("submitted", "published") or db.execute("SELECT 1 FROM bot_requests WHERE draft_id=?", (draft_id,)).fetchone():
            return False, "этот черновик уже отправлен"
        if status != "ready":
            return False, f"черновик в статусе «{status}»"
        if self._today_count() >= self.daily:
            self._admins(f"⚠️ Дневной лимит автоотправки ({self.daily}) исчерпан. Черновик №{draft_id} не отправлен: /sendnow {draft_id} завтра или поднимите AI_SUBMIT_DAILY.")
            return False, "дневной лимит автоотправки исчерпан"
        spec = json.loads(spec_json or "{}")
        body, errors, _warns = card.build(dict(spec), db)
        if errors or not body:
            return False, "в черновике не хватает данных: " + "; ".join(errors)
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                rid, created = submit.create_one(self.client(), db, body, True)
                if rid and created:
                    submit.moderate(self.client(), rid, True)
        except Exception as e:                                  # сбой сети или отказ каталога: черновик остаётся «готов», можно повторить
            log.exception("автоотправка черновика %s", draft_id)
            self._admins(f"⚠️ Не удалось отправить черновик №{draft_id} в каталог: {type(e).__name__}: {str(e)[:300]}\nПовторить: /sendnow {draft_id}")
            return False, "каталог сейчас не принял заявку"
        if not rid:
            self._admins(f"⚠️ Каталог не вернул номер заявки для черновика №{draft_id}.\n{out.getvalue()[:400]}")
            return False, "каталог не создал заявку"
        name = submit.name_of(body)
        db.execute("INSERT OR REPLACE INTO bot_requests VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (draft_id, rid, "underModeration" if created else "new", None, name, spec.get("gtin") or "", uid, uname,
                    datetime.now().isoformat(timespec="seconds"), None))
        db.execute("UPDATE ai_drafts SET status='submitted' WHERE id=?", (draft_id,))
        db.commit()
        if created:
            self._admins(f"📤 Отправлено на модерацию: заявка {rid} ({name[:80]}), подал {uname or uid}.")
            return True, f"заявка {rid}"
        self._admins(f"ℹ️ Для черновика №{draft_id} заявка уже была создана раньше: {rid}. Слежу за ней.")
        return True, f"заявка {rid} (уже создана раньше)"

    # ------------------------------------------------------------ слежение за статусами
    def poll(self, force=False):
        now = time.time()
        if not force and now - self._last_poll < self.interval:
            return 0
        self._last_poll = now
        rows = self.b.db.execute("SELECT draft_id, request_id FROM bot_requests WHERE status IS NULL OR status NOT IN "
                                 "('completed','rejected','cancelled','existingProductSelected')").fetchall()
        for did, rid in rows:
            try:
                self.check(did, rid)
            except Exception:
                log.exception("слежение за заявкой %s", rid)
        return len(rows)

    def check(self, draft_id, rid):
        db, client = self.b.db, self.client()
        s = client.get_json(f"{REQ}/{rid}/status")
        code = s.get("code")
        if code == "readyToPublish":
            client.put_json(f"{REQ}/{rid}/publish")
            time.sleep(1)
            s = client.get_json(f"{REQ}/{rid}/status")
            code = s.get("code")
        name, gtin, uid, uname, notified = db.execute(
            "SELECT name, gtin, user_id, user_name, notified FROM bot_requests WHERE draft_id=?", (draft_id,)).fetchone()
        ntin = s.get("ntinCode")
        db.execute("UPDATE bot_requests SET status=?, ntin=COALESCE(?, ntin) WHERE draft_id=?", (code, ntin, draft_id))
        db.execute("UPDATE requests SET status=?, ntin=COALESCE(?, ntin) WHERE request_id=?", (code, ntin, rid))
        db.commit()
        if code in DONE_WITH_CODE and ntin:
            self._finish(draft_id, rid, ntin, name, gtin, uid, uname)
        elif code == "underRevision" and notified != code:
            self._revision(draft_id, rid, s, name, uid)
        elif code in ("rejected", "cancelled") and notified != code:
            reason = s.get("reason") or s.get("comment") or ""
            self._admins(f"❌ Заявка {rid} ({name[:60]}): {code}. {reason}")
            self._tell(uid, f"❌ Заявку на «{name[:60]}» каталог не принял. Администратор разберётся и напишет вам.")
            db.execute("UPDATE bot_requests SET notified=? WHERE draft_id=?", (code, draft_id))
            db.commit()
        return code

    def _finish(self, draft_id, rid, ntin, name, gtin, uid, uname):
        db = self.b.db
        db.execute("UPDATE ai_drafts SET status='published', ntin=?, chosen_name=?, chosen_gtin=? WHERE id=?", (ntin, name, gtin, draft_id))
        db.execute("UPDATE bot_requests SET status='completed', notified='completed' WHERE draft_id=?", (draft_id,))
        db.commit()
        if gtin:
            self.b.remember(gtin, ntin, name, uid, uname)          # теперь проверка этого штрихкода сразу покажет код
        text = f"✅ Карточка опубликована. Код NTIN: {ntin}\n{name}\nЗаявка {rid}."
        self._tell(uid, text)
        for a in self.b.admins:
            if a != uid:
                self._tell(a, text + f"\n(подал {uname or uid})")

    def _revision(self, draft_id, rid, s, name, uid):
        db, client = self.b.db, self.client()
        lines = [f"⚠️ Заявка {rid} ({name[:70]}) на доработке."]
        for key in ("comment", "reason"):
            if s.get(key):
                lines.append(f"Комментарий: {s[key]}")
        for d in s.get("revisionDetails") or []:
            lines.append(f"Замечание по полю {d.get('attributeCode')}: {d.get('comment')} (сейчас: {d.get('value')})")
        try:
            dups = client.get_json(f"{REQ}/{rid}/duplicates")
            if isinstance(dups, dict):
                dups = dups.get("content") or dups.get("duplicates") or []
        except Exception:
            dups = []
        for x in (dups or [])[:5]:
            lines.append(f"Похожая карточка: {x.get('ntin')} | {str(x.get('name'))[:80]} | совпадение {x.get('similarity')}%")
        if dups:
            lines.append(f"Решение: /use {rid} КОД (взять готовую карточку) или /go {rid} (продолжить свою).")
        self._admins("\n".join(lines))
        self._tell(uid, f"⏳ По товару «{name[:60]}» каталог просит уточнения. Администратор займётся этим и сообщит код.")
        db.execute("UPDATE bot_requests SET notified='underRevision' WHERE draft_id=?", (draft_id,))
        db.commit()

    # ------------------------------------------------------------ команды администратора
    def admin(self, cmd, text):
        db = self.b.db
        arg = text.split()[1:] if text else []
        if cmd == "/requests":
            rows = db.execute("SELECT request_id, status, ntin, name, user_name FROM bot_requests ORDER BY draft_id DESC LIMIT 15").fetchall()
            if not rows:
                return ["Заявок, отправленных ботом, пока нет."]
            state = "включена" if self.enabled() else "выключена"
            return [f"Автоотправка: {state}. Сегодня отправлено {self._today_count()} из {self.daily}.\n" +
                    "\n".join(f"{r} | {st or '?'} | {n or '-'} | {(nm or '')[:50]} | {u or '?'}" for r, st, n, nm, u in rows)]
        if cmd == "/submit":
            what = (arg[0].lower() if arg else "")
            if what not in ("on", "off"):
                return ["Укажите: /submit off (остановить новые отправки) или /submit on"]
            oktru.meta_set(db, "submit_paused", "1" if what == "off" else "0")
            db.commit()
            if what == "on" and not str(self.env.get("AI_SUBMIT", "0")).strip().lower() in ("1", "true", "yes", "on"):
                return ["Пауза снята, но в .env нет AI_SUBMIT=1, поэтому автоотправка остаётся выключенной."]
            return ["Новые отправки остановлены. Слежение за уже отправленными продолжается." if what == "off" else "Автоотправка включена."]
        if cmd == "/link":
            if len(arg) < 2 or not (arg[0].isdigit() and arg[1].isdigit()):
                return ["Укажите: /link НОМЕР_ЧЕРНОВИКА НОМЕР_ЗАЯВКИ, например /link 12 49246169"]
            did, rid = int(arg[0]), int(arg[1])
            row = db.execute("SELECT spec, user_id, user_name FROM ai_drafts WHERE id=?", (did,)).fetchone()
            if not row:
                return [f"Черновика №{did} нет."]
            if db.execute("SELECT 1 FROM bot_requests WHERE draft_id=? OR request_id=?", (did, rid)).fetchone():
                return ["Этот черновик или эта заявка уже связаны."]
            spec = json.loads(row[0] or "{}")
            db.execute("INSERT INTO bot_requests VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (did, rid, "underModeration", None, card.compose_names(spec)[0], spec.get("gtin") or "", row[1], row[2],
                        datetime.now().isoformat(timespec="seconds"), None))
            db.execute("UPDATE ai_drafts SET status='submitted' WHERE id=?", (did,))
            db.commit()
            return [f"Связал черновик №{did} с заявкой {rid}. Бот следит за статусом и пришлёт код, когда карточку опубликуют."]
        if cmd == "/sendnow":
            if not arg or not arg[0].isdigit():
                return ["Укажите номер черновика: /sendnow 12"]
            ok, info = self.submit_draft(int(arg[0]))
            return [("📤 Отправлено: " if ok else "Не отправлено: ") + info]
        if cmd in ("/use", "/go"):
            if not arg or not arg[0].isdigit():
                return ["Укажите номер заявки: /use 49246169 0200000000000 или /go 49246169"]
            rid = int(arg[0])
            row = db.execute("SELECT draft_id FROM bot_requests WHERE request_id=?", (rid,)).fetchone()
            if not row:
                return [f"Заявки {rid} нет среди отправленных ботом."]
            if cmd == "/use":
                ntin = "".join(ch for ch in (arg[1] if len(arg) > 1 else "") if ch.isdigit())
                if not ntin:
                    return ["Укажите и код карточки: /use 49246169 0200000000000"]
                body = {"decision": "USE_EXISTING", "ntin": ntin}
            else:
                body = {"decision": "CONTINUE"}
            try:
                self.client().post_json(f"{REQ}/{rid}/duplicate-decision", body)
            except Exception as e:
                return [f"Каталог не принял решение: {type(e).__name__}: {str(e)[:200]}"]
            db.execute("UPDATE bot_requests SET notified=NULL WHERE request_id=?", (rid,))
            db.commit()
            self.poll(force=True)
            return [f"Решение отправлено ({'взять ' + body['ntin'] if cmd == '/use' else 'продолжить свою'}). Слежу за заявкой {rid}."]
        return ["Неизвестная команда."]
