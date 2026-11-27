"""Тесты тестера стенда (scripts/stand_tester.py, issue #6). Без сети и без Telegram: разбор сценариев, настройки,
ожидание ответа бота на поддельном клиенте с поддельными часами.

    python -m pytest scripts/test_stand_tester.py -q
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stand_tester as st  # noqa: E402

CONF = {"TESTER_BOT": "@stand_bot", "TESTER_GROUP": "-1001", "TESTER_GROUP_1": "-1002", "TESTER_B_ID": "2", "TESTER_GAP": "0", "TESTER_SETTLE": "4", "TESTER_TIMEOUT": "30"}


# --------------------------------------------------------------------------- разбор

def test_parse_all_kinds():
    text = """
    # комментарий
    to group
    a: привет\\nвторая строка
    b!: реплика без ожидания
    a reply: ответ боту
    a click: Да
    b file: pic.jpg | подпись
    b voice: v.ogg
    expect: User ID: \\d+
    expect_not: Traceback
    silent 5
    sleep 1.5
    timeout 180
    settle 6
    a: ${TESTER_BOT} кто ты?
    """
    steps = st.parse_scenario(text, CONF)
    kinds = [s.kind for s in steps]
    assert kinds == ["to", "send", "send", "reply", "click", "file", "voice", "expect", "expect_not",
                     "silent", "sleep", "timeout", "settle", "send"]
    assert steps[1].arg == "привет\nвторая строка" and steps[1].who == "a" and steps[1].wait
    assert steps[2].wait is False and steps[2].who == "b"
    assert steps[4].arg == "Да"
    assert steps[5].arg == "pic.jpg | подпись"
    assert steps[9].num == 5 and steps[10].num == 1.5
    assert steps[13].arg == "@stand_bot кто ты?"


@pytest.mark.parametrize("bad", [
    "to",                      # без адреса
    "silent пять",             # не число
    "sleep 0",
    "expect: (",               # плохой регэксп
    "expect:",                 # пустой
    "Петя: привет",            # имя не латиницей
    "a:",                      # пустое сообщение
    "a click:",                # без аргумента
    "a: ${NOPE}",              # нет переменной
    "просто текст",
])
def test_parse_errors(bad):
    with pytest.raises(st.ScenarioError):
        st.parse_scenario(bad, CONF)


def test_check_mode_tolerates_missing_vars():
    steps = st.parse_scenario("to ${TESTER_GROUP}\nb: ${TESTER_BOT} hi", {}, strict=False)
    assert steps[0].arg == "<TESTER_GROUP>" and steps[1].arg == "<TESTER_BOT> hi"


def test_bundled_scenarios_parse():
    for f in sorted((Path(__file__).parent / "stand_scenarios").glob("*.txt")):
        assert st.parse_scenario(f.read_text(encoding="utf-8"), CONF), f.name


def test_load_config(tmp_path):
    f = tmp_path / "tester.env"
    f.write_text("# коммент\nTG_API_ID=123\nTG_API_HASH='abc'\nTESTER_BOT=@x\nFOO=bar\n", encoding="utf-8")
    conf = st.load_config(f, environ={"TESTER_BOT": "@override", "HOME": "/root"})
    assert conf["TG_API_ID"] == "123" and conf["TG_API_HASH"] == "abc"
    assert conf["TESTER_BOT"] == "@override"   # окружение поверх файла
    assert "HOME" not in conf


def test_render():
    btn = SimpleNamespace(text="Да")
    m = SimpleNamespace(voice=None, photo=object(), sticker=None, document=None, file=None,
                        message="подпись", buttons=[[btn, SimpleNamespace(text="Нет")]])
    assert st.render(m) == "[фото]\nподпись\n[кнопки: Да | Нет]"
    doc = SimpleNamespace(voice=None, photo=None, sticker=None, document=object(), file=SimpleNamespace(name="a.md"),
                          message="", buttons=None)
    assert st.render(doc) == "[документ: a.md]"


# --------------------------------------------------------------------------- поддельный Telegram

BOT_ID, ME_ID = 777, 1


class Clock:
    def __init__(self):
        self.t = 0.0

    def mono(self):
        return self.t

    async def sleep(self, s):
        self.t += s


def msg(mid, text, sender=BOT_ID, out=False, buttons=None, at=0.0):
    return SimpleNamespace(id=mid, message=text, sender_id=sender, out=out, buttons=buttons, voice=None, photo=None,
                           sticker=None, document=None, file=None, at=at, click=None)


class FakeClient:
    """Чат с ботом: script — что бот «пишет» через сколько секунд после нашего сообщения."""

    def __init__(self, clock, script):
        self.clock, self.script, self.msgs, self.next_id = clock, script, [], 100
        self.sent = []

    def _new_id(self):
        self.next_id += 1
        return self.next_id

    async def send_message(self, ent, text, reply_to=None, link_preview=True):
        m = msg(self._new_id(), text, sender=ME_ID, out=True, at=self.clock.t)
        self.msgs.append(m)
        self.sent.append((text, reply_to))
        for delay, reply_text, buttons in self.script.pop(0) if self.script else []:
            self.msgs.append(msg(self._new_id(), reply_text, buttons=buttons, at=self.clock.t + delay))
        return m

    async def get_messages(self, ent, limit=None, min_id=0, ids=None):
        visible = [m for m in self.msgs if m.at <= self.clock.t]
        if ids is not None:
            return next((m for m in visible if m.id == ids), None)
        return sorted((m for m in visible if m.id > min_id), key=lambda m: -m.id)[:limit]

    async def disconnect(self):
        pass


def make_runner(monkeypatch, script):
    clock = Clock()
    monkeypatch.setattr(st.asyncio, "sleep", clock.sleep)
    monkeypatch.setattr(st.time, "monotonic", clock.mono)
    lines = []
    log = lambda s="": lines.append(s)  # noqa: E731
    log.close = lambda: None
    r = st.Runner(CONF, log)
    fc = FakeClient(clock, script)

    async def client(who):
        r.bot_ids[who] = BOT_ID
        r.clients[who] = fc
        return fc

    async def entity(who):
        return "chat"

    r.client, r.entity = client, entity
    return r, fc, clock, lines


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_collects_multipart_answer_and_checks(monkeypatch):
    r, fc, clock, lines = make_runner(monkeypatch, [[(3, "часть 1", None), (6, "часть 2", None)]])
    steps = st.parse_scenario("a: привет\nexpect: часть 1.*часть 2\nexpect_not: ошибка", CONF)

    async def go():
        for s in steps:
            await r.do(s)
    run(go())
    assert r.passed == 2 and r.failed == 0
    assert "часть 2" in r.last_text
    assert clock.t >= 6 + 4          # дождался второй части и тишины settle


def test_timeout_when_bot_silent(monkeypatch):
    r, fc, clock, lines = make_runner(monkeypatch, [[]])
    steps = st.parse_scenario("timeout 10\na: эй\nexpect: .", CONF)

    async def go():
        for s in steps:
            await r.do(s)
    run(go())
    assert r.failed == 1 and r.last_text == ""
    assert 10 <= clock.t < 15


def test_reply_goes_to_last_bot_message(monkeypatch):
    r, fc, clock, lines = make_runner(monkeypatch, [[(1, "первый", None), (2, "второй", None)], [(1, "ок", None)]])
    steps = st.parse_scenario("a: привет\na reply: и тебе", CONF)

    async def go():
        for s in steps:
            await r.do(s)
    run(go())
    bot_ids = [m.id for m in fc.msgs if m.sender_id == BOT_ID and m.message == "второй"]
    assert fc.sent[1] == ("и тебе", bot_ids[0])


def test_reply_without_bot_message_stops(monkeypatch):
    r, fc, clock, lines = make_runner(monkeypatch, [])
    steps = st.parse_scenario("a reply: ну?", CONF)
    with pytest.raises(st.ScenarioError):
        run(r.do(steps[0]))


def test_nowait_send_and_silent(monkeypatch):
    r, fc, clock, lines = make_runner(monkeypatch, [[], []])
    steps = st.parse_scenario("a!: просто болтаю\nsilent 5", CONF)

    async def go():
        for s in steps:
            await r.do(s)
    run(go())
    assert r.passed == 1 and r.failed == 0 and r.last_text is None


def test_silent_fails_if_bot_talks(monkeypatch):
    r, fc, clock, lines = make_runner(monkeypatch, [[(2, "а я отвечу", None)]])
    steps = st.parse_scenario("a!: просто болтаю\nsilent 5", CONF)

    async def go():
        for s in steps:
            await r.do(s)
    run(go())
    assert r.failed == 1


def test_expect_without_action_is_error(monkeypatch):
    r, *_ = make_runner(monkeypatch, [])
    with pytest.raises(st.ScenarioError):
        run(r.do(st.parse_scenario("expect: x", CONF)[0]))


def test_click_picks_button_and_sees_edit(monkeypatch):
    yes, no = SimpleNamespace(text="Да, забыть"), SimpleNamespace(text="Нет")
    r, fc, clock, lines = make_runner(monkeypatch, [[(1, "Точно забыть?", [[yes, no]])]])
    clicked = []

    def attach_click():
        m = next(m for m in fc.msgs if m.message == "Точно забыть?")

        async def click(text=None):
            clicked.append(text)
            m.message, m.buttons = "Забыла.", None          # бот правит то же сообщение
            return SimpleNamespace(message="готово")
        m.click = click

    steps = st.parse_scenario("a: /forget\na click: да\nexpect: Забыла\nexpect: всплывашка", CONF)

    async def go():
        await r.do(steps[0])
        attach_click()
        for s in steps[1:]:
            await r.do(s)
    run(go())
    assert clicked == ["Да, забыть"]
    assert r.passed == 2 and r.failed == 0


# --------------------------------------------------------------------------- rename (тёзки, смена ника)

def test_parse_rename():
    s = st.parse_scenario("a rename: Тест", CONF)[0]
    assert (s.kind, s.who, s.arg) == ("rename", "a", "Тест")
    with pytest.raises(st.ScenarioError):
        st.parse_scenario("a rename:", CONF)
    with pytest.raises(st.ScenarioError):
        st.parse_scenario("a rename: " + "x" * (st.RENAME_MAX + 1), CONF)


def test_parse_react_and_see():
    a = st.parse_scenario("b react: bot 1 😁\nb react: 3 -\na see: bot 2\na see: 1", CONF)
    assert [(x.kind, x.who, x.arg) for x in a] == [("react", "b", "bot 1 😁"), ("react", "b", "3 -"), ("see", "a", "bot 2"), ("see", "a", "1")]
    for bad in ("a react: 😁", "a react: x 😁", "a react: 2", "a see: x", "a see: 1 2", "a react:"):
        with pytest.raises(st.ScenarioError):
            st.parse_scenario(bad, CONF)


def _rename_runner(monkeypatch, fail_on=None):
    r, fc, clock, lines = make_runner(monkeypatch, [])
    names = {"first": "Master", "last": "Osterberg"}
    calls = []

    async def get_me():
        return SimpleNamespace(first_name=names["first"], last_name=names["last"])
    fc.get_me = get_me

    async def set_name(c, first, last):
        calls.append((first, last))
        names["first"], names["last"] = first, last
    monkeypatch.setattr(r, "_set_name", set_name)
    return r, names, calls, lines


def test_rename_twice_restores_original_once(monkeypatch):
    r, names, calls, lines = _rename_runner(monkeypatch)
    steps = st.parse_scenario("a rename: Тест\na rename: Тест-новый", CONF)

    async def go():
        for s in steps:
            await r.do(s)
        await r.close()
    run(go())
    # оригинал запомнен ДО первой смены, не «Тест» после неё
    assert calls == [("Тест", ""), ("Тест-новый", ""), ("Master", "Osterberg")]
    assert names == {"first": "Master", "last": "Osterberg"}


def test_rename_restored_even_when_scenario_stops(monkeypatch):
    r, names, calls, lines = _rename_runner(monkeypatch)
    steps = st.parse_scenario("a rename: Тест\na reply: не на что", CONF)   # reply без сообщения бота → ScenarioError
    monkeypatch.setattr(st, "Runner", lambda conf, log: r)
    rc = run(st.run_steps(steps, CONF, r.log))
    assert rc == 1
    assert names == {"first": "Master", "last": "Osterberg"}
    assert any("ОСТАНОВ" in ln for ln in lines)
