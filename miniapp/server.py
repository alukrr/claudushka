"""HTTP-слой мини-приложения настроек (feat/miniapp, docs/claude/miniapp.md, ТЗ docs/claude/tz-miniapp.md).

Здесь ТОЛЬКО транспорт: проверка initData, лимит частоты, заголовки безопасности, маршруты. Правила настроек и права живут в
bot.py (реестр SETTINGS, can_change, apply_setting, is_chat_admin) и приходят сюда через объект `deps` (см. MiniappDeps) —
так модуль не импортирует bot.py (нет циклического импорта) и тестируется с подменой deps.

initData и токен бота НЕ логируются.
"""
import hashlib
import hmac
import json
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

INDEX_PATH = Path(__file__).resolve().parent / "index.html"
RATE_LIMIT_PER_MIN = 30
# Разрешённые источники: свои inline-скрипты/стили + официальный скрипт Telegram. Всё остальное закрыто.
CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline' https://telegram.org; style-src 'self' 'unsafe-inline'; "
       "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'")
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class InitDataError(Exception):
    """Подпись initData не прошла (нет hash, чужой токен, изменено поле, просрочено)."""


class MiniappError(Exception):
    """Ошибка прикладного уровня: HTTP-статус и текст для пользователя (его бросает deps)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class MiniappDeps(Protocol):
    async def user_allowed(self, user_id: int) -> bool: ...
    async def list_chats(self, user_id: int) -> list[dict]: ...
    async def chat_view(self, user_id: int, chat_id: int) -> dict: ...
    async def set_setting(self, user_id: int, chat_id: int, key: str, value: Any) -> dict: ...
    async def chat_costs(self, user_id: int, chat_id: int) -> dict: ...
    async def topup_info(self, user_id: int, chat_id: int) -> dict: ...
    async def topup_link(self, user_id: int, chat_id: int, stars: int) -> dict: ...


def validate_init_data(init_data: str, bot_token: str, *, max_age: int = 3600, now: float | None = None) -> dict:
    """Проверка подписи `Telegram.WebApp.initData` — строго по документации Telegram Mini Apps,
    раздел «Validating data received via the Mini App» (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app):

      data_check_string = все полученные поля КРОМЕ `hash`, отсортированные по ключу, в формате `key=<value>`, через '\\n';
      secret_key        = HMAC_SHA256(key="WebAppData", msg=<bot_token>)   # «HMAC-SHA-256 токена бота, где константа WebAppData — ключ»
      hash              = hex(HMAC_SHA256(key=secret_key, msg=data_check_string))

    Поле `signature` (Ed25519, для проверки третьей стороной без токена) в этой схеме — обычное поле: в data_check_string оно
    ВХОДИТ (исключается только `hash`); исключать `signature` документация велит лишь в схеме для третьей стороны.
    `auth_date` — Unix-время получения данных Mini App'ом: документация советует проверять свежесть (предел задаёт max_age).

    Возвращает разобранные поля (user — dict). Любое нарушение → InitDataError."""
    if not init_data or not bot_token:
        raise InitDataError("empty")
    pairs = parse_qsl(init_data, keep_blank_values=True)
    fields: dict[str, str] = {}
    for k, v in pairs:
        if k in fields:
            raise InitDataError("duplicate field")
        fields[k] = v
    received = fields.pop("hash", None)
    if not received:
        raise InitDataError("no hash")
    data_check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise InitDataError("bad hash")
    try:
        auth_date = int(fields["auth_date"])
    except (KeyError, ValueError):
        raise InitDataError("no auth_date")
    t = time.time() if now is None else now
    if t - auth_date > max_age or auth_date - t > 300:   # просрочено / «из будущего» (часы)
        raise InitDataError("stale")
    try:
        user = json.loads(fields.get("user", ""))
        user_id = int(user["id"])
    except (ValueError, KeyError, TypeError):
        raise InitDataError("no user")
    fields["user"] = user
    fields["user_id"] = user_id
    return fields


class _RateLimiter:
    """Не больше `limit` запросов в минуту на user.id (скользящее окно в памяти)."""

    def __init__(self, limit: int = RATE_LIMIT_PER_MIN, window: float = 60.0):
        self.limit, self.window = limit, window
        self._hits: dict[int, deque] = defaultdict(deque)

    def allow(self, user_id: int, now: float | None = None) -> bool:
        t = time.monotonic() if now is None else now
        q = self._hits[user_id]
        while q and t - q[0] >= self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(t)
        return True


def create_app(*, bot_token: str, deps: MiniappDeps, max_age: int = 3600, rate_limit: int = RATE_LIMIT_PER_MIN) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    limiter = _RateLimiter(rate_limit)

    @app.middleware("http")
    async def _headers(request: Request, call_next):
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers[k] = v
        return response

    def _fail(status: int, message: str) -> JSONResponse:
        return JSONResponse({"ok": False, "message": message}, status_code=status)

    async def _auth(request: Request) -> tuple[int | None, JSONResponse | None]:
        """(user_id, None) или (None, ответ-отказ). 401 — нет/неверная подпись; 403 — пользователь заблокирован; 429 — частота."""
        header = request.headers.get("authorization", "")
        scheme, _, init_data = header.partition(" ")
        if scheme.lower() != "tma" or not init_data:
            return None, _fail(401, "Откройте приложение из Telegram.")
        try:
            data = validate_init_data(init_data, bot_token, max_age=max_age)
        except InitDataError:
            return None, _fail(401, "Сессия недействительна — откройте приложение заново из Telegram.")
        uid = data["user_id"]
        if not limiter.allow(uid):
            return None, _fail(429, "Слишком часто. Подождите минуту.")
        if not await deps.user_allowed(uid):
            return None, _fail(403, "Доступ закрыт.")
        return uid, None

    async def _run(request: Request, call):
        uid, denied = await _auth(request)
        if denied:
            return denied
        try:
            return JSONResponse(await call(uid))
        except MiniappError as e:
            return _fail(e.status, e.message)

    @app.api_route("/", methods=["GET", "HEAD"])
    async def index():
        return Response(INDEX_PATH.read_bytes(), media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})

    @app.get("/api/chats")
    async def api_chats(request: Request):
        async def call(uid):
            return {"chats": await deps.list_chats(uid)}
        return await _run(request, call)

    @app.get("/api/chats/{chat_id}/settings")
    async def api_settings(chat_id: int, request: Request):
        return await _run(request, lambda uid: deps.chat_view(uid, chat_id))

    @app.get("/api/chats/{chat_id}/costs")
    async def api_costs(chat_id: int, request: Request):
        return await _run(request, lambda uid: deps.chat_costs(uid, chat_id))

    @app.get("/api/chats/{chat_id}/topup")
    async def api_topup_info(chat_id: int, request: Request):
        return await _run(request, lambda uid: deps.topup_info(uid, chat_id))

    @app.post("/api/chats/{chat_id}/topup")
    async def api_topup_link(chat_id: int, request: Request):
        """Ссылка на счёт в Stars. Баланс НЕ зачисляется здесь: только бот по `successful_payment` (docs/claude/billing.md)."""
        async def call(uid):
            try:
                body = await request.json()
            except Exception:
                raise MiniappError(400, "Ожидается JSON {\"stars\": N}.")
            stars = body.get("stars") if isinstance(body, dict) else None
            if not isinstance(stars, int) or isinstance(stars, bool):
                raise MiniappError(400, "Ожидается JSON {\"stars\": N}.")
            return await deps.topup_link(uid, chat_id, stars)
        return await _run(request, call)

    @app.post("/api/chats/{chat_id}/settings/{key}")
    async def api_set(chat_id: int, key: str, request: Request):
        async def call(uid):
            try:
                body = await request.json()
            except Exception:
                raise MiniappError(400, "Ожидается JSON {\"value\": …}.")
            if not isinstance(body, dict) or "value" not in body:
                raise MiniappError(400, "Ожидается JSON {\"value\": …}.")
            return await deps.set_setting(uid, chat_id, key, body["value"])
        return await _run(request, call)

    return app
