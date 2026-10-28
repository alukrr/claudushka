"""Интерактивный curses-интерфейс для dedup_memory.py (в стиле htop). Только stdlib.

Запуск: docker exec -it claudushka python3 /repo/dedup_memory.py   (или --tui)
Вся логика (прогон, оценка, применение) — в dedup_memory.py; здесь только экран.
curses трогает ТОЛЬКО главный поток; API-вызовы идут в рабочем потоке, связь — queue.Queue.
"""
import collections
import curses
import json
import locale
import logging
import os
import queue
import sqlite3
import textwrap
import threading
import time
from dataclasses import dataclass

import dedup_memory as dm

MIN_H, MIN_W = 24, 80

# Раскладка ЙЦУКЕН -> QWERTY только для командных клавиш (не для ввода текста в поиск).
_RU = "йцукенгшщзхъфывапролджэячсмитьбю"
_EN = "qwertyuiop[]asdfghjkl;'zxcvbnm,."
RU2EN = {**dict(zip(_RU, _EN)), **dict(zip(_RU.upper(), _EN.upper()))}

K_ENTER, K_ESC, K_BS = "ENTER", "ESC", "BS"
EDIT_MARK = "✎"  # метка «правил человек»; если глиф ломает ширину в терминале — заменить на "*"


def fnum(n) -> str:
    return f"{int(n):,}".replace(",", " ")


def fcost(c, approx=True) -> str:
    return "$ ?" if c is None else f"{'~' if approx else ''}${c:.2f}"


def fdur(sec) -> str:
    if sec is None:
        return "?"
    m = sec / 60
    return f"~{sec:.0f} с" if m < 1 else (f"~{m:.0f} мин" if m < 120 else f"~{m / 60:.1f} ч")


def clip(s: str, w: int) -> str:
    return s if len(s) <= w else (s[:max(0, w - 1)] + "…" if w > 0 else "")


def meter(cur, total, width=20) -> str:
    fill = 0 if not total else min(width, round(width * cur / total))
    return "[" + "|" * fill + " " * (width - fill) + "]"


@dataclass
class Node:
    key: tuple
    label: str
    ident: str
    kind: str
    groups: list


@dataclass
class Row:
    kind: str          # chat | group
    node: Node
    group: object = None
    children: list = None


class App:
    def __init__(self, scr, model_alias="lite"):
        self.scr = scr
        self.model = dm.MODEL_ALIASES.get(model_alias, model_alias)
        self.screen = "main"
        self.modal = None
        self.dirty = True
        # данные
        self.groups, self.nodes, self.chats, self.users = [], {}, {}, {}
        self.snapshot_at = ""
        self.expired_n = 0
        self.prev_trunc = {}
        self.calibs = dm.load_calibration()
        self.est_cache = {}
        # состояние списка
        self.selected, self.expanded = set(), set()
        self.tier_f, self.ctx_f, self.sort = None, None, "facts"
        self.search, self.matches, self.match_i = "", [], -1
        self.limit = 0
        self.cur, self.top = 0, 0
        self.rows = []
        # прогон / результаты
        self.results: dict = {}       # key -> GroupResult (ещё не применённые)
        self.included: set = set()    # ключи, включённые в применение
        self.applied_keys: set = set()
        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.run_groups, self.run_results = [], []
        self.run_model = self.model
        self.run_started = self.run_ended = 0.0
        self.run_stopping = False
        self.run_error = None
        self.log_off = 0
        self.res_cur = self.res_top = 0
        self.vw = {}
        self.help_off = 0
        self.undo = {}            # key -> стек снимков «стало» (отмена правок группы)
        self.lined = None         # строчный редактор
        self.cursor_pos = None
        self.prefs_path = dm.DATA_DIR / "dedup_tui.json"
        try:
            self.prefs = json.loads(self.prefs_path.read_text(encoding="utf-8"))
            if not isinstance(self.prefs, dict):
                self.prefs = {}
        except (OSError, ValueError):
            self.prefs = {}
        self.pop_cur = 0
        self.applying = False
        self.status_msg = ""
        self.init_colors()
        self.load_data()

    # ------------------------------------------------------------ цвета
    def init_colors(self):
        a = {}
        self.color = False
        try:
            if curses.has_colors():
                curses.start_color()
                try:
                    curses.use_default_colors()
                    bg = -1
                except curses.error:
                    bg = curses.COLOR_BLACK
                for i, (fg, b) in enumerate([
                        (curses.COLOR_BLACK, curses.COLOR_CYAN), (curses.COLOR_BLACK, curses.COLOR_GREEN),
                        (curses.COLOR_WHITE, curses.COLOR_RED), (curses.COLOR_YELLOW, bg),
                        (curses.COLOR_GREEN, bg), (curses.COLOR_RED, bg), (curses.COLOR_CYAN, bg),
                        (curses.COLOR_BLACK, curses.COLOR_YELLOW)], 1):
                    curses.init_pair(i, fg, b)
                self.color = True
        except curses.error:
            self.color = False
        if self.color:
            a.update(sel=curses.color_pair(1), flabel=curses.color_pair(1), colhdr=curses.color_pair(2),
                     dry=curses.color_pair(2) | curses.A_BOLD, apply=curses.color_pair(3) | curses.A_BOLD,
                     warn=curses.color_pair(4) | curses.A_BOLD, ok=curses.color_pair(5),
                     bad=curses.color_pair(6) | curses.A_BOLD, hl=curses.color_pair(8),
                     title=curses.color_pair(7) | curses.A_BOLD)
        else:
            a.update(sel=curses.A_REVERSE, flabel=curses.A_REVERSE, colhdr=curses.A_REVERSE,
                     dry=curses.A_REVERSE, apply=curses.A_REVERSE | curses.A_BOLD, warn=curses.A_BOLD,
                     ok=0, bad=curses.A_BOLD, hl=curses.A_UNDERLINE, title=curses.A_BOLD)
        self.A = a

    # ------------------------------------------------------------ данные
    def load_data(self):
        conn, tmp = dm.get_conn(writable=False)
        try:
            self.groups = dm.load_groups(conn)
            self.chats, self.users = dm.load_directory(conn)
            self.expired_n = dm.count_expired(conn)
        finally:
            conn.close()
            if tmp:
                os.remove(tmp)
        self.snapshot_at = time.strftime("%H:%M:%S")
        self.refresh_prev_trunc()
        self.nodes = {}
        for g in self.groups:
            k = g.key
            nk = ("group", k.chat_id) if k.context == "group" else ("private", k.user_id)
            if nk not in self.nodes:
                if k.context == "group":
                    label, ident, kind = self.chats.get(k.chat_id) or str(k.chat_id), str(k.chat_id), "group"
                else:
                    label, ident, kind = self.users.get(k.user_id) or str(k.user_id), str(k.user_id), "личка"
                self.nodes[nk] = Node(nk, label, ident, kind, [])
            self.nodes[nk].groups.append(g)
        keys = {g.key for g in self.groups}
        self.selected &= keys
        self.est_cache.clear()
        self.rebuild()

    def refresh_prev_trunc(self):
        """Группы, которые в прошлый раз обрезало при тех же данных (по журналу прогонов)."""
        self.prev_trunc = dm.previously_truncated(self.groups, self.calibs.get(self.model))

    def user_label(self, uid):
        return self.users.get(uid) or str(uid)

    def est(self, g, model=None):
        model = model or self.model
        k = (g.key, model)
        if k not in self.est_cache:
            self.est_cache[k] = dm.estimate_group(g.facts, model, self.calibs.get(model))
        return self.est_cache[k]

    def sel_groups(self):
        gs = [g for g in self.groups if g.key in self.selected]
        return gs[:self.limit] if self.limit > 0 else gs

    def est_total(self, groups, model=None):
        model = model or self.model
        return dm.estimate_total([g.facts for g in groups], model, self.calibs.get(model))

    # ------------------------------------------------------------ список
    def pass_filter(self, g):
        if self.tier_f and g.key.tier != self.tier_f:
            return False
        return not (self.ctx_f and g.key.context != self.ctx_f)

    def group_sort_val(self, g):
        if self.sort == "cost":
            return -(self.est(g).cost or 0)
        if self.sort == "name":
            return (self.user_label(g.key.user_id).lower(), g.key.tier)
        return -len(g.facts)

    def rebuild(self):
        keep = self.rows[self.cur] if self.rows and self.cur < len(self.rows) else None
        vis = []
        for n in self.nodes.values():
            ch = sorted((g for g in n.groups if self.pass_filter(g)), key=self.group_sort_val)
            if ch:
                vis.append((n, ch))
        if self.sort == "name":
            vis.sort(key=lambda t: t[0].label.lower())
        elif self.sort == "cost":
            vis.sort(key=lambda t: -sum(self.est(g).cost or 0 for g in t[1]))
        else:
            vis.sort(key=lambda t: -sum(len(g.facts) for g in t[1]))
        rows = []
        for n, ch in vis:
            rows.append(Row("chat", n, None, ch))
            if n.key in self.expanded:
                rows += [Row("group", n, g) for g in ch]
        self.rows = rows
        if keep:
            for i, r in enumerate(rows):
                if r.kind == keep.kind and r.node is keep.node and r.group is keep.group:
                    self.cur = i
                    break
        self.cur = max(0, min(self.cur, len(rows) - 1))
        self.compute_matches()
        self.dirty = True

    def row_text(self, r):
        if r.kind == "chat":
            extra = " ".join(self.user_label(g.key.user_id) for g in r.node.groups) if r.node.kind == "group" else ""
            return f"{r.node.label} {r.node.ident} {extra}".lower()
        return f"{self.user_label(r.group.key.user_id)} {r.group.key.user_id} {r.group.key.tier}".lower()

    def compute_matches(self):
        s = self.search.lower()
        self.matches = [i for i, r in enumerate(self.rows) if s and s in self.row_text(r)] if s else []

    def row_groups(self, r):
        return r.children if r.kind == "chat" else [r.group]

    def sel_state(self, r):
        gs = self.row_groups(r)
        n = sum(1 for g in gs if g.key in self.selected)
        return "x" if n == len(gs) else ("~" if n else " ")

    def toggle(self, r):
        gs = self.row_groups(r)
        if all(g.key in self.selected for g in gs):
            self.selected -= {g.key for g in gs}
        else:
            self.selected |= {g.key for g in gs}

    def visible_groups(self):
        return [g for r in self.rows if r.kind == "chat" for g in r.children]

    def group_status(self, g):
        r = self.results.get(g.key)
        if self.screen == "run" and self.worker and g in self.run_groups and g.key not in {x.key for x in self.run_results}:
            return "…", 0
        if r:
            mark = EDIT_MARK if r.edited else ""
            cnt = f"{len(r.facts_before)}→{len(r.facts_after)}"
            if r.status in ("ok", "manual"):
                if not r.applicable:
                    return f"{mark}{'пусто' if r.status == 'ok' else 'без изм.'}", self.A["warn"]
                return f"{mark}{'ok' if r.status == 'ok' else 'ручн'} {cnt}{' ÷' + str(r.chunks) if r.chunks else ''}", self.A["ok"]
            return {"truncated": ("обрезано", self.A["bad"]), "error": ("ошибка", self.A["bad"]),
                    "empty": ("пусто", self.A["warn"])}[r.status]
        if g.key in self.applied_keys:
            return "применено", self.A["ok"]
        if g.key in self.prev_trunc:
            return "обрез.ранее", self.A["warn"]
        return "—", 0

    # ------------------------------------------------------------ рисование
    def put(self, y, x, s, attr=0):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w or x < 0:
            return
        try:
            self.scr.addstr(y, x, s[:w - x], attr)
        except curses.error:
            pass  # запись в правый нижний угол кидает ошибку уже после отрисовки символа

    def fill(self, y, attr, ch=" "):
        _, w = self.scr.getmaxyx()
        self.put(y, 0, ch * w, attr)

    def draw(self):
        h, w = self.scr.getmaxyx()
        self.scr.erase()
        if h < MIN_H or w < MIN_W:
            self.put(0, 0, f"Увеличь окно терминала: сейчас {w}x{h}, нужно минимум {MIN_W}x{MIN_H}.", self.A["warn"])
            self.put(2, 0, "F10 / q — выход")
        else:
            getattr(self, "draw_" + self.screen)(h, w)
            if self.modal:
                self.draw_modal(h, w)
        try:
            curses.curs_set(1 if (self.lined and self.cursor_pos and self.screen in ("view", "profiles")) else 0)
        except curses.error:
            pass
        if self.lined and self.cursor_pos and self.screen in ("view", "profiles") and not self.modal:
            try:
                self.scr.move(*self.cursor_pos)
            except curses.error:
                pass
        self.scr.noutrefresh()
        curses.doupdate()
        self.dirty = False

    def fbar(self, h, w, items):
        self.fill(h - 1, self.A["flabel"])
        x = 0
        for k, label in items:
            self.put(h - 1, x, k, 0)
            self.put(h - 1, x + len(k), label + " ", self.A["flabel"])
            x += len(k) + len(label) + 1
            if x >= w:
                break

    def short_model(self):
        return next((a for a, m in dm.MODEL_ALIASES.items() if m == self.model), self.model)

    def hidden_selected(self, groups=None):
        """Выбранные группы, которых не видно из-за tier/context-фильтра."""
        vis = {g.key for g in self.visible_groups()}
        return [g for g in (self.groups if groups is None else groups) if g.key in self.selected and g.key not in vis]

    def draw_header(self, h, w):
        tot_facts = sum(len(g.facts) for g in self.groups)
        sg = [g for g in self.groups if g.key in self.selected]
        sel_facts = sum(len(g.facts) for g in sg)
        self.put(0, 1, "Факты ", self.A["title"])
        self.put(0, 7, meter(sel_facts, tot_facts))
        self.put(0, 8, "|" * round(20 * sel_facts / tot_facts) if tot_facts else "", self.A["ok"])
        self.put(0, 30, f"{fnum(sel_facts)} / {fnum(tot_facts)}   Группы 2+: {len(self.groups)}")
        sel_txt = f"Выбрано: {len(sg)} гр. · {fnum(sel_facts)} фактов"
        self.put(1, 1, sel_txt)
        hid = self.hidden_selected(sg)
        if hid:
            self.put(1, 2 + len(sel_txt), f"(скрыто фильтром: {len(hid)} гр.)", self.A["warn"])
        nman = sum(1 for r in self.result_list() if r.status == "manual")
        if nman:
            self.put(1, w - 22, f"ручных правок: {nman} гр.", self.A["ok"])
        # Оценка — по ВСЕМУ выбранному (как и прогон), не по видимому.
        t = self.est_total(self.sel_groups())
        mid = dm.MODEL_ALIASES.get(self.short_model(), self.model)
        m_txt = f"Модель: {self.short_model()} ({mid})   "
        self.put(2, 1, m_txt)
        self.put(2, 1 + len(m_txt), f"Оценка: {fcost(t.cost)} · {fdur(t.seconds)}" +
                 (f"  [лимит {self.limit} гр.]" if self.limit else ""), self.A["title"])
        rg = self.sel_groups()
        risky = sum(1 for g in rg if self.est(g).risk)
        self.put(3, 1, dm.calibration_source(self.model, self.calibs) +
                 (f"   ÷ по частям (не влезают в один ответ): {risky} гр." if risky else ""), self.A["warn"] if risky else 0)
        mode, attr = ("[ APPLY ]", self.A["apply"]) if self.applying else ("[ DRY-RUN ]", self.A["dry"])
        self.put(4, 1, "Режим: ")
        self.put(4, 8, mode, attr)
        flt = f"tier={self.tier_f or 'все'} ctx={self.ctx_f or 'все'} сорт={self.sort}"
        s = f"поиск «{self.search}» {self.match_i + 1 if self.matches else 0}/{len(self.matches)}  " if self.search else ""
        self.put(4, 20, f"Снимок БД {self.snapshot_at}  {s}{flt}")
        self.put(5, 1, "Пробный прогон тоже вызывает API и стоит денег — в БД не пишет", self.A["warn"])
        if self.expired_n:
            self.put(6, 1, f"Просрочено medium: {fnum(self.expired_n)} (бот их не читает, dedup не видит) — x: удалить",
                     self.A["warn"])

    def col_layout(self, w):
        # sel(4) arrow(2) name(*) id(14) kind(7) facts(8) cost(8) flag(2) status(13)
        fixed = 4 + 2 + 14 + 7 + 8 + 8 + 2 + 13 + 8
        return max(10, w - fixed)

    def draw_main(self, h, w):
        self.draw_header(h, w)
        nw = self.col_layout(w)
        hdr = f"{'':8}{'Название':<{nw}} {'ID':<14} {'Тип':<7} {'Фактов':>8} {'$':>8} {'':2} {'Статус':<13}"
        self.put(7, 0, hdr.ljust(w), self.A["colhdr"])
        area = h - 9
        if self.cur < self.top:
            self.top = self.cur
        if self.cur >= self.top + area:
            self.top = self.cur - area + 1
        mset = set(self.matches)
        if not self.rows:
            self.put(8, 2, "Нет групп с 2+ живыми фактами под текущий фильтр.")
        for i in range(area):
            idx = self.top + i
            if idx >= len(self.rows):
                break
            r = self.rows[idx]
            y = 8 + i
            gs = self.row_groups(r)
            nf = sum(len(g.facts) for g in gs)
            ec = [self.est(g).cost for g in gs]
            cost = None if any(c is None for c in ec) else sum(ec)
            risk = any(self.est(g).risk for g in gs)
            sel = self.sel_state(r)
            if r.kind == "chat":
                arrow = "▾" if r.node.key in self.expanded else "▸"
                name = r.node.label + (f" · {len({g.key.user_id for g in r.node.groups})} юз." if r.node.kind == "group" else "")
                ident, kind, status = r.node.ident, r.node.kind, ""
                sattr = 0
            else:
                arrow = " "
                name = "   " + self.user_label(r.group.key.user_id) if r.node.kind == "group" else "   " + r.group.key.tier
                ident = str(r.group.key.user_id) if r.node.kind == "group" else ""
                kind = r.group.key.tier if r.node.kind == "group" else ""
                status, sattr = self.group_status(r.group)
            line = (f" [{sel}] {arrow} {clip(name, nw):<{nw}} {clip(ident, 14):<14} {kind:<7} "
                    f"{fnum(nf):>8} {fcost(cost):>8} {'÷' if risk else ' ':<2} ")
            base = self.A["sel"] if idx == self.cur else (self.A["hl"] if idx in mset else 0)
            self.put(y, 0, line.ljust(w) if idx == self.cur else line, base)
            if status:
                self.put(y, len(line), clip(status, 13), base if idx == self.cur else sattr)
        self.fbar(h, w, [("F1", "Помощь"), ("F2", "Модель"), ("F3", "Поиск"), ("F4", "Фильтр"),
                         ("F5", "Прогон"), ("F6", "Сорт"), ("F7", "Цены"), ("F8", "Примен"), ("F9", "Проф"),
                         ("F10", "Выход")])

    # ---- экран прогона
    def draw_run(self, h, w):
        n = len(self.run_groups)
        done = len(self.run_results)
        total_f = sum(len(g.facts) for g in self.run_groups)
        done_f = sum(len(r.facts_before) for r in self.run_results)
        end = self.run_ended or time.monotonic()
        elapsed = end - self.run_started
        eta = elapsed / done * (n - done) if done else None
        spent = sum(r.cost or 0 for r in self.run_results)
        est = self.est_total(self.run_groups, self.run_model).cost
        over = est is not None and spent > est * 1.5 and done > 0
        running = self.worker is not None and self.worker.is_alive() and not self.run_ended
        title = "Пробный прогон" + (" — остановка после текущей группы…" if self.run_stopping and running else "")
        self.put(0, 1, title + f"  модель {self.run_model}", self.A["title"])
        self.put(2, 1, "Группы ")
        self.put(2, 8, meter(done, n, 30))
        self.put(2, 9, "|" * round(30 * done / n) if n else "", self.A["ok"])
        self.put(2, 40, f"{done}/{n}")
        self.put(3, 1, "Факты  ")
        self.put(3, 8, meter(done_f, total_f, 30))
        self.put(3, 9, "|" * round(30 * done_f / total_f) if total_f else "", self.A["ok"])
        self.put(3, 40, f"{fnum(done_f)}/{fnum(total_f)}")
        self.put(5, 1, f"Прошло: {dm.fmt_dur(elapsed)}   ETA: {dm.fmt_dur(eta) if eta is not None else '?'}   "
                       f"Потрачено: ${spent:.4f} / оценка {fcost(est)}", self.A["warn"] if over else 0)
        if over:
            self.put(6, 1, "Факт уже больше оценки на 50%+ (оценка грубая: out/in = 1.0)", self.A["warn"])
        if running and done < n:
            g = self.run_groups[done]
            self.put(7, 1, f"Сейчас: {self.group_title(g)} · {g.key.tier} · {len(g.facts)} фактов", self.A["title"])
        elif self.run_error:
            self.put(7, 1, "Сбой: " + clip(self.run_error, w - 10), self.A["bad"])
        self.put(9, 0, " Лог".ljust(w), self.A["colhdr"])
        area = h - 11
        lines = [self.log_line(r) for r in self.run_results]
        off = max(0, min(self.log_off, max(0, len(lines) - area)))
        end_i = len(lines) - off
        for i, (txt, attr) in enumerate(lines[max(0, end_i - area):end_i]):
            self.put(10 + i, 1, clip(txt, w - 2), attr)
        self.fbar(h, w, [("Esc", "Стоп"), ("↑↓", "Лог")] if running else [("Enter", "К результатам"), ("Esc", "К результатам")])

    def group_title(self, g):
        k = g.key  # и Group, и GroupResult имеют .key
        who = self.user_label(k.user_id)
        if k.context == "group":
            return f"{self.chats.get(k.chat_id) or k.chat_id} / {who}"
        return who

    def log_line(self, r):
        g_title = self.group_title(r)
        c = f"${r.cost:.4f}" if r.cost is not None else "$?"
        pre = f"{g_title} {r.key.tier}: "
        if r.status == "ok":
            parts = f" ({dm.chunk_label(r)})" if r.chunks else ""
            return pre + f"ok {len(r.facts_before)}→{len(r.facts_after)}{parts} {c} {r.seconds:.1f}с", self.A["ok"]
        if r.status == "truncated":
            return pre + f"ОБРЕЗАНО{' (даже по частям)' if r.chunks else ''} — не будет применено {c}", self.A["bad"]
        if r.status == "empty":
            return pre + f"пустой ответ — не будет применено {c}", self.A["warn"]
        return pre + f"ошибка: {r.error}", self.A["bad"]

    # ---- экран результатов
    def result_list(self):
        return list(self.results.values())

    def applicable_included(self):
        return [r for r in self.result_list() if r.applicable and r.key in self.included]

    def draw_results(self, h, w):
        self.put(0, 1, "Результаты прогона и ручных правок (ещё не применённые)", self.A["title"])
        self.put(1, 1, "Пробел — включить/исключить группу, Enter — просмотр «было | стало»", 0)
        self.put(3, 0, f" {'':4}{'Группа':<{w - 52}} {'Тип':<7} {'Было→стало':>12} {'$':>8} {'Статус':<12}".ljust(w), self.A["colhdr"])
        rs = self.result_list()
        area = h - 7
        self.res_cur = max(0, min(self.res_cur, len(rs) - 1))
        if self.res_cur < self.res_top:
            self.res_top = self.res_cur
        if self.res_cur >= self.res_top + area:
            self.res_top = self.res_cur - area + 1
        if not rs:
            self.put(5, 2, "Результатов нет — сначала пробный прогон (F5).")
        for i in range(area):
            idx = self.res_top + i
            if idx >= len(rs):
                break
            r = rs[idx]
            box = "[x]" if (r.applicable and r.key in self.included) else ("[ ]" if r.applicable else "[-]")
            st = (EDIT_MARK + " " if r.edited else "") + self.res_label(r) + (f" ÷{r.chunks}" if r.chunks else "")
            line = f" {box}  {clip(self.group_title(r), w - 52):<{w - 52}} {r.key.tier:<7} " \
                   f"{(str(len(r.facts_before)) + '→' + (str(len(r.facts_after)) if r.applicable else '—')):>12} " \
                   f"{('$%.3f' % r.cost) if r.cost is not None else '$?':>8} {st:<12}"
            attr = self.A["sel"] if idx == self.res_cur else (0 if r.applicable else self.A["bad"])
            self.put(4 + i, 0, line.ljust(w) if idx == self.res_cur else line, attr)
        inc = self.applicable_included()
        b = sum(len(r.facts_before) for r in inc)
        a = sum(len(r.facts_after) for r in inc)
        self.put(h - 2, 1, f"К применению: {len(inc)} групп, {fnum(b)} → {fnum(a)} фактов", self.A["title"])
        self.fbar(h, w, [("Space", "Вкл/выкл"), ("Enter", "Просмотр"), ("e", "Править"), ("F8", "Применить"), ("Esc", "Назад")])

    # ---- просмотр группы и построчная правка
    def show_before(self, w):
        """Колонка «было»: выбор пользователя (запоминается), иначе по ширине окна (<100 — одна колонка)."""
        v = self.prefs.get("show_before")
        return (w >= 100) if v is None else bool(v)

    def save_prefs(self):
        try:
            self.prefs_path.write_text(json.dumps(self.prefs), encoding="utf-8")
        except OSError:
            pass

    def open_view(self, g, r=None, edit=False):
        self.vw = {"g": g, "r": r, "edit": False, "cur": 0, "bcur": 0, "col": "after", "top": 0, "msg": ""}
        self.prev_screen = self.screen
        self.screen = "view"
        if edit:
            self.begin_edit()

    def vw_lists(self):
        """(было, стало|None): «стало» есть только у результатов ok/manual."""
        r = self.vw["r"]
        before = r.facts_before if r else self.vw["g"].facts
        return before, (r.facts_after if (r and r.status in ("ok", "manual")) else None)

    @staticmethod
    def wrap_facts(facts, width):
        out = []
        for i, f in enumerate(facts):
            parts = textwrap.wrap(f, max(8, width - 2)) or [""]
            out += [(i, ("• " if j == 0 else "  ") + p) for j, p in enumerate(parts)]
        return out

    def draw_view(self, h, w):
        vw = self.vw
        r, g = vw["r"], vw["g"]
        before, after = self.vw_lists()
        two = after is not None and self.show_before(w)
        title = f"{self.group_title(r or g)} · {g.key.tier}"
        if r:
            title += f" · {self.res_label(r)}" + (f" ({dm.chunk_label(r)})" if r.chunks else "") + \
                (f" {EDIT_MARK}" if r.edited else "")
            if r.status in ("ok", "manual"):
                title += f" · правок: {len(self.undo.get(r.key, []))}"
        if vw["edit"]:
            title += "  [ ПРАВКА ]"
        self.put(0, 1, clip(title, w - 2), self.A["title"])
        cw = (w - 5) // 2 if two else w - 3
        L = self.wrap_facts(before, cw) if (two or after is None) else []
        R = self.wrap_facts(after, cw) if after is not None else []
        rows = max(len(L), len(R))
        area = h - 4
        # заголовок колонок
        if two:
            self.put(1, 0, f" {'БЫЛО (' + str(len(before)) + ')':<{cw}} │ {'СТАЛО (' + str(len(after)) + ')':<{cw}}".ljust(w), self.A["colhdr"])
        elif after is not None:
            self.put(1, 0, f" СТАЛО ({len(after)})".ljust(w), self.A["colhdr"])
        else:
            self.put(1, 0, f" ТЕКУЩИЕ ФАКТЫ ({len(before)})".ljust(w), self.A["colhdr"])
        # прокрутка: в режиме правки следуем за курсором
        if vw["edit"]:
            lines, idx = (R, vw["cur"]) if vw["col"] == "after" else (L, vw["bcur"])
            pos = [i for i, (fi, _) in enumerate(lines) if fi == idx]
            if pos:
                if pos[0] < vw["top"]:
                    vw["top"] = pos[0]
                elif pos[-1] >= vw["top"] + area:
                    vw["top"] = max(0, pos[-1] - area + 1) if len(pos) <= area else pos[0]
        vw["top"] = max(0, min(vw["top"], max(0, rows - area)))
        dups = {f for f, n in collections.Counter(after or []).items() if n > 1}
        for i in range(area):
            li = vw["top"] + i
            if li >= rows:
                break

            def attr(lines, col, cur, facts):
                if li >= len(lines):
                    return 0
                fi = lines[li][0]
                if vw["edit"] and fi == cur:
                    return self.A["sel"] if vw["col"] == col else curses.A_BOLD
                return self.A["warn"] if (col == "after" and facts[fi] in dups) else 0
            y = 2 + i
            if two:
                lt = L[li][1] if li < len(L) else ""
                rt = R[li][1] if li < len(R) else ""
                self.put(y, 1, clip(lt, cw).ljust(cw), attr(L, "before", vw["bcur"], before))
                self.put(y, 1 + cw, " │ ")
                self.put(y, 4 + cw, clip(rt, cw).ljust(cw), attr(R, "after", vw["cur"], after))
            elif after is not None:
                rt = R[li][1] if li < len(R) else ""
                self.put(y, 1, clip(rt, cw).ljust(cw), attr(R, "after", vw["cur"], after))
            else:
                lt = L[li][1] if li < len(L) else ""
                self.put(y, 1, clip(lt, cw), 0)
        # строка состояния / строчный редактор
        self.cursor_pos = None
        if self.draw_lined_bar(h, w):
            pass
        else:
            bits = []
            if after is not None and vw["edit"]:
                bits.append(f"строка {min(vw['cur'] + 1, len(after)) if after else 0}/{len(after)}")
                if not after:
                    bits.append("«стало» пусто — группа не будет применена (a — добавить)")
                elif vw["col"] == "after" and after[vw["cur"]] in dups:
                    bits.append("дубль в «стало»")
            if after is not None and not two:
                bits.append("b — показать было")
            if vw["msg"]:
                bits.append(vw["msg"])
            self.put(h - 2, 1, clip("  ·  ".join(bits), w - 2), self.A["warn"] if vw["msg"] else 0)
        if self.lined:
            self.fbar(h, w, [("Enter", "Сохранить"), ("Esc", "Отмена строки"), ("←→ Home End", "Курсор")])
        elif vw["edit"]:
            self.fbar(h, w, [("Enter", "Правка"), ("a", "Добавить"), ("d", "Удалить")] +
                      ([("→", "Из было")] if two else []) + [("u", "Отмена"), ("R", "Сброс"), ("b", "Было"), ("Esc", "Выход")])
        else:
            self.fbar(h, w, [("↑↓ PgUp PgDn", "Прокрутка")] + ([("e", "Править")]) + [("b", "Было"), ("Esc", "Назад")])

    def draw_lined_bar(self, h, w) -> bool:
        """Строчный редактор на строке h-2 (общий для просмотра группы и экрана «Профили»)."""
        le = self.lined
        if not le:
            return False
        prompt = " правка: "
        avail = max(5, w - len(prompt) - 2)
        if le["pos"] < le["scroll"]:
            le["scroll"] = le["pos"]
        elif le["pos"] >= le["scroll"] + avail:
            le["scroll"] = le["pos"] - avail + 1
        self.put(h - 2, 0, (prompt + le["buf"][le["scroll"]:le["scroll"] + avail]).ljust(w), self.A["warn"])
        self.cursor_pos = (h - 2, len(prompt) + le["pos"] - le["scroll"])
        return True

    # ---- правка
    def res_label(self, r):
        if r.status == "ok":
            return "ok" if r.facts_after else "пусто"
        return {"truncated": "обрезано", "error": "ошибка", "empty": "пусто", "manual": "ручная"}[r.status]

    def begin_edit(self):
        vw = self.vw
        r = vw["r"]
        if r is None:
            self.start_manual(vw["g"])
        elif r.status in ("ok", "manual"):
            vw["edit"], vw["col"] = True, "after"
            vw["cur"] = max(0, min(vw["cur"], len(r.facts_after) - 1))
        else:
            self.info(f"Править можно результаты ok и ручные; здесь статус «{self.res_label(r)}».")

    def start_manual(self, g):
        """Правка без модели: «было» = текущие факты снимка, «стало» = их копия. Денег не стоит."""
        ex = self.results.get(g.key)
        if ex is not None and ex.status == "manual":
            self.open_view(g, ex, edit=True)
        elif ex is not None:
            self.confirm([f"Для этой группы уже есть результат прогона ({self.res_label(ex)}"
                          f"{', с правками' if ex.edited else ''}).",
                          "Заменить его ручной правкой? Ответ модели останется в журнале прогона."],
                         lambda: self._begin_manual(g))
        else:
            self._begin_manual(g)

    def _begin_manual(self, g):
        r = dm.make_manual_result(g)
        self.results[g.key] = r
        self.included.discard(g.key)
        self.applied_keys.discard(g.key)
        self.undo.pop(g.key, None)
        if self.screen != "view":
            self.open_view(g, r, edit=True)
        else:
            self.vw.update(r=r, edit=True, col="after", cur=0, top=0)

    def sync_included(self, r, was):
        if r.applicable and not was:
            self.included.add(r.key)
        elif not r.applicable:
            self.included.discard(r.key)

    def mutate(self, fn):
        """Изменить «стало» с записью в стек отмены и пересчётом включённости."""
        r = self.vw["r"]
        was = r.applicable
        self.undo.setdefault(r.key, []).append(list(r.facts_after))
        fn(r.facts_after)
        self.sync_included(r, was)

    def open_line(self, text, on_done):
        self.lined = {"buf": text, "pos": len(text), "scroll": 0, "on_done": on_done}

    def keys_lined(self, k):
        le = self.lined
        if k == K_ENTER:
            self.lined = None
            le["on_done"](le["buf"].strip())
        elif k == K_ESC:
            self.lined = None
            le["on_done"](None)
        elif k == curses.KEY_LEFT:
            le["pos"] = max(0, le["pos"] - 1)
        elif k == curses.KEY_RIGHT:
            le["pos"] = min(len(le["buf"]), le["pos"] + 1)
        elif k == curses.KEY_HOME:
            le["pos"] = 0
        elif k == curses.KEY_END:
            le["pos"] = len(le["buf"])
        elif k == K_BS:
            if le["pos"]:
                le["buf"] = le["buf"][:le["pos"] - 1] + le["buf"][le["pos"]:]
                le["pos"] -= 1
        elif k == curses.KEY_DC:
            le["buf"] = le["buf"][:le["pos"]] + le["buf"][le["pos"] + 1:]
        elif isinstance(k, str) and k.isprintable():
            le["buf"] = le["buf"][:le["pos"]] + k + le["buf"][le["pos"]:]
            le["pos"] += 1
        self.dirty = True

    def leave_edit(self):
        vw = self.vw
        r = vw["r"]
        vw["edit"] = False
        if r is not None and r.status == "manual" and not r.edited:
            # ничего не поменяли — ручной результат не засоряет список
            self.results.pop(r.key, None)
            self.included.discard(r.key)
            self.undo.pop(r.key, None)
            vw["r"] = None

    def keys_view(self, k):
        vw = self.vw
        r = vw["r"]
        h, w = self.scr.getmaxyx()
        area = h - 4
        before, after = self.vw_lists()
        two = after is not None and self.show_before(w)
        vw["msg"] = ""
        if k == "b" and after is not None:
            self.prefs["show_before"] = not self.show_before(w)
            self.save_prefs()
            if not self.show_before(w) and vw["col"] == "before":
                vw["col"] = "after"
            return False
        if not vw["edit"]:
            if k in self.NAV:
                vw["top"] = self.move(k, vw["top"], 10 ** 6, area)
            elif k == "e":
                self.begin_edit()
            elif k in (K_ESC, "q", K_ENTER):
                self.screen = self.prev_screen
            return False
        # ---- режим правки
        col = vw["col"]
        if k in self.NAV:
            if col == "after":
                vw["cur"] = self.move(k, vw["cur"], len(after), area)
            else:
                vw["bcur"] = self.move(k, vw["bcur"], len(before), area)
        elif k == "\t" and two:
            vw["col"] = "before" if col == "after" else "after"
        elif k in ("d", curses.KEY_DC) and col == "after" and after:
            i = vw["cur"]
            self.mutate(lambda a: a.pop(i))
            vw["cur"] = max(0, min(i, len(after) - 1))
        elif k in (K_ENTER, "e") and col == "after" and after:
            i = vw["cur"]
            old = after[i]

            def done(text, i=i, old=old):
                if text is None or text == old:
                    return
                if text == "":
                    self.mutate(lambda a: a.pop(i))
                    vw["cur"] = max(0, min(i, len(after) - 1))
                else:
                    self.mutate(lambda a: a.__setitem__(i, text))
            self.open_line(old, done)
        elif k in ("a", curses.KEY_IC) and col == "after":
            at = vw["cur"] + 1 if after else 0

            def done_new(text, at=at):
                if text:
                    self.mutate(lambda a: a.insert(at, text))
                    vw["cur"] = at
            self.open_line("", done_new)
        elif k == curses.KEY_RIGHT and col == "before" and before:
            fact = before[vw["bcur"]]
            at = vw["cur"] + 1 if after else 0
            self.mutate(lambda a: a.insert(at, fact))
            vw["cur"] = at
            vw["msg"] = "скопировано в «стало»"
        elif k == "u":
            st = self.undo.get(r.key, [])
            if st:
                was = r.applicable
                r.facts_after[:] = st.pop()
                self.sync_included(r, was)
                vw["cur"] = max(0, min(vw["cur"], len(r.facts_after) - 1))
            else:
                vw["msg"] = "нечего отменять"
        elif k == "R":
            base = r.facts_model if r.status == "ok" else r.facts_before
            self.confirm(["Сбросить правки группы к " + ("ответу модели" if r.status == "ok" else "исходным фактам") + "?",
                          "Сброс можно отменить клавишей u."], lambda: self._reset_edits(base))
        elif k == K_ESC:
            self.leave_edit()
        return False

    def _reset_edits(self, base):
        r = self.vw["r"]
        self.mutate(lambda a: a.__setitem__(slice(None), list(base)))
        self.vw["cur"] = max(0, min(self.vw["cur"], len(r.facts_after) - 1))

    # ---- экран «Профили» (docs/claude/profile.md)
    def open_profiles(self):
        self.pf = {"tab": 0, "cur": 0, "top": 0, "marked": set(), "props": [], "profs": [], "rows": [], "missing": False}
        self.pf_reload()
        self.screen = "profiles"

    def pf_reload(self):
        pf = self.pf
        conn, tmp = dm.get_conn(writable=False)
        try:
            pf["props"], pf["profs"] = dm.load_profile_data(conn)
            pf["missing"] = False
        except sqlite3.OperationalError:
            pf["props"], pf["profs"], pf["missing"] = [], [], True  # бот с профилями ещё не запускался
        finally:
            conn.close()
            if tmp:
                os.remove(tmp)
        pf["marked"] &= {p["id"] for p in pf["props"]}
        self.pf_rebuild()

    def pf_rebuild(self):
        """Плоский список: чат -> человек -> (предложения | 9 полей профиля). Личка — отдельным пунктом."""
        pf = self.pf
        people: dict = {}   # (ctx, cid) -> {uid: {"name", "props", "fields"}}
        titles: dict = {}

        def person(ctx, cid, uid, name, chat_name):
            key = (ctx, cid if ctx == "group" else 0)
            titles[key] = "Личка" if ctx == "private" else (chat_name or f"чат {cid}")
            return people.setdefault(key, {}).setdefault(uid, {"name": name, "props": [], "fields": {}})
        for p in pf["props"]:
            person(p["context"], p["chat_id"], p["user_id"], p["user_name"], p["chat_name"])["props"].append(p)
        for r in pf["profs"]:
            person(r["context"], r["chat_id"], r["user_id"], r["user_name"], r["chat_name"])["fields"][r["field"]] = r
        rows = []
        for key in sorted(people, key=lambda k: (k[0] == "private", titles[k].lower())):
            n_props = sum(len(p["props"]) for p in people[key].values())
            if pf["tab"] == 0 and not n_props:
                continue
            rows.append({"kind": "chat", "title": titles[key], "key": key,
                         "count": n_props if pf["tab"] == 0 else len(people[key])})
            for uid, pr in sorted(people[key].items(), key=lambda kv: kv[1]["name"].lower()):
                if pf["tab"] == 0 and not pr["props"]:
                    continue
                rows.append({"kind": "person", "name": pr["name"], "uid": uid, "key": key, "props": pr["props"]})
                if pf["tab"] == 0:
                    rows += [{"kind": "prop", "p": p, "key": key, "uid": uid} for p in pr["props"]]
                else:
                    rows += [{"kind": "field", "key": key, "uid": uid, "field": f, "rec": pr["fields"].get(f)}
                             for f in dm._bot_db().PROFILE_FIELDS]
        pf["rows"] = rows
        pf["cur"] = max(0, min(pf["cur"], len(rows) - 1))
        self.dirty = True

    def pf_targets(self, row):
        """id предложений, на которые действуют y/n: отмеченные, иначе область под курсором."""
        pf = self.pf
        if pf["marked"]:
            return sorted(pf["marked"])
        if row is None:
            return []
        if row["kind"] == "prop":
            return [row["p"]["id"]]
        if row["kind"] == "person":
            return [p["id"] for p in row["props"]]
        if row["kind"] == "chat":
            return [r["p"]["id"] for r in pf["rows"] if r["kind"] == "prop" and r["key"] == row["key"]]
        return []

    def draw_profiles(self, h, w):
        pf = self.pf
        d = dm._bot_db()
        n_pend = len(pf["props"])
        for i, (name, x) in enumerate((("Предложения (%d)" % n_pend, 1), ("Профили (%d)" % len(pf["profs"]), 22))):
            self.put(0, x, f" {name} ", self.A["sel"] if pf["tab"] == i else 0)
        self.put(0, 44, "Tab — вкладка", 0)
        self.put(1, 1, "Запись в БОЕВУЮ базу: перед ней бэкап, подтверждение y/n. Личные чаты — полноправно, цитата видна здесь.",
                 self.A["warn"])
        if pf["missing"]:
            self.put(3, 2, "Таблиц профилей ещё нет: бот с профилями не запускался (перезапусти claudushka).")
            self.fbar(h, w, [("Esc", "Назад")])
            return
        self.put(2, 0, f" {'Предложения: чат → человек → поле' if pf['tab'] == 0 else 'Профили: чат → человек → поле'}".ljust(w),
                 self.A["colhdr"])
        area = h - 8
        rows = pf["rows"]
        if pf["cur"] < pf["top"]:
            pf["top"] = pf["cur"]
        if pf["cur"] >= pf["top"] + area:
            pf["top"] = pf["cur"] - area + 1
        if not rows:
            self.put(3, 2, "Нет ожидающих предложений." if pf["tab"] == 0 else "Профилей пока нет.")
        for i in range(area):
            idx = pf["top"] + i
            if idx >= len(rows):
                break
            r = rows[idx]
            if r["kind"] == "chat":
                text, attr = f"{r['title']}  ({r['count']})", self.A["title"]
            elif r["kind"] == "person":
                text, attr = f"  {r['name']}", curses.A_BOLD
            elif r["kind"] == "prop":
                p = r["p"]
                mark = "x" if p["id"] in pf["marked"] else " "
                text = (f"    [{mark}] {d.PROFILE_LABELS.get(p['field'], p['field'])}: {p['old_value'] or '—'} → {p['new_value']}"
                        f"   (дайджест {p['digest_id'] if p['digest_id'] else 'не было'})")
                attr = 0
            else:
                rec = r["rec"]
                text = f"    {d.PROFILE_LABELS[r['field']]:<14} {rec['value'] if rec else '—'}" + \
                       (f"   [{rec['source']}]" if rec else "")
                attr = 0 if rec else curses.A_DIM
            if idx == pf["cur"]:
                self.put(3 + i, 0, clip(text, w).ljust(w), self.A["sel"])
            else:
                self.put(3 + i, 0, clip(text, w), attr)
        # панель деталей: цитата-основание
        cur = rows[pf["cur"]] if rows else None
        if cur and cur["kind"] == "prop":
            p = cur["p"]
            ev = f"«{p['evidence']}»" if p["evidence"] else "—"
            for j, ln in enumerate(textwrap.wrap(f"Основание: {ev}", w - 3)[:2]):
                self.put(h - 5 + j, 1, ln, self.A["ok"])
            self.put(h - 3, 1, f"{'личка' if p['context'] == 'private' else 'группа'} · создано "
                               f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(p['created_at']))}"
                               f"{' · цитата есть только здесь, в .md-дайджесте её нет' if p['context'] == 'private' else ''}")
        if not self.draw_lined_bar(h, w):
            self.cursor_pos = None
        if self.lined:
            self.fbar(h, w, [("Enter", "Сохранить"), ("Esc", "Отмена"), ("←→ Home End", "Курсор")])
        elif pf["tab"] == 0:
            self.fbar(h, w, [("Space", "Отметить"), ("a", "Все"), ("y", "Подтвердить"), ("n", "Отклонить"),
                             ("Enter", "Исправить"), ("Tab", "Вкладка"), ("Esc", "Назад")])
        else:
            self.fbar(h, w, [("Enter", "Править"), ("d", "Удалить поле"), ("Tab", "Вкладка"), ("r", "Обновить"),
                             ("Esc", "Назад")])

    def keys_profiles(self, k):
        pf = self.pf
        rows = pf["rows"]
        area = self.scr.getmaxyx()[0] - 8
        row = rows[pf["cur"]] if rows else None
        if pf["missing"]:
            if k in (K_ESC, "q"):
                self.screen = "main"
            return False
        if k in self.NAV:
            pf["cur"] = self.move(k, pf["cur"], len(rows), area)
        elif k == "\t":
            pf["tab"] ^= 1
            pf["cur"] = pf["top"] = 0
            self.pf_rebuild()
        elif k in (K_ESC, "q"):
            self.screen = "main"
        elif k == "r":
            self.pf_reload()
        elif pf["tab"] == 0:
            ids = self.pf_targets(row)
            if k == " " and row:
                if row["kind"] == "prop":
                    pf["marked"] ^= {row["p"]["id"]}
                else:
                    target = set(self.pf_targets_scope(row))
                    pf["marked"] = (pf["marked"] - target) if target <= pf["marked"] else (pf["marked"] | target)
            elif k == "a":
                pf["marked"] = {p["id"] for p in pf["props"]}
            elif k in ("y", "n") and ids:
                self.pf_confirm_decide(ids, approve=(k == "y"))
            elif k == K_ENTER and row and row["kind"] == "prop":
                p = row["p"]
                self.open_line(p["new_value"], lambda text, p=p: self.pf_edit_done(p, text))
        else:
            if k == K_ENTER and row and row["kind"] == "field":
                cur = row["rec"]["value"] if row["rec"] else ""
                self.open_line(cur, lambda text, row=row: self.pf_field_done(row, text))
            elif k in ("d", curses.KEY_DC) and row and row["kind"] == "field" and row["rec"]:
                label = dm._bot_db().PROFILE_LABELS[row["field"]]
                op = ("delete", row["uid"], row["key"][0], row["key"][1], row["field"])
                self.confirm([f"Удалить поле «{label}» ({row['rec']['value']}) из профиля?",
                              "Перед записью — бэкап БД. Запись в боевую базу."], lambda: self.pf_commit([op]))
        return False

    def pf_targets_scope(self, row):
        """id предложений под строкой-заголовком (для отметки пробелом на чате/человеке)."""
        pf = self.pf
        if row["kind"] == "person":
            return [p["id"] for p in row["props"]]
        return [r["p"]["id"] for r in pf["rows"] if r["kind"] == "prop" and r["key"] == row["key"]]

    def pf_confirm_decide(self, ids, approve):
        by_id = {p["id"]: p for p in self.pf["props"]}
        lines = [f"{'Подтвердить' if approve else 'Отклонить'} предложений: {len(ids)}.", ""]
        d = dm._bot_db()
        for i in ids[:6]:
            p = by_id[i]
            lines.append(f"· {p['user_name']}: {d.PROFILE_LABELS.get(p['field'], p['field'])} → {p['new_value']}")
        if len(ids) > 6:
            lines.append(f"· … и ещё {len(ids) - 6}")
        lines.append("Перед записью — бэкап БД. Запись в боевую базу.")
        op = "approve" if approve else "reject"
        self.confirm(lines, lambda: self.pf_commit([(op, i) for i in ids]))

    def pf_edit_done(self, p, text):
        if text is None or not text:
            return
        d = dm._bot_db()
        override = None if text == p["new_value"] else text
        self.confirm([f"Подтвердить {p['user_name']}: {d.PROFILE_LABELS.get(p['field'], p['field'])} → {text}"
                      + (" (исправлено вами, source=admin)" if override else "") + "?",
                      "Перед записью — бэкап БД. Запись в боевую базу."],
                     lambda: self.pf_commit([("approve", p["id"], override)]))

    def pf_field_done(self, row, text):
        if text is None or not text:
            return
        d = dm._bot_db()
        op = ("set", row["uid"], row["key"][0], row["key"][1], row["field"], text)
        self.confirm([f"Записать в профиль: {d.PROFILE_LABELS[row['field']]} = {text}  (source=admin)?",
                      "Перед записью — бэкап БД. Запись в боевую базу."], lambda: self.pf_commit([op]))

    def pf_commit(self, ops):
        try:
            rep = dm.profile_commit(ops)
        except dm.BackupError as e:
            self.info(f"Не записано: {e}", "В БД ничего не изменено.")
            return
        self.pf_reload()
        lines = [f"Готово: подтверждено {rep['approved']}, отклонено {rep['rejected']}, записано {rep['set']}, "
                 f"удалено {rep['deleted']}; уже решено {rep['already']}.", f"Бэкап: {rep['backup']}"]
        for op, why in rep["failed"][:4]:
            lines.append(f" · не применено: {why}")
        self.info(*lines)

    # ---- модель / сравнение / помощь
    def model_rows(self):
        out = []
        sg = self.sel_groups() or self.visible_groups()
        for alias, mid in dm.MODEL_ALIASES.items():
            t = self.est_total(sg, mid)
            out.append((alias, mid, t))
        return out

    def draw_model(self, h, w):
        self.draw_main(h, w)
        rows = self.model_rows()
        bw, bh = min(w - 4, 76), len(rows) + 4
        y0, x0 = (h - bh) // 2, (w - bw) // 2
        for y in range(bh):
            self.put(y0 + y, x0, " " * bw, self.A["sel"])
        self.put(y0, x0 + 2, " Выбор модели ", self.A["sel"] | curses.A_BOLD)
        for i, (alias, mid, t) in enumerate(rows):
            p = dm.PRICING.get(mid)
            price = f"{p[0]:g}/{p[1]:g}" if p else "?"
            line = f" {alias:<7}{mid:<19}${price:<9}{fcost(t.cost):>9} {fdur(t.seconds):>9}"
            attr = (self.A["warn"] | curses.A_REVERSE) if i == self.pop_cur else self.A["sel"]
            self.put(y0 + 2 + i, x0 + 1, clip(line, bw - 2).ljust(bw - 2), attr)
        self.put(y0 + bh - 1, x0 + 2, "$ in/out за MTok · Enter выбрать · Esc закрыть", self.A["sel"])

    def draw_compare(self, h, w):
        sg = self.sel_groups()
        basis = "по выбору" if sg else "по ВСЕМ видимым (ничего не выбрано)"
        sg = sg or self.visible_groups()
        self.put(0, 1, f"Сравнение стоимости по моделям — {len(sg)} групп, {basis}", self.A["title"])
        self.put(2, 0, f" {'Модель':<8}{'≈вход ток':>11}{'≈выход ток':>12}{'≈$':>10}{'≈время':>10}{'по частям':>10}  Источник".ljust(w), self.A["colhdr"])
        for i, (alias, mid, t) in enumerate(self.model_rows()):
            risk = sum(1 for g in sg if self.est(g, mid).risk)
            mark = "▸" if mid == self.model else " "
            line = (f"{mark}{alias:<8}{fnum(t.in_tok):>11}{fnum(t.out_tok):>12}{fcost(t.cost):>10}"
                    f"{fdur(t.seconds):>10}{risk:>10}  {dm.calibration_source(mid, self.calibs)}")
            self.put(3 + i, 0, clip(line, w).ljust(w) if i == self.pop_cur else clip(line, w),
                     self.A["sel"] if i == self.pop_cur else 0)
        notes = [f"Допущения: {dm.CHARS_PER_TOKEN} симв./токен (одна точка замера — грубо); выход = токены фактов × "
                 f"{dm.OUT_RATIO} (верхняя граница: «ничего не слилось»); вход тарифицируется как запись кэша 5m "
                 f"(×1.25); у Haiku — ступень >100k токенов; потолок ответа {dm.MAX_OUT_TOKENS}, 'по частям' — группы, "
                 "которые не влезают в один ответ: идут по частям (куски + слияние, вызовов больше — оценка грубая). "
                 "Время — только из калибровки. Gemini: оценка ПРИБЛИЗИТЕЛЬНАЯ — рассуждения идут в вывод "
                 f"(+{dm.THINKING_EST['gemini-3.8-flash']} токенов на вызов, замер единичный)."]
        for i, ln in enumerate(textwrap.wrap(notes[0], w - 2)):
            self.put(9 + i, 1, ln)
        self.fbar(h, w, [("Enter", "Сделать текущей"), ("x", "Сбросить калибровку"), ("Esc", "Назад")])

    HELP = [
        ("↑ ↓ PgUp PgDn Home End", "навигация"),
        ("→ / Enter", "раскрыть чат; на группе — просмотр фактов"),
        ("←", "свернуть / к родителю"),
        ("Пробел", "выбрать / снять (на чате — все его группы; [~] — частично)"),
        ("a / n / i", "выбрать всё видимое / снять всё / инвертировать видимое"),
        ("/ или F3", "поиск; Enter в строке поиска — перейти к первому совпадению"),
        ("N / P (или F3)", "следующее / предыдущее совпадение (Esc — сбросить поиск)"),
        ("F4 / t", "фильтр tier: все → long → medium;   p — context: все → group → private"),
        ("F6 / s", "сортировка: по фактам → по $ → по имени"),
        ("F2 / m", "выбор модели"),
        ("F7 / c", "сравнение стоимости по моделям (x — сбросить калибровку модели)"),
        ("F5 / d", "пробный прогон выбранного (СТОИТ ДЕНЕГ, в БД не пишет)"),
        ("l", "лимит групп для прогона (0 — без лимита)"),
        ("e", "правка группы БЕЗ модели (на строке группы): удалить/поправить/добавить факты, бесплатно"),
        ("F9 / f", "экран «Профили»: предложения модели (подтвердить/отклонить/исправить) и ручная правка профилей"),
        ("x", "удалить просроченные medium-факты (бэкап + ввод APPLY; бот чистит их и сам раз в сутки)"),
        ("r", "перечитать снимок БД"),
        ("v", "экран результатов прогона"),
        ("Просмотр группы", "e — режим правки «стало»; b — показать/скрыть «было» (запоминается)"),
        ("В режиме правки", "↑↓ курсор · Tab колонка · Enter/e правка · a/Ins добавить · d/Del удалить"),
        ("", "→ (курсор в «было») копировать в «стало» · u отмена · R сброс к ответу модели · Esc выход"),
        ("В строчном редакторе", "← → Home End Backspace Del · Enter сохранить · Esc отмена строки; пусто = удалить"),
        ("F8 / A", "применить отрецензированные результаты (бэкап + ввод APPLY)"),
        ("Esc", "закрыть попап / назад / сбросить поиск; на главном экране — ничего"),
        ("F10 / q", "выход (спросит, если есть непримененные результаты)"),
        ("", "Русская раскладка для командных клавиш работает (й=q, ф=a, …)."),
    ]

    def help_lines(self, w):
        out = []
        for k, d in self.HELP:
            parts = textwrap.wrap(d, max(20, w - 30)) or [""]
            out += [(k if j == 0 else "", p) for j, p in enumerate(parts)]
        return out

    def draw_help(self, h, w):
        self.put(0, 1, "Помощь — клавиши (↑↓ прокрутка)", self.A["title"])
        lines = self.help_lines(w)
        area = h - 4
        self.help_off = max(0, min(self.help_off, max(0, len(lines) - area)))
        for i, (k, d) in enumerate(lines[self.help_off:self.help_off + area]):
            self.put(2 + i, 1, f"{k:<26}", self.A["warn"])
            self.put(2 + i, 28, d)
        self.fbar(h, w, [("↑↓", "Прокрутка"), ("Esc", "Назад")])

    # ---- модальные окна
    def draw_modal(self, h, w):
        m = self.modal
        lines = []
        for ln in m["lines"]:
            lines += textwrap.wrap(ln, 70) or [""]
        if m["type"] == "input":
            lines += ["", m["prompt"] + m["buf"] + "_"]
        elif m["type"] in ("confirm", "choice"):
            lines += ["", m.get("hint") or "y — да, n / Esc — нет"]
        else:
            lines += ["", "любая клавиша — закрыть"]
        bw = min(w - 2, max(len(x) for x in lines) + 4)
        bh = len(lines) + 2
        y0, x0 = max(0, (h - bh) // 2), max(0, (w - bw) // 2)
        attr = self.A["apply"] if m.get("danger") else self.A["sel"]
        for y in range(bh):
            self.put(y0 + y, x0, " " * bw, attr)
        for i, ln in enumerate(lines):
            self.put(y0 + 1 + i, x0 + 2, ln[:bw - 4], attr)

    def info(self, *lines):
        self.modal = {"type": "info", "lines": list(lines)}
        self.dirty = True

    def confirm(self, lines, on_yes, hint=None, enter_ok=False):
        self.modal = {"type": "confirm", "lines": lines, "on_yes": on_yes, "hint": hint, "enter_ok": enter_ok}
        self.dirty = True

    def choose(self, lines, options, hint):
        self.modal = {"type": "choice", "lines": lines, "options": options, "hint": hint}
        self.dirty = True

    def ask(self, lines, prompt, on_ok, danger=False):
        self.modal = {"type": "input", "lines": lines, "prompt": prompt, "buf": "", "on_ok": on_ok, "danger": danger}
        self.dirty = True

    # ------------------------------------------------------------ ввод
    def read_key(self):
        try:
            k = self.scr.get_wch()
        except curses.error:
            return None
        if isinstance(k, str):
            if k in ("\n", "\r"):
                return K_ENTER
            if k == "\x1b":
                return K_ESC
            if k in ("\x7f", "\b"):
                return K_BS
            return k
        if k == curses.KEY_ENTER:
            return K_ENTER
        if k == curses.KEY_BACKSPACE:
            return K_BS
        return k

    @staticmethod
    def cmd(k):
        return RU2EN.get(k, k) if isinstance(k, str) else k

    def handle_modal(self, k):
        m = self.modal
        t = m["type"]
        if t == "info":
            self.modal = None
        elif t == "choice":
            kk = "ENTER" if k == K_ENTER else self.cmd(k)
            if k == K_ESC:
                self.modal = None
            elif kk in m["options"]:
                self.modal = None
                m["options"][kk]()
        elif t == "confirm":
            kk = self.cmd(k)
            if kk in ("y", "Y") or (k == K_ENTER and m.get("enter_ok")):
                self.modal = None
                m["on_yes"]()
            elif kk in ("n", "N", K_ESC):
                self.modal = None
        else:
            if k == K_ESC:
                self.modal = None
            elif k == K_ENTER:
                self.modal = None
                m["on_ok"](m["buf"])
            elif k == K_BS:
                m["buf"] = m["buf"][:-1]
            elif isinstance(k, str) and k.isprintable():
                m["buf"] += k
        self.dirty = True

    def handle_key(self, k):
        """True — выйти из программы."""
        self.dirty = True
        if self.modal:
            self.handle_modal(k)
            return False
        if self.lined:
            self.keys_lined(k)  # сырой ввод (не cmd): кириллица в тексте не должна мапиться в команды
            return False
        h, w = self.scr.getmaxyx()
        if h < MIN_H or w < MIN_W:
            return self.cmd(k) in ("q", "Q", curses.KEY_F10)
        return getattr(self, "keys_" + self.screen)(self.cmd(k))

    def move(self, k, cur, n, area):
        if k == curses.KEY_UP:
            cur -= 1
        elif k == curses.KEY_DOWN:
            cur += 1
        elif k == curses.KEY_PPAGE:
            cur -= area
        elif k == curses.KEY_NPAGE:
            cur += area
        elif k == curses.KEY_HOME:
            cur = 0
        elif k == curses.KEY_END:
            cur = n - 1
        return max(0, min(cur, n - 1)) if n else 0

    NAV = (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE, curses.KEY_HOME, curses.KEY_END)

    def keys_main(self, k):
        area = self.scr.getmaxyx()[0] - 9
        if k in self.NAV:
            self.cur = self.move(k, self.cur, len(self.rows), area)
            return False
        r = self.rows[self.cur] if self.rows else None
        if k in ("q", curses.KEY_F10):
            return self.try_quit()
        if k == curses.KEY_RIGHT and r and r.kind == "chat":
            self.expanded.add(r.node.key)
            self.rebuild()
        elif k == K_ENTER and r:
            if r.kind == "chat":
                (self.expanded.discard if r.node.key in self.expanded else self.expanded.add)(r.node.key)
                self.rebuild()
            else:
                self.open_view(r.group, self.results.get(r.group.key))
        elif k == curses.KEY_LEFT and r:
            if r.kind == "group":
                self.cur = next(i for i, x in enumerate(self.rows) if x.kind == "chat" and x.node is r.node)
            else:
                self.expanded.discard(r.node.key)
                self.rebuild()
        elif k == " " and r:
            self.toggle(r)
        elif k == "a":
            self.selected |= {g.key for g in self.visible_groups()}
        elif k == "n":
            self.selected.clear()
        elif k == "i":
            vis = {g.key for g in self.visible_groups()}
            self.selected = (self.selected - vis) | (vis - self.selected)
        elif k in ("/", curses.KEY_F3):
            if k == curses.KEY_F3 and self.search:
                self.goto_match(1)
            else:
                self.ask(["Поиск по названию чата, имени, id."], "Поиск: ", self.do_search)
        elif k == "N":
            self.goto_match(1)
        elif k == "P":
            self.goto_match(-1)
        elif k == K_ESC:
            if self.search:
                self.search, self.matches, self.match_i = "", [], -1
        elif k in ("t", curses.KEY_F4):
            self.tier_f = {None: "long", "long": "medium", "medium": None}[self.tier_f]
            self.rebuild()
        elif k == "p":
            self.ctx_f = {None: "group", "group": "private", "private": None}[self.ctx_f]
            self.rebuild()
        elif k in ("s", curses.KEY_F6):
            self.sort = {"facts": "cost", "cost": "name", "name": "facts"}[self.sort]
            self.rebuild()
        elif k in ("m", curses.KEY_F2):
            self.pop_cur = list(dm.MODEL_ALIASES.values()).index(self.model) if self.model in dm.MODEL_ALIASES.values() else 0
            self.screen = "model"
        elif k in ("c", curses.KEY_F7):
            self.pop_cur = list(dm.MODEL_ALIASES.values()).index(self.model) if self.model in dm.MODEL_ALIASES.values() else 0
            self.screen = "compare"
        elif k in ("d", curses.KEY_F5):
            self.start_run()
        elif k in ("A", curses.KEY_F8):
            if self.results:
                self.screen = "results"
            self.start_apply()
        elif k in (curses.KEY_F9, "f"):
            self.open_profiles()
        elif k == "x":
            self.start_purge()
        elif k == "e":
            if r and r.kind == "group":
                self.start_manual(r.group)
            else:
                self.info("Правка без модели — на строке группы (раскрой чат: →). На чате она не работает.")
        elif k == "v":
            self.screen = "results"
        elif k == "l":
            self.ask(["Лимит групп для пробного прогона (0 — без лимита)."], "Лимит: ", self.set_limit)
        elif k == "r":
            self.load_data()
        elif k in ("?", curses.KEY_F1):
            self.screen = "help"
        return False

    def start_purge(self):
        if not self.expired_n:
            self.info("Просроченных строк нет.")
            return
        self.applying = True
        self.ask([f"УДАЛЕНИЕ ПРОСРОЧЕННЫХ ФАКТОВ ИЗ БОЕВОЙ БД: {fnum(self.expired_n)} строк (expires_at <= now).",
                  "Бот их не читает и dedup не видит; long не затрагивается. Модель не вызывается.",
                  f"Перед удалением будет сделан бэкап: {dm.BACKUPS_DIR}/claudushka-dedup-<время>.db",
                  "Введите APPLY и Enter (пусто / Esc — отмена)."], "> ", self._purge, danger=True)

    def _purge(self, text):
        self.applying = False
        if text.strip() != "APPLY":
            self.info("Отменено — ничего не удалено.")
            return
        try:
            n, backup = dm.purge_expired_reviewed()
        except dm.BackupError as e:
            self.info(f"Не удалено: {e}", "В БД ничего не изменено.")
            return
        self.load_data()
        self.info(f"Удалено просроченных строк: {fnum(n)}.", f"Бэкап: {backup}")

    def do_search(self, text):
        self.search = text.strip()
        s = self.search.lower()
        if s:  # раскрыть чаты, где совпал кто-то из детей
            for n in self.nodes.values():
                if any(s in f"{self.user_label(g.key.user_id)} {g.key.user_id}".lower() for g in n.groups):
                    self.expanded.add(n.key)
        self.rebuild()
        self.match_i = -1
        self.goto_match(1)

    def goto_match(self, d):
        if not self.matches:
            return
        self.match_i = (self.match_i + d) % len(self.matches)
        self.cur = self.matches[self.match_i]

    def set_limit(self, text):
        try:
            self.limit = max(0, int(text.strip() or 0))
        except ValueError:
            self.info("Нужно целое число.")

    def keys_model(self, k):
        rows = list(dm.MODEL_ALIASES.values())
        if k in self.NAV:
            self.pop_cur = self.move(k, self.pop_cur, len(rows), 4)
        elif k == K_ENTER:
            self.model = rows[self.pop_cur]
            self.est_cache.clear()
            self.refresh_prev_trunc()
            self.rebuild()
            self.screen = "main"
        elif k == K_ESC:
            self.screen = "main"
        return False

    def keys_compare(self, k):
        rows = list(dm.MODEL_ALIASES.values())
        if k in self.NAV:
            self.pop_cur = self.move(k, self.pop_cur, len(rows), 4)
        elif k == K_ENTER:
            self.model = rows[self.pop_cur]
            self.est_cache.clear()
            self.rebuild()
        elif k == "x":
            mid = rows[self.pop_cur]
            if mid not in self.calibs:
                self.info(f"У {mid} нет калибровки — сбрасывать нечего.")
            else:
                self.confirm([f"Сбросить калибровку {mid}?",
                              "Оценки вернутся к дефолтам (2.5 симв./токен, без времени).",
                              "Вручную: удалить ключ модели из data/dedup_calibration.json."],
                             lambda: self._reset_calib(mid))
        elif k in (K_ESC, "q"):
            self.screen = "main"
        return False

    def _reset_calib(self, mid):
        dm.reset_calibration(mid)
        self.calibs = dm.load_calibration()
        self.est_cache.clear()
        self.rebuild()

    def keys_help(self, k):
        if k in self.NAV:
            area = self.scr.getmaxyx()[0] - 4
            self.help_off = self.move(k, self.help_off, 10 ** 6, area)
        elif k in (K_ESC, "q", K_ENTER):
            self.screen = "main"
        return False

    def keys_results(self, k):
        rs = self.result_list()
        area = self.scr.getmaxyx()[0] - 7
        if k in self.NAV:
            self.res_cur = self.move(k, self.res_cur, len(rs), area)
        elif k == " " and rs:
            r = rs[self.res_cur]
            if r.applicable:
                self.included ^= {r.key}
        elif k == K_ENTER and rs:
            r = rs[self.res_cur]
            g = next((x for x in self.groups if x.key == r.key), None)
            if g:
                self.open_view(g, r)
        elif k == "e" and rs:
            r = rs[self.res_cur]
            g = next((x for x in self.groups if x.key == r.key), None)
            if g:
                self.open_view(g, r, edit=True)
        elif k in (curses.KEY_F8, "A"):
            self.start_apply()
        elif k in (K_ESC, "q"):
            self.screen = "main"
        return False

    def keys_run(self, k):
        running = self.worker is not None and self.worker.is_alive() and not self.run_ended
        area = self.scr.getmaxyx()[0] - 11
        if k == curses.KEY_UP:
            self.log_off = min(self.log_off + 1, max(0, len(self.run_results) - area))
        elif k == curses.KEY_DOWN:
            self.log_off = max(0, self.log_off - 1)
        elif running and k in (K_ESC, "q"):
            self.confirm(["Остановить после текущей группы?",
                          "Идущий запрос дойдёт до конца, уже полученные результаты сохранятся."], self.request_stop)
        elif not running and k in (K_ESC, K_ENTER, "q"):
            self.screen = "results" if self.results else "main"
        return False

    # ------------------------------------------------------------ прогон
    def start_run(self):
        gs = self.sel_groups()
        if not gs:
            self.info("Ничего не выбрано. Пробел — выбрать чат/группу, a — всё видимое.")
            return
        if self.worker and self.worker.is_alive():
            self.info("Прогон уже идёт.")
            return
        try:
            client = dm.make_client()
        except dm.DedupEnvError as e:
            self.info(*str(e).split("\n"))
            return
        flagged = [g for g in gs if g.key in self.prev_trunc]
        if not flagged:
            self._confirm_run(gs, client)
            return
        names = [f"  · {self.group_title(g)} · {g.key.tier} · {len(g.facts)} ф. ({self.prev_trunc[g.key]['date']})"
                 for g in flagged[:8]]
        if len(flagged) > 8:
            names.append(f"  · … и ещё {len(flagged) - 8}")
        rest = [g for g in gs if g.key not in self.prev_trunc]
        self.choose([f"В прошлый раз обрезано при тех же данных: {len(flagged)} групп.", *names,
                     "Повтор даст то же самое и снова потратит деньги."],
                    {"ENTER": lambda: (self._confirm_run(rest, client) if rest else self.info("Все выбранные группы пропущены.")),
                     "f": lambda: self._confirm_run(gs, client)},
                    hint="Enter — пропустить их · f — всё равно гнать · Esc — отмена")

    def _confirm_run(self, gs, client):
        t = self.est_total(gs)
        hid = self.hidden_selected(gs)
        self.confirm([f"Пробный прогон: {len(gs)} групп, {fnum(sum(len(g.facts) for g in gs))} фактов, модель {self.short_model()}.",
                      f"Оценка: {fcost(t.cost)} · {fdur(t.seconds)}" + (f"  · вызовов модели: {t.calls}, большие группы идут по частям" if t.risk else ""),
                      *([f"Из них скрыто фильтром: {len(hid)} групп."] if hid else []),
                      "Это вызывает API и СТОИТ ДЕНЕГ; в БД ничего не пишется."],
                     lambda: self._launch(gs, client), hint="Enter — запустить, Esc — отмена", enter_ok=True)

    def _launch(self, gs, client):
        self.run_groups, self.run_results = gs, []
        self.run_model, self.run_error, self.run_stopping = self.model, None, False
        self.stop_event = threading.Event()
        self.run_started, self.run_ended, self.log_off = time.monotonic(), 0.0, 0
        self.q = queue.Queue()

        def work(groups, model, stop, q, calib):
            try:
                for res in dm.run_llm(groups, model, client, stop, calib):
                    q.put(("res", res))
                q.put(("done", None))
            except Exception as e:  # noqa: BLE001 — любой сбой потока показываем, не роняем curses
                q.put(("fail", f"{type(e).__name__}: {e}"))

        self.worker = threading.Thread(target=work, args=(gs, self.model, self.stop_event, self.q, self.calibs.get(self.model)), daemon=True)
        self.worker.start()
        self.screen = "run"
        self.dirty = True

    def request_stop(self):
        self.run_stopping = True
        self.stop_event.set()

    def drain_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                self.dirty = True
                if kind == "res":
                    self.run_results.append(payload)
                elif kind in ("done", "fail"):
                    self.run_ended = time.monotonic()
                    if kind == "fail":
                        self.run_error = payload
                    self.finish_run()
        except queue.Empty:
            pass
        if self.screen == "run" and not self.run_ended:
            self.dirty = True  # тикают часы

    def finish_run(self):
        kept = 0
        for r in self.run_results:
            ex = self.results.get(r.key)
            if ex is not None and ex.edited:
                kept += 1  # правки человека важнее нового ответа модели; он остался в журнале прогона
                continue
            self.results[r.key] = r
            self.undo.pop(r.key, None)
            self.applied_keys.discard(r.key)
            if r.applicable:
                self.included.add(r.key)
            else:
                self.included.discard(r.key)
        path, calib = dm.finish_run(self.run_model, self.run_results)
        if calib:
            self.calibs = dm.load_calibration()
            self.est_cache.clear()
        self.run_log_path = path
        self.refresh_prev_trunc()
        if kept:
            self.info(f"{kept} групп с вашими правками не перезаписаны новым прогоном.",
                      "Новый ответ модели по ним — в журнале прогона (data/dedup_runs/).")

    # ------------------------------------------------------------ применение
    def start_apply(self):
        inc = self.applicable_included()
        if not inc:
            self.info("Нечего применять: нет включённых групп со статусом ok.",
                      "Сначала пробный прогон (F5), затем отметь группы на экране результатов.")
            return
        b = sum(len(r.facts_before) for r in inc)
        a = sum(len(r.facts_after) for r in inc)
        self.applying = True
        vis = {g.key for g in self.visible_groups()}
        hid = [r for r in inc if r.key not in vis]
        self.ask([f"ПРИМЕНЕНИЕ В БОЕВУЮ БД: {len(inc)} групп, {fnum(b)} → {fnum(a)} фактов.",
                  *([f"Из них скрыто фильтром: {len(hid)} групп."] if hid else []),
                  *([f"Ручных правок (без модели): {sum(1 for r in inc if r.status == 'manual')} групп."]
                    if any(r.status == "manual" for r in inc) else []),
                  *([f"С правками человека поверх ответа модели: {sum(1 for r in inc if r.status == 'ok' and r.edited)} групп."]
                    if any(r.status == "ok" and r.edited for r in inc) else []),
                  f"Перед записью будет сделан бэкап: {dm.BACKUPS_DIR}/claudushka-dedup-<время>.db",
                  "Модель повторно не вызывается — пишется ровно то, что в результатах.",
                  "Введите APPLY и Enter (пусто / Esc — отмена)."], "> ", lambda t: self._apply(inc, t), danger=True)

    def _apply(self, inc, text):
        self.applying = False
        if text.strip() != "APPLY":
            self.info("Отменено — ничего не записано.")
            return
        # журнал ДО записи: ответ модели + итог после правок (edited / manual)
        dm.save_run(self.run_model if any(r.status == "ok" for r in inc) else "manual", inc, suffix="-reviewed")
        try:
            rep = dm.apply_reviewed(inc)
        except dm.BackupError as e:
            self.info(f"Не применено: {e}", "В БД ничего не записано.")
            return
        for k in rep.applied:
            self.results.pop(k, None)
            self.included.discard(k)
            self.applied_keys.add(k)
        lines = [f"Применено групп: {len(rep.applied)} ({fnum(rep.facts_before)} → {fnum(rep.facts_after)} фактов).",
                 f"Пропущено: {len(rep.skipped)}", f"Бэкап: {rep.backup_path}"]
        for k, why in rep.skipped[:6]:
            lines.append(f" · {self.user_label(k.user_id)} {k.tier}: {why}")
        if len(rep.skipped) > 6:
            lines.append(f" · … и ещё {len(rep.skipped) - 6}")
        self.load_data()
        self.info(*lines)

    def try_quit(self):
        pending = [r for r in self.result_list() if r.applicable]
        running = self.worker is not None and self.worker.is_alive() and not self.run_ended
        if pending or running:
            msg = "Идёт прогон! " if running else ""
            self.confirm([msg + f"Есть непримененные результаты ({len(pending)} гр.). Выйти без применения?",
                          "Журнал прогона уже сохранён в data/dedup_runs/; правки сохранятся отдельным файлом."
                          if not running else "Текущий запрос будет потерян."],
                         lambda: (self.save_edits_journal(), setattr(self, "quit_now", True)))
            return False
        return True

    def save_edits_journal(self):
        ed = [r for r in self.result_list() if r.edited]
        if ed:
            dm.save_run(self.run_model if any(r.status == "ok" for r in ed) else "manual", ed, suffix="-unapplied")

    quit_now = False

    # ------------------------------------------------------------ цикл
    def loop(self):
        self.scr.timeout(100)
        while True:
            self.drain_queue()
            if self.quit_now:
                self.stop_event.set()
                return
            if self.dirty:
                self.draw()
            k = self.read_key()
            if k is None:
                continue
            if k == curses.KEY_RESIZE:
                curses.update_lines_cols()
                self.dirty = True
                continue
            if self.handle_key(k):
                self.stop_event.set()
                return


def _main(scr, model_alias):
    try:
        curses.set_escdelay(25)  # Esc срабатывает сразу, а не через секунду
    except (AttributeError, curses.error):
        pass
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    scr.keypad(True)
    app = App(scr, model_alias)
    try:
        app.loop()
    except KeyboardInterrupt:
        # Ctrl+C: терминал восстановит curses.wrapper; уже полученное из прогона не теряем
        if app.run_results and not app.run_ended:
            dm.finish_run(app.run_model, app.run_results)
        app.save_edits_journal()
        raise


def run(model_alias="lite") -> int:
    """Точка входа из dedup_memory.py. 2 — не TTY."""
    import sys
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("Нужен интерактивный терминал: docker exec -it claudushka python3 /repo/dedup_memory.py")
        return 2
    locale.setlocale(locale.LC_ALL, "")
    # api_errors пишет предупреждения через logging -> по умолчанию stderr, а это ломает экран
    # curses (мусор поверх интерфейса). В TUI всё логирование — в файл рядом с БД.
    try:
        handler = logging.FileHandler(dm.DATA_DIR / "dedup_tui.log", encoding="utf-8")
    except OSError:
        handler = logging.NullHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().handlers[:] = [handler]
    try:
        curses.wrapper(_main, model_alias)
    except KeyboardInterrupt:
        print("Прервано (Ctrl+C). Журналы прогонов — в data/dedup_runs/.")
    return 0
