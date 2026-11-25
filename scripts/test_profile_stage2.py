"""Профиль, вторая очередь (feat/profile-stage2, docs/claude/tz-profile-stage2.md): дедуп сверяет факты с профилем,
извлечение не дублирует профиль. Без сети: модель — фейк, БД временная. Имён и данных людей здесь нет — только вымышленные.

    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python3 -m pytest scripts/test_profile_stage2.py -q
"""
import json
import os
import sys
import time
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
import dedup_memory as dm  # noqa: E402
Path.mkdir = _orig_mkdir

ADMIN, U1, U2 = 900, 111, 222
CHAT = -1001
NOW = int(time.time())
OLD = NOW - 10 * 86400

# SYSTEM_PROMPT до этой задачи — дословно: группы без профиля обязаны уходить с ним байт в байт.
OLD_SYSTEM_PROMPT = (
    "Тебе дан список фактов об одном человеке — часть из них дубли или "
    "перефразировки одного и того же. Объедини по смыслу, убери повторы, "
    "оставь МИНИМАЛЬНЫЙ набор уникальных фактов, каждый — короткая фраза "
    "как в источнике. Не выдумывай ничего нового, не добавляй факты, "
    "которых не было. Верни ТОЛЬКО JSON: {\"facts\": [\"...\", \"...\"]}"
)


def resp(facts, dropped=None, out_tok=50, stop="end_turn"):
    body = {"facts": facts}
    if dropped is not None:
        body["dropped_by_profile"] = dropped
    return SimpleNamespace(stop_reason=stop, model="gemini-3.8-flash",
                           content=[SimpleNamespace(type="text", text=json.dumps(body, ensure_ascii=False))],
                           usage=SimpleNamespace(input_tokens=100, output_tokens=out_tok, cache_creation_input_tokens=0,
                                                 cache_read_input_tokens=0, cache_creation=None))


def set_profile(uid, fields, context="private", chat_id=0, at=None):
    conn = db.get_conn()
    for f, v in fields.items():
        conn.execute("INSERT OR REPLACE INTO user_profile (user_id, context, chat_id, field, value, source, updated_at, updated_by) "
                     "VALUES (?,?,?,?,?,?,?,?)", (uid, context, chat_id, f, v, "admin", at or NOW, ADMIN))
    conn.commit()
    conn.close()


def ins_facts(uid, facts, ctx="private", chat_id=None, tier="long", created=OLD):
    conn = db.get_conn()
    for f in facts:
        conn.execute("INSERT INTO memory (user_id, fact, context, chat_id, created_at, tier) VALUES (?,?,?,?,?,?)",
                     (uid, f, ctx, chat_id, created, tier))
    conn.commit()
    conn.close()


def numbered(n, prefix="факт"):
    return [f"{prefix} {i}" for i in range(n)]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    for uid in (ADMIN, U1, U2):
        db.get_or_create_user(uid, full_name=f"U{uid}")
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(dm, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(dm, "DATA_DIR", d)
    monkeypatch.setattr(dm, "BACKUPS_DIR", d / "backups")
    monkeypatch.setattr(dm, "RUNS_DIR", d / "dedup_runs")
    monkeypatch.setattr(dm, "CALIBRATION_PATH", d / "dedup_calibration.json")
    monkeypatch.setattr(bot, "DEDUP_AUTO_FILE", d / "dedup_auto.json")
    monkeypatch.setattr(bot, "is_admin", lambda uid: uid == ADMIN)
    monkeypatch.setattr(bot, "ADMIN_IDS", {ADMIN})
    e = SimpleNamespace(calls=[], answer=lambda facts: (facts, []), ev=SimpleNamespace(is_set=lambda: False))

    def fake_sync_create(**kw):
        payload = json.loads(kw["messages"][0]["content"])
        facts = payload["facts"] if isinstance(payload, dict) else payload
        e.calls.append({"system": kw["system"], "payload": payload, "raw": kw["messages"][0]["content"], "label": kw.get("label")})
        out, dropped = e.answer(facts)
        return resp(out, dropped if isinstance(payload, dict) else None)
    monkeypatch.setattr(bot, "sync_create", fake_sync_create)
    monkeypatch.setattr(dm, "_native_create", lambda **kw: pytest.fail("_native_create вызван"))
    return e


def go(e, budget=2.0):
    return bot._dedup_auto_run_sync(budget, e.ev)


# ------------------------------------------------------------------ load_profiles

def test_load_profiles_group_private_and_pending(env):
    set_profile(U1, {"city": "Гамбург", "name": "Дима"})                        # личка, chat_id=0
    set_profile(U1, {"city": "Бремен"}, context="group", chat_id=CHAT)
    conn = db.get_conn()
    conn.execute("INSERT INTO profile_proposals (user_id, context, chat_id, field, new_value, evidence, created_at, status) "
                 "VALUES (?,?,?,?,?,?,?,?)", (U2, "group", CHAT, "city", "Кёльн", "", NOW, "pending"))
    conn.commit()
    pr = dm.load_profiles(conn)
    conn.close()
    assert pr[(U1, "private", 0)] == "Имя: Дима; Город: Гамбург"                # порядок и подписи как у `_profile_text`
    assert pr[(U1, "group", CHAT)] == "Город: Бремен"
    assert not any(k[0] == U2 for k in pr)                                       # pending не берём


def test_load_profiles_empty_value_and_no_table(env):
    set_profile(U1, {"city": "   "})
    conn = db.get_conn()
    assert dm.load_profiles(conn) == {}
    conn.execute("DROP TABLE user_profile")
    assert dm.load_profiles(conn) == {}
    conn.close()


def test_load_profiles_trimmed_by_whole_fields(env, monkeypatch):
    monkeypatch.setattr(db, "PROFILE_TOTAL_MAX", 40)
    set_profile(U1, {"name": "Дима", "city": "Гамбург", "profession": "x" * 60})
    conn = db.get_conn()
    pr = dm.load_profiles(conn)
    conn.close()
    assert pr[(U1, "private", 0)] == "Имя: Дима; Город: Гамбург"                 # профессия не влезла — целиком отброшена


def test_group_gets_profile_private_uses_chat_zero(env):
    ins_facts(U1, numbered(12))                                                  # личка: chat_id NULL в memory
    ins_facts(U1, numbered(12, "г"), ctx="group", chat_id=CHAT)
    ins_facts(U2, numbered(12, "х"))
    set_profile(U1, {"city": "Гамбург"})
    conn = db.get_conn()
    groups = {(g.key.user_id, g.key.context): g for g in dm.load_groups(conn)}
    conn.close()
    assert groups[(U1, "private")].profile == "Город: Гамбург"
    assert groups[(U1, "group")].profile == ""                                   # профиль группы — отдельный, его нет
    assert groups[(U2, "private")].profile == ""


# ------------------------------------------------------------------ запрос

class Cap:
    def __init__(self, answer):
        self.calls = []
        self.answer = answer
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        payload = json.loads(kw["messages"][0]["content"])
        facts = payload["facts"] if isinstance(payload, dict) else payload
        return self.answer(facts, isinstance(payload, dict))


def _group(facts, profile=""):
    k = dm.GroupKey(U1, "private", None, "long")
    return dm.Group(k, list(range(len(facts))), list(facts), None, profile)


def test_request_without_profile_is_byte_identical():
    assert dm.SYSTEM_PROMPT == OLD_SYSTEM_PROMPT
    facts = ["Любит котов", "Обожает котов"]
    cap = Cap(lambda f, prof: resp(f[:1]))
    [r] = list(dm.run_llm([_group(facts)], "gemini-3.8-flash", cap, None, None, {"no_fallback": True}))
    [c] = cap.calls
    assert c["system"] == OLD_SYSTEM_PROMPT
    assert c["messages"][0]["content"] == json.dumps(facts, ensure_ascii=False)  # голый список, как раньше
    assert r.dropped_by_profile == []


def test_request_with_profile_object_and_system():
    facts = ["Живёт в Москве", "Любит котов"]
    cap = Cap(lambda f, prof: resp(f[1:], ["Живёт в Москве"]))
    [r] = list(dm.run_llm([_group(facts, "Город: Гамбург")], "gemini-3.8-flash", cap, None, None, {"no_fallback": True}))
    [c] = cap.calls
    assert c["system"] == dm.SYSTEM_PROMPT_PROFILE != dm.SYSTEM_PROMPT
    assert json.loads(c["messages"][0]["content"]) == {"profile": "Город: Гамбург", "facts": facts}
    assert "dropped_by_profile" in dm.SYSTEM_PROMPT_PROFILE and "ВЕРНЫ" in dm.SYSTEM_PROMPT_PROFILE
    assert r.facts_after == ["Любит котов"] and r.dropped_by_profile == ["Живёт в Москве"]


def test_dropped_only_verbatim_and_only_if_really_removed():
    facts = ["Живёт в Москве", "Работает врачом", "Любит котов", "Любит собак"]
    # «Живёт в москве» — не дословно; «Любит котов» заявлен, но остался в итоге; «Работает врачом» — честный
    cap = Cap(lambda f, prof: resp(["Любит котов", "Любит собак"], ["Живёт в москве", "Работает врачом", "Любит котов", "Выдумка"]))
    [r] = list(dm.run_llm([_group(facts, "Профессия: врач")], "gemini-3.8-flash", cap, None, None, {"no_fallback": True}))
    assert r.dropped_by_profile == ["Работает врачом"]


def test_dropped_missing_key_is_empty():
    cap = Cap(lambda f, prof: resp(f[:1]))                                       # ответ без dropped_by_profile
    [r] = list(dm.run_llm([_group(["a", "b"], "Город: Гамбург")], "gemini-3.8-flash", cap, None, None, {"no_fallback": True}))
    assert r.dropped_by_profile == [] and r.facts_after == ["a"]


def test_profile_in_every_chunk_and_in_merge():
    facts = [f"Факт номер {i} " + "я" * 90 for i in range(450)]                  # заведомо больше одного ответа → куски + слияние
    assert len(dm.plan_chunks(facts)) > 1
    cap = Cap(lambda f, prof: resp(f[: max(1, len(f) // 3)], []))
    [r] = list(dm.run_llm([_group(facts, "Город: Гамбург")], "gemini-3.8-flash", cap, None, None, {"no_fallback": True}))
    assert r.status == "ok" and r.chunks > 1 and len(cap.calls) > r.chunks       # были куски И слияние
    for c in cap.calls:
        assert c["system"] == dm.SYSTEM_PROMPT_PROFILE
        assert json.loads(c["messages"][0]["content"])["profile"] == "Город: Гамбург"


# ------------------------------------------------------------------ оценка

def test_estimate_unchanged_without_profile_and_larger_with():
    facts = numbered(100)
    base = dm.estimate_group(facts, "gemini-3.8-flash")
    assert dm.estimate_group(facts, "gemini-3.8-flash", None, "") == base       # калибровка и оценка без профиля — как были
    prof = "Город: Гамбург; " * 20
    more = dm.estimate_group(facts, "gemini-3.8-flash", None, prof)
    assert more.in_tok > base.in_tok + len(prof) / dm.CHARS_PER_TOKEN - 1
    tot = dm.estimate_total([facts, facts], "gemini-3.8-flash", None, ["", prof])
    assert tot.in_tok == pytest.approx(base.in_tok + more.in_tok)


# ------------------------------------------------------------------ автодедуп: страховка, отчёт, файл прогона

SECRET = "Секретный личный факт про переезд"


def test_report_has_numbers_only_and_run_file_has_facts(env):
    facts = [SECRET] + numbered(19)
    ins_facts(U1, facts)
    set_profile(U1, {"city": "Гамбург"})
    env.answer = lambda f: ([x for x in f if x != SECRET], [SECRET])
    st = go(env)
    assert st["applied"] == 1 and st["profile_dropped"] == 1
    text = bot._dedup_auto_report_text(st, "Итог", 0.1, 1)
    assert "По профилю удалено фактов: 1" in text and SECRET not in text
    runs = list(dm.RUNS_DIR.glob("*.json"))
    assert len(runs) == 1 and SECRET in runs[0].read_text(encoding="utf-8")      # сами факты — только в файле прогона
    assert json.loads(runs[0].read_text(encoding="utf-8"))["groups"][0]["dropped_by_profile"] == [SECRET]


def test_profile_drop_over_30_percent_not_applied(env):
    ins_facts(U1, numbered(20))
    set_profile(U1, {"city": "Гамбург"})
    env.answer = lambda f: (f[7:], f[:7])                                        # 7 из 20 = 35% > 30%
    st = go(env)
    assert st["applied"] == 0 and len(st["profile_guard"]) == 1
    conn = db.get_conn()
    assert conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0] == 20       # база не тронута
    conn.close()
    text = bot._dedup_auto_report_text(st, "Итог", 0.1, 1)
    assert "Подозрительно много удалений по профилю" in text and "фактов 20, по профилю 7" in text
    assert bot.DEDUP_PROFILE_MAX_DROP == 0.3


def test_profile_drop_exactly_30_percent_applied(env):
    ins_facts(U1, numbered(20))
    set_profile(U1, {"city": "Гамбург"})
    env.answer = lambda f: (f[6:], f[:6])                                        # 6 из 20 = 30% — граница допустима
    st = go(env)
    assert st["applied"] == 1 and st["profile_dropped"] == 6


def test_group_without_profile_request_unchanged_in_bot(env):
    ins_facts(U1, numbered(20))
    env.answer = lambda f: (f[:10], None)
    go(env)
    [c] = env.calls
    assert isinstance(c["payload"], list) and c["system"] == dm.SYSTEM_PROMPT


def test_estimate_text_marks_profile_trigger(env):
    ins_facts(U1, numbered(20))
    set_profile(U1, {"city": "Гамбург"}, at=OLD - 100)
    env.answer = lambda f: (f, [])
    go(env)                                                                      # отметка поставлена сейчас
    set_profile(U1, {"city": "Берлин"}, at=int(time.time()) + 5)                 # профиль поправили ПОСЛЕ проверки
    plan = bot._dedup_auto_plan()
    assert len(plan) == 1 and plan[0]["new"] == 0 and plan[0]["group"].profile == "Город: Берлин"
    assert "профиль изменён" in bot._dedup_auto_estimate_text(plan, "Оценка")


# ------------------------------------------------------------------ отбор: профиль изменился после проверки

def _due():
    return [k for k, _n, _f in db.dedup_due_groups(10, 5, 0.2)]


def test_profile_change_after_check_makes_group_due(env):
    ins_facts(U1, numbered(12))
    db.dedup_state_put(U1, "private", None, "long", NOW, 12, 12, "unchanged", 0.0)
    assert _due() == []                                                          # новых нет, профиля нет
    set_profile(U1, {"city": "Гамбург"}, at=NOW - 1000)                          # профиль СТАРШЕ проверки — не повод
    assert _due() == []
    set_profile(U1, {"city": "Берлин"}, at=NOW + 5)                              # поправили после проверки — повод
    assert _due() == [(U1, "private", None, "long")]


def test_profile_change_does_not_resurrect_small_or_expired_groups(env):
    ins_facts(U1, numbered(9))                                                   # меньше DEDUP_AUTO_MIN_FACTS
    db.dedup_state_put(U1, "private", None, "long", NOW, 9, 9, "unchanged", 0.0)
    set_profile(U1, {"city": "Гамбург"}, at=NOW + 5)
    assert _due() == []


def test_checked_after_profile_change_is_quiet_again(env):
    ins_facts(U1, numbered(12))
    set_profile(U1, {"city": "Гамбург"}, at=NOW - 10)
    db.dedup_state_put(U1, "private", None, "long", NOW, 12, 12, "unchanged", 0.0)
    assert _due() == []


# ------------------------------------------------------------------ извлечение: правило в промптах

class Cap2:
    def __init__(self, monkeypatch, answer):
        self.calls = []
        self.answer = answer
        monkeypatch.setattr(bot, "sync_create", self)

    def __call__(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(self.answer, ensure_ascii=False))],
                               stop_reason="end_turn")


def test_rule_text_in_profile_rules():
    r = bot._profile_rules()
    assert bot.PROFILE_NO_DUP_RULE in r and "Не записывай в facts" in r and "profile_updates" in r
    assert len(bot.PROFILE_NO_DUP_RULE) < 400                                    # ~250 знаков, лимит размера


def test_private_extraction_prompt_has_rule(env, monkeypatch):
    m = Cap2(monkeypatch, {"facts": [], "profile_updates": []})
    bot._extract_private_raw(U1, [{"role": "user", "content": "я из Гамбурга"}])
    assert bot.PROFILE_NO_DUP_RULE in m.calls[0]["system"]


def test_backfill_uses_same_prompt_with_rule(env, monkeypatch):
    m = Cap2(monkeypatch, {"facts": [], "profile_updates": []})
    bot._extract_private_raw(U1, [{"role": "user", "content": "привет"}], "memory_backfill")
    assert m.calls[0]["label"] == "memory_backfill" and bot.PROFILE_NO_DUP_RULE in m.calls[0]["system"]


def test_group_extraction_prompt_has_rule(env, monkeypatch):
    db.save_group_message(CHAT, U1, "Аня", "я из Гамбурга")
    m = Cap2(monkeypatch, {"participants": []})
    bot.extract_all_participants_memory(CHAT)
    assert m.calls and bot.PROFILE_NO_DUP_RULE in m.calls[0]["system"]
