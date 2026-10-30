"""
Клиент портального API Национального каталога товаров (НКТ).

База:      https://nationalcatalog.kz/gwp
Авторизация: заголовок X-API-KEY. Ключ берётся из переменной окружения NKT_API_KEY
             или из файла .env рядом с этим скриптом (строка NKT_API_KEY=...).

Что делает клиент:
  - держит паузу между запросами (чтобы не упереться в лимиты ключа);
  - повторяет запрос при 429 и 5xx (с нарастающей паузой, учитывает Retry-After);
  - при 401/403 и ошибках проверки данных (400/422) бросает понятные исключения;
  - никогда не печатает ключ.
"""
import mimetypes
import os
import re
import time

import requests

BASE = "https://nationalcatalog.kz/gwp"
HERE = os.path.dirname(os.path.abspath(__file__))


class NktError(Exception):
    """Любая ошибка обращения к НКТ."""


class AuthError(NktError):
    """401/403: ключ не принят или нет прав."""


class ValidationError(NktError):
    """400/422: данные не прошли проверку. В .body текст ответа сервера."""

    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:500]}")
        self.status, self.body = status, body


def _read_dotenv(path):
    values = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
            if m:
                values[m.group(1)] = m.group(2).strip("'\"")
    return values


def load_key():
    key = os.environ.get("NKT_API_KEY") or _read_dotenv(os.path.join(HERE, ".env")).get("NKT_API_KEY", "")
    key = re.sub(r"\s+", "", key)
    if not key:
        raise NktError("Ключ не найден. Положите NKT_API_KEY=... в файл .env рядом со скриптом.")
    half = len(key) // 2
    if len(key) % 2 == 0 and key[:half] == key[half:]:
        raise NktError("Ключ в .env вставлен дважды подряд. Оставьте одну копию.")
    return key


class NktClient:
    def __init__(self, api_key=None, base=BASE, min_interval=None, retries=None, timeout=30):
        self._key = api_key or load_key()
        self.base = base.rstrip("/")
        # Если каталог отвечает «слишком часто» (HTTP 429), запуск с NKT_MIN_INTERVAL=1 NKT_RETRIES=8 идёт медленнее и терпеливее.
        self.min_interval = float(os.environ.get("NKT_MIN_INTERVAL", 0.25)) if min_interval is None else min_interval
        self.retries = int(os.environ.get("NKT_RETRIES", 5)) if retries is None else retries
        self.timeout = timeout
        self._last = 0.0
        self.session = requests.Session()
        self.session.headers.update({"X-API-KEY": self._key, "Accept": "application/json"})

    def __repr__(self):
        return f"NktClient(base={self.base!r})"

    def _pause(self):
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def request(self, method, path, **kw):
        url = self.base + path
        kw.setdefault("timeout", self.timeout)
        last = None
        for attempt in range(self.retries):
            self._pause()
            try:
                r = self.session.request(method, url, **kw)
            except requests.RequestException as e:
                last = e
                time.sleep(min(2 ** attempt, 30))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                try:
                    delay = float(r.headers.get("Retry-After", ""))
                except ValueError:
                    delay = min(2 ** attempt, 30)
                last = NktError(f"HTTP {r.status_code}")
                time.sleep(delay)
                continue
            if r.status_code in (401, 403):
                raise AuthError(f"HTTP {r.status_code}: ключ не принят или нет прав на {path}")
            if r.status_code in (400, 404, 409, 422):
                raise ValidationError(r.status_code, r.text)
            r.raise_for_status()
            return r
        raise NktError(f"Не удалось выполнить {method} {path} после {self.retries} попыток: {last}")

    def get_json(self, path, **params):
        return self.request("GET", path, params=params or None).json()

    # ---- справочники
    def dictionaries(self):
        return self.get_json("/portal/api/v1/dictionaries")

    def dictionary_items(self, code, page=1, size=100):
        """Постранично (page с 1, size до 100). Ответ: content, totalElements, totalPages."""
        return self.get_json(f"/portal/api/v1/dictionaries/{code}/items", page=page, size=size)

    # ---- заявки
    def base_attributes(self):
        return self.get_json("/portal/api/v1/products/requests/attributes")

    def my_requests(self, page=1, size=100, status=None):
        params = {"page": page, "size": size}
        if status:
            params["status"] = status
        return self.get_json("/portal/api/v1/products/requests", **params)

    # ---- запись (используется модулем submit.py)
    @staticmethod
    def body(r):
        """JSON-тело ответа или {} если тела нет."""
        try:
            return r.json()
        except ValueError:
            return {}

    def post_json(self, path, body=None, **params):
        return self.request("POST", path, json=body, params=params or None)

    def put_json(self, path, body=None, **params):
        return self.request("PUT", path, json=body, params=params or None)

    def delete(self, path, **params):
        return self.request("DELETE", path, params=params or None)

    def upload(self, path, filepath, **params):
        """multipart-загрузка файла в поле «file»."""
        mime = mimetypes.guess_type(filepath)[0] or "application/octet-stream"
        with open(filepath, "rb") as fh:
            return self.request("POST", path, files={"file": (os.path.basename(filepath), fh, mime)},
                                params=params or None)
