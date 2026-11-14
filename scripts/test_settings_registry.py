"""Характеризационные тесты настроек чата (feat/settings-registry, docs/claude/tz-settings-registry.md).

ШАГ 1 ТЗ: записаны на НЕТРОНУТОМ коде (до рефакторинга) и обязаны проходить после него БЕЗ правок ожиданий — это и есть
доказательство «поведение не изменилось». Каждый случай — вызов настоящего хендлера команды с подменённым update/context
для сценария «кто / какой чат / тариф / аргументы»; сравниваются тексты всех ответов и строки настроек в БД с эталоном
`scripts/settings_golden.json`. Эталон правится ТОЛЬКО осознанным изменением поведения (спросить Алексея), не «подгонкой».

Без сети. Запуск из корня репозитория:
    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_settings_registry.py -q
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_orig_mkdir = Path.mkdir
Path.mkdir = lambda self, *a, **k: None if str(self).replace("\\", "/") == "/app/data" else _orig_mkdir(self, *a, **k)
import bot  # noqa: E402
import db  # noqa: E402
Path.mkdir = _orig_mkdir

GOLDEN = Path(__file__).resolve().parent / "settings_golden.json"

BOT_ADMIN = 592441          # из bot.ADMIN_IDS
CHAT_ADMIN = 222            # админ группы (не админ бота)
MEMBER = 333                # обычный участник
GROUP = -1001000000001      # текущая группа (известна боту)
FOREIGN = -1001000000002    # чужая группа, известна боту
UNKNOWN = -1001000000003    # чужая группа, боту неизвестна
PRIVATE = 333               # в личке chat_id == user_id

# --- сценарии: (тип чата, актёр, чат-админ ли актёр в группе, тариф текущего чата, тариф чужого чата) ---
SCENARIOS = {
    "priv_user_paid":   dict(chat="private", uid=MEMBER,     chat_admin=False, tier="paid"),
    "priv_user_free":   dict(chat="private", uid=MEMBER,     chat_admin=False, tier="free"),
    "priv_botadm_free": dict(chat="private", uid=BOT_ADMIN,  chat_admin=False, tier="free"),
    "priv_botadm_paid": dict(chat="private", uid=BOT_ADMIN,  chat_admin=False, tier="paid"),
    "grp_member_paid":  dict(chat="group",   uid=MEMBER,     chat_admin=False, tier="paid"),
    "grp_cadm_paid":    dict(chat="group",   uid=CHAT_ADMIN, chat_admin=True,  tier="paid"),
    "grp_botadm_paid":  dict(chat="group",   uid=BOT_ADMIN,  chat_admin=False, tier="paid"),
    "grp_member_free":  dict(chat="group",   uid=MEMBER,     chat_admin=False, tier="free"),
    "grp_cadm_free":    dict(chat="group",   uid=CHAT_ADMIN, chat_admin=True,  tier="free"),
    "grp_botadm_free":  dict(chat="group",   uid=BOT_ADMIN,  chat_admin=False, tier="free"),
}
FOREIGN_TIERS = ("paid", "free")   # тариф чужого чата для сценариев с chat_id аргументом

# --- команды и варианты аргументов: (команда, ключ/значение, [(имя варианта, args)]) ---
# chat_id в args у model/image/persona — первый аргумент, у searchmode/review_persona/activity — второй.
def _variants():
    f, u = str(FOREIGN), str(UNKNOWN)
    return [
        ("model:argon", "model", "argon", [("plain", []), ("foreign", [f]), ("unknown", [u]), ("badid", ["abc"])]),
        ("model:flash", "model", "flash", [("plain", []), ("foreign", [f])]),
        ("model:luna", "model", "luna", [("plain", []), ("foreign", [f])]),
        ("image:bananapro", "image", "bananapro", [("plain", []), ("foreign", [f]), ("unknown", [u]), ("badid", ["abc"])]),
        ("image:gpt", "image", "gpt", [("plain", []), ("foreign", [f])]),
        ("image:banana", "image", "banana", [("plain", []), ("foreign", [f])]),
        ("persona:toxic", "persona", "toxic", [("plain", []), ("foreign", [f]), ("unknown", [u]), ("badid", ["abc"])]),
        ("persona:classic", "persona", "classic", [("plain", [])]),
        ("review_persona", "review_persona", None, [
            ("show", []), ("set_toxic", ["toxic"]), ("set_upper", ["TOXIC"]), ("off", ["off"]), ("bad", ["zzz"]),
            ("foreign_set", ["toxic", f]), ("foreign_off", ["off", f]), ("unknown_set", ["toxic", u]),
            ("badid", ["toxic", "x"])]),
        ("review_on", "review_enabled", True, [("plain", [])]),
        ("review_off", "review_enabled", False, [("plain", [])]),
        ("searchmode", "search_mode", None, [
            ("show", []), ("set_gpt", ["gpt"]), ("set_upper", ["TAVILY"]), ("bad", ["zzz"]),
            ("foreign_set", ["gpt", f]), ("unknown_set", ["gpt", u]), ("badid", ["gpt", "x"])]),
        ("activity", "activity", None, [
            ("show", []), ("set_50", ["50"]), ("set_0", ["0"]), ("set_100", ["100"]), ("range", ["101"]),
            ("neg", ["-1"]), ("nan", ["abc"]), ("foreign_set", ["30", f]), ("unknown_set", ["30", u]),
            ("badid", ["30", "x"])]),
        ("autoignore", "autoignore", None, [("show", []), ("on", ["on"]), ("off", ["off"]), ("bogus", ["bogus"])]),
    ]


def _probe_modes(cmd: str, variant: str):
    return ("ok", "fail") if cmd.startswith("model:") and cmd != "model:luna" and variant == "plain" else ("ok",)


def all_cases():
    out = []
    for cmd, kind, value, variants in _variants():
        for vname, args in variants:
            for sname in SCENARIOS:
                foreign_arg = any(a.lstrip("-").isdigit() and len(a) > 6 for a in args)
                for ftier in (FOREIGN_TIERS if foreign_arg else ("paid",)):
                    for probe in _probe_modes(cmd, vname):
                        cid = f"{cmd}|{vname}|{sname}" + (f"|foreign_{ftier}" if foreign_arg else "") + \
                              ("|probe_fail" if probe == "fail" else "")
                        out.append(dict(id=cid, cmd=cmd, kind=kind, value=value, args=list(args), scenario=sname,
                                        foreign_tier=ftier, probe=probe))
    return out


# --- окружение ---
_TEMPLATE: dict = {}


def _template_db() -> Path:
    if "p" not in _TEMPLATE:
        tmp = Path(tempfile.mkdtemp(prefix="settings_golden_"))
        old = db.DB_PATH
        db.DB_PATH = tmp / "template.db"
        db.init_db()
        c = db.get_conn()
        for cid, name in ((GROUP, "Моя группа"), (FOREIGN, "Чужая группа")):
            c.execute("INSERT INTO allowed_chats (chat_id, name, status) VALUES (?, ?, 'approved')", (cid, name))
        c.commit()
        c.close()
        db.DB_PATH = old
        _TEMPLATE["p"] = tmp / "template.db"
    return _TEMPLATE["p"]


class FakeMsg:
    def __init__(self, sink):
        self.sink = sink
        self.reply_to_message = None

    async def reply_text(self, text, **kw):
        self.sink.append(text)


def _state(chat_ids) -> dict:
    """Строки настроек (сырые) для интересующих чатов."""
    c = db.get_conn()
    out = {}
    try:
        for table, cols in (("chat_models", "model"), ("chat_image_provider", "provider"), ("chat_persona", "persona"),
                            ("chat_review_persona", "persona"), ("chat_search_mode", "mode"),
                            ("allowed_chats", "daily_review_enabled, activity_percent, autoignore_enabled")):
            for cid in chat_ids:
                row = c.execute(f"SELECT {cols} FROM {table} WHERE chat_id = ?", (cid,)).fetchone()
                if row is not None:   # только существующие строки (эталон компактнее)
                    out[f"{table}:{cid}"] = list(tuple(row))
    finally:
        c.close()
    return out


def run_case(case: dict, tmp_path: Path, monkeypatch) -> dict:
    sc = SCENARIOS[case["scenario"]]
    cur_chat = GROUP if sc["chat"] == "group" else PRIVATE
    path = tmp_path / "t.db"
    shutil.copy(_template_db(), path)
    monkeypatch.setattr(db, "DB_PATH", path)
    tiers = {cur_chat: sc["tier"], FOREIGN: case["foreign_tier"], UNKNOWN: case["foreign_tier"]}
    monkeypatch.setattr(bot, "chat_tier", lambda cid: tiers.get(cid, "paid"))
    monkeypatch.setattr(bot, "free_persona", lambda cid: "classic")   # режим дня зависит от даты
    monkeypatch.setattr(bot, "SEARCH_PROVIDER_FREE", "tavily")        # зависит от окружения (.env)
    probes = []

    async def fake_probe(context, chat_id, model_id):
        probes.append([chat_id, model_id])
        if case["probe"] == "fail":
            raise RuntimeError("boom")

    monkeypatch.setattr(bot, "_probe_model", fake_probe)

    async def get_member(chat_id, user_id):
        return SimpleNamespace(status="administrator" if (sc["chat_admin"] and user_id == sc["uid"]) else "member")

    sink = []
    msg = FakeMsg(sink)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=sc["uid"], username="u", full_name="U", first_name="U"),
        effective_chat=SimpleNamespace(id=cur_chat, type="supergroup" if sc["chat"] == "group" else "private"),
        effective_message=msg, message=msg)
    context = SimpleNamespace(args=list(case["args"]), bot=SimpleNamespace(get_chat_member=get_member))

    kind = case["kind"]
    if kind == "model":
        coro = bot._set_chat_model(update, context, case["value"])
    elif kind == "image":
        coro = bot._set_image_provider(update, context, case["value"])
    elif kind == "persona":
        coro = bot._set_persona(update, context, case["value"])
    elif kind == "review_persona":
        coro = bot.cmd_review_persona(update, context)
    elif kind == "review_enabled":
        coro = bot._set_chat_review_enabled(update, context, case["value"])
    elif kind == "search_mode":
        coro = bot.cmd_searchmode(update, context)
    elif kind == "activity":
        coro = bot.cmd_activity(update, context)
    elif kind == "autoignore":
        coro = bot.cmd_autoignore(update, context)
    else:
        raise AssertionError(kind)
    asyncio.run(coro)
    return {"replies": sink, "probes": probes, "state": _state([cur_chat, FOREIGN, UNKNOWN])}


def record(path: Path = GOLDEN):
    """ОДНОРАЗОВО, на нетронутом коде: записать эталон. После рефакторинга НЕ перезаписывать."""
    import pytest as _pt
    mp = _pt.MonkeyPatch()
    result = {}
    for case in all_cases():
        tmp = Path(tempfile.mkdtemp(prefix="settings_rec_"))
        try:
            result[case["id"]] = run_case(case, tmp, mp)
        finally:
            mp.undo()
            shutil.rmtree(tmp, ignore_errors=True)
    path.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    return len(result)


CASES = all_cases()


@pytest.fixture(scope="module")
def golden():
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_characterization(case, golden, tmp_path, monkeypatch):
    got = run_case(case, tmp_path, monkeypatch)
    assert case["id"] in golden, "нет эталона для случая — эталон пишется один раз на нетронутом коде"
    assert got == golden[case["id"]]


def test_golden_has_no_stale_cases(golden):
    assert set(golden) == {c["id"] for c in CASES}


if __name__ == "__main__":
    print("recorded cases:", record())
