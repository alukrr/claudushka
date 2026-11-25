"""Прогон цепочки фолбэка вживую НА СТЕНДЕ (docs/claude/staging-checklist-fallback-chain.md, issue #5). Не для прода и не для pytest.

Запуск (из каталога стенда, контейнер тестового бота; отдельный процесс, ходит в настоящий пул и к Google по ключам из .env.test):
    docker cp scripts/stand_fallback_chain.py claudushka-test:/tmp/ && docker exec claudushka-test python3 /tmp/stand_fallback_chain.py two-hops
Сценарии:
    two-hops   — несуществующая модель пула (404) → Flash у Google с НЕВЕРНЫМ ключом (400 «API key not valid») → GPT Terra. Цепочку и ключ
                 подменяем только в этом процессе; бот стенда не трогаем.
    google-key — модель чата /flash_g с неверным ключом Google → Flash пула (если жив; иначе дальше Terra).
    real       — просто вызов STAND_MODEL (по умолчанию gemini-3.8-flash) как есть: нормальный путь для сравнения времени.
Печатает: по звеньям (по логу FALLBACK), модель ответа, время, последние строки usage_log (метка *_fallback). Тексты алертов админам в
этом процессе уходят в лог («бот ещё не поднят») — их видно в выводе. Расход — копейки со счёта пула/Google.
"""
import asyncio
import logging
import os
import sys
import time

sys.path.insert(0, "/app")
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
import api_errors  # noqa: E402
import bot  # noqa: E402
import db  # noqa: E402

CHAT = int(os.environ.get("STAND_CHAT", "-1"))   # чат для учёта расхода в usage_log стенда (любой отрицательный id)
HI = [{"role": "user", "content": "Ответь одним словом: привет."}]


async def ask(model: str):
    bot.usage_ctx.set((CHAT, "group", None))
    t0 = time.monotonic()
    try:
        r = await bot.call_claude(None, CHAT, label="stand_chain", usage_label="dialog", model=model,
                                  max_tokens=bot.out_tokens(model, 60), system="Ты бот. Отвечай кратко.", messages=HI)
        print(f"ОТВЕТ model={r.model!r} текст={api_errors.response_text(r)[:80]!r} за {time.monotonic() - t0:.1f} с")
    except Exception as e:  # noqa: BLE001
        print(f"ОШИБКА {type(e).__name__} HTTP {getattr(e, 'status_code', None)} за {time.monotonic() - t0:.1f} с: {str(e)[:200]}")


def tail():
    c = db.get_conn()
    try:
        for r in c.execute("SELECT ts, label, model, input, output, cost_usd FROM usage_log WHERE chat_id = ? ORDER BY ts DESC LIMIT 3", (CHAT,)):
            print("usage_log:", tuple(r))
    finally:
        c.close()


def main():
    scenario = sys.argv[1] if len(sys.argv) > 1 else "two-hops"
    bot._fallback_until.clear()
    if scenario == "two-hops":
        bot.GEMINI_API_KEY = "bad-key-for-stand-test"
        ghost = "gemini-9-nonexistent"
        bot.FALLBACK_CHAINS[ghost] = ("google-direct/gemini-3.8-flash", "gpt-6.1-terra")
        print("цепочка:", bot.fallback_chain(ghost), "(ключ Google подменён на неверный)")
        asyncio.run(ask(ghost))
    elif scenario == "google-key":
        bot.GEMINI_API_KEY = "bad-key-for-stand-test"
        m = bot.MODELS["flash_g"]["id"]
        print("цепочка:", bot.fallback_chain(m))
        asyncio.run(ask(m))
    elif scenario == "real":
        m = os.environ.get("STAND_MODEL", "gemini-3.8-flash")
        print("цепочка:", bot.fallback_chain(m))
        asyncio.run(ask(m))
    else:
        sys.exit(f"неизвестный сценарий {scenario!r}")
    tail()


if __name__ == "__main__":
    main()
