[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

# I need IMAX Seats (╯°□°)╯︵ ┻━┻

Watches a [Traumpalast](https://leonberg.traumpalast.de/) film page and pings a
Discord channel as soon as new showtimes appear for **one specific screening
version** — by default the German IMAX one, not the original-language version.

The cinema publishes the next two weeks of showtimes, usually on Mondays. This
notifier polls the page, compares the showtimes it finds against the ones it has
already seen, and posts only the difference:

```
🎬 Die Odyssee — 5 new IMAX (Deutsch) showtimes!
https://leonberg.traumpalast.de/index.php/FN/8415/PID/5574.html
So, 09.08. — 14:30
Mo, 10.08. — 19:00
Di, 11.08. — 14:30
Mi, 12.08. — 14:30 · 19:00
```

Each time links straight to its ticket page.

## Dates you actually want

Set `WATCH_DATES` to the days you are trying to book. Those get their own
alert, separate from the general listing, with a `MENTION` prefix so your phone
pushes even if the channel is set to *Only @mentions*:

```
@everyone 🎯 Die Odyssee is now bookable on Sa, 22.08, So, 23.08 (IMAX (Deutsch))!
🎯 Sa, 22.08. — 14:30 · 19:00
🎯 So, 23.08. — 10:00 · 19:00
```

A watched date is announced the first time it appears on the page at all — not
only when a showtime is "new" — so adding a date later still alerts you, and a
date that gets pulled and re-published alerts again. It is announced once, and
the state file remembers that across restarts.

## How it picks the right showtimes

The film page lists every version under an `<h4 class="version">` heading —
`Standardfassung`, `IMAX`, `Originalversion`, `IMAX (Originalversion)` and so
on. The notifier matches that heading **exactly** (title *and* the language
line below it), then reads only the showtimes underneath it. That is why
`VERSION_TITLE=IMAX` never picks up `IMAX (Originalversion)`.

Showtimes are identified by their start time, so re-published listings or
changed ticket IDs do not cause duplicate alerts. Seen showtimes are written to
`STATE_FILE`, which means a restart stays quiet instead of announcing the whole
programme again.

Besides new showtimes it also reports a sold-out screening becoming bookable
again, and warns you on Discord if the site has been unreachable for several
checks in a row.

## Getting it onto your phone

Two channels, usable together — an alert counts as delivered if at least one
of them accepts it, so a broken channel never blocks or duplicates the other.

**Discord** (`DISCORD_WEBHOOK_URL`) works if the channel is set to *All
Messages*; if it is on *Only @mentions*, set `MENTION=@everyone` so watched-date
alerts still push.

**ntfy.sh** (`NTFY_TOPIC`) is the more reliable option and needs no account:
install the [ntfy app](https://ntfy.sh/), subscribe to a topic, and put that
topic in `.env`. Watched-date alerts go out at max priority (audible through
Do Not Disturb on Android) and tapping the notification opens the ticket page
directly. Topics are **public to anyone who knows the name**, so use a long
random one — the notifier never sends anything but showtimes, but pick
`imax-seats-<random>` rather than `imax`.

Verify either one with:

```bash
python notifier.py --test-notify
```

## Setup

```bash
git clone https://github.com/xD33m/imax-seats.git
cd imax-seats
pip install -r requirements.txt
cp .env.example .env   # then fill in TARGET_URL and DISCORD_WEBHOOK_URL
```

`TARGET_URL` is the film page you want to watch — open the film on the
Traumpalast site and copy the URL. `DISCORD_WEBHOOK_URL` comes from your Discord
channel under *Settings → Integrations → Webhooks → New Webhook*.

Check that it sees the right showtimes before letting it run:

```bash
python notifier.py --show
```

That prints the showtimes of the configured version, says whether each watched
date is listed yet, and exits — no Discord message, no state file. Compare it
against the row on the website. Then:

```bash
python notifier.py
```

### Docker

```bash
docker compose up -d --build
```

State lives in `./data` on the host, so the container can be recreated without
re-announcing everything. Logs: `docker compose logs -f`.

## Configuration

Everything is set through the environment (or `.env`). `TARGET_URL` plus at
least one channel is required; see [.env.example](.env.example) for the full
list.

| Variable | Default | Meaning |
| --- | --- | --- |
| `TARGET_URL` | — | Film page to watch |
| `DISCORD_WEBHOOK_URL` | — | Discord webhook to post to |
| `NTFY_TOPIC` | — | ntfy topic to push to (enables the ntfy channel) |
| `NTFY_SERVER` | `https://ntfy.sh` | ntfy server, if you self-host |
| `NTFY_TOKEN` | — | Bearer token, for a protected ntfy topic |
| `VERSION_TITLE` | `IMAX` | Version heading to match, exactly |
| `VERSION_LANGUAGE` | `Deutsch` | Language line under the heading (empty = ignore) |
| `WATCH_DATES` | — | Dates that get their own alert, e.g. `16.08.2026,22.08.2026` |
| `MENTION` | — | Prefix for watched-date alerts, e.g. `@everyone` |
| `CHECK_INTERVAL` | `120` | Seconds between checks (minimum 15) |
| `STATE_FILE` | `state.json` | Where already-announced showtimes are stored |
| `HEALTH_FILE` | — | Touched after each successful check, for the Docker healthcheck |
| `NOTIFY_ON_START` | `true` | Post a summary of what is listed on startup |
| `NOTIFY_SOLD_OUT_CHANGES` | `true` | Also report showtimes going back on sale |
| `FAILURE_ALERT_AFTER` | `3` | Consecutive failures before a warning is posted |
| `MAX_BACKOFF` | `1800` | Cap for the backoff after failures, in seconds |
| `REQUEST_TIMEOUT` | `20` | HTTP timeout in seconds |
| `HTTP_RETRIES` | `3` | Retries per HTTP request |
| `HEARTBEAT_HOURS` | `0` | "Still alive" message every N hours (0 = off) |

To watch the English IMAX version instead:

```
VERSION_TITLE=IMAX (Originalversion)
VERSION_LANGUAGE=Originalsprache
```

## Command line

```
python notifier.py                 # run the watcher
python notifier.py --show          # print the current showtimes and exit
python notifier.py --test-notify   # send one test message to every channel
python notifier.py --once          # single check, then exit (for cron)
python notifier.py --dry-run       # log what would be posted, post nothing
python notifier.py --verbose       # debug logging
```

`--once` exits non-zero when the check fails, so it works as a cron job:

```
*/5 * * * * cd /opt/imax-seats && python notifier.py --once >> watch.log 2>&1
```

## Behaviour worth knowing

- **Failures do not lose alerts.** If Discord cannot be reached, the state file
  is not advanced, so the same showtimes are reported on the next check.
- **Backoff.** Repeated failures back off exponentially up to `MAX_BACKOFF`,
  and only one warning is posted per outage, followed by a recovery message.
- **Encoding.** The Traumpalast server declares `charset=utf-8` but actually
  sends cp1252; the notifier decodes defensively rather than trusting either.
- **Missing version.** If the version disappears from the page (e.g. the film
  stops running in IMAX), that is logged once and nothing is announced.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The suite parses a saved copy of a real film page and runs the watcher against a
local HTTP server that impersonates both the cinema and Discord, including the
Monday case where a second week of showtimes appears.

## License

MIT License
