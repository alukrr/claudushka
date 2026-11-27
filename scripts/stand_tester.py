"""Тестер стенда: пользовательские аккаунты (Telethon, MTProto) пишут тестовому боту и проверяют ответы. Issue #6.

Только для стенда: тестовый бот и тестовая группа. На прод-бота и боевые чаты не направлять.
Живёт на сервере рядом со стендом, запускается разовым контейнером (сервис `stand-tester` в docker-compose.test.yml,
профиль `tester`), удобнее всего — функцией `tester` из docs/claude/staging-aliases.sh:

    tester login a                 # один раз на аккаунт: номер, код из Telegram, пароль 2FA (если есть)
    tester whoami                  # кто залогинен, их user_id (для /verify, /manager и т.п.)
    tester chats a                 # последние диалоги аккаунта с id — найти id тестовой группы
    tester say a "/id"             # одно сообщение в личку тестовому боту и его ответ
    tester say b "привет" --to group
    tester run smoke               # сценарий scripts/stand_scenarios/smoke.txt
    tester check                   # разобрать все сценарии без сети (ошибки синтаксиса)

Настройки — data-tester/tester.env (KEY=VALUE, только на сервере; образец scripts/stand_tester.env.example) или переменные окружения:
    TG_API_ID, TG_API_HASH   — с my.telegram.org (один раз, с любого аккаунта)
    TESTER_BOT               — @username тестового бота
    TESTER_GROUP             — id тестовой группы (`to group` в сценарии), необязательно
    TESTER_GAP=2             — минимум секунд между нашими сообщениями (антиспам Telegram)
    TESTER_TIMEOUT=90        — сколько ждать первого ответа бота
    TESTER_SETTLE=4          — бот «затих» столько секунд — ответ считаем полным

Формат сценария (строка — шаг, `#` — комментарий, `${VAR}` — подстановка из настроек/окружения):
    to bot | to group | to <id|@username>   — куда пишем дальше (по умолчанию bot)
    a: текст                — тестер a пишет и ждёт ответ бота; `\\n` в тексте — перенос строки
    a!: текст               — пишет и НЕ ждёт (реплика в группе, не обращённая к боту)
    a reply: текст          — ответ реплаем на последнее сообщение бота в этом чате
    a click: Да             — нажать кнопку (по подстроке текста) на последнем сообщении бота с кнопками
    a file: путь | подпись  — отправить файл (путь — относительно каталога сценариев)
    a voice: путь.ogg       — отправить как голосовое
    a rename: Тест          — сменить отображаемое имя аккаунта (first_name, фамилия пустая) — тёзки и смена ника; прежнее
                              имя возвращается САМО в конце прогона (и при ошибке)
    expect: регэксп         — в ответе на последнее действие есть совпадение (без учёта регистра)
    expect_not: регэксп     — совпадения нет
    silent 10               — бот молчит 10 с (в текущем чате)
    a react: 2 😁           — поставить реакцию на 2-е с конца сообщение чата (1 — последнее, любой отправитель); `-` вместо эмодзи — снять
    a react: bot 1 😁       — то же, но n считается среди сообщений БОТА (1 — его последнее)
    sleep 5 / timeout 180 / settle 6

Файлы сессий (data-tester/<имя>.session) — полный доступ к аккаунту: каталог 700, не в git, не в чат.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(os.environ.get("TESTER_DATA", "/data"))
SCEN_DIR = Path(os.environ.get("TESTER_SCENARIOS", "/app/scenarios"))
CONF_FILE = DATA_DIR / "tester.env"
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,15}$")
VAR_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
ACT_RE = re.compile(r"^([a-z][a-z0-9_]{0,15})(!?)(?:\s+(reply|click|file|voice|rename|react))?\s*:\s?(.*)$")
REACT_RE = re.compile(r"^(bot\s+)?(\d{1,3})\s+(\S+)$")      # `a react: [bot] N эмодзи|-`
RENAME_MAX = 64                       # лимит Telegram на first_name


# --------------------------------------------------------------------------- настройки

def load_config(path: Path = CONF_FILE, environ: dict | None = None) -> dict:
    """tester.env (KEY=VALUE, # — комментарий) + окружение поверх него."""
    conf: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            conf[k.strip()] = v.strip().strip('"').strip("'")
    env = os.environ if environ is None else environ
    for k, v in env.items():
        if k.startswith(("TG_", "TESTER_")):
            conf[k] = v
    return conf


# --------------------------------------------------------------------------- разбор сценария

class ScenarioError(Exception):
    pass


@dataclass
class Step:
    kind: str            # to | send | reply | click | file | voice | expect | expect_not | silent | sleep | timeout | settle
    line: int
    arg: str = ""
    who: str = ""
    wait: bool = True    # для send: ждать ли ответ
    num: float = 0.0


def _subst(text: str, conf: dict, line: int, strict: bool = True) -> str:
    def rep(m):
        name = m.group(1)
        if name not in conf:
            if not strict:
                return f"<{name}>"
            raise ScenarioError(f"строка {line}: нет переменной {name} (tester.env или окружение)")
        return conf[name]
    return VAR_RE.sub(rep, text)


def parse_scenario(text: str, conf: dict, strict: bool = True) -> list[Step]:
    """strict=False — для `check`: незаданные ${VAR} не ошибка (настроек на машине разработчика нет)."""
    steps: list[Step] = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        low = line.split(None, 1)
        head = low[0].lower()
        rest = low[1].strip() if len(low) > 1 else ""

        if head == "to":
            if not rest:
                raise ScenarioError(f"строка {n}: `to` без адреса")
            steps.append(Step("to", n, arg=_subst(rest, conf, n, strict)))
            continue
        if head in ("silent", "sleep", "timeout", "settle"):
            try:
                val = float(rest)
            except ValueError:
                raise ScenarioError(f"строка {n}: `{head}` ждёт число секунд, а не {rest!r}") from None
            if val <= 0:
                raise ScenarioError(f"строка {n}: `{head}` должно быть > 0")
            steps.append(Step(head, n, num=val))
            continue
        m_exp = re.match(r"^(expect|expect_not)\s*:\s?(.*)$", line)
        if m_exp:
            pat = _subst(m_exp.group(2).strip(), conf, n, strict)
            if not pat:
                raise ScenarioError(f"строка {n}: пустой регэксп")
            try:
                re.compile(pat)
            except re.error as e:
                raise ScenarioError(f"строка {n}: плохой регэксп {pat!r}: {e}") from None
            steps.append(Step(m_exp.group(1), n, arg=pat))
            continue
        m = ACT_RE.match(line)
        if not m:
            raise ScenarioError(f"строка {n}: не понимаю {line!r}")
        who, nowait, verb, body = m.group(1), m.group(2), m.group(3), m.group(4)
        body = _subst(body, conf, n, strict)
        if verb is None:
            if not body:
                raise ScenarioError(f"строка {n}: пустое сообщение")
            steps.append(Step("send", n, arg=body.replace("\\n", "\n"), who=who, wait=not nowait))
        else:
            if not body.strip():
                raise ScenarioError(f"строка {n}: `{verb}` без аргумента")
            if verb == "reply":
                body = body.replace("\\n", "\n")
            if verb == "react" and not REACT_RE.match(body.strip()):
                raise ScenarioError(f"строка {n}: `react` ждёт «[bot] N эмодзи|-», а не {body!r}")
            if verb == "rename" and len(body.strip()) > RENAME_MAX:
                raise ScenarioError(f"строка {n}: имя длиннее {RENAME_MAX} знаков")
            steps.append(Step(verb, n, arg=body.strip(), who=who, wait=not nowait))
    return steps


# --------------------------------------------------------------------------- вывод

class Log:
    def __init__(self, path: Path | None):
        self.fh = None
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.fh = path.open("w", encoding="utf-8")

    def __call__(self, msg: str = ""):
        stamp = datetime.now().strftime("%H:%M:%S")
        for i, ln in enumerate(msg.splitlines() or [""]):
            out = f"{stamp} {ln}" if i == 0 else f"         {ln}"
            print(out, flush=True)
            if self.fh:
                self.fh.write(out + "\n")
                self.fh.flush()

    def close(self):
        if self.fh:
            self.fh.close()


def render(m) -> str:
    """Текст сообщения бота для проверок и лога: текст/подпись + пометки медиа и кнопок."""
    parts = []
    if getattr(m, "voice", None):
        parts.append("[голосовое]")
    elif getattr(m, "photo", None):
        parts.append("[фото]")
    elif getattr(m, "sticker", None):
        parts.append("[стикер]")
    elif getattr(m, "document", None):
        name = getattr(getattr(m, "file", None), "name", None) or "без имени"
        parts.append(f"[документ: {name}]")
    if m.message:
        parts.append(m.message)
    if m.buttons:
        labels = [b.text for row in m.buttons for b in row]
        parts.append("[кнопки: " + " | ".join(labels) + "]")
    return "\n".join(parts) or "[пусто]"


# --------------------------------------------------------------------------- работа с Telegram

def _need_telethon():
    try:
        import telethon  # noqa: F401
    except ImportError:
        sys.exit("нет telethon: запускай через `tester …` (контейнер ставит telethon сам)")


def make_client(name: str, conf: dict):
    from telethon import TelegramClient
    if not NAME_RE.match(name):
        sys.exit(f"имя тестера {name!r}: латиница/цифры, с буквы, до 16 знаков")
    try:
        api_id, api_hash = int(conf["TG_API_ID"]), conf["TG_API_HASH"]
    except (KeyError, ValueError):
        sys.exit(f"нет TG_API_ID/TG_API_HASH — заполни {CONF_FILE} (образец scripts/stand_tester.env.example)")
    return TelegramClient(str(DATA_DIR / name), api_id, api_hash,
                          device_model="Claudushka stand tester", app_version="stand_tester", lang_code="ru")


class Runner:
    def __init__(self, conf: dict, log: Log):
        self.conf = conf
        self.log = log
        self.gap = float(conf.get("TESTER_GAP", 2))
        self.timeout = float(conf.get("TESTER_TIMEOUT", 90))
        self.settle = float(conf.get("TESTER_SETTLE", 4))
        self.clients: dict = {}
        self.bot_ids: dict = {}           # who -> user_id бота (через entity в его клиенте)
        self.target = "bot"
        self.last_text = None             # ответ на последнее действие (для expect)
        self.last_bot_msg: dict = {}      # (who|None, target) -> id последнего сообщения бота
        self.last_sent_at = 0.0
        self.last_who = None              # от чьего имени смотреть `silent`
        self.passed = 0
        self.failed = 0
        self.orig_names: dict = {}        # who -> (first_name, last_name) до первого `rename` — вернуть в close()

    @staticmethod
    async def _set_name(c, first: str, last: str):
        from telethon.tl.functions.account import UpdateProfileRequest
        await c(UpdateProfileRequest(first_name=first, last_name=last))

    async def _rename(self, st: Step):
        c = await self.client(st.who)
        if st.who not in self.orig_names:
            me = await c.get_me()
            self.orig_names[st.who] = (me.first_name or "", me.last_name or "")
        await self._pace()
        await self._set_name(c, st.arg, "")
        self.log(f"{st.who} ✎ имя → «{st.arg}»")
        await asyncio.sleep(self.gap)     # бот получит новое имя со следующим сообщением; дать Telegram разойтись

    async def _react(self, st: Step, c, ent):
        """`a react: [bot] N эмодзи|-` — Telethon SendReactionRequest на N-е с конца сообщение чата (или N-е среди сообщений бота)."""
        from telethon.tl.functions.messages import SendReactionRequest
        from telethon.tl.types import ReactionEmoji
        m = REACT_RE.match(st.arg)
        only_bot, n, emoji = bool(m.group(1)), int(m.group(2)), m.group(3)
        msgs = await c.get_messages(ent, limit=50 if only_bot else n)
        if only_bot:
            msgs = [x for x in msgs if x.sender_id == self.bot_ids[st.who] and not x.out]
        if len(msgs) < n:
            raise ScenarioError(f"строка {st.line}: в чате нет {n}-го с конца сообщения{' бота' if only_bot else ''}")
        target = msgs[n - 1]
        self.last_who = st.who
        await self._pace()
        await c(SendReactionRequest(peer=ent, msg_id=target.id, reaction=[] if emoji == "-" else [ReactionEmoji(emoticon=emoji)]))
        self.log(f"{st.who} ♥ {'снял реакцию' if emoji == '-' else emoji} → {'сообщение бота' if only_bot else 'сообщение'} №{n} с конца")

    async def client(self, who: str):
        if who in self.clients:
            return self.clients[who]
        if not (DATA_DIR / f"{who}.session").exists():
            raise ScenarioError(f"тестер {who!r} не залогинен: tester login {who}")
        c = make_client(who, self.conf)
        await c.connect()
        if not await c.is_user_authorized():
            raise ScenarioError(f"сессия {who!r} больше не авторизована: tester login {who}")
        bot = self.conf.get("TESTER_BOT")
        if not bot:
            raise ScenarioError("нет TESTER_BOT (@username тестового бота) в tester.env")
        self.bot_ids[who] = (await c.get_entity(bot)).id
        self.clients[who] = c
        return c

    async def entity(self, who: str):
        c = await self.client(who)
        t = self.target
        if t == "bot":
            t = self.conf["TESTER_BOT"]
        elif t == "group":
            t = self.conf.get("TESTER_GROUP") or ""
            if not t:
                raise ScenarioError("`to group`, но TESTER_GROUP не задан")
        key = int(t) if re.fullmatch(r"-?\d+", t) else t
        try:
            return await c.get_entity(key)
        except ValueError:
            await c.get_dialogs()          # id группы ещё не в кэше сессии — подгрузить диалоги и повторить
            return await c.get_entity(key)

    def _key(self, who: str):
        return (who if self.target == "bot" else None, self.target)

    async def _pace(self):
        wait = self.gap - (time.monotonic() - self.last_sent_at)
        if wait > 0:
            await asyncio.sleep(wait)
        self.last_sent_at = time.monotonic()

    async def _bot_msgs(self, c, ent, who, min_id: int) -> dict:
        out = {}
        for m in await c.get_messages(ent, limit=50, min_id=min_id):
            if m.sender_id == self.bot_ids[who] and not m.out:
                out[m.id] = m
        return out

    async def _collect(self, c, ent, who, min_id: int, before: dict, timeout: float) -> list:
        """Ждёт новые/изменённые сообщения бота после min_id, пока он не затихнет на settle секунд."""
        seen: dict = {}
        started = last_change = time.monotonic()
        while True:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            for mid, m in (await self._bot_msgs(c, ent, who, min_id)).items():
                txt = render(m)
                if before.get(mid) == txt or seen.get(mid, (None,))[0] == txt:
                    continue
                seen[mid] = (txt, m)
                last_change = now
            if seen and now - last_change >= self.settle:
                break
            if not seen and now - started >= timeout:
                break
            if now - started >= timeout + 60:   # бот всё говорит и говорит — хватит
                break
        msgs = [seen[k][1] for k in sorted(seen)]
        if msgs:
            self.last_bot_msg[self._key(who)] = msgs[-1].id
        return msgs

    def _show(self, who: str, msgs: list, waited: bool):
        if not waited:
            self.last_text = None
            return
        if not msgs:
            self.log("   ← (бот молчит)")
            self.last_text = ""
            return
        texts = [render(m) for m in msgs]
        for t in texts:
            self.log("   ← " + t)
        self.last_text = "\n".join(texts)

    async def do(self, st: Step):
        if st.kind == "to":
            self.target = st.arg
            self.log(f"→ чат: {st.arg}")
            return
        if st.kind in ("timeout", "settle"):
            setattr(self, st.kind, st.num)
            return
        if st.kind == "sleep":
            await asyncio.sleep(st.num)
            return
        if st.kind in ("expect", "expect_not"):
            self._check(st)
            return
        if st.kind == "silent":
            who = self.last_who
            if who is None:
                raise ScenarioError(f"строка {st.line}: `silent` до первого сообщения — не от чьего имени смотреть")
            c, ent = await self.client(who), await self.entity(who)
            last = (await c.get_messages(ent, limit=1))
            min_id = last[0].id if last else 0
            await asyncio.sleep(st.num)
            got = await self._bot_msgs(c, ent, who, min_id)
            ok = not got
            self._verdict(ok, f"silent {st.num:g}", "" if ok else render(got[max(got)]))
            return

        if st.kind == "rename":
            await self._rename(st)
            return

        c = await self.client(st.who)
        ent = await self.entity(st.who)
        await self._pace()
        if st.kind == "react":
            await self._react(st, c, ent)
            return
        if st.kind == "click":
            mid = self.last_bot_msg.get(self._key(st.who))
            if not mid:
                raise ScenarioError(f"строка {st.line}: нечего нажимать — бот ещё ничего не прислал в этом чате")
            msg = await c.get_messages(ent, ids=mid)
            if not msg or not msg.buttons:
                raise ScenarioError(f"строка {st.line}: у последнего сообщения бота нет кнопок")
            labels = [b.text for row in msg.buttons for b in row]
            hit = next((b for b in labels if st.arg.lower() in b.lower()), None)
            if hit is None:
                raise ScenarioError(f"строка {st.line}: нет кнопки с «{st.arg}», есть: {labels}")
            before = {k: render(v) for k, v in (await self._bot_msgs(c, ent, st.who, mid - 1)).items()}
            self.log(f"{st.who} ⏺ [{hit}]")
            self.last_who = st.who
            answer = await msg.click(text=hit)
            toast = getattr(answer, "message", None) if answer is not None else None
            if toast:
                self.log(f"   ← [всплывашка] {toast}")
            msgs = await self._collect(c, ent, st.who, mid - 1, before, self.timeout) if st.wait else []
            self._show(st.who, msgs, st.wait)
            if toast and st.wait:
                self.last_text = (f"[всплывашка] {toast}\n" + (self.last_text or "")).strip()
            return

        self.last_who = st.who
        reply_to = None
        if st.kind == "reply":
            reply_to = self.last_bot_msg.get(self._key(st.who))
            if not reply_to:
                raise ScenarioError(f"строка {st.line}: не на что отвечать — бот ещё ничего не прислал в этом чате")
        if st.kind in ("file", "voice"):
            path_s, _, caption = st.arg.partition("|")
            path = Path(path_s.strip())
            if not path.is_absolute():
                path = SCEN_DIR / path
            if not path.exists():
                raise ScenarioError(f"строка {st.line}: нет файла {path}")
            sent = await c.send_file(ent, str(path), caption=caption.strip() or None, voice_note=st.kind == "voice")
            self.log(f"{st.who} → [{st.kind}: {path.name}] {caption.strip()}".rstrip())
        else:
            sent = await c.send_message(ent, st.arg, reply_to=reply_to, link_preview=False)
            mark = "↩ " if reply_to else ""
            self.log(f"{st.who} → {mark}{st.arg}" + ("" if st.wait else "   (без ожидания)"))
        msgs = await self._collect(c, ent, st.who, sent.id, {}, self.timeout) if st.wait else []
        self._show(st.who, msgs, st.wait)

    def _check(self, st: Step):
        if self.last_text is None:
            raise ScenarioError(f"строка {st.line}: `{st.kind}` не к чему применить — перед ним нет действия с ожиданием ответа")
        found = re.search(st.arg, self.last_text, re.IGNORECASE | re.DOTALL)
        ok = bool(found) if st.kind == "expect" else not found
        self._verdict(ok, f"{st.kind}: {st.arg}", "" if ok else (self.last_text[:300] or "(ответа нет)"))

    def _verdict(self, ok: bool, what: str, detail: str):
        if ok:
            self.passed += 1
            self.log(f"   ✓ {what}")
        else:
            self.failed += 1
            self.log(f"   ✗ {what}" + (f"\n     в ответе: {detail}" if detail else ""))

    async def close(self):
        for who, (first, last) in self.orig_names.items():   # вернуть имена аккаунтов, даже если прогон упал
            try:
                await self._set_name(self.clients[who], first, last)
                self.log(f"{who} ✎ имя возвращено → «{(first + ' ' + last).strip()}»")
            except Exception as e:  # noqa: BLE001
                self.log(f"!!! {who}: имя НЕ возвращено ({type(e).__name__}: {e}) — вернуть руками в Telegram: «{first} {last}»")
        self.orig_names.clear()
        for c in self.clients.values():
            await c.disconnect()


# --------------------------------------------------------------------------- команды

def scenario_path(name: str) -> Path:
    p = Path(name)
    if p.exists():
        return p
    for cand in (SCEN_DIR / name, SCEN_DIR / f"{name}.txt"):
        if cand.exists():
            return cand
    sys.exit(f"нет сценария {name!r} (ищу в {SCEN_DIR})")


async def run_steps(steps: list[Step], conf: dict, log: Log) -> int:
    r = Runner(conf, log)
    try:
        for st in steps:
            await r.do(st)
    except ScenarioError as e:
        log(f"ОСТАНОВ: {e}")
        r.failed += 1
    finally:
        await r.close()
    log(f"итог: ✓ {r.passed}  ✗ {r.failed}")
    return 1 if r.failed else 0


async def cmd_login(name: str, conf: dict) -> int:
    c = make_client(name, conf)
    await c.start()                      # спросит номер, код из Telegram и пароль 2FA
    me = await c.get_me()
    print(f"готово: {name} = {me.first_name} @{me.username or '-'} id={me.id}")
    await c.disconnect()
    return 0


async def cmd_whoami(conf: dict) -> int:
    sessions = sorted(DATA_DIR.glob("*.session"))
    if not sessions:
        print("нет ни одной сессии: tester login a")
        return 1
    for s in sessions:
        c = make_client(s.stem, conf)
        await c.connect()
        if await c.is_user_authorized():
            me = await c.get_me()
            print(f"{s.stem}: {me.first_name} @{me.username or '-'} id={me.id}")
        else:
            print(f"{s.stem}: не авторизована — tester login {s.stem}")
        await c.disconnect()
    return 0


async def cmd_chats(name: str, conf: dict) -> int:
    c = make_client(name, conf)
    await c.connect()
    if not await c.is_user_authorized():
        sys.exit(f"tester login {name}")
    async for d in c.iter_dialogs(limit=30):
        print(f"{d.id:>16}  {d.name}")
    await c.disconnect()
    return 0


def main(argv: list[str]) -> int:
    os.umask(0o077)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, args = argv[0], argv[1:]
    conf = load_config()

    if cmd == "check":
        files = [scenario_path(a) for a in args] or sorted(SCEN_DIR.glob("*.txt"))
        bad = 0
        for f in files:
            try:
                n = len(parse_scenario(f.read_text(encoding="utf-8"), conf, strict=False))
                print(f"ok   {f.name}: шагов {n}")
            except ScenarioError as e:
                bad += 1
                print(f"FAIL {f.name}: {e}")
        return 1 if bad else 0

    _need_telethon()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if cmd == "login" and len(args) == 1:
        return asyncio.run(cmd_login(args[0], conf))
    if cmd == "whoami":
        return asyncio.run(cmd_whoami(conf))
    if cmd == "chats" and len(args) == 1:
        return asyncio.run(cmd_chats(args[0], conf))
    if cmd == "say" and len(args) >= 2:
        who, text, rest = args[0], args[1], args[2:]
        steps = []
        while rest:
            opt = rest.pop(0)
            if opt == "--to" and rest:
                steps.append(Step("to", 0, arg=rest.pop(0)))
            elif opt == "--timeout" and rest:
                steps.append(Step("timeout", 0, num=float(rest.pop(0))))
            else:
                sys.exit(f"не понимаю {opt!r}: say <кто> <текст> [--to bot|group|<id>] [--timeout N]")
        steps.append(Step("send", 0, arg=text, who=who))
        return asyncio.run(run_steps(steps, conf, Log(None)))
    if cmd == "run" and args:
        rc = 0
        for name in args:
            path = scenario_path(name)
            try:
                steps = parse_scenario(path.read_text(encoding="utf-8"), conf)
            except ScenarioError as e:
                print(f"{path.name}: {e}")
                rc = 1
                continue
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            log = Log(DATA_DIR / "runs" / f"{stamp}-{path.stem}.log")
            log(f"=== сценарий {path.name}: шагов {len(steps)}")
            rc |= asyncio.run(run_steps(steps, conf, log))
            log.close()
        return rc
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
