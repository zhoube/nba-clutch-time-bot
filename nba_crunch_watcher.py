#!/usr/bin/env python3
"""
NBA Crunch-Time & Lakers Watcher (Adaptive Polling + Beat-Writer Gemini Summaries + Win Prob + Logos)
=====================================================================================================
Monitors live NBA games using ESPN's public JSON scoreboard & game summary APIs.
Includes ESPN Analytics live Win Probability, personal foul-trouble & team bonus tracking,
unfiltered minute-by-minute play-by-play with [TEAM] tags, combined multi-game digests,
and real ESPN team-logo matchup banners via Pillow (optional; falls back to text-only).
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import io
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable
from zoneinfo import ZoneInfo

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = ImageDraw = ImageFont = None  # type: ignore[assignment]

SGT = ZoneInfo("Asia/Singapore")
UTC = ZoneInfo("UTC")

ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
ESPN_SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_GEMINI_MODEL = "gemini-4-hydrogen"
USER_AGENT = "NBACrunchWatcher/3.0 (+https://github.com/)"

# Built-in Telegram topic target: "Sarah and Kelvin" supergroup -> "NBA" topic
BUILTIN_TELEGRAM_TARGETS = ["-1002615344601:270"]

LAKERS_ABBR = "LAL"
CLOSE_MARGIN_DEFAULT = 10
CRUNCH_POLL_SECONDS = 60

# Official NBA Rule 5 intermission lengths: 15 min halftime; 2:30 local (3:30 national TV) quarter/OT breaks
HALFTIME_SLEEP_SECONDS = 15 * 60
QUARTER_BREAK_SLEEP_SECONDS = 150  # 2.5 minutes
MAX_IDLE_WAIT_SECONDS = 4 * 3600  # Exit cleanly if no game is live and next tip is >4h away

TEAM_EMOJI: dict[str, str] = {
    "ATL": "🦅", "BOS": "☘️", "BKN": "🕸️", "CHA": "🐝", "CHI": "🐂",
    "CLE": "⚔️", "DAL": "🐴", "DEN": "⛏️", "DET": "⚙️", "GS": "🌉",
    "GSW": "🌉", "HOU": "🚀", "IND": "🏎️", "LAC": "⛵", "LAL": "💜💛",
    "MEM": "🐻", "MIA": "🔥", "MIL": "🦌", "MIN": "🐺", "NO": "⚜️",
    "NOP": "⚜️", "NY": "🗽", "NYK": "🗽", "OKC": "⚡", "ORL": "🪄",
    "PHI": "🔔", "PHX": "☀️", "POR": "❤️", "SA": "🖤", "SAS": "🖤",
    "SAC": "👑", "TOR": "🦖", "UTA": "🎵", "UTAH": "🎵", "WAS": "🧙",
    "WSH": "🧙",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("nba_watcher")


def team_badge(abbr: str) -> str:
    return TEAM_EMOJI.get(abbr.upper(), "🏀")


@dataclass
class TeamSnapshot:
    team_id: str
    abbr: str
    name: str
    score: int
    linescores: list[int] = field(default_factory=list)
    record: str = ""
    logo_url: str = ""
    color_hex: str = ""


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

    def score_line(self, with_emojis: bool = True) -> str:
        if with_emojis:
            return f"{team_badge(self.away.abbr)} {self.away.abbr} {self.away.score} – {self.home.score} {self.home.abbr} {team_badge(self.home.abbr)}"
        return f"{self.away.abbr} {self.away.score} – {self.home.score} {self.home.abbr}"

    def lead_summary(self) -> str:
        if self.away.score == self.home.score:
            return f"Tied {self.away.score}-{self.home.score}"
        leader = self.home if self.home.score > self.away.score else self.away
        diff = self.margin
        poss = (diff + 2) // 3
        poss_lbl = "1-possession game" if poss == 1 else f"{poss}-possession game"
        return f"{leader.name} ({leader.abbr}) lead by {diff} ({poss_lbl})"


@dataclass
class GameTracker:
    crunch_alerted: bool = False
    crunch_final_sent: bool = False
    last_play_seq: int = 0
    last_lakers_period_sent: int = 0
    lakers_final_sent: bool = False
    last_home_win_pct: float | None = None


def http_get_bytes(url: str, timeout: int = 15, retries: int = 2) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            last_err = exc
            time.sleep(min(5, 2 ** attempt))
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


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


def strip_html_tags(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def sanitize_telegram_html(text: str) -> str:
    """Keep only Telegram-safe inline HTML tags (<b>, <i>, <code>) and escape stray <, >, &."""
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    placeholders: dict[str, str] = {}
    for idx, tag in enumerate(("<b>", "</b>", "<i>", "</i>", "<code>", "</code>")):
        token = f"__TG_TAG_{idx}__"
        placeholders[token] = tag
        text = text.replace(tag, token)
    text = html.escape(text, quote=False)
    for token, tag in placeholders.items():
        text = text.replace(token, tag)
    return text.strip()


def _parse_hex_color(hex_str: str, default: tuple[int, int, int]) -> tuple[int, int, int]:
    cleaned = hex_str.strip().lstrip("#")
    if len(cleaned) == 6:
        try:
            return int(cleaned[0:2], 16), int(cleaned[2:4], 16), int(cleaned[4:6], 16)
        except ValueError:
            pass
    return default


def _load_font(size: int, bold: bool = True) -> Any:
    if ImageFont is None:
        return None
    candidates = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]
        if bold
        else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]
    )
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass
    return ImageFont.load_default()


def render_matchup_banner(game: GameSnapshot, status_badge: str, win_prob_note: str = "") -> bytes | None:
    """Render a 900x320 broadcast-style matchup banner with real ESPN team PNG logos."""
    if Image is None or ImageDraw is None:
        return None
    try:
        w, h = 900, 320
        bg = (15, 23, 42)
        img = Image.new("RGBA", (w, h), bg + (255,))
        draw = ImageDraw.Draw(img)

        away_col = _parse_hex_color(game.away.color_hex, (59, 130, 246))
        home_col = _parse_hex_color(game.home.color_hex, (239, 68, 68))

        draw.rectangle([0, 0, w // 2, 10], fill=away_col + (255,))
        draw.rectangle([w // 2, 0, w, 10], fill=home_col + (255,))
        draw.rounded_rectangle([24, 26, w - 24, h - 24], radius=22, fill=(24, 34, 58, 255), outline=(51, 65, 85, 255), width=2)

        def fetch_logo(team: TeamSnapshot) -> Any:
            url = team.logo_url or f"https://a.espncdn.com/i/teamlogos/nba/500/{team.abbr.lower()}.png"
            raw = http_get_bytes(url, timeout=10, retries=2)
            logo = Image.open(io.BytesIO(raw)).convert("RGBA")
            logo.thumbnail((148, 148), Image.Resampling.LANCZOS)
            return logo

        away_logo = fetch_logo(game.away)
        home_logo = fetch_logo(game.home)

        img.alpha_composite(away_logo, (68 + (148 - away_logo.width) // 2, 54 + (148 - away_logo.height) // 2))
        img.alpha_composite(home_logo, (w - 68 - 148 + (148 - home_logo.width) // 2, 54 + (148 - home_logo.height) // 2))

        f_score = _load_font(68, bold=True)
        f_abbr = _load_font(28, bold=True)
        f_rec = _load_font(18, bold=False)
        f_badge = _load_font(22, bold=True)

        draw.text((142, 222), game.away.abbr, fill=(248, 250, 252), font=f_abbr, anchor="mm")
        if game.away.record:
            draw.text((142, 252), game.away.record, fill=(148, 163, 184), font=f_rec, anchor="mm")

        draw.text((w - 142, 222), game.home.abbr, fill=(248, 250, 252), font=f_abbr, anchor="mm")
        if game.home.record:
            draw.text((w - 142, 252), game.home.record, fill=(148, 163, 184), font=f_rec, anchor="mm")

        pill_text = status_badge.upper()
        pill_w = max(180, len(pill_text) * 14 + 44)
        px1, px2 = (w - pill_w) // 2, (w + pill_w) // 2
        pill_fill = (22, 163, 74, 255) if "FINAL" in pill_text else (220, 38, 38, 255)
        draw.rounded_rectangle([px1, 54, px2, 96], radius=21, fill=pill_fill)
        draw.text((w // 2, 75), pill_text, fill=(255, 255, 255), font=f_badge, anchor="mm")

        score_text = f"{game.away.score}  –  {game.home.score}"
        draw.text((w // 2, 162), score_text, fill=(255, 255, 255), font=f_score, anchor="mm")

        footer = win_prob_note or format_linescore_compact(game)
        if footer:
            draw.text((w // 2, 252), footer, fill=(203, 213, 225), font=f_rec, anchor="mm")

        out = io.BytesIO()
        img.convert("RGB").save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception as exc:
        log.warning("Banner render failed (%s) — sending text-only.", exc)
        return None


def _encode_multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    boundary = f"----NBACrunchBoundary{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for key, val in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{val}\r\n".encode("utf-8")
        )
    for key, (filename, content, mime) in files.items():
        header = (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"{key}\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8")
        parts.append(header + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


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

    def summarize(self, instruction: str, raw_context: str, fallback: str, max_tokens: int = 750) -> str:
        if not self.enabled:
            return fallback
        models_to_try = [self.model] + [
            m for m in ("gemini-3.8-flash", "gemini-3.5-flash-lite") if m != self.model
        ]
        prompt = (
            "You are a sharp NBA beat writer / League Pass play-by-play analyst writing live game updates for a Telegram group chat.\n\n"
            f"TASK:\n{instruction}\n\n"
            "VOICE, BASKETBALL IQ & FORMATTING RULES:\n"
            "- Write like an informed NBA beat writer—punchy, vivid, and natural. Use earned basketball terms and iconic NBA flavor "
            "when the play genuinely warrants it (e.g. 'Dame Time' on a clutch Damian Lillard three, 'And-1', 'dagger', 'in the bonus', 'foul trouble'), "
            "without greetings ('Yo', 'Hey'), bro-talk, or hype-bot spam.\n"
            "- MERGE linked plays into one clean bullet (e.g., a made layup + shooting foul at the same timestamp = one And-1 bullet; "
            "a shooting foul + the resulting free throws = one bullet; a steal + transition bucket = one bullet).\n"
            "- NEVER restate the header score or clock as a separate filler bullet, and NEVER overuse 'momentum'—only mention momentum on an actual run, lead change, or comeback surge.\n"
            "- BANNED clichés: 'Late-game battle', 'Trailing [Team]', 'Momentum check', 'Momentum & strategy', 'execution down the stretch', 'tightly contested stretch', 'crucial stoppage'.\n"
            "- ALWAYS call out personal foul trouble (Q1 ≥2, Q2 ≥3, Q3 ≥4, Q4/OT ≥5 PF) and team bonus situations when flagged in LIVE DATA.\n"
            "- Use ONLY facts from LIVE DATA (pay strict attention to [TEAM] tags and lead margin—never mix up which team scored or miscount possessions).\n"
            "- Format cleanly for Telegram using ONLY <b>bold</b>, <i>italic</i>, <code>code</code>, and • bullets. Use consistent <b>bold</b> lead-ins (no random italics for labels). No Markdown (** or #).\n\n"
            "GOOD EXAMPLES OF STYLE:\n"
            "• <b>Wemby And-1 & Clingan foul trouble:</b> Victor Wembanyama powers through Donovan Clingan for an And-1 layup to cut it to 96–91 — that's Clingan's 5th foul, putting him one away from fouling out.\n"
            "• <b>Dame Time:</b> Damian Lillard drills a 26-foot three off a Jrue Holiday feed to stretch Portland's lead to 104–102.\n\n"
            f"LIVE DATA:\n{raw_context}"
        )
        gen_cfg: dict[str, Any] = {
            "temperature": 0.65,
            "maxOutputTokens": max_tokens,
        }
        if "gemini-4" in self.model or "gemini-3" in self.model or "gemini-2.5" in self.model:
            gen_cfg["thinkingConfig"] = {"thinkingLevel": "low"}

        payload = json.dumps({
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": gen_cfg,
        }).encode("utf-8")

        last_exc: Exception | None = None
        for model_name in models_to_try:
            url = f"{GEMINI_API_BASE}/{urllib.parse.quote(model_name)}:generateContent?key={urllib.parse.quote(self.api_key)}"
            try:
                raw = self.post_fn(url, payload, {"Content-Type": "application/json", "User-Agent": USER_AGENT}, 35)
                data = json.loads(raw.decode("utf-8"))
                parts = data["candidates"][0]["content"]["parts"]
                text = "".join(str(p.get("text", "")) for p in parts if not p.get("thought")).strip()
                if text:
                    return sanitize_telegram_html(text)
            except Exception as exc:
                last_exc = exc
                err_s = str(exc).lower()
                if any(code in err_s for code in ("429", "503", "404", "timed out")):
                    log.info("Gemini model %s unavailable (%s); trying fallback model.", model_name, exc)
                    continue
                break
        log.warning("Gemini summary failed (%s) — using built-in template.", last_exc)
        return fallback


def parse_telegram_targets(raw_env: str, builtins: list[str], group_only: bool = True) -> list[tuple[str, int | None]]:
    combined: list[str] = []
    for item in [x.strip() for x in raw_env.split(",") if x.strip()] + list(builtins):
        chat_part = item.split(":", 1)[0].strip()
        if group_only and not chat_part.startswith("-"):
            continue
        if item not in combined:
            combined.append(item)
    targets: list[tuple[str, int | None]] = []
    for entry in combined:
        if ":" in entry:
            chat_id, thread_str = entry.split(":", 1)
            try:
                targets.append((chat_id.strip(), int(thread_str.strip())))
            except ValueError:
                targets.append((chat_id.strip(), None))
        else:
            targets.append((entry, None))
    return targets


class Notifier:
    def __init__(
        self,
        dry_run: bool = False,
        post_fn: Callable[[str, bytes, dict[str, str], int], bytes] = http_post,
    ) -> None:
        self.dry_run = dry_run
        self.post_fn = post_fn
        raw_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip().rstrip("/")
        if raw_token.lower().startswith("bot") and ":" in raw_token:
            raw_token = raw_token[3:]
        self.tg_token = raw_token
        self.tg_targets = parse_telegram_targets(
            os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            BUILTIN_TELEGRAM_TARGETS,
        )
        self.discord_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
        self.ntfy_topic = os.getenv("NTFY_TOPIC", "").strip()
        self.ntfy_server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

    def _send_tg_text(self, chat_id: str, thread_id: int | None, html_msg: str, plain_msg: str) -> None:
        msg_data: dict[str, Any] = {
            "chat_id": chat_id,
            "text": html_msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if thread_id is not None:
            msg_data["message_thread_id"] = thread_id
        try:
            self.post_fn(
                f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                json.dumps(msg_data).encode("utf-8"),
                {"Content-Type": "application/json", "User-Agent": USER_AGENT},
                20,
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 400:
                msg_data.pop("parse_mode", None)
                msg_data["text"] = plain_msg
                try:
                    self.post_fn(
                        f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                        json.dumps(msg_data).encode("utf-8"),
                        {"Content-Type": "application/json", "User-Agent": USER_AGENT},
                        20,
                    )
                    return
                except Exception as retry_exc:
                    log.error("Telegram plain fallback failed for %s (thread=%s): %s", chat_id, thread_id, retry_exc)
                    return
            log.error("Telegram send failed for %s (thread=%s): %s", chat_id, thread_id, exc)
        except Exception as exc:
            log.error("Telegram send failed for %s (thread=%s): %s", chat_id, thread_id, exc)

    def _send_tg_photo(self, chat_id: str, thread_id: int | None, photo_bytes: bytes, caption_html: str, caption_plain: str) -> bool:
        fields: dict[str, str] = {"chat_id": chat_id, "caption": caption_html, "parse_mode": "HTML"}
        if thread_id is not None:
            fields["message_thread_id"] = str(thread_id)
        body_bytes, content_type = _encode_multipart(fields, {"photo": ("matchup.png", photo_bytes, "image/png")})
        try:
            self.post_fn(
                f"https://api.telegram.org/bot{self.tg_token}/sendPhoto",
                body_bytes,
                {"Content-Type": content_type, "User-Agent": USER_AGENT},
                25,
            )
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 400:
                fields.pop("parse_mode", None)
                fields["caption"] = caption_plain[:1020]
                body_bytes, content_type = _encode_multipart(fields, {"photo": ("matchup.png", photo_bytes, "image/png")})
                try:
                    self.post_fn(
                        f"https://api.telegram.org/bot{self.tg_token}/sendPhoto",
                        body_bytes,
                        {"Content-Type": content_type, "User-Agent": USER_AGENT},
                        25,
                    )
                    return True
                except Exception as retry_exc:
                    log.error("Telegram photo plain fallback failed for %s (thread=%s): %s", chat_id, thread_id, retry_exc)
                    return False
            log.error("Telegram photo send failed for %s (thread=%s): %s", chat_id, thread_id, exc)
            return False
        except Exception as exc:
            log.error("Telegram photo send failed for %s (thread=%s): %s", chat_id, thread_id, exc)
            return False

    def send(self, title: str, body: str, photo_bytes: bytes | None = None) -> None:
        html_title = f"<b>{html.escape(title, quote=False)}</b>"
        html_msg = f"{html_title}\n\n{body}".strip()
        plain_msg = f"{title}\n\n{strip_html_tags(body)}".strip()
        banner_tag = " [+LOGO BANNER]" if photo_bytes else ""
        print("\n" + "=" * 60 + banner_tag + "\n" + plain_msg + "\n" + "=" * 60 + "\n", flush=True)
        if self.dry_run:
            return
        if self.tg_token and self.tg_targets:
            for chat_id, thread_id in self.tg_targets:
                if photo_bytes:
                    if len(html_msg) <= 1020:
                        ok = self._send_tg_photo(chat_id, thread_id, photo_bytes, html_msg, plain_msg)
                        if not ok:
                            self._send_tg_text(chat_id, thread_id, html_msg, plain_msg)
                    else:
                        self._send_tg_photo(chat_id, thread_id, photo_bytes, html_title, title)
                        self._send_tg_text(chat_id, thread_id, body, strip_html_tags(body))
                else:
                    self._send_tg_text(chat_id, thread_id, html_msg, plain_msg)
        if self.discord_url:
            try:
                payload = json.dumps({"content": f"**{title}**\n{strip_html_tags(body)}"[:1990]}).encode("utf-8")
                self.post_fn(self.discord_url, payload, {"Content-Type": "application/json", "User-Agent": USER_AGENT}, 20)
            except Exception as exc:
                log.error("Discord send failed: %s", exc)
        if self.ntfy_topic:
            try:
                self.post_fn(
                    f"{self.ntfy_server}/{urllib.parse.quote(self.ntfy_topic)}",
                    strip_html_tags(body).encode("utf-8"),
                    {"Title": title.encode("ascii", "ignore").decode("ascii") or "NBA Alert", "User-Agent": USER_AGENT},
                    20,
                )
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
            records = c.get("records") or []
            rec_str = str(records[0].get("summary", "")) if records else ""
            snap = TeamSnapshot(
                str(t.get("id", "")),
                str(t.get("abbreviation", "???")),
                str(t.get("displayName", "Unknown")),
                int(c.get("score", 0) or 0),
                linescores,
                rec_str,
                str(t.get("logo", "")),
                str(t.get("color", "")),
            )
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


def extract_win_probability(
    game: GameSnapshot,
    summary: dict[str, Any],
    prev_home_pct: float | None = None,
) -> tuple[float | None, str, str]:
    """Return (home_pct, html_line, plain_banner_note) from ESPN Analytics winprobability."""
    wp = summary.get("winprobability") or []
    if not wp:
        return None, "", ""
    try:
        home_pct = float(wp[-1].get("homeWinPercentage", 0.5))
    except (TypeError, ValueError):
        return None, "", ""
    away_pct = max(0.0, min(1.0, 1.0 - home_pct))
    h_int = round(home_pct * 100)
    a_int = 100 - h_int

    delta_str = ""
    if prev_home_pct is not None:
        if home_pct >= away_pct:
            diff = round((home_pct - prev_home_pct) * 100)
        else:
            diff = round((away_pct - (1.0 - prev_home_pct)) * 100)
        if abs(diff) >= 2:
            sign = "+" if diff > 0 else ""
            delta_str = f" ({sign}{diff}%)"

    if home_pct >= away_pct:
        leader_part = f"{game.home.abbr} {h_int}%{delta_str} | {game.away.abbr} {a_int}%"
    else:
        leader_part = f"{game.away.abbr} {a_int}%{delta_str} | {game.home.abbr} {h_int}%"

    html_line = f"📊 <b>Win Prob (ESPN):</b> {html.escape(leader_part)}"
    banner_note = f"Win Prob (ESPN): {game.away.abbr} {a_int}%  |  {game.home.abbr} {h_int}%"
    return home_pct, html_line, banner_note


def extract_peak_win_prob_swing(game: GameSnapshot, summary: dict[str, Any]) -> str:
    wp = summary.get("winprobability") or []
    if len(wp) < 10:
        return ""
    home_vals = [float(x.get("homeWinPercentage", 0.5)) for x in wp if x.get("homeWinPercentage") is not None]
    if not home_vals:
        return ""
    cur_home = home_vals[-1]
    max_home = max(home_vals)
    max_away = max(1.0 - v for v in home_vals)
    if cur_home < 0.55 and max_home >= 0.75:
        return f"Win prob swing: {game.home.name} peaked at {round(max_home * 100)}% win probability earlier."
    if (1.0 - cur_home) < 0.55 and max_away >= 0.75:
        return f"Win prob swing: {game.away.name} peaked at {round(max_away * 100)}% win probability earlier."
    return ""


def foul_trouble_threshold(period: int) -> int:
    """Kelvin's foul-trouble rules: Q1>=2, Q2>=3, Q3>=4, Q4/OT>=5."""
    if period <= 1:
        return 2
    if period == 2:
        return 3
    if period == 3:
        return 4
    return 5


def extract_foul_trouble(game: GameSnapshot, summary: dict[str, Any]) -> list[str]:
    thresh = foul_trouble_threshold(game.period)
    alerts: list[str] = []
    for group in summary.get("boxscore", {}).get("players", []):
        abbr = group.get("team", {}).get("abbreviation", "???")
        stat_blocks = group.get("statistics", [])
        if not stat_blocks:
            continue
        labels = [lbl.upper() for lbl in stat_blocks[0].get("labels", [])]
        if "PF" not in labels:
            continue
        pf_idx = labels.index("PF")
        for entry in stat_blocks[0].get("athletes", []):
            if entry.get("didNotPlay"):
                continue
            stats = entry.get("stats", [])
            if pf_idx >= len(stats):
                continue
            try:
                pf = int(stats[pf_idx])
            except ValueError:
                continue
            if pf >= thresh:
                name = entry.get("athlete", {}).get("displayName") or entry.get("athlete", {}).get("shortName", "Player")
                note = "FOULED OUT (6 PF)" if pf >= 6 else (f"{pf} PF — 1 away from fouling out" if pf == 5 else f"{pf} PF in Q{game.period}")
                alerts.append(f"[{abbr}] {name}: {note}")
    return alerts


def compute_team_fouls_status(game: GameSnapshot, summary: dict[str, Any]) -> str:
    """Count current-quarter team fouls (excluding offensive/technical/def-3s) & flag NBA Bonus."""
    plays = summary.get("plays", [])
    if not plays or game.period <= 0:
        return ""
    id_to_abbr = {game.away.team_id: game.away.abbr, game.home.team_id: game.home.abbr}
    q_fouls = {game.away.abbr: 0, game.home.abbr: 0}
    last2_fouls = {game.away.abbr: 0, game.home.abbr: 0}

    for p in plays:
        if int(p.get("period", {}).get("number", 0) or 0) != game.period:
            continue
        txt = str(p.get("text", "")).lower()
        if "foul" not in txt:
            continue
        if any(skip in txt for skip in ("offensive", "technical", "defensive 3", "double technical", "delay of game")):
            continue
        tid = str(p.get("team", {}).get("id", ""))
        abbr = id_to_abbr.get(tid)
        if not abbr:
            continue
        q_fouls[abbr] += 1
        clk_val = p.get("clock", {}).get("value")
        if clk_val is not None:
            try:
                if float(clk_val) <= 120.0:
                    last2_fouls[abbr] += 1
            except (TypeError, ValueError):
                pass

    def fmt_side(committing: str, beneficiary: str) -> str:
        cnt = q_fouls[committing]
        l2 = last2_fouls[committing]
        if cnt >= 5 or l2 >= 2:
            return f"{committing} {cnt} ({beneficiary} IN THE BONUS)"
        if cnt == 4 or (game.clock_seconds <= 120.0 and l2 == 1):
            return f"{committing} {cnt} (1 away from {beneficiary} bonus)"
        return f"{committing} {cnt}"

    p_lbl = f"Q{game.period}" if game.period <= 4 else f"OT{game.period - 4}"
    return f"Team Fouls ({p_lbl}): {fmt_side(game.away.abbr, game.home.abbr)} | {fmt_side(game.home.abbr, game.away.abbr)}"


def extract_top_performers(summary: dict[str, Any], top_n: int = 3) -> dict[str, list[str]]:
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
            name = entry.get("athlete", {}).get("displayName") or entry.get("athlete", {}).get("shortName", "Player")
            fg = get_stat("FG")
            tp = get_stat("3PT")
            pf = get_stat("PF")
            extra_parts = []
            if tp and tp != "0-0":
                extra_parts.append(f"{tp} 3PT")
            if pf and pf != "0":
                extra_parts.append(f"{pf} PF")
            extra = (", " + ", ".join(extra_parts)) if extra_parts else ""
            parsed.append((pts, reb + ast, f"{name}: {pts} PTS, {reb} REB, {ast} AST ({fg} FG{extra})"))
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
        notes.append(f"Comeback alert: {game.home.name} trailed by as many as {max_away_lead} earlier.")
    if cur_diff <= 3 and max_home_lead >= 12:
        notes.append(f"Comeback alert: {game.away.name} trailed by as many as {max_home_lead} earlier.")
    recent = plays[-15:]
    if len(recent) >= 2:
        run_a = max(0, int(recent[-1].get("awayScore", 0) or 0) - int(recent[0].get("awayScore", 0) or 0))
        run_h = max(0, int(recent[-1].get("homeScore", 0) or 0) - int(recent[0].get("homeScore", 0) or 0))
        if run_a - run_h >= 8:
            notes.append(f"Hot stretch: {game.away.name} on a {run_a}-{run_h} run.")
        elif run_h - run_a >= 8:
            notes.append(f"Hot stretch: {game.home.name} on a {run_h}-{run_a} run.")
    wp_swing = extract_peak_win_prob_swing(game, summary)
    if wp_swing:
        notes.append(wp_swing)
    return notes


def format_play_with_team(game: GameSnapshot, play: dict[str, Any]) -> str:
    """Format an ESPN play with explicit [TEAM] tag and clock/score so Gemini never misattributes a play."""
    text = str(play.get("text", "")).strip()
    if not text:
        return ""
    id_to_abbr = {game.away.team_id: game.away.abbr, game.home.team_id: game.home.abbr}
    tid = str(play.get("team", {}).get("id", ""))
    team_tag = f"[{id_to_abbr[tid]}] " if tid in id_to_abbr else ""
    clock = play.get("clock", {}).get("displayValue", "")
    qtr = play.get("period", {}).get("number", "")
    score_tag = f" ({game.away.abbr} {play.get('awayScore')}-{play.get('homeScore')} {game.home.abbr})" if play.get("scoringPlay") else ""
    return f"[Q{qtr} {clock}] {team_tag}{text}{score_tag}"


def extract_recent_plays(game: GameSnapshot, summary: dict[str, Any], limit: int = 10) -> list[str]:
    plays = summary.get("plays", [])
    recent: list[str] = []
    for p in plays[-20:]:
        txt = str(p.get("text", "")).strip()
        if not txt or txt.lower().startswith("end of the"):
            continue
        formatted = format_play_with_team(game, p)
        if formatted:
            recent.append(formatted)
    return recent[-limit:]


def format_linescore_compact(game: GameSnapshot) -> str:
    if not game.away.linescores or not game.home.linescores:
        return ""
    a_str = "-".join(str(s) for s in game.away.linescores)
    h_str = "-".join(str(s) for s in game.home.linescores)
    return f"{game.away.abbr} ({a_str} = {game.away.score}) | {game.home.abbr} ({h_str} = {game.home.score})"


def format_linescore_code_block(game: GameSnapshot) -> str:
    """Clean aligned monospaced quarter-by-quarter block for Telegram HTML."""
    if not game.away.linescores or not game.home.linescores:
        return ""
    n_q = max(len(game.away.linescores), len(game.home.linescores))
    quarters = [f"Q{i+1}" if i < 4 else f"OT{i-3}" for i in range(n_q)]
    hdr = "     " + " ".join(f"{q:>3}" for q in quarters) + " | TOT"
    a_row = f"{game.away.abbr:<4} " + " ".join(f"{s:>3}" for s in game.away.linescores) + f" | {game.away.score:>3}"
    h_row = f"{game.home.abbr:<4} " + " ".join(f"{s:>3}" for s in game.home.linescores) + f" | {game.home.score:>3}"
    return f"<code>{html.escape(hdr)}\n{html.escape(a_row)}\n{html.escape(h_row)}</code>"


def is_pbp_behind_scoreboard(game: GameSnapshot, summary: dict[str, Any]) -> bool:
    """Detect ESPN sync lag where scoreboard has updated points not yet present in play-by-play."""
    plays = summary.get("plays", [])
    if not plays:
        return game.away.score > 0 or game.home.score > 0
    last = plays[-1]
    pbp_a = int(last.get("awayScore", 0) or 0)
    pbp_h = int(last.get("homeScore", 0) or 0)
    return pbp_a < game.away.score or pbp_h < game.home.score


def build_crunch_intro(
    game: GameSnapshot,
    summary: dict[str, Any],
    summarizer: GeminiSummarizer | None = None,
) -> tuple[str, str, int, bytes | None, float | None]:
    period_lbl = f"Q{game.period}" if game.period <= 4 else f"OT{game.period - 4}"
    title = f"🏀 Crunch Time ({period_lbl} {game.display_clock}) — {game.score_line()}"
    ls_code = format_linescore_code_block(game)
    home_wp, wp_html, wp_banner = extract_win_probability(game, summary)
    story_notes = detect_comeback_and_run(game, summary)
    performers = extract_top_performers(summary, top_n=3)
    foul_trouble = extract_foul_trouble(game, summary)
    team_fouls = compute_team_fouls_status(game, summary)
    recent_plays = extract_recent_plays(game, summary, limit=10)

    # Fallback body (clean team-split)
    fb_parts = [p for p in (ls_code, wp_html) if p]
    for tm in (game.away, game.home):
        tp = performers.get(tm.abbr, [])[:2]
        if tp:
            fb_parts.append(f"<b>{team_badge(tm.abbr)} {html.escape(tm.name)}</b>\n" + "\n".join(f"• {html.escape(x)}" for x in tp))
    if foul_trouble or team_fouls:
        extra_f = ([team_fouls] if team_fouls else []) + foul_trouble
        fb_parts.append("<b>Foul watch:</b> " + html.escape(" | ".join(extra_f)))
    fallback_body = "\n\n".join(fb_parts)

    away_lbl = f"{game.away.name} ({game.away.record})" if game.away.record else game.away.name
    home_lbl = f"{game.home.name} ({game.home.record})" if game.home.record else game.home.name
    raw_context = "\n\n".join(filter(None, [
        f"Matchup: {away_lbl} @ {home_lbl}",
        f"Current State: {game.lead_summary()} ({period_lbl} {game.display_clock})",
        f"Quarter-by-quarter: {format_linescore_compact(game)}",
        wp_banner,
        team_fouls,
        ("Personal Foul Trouble:\n" + "\n".join(f"- {f}" for f in foul_trouble)) if foul_trouble else "",
        ("Comeback / Run Notes:\n" + "\n".join(f"- {n}" for n in story_notes)) if story_notes else "",
        "Top Performers:\n" + "\n".join(
            f"- {abbr}: " + " | ".join(performers.get(abbr, []))
            for abbr in (game.away.abbr, game.home.abbr)
        ),
        ("Most Recent Plays:\n" + "\n".join(f"- {rp}" for rp in recent_plays)) if recent_plays else "",
    ]))

    instruction = (
        "Write a strong, clean Crunch-Time Intro split clearly by team (do NOT include a quarter-by-quarter table or win-prob line—those are prepended automatically):\n"
        f"1. <b>{team_badge(game.away.abbr)} {game.away.name}:</b> 1–2 crisp • bullets. Lead with the biggest game-long star/performer FIRST, then mention any key supporting/clutch plays or foul trouble.\n"
        f"2. <b>{team_badge(game.home.abbr)} {game.home.name}:</b> 1–2 crisp • bullets. Same order—biggest game-long performer FIRST, then supporting/clutch plays or foul trouble.\n"
        "3. <b>The Setup:</b> 1 short closing sentence on how we got here (comeback, late-Q3 swing, or bonus/foul-trouble factor heading into crunch time)."
    )
    ai_body = summarizer.summarize(instruction, raw_context, "", max_tokens=700) if (summarizer and summarizer.enabled) else ""
    if ai_body:
        header_meta = "\n".join(p for p in (ls_code, wp_html) if p)
        body = f"{header_meta}\n\n{ai_body}" if header_meta else ai_body
    else:
        body = fallback_body

    banner = render_matchup_banner(game, f"CRUNCH TIME · {period_lbl} {game.display_clock}", win_prob_note=wp_banner)
    plays = summary.get("plays", [])
    return title, body, int(plays[-1].get("sequenceNumber", len(plays))) if plays else 0, banner, home_wp


def build_final_recap(
    game: GameSnapshot,
    summary: dict[str, Any],
    last_seq: int,
    summarizer: GeminiSummarizer | None = None,
) -> tuple[str, str, int, bytes | None]:
    """Always build a proper Final wrap-up with FINAL team-logo banner (never 'Quiet minute')."""
    title = f"🏁 Final — {game.score_line()}"
    ls_code = format_linescore_code_block(game)
    performers = extract_top_performers(summary, top_n=2)
    story_notes = detect_comeback_and_run(game, summary)
    recent_plays = extract_recent_plays(game, summary, limit=8)

    fb_parts = [ls_code] if ls_code else []
    for tm in (game.away, game.home):
        tp = performers.get(tm.abbr, [])
        if tp:
            fb_parts.append(f"<b>{team_badge(tm.abbr)} {html.escape(tm.name)}</b>\n" + "\n".join(f"• {html.escape(x)}" for x in tp))
    fallback_body = "\n\n".join(fb_parts)

    raw_context = "\n\n".join(filter(None, [
        f"Final: {game.away.name} {game.away.score}, {game.home.name} {game.home.score} ({game.lead_summary()})",
        f"Quarter-by-quarter: {format_linescore_compact(game)}",
        ("Comeback / Win-Prob Swing:\n" + "\n".join(f"- {n}" for n in story_notes)) if story_notes else "",
        "Top Performers:\n" + "\n".join(
            f"- {abbr}: " + " | ".join(performers.get(abbr, []))
            for abbr in (game.away.abbr, game.home.abbr)
        ),
        ("Closing Plays:\n" + "\n".join(f"- {rp}" for rp in recent_plays)) if recent_plays else "",
    ]))
    instruction = (
        "Write a crisp NBA beat-writer Final Buzzer wrap-up (do NOT include the linescore—it is prepended automatically):\n"
        "• 2–3 tight bullets covering who closed it out in the final minute, the game's top star stat line on each side, and any comeback/win-probability swing."
    )
    ai_body = summarizer.summarize(instruction, raw_context, "", max_tokens=550) if (summarizer and summarizer.enabled) else ""
    body = f"{ls_code}\n\n{ai_body}" if (ai_body and ls_code) else (ai_body or fallback_body)

    banner = render_matchup_banner(game, "FINAL")
    plays = summary.get("plays", [])
    newest_seq = max(last_seq, int(plays[-1].get("sequenceNumber", len(plays))) if plays else last_seq)
    return title, body, newest_seq, banner


def build_minute_update(
    game: GameSnapshot,
    summary: dict[str, Any],
    last_seq: int,
    prev_home_win_pct: float | None = None,
    summarizer: GeminiSummarizer | None = None,
    summary_fetcher: Callable[[str], dict[str, Any]] | None = None,
    sync_retry_delay: float = 8.0,
) -> tuple[str, str, int, float | None] | None:
    """Build a 1-minute crunch update using ALL new plays. Returns None if PBP lags a score jump."""
    def collect_all_plays(summ: dict[str, Any]) -> tuple[list[str], int]:
        plays = summ.get("plays", [])
        all_new, newest = [], last_seq
        for idx, p in enumerate(plays, start=1):
            seq = int(p.get("sequenceNumber", idx) or idx)
            if seq > last_seq:
                newest = max(newest, seq)
                txt = str(p.get("text", "")).strip()
                if not txt or txt.lower().startswith("end of the"):
                    continue
                formatted = format_play_with_team(game, p)
                if formatted:
                    all_new.append(formatted)
        return all_new, newest

    all_new_plays, newest_seq = collect_all_plays(summary)

    # ESPN sync guard: if scoreboard shows new points not yet in play-by-play, wait briefly and re-fetch once
    if is_pbp_behind_scoreboard(game, summary):
        if summary_fetcher is not None and sync_retry_delay > 0:
            log.info("Play-by-play lagging scoreboard for %s; retrying in %.0fs...", game.short_name, sync_retry_delay)
            time.sleep(sync_retry_delay)
            try:
                summary = summary_fetcher(game.game_id)
                all_new_plays, newest_seq = collect_all_plays(summary)
            except Exception as exc:
                log.warning("Sync re-fetch failed for %s: %s", game.short_name, exc)
        if not all_new_plays and is_pbp_behind_scoreboard(game, summary):
            log.info("Deferring update for %s until play-by-play catches up with scoreboard.", game.short_name)
            return None

    period_lbl = f"Q{game.period} {game.display_clock}" if game.period <= 4 else f"OT{game.period - 4} {game.display_clock}"
    title = f"⏱ {period_lbl} — {game.score_line()}"
    home_wp, wp_html, wp_banner = extract_win_probability(game, summary, prev_home_win_pct)

    if not all_new_plays:
        quiet = "<i>Quiet minute — timeout, stoppage, or empty trips on both ends.</i>"
        body = f"{wp_html}\n{quiet}" if wp_html else quiet
        return title, body, newest_seq, home_wp

    foul_trouble = extract_foul_trouble(game, summary)
    team_fouls = compute_team_fouls_status(game, summary)

    fallback_lines = [wp_html] if wp_html else []
    fallback_lines.append("<b>Last minute:</b>")
    fallback_lines.extend(f"• {html.escape(h)}" for h in all_new_plays[-10:])
    fallback_body = "\n".join(fallback_lines)

    raw_context = "\n\n".join(filter(None, [
        f"Current State: {game.lead_summary()} ({period_lbl})",
        wp_banner,
        team_fouls,
        ("Personal Foul Trouble:\n" + "\n".join(f"- {f}" for f in foul_trouble)) if foul_trouble else "",
        "Complete Play Sequence in Last Minute (chronological):\n" + "\n".join(all_new_plays[-15:]),
    ]))
    instruction = (
        "Tell the story of what happened in the last minute in 1–3 crisp • bullets (do NOT include the win-prob line or restate the header score/clock):\n"
        "- Cover the actual flow (made/missed shots, missed free throws, And-1s, rebounds, fouls, turnovers, timeouts).\n"
        "- Merge linked plays (And-1 + foul, foul + FTs, steal + bucket) into a single bullet.\n"
        "- Call out foul trouble (5+ PF in Q4) or a team entering/in the bonus if triggered during this minute."
    )
    ai_body = summarizer.summarize(instruction, raw_context, "", max_tokens=450) if (summarizer and summarizer.enabled) else ""
    if ai_body:
        body = f"{wp_html}\n{ai_body}" if wp_html else ai_body
    else:
        body = fallback_body
    return title, body, newest_seq, home_wp


def build_lakers_quarter_update(
    game: GameSnapshot,
    summary: dict[str, Any],
    period_done: int,
    summarizer: GeminiSummarizer | None = None,
) -> tuple[str, str, bytes | None]:
    label = "Final" if game.is_final else ("Halftime" if period_done == 2 else (f"End of Q{period_done}" if period_done <= 4 else f"End of OT{period_done - 4}"))
    title = f"💜💛 Lakers {label} — {game.score_line()}"
    ls_code = format_linescore_code_block(game)
    _, wp_html, wp_banner = extract_win_probability(game, summary)
    performers = extract_top_performers(summary, top_n=3)
    story_notes = detect_comeback_and_run(game, summary)
    foul_trouble = extract_foul_trouble(game, summary)

    fb_parts = [p for p in (ls_code, wp_html) if p]
    for tm in (game.away, game.home):
        tp = performers.get(tm.abbr, [])[:2]
        if tp:
            fb_parts.append(f"<b>{team_badge(tm.abbr)} {html.escape(tm.name)}</b>\n" + "\n".join(f"• {html.escape(x)}" for x in tp))
    fallback_body = "\n\n".join(fb_parts)

    raw_context = "\n\n".join(filter(None, [
        f"Game: {game.away.name} @ {game.home.name} ({label})",
        f"Current State: {game.lead_summary()}",
        f"Quarter-by-quarter: {format_linescore_compact(game)}",
        wp_banner,
        ("Foul Trouble:\n" + "\n".join(f"- {f}" for f in foul_trouble)) if foul_trouble else "",
        ("Runs / Comebacks:\n" + "\n".join(f"- {n}" for n in story_notes)) if story_notes else "",
        "Top Performers:\n" + "\n".join(
            f"- {abbr}: " + " | ".join(performers.get(abbr, []))
            for abbr in (game.away.abbr, game.home.abbr)
        ),
    ]))
    instruction = (
        f"Write a sharp NBA beat-writer {label} check-in for a Lakers fan (do NOT include the linescore or win-prob line—they are prepended automatically):\n"
        "• 2–3 crisp bullets covering how the Lakers look right now, who's leading LA and the opponent (plus any foul trouble), and what to watch next."
    )
    ai_body = summarizer.summarize(instruction, raw_context, "", max_tokens=600) if (summarizer and summarizer.enabled) else ""
    if ai_body:
        header_meta = "\n".join(p for p in (ls_code, wp_html) if p)
        body = f"{header_meta}\n\n{ai_body}" if header_meta else ai_body
    else:
        body = fallback_body
    banner = render_matchup_banner(game, f"LAKERS · {label}", win_prob_note=wp_banner)
    return title, body, banner


def seconds_until_wake_window(now_sgt: dt.datetime, wake_hour_sgt: int, ignore_wake_window: bool) -> float:
    if ignore_wake_window:
        return 0.0
    if now_sgt.weekday() < 5 and now_sgt.hour < wake_hour_sgt:
        return max(0.0, (now_sgt.replace(hour=wake_hour_sgt, minute=0, second=0, microsecond=0) - now_sgt).total_seconds())
    return 0.0


def q4_silent_watch_sleep(margin: int) -> tuple[int, str]:
    """Tiered silent Q4/OT polling intervals based on current point margin."""
    if margin <= 15:
        return 60, f"Q4 silent watch 11-15 pts (margin {margin})"
    if margin <= 20:
        return 120, f"Q4 silent watch 16-20 pts (margin {margin})"
    if margin <= 30:
        return 180, f"Q4 silent watch 21-30 pts (margin {margin})"
    return 300, f"Q4 silent watch 31+ pts (margin {margin})"


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
        if g.is_live and g.period >= 4 and g.margin <= close_margin:
            if g.is_end_of_period:
                candidates.append((float(QUARTER_BREAK_SLEEP_SECONDS), f"pre-OT break ({g.short_name})"))
                continue
            return CRUNCH_POLL_SECONDS, f"crunch watch ({g.short_name})"
        if g.is_pre:
            offset = 20 * 60 if g.is_lakers else 75 * 60
            candidates.append((max(120.0, (g.start_time_utc - now_utc).total_seconds() + offset), f"waiting for {g.short_name}"))
        elif g.is_live and g.is_halftime:
            candidates.append((float(HALFTIME_SLEEP_SECONDS), f"halftime 15m ({g.short_name})"))
        elif g.is_live and g.is_end_of_period:
            candidates.append((float(QUARTER_BREAK_SLEEP_SECONDS), f"quarter break 2.5m ({g.short_name})"))
        elif g.is_live and g.period >= 4:
            secs, lbl = q4_silent_watch_sleep(g.margin)
            candidates.append((float(secs), f"{lbl} ({g.short_name})"))
        elif g.is_live and g.is_lakers:
            est = g.clock_seconds * 1.8
            candidates.append((60.0 if est <= 180 else min(15 * 60.0, max(120.0, est - 150.0)), f"Lakers Q{g.period}"))
        elif g.is_live:
            if g.period == 1:
                candidates.append((40 * 60.0, f"Q1 chill ({g.short_name})"))
            elif g.period == 2:
                candidates.append((30 * 60.0, f"Q2 chill ({g.short_name})"))
            elif g.period == 3:
                est = g.clock_seconds * 1.8
                candidates.append((90.0 if est <= 180 else min(18 * 60.0, max(120.0, est - 150.0)), f"Q3 ({g.short_name})"))
    if not candidates:
        return 300, "fallback"
    best_wait, best_reason = min(candidates, key=lambda x: x[0])
    if not any(g.is_live for g in games) and best_wait > MAX_IDLE_WAIT_SECONDS:
        return 0, f"next_tip_too_far ({best_wait / 3600.0:.1f}h: {best_reason})"
    return max(60, int(best_wait)), best_reason


def process_games(
    games: list[GameSnapshot],
    trackers: dict[str, GameTracker],
    notifier: Notifier,
    close_margin: int,
    summary_fetcher=fetch_game_summary,
    summarizer: GeminiSummarizer | None = None,
    sync_retry_delay: float = 8.0,
) -> None:
    minute_batch: list[tuple[str, str]] = []

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
                title, body, banner = build_lakers_quarter_update(g, get_summary(), g.period, summarizer)
                notifier.send(title, body, photo_bytes=banner)
                tr.lakers_final_sent = True
                tr.last_lakers_period_sent = max(tr.last_lakers_period_sent, g.period)
            elif g.is_live:
                completed = 0
                if g.is_end_of_period and g.period >= 1:
                    completed = g.period
                elif g.period > 1 and (g.period - 1) > tr.last_lakers_period_sent:
                    # Only catch up a just-ended quarter within the first 2 min of the next period;
                    # if starting mid/late quarter (e.g. 1 min left in Q2), skip the stale Q1 alert.
                    fresh_clock_floor = 180.0 if g.period >= 5 else 600.0
                    if g.clock_seconds >= fresh_clock_floor:
                        completed = g.period - 1
                    else:
                        log.info(
                            "Skipping stale Lakers End of Q%d alert (currently Q%d %s); waiting for next horn.",
                            g.period - 1, g.period, g.display_clock,
                        )
                        tr.last_lakers_period_sent = g.period - 1
                if completed > tr.last_lakers_period_sent:
                    title, body, banner = build_lakers_quarter_update(g, get_summary(), completed, summarizer)
                    notifier.send(title, body, photo_bytes=banner)
                    tr.last_lakers_period_sent = completed

        if g.is_live and g.period >= 4:
            if g.margin <= close_margin:
                if not tr.crunch_alerted:
                    title, body, last_seq, banner, home_wp = build_crunch_intro(g, get_summary(), summarizer)
                    notifier.send(title, body, photo_bytes=banner)
                    tr.crunch_alerted, tr.last_play_seq, tr.last_home_win_pct = True, last_seq, home_wp
                else:
                    res = build_minute_update(g, get_summary(), tr.last_play_seq, tr.last_home_win_pct, summarizer, summary_fetcher, sync_retry_delay)
                    if res is not None:
                        title, body, newest_seq, home_wp = res
                        minute_batch.append((title, body))
                        tr.last_play_seq = newest_seq
                        if home_wp is not None:
                            tr.last_home_win_pct = home_wp
            elif tr.crunch_alerted:
                log.info("Pausing minute updates for %s (margin %d > %d); silent Q4 watch.", g.short_name, g.margin, close_margin)
        elif g.is_final and tr.crunch_alerted and not tr.crunch_final_sent:
            title, body, newest_seq, banner = build_final_recap(g, get_summary(), tr.last_play_seq, summarizer)
            notifier.send(title, body, photo_bytes=banner)
            tr.last_play_seq, tr.crunch_final_sent = newest_seq, True

    if len(minute_batch) == 1:
        notifier.send(minute_batch[0][0], minute_batch[0][1])
    elif len(minute_batch) > 1:
        digest_title = f"🏀 Crunch-Time Digest ({len(minute_batch)} Games)"
        sections = [f"<b>{html.escape(t, quote=False)}</b>\n{b}" for t, b in minute_batch]
        notifier.send(digest_title, "\n\n".join(sections))


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