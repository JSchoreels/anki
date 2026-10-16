# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""kuma3: the Anki XP game's rules, a 1:1 port of kuma3's XpGame.kt (AnkiDroid fork, xp/XpGame.kt), itself a port of
the kuma3 Skins "Anki XP" skin. No Qt, no collection: aqt/xp/review.py feeds it answers, aqt/xp/progress.py the
other devices' parts and the review streak.

Honest grading never costs anything: every grade earns 10 points (15 within the clock on Good/Easy) × the
multiplier and grows the combo; the combo breaks only when the reviewer closes or after 5 minutes on one card;
×1.5 after the daily goal; boss cards ×3; three date-seeded quests; trophies.

This device's part `mine` = {xp, record, goal: [reps, set at ms], days: {day: {reps, fast, best, xp, got, goal}},
ach: {id: ms}, bosses}, the same format as the phone's and kuma3 Skins' parts of the shared progress notes.
"""

from __future__ import annotations

import copy
import datetime
import math
import random
import string
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

QUEST_XP = 100
POINTS = 10  # any grade
FAST_POINTS = 15  # Good/Easy after a flip within the clock
IDLE_MS = 5 * 60_000  # this long on one card and the combo is over
MILESTONES = (10, 25, 50, 100)
LIMITS = (5, 8, 10, 15, 20)


def level(xp: int) -> int:
    """0-based level; level lv is 100·(lv+1) XP long."""
    return int(math.floor((math.sqrt(1 + 0.08 * xp) - 1) / 2))


def level_start(lv: int) -> int:
    """XP where level lv (0-based) starts: the inverse of level()."""
    return 50 * lv * (lv + 1)


@dataclass
class Day:
    reps: int = 0
    fast: int = 0
    best: int = 0
    xp: int = 0
    got: list[str] = field(default_factory=list)
    goal: int = 0


@dataclass
class Merged:
    """Every device's part added up (the phone's XpGame.Merged)."""

    ach: dict[str, int]
    bosses: int
    xp: int
    record: int
    goal: int
    day: str
    today: Day
    streak: int
    days: dict[str, Day]

    @property
    def reps(self) -> int:
        return self.today.reps

    def hit(self, k: str) -> bool:
        d = self.days.get(k)
        return d is not None and d.reps >= (d.goal if d.goal > 0 else self.goal)

    @property
    def goal_hit(self) -> bool:
        return self.hit(self.day)


@dataclass
class Quest:
    id: str
    text: str
    need: int
    have: int


@dataclass
class Trophy:
    id: str
    icon: str
    name: str
    desc: str
    ok: Callable[[Game], bool]


@dataclass
class Session:
    xp0: int
    start: int
    reps: int = 0
    again: int = 0
    best: int = 0
    think: int = 0
    think_n: int = 0


@dataclass
class Result:
    """What an answer did, for the screen and the sounds."""

    xp: int
    ease: int
    points: int
    mult: float
    broke_combo: int
    mult_up: bool
    boss: bool
    wins: list[tuple[str, str]]
    level_up: bool


TROPHIES: list[Trophy] = [
    Trophy("goal", "🎯", "Goal getter", "Reach the daily goal", lambda g: g.g.goal_hit),
    Trophy(
        "overtime",
        "⏱️",
        "Overtime",
        "Double the daily goal in a day",
        lambda g: g.g.reps >= 2 * g.g.goal,
    ),
    Trophy(
        "speed",
        "🏎️",
        "Speedy",
        "50 speed bonuses in a day",
        lambda g: g.g.today.fast >= 50,
    ),
    Trophy("combo10", "⚡", "On a roll", "A 10-rep combo", lambda g: g.g.record >= 10),
    Trophy(
        "combo25", "⚡", "Unstoppable", "A 25-rep combo", lambda g: g.g.record >= 25
    ),
    Trophy("combo50", "🌩️", "Lightning", "A 50-rep combo", lambda g: g.g.record >= 50),
    Trophy(
        "combo100",
        "💯",
        "Hundred in a row",
        "A 100-rep combo",
        lambda g: g.g.record >= 100,
    ),
    Trophy("streak7", "🔥", "One week", "A 7-day streak", lambda g: g.g.streak >= 7),
    Trophy(
        "streak30", "🔥", "One month", "A 30-day streak", lambda g: g.g.streak >= 30
    ),
    Trophy(
        "streak100",
        "🔥",
        "Hundred days",
        "A 100-day streak",
        lambda g: g.g.streak >= 100,
    ),
    Trophy(
        "reps1000",
        "📚",
        "Thousand",
        "1,000 reps here",
        lambda g: g.total_reps() >= 1000,
    ),
    Trophy(
        "reps10000",
        "🏛️",
        "Ten thousand",
        "10,000 reps here",
        lambda g: g.total_reps() >= 10000,
    ),
    Trophy(
        "level10", "⭐", "Level 10", "Reach level 10", lambda g: level(g.g.xp) + 1 >= 10
    ),
    Trophy(
        "level25", "🌟", "Level 25", "Reach level 25", lambda g: level(g.g.xp) + 1 >= 25
    ),
    Trophy(
        "perfect", "💎", "Clean sweep", "Finish a sitting of 20+ reps", lambda g: False
    ),
    Trophy("boss", "👑", "Boss slayer", "Beat a boss card", lambda g: g.g.bosses >= 1),
    Trophy(
        "boss10", "👑", "Boss hunter", "Beat 10 boss cards", lambda g: g.g.bosses >= 10
    ),
]


class Game:
    """One device's game. [store] is a JSON-able dict kept by the caller (the profile): mine, bosses, device,
    limit, sound, clockHint."""

    def __init__(
        self, store: dict[str, Any], now_ms: Callable[[], int] | None = None
    ) -> None:
        self.store = store
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))
        if not store.get("device"):
            tail = "".join(
                random.choice(string.ascii_lowercase + string.digits) for _ in range(6)
            )
            store["device"] = f"kuma3-desktop-{tail}"
        store.setdefault("mine", {})
        store.setdefault("bosses", {})
        self.others: dict[str, dict] = {}
        self.review_streak = 0
        self.reviewed_today = False
        self.longest_streak = 0
        self.combo = 0
        self.record_shown = False
        self.session: Session | None = None
        self._answers: deque[tuple[int, dict, dict, int]] = deque(maxlen=50)
        self.g: Merged = self.merge()

    # ------------------------------------------------------------ settings

    @property
    def device(self) -> str:
        return self.store["device"]

    @property
    def mine(self) -> dict:
        return self.store["mine"]

    @mine.setter
    def mine(self, v: dict) -> None:
        self.store["mine"] = v

    @property
    def limit(self) -> int:
        """The speed bonus clock in seconds."""
        return int(self.store.get("limit", 10))

    @property
    def sound(self) -> bool:
        return bool(self.store.get("sound", True))

    # ------------------------------------------------------------ days, merge

    def day_key(self, back: int = 0) -> str:
        """Anki's day starts at 4 am; the skin's format ("2026-10-14", no zero padding)."""
        t = datetime.datetime.fromtimestamp(self.now_ms() / 1000) - datetime.timedelta(
            hours=4, days=back
        )
        return f"{t.year}-{t.month}-{t.day}"

    def merge(self) -> Merged:
        parts = [self.mine, *self.others.values()]
        days: dict[str, Day] = {}
        for p in parts:
            for k, v in (p.get("days") or {}).items():
                if not isinstance(v, dict):
                    continue
                d = days.setdefault(k, Day())
                d.reps += int(v.get("reps", 0))
                d.fast += int(v.get("fast", 0))
                d.xp += int(v.get("xp", 0))
                d.best = max(d.best, int(v.get("best", 0)))
                d.goal = max(d.goal, int(v.get("goal", 0)))
                for q in v.get("got") or []:
                    if q not in d.got:
                        d.got.append(q)
        goal, goal_at = 100, -1
        for p in parts:
            a = p.get("goal")
            if isinstance(a, list) and len(a) >= 2 and int(a[1]) > goal_at:
                goal, goal_at = int(a[0] or 100), int(a[1])
        day = self.day_key()
        ach: dict[str, int] = {}
        for p in parts:
            for k, v in (p.get("ach") or {}).items():
                ach[k] = min(ach.get(k, 2**62), int(v))
        # the review streak (as the heatmap counts it), plus today once reviewed here
        reps_here = int(((self.mine.get("days") or {}).get(day) or {}).get("reps", 0))
        streak = self.review_streak + (
            1 if not self.reviewed_today and reps_here > 0 else 0
        )
        return Merged(
            ach=ach,
            bosses=sum(int(p.get("bosses", 0)) for p in parts),
            xp=sum(int(p.get("xp", 0)) for p in parts),
            record=max(int(p.get("record", 0)) for p in parts),
            goal=goal,
            day=day,
            today=days.get(day, Day()),
            streak=streak,
            days=days,
        )

    def _today(self) -> dict:
        days = self.mine.setdefault("days", {})
        k = self.day_key()
        if k not in days:
            days[k] = {
                "reps": 0,
                "fast": 0,
                "best": 0,
                "xp": 0,
                "got": [],
                "goal": self.g.goal,
            }
        return days[k]

    def save(self) -> None:
        self.g = self.merge()

    def new_day(self) -> None:
        if self.g.day != self.day_key():
            self.g = self.merge()
            self.combo = 0

    def mult(self) -> float:
        return min(5, 1 + self.combo // 5) * (
            1.5 if self.g.reps >= self.g.goal else 1.0
        )

    def set_goal(self, goal: int) -> None:
        self.mine["goal"] = [max(10, goal), self.now_ms()]
        self._today()["goal"] = max(10, goal)
        self.save()

    def total_reps(self) -> int:
        return sum(d.reps for d in self.g.days.values())

    # ------------------------------------------------------------ quests, trophies

    def quests(self) -> list[Quest]:
        h = 7
        for c in self.g.day:
            h = (h * 31 + ord(c)) & 0xFFFFFFFF
        reps = round(self.g.goal * (1.2, 1.5, 2.0)[h % 3])
        combo = (15, 20, 25, 30)[h % 4]
        fast = round(self.g.goal * (0.4, 0.5, 0.6)[(h >> 3) % 3])
        return [
            Quest("reps", f"Do {reps} reps", reps, self.g.reps),
            Quest("combo", f"Reach a {combo}-rep combo", combo, self.g.today.best),
            Quest("fast", f"{fast} speed bonuses", fast, self.g.today.fast),
        ]

    def check_trophies(self, extra: tuple[str, ...] = ()) -> list[Trophy]:
        out = []
        for t in TROPHIES:
            if t.id in self.g.ach or not (t.id in extra or t.ok(self)):
                continue
            self.mine.setdefault("ach", {})[t.id] = self.now_ms()
            self.g = self.merge()
            out.append(t)
        return out

    # ------------------------------------------------------------ boss cards

    def set_boss(self, deck_id: int, card_id: int) -> None:
        """Per deck per day: its due card with the most lapses (progress.pick_boss finds it); 0 = none."""
        key = str(deck_id)
        if (self.store["bosses"].get(key) or {}).get("day") == self.day_key():
            return
        self.store["bosses"][key] = {
            "day": self.day_key(),
            "card": card_id,
            "beaten": False,
        }

    def needs_boss(self, deck_id: int) -> bool:
        return (self.store["bosses"].get(str(deck_id)) or {}).get(
            "day"
        ) != self.day_key()

    def is_boss(self, deck_id: int, card_id: int) -> bool:
        b = self.store["bosses"].get(str(deck_id)) or {}
        return (
            b.get("day") == self.day_key()
            and not b.get("beaten")
            and b.get("card") == card_id
        )

    def _beat_boss(self, deck_id: int) -> None:
        self.store["bosses"].setdefault(str(deck_id), {})["beaten"] = True
        self.mine["bosses"] = int(self.mine.get("bosses", 0)) + 1

    # ------------------------------------------------------------ scoring

    def first_clock(self) -> bool:
        """True once ever: the reviewer explains the speed bonus the first time."""
        if self.store.get("clockHint"):
            return False
        self.store["clockHint"] = True
        return True

    def start_session(self) -> None:
        self.session = Session(self.g.xp, self.now_ms())

    def break_combo(self) -> int:
        broke, self.combo, self.record_shown = self.combo, 0, False
        return broke

    def undo_answer(self) -> bool:
        """An undone "Answer Card": the game as it was before that answer (rep, XP, quests, boss)."""
        if not self._answers:
            return False
        ease, mine, bosses, combo = self._answers.pop()
        self.mine = mine
        self.store["bosses"] = bosses
        self.combo = combo
        if self.session:
            self.session.reps -= 1
            if ease == 1:
                self.session.again -= 1
        self.save()
        return True

    def score(
        self, ease: int, think_ms: int, idle: bool, deck_id: int, card_id: int
    ) -> Result:
        self.new_day()
        self._answers.append(
            (
                ease,
                copy.deepcopy(self.mine),
                copy.deepcopy(self.store["bosses"]),
                self.combo,
            )
        )
        fast = think_ms < self.limit * 1000 and ease >= 3
        lv = level(self.g.xp)
        m0 = self.mult()
        was_hit = self.g.goal_hit
        record = self.g.record
        first_today = self.g.reps == 0
        broke = self.break_combo() if idle else 0
        self.combo += 1
        boss = self.is_boss(deck_id, card_id)
        points = FAST_POINTS if fast else POINTS
        mult = self.mult() * (3 if boss else 1)
        xp = math.floor(
            points * mult + 0.5
        )  # the phone's Math.round (half up), not banker's
        t = self._today()
        t["reps"] = int(t.get("reps", 0)) + 1
        if fast:
            t["fast"] = int(t.get("fast", 0)) + 1
        t["best"] = max(int(t.get("best", 0)), self.combo)
        t["xp"] = int(t.get("xp", 0)) + xp
        t["goal"] = self.g.goal
        self.mine["xp"] = int(self.mine.get("xp", 0)) + xp
        self.mine["record"] = max(int(self.mine.get("record", 0)), self.combo)
        self.g = self.merge()
        if s := self.session:
            s.reps += 1
            s.again += ease == 1
            s.best = max(s.best, self.combo)
            if think_ms < 60000:
                s.think += think_ms
                s.think_n += 1
        wins: list[tuple[str, str]] = []
        if boss and ease > 1:
            self._beat_boss(deck_id)
            self.g = self.merge()
            wins.append(("👑 Boss defeated!", f"Triple XP: +{xp}."))
        elif boss:
            wins.append(
                (
                    "👑 The boss won this round",
                    f"Triple XP anyway: +{xp}. It comes back later today.",
                )
            )
        if self.combo in MILESTONES:
            wins.append((f"{self.combo} combo!", f"{self.combo} in a row. Keep going!"))
        if self.combo > record and not self.record_shown and record >= 5:
            wins.append(("New record!", f"Best combo ever: {self.combo} and counting."))
            self.record_shown = True
        if first_today:
            days = self.g.streak
            wins.append(
                (
                    f"🔥 {days}-day streak",
                    "Another day in a row. Keep it going!"
                    if days > 1
                    else "Day 1. Come back tomorrow!",
                )
            )
        if self.g.goal_hit and not was_hit:
            wins.append(("Daily goal done!", "Overtime: every rep now gives 1.5× XP."))
        for q in self.quests():
            if q.have < q.need or q.id in self.g.today.got:
                continue
            t.setdefault("got", []).append(q.id)
            t["xp"] = int(t.get("xp", 0)) + QUEST_XP
            self.mine["xp"] = int(self.mine.get("xp", 0)) + QUEST_XP
            self.g = self.merge()
            wins.append(("Quest complete!", f"{q.text} · +{QUEST_XP} XP"))
        level_up = level(self.g.xp) > lv
        if level_up:
            wins.append((f"Level {level(self.g.xp) + 1}!", "Keep the combo going."))
        for tr in self.check_trophies():
            wins.append((f"🏆 {tr.icon} {tr.name}", tr.desc))
        self.save()
        return Result(
            xp,
            ease,
            points,
            mult,
            broke,
            self.mult() > m0 and self.combo > 0,
            boss,
            wins,
            level_up,
        )

    def finish_session(self) -> list[tuple[str, str]]:
        """The reviewer closes: the combo ends; a sitting of 20+ reps without Again is a Clean sweep."""
        self.break_combo()
        s, self.session = self.session, None
        if s and s.reps >= 20 and s.again == 0:
            return [
                (f"🏆 {t.icon} {t.name}", t.desc)
                for t in self.check_trophies(("perfect",))
            ]
        return []


def _self_check() -> None:
    clock = [int(datetime.datetime(2026, 10, 15, 12).timestamp() * 1000)]
    g = Game({}, now_ms=lambda: clock[0])
    assert g.device.startswith("kuma3-desktop-")
    assert (
        level(0) == 0
        and level(99) == 0
        and level(100) == 1
        and level_start(1) == 100
        and level(level_start(5)) == 5
    )
    g.start_session()
    r = g.score(3, 2000, False, 1, 10)  # fast Good
    assert (r.points, r.xp, g.combo) == (15, 15, 1), r
    assert r.wins[0][0] == "🔥 1-day streak"
    r = g.score(1, 2000, False, 1, 11)  # Again: 10, never less, combo still grows
    assert (r.points, r.xp, g.combo) == (10, 10, 2), r
    r = g.score(2, 30000, False, 1, 12)  # slow Hard
    assert (r.points, r.xp) == (10, 10)
    for i in range(2):
        g.score(3, 99999, False, 1, 20 + i)
    assert g.combo == 5 and g.mult() == 2  # ×2 from 5 in a row
    r = g.score(3, 99999, False, 1, 30)
    assert r.xp == 20
    g.set_boss(1, 40)
    r = g.score(4, 1000, False, 1, 40)
    assert (
        r.boss
        and r.xp == 15 * 2 * 3
        and r.wins[0][0] == "👑 Boss defeated!"
        and not g.is_boss(1, 40)
    )
    xp_before, reps_before = g.g.xp, g.g.reps
    g.score(3, 1000, False, 1, 50)
    assert g.undo_answer() and (g.g.xp, g.g.reps) == (xp_before, reps_before)
    r = g.score(3, 1000, True, 1, 51)  # 5 min on one card: the combo breaks first
    assert r.broke_combo == 7 and g.combo == 1
    # merge: another device's part adds up; the newest goal wins
    g.others = {
        "phone": {
            "xp": 500,
            "record": 30,
            "goal": [50, clock[0] + 1],
            "days": {g.day_key(): {"reps": 40, "got": ["reps"]}},
        }
    }
    g.save()
    assert (
        g.g.xp == g.mine["xp"] + 500
        and g.g.record == 30
        and g.g.goal == 50
        and g.g.reps == g.mine["days"][g.day_key()]["reps"] + 40
    )
    assert "reps" in g.g.today.got
    # a new day: the combo resets, today starts empty
    clock[0] += 86_400_000
    g.new_day()
    assert g.combo == 0 and g.g.reps == 0
    print("aqt.xp.game self-check ok")


if __name__ == "__main__":
    _self_check()
