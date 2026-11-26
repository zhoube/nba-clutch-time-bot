#!/usr/bin/env python3
"""
NBA Crunch-Time & Lakers Watcher (Adaptive Polling + Gemini Editorial Summaries + Team-Logo Banners)
====================================================================================================
Monitors live NBA games using ESPN's public JSON scoreboard & game summary APIs.
Uses Pillow (optional) to render real ESPN team-logo matchup banners on initial crunch-time alerts
and Lakers end-of-quarter recaps; gracefully falls back to text-only if Pillow is unavailable.
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
USER_AGENT = "NBACrunchWatcher/2.2 (+https://github.com/)"

# Built-in Telegram topic target: "Sarah and Kelvin" supergroup -> "NBA" topic
BUILTIN_TELEGRAM_TARGETS = ["-1002615344601:270"]

LAKERS_ABBR = "LAL"
CLOSE_MARGIN_DEFAULT = 10
CRUNCH_POLL_SECONDS = 60

# Official NBA Rule 5 intermission lengths: 15 min halftime; 2:30 local (3:30 national TV) quarter/OT breaks
HALFTIME_SLEEP_SECONDS = 15 * 60
QUARTER_BREAK_SLEEP_SECONDS = 150  # 2.5 minutes

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


@dataclass
class GameTracker:
    crunch_alerted: bool = False
    crunch_final_sent: bool = False
    last_play_seq: int = 0
    last_lakers_period_sent: int = 0
    lakers_final_sent: bool = False


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


def render_matchup_banner(game: GameSnapshot, status_badge: str) -> bytes | None:
    """Render a 900x320 broadcast-style matchup banner with real ESPN team PNG logos."""
    if Image is None or ImageDraw is None:
        return None
    try:
        w, h = 900, 320
        bg = (15, 23, 42)  # dark slate
        img = Image.new("RGBA", (w, h), bg + (255,))
        draw = ImageDraw.Draw(img)

        away_col = _parse_hex_color(game.away.color_hex, (59, 130, 246))
        home_col = _parse_hex_color(game.home.color_hex, (239, 68, 68))

        # Top team-color accent bars
        draw.rectangle([0, 0, w // 2, 10], fill=away_col + (255,))
        draw.rectangle([w // 2, 0, w, 10], fill=home_col + (255,))
        # Subtle card divider
        draw.rounded_rectangle([24, 26, w - 24, h - 24], radius=22, fill=(24, 34, 58, 255), outline=(51, 65, 85, 255), width=2)

        def fetch_logo(team: TeamSnapshot) -> Any:
            url = team.logo_url or f"https://a.espncdn.com/i/teamlogos/nba/500/{team.abbr.lower()}.png"
            raw = http_get_bytes(url, timeout=10, retries=2)
            logo = Image.open(io.BytesIO(raw)).convert("RGBA")
            logo.thumbnail((148, 148), Image.Resampling.LANCZOS)
            return logo

        away_logo = fetch_logo(game.away)
        home_logo = fetch_logo(game.home)

        # Paste logos left and right
        img.alpha_composite(away_logo, (68 + (148 - away_logo.width) // 2, 54 + (148 - away_logo.height) // 2))
        img.alpha_composite(home_logo, (w - 68 - 148 + (148 - home_logo.width) // 2, 54 + (148 - home_logo.height) // 2))

        f_score = _load_font(68, bold=True)
        f_abbr = _load_font(28, bold=True)
        f_rec = _load_font(18, bold=False)
        f_badge = _load_font(22, bold=True)

        # Team abbreviations & records beneath logos
        draw.text((142, 222), game.away.abbr, fill=(248, 250, 252), font=f_abbr, anchor="mm")
        if game.away.record:
            draw.text((142, 252), game.away.record, fill=(148, 163, 184), font=f_rec, anchor="mm")

        draw.text((w - 142, 222), game.home.abbr, fill=(248, 250, 252), font=f_abbr, anchor="mm")
        if game.home.record:
            draw.text((w - 142, 252), game.home.record, fill=(148, 163, 184), font=f_rec, anchor="mm")

        # Center status pill
        pill_text = status_badge.upper()
        pill_w = max(180, len(pill_text) * 14 + 44)
        px1, px2 = (w - pill_w) // 2, (w + pill_w) // 2
        draw.rounded_rectangle([px1, 56, px2, 98], radius=21, fill=(220, 38, 38, 255))
        draw.text((w // 2, 77), pill_text, fill=(255, 255, 255), font=f_badge, anchor="mm")

        # Center scoreline
        score_text = f"{game.away.score}  –  {game.home.score}"
        draw.text((w // 2, 166), score_text, fill=(255, 255, 255), font=f_score, anchor="mm")

        # Quarter-by-quarter footer strip inside card
        ls = format_linescore_compact(game)
        if ls:
            draw.text((w // 2, 252), ls, fill=(203, 213, 225), font=f_rec, anchor="mm")

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

    def summarize(self, instruction: str, raw_context: str, fallback: str, max_tokens: int = 700) -> str:
        if not self.enabled:
            return fallback
        models_to_try = [self.model] + [
            m for m in ("gemini-3.8-flash", "gemini-3.5-flash-lite") if m != self.model
        ]
        prompt = (
            "You are an NBA analyst writing crisp, readable live game updates for a Telegram group chat "
            "(matching the clean, informative editorial style of a nightly NBA preview).\n\n"
            f"TASK:\n{instruction}\n\n"
            "STYLE & FORMATTING RULES:\n"
            "- Keep the tone sharp, clear, and engaging—never overly casual, no greetings (no 'Yo', 'Hey', 'wake up', or addressing anyone by name), and no slang.\n"
            "- Highlight the real basketball storylines in the data: momentum swings, comebacks, shooting efficiency or struggles, "
            "standout stat lines or triple-double watches, and key recent possessions.\n"
            "- Use ONLY the facts in LIVE DATA below—never invent players, stats, records, or plays.\n"
            "- Format cleanly for Telegram using ONLY <b>bold</b>, <i>italic</i>, and • bullets. "
            "No Markdown (** or #), no ASCII box tables, no walls of text.\n\n"
            f"LIVE DATA:\n{raw_context}"
        )
        gen_cfg: dict[str, Any] = {
            "temperature": 0.55,
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
                        # Caption exceeds Telegram's 1024-char limit: send banner + full text right below
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
            extra = f", {tp} 3PT" if tp and tp != "0-0" else ""
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
    return notes


def extract_recent_plays(summary: dict[str, Any], limit: int = 8) -> list[str]:
    plays = summary.get("plays", [])
    recent: list[str] = []
    for p in plays[-20:]:
        text = str(p.get("text", "")).strip()
        if not text:
            continue
        is_scoring = bool(p.get("scoringPlay"))
        if is_scoring or any(k in text.lower() for k in ("steal", "block", "turnover", "timeout", "foul", "challenge")):
            clock = p.get("clock", {}).get("displayValue", "")
            qtr = p.get("period", {}).get("number", "")
            score_tag = f" ({p.get('awayScore')}-{p.get('homeScore')})" if is_scoring else ""
            recent.append(f"[Q{qtr} {clock}] {text}{score_tag}")
    return recent[-limit:]


def format_linescore_compact(game: GameSnapshot) -> str:
    if not game.away.linescores or not game.home.linescores:
        return ""
    a_str = "-".join(str(s) for s in game.away.linescores)
    h_str = "-".join(str(s) for s in game.home.linescores)
    return f"{game.away.abbr} ({a_str} = {game.away.score}) | {game.home.abbr} ({h_str} = {game.home.score})"


def is_pbp_behind_scoreboard(game: GameSnapshot, summary: dict[str, Any]) -> bool:
    """Detect ESPN sync lag where scoreboard has updated points not yet present in play-by-play."""
    plays = summary.get("plays", [])
    if not plays:
        return game.away.score > 0 or game.home.score > 0
    last = plays[-1]
    pbp_a = int(last.get("awayScore", 0) or 0)
    pbp_h = int(last.get("homeScore", 0) or 0)
    return pbp_a < game.away.score or pbp_h < game.home.score


def build_crunch_intro(game: GameSnapshot, summary: dict[str, Any], summarizer: GeminiSummarizer | None = None) -> tuple[str, str, int, bytes | None]:
    period_lbl = f"Q{game.period}" if game.period <= 4 else f"OT{game.period - 4}"
    title = f"🏀 Crunch Time ({period_lbl} {game.display_clock}) — {game.score_line()}"
    ls_compact = format_linescore_compact(game)
    story_notes = detect_comeback_and_run(game, summary)
    performers = extract_top_performers(summary, top_n=3)
    recent_plays = extract_recent_plays(summary, limit=8)

    fallback_lines = []
    if ls_compact:
        fallback_lines.append(f"<b>Quarters:</b> {html.escape(ls_compact)}")
    if story_notes:
        fallback_lines.extend(f"• {html.escape(n)}" for n in story_notes)
    if performers:
        fallback_lines.append("\n<b>Top performers:</b>")
        for abbr in (game.away.abbr, game.home.abbr):
            fallback_lines.extend(f"• [{abbr}] {html.escape(p)}" for p in performers.get(abbr, [])[:2])
    fallback_body = "\n".join(fallback_lines)

    away_lbl = f"{game.away.name} ({game.away.record})" if game.away.record else game.away.name
    home_lbl = f"{game.home.name} ({game.home.record})" if game.home.record else game.home.name
    raw_context_parts = [
        f"Matchup: {away_lbl} @ {home_lbl}",
        f"Current Score & Clock: {game.away.abbr} {game.away.score}, {game.home.abbr} {game.home.score} ({period_lbl} {game.display_clock})",
        f"Quarter-by-quarter: {ls_compact}" if ls_compact else "",
        "Comeback / Run Notes:\n" + "\n".join(f"- {n}" for n in story_notes) if story_notes else "Comeback / Run Notes: None flagged",
        "Top Performers:\n"
        + "\n".join(
            f"- {abbr}: " + " | ".join(performers.get(abbr, []))
            for abbr in (game.away.abbr, game.home.abbr)
        ),
        "Most Recent Key Plays:\n" + "\n".join(f"- {rp}" for rp in recent_plays) if recent_plays else "",
    ]
    raw_context = "\n\n".join(p for p in raw_context_parts if p)

    instruction = (
        "Write a clean, easy-to-scan crunch-time update for this close game:\n"
        "1. Lead directly with a compact <b>Quarter flow:</b> line (no greeting or hype intro).\n"
        "2. Add 3–4 crisp • bullets covering the game's momentum/comeback context, who's driving each side (notable efficiency, shooting struggles, or triple-double watches), and what happened on the most recent possessions."
    )
    body = summarizer.summarize(instruction, raw_context, fallback_body) if (summarizer and summarizer.enabled) else fallback_body
    banner = render_matchup_banner(game, f"CRUNCH TIME · {period_lbl} {game.display_clock}")
    plays = summary.get("plays", [])
    return title, body, int(plays[-1].get("sequenceNumber", len(plays))) if plays else 0, banner


def build_minute_update(
    game: GameSnapshot,
    summary: dict[str, Any],
    last_seq: int,
    summarizer: GeminiSummarizer | None = None,
    summary_fetcher: Callable[[str], dict[str, Any]] | None = None,
    sync_retry_delay: float = 8.0,
) -> tuple[str, str, int] | None:
    """Build a 1-minute crunch update. Returns None if play-by-play is lagging behind a score jump."""
    def collect(summ: dict[str, Any]) -> tuple[list[str], int]:
        plays = summ.get("plays", [])
        new_plays, newest = [], last_seq
        for idx, p in enumerate(plays, start=1):
            seq = int(p.get("sequenceNumber", idx) or idx)
            if seq > last_seq:
                new_plays.append(p)
                newest = max(newest, seq)
        hl = []
        for p in new_plays:
            text = str(p.get("text", "")).strip()
            if not text:
                continue
            is_scoring = bool(p.get("scoringPlay"))
            if is_scoring or any(k in text.lower() for k in ("steal", "block", "turnover", "timeout", "foul", "challenge")):
                clock = p.get("clock", {}).get("displayValue", "")
                score_tag = f" ({p.get('awayScore')}-{p.get('homeScore')})" if is_scoring else ""
                hl.append(f"[{clock}] {text}{score_tag}")
        return hl, newest

    highlights, newest_seq = collect(summary)

    # ESPN sync guard: if scoreboard shows new points not yet in play-by-play, wait briefly and re-fetch once
    if is_pbp_behind_scoreboard(game, summary):
        if summary_fetcher is not None and sync_retry_delay > 0:
            log.info("Play-by-play lagging scoreboard for %s; retrying in %.0fs...", game.short_name, sync_retry_delay)
            time.sleep(sync_retry_delay)
            try:
                summary = summary_fetcher(game.game_id)
                highlights, newest_seq = collect(summary)
            except Exception as exc:
                log.warning("Sync re-fetch failed for %s: %s", game.short_name, exc)
        if not highlights and is_pbp_behind_scoreboard(game, summary) and not game.is_final:
            log.info("Deferring update for %s until play-by-play catches up with scoreboard.", game.short_name)
            return None

    period_lbl = "FINAL" if game.is_final else (f"Q{game.period} {game.display_clock}" if game.period <= 4 else f"OT{game.period - 4} {game.display_clock}")
    prefix = "🏁 Final" if game.is_final else f"⏱ {period_lbl}"
    title = f"{prefix} — {game.score_line()}"

    if not highlights:
        return title, "<i>Quiet minute — timeout, stoppage, or empty trips on both ends.</i>", newest_seq
    fallback_body = "<b>Last minute:</b>\n" + "\n".join(f"• {html.escape(h)}" for h in highlights[-8:])
    if game.is_final:
        performers = extract_top_performers(summary, top_n=2)
        perf_ctx = "\n".join(
            f"- {abbr}: " + " | ".join(performers.get(abbr, []))
            for abbr in (game.away.abbr, game.home.abbr)
        )
        instruction = (
            "Write a brisk, clean final-buzzer wrap-up (2 short sentences + 2 • bullets): "
            "how the closing minute sealed it, who closed the game out, and the top stat line."
        )
        raw_context = f"Final: {game.away.name} {game.away.score}, {game.home.name} {game.home.score}\nClosing plays:\n" + "\n".join(highlights[-8:]) + f"\n\nTop performers:\n{perf_ctx}"
    else:
        instruction = (
            "Summarize the last minute of this close game in 2–3 crisp • bullets covering who scored, "
            "any key stops, turnovers, fouls, or timeouts, and the current momentum."
        )
        raw_context = f"Score: {game.away.name} {game.away.score}, {game.home.name} {game.home.score} ({period_lbl})\nPlays in last minute:\n" + "\n".join(highlights[-8:])
    body = summarizer.summarize(instruction, raw_context, fallback_body, max_tokens=450) if (summarizer and summarizer.enabled) else fallback_body
    return title, body, newest_seq


def build_lakers_quarter_update(game: GameSnapshot, summary: dict[str, Any], period_done: int, summarizer: GeminiSummarizer | None = None) -> tuple[str, str, bytes | None]:
    label = "Final" if game.is_final else ("Halftime" if period_done == 2 else (f"End of Q{period_done}" if period_done <= 4 else f"End of OT{period_done - 4}"))
    title = f"💜💛 Lakers {label} — {game.score_line()}"
    ls_compact = format_linescore_compact(game)
    performers = extract_top_performers(summary, top_n=3)
    story_notes = detect_comeback_and_run(game, summary)

    fallback_lines = []
    if ls_compact:
        fallback_lines.append(f"<b>Quarters:</b> {html.escape(ls_compact)}")
    if performers:
        fallback_lines.append("\n<b>Leaders:</b>")
        for abbr in (game.away.abbr, game.home.abbr):
            fallback_lines.extend(f"• [{abbr}] {html.escape(p)}" for p in performers.get(abbr, [])[:2])
    fallback_body = "\n".join(fallback_lines)

    raw_context = "\n\n".join(filter(None, [
        f"Game: {game.away.name} @ {game.home.name} ({label})",
        f"Score: {game.score_line(with_emojis=False)}",
        f"Quarter-by-quarter: {ls_compact}" if ls_compact else "",
        "Runs / Comebacks:\n" + "\n".join(f"- {n}" for n in story_notes) if story_notes else "",
        "Top Performers:\n" + "\n".join(
            f"- {abbr}: " + " | ".join(performers.get(abbr, []))
            for abbr in (game.away.abbr, game.home.abbr)
        ),
    ]))
    instruction = (
        f"Write a sharp, clean {label} check-in for a Lakers fan:\n"
        "1. 1–2 sentences on how the Lakers look right now and the quarter's momentum.\n"
        "2. Compact <b>Quarters:</b> line.\n"
        "3. 2–3 crisp • bullets on who's carrying LA, who's hurting them on the other side, and what to watch next."
    )
    body = summarizer.summarize(instruction, raw_context, fallback_body) if (summarizer and summarizer.enabled) else fallback_body
    banner = render_matchup_banner(game, f"LAKERS · {label}")
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
        if g.is_live and (tr.crunch_alerted or (g.period >= 4 and g.margin <= close_margin)):
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
                completed = g.period if (g.is_end_of_period and g.period >= 1) else (g.period - 1 if g.period > 1 and (g.period - 1) > tr.last_lakers_period_sent else 0)
                if completed > tr.last_lakers_period_sent:
                    title, body, banner = build_lakers_quarter_update(g, get_summary(), completed, summarizer)
                    notifier.send(title, body, photo_bytes=banner)
                    tr.last_lakers_period_sent = completed

        if g.is_live and g.period >= 4:
            if not tr.crunch_alerted and g.margin <= close_margin:
                title, body, last_seq, banner = build_crunch_intro(g, get_summary(), summarizer)
                notifier.send(title, body, photo_bytes=banner)
                tr.crunch_alerted, tr.last_play_seq = True, last_seq
            elif tr.crunch_alerted:
                res = build_minute_update(g, get_summary(), tr.last_play_seq, summarizer, summary_fetcher, sync_retry_delay)
                if res is not None:
                    title, body, newest_seq = res
                    minute_batch.append((title, body))
                    tr.last_play_seq = newest_seq
        elif g.is_final and tr.crunch_alerted and not tr.crunch_final_sent:
            res = build_minute_update(g, get_summary(), tr.last_play_seq, summarizer, summary_fetcher, sync_retry_delay)
            if res is not None:
                title, body, newest_seq = res
                minute_batch.append((title, body))
                tr.last_play_seq, tr.crunch_final_sent = newest_seq, True

    # Combined multi-game digest when 2+ crunch-time games tick on the same minute
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