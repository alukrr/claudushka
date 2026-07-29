"""Обработка ошибок Anthropic API: классификация, ретраи, сообщения пользователю.

Общий модуль для bot.py (Telegram) и whatsapp.py — чтобы поведение не разъехалось
между сервисами.

Инвариант: пользователь НИКОГДА не видит сырой текст исключения. Транзиентные ошибки
(529 overloaded / 5xx, 429, обрыв связи) переживаем ретраями молча, окончательные —
объясняем по-человечески. Полный traceback уходит в logger.error.

Проверено на anthropic 0.43.0: SDK маппит HTTP 529 в anthropic.InternalServerError
(подкласс APIStatusError, status_code >= 500). Свой класс под 529 в SDK не заведён,
поэтому ловим весь 5xx одним типом.
"""

import asyncio
import logging

import anthropic

logger = logging.getLogger(__name__)

# Паузы между повторами транзиентной ошибки (сек). Длина кортежа = число ретраев.
RETRY_DELAYS = (2, 6, 15)

PROMPT_TOO_LONG_MARKER = "prompt is too long"

# Нейтральная подсказка: /clear чистит историю диалога, /forget — память.
# Вес промпта бывает и там и там (инцидент 2026-07-26 — память), поэтому по умолчанию
# называем оба. Ветки восстановления подставляют свою, более точную.
CLEAR_HINT_DEFAULT = "Напиши /clear, а если не поможет — /forget."


def is_prompt_too_long(exc: BaseException) -> bool:
    """400 с 'prompt is too long' — особый случай: лечится обрезкой промпта, не ретраем.

    Смотрим и на текст исключения, и на exc.body: SDK обычно кладёт тело ответа в
    сообщение (`Error code: 400 - {...}`), но в ветке «ответ закрыт до чтения» строит
    голое `Error code: 400` с body=None. Проверять только str(exc) — терять восстановление.
    """
    if not isinstance(exc, anthropic.BadRequestError):
        return False
    if PROMPT_TOO_LONG_MARKER in str(exc).lower():
        return True
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        message = body.get("error", {}).get("message", "") if isinstance(body.get("error"), dict) else ""
        return PROMPT_TOO_LONG_MARKER in str(message).lower()
    return False


def is_retryable(exc: BaseException) -> bool:
    """Переживаемые сами по себе: перегруз сервиса, рейт-лимит, сетевой обрыв/таймаут."""
    return isinstance(
        exc,
        (
            anthropic.InternalServerError,  # 5xx, включая 529 overloaded_error
            anthropic.RateLimitError,       # 429
            anthropic.APIConnectionError,   # сеть + APITimeoutError (наследник)
        ),
    )


def user_message(exc: BaseException, default: str | None = None, clear_hint: str = CLEAR_HINT_DEFAULT) -> str:
    """Что показать пользователю. В характере Клодушки, коротко, без JSON и traceback.

    clear_hint — как пользователю сбросить историю. В Telegram это /clear,
    в WhatsApp команд нет, поэтому текст задаёт вызывающая сторона.
    """
    if isinstance(exc, anthropic.InternalServerError):
        return "Сервис перегружен, я не виновата. Попробуй через минуту."
    if isinstance(exc, anthropic.RateLimitError):
        return "Слишком часто. Притормози на минутку — я не резиновая."
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return "Проблема с доступом к сервису. Это чинит Алексей, не ты."
    if is_prompt_too_long(exc):
        return f"Контекст переполнен — я больше не влезаю в собственную память. {clear_hint}"
    if isinstance(exc, anthropic.BadRequestError):
        return "Запрос не прошёл: он API не понравился. Попробуй переформулировать."
    if isinstance(exc, anthropic.NotFoundError):
        # Важно не путать с 529: «модели нет» и «сервис перегружен» требуют разных действий.
        return "Такой модели нет — пул её не знает."
    if isinstance(exc, anthropic.APIConnectionError):
        return "Не достучалась до сервиса. Попробуй ещё раз."
    if isinstance(exc, anthropic.APIStatusError):
        return "Сервис ответил ошибкой. Попробуй позже."
    return default or "Что-то сломалось на моей стороне. Попробуй ещё раз."


def history_stats(messages: list) -> tuple[int, int]:
    """(символы ТЕКСТА, число картинок).

    base64 изображений в символы НЕ включаем: одна картинка — сотни килобайт,
    метрика распухает и перестаёт соотноситься с токенами. Именно эта цифра нужна
    при разборе инцидентов, поэтому она должна быть честной.
    """
    chars = 0
    imgs = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            chars += len(content)
            continue
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    chars += len(str(block))
                elif block.get("type") == "text":
                    chars += len(block.get("text", ""))
                elif block.get("type") == "image":
                    imgs += 1
                else:
                    chars += len(str(block.get("content", "")))
        else:
            chars += len(str(content))
    return chars, imgs


def halve_history(messages: list) -> list | None:
    """Свежая половина истории (лечение 400 prompt is too long).

    Возвращает None, если обрезать нечего — вызывающая сторона НЕ должна повторять
    запрос: он соберётся идентичным и гарантированно получит тот же 400.

    Первым обязан идти user, иначе API вернёт уже другой 400 про чередование ролей.
    Если в свежей половине user-реплики не осталось (хвост из одних assistant) —
    откатываемся к ПОСЛЕДНЕМУ user-сообщению, а не к последнему вообще.
    """
    trimmed = messages[len(messages) // 2:]
    while trimmed and trimmed[0].get("role") != "user":
        trimmed = trimmed[1:]
    if not trimmed:
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                trimmed = messages[i:]
                break
    if not trimmed or len(trimmed) >= len(messages):
        return None
    return trimmed


def log_api_error(exc: BaseException, *, context_label: str) -> None:
    """Полный traceback в лог. Для 401/403 — явное указание на ключ, чтобы не искать."""
    status = getattr(exc, "status_code", None)
    where = f"[{context_label}]" + (f" HTTP {status}" if status else "")
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        logger.error(
            f"{where} ПРОВЕРЬ ANTHROPIC_API_KEY (ключ невалиден, отозван или нет прав): {exc}",
            exc_info=True,
        )
    else:
        logger.error(f"{where} {type(exc).__name__}: {exc}", exc_info=True)


async def reply_api_error(
    send,
    exc: BaseException,
    *,
    context_label: str,
    default: str | None = None,
    clear_hint: str = CLEAR_HINT_DEFAULT,
) -> None:
    """Залогировать ошибку и отправить пользователю человеческое сообщение.

    send — корутина одного аргумента (update.message.reply_text,
    functools.partial(send_whatsapp_message, phone) и т.п.).
    default — что сказать, если исключение вообще не от Anthropic API
    (например «Не смогла прочитать файл»).
    """
    log_api_error(exc, context_label=context_label)
    try:
        await send(user_message(exc, default, clear_hint))
    except Exception as send_exc:
        logger.error(f"[{context_label}] не смогла отправить сообщение об ошибке: {send_exc}")


async def _sleep_with_keepalive(delay: float, keepalive, label: str) -> None:
    """Пауза между попытками, в течение которой работает keepalive.

    keepalive — корутина, принимающая asyncio.Event и тикающая, пока он не выставлен
    (в боте — обновление «печатает…» каждые 4с). Индикатор Telegram живёт ~5 секунд,
    а пауза бывает 15 — поэтому его надо держать ВО ВРЕМЯ сна, а не дёргать после.
    """
    if keepalive is None:
        await asyncio.sleep(delay)
        return
    stop_event = asyncio.Event()
    task = asyncio.create_task(keepalive(stop_event))
    try:
        await asyncio.sleep(delay)
    finally:
        stop_event.set()
        try:
            await task
        except Exception as keepalive_exc:
            logger.debug(f"[{label}] keepalive failed: {keepalive_exc}")


async def call_with_retry(fn, *, label: str, keepalive=None):
    """Выполнить синхронный вызов API в отдельном потоке, переживая транзиентные ошибки.

    fn — функция без аргументов (обычно functools.partial(client.messages.create, ...)).
    Ретраи наши, а не SDK-шные: SDK умеет max_retries с бэкоффом (проверено в 0.43.0:
    ретраит 408/409/429/5xx, задержка 0.5→8с), но это блокирующий sleep внутри потока,
    между попытками некому обновить «печатает…» и задержки перемножились бы с нашими.
    Поэтому здесь fn обязан быть построен на клиенте с max_retries=0.

    keepalive — необязательная корутина, принимающая asyncio.Event; работает всю паузу
    между попытками (см. _sleep_with_keepalive).
    """
    attempts = len(RETRY_DELAYS) + 1
    for attempt in range(1, attempts + 1):
        try:
            return await asyncio.to_thread(fn)
        except Exception as exc:
            if attempt == attempts or not is_retryable(exc):
                raise
            delay = RETRY_DELAYS[attempt - 1]
            logger.warning(
                f"[{label}] {type(exc).__name__}: повтор через {delay}с "
                f"(попытка {attempt}/{attempts})"
            )
            await _sleep_with_keepalive(delay, keepalive, label)
