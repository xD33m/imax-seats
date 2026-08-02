"""Watch a Traumpalast film page and report new IMAX showtimes to Discord.

The cinema publishes the next two weeks of showtimes (usually on Mondays).
This script polls the film page, extracts the showtimes of one specific
screening version (by default "IMAX" / "Sprache: Deutsch", i.e. *not* the
original-language IMAX version) and posts to a Discord webhook as soon as
showtimes appear that it has not seen before.

Seen showtimes are persisted to disk, so a restart does not re-announce
everything.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOG = logging.getLogger("imax-seats")

STATE_VERSION = 1
DISCORD_LIMIT = 1900  # real limit is 2000; leave room for formatting
WEEKDAYS_DE = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")


class ConfigError(Exception):
    """Raised when the environment is not usable."""


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


def _env_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _env_dates(name: str) -> tuple[str, ...]:
    """Parse a comma-separated date list into sorted ISO dates.

    Accepts the German ``16.08.2026`` as well as ``2026-08-16``.
    """
    raw = _env_str(name)
    if not raw:
        return ()
    dates = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y"):
            try:
                dates.append(datetime.strptime(part, fmt).strftime("%Y-%m-%d"))
                break
            except ValueError:
                continue
        else:
            raise ConfigError(
                f"{name}: cannot read {part!r} as a date (use 16.08.2026 or 2026-08-16)"
            )
    return tuple(sorted(set(dates)))


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_str(name).lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean, got {raw!r}")


@dataclass(frozen=True)
class Config:
    target_url: str
    webhook_url: str = ""
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_token: str = ""
    notify_attempts: int = 3
    notify_retry_delay: float = 2.0
    version_title: str = "IMAX"
    version_language: str = "Deutsch"
    watch_dates: tuple[str, ...] = ()  # ISO dates that get their own loud alert
    mention: str = ""  # prepended to watched-date alerts, e.g. "@everyone"
    interval: int = 120
    max_backoff: int = 1800
    timeout: int = 20
    state_file: Path = Path("state.json")
    health_file: Path | None = None
    failure_alert_after: int = 3
    http_retries: int = 3
    http_backoff: float = 1.5
    notify_on_start: bool = True
    notify_sold_out_changes: bool = True
    heartbeat_hours: int = 0
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    @classmethod
    def from_env(cls) -> "Config":
        target_url = _env_str("TARGET_URL")
        webhook_url = _env_str("DISCORD_WEBHOOK_URL")
        ntfy_topic = _env_str("NTFY_TOPIC")

        if not target_url:
            raise ConfigError("missing required environment variable: TARGET_URL")
        if not target_url.startswith(("http://", "https://")):
            raise ConfigError(f"TARGET_URL must be an http(s) URL, got {target_url!r}")
        if not webhook_url and not ntfy_topic:
            raise ConfigError(
                "no notification channel configured: set DISCORD_WEBHOOK_URL, "
                "NTFY_TOPIC, or both"
            )
        if webhook_url and not webhook_url.startswith("https://"):
            raise ConfigError("DISCORD_WEBHOOK_URL must be an https URL")

        return cls(
            target_url=target_url,
            webhook_url=webhook_url,
            ntfy_server=_env_str("NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
            ntfy_topic=ntfy_topic,
            ntfy_token=_env_str("NTFY_TOKEN"),
            version_title=_env_str("VERSION_TITLE", "IMAX"),
            version_language=_env_str("VERSION_LANGUAGE", "Deutsch"),
            watch_dates=_env_dates("WATCH_DATES"),
            mention=_env_str("MENTION"),
            interval=_env_int("CHECK_INTERVAL", 120, minimum=15),
            max_backoff=_env_int("MAX_BACKOFF", 1800, minimum=15),
            timeout=_env_int("REQUEST_TIMEOUT", 20, minimum=1),
            state_file=Path(_env_str("STATE_FILE", "state.json")),
            health_file=Path(_env_str("HEALTH_FILE")) if _env_str("HEALTH_FILE") else None,
            failure_alert_after=_env_int("FAILURE_ALERT_AFTER", 3, minimum=1),
            http_retries=_env_int("HTTP_RETRIES", 3),
            notify_on_start=_env_bool("NOTIFY_ON_START", True),
            notify_sold_out_changes=_env_bool("NOTIFY_SOLD_OUT_CHANGES", True),
            heartbeat_hours=_env_int("HEARTBEAT_HOURS", 0),
            user_agent=_env_str("USER_AGENT") or cls.user_agent,
        )


# --------------------------------------------------------------------------
# scraping
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Showing:
    """One screening of the watched version."""

    starts_at: str  # "2026-08-03T14:30:00" as published by the site
    time_label: str  # "14:30"
    url: str  # absolute ticket URL ("" when not bookable)
    soldrate: str  # "low" | "above" | "high" | "out" | "unknown"
    sold_out: bool

    @property
    def date(self) -> str:
        return self.starts_at[:10]

    def day_label(self) -> str:
        try:
            day = datetime.strptime(self.date, "%Y-%m-%d")
        except ValueError:
            return self.date
        return f"{WEEKDAYS_DE[day.weekday()]}, {day:%d.%m.}"


@dataclass
class PageSnapshot:
    film_title: str
    showings: list[Showing] = field(default_factory=list)
    version_found: bool = False


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def decode_response(response: requests.Response) -> str:
    """Decode HTML defensively.

    The Traumpalast server announces ``charset=utf-8`` but actually serves
    cp1252, so trusting either the header or requests' guess produces
    mojibake. Try the encodings in order of likelihood instead.
    """
    raw = response.content
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _version_matches(heading, title: str, language: str) -> bool:
    """Check an ``<h4 class="version">`` against the configured version.

    The heading looks like ``IMAX<br><small>Sprache: Deutsch</small>``. The
    title is compared exactly so that "IMAX" does not also match
    "IMAX (Originalversion)".
    """
    small = heading.find("small")
    heading_language = ""
    if small is not None:
        heading_language = _clean(small.get_text()).removeprefix("Sprache:").strip()
        small.extract()  # so it does not end up in the title
    heading_title = _clean(heading.get_text())

    if heading_title.casefold() != title.casefold():
        return False
    if language and heading_language.casefold() != language.casefold():
        return False
    return True


def parse_page(html: str, base_url: str, title: str, language: str) -> PageSnapshot:
    """Extract the showings of the wanted version from a film page."""
    soup = BeautifulSoup(html, "html.parser")

    film_heading = soup.find("h1")
    film_title = _clean(film_heading.get_text()) if film_heading else "Film"

    snapshot = PageSnapshot(film_title=film_title)

    for heading in soup.select("h4.version"):
        if not _version_matches(heading, title, language):
            continue

        snapshot.version_found = True
        block = heading.find_next_sibling("div")
        if block is None:
            LOG.warning("version heading has no showtimes container")
            continue

        for entry in block.select(".day-time"):
            time_tag = entry.find("time")
            starts_at = (time_tag.get("datetime") if time_tag else "") or ""
            if not starts_at:
                LOG.debug("skipping showtime without datetime attribute")
                continue

            classes = entry.get("class") or []
            soldrate = next(
                (c.removeprefix("soldrate-") for c in classes if c.startswith("soldrate-")),
                "unknown",
            )
            href = entry.get("href") or ""
            snapshot.showings.append(
                Showing(
                    starts_at=starts_at,
                    time_label=_clean(time_tag.get_text()) if time_tag else starts_at[11:16],
                    url=urljoin(base_url, href) if href else "",
                    soldrate=soldrate,
                    # no ticket link at all means the show cannot be booked
                    sold_out=not href,
                )
            )
        break  # a version appears once per page

    snapshot.showings.sort(key=lambda s: s.starts_at)
    return snapshot


def fetch(session: requests.Session, config: Config) -> PageSnapshot:
    response = session.get(config.target_url, timeout=config.timeout)
    response.raise_for_status()
    html = decode_response(response)
    return parse_page(
        html, config.target_url, config.version_title, config.version_language
    )


def build_session(config: Config) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": config.user_agent,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "de-DE,de;q=0.9",
        }
    )
    retry = Retry(
        total=config.http_retries,
        connect=config.http_retries,
        read=config.http_retries,
        backoff_factor=config.http_backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------


@dataclass
class State:
    """What we have already told the user about."""

    path: Path
    showings: dict[str, dict] = field(default_factory=dict)
    announced_dates: set[str] = field(default_factory=set)
    known: bool = False  # False until a first successful check was stored

    @classmethod
    def load(cls, path: Path) -> "State":
        state = cls(path=path)
        if not path.exists():
            LOG.info("no state file at %s, starting fresh", path)
            return state
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("could not read state file %s (%s), starting fresh", path, exc)
            return state
        if data.get("state_version") != STATE_VERSION:
            LOG.warning("state file has an incompatible version, starting fresh")
            return state
        showings = data.get("showings")
        if not isinstance(showings, dict):
            LOG.warning("state file has no usable showings, starting fresh")
            return state
        state.showings = showings
        announced = data.get("announced_dates")
        state.announced_dates = set(announced) if isinstance(announced, list) else set()
        state.known = True
        LOG.info("loaded %d known showtime(s) from %s", len(showings), path)
        return state

    def save(self, showings: Sequence[Showing], film_title: str, url: str) -> None:
        self.showings = {
            s.starts_at: {"soldrate": s.soldrate, "sold_out": s.sold_out, "url": s.url}
            for s in showings
        }
        self.known = True
        payload = {
            "state_version": STATE_VERSION,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "film_title": film_title,
            "url": url,
            "announced_dates": sorted(self.announced_dates),
            "showings": self.showings,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            LOG.error("could not write state file %s: %s", self.path, exc)


def diff(state: State, showings: Sequence[Showing]) -> tuple[list[Showing], list[Showing], list[str]]:
    """Return (newly added, back on sale, disappeared)."""
    added = [s for s in showings if s.starts_at not in state.showings]
    back_on_sale = [
        s
        for s in showings
        if state.showings.get(s.starts_at, {}).get("sold_out") and not s.sold_out
    ]
    current = {s.starts_at for s in showings}
    gone = sorted(key for key in state.showings if key not in current)
    return added, back_on_sale, gone


# --------------------------------------------------------------------------
# notifications
# --------------------------------------------------------------------------


def group_by_day(showings: Iterable[Showing]) -> dict[str, list[Showing]]:
    by_day: dict[str, list[Showing]] = {}
    for showing in sorted(showings, key=lambda s: s.starts_at):
        by_day.setdefault(showing.date, []).append(showing)
    return by_day


def format_showings(
    showings: Iterable[Showing], watch_dates: Sequence[str] = ()
) -> list[str]:
    """One line per day: ``Mo, 03.08. — [14:30](url) · [19:00](url)``.

    Days in ``watch_dates`` are marked, so a watched date stands out even in a
    long listing.
    """
    lines = []
    for date, day_showings in group_by_day(showings).items():
        times = []
        for showing in day_showings:
            label = showing.time_label
            if showing.sold_out:
                times.append(f"~~{label}~~")
            elif showing.url:
                times.append(f"[{label}](<{showing.url}>)")
            else:
                times.append(label)
        marker = "🎯 " if date in watch_dates else ""
        lines.append(f"{marker}**{day_showings[0].day_label()}** — " + " · ".join(times))
    return lines


def chunk_message(header: str, lines: Sequence[str]) -> list[str]:
    """Split a header + lines into Discord-sized messages."""
    messages: list[str] = []
    current = header
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > DISCORD_LIMIT and current:
            messages.append(current)
            current = line
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\(<?([^)>]+)>?\)")


def to_plain_text(content: str) -> str:
    """Strip Discord markup so the same message reads well as a push notification."""
    text = MARKDOWN_LINK.sub(r"\1", content)  # [14:30](<url>) -> 14:30
    text = re.sub(r"\*\*|~~|__|(?<!\w)\*(?!\w)", "", text)
    text = re.sub(r"^\s*<?@\S+>?\s*", "", text)  # a leading @everyone means nothing here
    return text.strip()


class Channel:
    """One place to deliver a message to."""

    name = "channel"

    def __init__(self, session: requests.Session, attempts: int = 3, retry_delay: float = 2.0):
        self.session = session
        self.attempts = max(1, attempts)
        self.retry_delay = retry_delay

    def _post(self, url: str, **kwargs) -> bool:
        """POST with retries. Returns True once the service accepted it."""
        for attempt in range(1, self.attempts + 1):
            try:
                response = self.session.post(url, timeout=15, **kwargs)
            except requests.RequestException as exc:
                LOG.warning(
                    "%s request failed (attempt %d/%d): %s",
                    self.name, attempt, self.attempts, exc,
                )
            else:
                if response.status_code == 429:
                    wait = 5.0
                    try:
                        wait = float(response.json().get("retry_after", wait))
                    except (ValueError, requests.JSONDecodeError):
                        pass
                    LOG.warning("%s rate limited, waiting %.1fs", self.name, wait)
                    time.sleep(min(wait, 30))
                    continue
                if response.ok:
                    return True
                LOG.warning(
                    "%s returned %s (attempt %d/%d): %s",
                    self.name, response.status_code, attempt, self.attempts,
                    response.text[:200],
                )
            if attempt < self.attempts:
                time.sleep(self.retry_delay * attempt)
        LOG.error("giving up on the %s message", self.name)
        return False

    def send(self, content: str, *, title: str = "", click_url: str = "",
             allow_mentions: bool = False, priority: bool = False) -> bool:
        raise NotImplementedError


class DiscordChannel(Channel):
    name = "Discord"

    def __init__(self, session, webhook_url: str, attempts: int = 3, retry_delay: float = 2.0):
        super().__init__(session, attempts, retry_delay)
        self.webhook_url = webhook_url

    def send(self, content, *, title="", click_url="", allow_mentions=False, priority=False):
        # mentions are what makes a phone buzz, so they are only let through
        # on watched-date alerts
        allowed = {"parse": ["everyone", "roles", "users"]} if allow_mentions else {"parse": []}
        return self._post(
            self.webhook_url,
            json={"content": content, "allowed_mentions": allowed},
        )


class NtfyChannel(Channel):
    """https://ntfy.sh — push straight to the phone app, no account needed."""

    name = "ntfy"

    def __init__(self, session, server: str, topic: str, token: str = "",
                 attempts: int = 3, retry_delay: float = 2.0):
        super().__init__(session, attempts, retry_delay)
        self.server = server.rstrip("/")
        self.topic = topic
        self.token = token

    def send(self, content, *, title="", click_url="", allow_mentions=False, priority=False):
        body = to_plain_text(content)
        payload = {
            "topic": self.topic,
            "message": body,
            # 5 = max, buzzes even through Do-Not-Disturb on Android
            "priority": 5 if priority else 3,
            "tags": ["dart"] if priority else ["clapper"],
        }
        if title:
            payload["title"] = to_plain_text(title)[:250]
        if click_url:
            payload["click"] = click_url
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        # the JSON endpoint takes UTF-8 properly, unlike the header-based one
        return self._post(self.server, json=payload, headers=headers)


class Notifier:
    """Fans one message out to every configured channel."""

    def __init__(self, channels: Sequence[Channel], dry_run: bool = False):
        self.channels = list(channels)
        self.dry_run = dry_run

    @classmethod
    def from_config(cls, config: Config, session: requests.Session, dry_run: bool = False):
        channels: list[Channel] = []
        if config.webhook_url:
            channels.append(
                DiscordChannel(
                    session, config.webhook_url,
                    config.notify_attempts, config.notify_retry_delay,
                )
            )
        if config.ntfy_topic:
            channels.append(
                NtfyChannel(
                    session, config.ntfy_server, config.ntfy_topic, config.ntfy_token,
                    config.notify_attempts, config.notify_retry_delay,
                )
            )
        return cls(channels, dry_run=dry_run)

    def send(self, content: str, allow_mentions: bool = False, *,
             title: str = "", click_url: str = "", priority: bool = False) -> bool:
        """Deliver to every channel.

        Counts as delivered when at least one channel accepted it — otherwise a
        single broken channel would make the caller retry forever and spam the
        working one.
        """
        if self.dry_run:
            LOG.info(
                "[dry-run] would send to %s:\n%s",
                ", ".join(c.name for c in self.channels) or "nothing",
                content,
            )
            return True

        results = [
            channel.send(
                content, title=title, click_url=click_url,
                allow_mentions=allow_mentions, priority=priority,
            )
            for channel in self.channels
        ]
        if results and not all(results):
            failed = [c.name for c, ok in zip(self.channels, results) if not ok]
            LOG.error("delivery failed for: %s", ", ".join(failed))
        return any(results)

    def send_all(self, messages: Sequence[str], allow_mentions: bool = False, *,
                 title: str = "", click_url: str = "", priority: bool = False) -> bool:
        return all(
            [
                self.send(
                    message, allow_mentions,
                    title=title, click_url=click_url, priority=priority,
                )
                for message in messages
            ]
        )


# --------------------------------------------------------------------------
# the watcher
# --------------------------------------------------------------------------


class Watcher:
    def __init__(self, config: Config, notifier: Notifier, session: requests.Session):
        self.config = config
        self.notifier = notifier
        self.session = session
        self.state = State.load(config.state_file)
        self.failures = 0
        self.failure_alerted = False
        self.missing_version_logged = False
        self.last_heartbeat = time.monotonic()

    @property
    def target_label(self) -> str:
        language = self.config.version_language
        return f"{self.config.version_title}{f' ({language})' if language else ''}"

    def check(self) -> None:
        """One poll cycle. Raises on network/parse trouble."""
        snapshot = fetch(self.session, self.config)

        if not snapshot.version_found:
            if not self.missing_version_logged:
                LOG.warning(
                    "version %r not listed on the page (yet) — nothing to compare",
                    self.target_label,
                )
                self.missing_version_logged = True
            return
        self.missing_version_logged = False

        added, back_on_sale, gone = diff(self.state, snapshot.showings)
        watch_dates = self.config.watch_dates

        # A watched date is announced the first time it shows up at all, not
        # only when a showtime is "new" — so adding a date to WATCH_DATES later
        # still alerts, and a date that vanishes and returns alerts again.
        present_watched = {s.date for s in snapshot.showings if s.date in watch_dates}
        newly_watched = sorted(present_watched - self.state.announced_dates)
        watched_showings = [s for s in snapshot.showings if s.date in newly_watched]

        if not self.state.known:
            LOG.info(
                "baseline: %d %s showtime(s) for %r",
                len(snapshot.showings),
                self.target_label,
                snapshot.film_title,
            )
            sent = True
            if self.config.notify_on_start:
                sent = self.notifier.send_all(
                    chunk_message(
                        f"👀 Now watching **{snapshot.film_title}** — {self.target_label}\n"
                        f"{len(snapshot.showings)} showtime(s) currently listed:",
                        format_showings(snapshot.showings, watch_dates) or ["_none yet_"],
                    ),
                    title=f"Now watching {snapshot.film_title}",
                    click_url=self.config.target_url,
                )
            if watched_showings and self.announce_watched(snapshot, watched_showings):
                self.state.announced_dates |= set(newly_watched)
            self.state.save(snapshot.showings, snapshot.film_title, self.config.target_url)
            if not sent:
                LOG.error("startup summary could not be delivered")
            return

        # the watched dates get their own message, so they are not buried
        if watched_showings:
            if not self.announce_watched(snapshot, watched_showings):
                LOG.error("watched-date alert failed, will retry next cycle")
                return
            self.state.announced_dates |= set(newly_watched)

        # (message chunks, push title) pairs, delivered in order
        batches: list[tuple[list[str], str]] = []
        other_added = [s for s in added if s.date not in newly_watched]
        if other_added:
            LOG.info(
                "%d new showtime(s): %s", len(other_added), [s.starts_at for s in other_added]
            )
            plural = "s" if len(other_added) != 1 else ""
            batches.append(
                (
                    chunk_message(
                        f"🎬 **{snapshot.film_title}** — {len(other_added)} new "
                        f"{self.target_label} showtime{plural}!\n{self.config.target_url}",
                        format_showings(other_added, watch_dates),
                    ),
                    f"{len(other_added)} new {self.target_label} showtime{plural}",
                )
            )
        if back_on_sale and self.config.notify_sold_out_changes:
            LOG.info("%d showtime(s) back on sale", len(back_on_sale))
            batches.append(
                (
                    chunk_message(
                        f"🎟️ Back on sale — **{snapshot.film_title}** ({self.target_label}):",
                        format_showings(back_on_sale, watch_dates),
                    ),
                    f"{snapshot.film_title} — back on sale",
                )
            )
        if gone:
            LOG.info("%d showtime(s) disappeared: %s", len(gone), gone)

        # a watched date that disappears should alert again if it comes back
        dropped = self.state.announced_dates - present_watched
        if dropped:
            LOG.info("watched date(s) no longer listed: %s", sorted(dropped))
            self.state.announced_dates &= present_watched

        if batches:
            for messages, title in batches:
                if not self.notifier.send_all(
                    messages, title=title, click_url=self.config.target_url
                ):
                    # keep the old state so the next cycle retries the alert
                    LOG.error("notification failed, will retry next cycle")
                    return
        elif not watched_showings:
            LOG.info("no changes (%d showtime(s) known)", len(snapshot.showings))

        if added or back_on_sale or gone or watched_showings or dropped:
            self.state.save(snapshot.showings, snapshot.film_title, self.config.target_url)

    def announce_watched(self, snapshot: PageSnapshot, showings: Sequence[Showing]) -> bool:
        """Post the loud alert for the dates the user actually wants to book."""
        days = ", ".join(sorted({s.day_label().rstrip(".") for s in showings}))
        LOG.info("watched date(s) available: %s", days)
        mention = f"{self.config.mention} " if self.config.mention else ""
        header = (
            f"{mention}🎯 **{snapshot.film_title}** is now bookable on **{days}** "
            f"({self.target_label})!\n{self.config.target_url}"
        )
        # tapping the push notification should land on a ticket page, not the
        # generic film page
        bookable = next((s.url for s in showings if s.url), self.config.target_url)
        return self.notifier.send_all(
            chunk_message(header, format_showings(showings)),
            allow_mentions=bool(mention),
            title=f"🎯 {snapshot.film_title} — {days}",
            click_url=bookable,
            priority=True,
        )

    def on_success(self) -> None:
        if self.failure_alerted:
            self.notifier.send("✅ IMAX watcher is back online.", title="IMAX watcher recovered")
        self.failures = 0
        self.failure_alerted = False
        self.touch_health_file()

    def touch_health_file(self) -> None:
        """Bump the liveness marker a container healthcheck can look at."""
        if self.config.health_file is None:
            return
        try:
            self.config.health_file.parent.mkdir(parents=True, exist_ok=True)
            self.config.health_file.write_text(
                datetime.now().isoformat(timespec="seconds"), encoding="utf-8"
            )
        except OSError as exc:
            LOG.warning("could not update health file: %s", exc)

    def on_failure(self, exc: Exception) -> float:
        self.failures += 1
        LOG.error("check failed (%d in a row): %s", self.failures, exc)
        if self.failures >= self.config.failure_alert_after and not self.failure_alerted:
            self.failure_alerted = True
            self.notifier.send(
                f"⚠️ IMAX watcher: {self.failures} failed checks in a row.\n"
                f"`{type(exc).__name__}: {exc}`",
                title="IMAX watcher is failing",
            )
        backoff = self.config.interval * (2 ** min(self.failures - 1, 6))
        return min(backoff, self.config.max_backoff)

    def maybe_heartbeat(self) -> None:
        if not self.config.heartbeat_hours:
            return
        elapsed = time.monotonic() - self.last_heartbeat
        if elapsed < self.config.heartbeat_hours * 3600:
            return
        self.last_heartbeat = time.monotonic()
        self.notifier.send(
            f"💓 IMAX watcher alive — {len(self.state.showings)} showtime(s) known."
        )

    def run(self, stop: threading.Event) -> None:
        LOG.info(
            "watching %s for %s every %ds",
            self.config.target_url,
            self.target_label,
            self.config.interval,
        )
        if self.config.watch_dates:
            LOG.info(
                "watched date(s): %s",
                ", ".join(
                    f"{datetime.strptime(d, '%Y-%m-%d'):%d.%m.%Y}"
                    for d in self.config.watch_dates
                ),
            )
        while not stop.is_set():
            try:
                self.check()
            except Exception as exc:  # noqa: BLE001 - the loop must survive anything
                delay = self.on_failure(exc)
            else:
                self.on_success()
                self.maybe_heartbeat()
                delay = self.config.interval + random.uniform(0, self.config.interval * 0.1)
            LOG.debug("sleeping %.0fs", delay)
            stop.wait(delay)
        LOG.info("stopped")


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--once", action="store_true", help="run a single check and exit"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="never post to Discord, just log what would be sent",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="print the currently listed showtimes and exit (no state, no messages)",
    )
    parser.add_argument(
        "--test-notify",
        action="store_true",
        help="send one test message through every configured channel and exit",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    return parser.parse_args(argv)


def setup_logging(verbose: bool) -> None:
    for stream in (sys.stdout, sys.stderr):
        # Windows consoles default to cp1252 and would choke on the emoji
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="[%(asctime)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)
    load_dotenv()

    try:
        config = Config.from_env()
    except ConfigError as exc:
        LOG.error("configuration error: %s", exc)
        return 2

    session = build_session(config)

    if args.show:
        snapshot = fetch(session, config)
        print(f"{snapshot.film_title} — {config.version_title} ({config.version_language})")
        if not snapshot.version_found:
            print("version not listed on the page")
            return 1
        lines = format_showings(snapshot.showings, config.watch_dates)
        for line in lines or ["no showtimes"]:
            print(re.sub(r"\*\*|<|>", "", line))
        if config.watch_dates:
            listed = {s.date for s in snapshot.showings}
            for date in config.watch_dates:
                mark = "yes" if date in listed else "not yet"
                print(f"watched {datetime.strptime(date, '%Y-%m-%d'):%d.%m.%Y}: {mark}")
        return 0

    notifier = Notifier.from_config(config, session, dry_run=args.dry_run)
    LOG.info("channels: %s", ", ".join(c.name for c in notifier.channels))

    if args.test_notify:
        ok = notifier.send(
            f"🔔 Test from the IMAX watcher — watching **{config.version_title} "
            f"({config.version_language})** at\n{config.target_url}",
            allow_mentions=bool(config.mention),
            title="IMAX watcher test",
            click_url=config.target_url,
            priority=True,
        )
        LOG.info("test notification %s", "sent" if ok else "FAILED")
        return 0 if ok else 1

    watcher = Watcher(config, notifier, session)

    if args.once:
        try:
            watcher.check()
        except Exception as exc:  # noqa: BLE001
            watcher.on_failure(exc)
            return 1
        return 0

    stop = threading.Event()

    def handle_signal(signum, _frame):
        LOG.info("received %s, shutting down", signal.Signals(signum).name)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handle_signal)

    watcher.run(stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
