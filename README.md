# plex-lb-sync

Submit Plex playback history to ListenBrainz — including tracks played offline.

> **Disclaimer:** This is an independent, unofficial hobby project. It is not
> affiliated with, endorsed by, sponsored by, or otherwise connected to Plex,
> ListenBrainz, MetaBrainz, multi-scrobbler, or any other project, product or
> company referenced in this repository. Those names are used solely to describe
> what this tool interoperates with. All trademarks are the property of their
> respective owners. The software is provided as-is, without any warranty — see
> [LICENSE](LICENSE).

## Personal Disclaimer
My skills are pretty shitty, so this is a combination of me and the help of ai. I'm pretty sure there is a lot to improve, but it works for me.

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
- **Fails softly**: a rejected track never aborts a run, and the state only
  advances after a successful submission (only a rejected token stops the pass,
  since retrying other tracks would be pointless)
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

### Installing on a QNAP NAS

Container Station 3.x. The compose file uses `build: .`, so the image is built
from source rather than pulled from a registry — Container Station's "create
application" dialog has no build context for that, which makes SSH the reliable
route here.

**1. Enable SSH** — Control Panel → Telnet/SSH → *Allow SSH connection*. Then
connect from your machine: `ssh admin@<nas-ip>`.

**2. Get the project onto the NAS** — pick whichever of the three fits. Note
that QNAP does not ship `git` by default; `which git` tells you whether yours
has it. Installing it (via Entware) adds a second package manager next to QTS
that firmware updates can break, so it is rarely worth it just to clone a repo —
options B and C avoid it entirely.

*Option A — clone it, if `git` is available:*

```bash
mkdir -p /share/Container/plex-lb-sync-app
cd /share/Container/plex-lb-sync-app
git clone https://github.com/nik0r-404/multi-scrobbler-Plex-offline-sync.git .
```

*Option B — download the archive:*

```bash
mkdir -p /share/Container/plex-lb-sync-app
cd /share/Container/plex-lb-sync-app
curl -L -o repo.zip https://github.com/nik0r-404/multi-scrobbler-Plex-offline-sync/archive/refs/heads/main.zip
unzip repo.zip && mv multi-scrobbler-Plex-offline-sync-main/* . \
  && rm -rf repo.zip multi-scrobbler-Plex-offline-sync-main
```

*Option C — copy it from your own machine.* Handy if you already have the
project checked out locally, or intend to make changes to it: no `git` on the
NAS and no download from the internet. Run this on your machine, not on the NAS:

```bash
rsync -av --exclude '.git' --exclude '.venv' --exclude '.env' --exclude 'data' \
  /path/to/plex-lb-sync/ admin@<nas-ip>:/share/Container/plex-lb-sync-app/
```

`.env` is excluded on purpose: the NAS needs its own, with the paths and volume
layout used there. Use `scp -r` if `rsync` is missing.

**3. Create the data directory with the right ownership** — the container runs
unprivileged as UID 1000, so the directory holding `state.json` must belong to
that UID. Otherwise the container starts fine and then cannot write:

```bash
mkdir -p /share/Container/plex-lb-sync
chown -R 1000:1000 /share/Container/plex-lb-sync
```

**4. Point the volume at that directory** — the compose file ships with
`./data:/data`:

```bash
sed -i 's#- ./data:/data#- /share/Container/plex-lb-sync:/data#' docker-compose.yaml
```

**5. Create the `.env`**

```bash
cp .env.example .env
vi .env
chmod 600 .env        # it holds your tokens
```

```
PLEX_URL=http://<nas-ip>:32400
PLEX_TOKEN=your-plex-token
LISTENBRAINZ_TOKEN=your-listenbrainz-token
PLEX_ACCOUNT_ID=1
TZ=Europe/Berlin
```

Use the NAS IP in `PLEX_URL` even when Plex runs on the same NAS — inside the
container, `localhost` refers to the container itself. The variables at the
bottom of `.env` (`STATE_FILE`, `DRY_RUN`, `RUN_ONCE`, …) only apply to local
runs outside Docker; the compose file overrides them.

**6. Do a dry run first**

```bash
docker compose run --rm -e DRY_RUN=true -e RUN_ONCE=true plex-lb-sync
```

The first invocation builds the image, which takes a minute or two. If
`docker compose` is not found, try `docker-compose` — the command name differs
between Container Station versions.

**7. Start it for good**

```bash
docker compose up -d
docker compose logs -f          # Ctrl+C only stops the log view
```

The container then runs every 15 minutes and comes back after a NAS reboot via
`restart: unless-stopped`. It shows up under *Containers* in Container Station,
where you can read logs and stop or start it — but keep managing its
configuration over SSH, otherwise the UI and the compose file drift apart.

**Updating later** — fetch the new version the same way you installed it, then
rebuild. The `--build` is what matters: without it Docker reuses the old image
and nothing changes.

```bash
cd /share/Container/plex-lb-sync-app

git pull                      # option A
# or re-download and unpack the archive over it   (option B)
# or rsync from your machine again                (option C)

docker compose up -d --build
docker compose logs -f
```

Your `.env` and the state file live outside the image — the first next to the
compose file, the second in the mounted directory — so an update touches
neither. Already-submitted tracks are not submitted again.

If a rebuild goes wrong, `docker compose down` stops everything and the previous
image is still there; `docker image ls` lists what you can roll back to.

### On other NAS systems

The same principles apply: mount a share into `/data`, make it writable by UID
1000, and set `TZ` so log timestamps match local time.

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
| `PLEX_MAX_PAGES` | no | `50` | Upper bound on Plex history pages per pass (100 entries each). Raise it if a pass logs that older plays were not fetched |
| `LISTENBRAINZ_URL` | no | `https://api.listenbrainz.org` | For self-hosted ListenBrainz instances |
| `LOG_LEVEL` | no | `INFO` | `DEBUG` for more detail |
| `TZ` | no | UTC | Timezone of the log output, e.g. `Europe/Berlin` |

¹ Not needed in a dry run — but without it the cross-check against existing
listens is skipped, so the dry-run preview may list plays another scrobbler
already submitted.

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
2026-01-15 22:10:01 INFO    plex-lb-sync 1.1.0 started -- Plex http://plex.local:32400, window 72h, state /data/state.json, accountID 1
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

## Troubleshooting

| Symptom | Cause |
|---|---|
| `Missing environment variables` | `.env` is not next to `docker-compose.yaml`, or the values are empty |
| `State file /data/state.json is not writable` | The mounted directory does not belong to UID 1000 |
| `Plex request failed: ... Connection refused` | Wrong `PLEX_URL`, or `localhost` used from inside the container |
| `No music library found` | The token belongs to a user without access to the library, or there is no library of type `artist` |
| `Plex history: 0 track entries in the window` | Nothing was played inside `LOOKBACK_HOURS` — or the Plex token belongs to a different account than `PLEX_ACCOUNT_ID` |
| Container runs but never submits anything | `DRY_RUN` is still `"true"` in `docker-compose.yaml` |
| Log timestamps in the wrong timezone | `TZ` is not set |

For a closer look at what Plex reports, run `verify_plex_history.py` — see
[Verifying your setup](#verifying-your-setup).

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
