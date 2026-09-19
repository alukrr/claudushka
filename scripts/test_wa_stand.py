#!/usr/bin/env python3
"""Тест WhatsApp-цепочки на тестовом стенде (docs/claude/staging.md, блок D ТЗ-4).

Собирает Meta-подобный webhook payload с текстовым сообщением, подписывает его
WHATSAPP_APP_SECRET (HMAC-SHA256, как настоящий вебхук Meta) и отправляет на
claudushka-wa-test через curl. Стенд обработает его как реальное входящее сообщение
и пришлёт ответ ЧЕРЕЗ РЕАЛЬНЫЙ WhatsApp на указанный номер — так проверяется вся
цепочка (подпись → дедуп → очередь по номеру → Claude → отправка), кроме самого
факта доставки от Meta (это подделываем).

Секретов в файле нет: WHATSAPP_APP_SECRET читается из .env.test в корне репозитория
(рядом с этим скриптом, в scripts/..), либо передаётся флагом --secret.

Использование (с сервера, из корня репозитория):
    python3 scripts/test_wa_stand.py <номер получателя без +> ["текст сообщения"]

Пример:
    python3 scripts/test_wa_stand.py 491701234567 "привет со стенда"
"""
import argparse
import hashlib
import hmac
import json
import subprocess
import sys
import uuid
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8081/webhook/whatsapp"


def read_secret_from_env_test() -> str | None:
    """Ищет WHATSAPP_APP_SECRET в .env.test в корне репо (родитель scripts/)."""
    env_test = Path(__file__).resolve().parent.parent / ".env.test"
    if not env_test.exists():
        return None
    for line in env_test.read_text().splitlines():
        line = line.strip()
        if line.startswith("WHATSAPP_APP_SECRET="):
            return line.split("=", 1)[1].strip()
    return None


def build_payload(phone: str, text: str) -> bytes:
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": f"wamid.TESTSTAND{uuid.uuid4().hex[:16]}",
                        "from": phone,
                        "type": "text",
                        "text": {"body": text},
                    }]
                }
            }]
        }]
    }
    return json.dumps(payload).encode()


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("phone", help="Номер получателя для Meta, без '+' (например 491701234567)")
    parser.add_argument("text", nargs="?", default="тест со стенда", help="Текст сообщения")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"URL вебхука стенда (дефолт: {DEFAULT_URL})")
    parser.add_argument("--secret", help="WHATSAPP_APP_SECRET явно (по умолчанию читается из .env.test)")
    args = parser.parse_args()

    secret = args.secret or read_secret_from_env_test()
    if not secret:
        print(
            "WHATSAPP_APP_SECRET не найден: запусти из корня репозитория, где есть "
            ".env.test, либо передай --secret явно.",
            file=sys.stderr,
        )
        return 1

    body = build_payload(args.phone, args.text)
    signature = sign(body, secret)

    result = subprocess.run(
        [
            "curl", "-s", "-w", "\nHTTP %{http_code}\n",
            args.url,
            "-H", f"x-hub-signature-256: {signature}",
            "-H", "content-type: application/json",
            "--data-binary", "@-",
        ],
        input=body,
        capture_output=True,
    )
    print(result.stdout.decode(errors="replace"))
    if result.stderr:
        print(result.stderr.decode(errors="replace"), file=sys.stderr)
    if result.returncode != 0:
        return result.returncode

    print(
        f"Отправлено. Если стенд (claudushka-wa-test) поднят и .env.test корректен — "
        f"на номер {args.phone} должен прийти реальный ответ в WhatsApp в течение "
        f"нескольких секунд. Смотри docker logs claudushka-wa-test, если ответа нет."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
