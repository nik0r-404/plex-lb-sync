# plex-lb-sync

Submit Plex playback history to ListenBrainz — including tracks played offline.

> **Disclaimer:** This is an independent, unofficial hobby project. It is not
> affiliated with, endorsed by, sponsored by, or otherwise connected to Plex,
> ListenBrainz, MetaBrainz, multi-scrobbler, or any other project, product or
> company referenced in this repository. Those names are used solely to describe
> what this tool interoperates with. All trademarks are the property of their
> respective owners. The software is provided as-is, without any warranty — see
> [LICENSE](LICENSE).

## The problem

If you use [multi-scrobbler](https://github.com/FoxxMD/multi-scrobbler) to send
your Plex plays to [ListenBrainz](https://listenbrainz.org/), everything you
listen to while connected shows up fine. Everything you listen to **offline**
does not.

Plexamp lets you download tracks and play them without a network connection. On
the next reconnect it reports those plays back to the Plex server, where they
land in the playback history. multi-scrobbler never sees them, because it only
polls *active sessions* — a play that already finished is invisible to it. This
is a known open issue: [FoxxMD/multi-scrobbler#409](https://github.com/FoxxMD/multi-scrobbler/issues/409).

The result is a listening history with a hole in it, exactly where your commute,
your flight or your basement gym would be — which in turn skews anything built
on that history, such as recommendation playlists.

## What this tool does

`plex-lb-sync` periodically reads the Plex playback history and submits every
music track that has not reached ListenBrainz yet. It runs alongside
multi-scrobbler; you do not have to replace it.

- **Reads the Plex history**, not active sessions — so it sees plays reported
  after the fact
- **Cross-checks against ListenBrainz** before submitting, so tracks another
  scrobbler already reported are not scrobbled twice
- **Keeps a state file** so nothing is submitted repeatedly
- **Fails softly**: a single failed track never aborts a run, and the state only
  advances after a successful submission
- **Dry-run mode** so you can see exactly what would be submitted
- Small container, standard library plus `requests`

## Requirements

- A Plex Media Server with a music library and a Plex token
- A ListenBrainz account and API token
- Either Docker (recommended) or Python 3.9+

## Quick start

```bash
git clone https://github.com/nik0r-404/multi-scrobbler-Plex-offline-sync.git plex-lb-sync
cd plex-lb-sync
cp .env.example .env
$EDITOR .env          # fill in PLEX_URL, PLEX_TOKEN, LISTENBRAINZ_TOKEN
```

Verify that your setup behaves as expected (see
[Verifying your setup](#verifying-your-setup) — worth doing before the first
real run):

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
set -a; source .env; set +a
.venv/bin/python verify_plex_history.py --limit 30
```

Then do a dry run, which submits nothing and writes no state:

```bash
.venv/bin/python plex_lb_sync.py       # DRY_RUN=true is set in .env.example
```

If the output looks right, run it for real:

```bash
DRY_RUN=false .venv/bin/python plex_lb_sync.py
```

## Getting the tokens

**Plex token** — in the Plex web UI, open any item, choose `⋯` → *Get Info* →
*View XML*. The URL of the tab that opens contains `X-Plex-Token=…`. See
[the official article](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).

**ListenBrainz token** — visit [listenbrainz.org/settings](https://listenbrainz.org/settings/)
while logged in.

## Running with Docker

```bash
cp .env.example .env
$EDITOR .env
mkdir -p data && chown 1000:1000 data     # the container runs as UID 1000
docker compose up -d
docker compose logs -f
```

The container runs a loop and checks every `INTERVAL_MINUTES` (default 15).
State lives in `data/state.json` via the volume mount, so it survives restarts.

Set `DRY_RUN: "true"` in `docker-compose.yaml` for the first start, watch the
logs, then switch it back to `"false"` and run `docker compose up -d` again.

### On a NAS

The `docker-compose.yaml` mounts `./data`. On a NAS such as QNAP or Synology,
point that at a share instead and create it beforehand:

```yaml
    volumes:
      - /share/Container/plex-lb-sync:/data
```

```bash
mkdir -p /share/Container/plex-lb-sync
chown 1000:1000 /share/Container/plex-lb-sync
```

The directory must be writable by UID 1000 — the container deliberately does not
run as root. Also set `TZ` in your `.env` so log timestamps match your local
time, e.g. `TZ=Europe/Berlin`.

## Configuration

Everything is configured through environment variables:

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `PLEX_URL` | yes | – | Base URL of the Plex server, e.g. `http://plex.local:32400` |
| `PLEX_TOKEN` | yes | – | X-Plex-Token |
| `LISTENBRAINZ_TOKEN` | yes¹ | – | Token from https://listenbrainz.org/settings/ |
| `PLEX_ACCOUNT_ID` | no | empty | Only scrobble plays of this Plex user. Empty = all users |
| `PLEX_LIBRARY_SECTION` | no | auto | ID of the music library. Empty = first library of type `artist` |
| `LOOKBACK_HOURS` | no | `72` | How far back each run looks (see below) |
| `DUPLICATE_WINDOW_SECONDS` | no | `600` | Tolerance when cross-checking against existing listens. `0` = cross-check off |
| `STATE_FILE` | no | `/data/state.json` | Path of the state file |
| `DRY_RUN` | no | `false` | `true` = only print what would be submitted; sends nothing, writes no state |
| `INTERVAL_MINUTES` | no | `15` | Delay between two passes |
| `RUN_ONCE` | no | `false` | `true` = one pass, then exit (for testing or an external scheduler) |
| `REQUEST_TIMEOUT` | no | `30` | Timeout in seconds for Plex and ListenBrainz calls |
| `LISTENBRAINZ_URL` | no | `https://api.listenbrainz.org` | For self-hosted ListenBrainz instances |
| `LOG_LEVEL` | no | `INFO` | `DEBUG` for more detail |
| `TZ` | no | UTC | Timezone of the log output, e.g. `Europe/Berlin` |

¹ Not needed in a dry run.

## Verifying your setup

Before relying on this tool, confirm that your Plex server actually records
offline plays the way this tool assumes. `verify_plex_history.py` reads the
history and analyses it — it never writes or submits anything:

```bash
set -a; source .env; set +a
.venv/bin/python verify_plex_history.py --limit 30
```

A useful test: take a baseline, then enable airplane mode, play two or three
downloaded tracks to the end, reconnect, wait a few minutes with Plexamp in the
foreground, and compare.

```bash
.venv/bin/python verify_plex_history.py --limit 30 > baseline.txt
# ... play offline, reconnect, wait ...
.venv/bin/python verify_plex_history.py --limit 30 > after.txt
diff baseline.txt after.txt
```

Three things are worth checking:

1. **Do the offline tracks appear at all?**
2. **Is `viewedAt` the real listening time or the sync time?** The `delta`
   column answers this: gaps in the range of track lengths (roughly 120–400 s)
   mean real listening times. Several tracks sharing a timestamp within a few
   seconds of each other mean Plex recorded the moment of the report instead —
   in which case the timestamps are not usable and this tool is not much help.
3. **Do all played tracks appear, or only some?**

Note that Plex only counts a track as played once roughly 90 % of it has
elapsed, so let each track finish during the test.

## How it works

### One pass

1. Query the Plex history:
   `/status/sessions/history/all?librarySectionID=<music>&viewedAt>=<ts>&sort=viewedAt:desc`,
   paginated to the edge of the time window
2. Drop entries that have already been submitted successfully
3. Drop candidates another scrobbler has already reported (see
   [Cross-checking](#cross-checking-against-listenbrainz))
4. Submit each remaining track to
   `POST https://api.listenbrainz.org/1/submit-listens`
5. Update the state file — only after a successful submission

### Field mapping

| Plex history field | ListenBrainz |
|---|---|
| `title` | `track_metadata.track_name` |
| `grandparentTitle` | `track_metadata.artist_name` |
| `parentTitle` | `track_metadata.release_name` (omitted if empty) |
| `viewedAt` | `listened_at` |
| `ratingKey` | `additional_info.origin_url` as `plex://track/<id>` |
| `accountID` | used as a filter, not submitted |

Every submission carries `submission_client: plex-lb-sync`, so you can tell in
ListenBrainz which listens came from this tool.

### Why `type=10` is not used

The Plex API documentation and most examples suggest filtering the history for
audio tracks with `type=10`. **On `/status/sessions/history/all` that filter has
no effect** — it returns an empty result, with HTTP 200 and no error. A tool
built on it silently reports "0 entries found" forever.

What does work on this endpoint: `librarySectionID`, `accountID` and
`viewedAt>=`. This tool therefore resolves the music library (the first library
of type `artist` under `/library/sections`, overridable with
`PLEX_LIBRARY_SECTION`) and filters client-side on `type == "track"`.

### Why selection is not based on a timestamp alone

The obvious design would be "submit everything newer than the last run". For
this particular use case that breaks.

When a client reports offline plays, Plex records the **real listening time** in
`viewedAt`. Those entries therefore appear *behind* a high-water mark, in the
past. Concretely: the tool runs at 10:00 and stores 10:00 as its mark. You were
offline from 08:00 to 09:00, get home at 10:05, and Plexamp reports the plays.
The run at 10:15 asks for "everything newer than 10:00" and misses the 08:00
entries — permanently.

Instead, every run looks back a fixed `LOOKBACK_HOURS` and remembers the
**keys** of the history entries it has submitted (`historyKey`, falling back to
`ratingKey:viewedAt`) in the state file. Entries that fall out of the window are
dropped from the state, so it does not grow without bound.

**Practical consequence:** `LOOKBACK_HOURS` has to cover the longest offline
period you expect. A week without a connection means `168`. Anything older than
the window is never submitted.

### Cross-checking against ListenBrainz

ListenBrainz de-duplicates listens by user, timestamp and track name, so you
might expect double submissions to be harmless. They are not, because **the
timestamps do not match**:

| Track | multi-scrobbler | plex-lb-sync | Δ |
|---|---|---|---|
| Track A | 17:30:59 | 17:32:19 | 80 s |
| Track B | 17:34:05 | 17:36:06 | 121 s |
| Track C | 17:38:05 | 17:40:03 | 118 s |
| Track D | 17:41:46 | 17:43:13 | 87 s |

The reason: **multi-scrobbler reports a track while it is playing**, as soon as
it counts as played. **Plex sets `viewedAt` when the track ends.** The two
timestamps differ by one to two minutes, and ListenBrainz only de-duplicates on
an exact match. Without a countermeasure, every track you play *online* is
counted twice — over-weighting exactly the music you listen to at home.

So before submitting, each run fetches the listens that already exist in the
window (`GET /1/user/<name>/listens`) and skips any candidate for which a listen
with the same artist and track already exists within `DUPLICATE_WINDOW_SECONDS`
— regardless of which client reported it. Skipped entries go into the state file
and are not checked again.

Two properties worth knowing:

- **If the lookup fails, nothing is filtered.** An outage can produce a
  duplicate at worst, but never a lost play.
- **A track played twice within the tolerance is skipped the second time.** At
  the default 600 s this only affects genuinely repeating a short track. Lower
  the value to roughly your average track length if you care;
  `DUPLICATE_WINDOW_SECONDS=0` disables the cross-check entirely.

If you ever stop using multi-scrobbler for Plex, the cross-check can stay on —
it then costs one extra request per run and changes nothing about the result.

### A loop instead of cron

The container runs its own loop (`INTERVAL_MINUTES`, default 15) rather than a
cron daemon:

- A slim `python:3.12-slim` image ships no cron; it would have to be installed
  and supervised as a second process next to PID 1.
- Cron jobs do not inherit the container's environment variables — they would
  have to be written to a file at startup and sourced again in the job.
- Logs go straight to stdout and are visible in `docker logs` without detours.
  Cron would send them to a file or the mail spool.
- Restart behaviour is handled by Docker via `restart: unless-stopped`.

If you prefer an external scheduler, set `RUN_ONCE=true` and invoke the
container from it:

```bash
docker compose run --rm -e RUN_ONCE=true plex-lb-sync
```

## Example output

```
2026-01-15 22:10:01 INFO    plex-lb-sync 1.0.0 started -- Plex http://plex.local:32400, window 72h, state /data/state.json, accountID 1
2026-01-15 22:10:01 INFO    Cross-check against existing listens is active (tolerance 600s). Set DUPLICATE_WINDOW_SECONDS=0 to disable it.
2026-01-15 22:10:01 INFO    No state file at /data/state.json -- first run.
2026-01-15 22:10:01 INFO    First run: looking back 72 hours (from 2026-01-12 22:10:01).
2026-01-15 22:10:01 INFO    Music library detected: Music (librarySectionID=3)
2026-01-15 22:10:02 INFO    Plex history: 12 track entries in the window, 5 of them not submitted yet.
2026-01-15 22:10:03 INFO    DUP already scrobbled (-82s): 2026-01-15 18:12:44 | Portishead - Roads [Dummy]
2026-01-15 22:10:03 INFO    SENT 2026-01-15 08:18:41 | Boards of Canada - Roygbiv [Music Has the Right to Children]
2026-01-15 22:10:04 INFO    SENT 2026-01-15 08:21:56 | Boards of Canada - Rue the Whirl [Geogaddi]
2026-01-15 22:10:04 INFO    Run finished: 2 submitted, 3 duplicates, 0 skipped, 0 pending (retried next run).
2026-01-15 22:10:04 INFO    Next run in 15 minutes.
```

Log lines you may see:

| Prefix | Meaning |
|---|---|
| `SENT` | Submitted successfully |
| `DUP` | Already present at ListenBrainz, skipped |
| `SKIP` | Entry incomplete or timestamp in the future |
| `RETRY` | Temporary failure, will be attempted again next run |
| `DROP` | Permanently rejected by ListenBrainz, will not be retried |

## Limitations

- **Tracks older than `LOOKBACK_HOURS` are never submitted.** Raise the value if
  you go offline for longer stretches.
- **Plex sometimes records plays that did not happen** — a track loaded into the
  queue can end up in the history. The history entries carry no playback
  duration, so this cannot be filtered out.
- **Repeat plays within `DUPLICATE_WINDOW_SECONDS` are collapsed into one.**
- **Only music is handled.** The library is resolved by type `artist`; other
  media types are ignored.
- **Timestamps depend on your Plex server's clock.** If it drifts, so do your
  listens.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest tests/ -q
```

The test suite runs without network access — Plex and ListenBrainz are replaced
by fakes. Contributions are welcome; please keep the tests passing and add cases
for new behaviour.

## License

MIT — see [LICENSE](LICENSE).
