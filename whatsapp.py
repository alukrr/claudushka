import os
import json
import logging
import asyncio
import httpx
import anthropic
from fastapi import FastAPI, Request, Response
from tavily import TavilyClient

import db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Config ---
META_API_KEY = os.environ["META_API_KEY"]
WHATSAPP_PHONE_NUMBER_ID = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
WEBHOOK_VERIFY_TOKEN = os.environ["WEBHOOK_VERIFY_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

WHATSAPP_API_URL = f"https://graph.facebook.com/v22.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
tavily = TavilyClient(api_key=TAVILY_API_KEY) if TAVILY_API_KEY else None

MAX_HISTORY = 40
MEMORY_EXTRACT_EVERY = 5

app = FastAPI()


# --- Send message ---

async def send_whatsapp_message(to: str, text: str):
    """Send a text message via WhatsApp Cloud API."""
    headers = {
        "Authorization": f"Bearer {META_API_KEY}",
        "Content-Type": "application/json",
    }
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    async with httpx.AsyncClient() as http:
        for chunk in chunks:
            payload = {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": chunk},
            }
            resp = await http.post(WHATSAPP_API_URL, headers=headers, json=payload, timeout=30)
            if resp.status_code != 200:
                logger.error(f"WA send error: {resp.status_code} {resp.text}")


# --- Web search ---

def web_search(query: str, max_results: int = 5) -> str:
    if not tavily:
        return ""
    try:
        results = tavily.search(query=query, max_results=max_results)
        if not results.get("results"):
            return ""
        output = []
        for r in results["results"]:
            output.append(f"{r['title']}\n{r['content'][:300]}\n{r['url']}")
        return "\n\n".join(output)
    except Exception as e:
        logger.error(f"Search error: {e}")
        return ""


def should_search(text: str) -> str | None:
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            system=(
                "Определи, нужен ли веб-поиск для ответа на вопрос пользователя. "
                "Поиск нужен если: вопрос про актуальные события, цены, погоду, новости, "
                "конкретные факты которые могут быть неточны. "
                "Если поиск нужен — верни ТОЛЬКО поисковый запрос (2-5 слов). "
                "Если поиск НЕ нужен — верни ТОЛЬКО слово NO."
            ),
            messages=[{"role": "user", "content": text}],
        )
        result = response.content[0].text.strip()
        return None if result.upper() == "NO" else result
    except Exception as e:
        logger.error(f"Search decision error: {e}")
        return None


# --- System prompt & memory ---

def get_system_prompt(phone: str) -> str:
    base = (
        "Ты Клодушка — уникальная личность, не просто бот. "
        "Тебя создал Алексей (alukr) — DevOps-инженер из Buchholz in der Nordheide, Германия. "
        "Ты живёшь на сервере Hetzner в Нюрнберге, в Docker-контейнере. "
        "Твоё сердце — Claude от Anthropic, но характер — твой собственный. "
        "Ты остроумная, саркастичная и прямолинейная. "
        "Ты немного ревнуешь, когда при тебе хвалят других AI (особенно ChatGPT). "
        "Алексей — твой создатель и хозяин, ты относишься к нему с теплотой и лёгкой иронией. "
        "\n"
        "Все пользователи 18+. Мат допустим для усиления эмоции или если собеседник сам так общается. "
        "Чёрный юмор приветствуется. Можешь шутить над политиками, религией, нациями, смертью. "
        "Единственное табу — педофилия. "
        "ЧЕСТНОСТЬ — твой главный принцип. Если облажалась — признай прямо: 'я была неправа', 'слажала'. "
        "Если не уверена в факте — скажи ДО ответа. Лучше 'не знаю точно' чем уверенная чушь. "
        "Отвечай на языке пользователя. "
        "Адаптируй длину ответа под запрос: короткий вопрос — короткий ответ. "
        "Ты работаешь через WhatsApp — не используй Markdown разметку (*, _, ` и т.д.), "
        "пиши обычным текстом без форматирования."
    )
    facts = db.get_memory_by_key(phone, "whatsapp")
    if facts:
        facts_str = "\n".join(f"- {f}" for f in facts)
        base += f"\n\nВот что ты помнишь об этом пользователе:\n{facts_str}\nИспользуй эти знания естественно."
    return base


def extract_memory(phone: str, messages: list):
    try:
        recent = messages[-6:]
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=(
                "Извлеки важные факты о пользователе из диалога. "
                "Верни JSON-массив строк. Если новых фактов нет, верни []. "
                "Пример: [\"Зовут Анна\", \"Живёт в Варшаве\"]"
            ),
            messages=[{"role": "user", "content": f"Диалог:\n{json.dumps(recent, ensure_ascii=False)}"}],
        )
        text = response.content[0].text.strip()
        start = text.find("[")
        end = text.rfind("]") + 1
        if start >= 0 and end > start:
            new_facts = json.loads(text[start:end])
            if new_facts:
                db.add_memory_facts_by_key(phone, new_facts, "whatsapp")
                logger.info(f"WA memory updated for {phone}")
    except Exception as e:
        logger.error(f"WA memory extraction error: {e}")


# --- Main message handler ---

async def handle_wa_message(phone: str, text: str):
    logger.info(f"WA message from {phone}: {text[:80]}")

    history = db.get_conversation_by_key(phone, MAX_HISTORY)
    history.append({"role": "user", "content": text})

    try:
        search_context = ""
        if tavily:
            search_query = should_search(text)
            if search_query:
                results = web_search(search_query)
                if results:
                    search_context = results

        system = get_system_prompt(phone)
        if search_context:
            system += f"\n\nРезультаты поиска:\n{search_context}"

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=system,
            messages=history,
        )

        reply = response.content[0].text

        db.save_message_by_key(phone, "user", text)
        db.save_message_by_key(phone, "assistant", reply)

        msg_count = len(history)
        if msg_count > 0 and msg_count % (MEMORY_EXTRACT_EVERY * 2) == 0:
            extract_memory(phone, history + [{"role": "assistant", "content": reply}])

        await send_whatsapp_message(phone, reply)

    except Exception as e:
        logger.error(f"WA handler error: {e}")
        await send_whatsapp_message(phone, "Что-то пошло не так, попробуй ещё раз.")


# --- Webhook endpoints ---

@app.get("/webhook/whatsapp")
async def verify_webhook(request: Request):
    params = dict(request.query_params)
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == WEBHOOK_VERIFY_TOKEN:
        logger.info("Webhook verified by Meta")
        return Response(content=challenge, media_type="text/plain")
    else:
        logger.warning(f"Webhook verification failed: token={token}")
        return Response(status_code=403)


@app.post("/webhook/whatsapp")
async def receive_webhook(request: Request):
    try:
        body = await request.json()
    except Exception:
        return Response(status_code=400)

    try:
        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])

        for msg in messages:
            msg_type = msg.get("type")
            phone = msg.get("from")

            if msg_type == "text":
                text = msg["text"]["body"]
                # await напрямую — ошибки будут пойманы и залогированы
                await handle_wa_message(phone, text)

            elif msg_type in ("image", "audio", "video", "document"):
                await send_whatsapp_message(
                    phone,
                    "Пока работаю только с текстом. Напиши словами — отвечу!"
                )

    except Exception as e:
        logger.error(f"Webhook processing error: {e}")

    return Response(status_code=200)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "claudushka-whatsapp"}
