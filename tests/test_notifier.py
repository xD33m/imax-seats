"""Tests for the notifier.

``fixtures/film_page.html`` is a real capture of the Traumpalast film page
(cp1252 bytes, served with a lying ``charset=utf-8`` header) for "Die Odyssee".
Its German IMAX version lists 15 showtimes across 10 days.
"""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import notifier as n

FIXTURE = Path(__file__).parent / "fixtures" / "film_page.html"
BASE_URL = "https://leonberg.traumpalast.de/index.php/FN/8415/PID/5574.html"
PAGE_BYTES = FIXTURE.read_bytes()
PAGE_HTML = PAGE_BYTES.decode("cp1252")


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


@pytest.fixture
def snapshot() -> n.PageSnapshot:
    return n.parse_page(PAGE_HTML, BASE_URL, "IMAX", "Deutsch")


def test_finds_the_german_imax_version(snapshot):
    assert snapshot.version_found
    assert snapshot.film_title == "Die Odyssee"
    assert len(snapshot.showings) == 15


def test_showings_are_sorted_and_complete(snapshot):
    starts = [s.starts_at for s in snapshot.showings]
    assert starts == sorted(starts)
    assert starts[0] == "2026-08-03T14:30:00"
    assert starts[-1] == "2026-08-12T19:00:00"
    assert "2026-08-08T23:30:00" in starts


def test_ticket_urls_are_absolute(snapshot):
    assert all(s.url.startswith("https://leonberg.traumpalast.de/") for s in snapshot.showings)
    assert all("/TICKET/" in s.url for s in snapshot.showings)


def test_soldrate_is_extracted(snapshot):
    assert {s.soldrate for s in snapshot.showings} <= {"low", "above", "high", "out"}
    assert not any(s.sold_out for s in snapshot.showings)


def test_original_version_is_a_separate_set(snapshot):
    original = n.parse_page(PAGE_HTML, BASE_URL, "IMAX (Originalversion)", "Originalsprache")
    assert original.version_found
    german = {s.starts_at for s in snapshot.showings}
    # the OV has a show today that the German version does not
    assert "2026-08-02T19:00:00" in {s.starts_at for s in original.showings}
    assert "2026-08-02T19:00:00" not in german


@pytest.mark.parametrize(
    "title,language",
    [
        ("IMAX", "Originalsprache"),  # right title, wrong language
        ("IMAX 3D", "Deutsch"),  # unknown version
        ("IMA", "Deutsch"),  # no prefix matching
        ("IMAX (Originalversion)", "Deutsch"),
    ],
)
def test_version_match_is_exact(title, language):
    assert not n.parse_page(PAGE_HTML, BASE_URL, title, language).version_found


def test_other_versions_still_parse():
    standard = n.parse_page(PAGE_HTML, BASE_URL, "Standardfassung", "Deutsch")
    assert standard.version_found
    assert len(standard.showings) == 4


def test_version_without_showtimes():
    html = """
    <h1>Testfilm</h1>
    <h4 class="version">IMAX<br><small>Sprache: Deutsch</small></h4>
    <div class="shows-scrolling"><div class="day-block">
      <strong class="day-header day-mo"><span class="day-date">03.08.</span></strong>
      <span class="day-body"><span class="sr-only">keine Vorstellungen</span></span>
    </div></div>
    """
    snap = n.parse_page(html, BASE_URL, "IMAX", "Deutsch")
    assert snap.version_found
    assert snap.showings == []


def test_showtime_without_ticket_link_counts_as_sold_out():
    html = """
    <h1>Testfilm</h1>
    <h4 class="version">IMAX<br><small>Sprache: Deutsch</small></h4>
    <div class="shows-scrolling"><span class="day-time soldrate-out">
      <time datetime="2026-08-03T19:00:00">19:00</time>
    </span></div>
    """
    snap = n.parse_page(html, BASE_URL, "IMAX", "Deutsch")
    assert len(snap.showings) == 1
    assert snap.showings[0].sold_out
    assert "~~19:00~~" in n.format_showings(snap.showings)[0]


def test_showtime_without_datetime_is_skipped():
    html = """
    <h1>Testfilm</h1>
    <h4 class="version">IMAX<br><small>Sprache: Deutsch</small></h4>
    <div class="shows-scrolling"><a class="day-time" href="/x.html">19:00</a></div>
    """
    assert n.parse_page(html, BASE_URL, "IMAX", "Deutsch").showings == []


def test_missing_page_structure_does_not_raise():
    snap = n.parse_page("<html><body>maintenance</body></html>", BASE_URL, "IMAX", "Deutsch")
    assert not snap.version_found
    assert snap.showings == []


# --------------------------------------------------------------------------
# decoding
# --------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, content: bytes):
        self.content = content


@pytest.mark.parametrize("encoding", ["utf-8", "cp1252"])
def test_decode_response_handles_both_encodings(encoding):
    text = n.decode_response(_FakeResponse("Tickets für Saal".encode(encoding)))
    assert "für" in text


def test_decode_response_never_raises():
    assert n.decode_response(_FakeResponse(b"\xff\xfe\x00 garbage"))


# --------------------------------------------------------------------------
# state and diffing
# --------------------------------------------------------------------------


def _showing(starts_at: str, sold_out: bool = False) -> n.Showing:
    return n.Showing(starts_at, starts_at[11:16], "https://example/t", "low", sold_out)


def test_state_round_trips(tmp_path, snapshot):
    path = tmp_path / "state.json"
    state = n.State.load(path)
    assert not state.known

    state.save(snapshot.showings, snapshot.film_title, BASE_URL)
    reloaded = n.State.load(path)
    assert reloaded.known
    assert len(reloaded.showings) == 15
    assert json.loads(path.read_text(encoding="utf-8"))["film_title"] == "Die Odyssee"


def test_corrupt_state_starts_fresh(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    assert not n.State.load(path).known


def test_state_with_wrong_version_starts_fresh(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"state_version": 999, "showings": {}}), encoding="utf-8")
    assert not n.State.load(path).known


def test_diff_detects_new_showtimes(tmp_path, snapshot):
    state = n.State.load(tmp_path / "state.json")
    state.save(snapshot.showings, snapshot.film_title, BASE_URL)

    next_week = [_showing("2026-08-17T19:00:00"), _showing("2026-08-18T14:30:00")]
    added, back_on_sale, gone = n.diff(state, snapshot.showings + next_week)
    assert [s.starts_at for s in added] == [s.starts_at for s in next_week]
    assert not back_on_sale and not gone


def test_diff_is_empty_for_an_unchanged_page(tmp_path, snapshot):
    state = n.State.load(tmp_path / "state.json")
    state.save(snapshot.showings, snapshot.film_title, BASE_URL)
    assert n.diff(state, snapshot.showings) == ([], [], [])


def test_diff_detects_a_show_going_back_on_sale(tmp_path, snapshot):
    state = n.State.load(tmp_path / "state.json")
    state.save(snapshot.showings, snapshot.film_title, BASE_URL)
    first = snapshot.showings[0]
    state.showings[first.starts_at]["sold_out"] = True

    added, back_on_sale, gone = n.diff(state, snapshot.showings)
    assert not added
    assert [s.starts_at for s in back_on_sale] == [first.starts_at]


def test_diff_detects_removed_showtimes(tmp_path, snapshot):
    state = n.State.load(tmp_path / "state.json")
    state.save(snapshot.showings, snapshot.film_title, BASE_URL)
    added, back_on_sale, gone = n.diff(state, snapshot.showings[1:])
    assert gone == [snapshot.showings[0].starts_at]
    assert not added and not back_on_sale


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------


def test_format_groups_by_day(snapshot):
    lines = n.format_showings(snapshot.showings)
    assert len(lines) == 10
    assert lines[0].startswith("**Mo, 03.08.**")
    assert lines[0].count("](") == 2  # 14:30 and 19:00


def test_format_handles_a_broken_date():
    line = n.format_showings([_showing("nonsense")])[0]
    assert "nonsense"[:10] in line


def test_chunking_stays_under_the_discord_limit(snapshot):
    lines = n.format_showings(snapshot.showings) * 20
    messages = n.chunk_message("HEADER", lines)
    assert len(messages) > 1
    assert all(len(m) <= n.DISCORD_LIMIT for m in messages)
    assert messages[0].splitlines()[0] == "HEADER"
    assert sum(len(m.splitlines()) for m in messages) == len(lines) + 1


def test_short_messages_are_not_chunked():
    assert len(n.chunk_message("H", ["a", "b"])) == 1


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


@pytest.fixture
def env(monkeypatch):
    for key in list(vars(n.Config)["__dataclass_fields__"]):
        monkeypatch.delenv(key.upper(), raising=False)
    for key in ("TARGET_URL", "DISCORD_WEBHOOK_URL", "CHECK_INTERVAL", "STATE_FILE",
                "NOTIFY_ON_START", "VERSION_TITLE", "VERSION_LANGUAGE", "HEARTBEAT_HOURS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TARGET_URL", BASE_URL)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/abc")
    return monkeypatch


def test_config_defaults_to_german_imax(env):
    config = n.Config.from_env()
    assert config.version_title == "IMAX"
    assert config.version_language == "Deutsch"
    assert config.interval == 120


def test_config_requires_a_target_url(monkeypatch):
    monkeypatch.delenv("TARGET_URL", raising=False)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/abc")
    with pytest.raises(n.ConfigError) as excinfo:
        n.Config.from_env()
    assert "TARGET_URL" in str(excinfo.value)


def test_config_requires_at_least_one_channel(env):
    env.delenv("DISCORD_WEBHOOK_URL")
    env.delenv("NTFY_TOPIC", raising=False)
    with pytest.raises(n.ConfigError) as excinfo:
        n.Config.from_env()
    assert "no notification channel" in str(excinfo.value)


def test_ntfy_alone_is_enough(env):
    env.delenv("DISCORD_WEBHOOK_URL")
    env.setenv("NTFY_TOPIC", "my-secret-topic")
    config = n.Config.from_env()
    assert config.ntfy_topic == "my-secret-topic"
    assert config.ntfy_server == "https://ntfy.sh"
    assert config.webhook_url == ""


def test_ntfy_server_trailing_slash_is_trimmed(env):
    env.setenv("NTFY_TOPIC", "t")
    env.setenv("NTFY_SERVER", "https://push.example.com/")
    assert n.Config.from_env().ntfy_server == "https://push.example.com"


@pytest.mark.parametrize(
    "key,value",
    [
        ("TARGET_URL", "ftp://example.com"),
        ("DISCORD_WEBHOOK_URL", "http://discord.com/api/webhooks/1/abc"),
        ("CHECK_INTERVAL", "5"),
        ("CHECK_INTERVAL", "not-a-number"),
        ("NOTIFY_ON_START", "maybe"),
    ],
)
def test_config_rejects_bad_values(env, key, value):
    env.setenv(key, value)
    with pytest.raises(n.ConfigError):
        n.Config.from_env()


def test_config_reads_overrides(env):
    env.setenv("VERSION_TITLE", "IMAX (Originalversion)")
    env.setenv("VERSION_LANGUAGE", "Originalsprache")
    env.setenv("CHECK_INTERVAL", "600")
    env.setenv("NOTIFY_ON_START", "no")
    config = n.Config.from_env()
    assert config.version_title == "IMAX (Originalversion)"
    assert config.interval == 600
    assert config.notify_on_start is False


# --------------------------------------------------------------------------
# the watcher, against a local server
# --------------------------------------------------------------------------


def _truncate_to_first_week(raw: bytes) -> bytes:
    """Return the page as it looked before the second week was published."""
    html = raw.decode("cp1252")
    start = html.index('<h4 class="version">IMAX<br>')
    end = html.index('<h4 class="version">Originalversion')
    block = html[start:end]
    cut = block.rindex('<div class="day-block">', 0, block.index('day-date">09.08.'))
    return (html[:start] + block[:cut] + "</div></div>" + html[end:]).encode("cp1252")


PAGE_WEEK_1 = _truncate_to_first_week(PAGE_BYTES)


class FakeSite:
    """Serves the film page and collects Discord webhook posts."""

    def __init__(self):
        self.body = PAGE_WEEK_1
        self.status = 200
        self.webhook_status = 204
        self.reject_path = ""  # a path that always 500s, for multi-channel tests
        self.posts: list[dict] = []
        self.headers: list[dict] = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if site.status != 200:
                    self.send_response(site.status)
                    self.end_headers()
                    self.wfile.write(b"error")
                    return
                self.send_response(200)
                # the real server announces utf-8 but sends cp1252
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(site.body)))
                self.end_headers()
                self.wfile.write(site.body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length))
                if site.webhook_status >= 400 or (
                    site.reject_path and self.path == site.reject_path
                ):
                    self.send_response(max(site.webhook_status, 500))
                    self.end_headers()
                    self.wfile.write(b"nope")
                    return
                site.posts.append(payload)
                site.headers.append(dict(self.headers))
                self.send_response(204)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def page_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/film.html"

    @property
    def webhook_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/hook"

    def contents(self) -> list[str]:
        return [p["content"] for p in self.posts]

    def stop(self):
        self.server.shutdown()


@pytest.fixture
def site():
    fake = FakeSite()
    yield fake
    fake.stop()


@pytest.fixture
def watcher(site, tmp_path):
    config = n.Config(
        target_url=site.page_url,
        webhook_url=site.webhook_url,
        interval=15,
        state_file=tmp_path / "state.json",
        failure_alert_after=3,
        http_retries=0,  # keep the suite fast
        http_backoff=0,
        notify_attempts=2,
        notify_retry_delay=0,
    )
    session = n.build_session(config)
    return n.Watcher(config, n.Notifier.from_config(config, session), session)


def test_first_check_stores_a_baseline_and_says_hello(watcher, site):
    watcher.check()
    assert len(site.posts) == 1
    assert "Now watching" in site.contents()[0]
    assert "Die Odyssee" in site.contents()[0]
    assert watcher.config.state_file.exists()
    assert len(watcher.state.showings) == 10


def test_unchanged_page_is_silent(watcher, site):
    watcher.check()
    site.posts.clear()
    watcher.check()
    assert site.posts == []


def test_new_showtimes_trigger_an_alert(watcher, site):
    watcher.check()  # baseline: week 1 only
    site.posts.clear()

    site.body = PAGE_BYTES  # Monday: the cinema publishes week 2
    watcher.check()

    assert len(site.posts) == 1
    message = site.contents()[0]
    assert "5 new IMAX (Deutsch) showtimes" in message
    assert "Die Odyssee" in message
    assert message.count("/TICKET/") == 5
    assert "So, 09.08." in message
    # the original-language IMAX show today must not leak in
    assert "02.08." not in message

    site.posts.clear()
    watcher.check()
    assert site.posts == []  # and it only says it once


def test_alert_is_retried_when_discord_is_down(watcher, site):
    watcher.check()
    site.webhook_status = 500
    site.body = PAGE_BYTES
    watcher.check()
    assert len(watcher.state.showings) == 10  # state not advanced

    site.webhook_status = 204
    watcher.check()
    assert len(site.posts) == 2  # baseline + the retried alert
    assert "5 new" in site.contents()[-1]
    assert len(watcher.state.showings) == 15


def test_repeated_failures_alert_once_then_recover(watcher, site):
    watcher.check()
    site.posts.clear()
    site.status = 503

    for _ in range(5):
        try:
            watcher.check()
        except Exception as exc:  # noqa: BLE001
            watcher.on_failure(exc)

    assert len(site.posts) == 1
    assert "failed checks in a row" in site.contents()[0]

    site.status = 200
    watcher.check()
    watcher.on_success()
    assert "back online" in site.contents()[-1]
    assert watcher.failures == 0


def test_failure_backoff_is_capped(watcher):
    delays = [watcher.on_failure(RuntimeError("boom")) for _ in range(12)]
    assert delays[0] == watcher.config.interval
    assert delays[1] == watcher.config.interval * 2
    assert max(delays) <= watcher.config.max_backoff


def test_missing_version_is_not_a_crash(watcher, site):
    site.body = re.sub(rb'<h4 class="version">IMAX<br>', rb'<h4 class="version">IMAX 70mm<br>', PAGE_BYTES)
    watcher.check()
    assert site.posts == []
    assert not watcher.state.known


def test_page_errors_propagate_to_the_loop(watcher, site):
    site.status = 503
    with pytest.raises(n.requests.HTTPError):
        watcher.check()


def test_run_loop_stops_on_the_stop_event(watcher):
    stop = threading.Event()
    thread = threading.Thread(target=watcher.run, args=(stop,))
    thread.start()
    time.sleep(1)
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


# --------------------------------------------------------------------------
# channels
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "markup,plain",
    [
        ("[14:30](<https://x/t.html>)", "14:30"),
        ("**Sa, 22.08.** — [10:00](<https://x>) · [19:00](<https://y>)",
         "Sa, 22.08. — 10:00 · 19:00"),
        ("~~19:00~~", "19:00"),
        ("@everyone 🎯 **Die Odyssee** is bookable", "🎯 Die Odyssee is bookable"),
        ("plain text", "plain text"),
    ],
)
def test_to_plain_text(markup, plain):
    assert n.to_plain_text(markup) == plain


def test_notifier_fans_out_to_every_channel(site):
    session = n.build_session(n.Config(target_url=site.page_url, http_retries=0))
    notifier = n.Notifier(
        [
            n.DiscordChannel(session, site.webhook_url, attempts=1),
            n.NtfyChannel(session, site.webhook_url, "topic", attempts=1),
        ]
    )
    assert notifier.send("hello", title="T", click_url="https://x")
    assert len(site.posts) == 2


def test_ntfy_payload_shape(site):
    session = n.build_session(n.Config(target_url=site.page_url, http_retries=0))
    channel = n.NtfyChannel(session, site.webhook_url, "my-topic", attempts=1)
    channel.send(
        "🎯 **Die Odyssee** — [22:00](<https://x/ticket.html>)",
        title="🎯 Die Odyssee — Sa, 22.08",
        click_url="https://x/ticket.html",
        priority=True,
    )
    payload = site.posts[0]
    assert payload["topic"] == "my-topic"
    assert payload["message"] == "🎯 Die Odyssee — 22:00"  # markdown stripped
    assert payload["title"] == "🎯 Die Odyssee — Sa, 22.08"
    assert payload["click"] == "https://x/ticket.html"
    assert payload["priority"] == 5  # max, so it cuts through Do-Not-Disturb


def test_ntfy_uses_normal_priority_for_ordinary_alerts(site):
    session = n.build_session(n.Config(target_url=site.page_url, http_retries=0))
    n.NtfyChannel(session, site.webhook_url, "t", attempts=1).send("hi")
    assert site.posts[0]["priority"] == 3


def test_ntfy_sends_auth_token_when_configured(site):
    session = n.build_session(n.Config(target_url=site.page_url, http_retries=0))
    n.NtfyChannel(session, site.webhook_url, "t", token="tk_secret", attempts=1).send("hi")
    assert site.headers[-1].get("Authorization") == "Bearer tk_secret"


def test_ntfy_omits_auth_header_without_a_token(site):
    session = n.build_session(n.Config(target_url=site.page_url, http_retries=0))
    n.NtfyChannel(session, site.webhook_url, "t", attempts=1).send("hi")
    assert "Authorization" not in site.headers[-1]


def test_one_working_channel_is_enough(site, caplog):
    """A broken channel must not make the caller retry and spam the good one."""
    session = n.build_session(n.Config(target_url=site.page_url, http_retries=0))
    working = n.DiscordChannel(session, site.webhook_url, attempts=1)
    broken = n.DiscordChannel(session, f"http://127.0.0.1:{site.port}/dead", attempts=1)
    broken.name = "broken"
    site.reject_path = "/dead"

    assert n.Notifier([working, broken]).send("hello") is True
    assert len(site.posts) == 1


def test_all_channels_failing_is_a_failure(site):
    session = n.build_session(n.Config(target_url=site.page_url, http_retries=0))
    site.webhook_status = 500
    channel = n.DiscordChannel(session, site.webhook_url, attempts=1)
    assert n.Notifier([channel]).send("hello") is False


def test_from_config_builds_the_configured_channels(site):
    session = n.build_session(n.Config(target_url=site.page_url, http_retries=0))
    both = n.Config(
        target_url=site.page_url, webhook_url=site.webhook_url, ntfy_topic="t"
    )
    assert [c.name for c in n.Notifier.from_config(both, session).channels] == [
        "Discord",
        "ntfy",
    ]
    ntfy_only = n.Config(target_url=site.page_url, ntfy_topic="t")
    assert [c.name for c in n.Notifier.from_config(ntfy_only, session).channels] == ["ntfy"]


# --------------------------------------------------------------------------
# watched dates
# --------------------------------------------------------------------------


@pytest.fixture
def date_watcher(site, tmp_path):
    """Watches 09.08. (in week 2 of the fixture) plus a date that never comes."""
    config = n.Config(
        target_url=site.page_url,
        webhook_url=site.webhook_url,
        watch_dates=("2026-08-09", "2026-08-22"),
        mention="@everyone",
        state_file=tmp_path / "state.json",
        notify_on_start=False,
        http_retries=0,
        http_backoff=0,
        notify_attempts=2,
        notify_retry_delay=0,
    )
    session = n.build_session(config)
    return n.Watcher(config, n.Notifier.from_config(config, session), session)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("16.08.2026", ("2026-08-16",)),
        ("2026-08-16", ("2026-08-16",)),
        ("16.08.2026,22.08.2026, 23.08.2026", ("2026-08-16", "2026-08-22", "2026-08-23")),
        ("23.08.2026,16.08.2026", ("2026-08-16", "2026-08-23")),  # sorted
        ("16.08.2026,16.08.2026", ("2026-08-16",)),  # deduped
        ("", ()),
    ],
)
def test_watch_dates_parsing(env, raw, expected):
    env.setenv("WATCH_DATES", raw)
    assert n.Config.from_env().watch_dates == expected


def test_watch_dates_reject_nonsense(env):
    env.setenv("WATCH_DATES", "16.08.2026, next tuesday")
    with pytest.raises(n.ConfigError):
        n.Config.from_env()


def test_watched_date_gets_its_own_alert(date_watcher, site):
    date_watcher.check()  # baseline: week 1, 09.08. not listed yet
    assert site.posts == []
    site.posts.clear()

    site.body = PAGE_BYTES  # week 2 published, 09.08. appears
    date_watcher.check()

    assert len(site.posts) == 2
    target, regular = site.contents()
    assert target.startswith("@everyone 🎯")
    assert "So, 09.08" in target
    assert "bookable" in target
    assert target.count("/TICKET/7167844.html") == 1  # the direct ticket link
    # the watched date is not repeated in the ordinary "new showtimes" message
    assert "09.08." not in regular
    assert "4 new" in regular


def test_watched_date_alert_allows_mentions(date_watcher, site):
    date_watcher.check()
    site.posts.clear()
    site.body = PAGE_BYTES
    date_watcher.check()
    target, regular = site.posts
    assert target["allowed_mentions"] == {"parse": ["everyone", "roles", "users"]}
    assert regular["allowed_mentions"] == {"parse": []}  # ordinary alerts stay quiet


def test_watched_date_is_announced_once(date_watcher, site):
    date_watcher.check()
    site.body = PAGE_BYTES
    date_watcher.check()
    site.posts.clear()

    date_watcher.check()
    assert site.posts == []
    assert date_watcher.state.announced_dates == {"2026-08-09"}


def test_watched_date_announcement_survives_a_restart(date_watcher, site, tmp_path):
    date_watcher.check()
    site.body = PAGE_BYTES
    date_watcher.check()

    restarted = n.Watcher(date_watcher.config, date_watcher.notifier, date_watcher.session)
    assert restarted.state.announced_dates == {"2026-08-09"}
    site.posts.clear()
    restarted.check()
    assert site.posts == []


def test_watched_date_alerts_again_if_it_disappears_and_returns(date_watcher, site):
    date_watcher.check()
    site.body = PAGE_BYTES
    date_watcher.check()

    site.body = PAGE_WEEK_1  # the cinema pulls the second week
    date_watcher.check()
    assert date_watcher.state.announced_dates == set()

    site.posts.clear()
    site.body = PAGE_BYTES  # and puts it back
    date_watcher.check()
    assert any("🎯" in c for c in site.contents())


def test_watched_date_already_listed_at_baseline_alerts(site, tmp_path):
    config = n.Config(
        target_url=site.page_url,
        webhook_url=site.webhook_url,
        watch_dates=("2026-08-08",),  # already in week 1
        state_file=tmp_path / "state.json",
        notify_on_start=True,
        http_retries=0,
        http_backoff=0,
    )
    session = n.build_session(config)
    watcher = n.Watcher(config, n.Notifier.from_config(config, session), session)
    watcher.check()

    assert len(site.posts) == 2
    summary, target = site.contents()
    assert "Now watching" in summary
    assert "🎯 **Sa, 08.08.**" in summary  # marked in the listing too
    assert target.startswith("🎯")  # no mention configured, so no prefix
    assert "Sa, 08.08" in target
    assert watcher.state.announced_dates == {"2026-08-08"}


def test_watched_date_alert_is_retried_when_discord_is_down(date_watcher, site):
    date_watcher.check()
    site.webhook_status = 500
    site.body = PAGE_BYTES
    date_watcher.check()
    assert date_watcher.state.announced_dates == set()
    assert len(date_watcher.state.showings) == 10  # state not advanced

    site.webhook_status = 204
    site.posts.clear()
    date_watcher.check()
    assert any("🎯" in c for c in site.contents())
    assert date_watcher.state.announced_dates == {"2026-08-09"}


def test_unwatched_dates_behave_as_before(watcher, site):
    assert watcher.config.watch_dates == ()
    watcher.check()
    site.posts.clear()
    site.body = PAGE_BYTES
    watcher.check()
    assert len(site.posts) == 1
    assert "🎯" not in site.contents()[0]


def test_format_marks_watched_days(snapshot):
    lines = n.format_showings(snapshot.showings, ("2026-08-08",))
    marked = [line for line in lines if line.startswith("🎯")]
    assert len(marked) == 1
    assert "Sa, 08.08." in marked[0]


def test_health_file_is_touched_after_a_successful_check(site, tmp_path):
    health = tmp_path / "sub" / "health"
    config = n.Config(
        target_url=site.page_url,
        webhook_url=site.webhook_url,
        state_file=tmp_path / "state.json",
        health_file=health,
        notify_on_start=False,
        http_retries=0,
        http_backoff=0,
    )
    session = n.build_session(config)
    watcher = n.Watcher(config, n.Notifier.from_config(config, session), session)
    assert not health.exists()

    watcher.check()
    watcher.on_success()
    assert health.exists()


def test_health_file_is_optional(watcher):
    assert watcher.config.health_file is None
    watcher.touch_health_file()  # must not raise


def test_dry_run_never_posts(site, tmp_path):
    config = n.Config(
        target_url=site.page_url,
        webhook_url=site.webhook_url,
        state_file=tmp_path / "state.json",
        http_retries=0,
        http_backoff=0,
    )
    session = n.build_session(config)
    watcher = n.Watcher(config, n.Notifier.from_config(config, session, dry_run=True), session)
    watcher.check()
    assert site.posts == []
