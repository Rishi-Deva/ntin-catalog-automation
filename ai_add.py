#!/usr/bin/env python3
"""
ИИ-помощник для добавления товара: читает фото упаковки, ищет готовую карточку в каталоге,
подбирает категорию и код ТН ВЭД из справочников и готовит черновик новой карточки.

Работает через обычный HTTP-запрос (пакет anthropic не нужен). Ключ берётся из .env:
  ANTHROPIC_API_KEY   ключ из консоли разработчика
  AI_MODEL            модель, по умолчанию claude-haiku-5-5

Все функции, которые обращаются к ИИ, получают словарь настроек env и возвращают (результат, расход).
В тестах функцию call_ai подменяют заглушкой.
"""
import base64
import json
import logging
import re
import time

import requests

log = logging.getLogger("ntinbot")
API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-5-5"
PRICE_IN, PRICE_OUT = 0.10 / 1e6, 0.50 / 1e6      # Haiku 5.5, запросы до 100 тысяч токенов, доллары за токен
MAX_SIDE = 1568                                    # рекомендуемый предел стороны картинки для ИИ
PRODUCT_KEYS = ("kind", "brand", "model_or_article", "features", "pack", "name_ru_on_pack", "name_kk_on_pack",
                "country_name", "manufacturer", "barcode", "oktru_query", "tnved_query")
LIST_KEYS = ("search_queries", "oktru_queries", "tnved_queries")


class AiError(Exception):
    """Ошибка ИИ с понятным людям текстом. raw: начало ответа ИИ (для диагностики), usage: расход до ошибки."""

    def __init__(self, msg, raw=None, usage=None):
        super().__init__(msg)
        self.raw, self.usage = raw, usage


def cost_usd(tokens_in, tokens_out):
    return tokens_in * PRICE_IN + tokens_out * PRICE_OUT


def prepare_image(data):
    """Уменьшает картинку до разумного размера и приводит к JPEG (меньше токенов, быстрее)."""
    try:
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return data
        m = max(img.shape[:2])
        if m > MAX_SIDE:
            img = cv2.resize(img, None, fx=MAX_SIDE / m, fy=MAX_SIDE / m, interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return buf.tobytes() if ok else data
    except ImportError:
        return data


def parse_json(text):
    """Достаёт первый JSON-объект из ответа ИИ (даже если он обёрнут в ```json ... ``` или после него есть пояснения)."""
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.S)
    dec = json.JSONDecoder()
    pos = s.find("{")
    while pos >= 0:
        try:
            obj, _ = dec.raw_decode(s, pos)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
        pos = s.find("{", pos + 1)
    raise AiError("ИИ ответил непонятно, попробуйте ещё раз.", raw=(text or "")[:300])


def call_ai(env, system, content, max_tokens=1200):
    """Один запрос к ИИ. content: список блоков (картинки и текст). Возвращает (текст ответа, расход)."""
    key = (env or {}).get("ANTHROPIC_API_KEY", "")
    if not key:
        raise AiError("ИИ не подключён (нет ключа в настройках).")
    body = {"model": (env or {}).get("AI_MODEL") or DEFAULT_MODEL, "max_tokens": max_tokens, "system": system,
            "messages": [{"role": "user", "content": content}]}
    try:
        r = requests.post(API_URL, headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                            "content-type": "application/json"}, json=body, timeout=90)
    except requests.RequestException as e:
        raise AiError("Нет связи с ИИ, попробуйте через минуту.") from None
    if r.status_code != 200:
        try:
            msg = (r.json().get("error") or {}).get("message", "")
        except ValueError:
            msg = ""
        if r.status_code == 401:
            raise AiError("Ключ ИИ не принят. Сообщите администратору.")
        if r.status_code == 429:
            raise AiError("Слишком много запросов к ИИ, повторите через минуту.")
        if r.status_code == 400 and "credit" in msg.lower():
            raise AiError("На балансе ИИ закончились деньги. Сообщите администратору.")
        if r.status_code >= 500 or r.status_code == 529:
            raise AiError("ИИ временно недоступен, попробуйте позже.")
        raise AiError(f"ИИ не принял запрос (код {r.status_code}). Сообщите администратору.")
    d = r.json()
    if d.get("stop_reason") == "refusal":
        raise AiError("ИИ отказался обрабатывать это фото. Попробуйте другое.")
    text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
    u = d.get("usage") or {}
    ti, to = int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0))
    return text, {"in": ti, "out": to, "cost": cost_usd(ti, to), "stop": d.get("stop_reason")}


# ------------------------------------------------------------------ чтение упаковки
EXTRACT_SYSTEM = """Ты помощник магазина в Казахстане. Тебе показывают фото упаковки товара (спереди, сторона с производителем, штрихкод).
Нужно прочитать упаковку и вернуть ТОЛЬКО один JSON-объект без пояснений. Ничего не выдумывай: чего не видно, ставь null.
Поля:
  kind: вид товара по-русски одним-тремя словами («Кабель USB Type-C», «Помада губная», «Чехол для телефона», «Серьга»)
  brand: бренд или null (если бренда нет, null)
  model_or_article: модель, артикул или оттенок или null
  features: ключевые характеристики одной строкой (материал, размер, цвет, длина) или null
  pack: объём, вес или количество в упаковке («100 мл», «12 шт.», «3,6 г») или null
  name_ru_on_pack: полное название товара по-русски так, как написано на упаковке, или null
  name_kk_on_pack: название по-казахски, только если оно напечатано на упаковке, иначе null
  country_name: страна производства по-русски («Китай», «Россия», «Франция») или null. Если страна прямо не названа, но указан адрес ИЗГОТОВИТЕЛЯ
    (не импортёра и не организации, принимающей претензии), возьми страну из этого адреса и добавь в problems «страна взята из адреса изготовителя».
    Страну происхождения сырья (например, масло из Кот-д'Ивуара) страной производства не считай
  manufacturer: изготовитель или производитель, как написано (без адреса), или null
  barcode: цифры штрихкода, если они хорошо видны, иначе null
  search_queries: список из 2-3 коротких запросов (по 2-5 слов) для поиска такого товара в каталоге по названию
  oktru_queries: список из 3 вариантов запроса для поиска категории в классификаторе товаров, по 1-3 слова, РАЗНЫМИ словами:
    название группы товаров так, как его пишут в классификаторе (например «кабели интерфейсные», «кабели для передачи данных», «аксессуары для телефонов»); модели, бренды и стандарты (USB, Type-C) не включай
  tnved_queries: список из 3 вариантов запроса для поиска кода ТН ВЭД (товарная номенклатура внешней экономической деятельности), по 2-4 слова,
    словами из названий товарных позиций ТН ВЭД, разными формулировками (например «косметические средства для ухода за кожей», «провода и кабели с соединителями», «заколки для волос»)
  confidence: число от 0 до 1, насколько вы уверены, что правильно распознали товар
  problems: список коротких замечаний, чего не видно или что плохо читается (может быть пустым). Не пиши про казахское название, перевод,
    советы по оформлению карточки и про то, как делают другие продавцы: пиши только о том, что видно на фото"""


def _s(v):
    """Строка или None (пустое, «null», «нет» считаем отсутствием)."""
    if v is None:
        return None
    t = re.sub(r"\s+", " ", str(v)).strip()
    return None if t.lower() in ("", "null", "none", "нет", "неизвестно", "n/a") else t


def normalize_product(d):
    p = {k: _s(d.get(k)) for k in PRODUCT_KEYS}
    for key in LIST_KEYS:
        q = d.get(key) or []
        if isinstance(q, str):
            q = [q]
        p[key] = [x for x in (_s(i) for i in q) if x][:3]
    for single, many in (("oktru_query", "oktru_queries"), ("tnved_query", "tnved_queries")):
        if not p[single] and p[many]:
            p[single] = p[many][0]
    try:
        p["confidence"] = max(0.0, min(1.0, float(d.get("confidence"))))
    except (TypeError, ValueError):
        p["confidence"] = 0.0
    pr = d.get("problems") or []
    p["problems"] = [x for x in (_s(i) for i in (pr if isinstance(pr, list) else [pr])) if x][:5]
    return p


def extract_product(env, images, note=None):
    """images: список байтов фото. Возвращает (описание товара, расход). При неудачном разборе один раз повторяет запрос."""
    pics = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                          "data": base64.b64encode(prepare_image(im)).decode()}} for im in images]
    ask = "Прочитай упаковку и верни JSON."
    if note:
        ask += f"\nПодсказка сотрудника: {note}"
    total, last = {"in": 0, "out": 0, "cost": 0.0}, None
    for attempt in (1, 2):
        text_block = ask if attempt == 1 else ask + "\nВАЖНО: ответь одним валидным JSON-объектом, без пояснений и без markdown."
        text, usage = call_ai(env, EXTRACT_SYSTEM, pics + [{"type": "text", "text": text_block}], max_tokens=1500)
        for k in total:
            total[k] += usage.get(k, 0)
        if usage.get("stop") == "max_tokens":
            last = AiError("Ответ ИИ оборвался, попробуйте ещё раз.", raw=text[:300])
            continue
        try:
            return normalize_product(parse_json(text)), total
        except AiError as e:
            last = e
    log.warning("ИИ не вернул разборчивый JSON (stop=%s): %r", usage.get("stop"), (last.raw or "")[:300])
    last.usage = total
    raise last


# ------------------------------------------------------------------ сопоставление с карточками каталога
def norm_words(s):
    return re.findall(r"[\w][\w-]*", str(s or "").lower().replace("ё", "е"))


def key_tokens(product):
    """Слова, по которым узнаём товар: вид, бренд, модель и объём."""
    toks = []
    for k in ("kind", "brand", "model_or_article", "pack"):
        toks += [w for w in norm_words(product.get(k)) if len(w) >= 3 or w.isdigit()]
    return list(dict.fromkeys(toks))


def score_card(name, product):
    """Доля ключевых слов товара, найденных в названии карточки (от 0 до 1). Нет бренда в названии: штраф."""
    key = key_tokens(product)
    if not key:
        return 0.0
    low = " ".join(norm_words(name))
    hit = sum(1 for w in key if w in low)
    score = hit / len(key)
    brand = norm_words(product.get("brand"))
    if brand and not all(b in low for b in brand):
        score *= 0.5
    return score


def find_candidates(product, search, min_score=0.6, limit=5, pause=0.6, sleep=None):
    """Ищет в каталоге по названию: search(запрос) -> список карточек или None. Возвращает лучшие совпадения."""
    queries = list(product.get("search_queries") or [])
    if not product.get("brand") and product.get("kind"):
        queries.append(f"{product['kind']} СТМ")            # общие карточки без бренда заводят как «СТМ»
    seen, found = set(), []
    for i, q in enumerate(dict.fromkeys(queries)):
        if i:
            (sleep or time.sleep)(pause)                  # публичный каталог просит не чаще 2-3 запросов в секунду
        cards = search(q)
        for c in cards or []:
            n = re.sub(r"\D", "", str(c.get("ntin_code") or ""))
            if not n or c.get("ntin_isdeactivated") or n in seen:
                continue
            seen.add(n)
            sc = score_card(c.get("name_ru") or "", product)
            if sc >= min_score:
                found.append({"ntin": n, "name": c.get("name_ru") or "", "gtin": re.sub(r"\D", "", str(c.get("gtin") or "")),
                              "score": round(sc, 2)})
    found.sort(key=lambda x: (-x["score"], len(x["name"])))
    return found[:limit]


# ------------------------------------------------------------------ выбор кодов из справочников
def describe(product):
    parts = [f"{k}: {product[k]}" for k in ("kind", "brand", "model_or_article", "features", "pack", "name_ru_on_pack") if product.get(k)]
    return "; ".join(parts) or "(описание пустое)"


_BAD = object()


def _pick_number(text):
    """Номер выбранного варианта из ответа ИИ: число, None (ничего не подходит) или _BAD (ответ не разобрать)."""
    try:
        d = parse_json(text)
        if "number" not in d:
            return _BAD
        v = d["number"]
    except AiError:
        m = re.search(r'"number"\s*:\s*(null|\d+)', text or "") or re.fullmatch(r"\s*(null|\d+)\s*", text or "")
        if not m:
            return _BAD
        v = m.group(1)
    if v is None or v == "null":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return _BAD


def choose_code(env, product, options, what):
    """Выбор одного варианта из списка [(код, название)]. Возвращает (код или None, расход или None).
    При неразборчивом ответе один раз повторяет запрос; если и тогда не вышло, поднимает AiError с расходом в e.usage."""
    if not options:
        return None, None
    if len(options) == 1:
        return options[0][0], None
    lines = "\n".join(f"{i}. {name}" for i, (_, name) in enumerate(options, 1))
    ask = (f"Товар: {describe(product)}\nВыбери {what}, которая по смыслу подходит этому товару.\n{lines}\n"
           "Выбирай только если вариант действительно подходит именно этому виду товара. Если подходят только "
           "категории из другой области (например, строительные для бытовой электроники) или ни один вариант не подходит, ответь null.\n"
           'Ответь ТОЛЬКО JSON без пояснений: {"number": номер варианта или null}')
    total, text = {"in": 0, "out": 0, "cost": 0.0}, ""
    for _ in range(2):
        text, usage = call_ai(env, "Ты помощник по классификаторам товаров Казахстана. Отвечай только JSON, без пояснений.",
                              [{"type": "text", "text": ask}], max_tokens=600)
        for k in total:
            total[k] += usage.get(k, 0)
        n = _pick_number(text)
        if n is not _BAD:
            return (options[n - 1][0] if n and 1 <= n <= len(options) else None), total
    log.warning("ИИ не выбрал %s (ответ не разобрать): %r", what, text[:200])
    err = AiError("ИИ ответил непонятно при выборе: " + what, raw=text[:300])
    err.usage = total
    raise err


def _queries(product, many, single):
    qs = ([product.get(single)] if product.get(single) else []) + list(product.get(many) or [])
    if not qs and product.get("kind"):
        qs = [product["kind"]]
    return list(dict.fromkeys(q for q in qs if q))


def resolve_codes(env, db, product):
    """Подбирает страну, категорию ОКТРУ и код ТН ВЭД по справочникам в базе. Возвращает (коды, заметки, расходы)."""
    import dicts
    import oktru
    codes, notes, usages = {}, [], []
    cname = product.get("country_name")
    if not cname:
        notes.append("страна на упаковке не указана")
    elif not dicts.is_loaded(db, "COUNTRY"):
        notes.append("справочник стран на сервере не загружен")
    else:
        rows = dicts.search(db, "COUNTRY", cname, 3)
        if rows:
            codes["country"], codes["country_name"] = rows[0][0], rows[0][1]
        else:
            notes.append(f"страна «{cname}» не найдена в справочнике")

    # категория ОКТРУ: несколько формулировок, в список попадают лучшие совпадения по полноте
    found = {}
    for q in _queries(product, "oktru_queries", "oktru_query"):
        for c, p, matched, total in oktru.search(db, q, limit=6):
            ratio = matched / total if total else 0
            if c not in found or ratio > found[c][1]:
                found[c] = (p, ratio)
    ranked = sorted(found.items(), key=lambda kv: (-kv[1][1], len(kv[1][0])))[:10]
    if not ranked:
        notes.append("категория ОКТРУ не найдена (справочник не загружен или слова не подошли)")
    else:
        opts = [(c, p) for c, (p, _) in ranked]
        try:
            code, u = choose_code(env, product, opts, "категорию")
        except AiError as e:
            code, u = None, e.usage
            notes.append("ИИ не смог выбрать категорию, нажмите «✏️ Исправить» и подскажите вид товара")
        usages.append(u)
        if code:
            codes["oktru"] = code
            codes["oktru_name"] = dict(opts)[code].rsplit(" > ", 1)[-1]
            codes["oktru_path"] = dict(opts)[code]
        else:
            if not any("категорию" in n for n in notes):
                notes.append("не удалось выбрать категорию ОКТРУ (подходящих по смыслу нет)")

    # код ТН ВЭД
    if dicts.is_loaded(db, "TNVED"):
        found, leaves = {}, dicts.leaves_with_paths(db, "TNVED")
        for q in _queries(product, "tnved_queries", "tnved_query"):
            for c, n in dicts.search_paths(db, "TNVED", q, 8, leaves=leaves):       # ищем по всему пути: группа > позиция > код
                found.setdefault(c, n)
        rows = list(found.items())[:12]
        try:
            code, u = choose_code(env, product, [(c, f"{c} {n}") for c, n in rows], "код ТН ВЭД")
        except AiError as e:
            code, u = None, e.usage
            notes.append("ИИ не смог выбрать код ТН ВЭД, администратор подберёт вручную")
        usages.append(u)
        if code:
            codes["tnved"] = code
            codes["tnved_name"] = dict(rows)[code]
        elif not any("ТН ВЭД" in n and "ИИ не смог" in n for n in notes):
            notes.append("не удалось выбрать код ТН ВЭД")
    else:
        notes.append("справочник ТН ВЭД на сервере не загружен")
    return codes, notes, [u for u in usages if u]


def build_spec(product, codes, gtin=""):
    """Описание товара в формате card.py (его потом проверяет card.build)."""
    spec = {"kind": product.get("kind") or "", "brand": product.get("brand") or "",
            "article": product.get("model_or_article") or "", "features": product.get("features") or "",
            "pack": product.get("pack") or "", "manufacturer": product.get("manufacturer") or "не указан",
            "unit": "796", "quantity": "1"}
    for k in ("country", "oktru", "tnved"):
        if codes.get(k):
            spec[k] = codes[k]
    if product.get("name_kk_on_pack"):
        spec["name_kk"] = product["name_kk_on_pack"]          # казахское название только с упаковки, без перевода
    if gtin:
        spec["gtin"] = gtin
    return spec


# ------------------------------------------------------------------ исправления
CORRECTABLE = ("kind", "brand", "model_or_article", "features", "pack", "country_name", "manufacturer",
               "oktru_query", "tnved_query")


def apply_correction(env, product, text):
    """Применяет правку сотрудника («бренд Baseus, страна Китай»). Возвращает (новое описание, расход)."""
    cur = {k: product.get(k) for k in CORRECTABLE}
    ask = (f"Текущее описание товара (JSON): {json.dumps(cur, ensure_ascii=False)}\n"
           f"Сотрудник просит исправить: {text}\n"
           "Верни JSON только с теми полями, которые нужно изменить (имена полей те же). "
           "Если исправляется вид товара, обнови и oktru_query, и tnved_query. Ничего не выдумывай.")
    out, usage = call_ai(env, "Ты помощник магазина. Отвечай только JSON.", [{"type": "text", "text": ask}], max_tokens=300)
    upd = parse_json(out)
    new = dict(product)
    for k in CORRECTABLE:
        if k in upd:
            new[k] = _s(upd[k])
    if "oktru_query" in upd:
        new["oktru_queries"] = []             # после исправления старые формулировки уже не подходят
    if "tnved_query" in upd:
        new["tnved_queries"] = []
    return new, usage
