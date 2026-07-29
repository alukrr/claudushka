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


def is_prompt_too_long(exc: BaseException) -> bool:
    """400 с 'prompt is too long' — особый случай: лечится обрезкой истории, не ретраем."""
    return isinstance(exc, anthropic.BadRequestError) and PROMPT_TOO_LONG_MARKER in str(exc).lower()


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


def user_message(exc: BaseException, default: str | None = None, clear_hint: str = "Сделай /clear.") -> str:
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
        return "Запрос не прошёл: API он не понравился. Попробуй переформулировать."
    if isinstance(exc, anthropic.APIConnectionError):
        return "Не достучалась до сервиса. Попробуй ещё раз."
    if isinstance(exc, anthropic.APIStatusError):
        return "Сервис ответил ошибкой. Попробуй позже."
    return default or "Что-то сломалось на моей стороне. Попробуй ещё раз."


def history_chars(messages: list) -> int:
    """Размер истории в символах. content бывает списком блоков (фото) — считаем как есть."""
    return sum(len(str(m.get("content", ""))) for m in messages)


def halve_history(messages: list) -> list:
    """Оставить свежую половину истории (лечение 400 prompt is too long).

    Первым обязан идти user — иначе API вернёт уже другой 400 про чередование ролей.
    """
    if len(messages) <= 2:
        return messages
    trimmed = messages[len(messages) // 2:]
    while trimmed and trimmed[0].get("role") != "user":
        trimmed = trimmed[1:]
    return trimmed or messages[-1:]


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
    clear_hint: str = "Сделай /clear.",
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


async def call_with_retry(fn, *, label: str, on_retry=None):
    """Выполнить синхронный вызов API в отдельном потоке, переживая транзиентные ошибки.

    fn — функция без аргументов (обычно functools.partial(client.messages.create, ...)).
    Ретраи наши, а не SDK-шные: SDK умеет max_retries с бэкоффом (проверено в 0.43.0:
    ретраит 408/409/429/5xx, задержка 0.5→8с), но это блокирующий sleep внутри потока,
    между попытками некому обновить «печатает…» и задержки перемножились бы с нашими.
    Поэтому здесь fn обязан быть построен на клиенте с max_retries=0.

    on_retry — необязательная корутина без аргументов, вызывается перед каждым повтором.
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
            await asyncio.sleep(delay)
            if on_retry is not None:
                try:
                    await on_retry()
                except Exception as keepalive_exc:
                    logger.debug(f"[{label}] on_retry failed: {keepalive_exc}")
