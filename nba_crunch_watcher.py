#!/usr/bin/env python3
"""
NBA Crunch-Time & Lakers Watcher (Adaptive Polling + Optional Gemini Summaries)
Monitors live NBA games via ESPN's public JSON APIs (pure Python 3.10+ stdlib).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable
from zoneinfo import ZoneInfo

SGT = ZoneInfo("Asia/Singapore")
UTC = ZoneInfo("UTC")

ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
ESPN_SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_GEMINI_MODEL = "gemini-4-hydrogen"
USER_AGENT = "NBACrunchWatcher/1.1 (+https://github.com/)"

LAKERS_ABBR = "LAL"
CLOSE_MARGIN_DEFAULT = 10
CRUNCH_POLL_SECONDS = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("nba_watcher")


@dataclass
class TeamSnapshot:
    team_id: str
    abbr: str
    name: str
    score: int
    linescores: list[int] = field(default_factory=list)


@dataclass
class GameSnapshot:
    game_id: str
    short_name: str
    start_time_utc: dt.datetime
    state: str
    status_name: str
    status_detail: str
    period: int
    clock_seconds: float
    display_clock: str
    away: TeamSnapshot
    home: TeamSnapshot

    @property
    def margin(self) -> int:
        return abs(self.home.score - self.away.score)

    @property
    def is_lakers(self) -> bool:
        return self.away.abbr == LAKERS_ABBR or self.home.abbr == LAKERS_ABBR

    @property
    def is_final(self) -> bool:
        return self.state == "post"

    @property
    def is_live(self) -> bool:
        return self.state == "in"

    @property
    def is_pre(self) -> bool:
        return self.state == "pre"

    @property
    def is_halftime(self) -> bool:
        return self.status_name == "STATUS_HALFTIME" or "halftime" in self.status_detail.lower()

    @property
    def is_end_of_period(self) -> bool:
        detail_l = self.status_detail.lower()
        return (
            self.status_name in {"STATUS_END_PERIOD", "STATUS_HALFTIME"}
            or detail_l.startswith("end of")
            or "halftime" in detail_l
        )

    def score_line(self) -> str:
        return f"{self.away.abbr} {self.away.score} - {self.home.score} {self.home.abbr}"


@dataclass
class GameTracker:
    crunch_alerted: bool = False
    crunch_final_sent: bool = False
    last_play_seq: int = 0
    last_lakers_period_sent: int = 0
    lakers_final_sent: bool = False


def http_get_json(url: str, timeout: int = 20, retries: int = 3) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_err = exc
            time.sleep(min(10, 2 ** attempt))
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def http_post(url: str, data: bytes, headers: dict[str, str], timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


class GeminiSummarizer:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        post_fn: Callable[[str, bytes, dict[str, str], int], bytes] = http_post,
    ) -> None:
        self.api_key = (api_key if api_key is not None else os.getenv("GEMINI_API_KEY", "")).strip()
        self.model = (model or os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)).strip()
        self.post_fn = post_fn

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def summarize(self, instruction: str, raw_context: str, fallback: str) -> str:
        if not self.enabled:
            return fallback
        url = f"{GEMINI_API_BASE}/{urllib.parse.quote(self.model)}:generateContent?key={urllib.parse.quote(self.api_key)}"
        prompt = (
            f"{instruction}\n\n"
            "Rules: Use ONLY the facts in the live data below. Do not invent stats, "
            "players, or plays. Keep it concise, energetic, and easy to scan on a phone "
            "(plain text, no markdown tables).\n\n"
            f"LIVE DATA:\n{raw_context}"
        )
        payload = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4, "maxOutputTokens": 260},
        }).encode("utf-8")
        try:
            raw = self.post_fn(url, payload, {"Content-Type": "application/json", "User-Agent": USER_AGENT}, 20)
            data = json.loads(raw.decode("utf-8"))
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(str(p.get("text", "")) for p in parts).strip()
            return text or fallback
        except Exception as exc:
            log.warning("Gemini summary failed (%s) — using built-in template.", exc)
            return fallback


class Notifier:
    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self.tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.tg_chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.discord_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
        self.ntfy_topic = os.getenv("NTFY_TOPIC", "").strip()
        self.ntfy_server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

    def send(self, title: str, body: str) -> None:
        full_msg = f"{title}\n{body}".strip()
        print("\n" + "=" * 60 + "\n" + full_msg + "\n" + "=" * 60 + "\n", flush=True)
        if self.dry_run:
            return
        if self.tg_token and self.tg_chat:
            try:
                payload = json.dumps({"chat_id": self.tg_chat, "text": full_msg, "disable_web_page_preview": True}).encode("utf-8")
                http_post(f"https://api.telegram.org/bot{self.tg_token}/sendMessage", payload, {"Content-Type": "application/json", "User-Agent": USER_AGENT})
            except Exception as exc:
                log.error("Telegram send failed: %s", exc)
        if self.discord_url:
            try:
                payload = json.dumps({"content": f"**{title}**\n{body}"[:1990]}).encode("utf-8")
                http_post(self.discord_url, payload, {"Content-Type": "application/json", "User-Agent": USER_AGENT})
            except Exception as exc:
                log.error("Discord send failed: %s", exc)
        if self.ntfy_topic:
            try:
                http_post(f"{self.ntfy_server}/{urllib.parse.quote(self.ntfy_topic)}", body.encode("utf-8"), {"Title": title.encode("ascii", "ignore").decode("ascii") or "NBA Alert", "User-Agent": USER_AGENT})
            except Exception as exc:
                log.error("ntfy send failed: %s", exc)


def parse_iso_utc(value: str) -> dt.datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(value)
    return (parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed).astimezone(UTC)


def parse_scoreboard(payload: dict[str, Any]) -> list[GameSnapshot]:
    games: list[GameSnapshot] = []
    for event in payload.get("events", []):
        comps = event.get("competitions", [])
        if not comps:
            continue
        comp = comps[0]
        status = comp.get("status") or event.get("status") or {}
        status_type = status.get("type") or {}
        away_team = home_team = None
        for c in comp.get("competitors", []):
            t = c.get("team") or {}
            linescores = [int(float(ls.get("value", 0))) for ls in c.get("linescores", [])]
            snap = TeamSnapshot(str(t.get("id", "")), str(t.get("abbreviation", "???")), str(t.get("displayName", "Unknown")), int(c.get("score", 0) or 0), linescores)
            if c.get("homeAway") == "home":
                home_team = snap
            else:
                away_team = snap
        if not away_team or not home_team:
            continue
        games.append(GameSnapshot(
            str(event.get("id", "")), str(event.get("shortName", f"{away_team.abbr} @ {home_team.abbr}")),
            parse_iso_utc(event.get("date", dt.datetime.now(UTC).isoformat())),
            str(status_type.get("state", "pre")), str(status_type.get("name", "")),
            str(status_type.get("shortDetail", status_type.get("detail", ""))),
            int(status.get("period", 0) or 0), float(status.get("clock", 0.0) or 0.0),
            str(status.get("displayClock", "0:00")), away_team, home_team,
        ))
    return games


def fetch_scoreboard(date_yyyymmdd: str | None = None) -> list[GameSnapshot]:
    url = f"{ESPN_SCOREBOARD_URL}?dates={date_yyyymmdd}" if date_yyyymmdd else ESPN_SCOREBOARD_URL
    return parse_scoreboard(http_get_json(url))


def fetch_game_summary(game_id: str) -> dict[str, Any]:
    return http_get_json(f"{ESPN_SUMMARY_URL}?event={game_id}")


def extract_top_performers(summary: dict[str, Any], top_n: int = 2) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for group in summary.get("boxscore", {}).get("players", []):
        abbr = group.get("team", {}).get("abbreviation", "???")
        stat_blocks = group.get("statistics", [])
        if not stat_blocks:
            continue
        labels = [lbl.upper() for lbl in stat_blocks[0].get("labels", [])]
        idx = {lbl: i for i, lbl in enumerate(labels)}
        parsed: list[tuple[int, int, str]] = []
        for entry in stat_blocks[0].get("athletes", []):
            if entry.get("didNotPlay"):
                continue
            stats = entry.get("stats", [])
            if not stats:
                continue
            get_stat = lambda key: stats[idx[key]] if key in idx and idx[key] < len(stats) else "0"
            try:
                pts = int(get_stat("PTS"))
                reb = int(get_stat("REB"))
                ast = int(get_stat("AST"))
            except ValueError:
                pts = reb = ast = 0
            name = entry.get("athlete", {}).get("shortName") or entry.get("athlete", {}).get("displayName", "Player")
            parsed.append((pts, reb + ast, f"{name}: {pts} PTS, {reb} REB, {ast} AST ({get_stat('FG')} FG)"))
        parsed.sort(key=lambda x: (x[0], x[1]), reverse=True)
        result[abbr] = [p[2] for p in parsed[:top_n]]
    return result


def detect_comeback_and_run(game: GameSnapshot, summary: dict[str, Any]) -> list[str]:
    plays = summary.get("plays", [])
    if not plays:
        return []
    notes: list[str] = []
    max_away_lead = max_home_lead = 0
    for p in plays:
        a, h = int(p.get("awayScore", 0) or 0), int(p.get("homeScore", 0) or 0)
        max_away_lead = max(max_away_lead, a - h)
        max_home_lead = max(max_home_lead, h - a)
    cur_diff = game.home.score - game.away.score
    if cur_diff >= -3 and max_away_lead >= 12:
        notes.append(f"Comeback alert: {game.home.abbr} trailed by {max_away_lead} earlier.")
    if cur_diff <= 3 and max_home_lead >= 12:
        notes.append(f"Comeback alert: {game.away.abbr} trailed by {max_home_lead} earlier.")
    recent = plays[-15:]
    if len(recent) >= 2:
        run_a = max(0, int(recent[-1].get("awayScore", 0) or 0) - int(recent[0].get("awayScore", 0) or 0))
        run_h = max(0, int(recent[-1].get("homeScore", 0) or 0) - int(recent[0].get("homeScore", 0) or 0))
        if run_a - run_h >= 8:
            notes.append(f"Hot stretch: {game.away.abbr} on a {run_a}-{run_h} run.")
        elif run_h - run_a >= 8:
            notes.append(f"Hot stretch: {game.home.abbr} on a {run_h}-{run_a} run.")
    return notes


def format_linescore(game: GameSnapshot) -> str:
    if not game.away.linescores or not game.home.linescores:
        return ""
    quarters = [f"Q{i+1}" if i < 4 else f"OT{i-3}" for i in range(max(len(game.away.linescores), len(game.home.linescores)))]
    header = "       " + " ".join(f"{q:>3}" for q in quarters) + " | TOT"
    away_row = f"{game.away.abbr:<5}: " + " ".join(f"{s:>3}" for s in game.away.linescores) + f" | {game.away.score:>3}"
    home_row = f"{game.home.abbr:<5}: " + " ".join(f"{s:>3}" for s in game.home.linescores) + f" | {game.home.score:>3}"
    return f"{header}\n{away_row}\n{home_row}"


def build_crunch_intro(game: GameSnapshot, summary: dict[str, Any], summarizer: GeminiSummarizer | None = None) -> tuple[str, str, int]:
    period_lbl = f"Q{game.period}" if game.period <= 4 else f"OT{game.period - 4}"
    title = f"CLOSE GAME ({period_lbl} {game.display_clock}): {game.score_line()}"
    lines = [ls] if (ls := format_linescore(game)) else []
    story_notes = detect_comeback_and_run(game, summary)
    if story_notes:
        lines.extend([""] + story_notes)
    performers = extract_top_performers(summary, top_n=2)
    if performers:
        lines.append("\nTop performers:")
        for abbr in (game.away.abbr, game.home.abbr):
            lines.extend(f"  - [{abbr}] {p}" for p in performers.get(abbr, []))
    fallback_body = "\n".join(lines)
    body = summarizer.summarize(
        "Write a punchy 3-4 sentence crunch-time alert telling a basketball fan why to tune into this close 4th-quarter/OT game right now: highlight the current score & clock, who is having a big game, and any comeback or hot run.",
        f"Game: {game.short_name} ({period_lbl} {game.display_clock})\n{fallback_body}",
        fallback_body,
    ) if (summarizer and summarizer.enabled) else fallback_body
    plays = summary.get("plays", [])
    return title, body, int(plays[-1].get("sequenceNumber", len(plays))) if plays else 0


def build_minute_update(game: GameSnapshot, summary: dict[str, Any], last_seq: int, summarizer: GeminiSummarizer | None = None) -> tuple[str, str, int]:
    plays = summary.get("plays", [])
    new_plays, newest_seq = [], last_seq
    for idx, p in enumerate(plays, start=1):
        seq = int(p.get("sequenceNumber", idx) or idx)
        if seq > last_seq:
            new_plays.append(p)
            newest_seq = max(newest_seq, seq)
    period_lbl = "FINAL" if game.is_final else (f"Q{game.period} {game.display_clock}" if game.period <= 4 else f"OT{game.period - 4} {game.display_clock}")
    title = f"{game.score_line()} ({period_lbl})"
    highlights = []
    for p in new_plays:
        text = str(p.get("text", "")).strip()
        if not text:
            continue
        is_scoring = bool(p.get("scoringPlay"))
        if is_scoring or any(k in text.lower() for k in ("steal", "block", "turnover", "timeout", "foul", "challenge")):
            clock = p.get("clock", {}).get("displayValue", "")
            score_tag = f" ({p.get('awayScore')}-{p.get('homeScore')})" if is_scoring else ""
            highlights.append(f"[{clock}] {text}{score_tag}")
    if not highlights:
        return title, "No scoring or major plays in the last minute (timeout / stoppage / missed shots).", newest_seq
    fallback_body = "Last minute:\n" + "\n".join(highlights[-8:])
    body = summarizer.summarize(
        "Summarize what happened in the previous minute of this close NBA game in 2-3 ultra-short bullet lines (who scored, key defensive plays/turnovers, and the current score/momentum).",
        f"Score: {game.score_line()} ({period_lbl})\n{fallback_body}",
        fallback_body,
    ) if (summarizer and summarizer.enabled) else fallback_body
    return title, body, newest_seq


def build_lakers_quarter_update(game: GameSnapshot, summary: dict[str, Any], period_done: int, summarizer: GeminiSummarizer | None = None) -> tuple[str, str]:
    label = "FINAL" if game.is_final else ("HALFTIME" if period_done == 2 else (f"END OF Q{period_done}" if period_done <= 4 else f"END OF OT{period_done - 4}"))
    title = f"LAKERS {label}: {game.score_line()}"
    lines = [ls] if (ls := format_linescore(game)) else []
    performers = extract_top_performers(summary, top_n=2)
    if performers:
        lines.append("\nLeaders:")
        for abbr in (game.away.abbr, game.home.abbr):
            lines.extend(f"  - [{abbr}] {p}" for p in performers.get(abbr, []))
    fallback_body = "\n".join(lines)
    body = summarizer.summarize(
        f"Write a crisp 2-3 sentence {label} recap for a Los Angeles Lakers fan: state how the Lakers are doing, who is leading for both teams, and the quarter trend.",
        f"Game: {game.short_name} ({label})\n{fallback_body}",
        fallback_body,
    ) if (summarizer and summarizer.enabled) else fallback_body
    return title, body


def seconds_until_wake_window(now_sgt: dt.datetime, wake_hour_sgt: int, ignore_wake_window: bool) -> float:
    if ignore_wake_window:
        return 0.0
    if now_sgt.weekday() < 5 and now_sgt.hour < wake_hour_sgt:
        return max(0.0, (now_sgt.replace(hour=wake_hour_sgt, minute=0, second=0, microsecond=0) - now_sgt).total_seconds())
    return 0.0


def compute_next_sleep_seconds(games: list[GameSnapshot], trackers: dict[str, GameTracker], now_utc: dt.datetime, close_margin: int, wake_hour_sgt: int, ignore_wake_window: bool) -> tuple[int, str]:
    if not games:
        return 0, "no_games"
    if all(g.is_final for g in games):
        return 0, "all_final"
    wake_wait = seconds_until_wake_window(now_utc.astimezone(SGT), wake_hour_sgt, ignore_wake_window)
    if wake_wait > 0:
        return int(wake_wait), f"waiting until {wake_hour_sgt:02d}:00 SGT"
    candidates: list[tuple[float, str]] = []
    for g in games:
        if g.is_final:
            continue
        tr = trackers.setdefault(g.game_id, GameTracker())
        if g.is_live and (tr.crunch_alerted or (g.period >= 4 and g.margin <= close_margin)):
            return CRUNCH_POLL_SECONDS, f"crunch watch ({g.short_name})"
        if g.is_pre:
            offset = 20 * 60 if g.is_lakers else 75 * 60
            candidates.append((max(120.0, (g.start_time_utc - now_utc).total_seconds() + offset), f"waiting for {g.short_name}"))
        elif g.is_live and g.is_lakers:
            if g.is_halftime:
                candidates.append((12 * 60.0, f"Lakers halftime ({g.short_name})"))
            elif g.is_end_of_period:
                candidates.append((180.0, f"Lakers break ({g.short_name})"))
            else:
                est = g.clock_seconds * 1.8
                candidates.append((60.0 if est <= 180 else min(15 * 60.0, max(120.0, est - 150.0)), f"Lakers Q{g.period}"))
        elif g.is_live:
            if g.period >= 4:
                candidates.append((210.0, f"Q4 blowout ({g.short_name})"))
            elif g.is_halftime:
                candidates.append((28 * 60.0, f"halftime ({g.short_name})"))
            elif g.period == 1:
                candidates.append((40 * 60.0, f"Q1 chill ({g.short_name})"))
            elif g.period == 2:
                candidates.append((30 * 60.0, f"Q2 chill ({g.short_name})"))
            elif g.period == 3:
                est = g.clock_seconds * 1.8
                candidates.append((90.0 if est <= 180 else min(18 * 60.0, max(120.0, est - 150.0)), f"Q3 ({g.short_name})"))
    if not candidates:
        return 300, "fallback"
    best_wait, best_reason = min(candidates, key=lambda x: x[0])
    return max(60, int(best_wait)), best_reason


def process_games(games: list[GameSnapshot], trackers: dict[str, GameTracker], notifier: Notifier, close_margin: int, summary_fetcher=fetch_game_summary, summarizer: GeminiSummarizer | None = None) -> None:
    for g in games:
        tr = trackers.setdefault(g.game_id, GameTracker())
        summary_cache: dict[str, Any] | None = None
        def get_summary() -> dict[str, Any]:
            nonlocal summary_cache
            if summary_cache is None:
                summary_cache = summary_fetcher(g.game_id)
            return summary_cache
        if g.is_lakers:
            if g.is_final and not tr.lakers_final_sent:
                notifier.send(*build_lakers_quarter_update(g, get_summary(), g.period, summarizer))
                tr.lakers_final_sent = True
                tr.last_lakers_period_sent = max(tr.last_lakers_period_sent, g.period)
            elif g.is_live:
                completed = g.period if (g.is_end_of_period and g.period >= 1) else (g.period - 1 if g.period > 1 and (g.period - 1) > tr.last_lakers_period_sent else 0)
                if completed > tr.last_lakers_period_sent:
                    notifier.send(*build_lakers_quarter_update(g, get_summary(), completed, summarizer))
                    tr.last_lakers_period_sent = completed
        if g.is_live and g.period >= 4:
            if not tr.crunch_alerted and g.margin <= close_margin:
                title, body, last_seq = build_crunch_intro(g, get_summary(), summarizer)
                notifier.send(title, body)
                tr.crunch_alerted, tr.last_play_seq = True, last_seq
            elif tr.crunch_alerted:
                title, body, newest_seq = build_minute_update(g, get_summary(), tr.last_play_seq, summarizer)
                notifier.send(title, body)
                tr.last_play_seq = newest_seq
        elif g.is_final and tr.crunch_alerted and not tr.crunch_final_sent:
            title, body, newest_seq = build_minute_update(g, get_summary(), tr.last_play_seq, summarizer)
            notifier.send(title, body)
            tr.last_play_seq, tr.crunch_final_sent = newest_seq, True


def run_watcher(args: argparse.Namespace) -> None:
    notifier = Notifier(dry_run=args.dry_run)
    summarizer = GeminiSummarizer(model=args.gemini_model)
    trackers: dict[str, GameTracker] = {}
    while True:
        wake_wait = seconds_until_wake_window(dt.datetime.now(UTC).astimezone(SGT), args.wake_hour, args.ignore_wake_window)
        if wake_wait > 0:
            time.sleep(wake_wait)
            continue
        try:
            games = fetch_scoreboard(args.date)
        except Exception as exc:
            log.error("Scoreboard fetch error: %s", exc)
            time.sleep(120)
            continue
        process_games(games, trackers, notifier, close_margin=args.margin, summarizer=summarizer)
        sleep_secs, reason = compute_next_sleep_seconds(games, trackers, dt.datetime.now(UTC), args.margin, args.wake_hour, args.ignore_wake_window)
        if sleep_secs <= 0:
            log.info("Done for the day (%s).", reason)
            break
        log.info("Next check in %.1f min (%s).", sleep_secs / 60.0, reason)
        time.sleep(sleep_secs)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Adaptive NBA Close-Game & Lakers Watcher")
    p.add_argument("--date", help="Optional ESPN slate date (YYYYMMDD)")
    p.add_argument("--margin", type=int, default=CLOSE_MARGIN_DEFAULT)
    p.add_argument("--wake-hour", type=int, default=10)
    p.add_argument("--ignore-wake-window", action="store_true")
    p.add_argument("--gemini-model", default=os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL))
    p.add_argument("--dry-run", action="store_true")
    try:
        run_watcher(p.parse_args())
    except KeyboardInterrupt:
        sys.exit(0)
