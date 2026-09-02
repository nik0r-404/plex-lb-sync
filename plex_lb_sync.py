#!/usr/bin/env python3
"""Backfill scrobbler: Plex playback history -> ListenBrainz.

Plexamp reports offline playback to the Plex server once the device reconnects.
multi-scrobbler only polls active sessions and therefore never sees those plays
(FoxxMD/multi-scrobbler#409). This tool periodically reads the Plex history and
submits everything that has not reached ListenBrainz yet.

Configuration is done entirely through environment variables, see README.md.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests

__version__ = "1.1.0"

CLIENT_NAME = "plex-lb-sync"
PLEX_TRACK_TYPE = "track"  # value of the "type" field in a history entry
PLEX_PAGE_SIZE = 100
LB_MAX_ATTEMPTS = 3
LB_LISTENS_PAGE_SIZE = 100  # maximum of the /1/user/<name>/listens endpoint
LB_MAX_LISTEN_PAGES = 50  # safety net against never-ending pagination
LB_SUBMIT_BATCH_SIZE = 50  # listens per submit-listens request (listen_type "import")
LB_MAX_RATE_WAIT = 300  # upper bound for the wait suggested by X-RateLimit-Reset-In
STATE_VERSION = 1

log = logging.getLogger(CLIENT_NAME)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

class ConfigError(RuntimeError):
    """Missing or unusable configuration."""


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int, minimum: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {value}")
    return value


@dataclass(frozen=True)
class Config:
    plex_url: str
    plex_token: str
    plex_account_id: str
    plex_library_section: str
    listenbrainz_token: str
    listenbrainz_url: str
    lookback_hours: int
    duplicate_window: int
    state_file: str
    dry_run: bool
    interval_minutes: int
    run_once: bool
    request_timeout: int
    plex_max_pages: int = 50  # safety net against never-ending pagination

    @classmethod
    def from_env(cls) -> "Config":
        plex_url = os.environ.get("PLEX_URL", "").strip().rstrip("/")
        plex_token = os.environ.get("PLEX_TOKEN", "").strip()
        listenbrainz_token = os.environ.get("LISTENBRAINZ_TOKEN", "").strip()
        dry_run = env_bool("DRY_RUN", False)

        missing = [
            name
            for name, value in (("PLEX_URL", plex_url), ("PLEX_TOKEN", plex_token))
            if not value
        ]
        # Nothing can be submitted without the LB token -- a dry run does not need it.
        if not listenbrainz_token and not dry_run:
            missing.append("LISTENBRAINZ_TOKEN")
        if missing:
            raise ConfigError("Missing environment variables: " + ", ".join(missing))

        return cls(
            plex_url=plex_url,
            plex_token=plex_token,
            plex_account_id=os.environ.get("PLEX_ACCOUNT_ID", "").strip(),
            plex_library_section=os.environ.get("PLEX_LIBRARY_SECTION", "").strip(),
            listenbrainz_token=listenbrainz_token,
            listenbrainz_url=os.environ.get(
                "LISTENBRAINZ_URL", "https://api.listenbrainz.org"
            ).strip().rstrip("/"),
            lookback_hours=env_int("LOOKBACK_HOURS", 72, minimum=1),
            duplicate_window=env_int("DUPLICATE_WINDOW_SECONDS", 600, minimum=0),
            state_file=os.environ.get("STATE_FILE", "/data/state.json").strip(),
            dry_run=dry_run,
            interval_minutes=env_int("INTERVAL_MINUTES", 15, minimum=1),
            run_once=env_bool("RUN_ONCE", False),
            request_timeout=env_int("REQUEST_TIMEOUT", 30, minimum=1),
            plex_max_pages=env_int("PLEX_MAX_PAGES", 50, minimum=1),
        )


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

@dataclass
class State:
    """What has already been submitted successfully.

    ``seen`` maps Plex history entries to their ``viewedAt`` timestamp and is
    only extended after a successful submission. The selection of entries to
    submit deliberately goes through ``seen`` instead of a plain high-water
    timestamp: because Plex records the real listening time when a client
    reports offline plays, those entries appear *behind* the newest submission
    and a timestamp comparison would swallow them.
    """

    last_run_at: int = 0
    seen: dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.seen is None:
            self.seen = {}

    @property
    def is_fresh(self) -> bool:
        return self.last_run_at == 0 and not self.seen


def load_state(path: str) -> State:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        log.info("No state file at %s -- first run.", path)
        return State()
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("State file %s is unreadable (%s) -- starting with empty state.", path, exc)
        return State()

    if not isinstance(raw, dict):
        log.warning("State file %s has an unexpected format -- starting with empty state.", path)
        return State()

    seen_raw = raw.get("seen", {})
    seen: dict[str, int] = {}
    if isinstance(seen_raw, dict):
        for key, value in seen_raw.items():
            try:
                seen[str(key)] = int(value)
            except (TypeError, ValueError):
                continue

    def as_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    return State(
        last_run_at=as_int(raw.get("last_run_at")),
        seen=seen,
    )


def save_state(path: str, state: State) -> None:
    payload = {
        "version": STATE_VERSION,
        "last_run_at": state.last_run_at,
        "seen": state.seen,
    }
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    # Write atomically so a container restart in the middle of a write does not
    # leave a truncated file behind.
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, prefix=".state-", suffix=".tmp", delete=False
    )
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# Plex
# --------------------------------------------------------------------------

class PlexError(RuntimeError):
    """Plex is unreachable or replied with something unusable."""


def entry_key(entry: dict[str, Any]) -> str:
    """Stable identifier of a history entry.

    ``historyKey`` is unique per entry; if it is missing, the combination of
    track and playback timestamp identifies the entry closely enough.
    """
    history_key = entry.get("historyKey")
    if isinstance(history_key, str) and history_key:
        return history_key
    return f"{entry.get('ratingKey', '?')}:{entry.get('viewedAt', '?')}"


_section_cache: dict[str, str] = {}


def resolve_library_section(config: Config, session: requests.Session) -> str:
    """Determine the music library id -- configured or auto-detected.

    The result is cached; it does not change while the tool is running.
    """
    if config.plex_library_section:
        return config.plex_library_section
    cached = _section_cache.get(config.plex_url)
    if cached:
        return cached

    try:
        response = session.get(
            f"{config.plex_url}/library/sections",
            headers={"Accept": "application/json", "X-Plex-Token": config.plex_token},
            timeout=config.request_timeout,
        )
        response.raise_for_status()
        directories = response.json().get("MediaContainer", {}).get("Directory", [])
    except requests.RequestException as exc:
        raise PlexError(f"Could not list libraries: {exc}") from exc
    except (ValueError, AttributeError) as exc:
        raise PlexError(f"Library list is unusable: {exc}") from exc

    for directory in directories:
        if isinstance(directory, dict) and directory.get("type") == "artist":
            section = str(directory.get("key"))
            log.info("Music library detected: %s (librarySectionID=%s)", directory.get("title"), section)
            _section_cache[config.plex_url] = section
            return section

    raise PlexError("No music library found -- please set PLEX_LIBRARY_SECTION.")


def fetch_history(config: Config, session: requests.Session, since: int) -> list[dict[str, Any]]:
    """Fetch all track entries with ``viewedAt >= since`` (newest first).

    Note: the obvious filter ``type=10`` has no effect on this endpoint -- it
    silently returns an empty result. Filtering therefore goes through
    ``librarySectionID`` plus a client-side check on the ``type`` field.
    """
    url = f"{config.plex_url}/status/sessions/history/all"
    section = resolve_library_section(config, session)
    collected: list[dict[str, Any]] = []
    known: set[str] = set()
    start = 0

    while True:
        params: dict[str, Any] = {
            "librarySectionID": section,
            "viewedAt>=": since,
            "sort": "viewedAt:desc",
            "X-Plex-Container-Start": start,
            "X-Plex-Container-Size": PLEX_PAGE_SIZE,
        }
        if config.plex_account_id:
            params["accountID"] = config.plex_account_id

        try:
            response = session.get(
                url,
                params=params,
                headers={"Accept": "application/json", "X-Plex-Token": config.plex_token},
                timeout=config.request_timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise PlexError(f"Plex request failed: {exc}") from exc
        except ValueError as exc:
            raise PlexError(f"Plex did not return valid JSON: {exc}") from exc

        container = payload.get("MediaContainer") if isinstance(payload, dict) else None
        if not isinstance(container, dict):
            raise PlexError("Plex response contains no MediaContainer")

        page = container.get("Metadata") or []
        if not isinstance(page, list) or not page:
            break

        # Evaluate the whole page instead of stopping at the first old entry: a
        # single outlier in the sort order would otherwise silently swallow
        # everything behind it.
        in_window = 0
        outside_window = 0
        added = 0
        for entry in page:
            if not isinstance(entry, dict):
                continue
            if entry.get("type") != PLEX_TRACK_TYPE:
                continue
            viewed_at = entry.get("viewedAt")
            if not isinstance(viewed_at, int):
                continue
            if viewed_at < since:
                outside_window += 1
                continue
            in_window += 1
            # Plex does not honour X-Plex-Container-Start in every combination;
            # without this guard the same entry would be collected repeatedly.
            key = entry_key(entry)
            if key in known:
                continue
            known.add(key)
            collected.append(entry)
            added += 1

        if len(page) < PLEX_PAGE_SIZE:
            break
        # The sort order is descending: a page with no in-window entries at all
        # means everything that follows is older still. A page that merely
        # *contains* old entries is not enough -- a single sort-order outlier
        # must not abort the pagination.
        if outside_window and not in_window:
            break
        if in_window and not added:
            log.warning(
                "Plex returned no new entries at offset %d -- stopping pagination "
                "(X-Plex-Container-Start is likely being ignored; entries beyond "
                "this point cannot be fetched).",
                start,
            )
            break
        start += PLEX_PAGE_SIZE
        if start >= PLEX_PAGE_SIZE * config.plex_max_pages:
            log.error(
                "Stopping after %d pages of Plex history -- older plays in the window "
                "are not fetched. Raise PLEX_MAX_PAGES or lower LOOKBACK_HOURS.",
                config.plex_max_pages,
            )
            break

    return collected


def select_new(
    entries: list[dict[str, Any]], state: State, window_start: int, account_id: str
) -> list[dict[str, Any]]:
    """Entries not submitted yet, oldest first."""
    candidates: list[dict[str, Any]] = []
    for entry in entries:
        viewed_at = entry.get("viewedAt")
        if not isinstance(viewed_at, int) or viewed_at < window_start:
            continue
        if account_id and str(entry.get("accountID", "")) != str(account_id):
            continue
        if entry_key(entry) in state.seen:
            continue
        candidates.append(entry)
    # Submit in ascending order so an abort halfway through a run leaves the
    # remaining (newer) entries for the next pass without skipping anything.
    candidates.sort(key=lambda item: item["viewedAt"])
    return candidates


def prune_seen(state: State, window_start: int) -> int:
    """Forget entries outside the search window -- they can never be candidates again."""
    stale = [key for key, viewed_at in state.seen.items() if viewed_at < window_start]
    for key in stale:
        del state.seen[key]
    return len(stale)


# --------------------------------------------------------------------------
# ListenBrainz
# --------------------------------------------------------------------------

class SubmitResult:
    SENT = "sent"
    PERMANENT_FAILURE = "permanent"
    TEMPORARY_FAILURE = "temporary"
    AUTH_FAILURE = "auth"  # token rejected -- retrying other listens is pointless


def build_listen(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Translate a Plex history entry into a ListenBrainz payload."""
    artist = str(entry.get("grandparentTitle") or "").strip()
    track = str(entry.get("title") or "").strip()
    album = str(entry.get("parentTitle") or "").strip()
    viewed_at = entry.get("viewedAt")

    if not artist or not track or not isinstance(viewed_at, int):
        return None

    additional_info: dict[str, Any] = {
        "submission_client": CLIENT_NAME,
        "submission_client_version": __version__,
        "media_player": "Plex",
    }
    rating_key = entry.get("ratingKey")
    if rating_key is not None:
        additional_info["music_service_name"] = "Plex"
        additional_info["origin_url"] = f"plex://track/{rating_key}"

    track_metadata: dict[str, Any] = {
        "artist_name": artist,
        "track_name": track,
        "additional_info": additional_info,
    }
    if album:
        track_metadata["release_name"] = album

    return {"listened_at": viewed_at, "track_metadata": track_metadata}


def submit_listens(config: Config, session: requests.Session, listens: list[dict[str, Any]]) -> str:
    """Submit one or more listens in a single request. Returns a ``SubmitResult`` value."""
    url = f"{config.listenbrainz_url}/1/submit-listens"
    body = {"listen_type": "import" if len(listens) > 1 else "single", "payload": listens}
    headers = {
        "Authorization": f"Token {config.listenbrainz_token}",
        "Content-Type": "application/json",
    }

    for attempt in range(1, LB_MAX_ATTEMPTS + 1):
        wait: float = 2 ** attempt
        try:
            response = session.post(url, json=body, headers=headers, timeout=config.request_timeout)
        except requests.RequestException as exc:
            log.warning("ListenBrainz unreachable (attempt %d/%d): %s", attempt, LB_MAX_ATTEMPTS, exc)
        else:
            if response.status_code in (200, 201):
                return SubmitResult.SENT

            if response.status_code in (401, 403):
                log.error(
                    "ListenBrainz rejected the token (HTTP %d): %s",
                    response.status_code,
                    response.text[:300],
                )
                return SubmitResult.AUTH_FAILURE

            if response.status_code == 429:
                try:
                    wait = min(max(1, int(response.headers.get("X-RateLimit-Reset-In", "5"))), LB_MAX_RATE_WAIT)
                except ValueError:
                    wait = 5
                log.info(
                    "ListenBrainz rate limit -- waiting %ds (attempt %d/%d).", wait, attempt, LB_MAX_ATTEMPTS
                )
            elif 400 <= response.status_code < 500:
                # The payload itself is wrong -- retrying will not change anything.
                log.error(
                    "ListenBrainz permanently rejected the submission (HTTP %d): %s",
                    response.status_code,
                    response.text[:300],
                )
                return SubmitResult.PERMANENT_FAILURE
            else:
                log.warning(
                    "ListenBrainz server error (HTTP %d, attempt %d/%d): %s",
                    response.status_code,
                    attempt,
                    LB_MAX_ATTEMPTS,
                    response.text[:200],
                )

        if attempt == LB_MAX_ATTEMPTS or _stop:
            break
        sleep_interruptible(wait)

    return SubmitResult.TEMPORARY_FAILURE


# --------------------------------------------------------------------------
# Cross-check against listens that already exist
# --------------------------------------------------------------------------

def listen_fingerprint(artist: str, track: str) -> tuple[str, str]:
    """Comparison key, insensitive to case and surrounding whitespace."""
    return (artist.strip().casefold(), track.strip().casefold())


def fetch_existing_listens(
    config: Config, session: requests.Session, since: int
) -> dict[tuple[str, str], list[int]] | None:
    """Existing listens in the time window as {fingerprint: [timestamps]}.

    Used to cross-check against other scrobblers -- see ``is_duplicate``.
    Returning ``None`` means "not retrievable"; in that case nothing is filtered
    so that a failed lookup never swallows a play.
    """
    headers = {"Authorization": f"Token {config.listenbrainz_token}"}
    try:
        response = session.get(
            f"{config.listenbrainz_url}/1/validate-token",
            headers=headers,
            timeout=config.request_timeout,
        )
        response.raise_for_status()
        user = response.json().get("user_name")
        if not user:
            log.warning("ListenBrainz returned no user name -- cross-check skipped.")
            return None

        index: dict[tuple[str, str], list[int]] = {}
        counted: set[tuple[tuple[str, str], int]] = set()
        min_ts = since
        for _ in range(LB_MAX_LISTEN_PAGES):
            response = session.get(
                f"{config.listenbrainz_url}/1/user/{user}/listens",
                params={"min_ts": min_ts, "count": LB_LISTENS_PAGE_SIZE},
                headers=headers,
                timeout=config.request_timeout,
            )
            response.raise_for_status()
            listens = response.json().get("payload", {}).get("listens", [])
            if not listens:
                break
            added = 0
            for listen in listens:
                metadata = listen.get("track_metadata") or {}
                listened_at = listen.get("listened_at")
                if not isinstance(listened_at, int):
                    continue
                fingerprint = listen_fingerprint(
                    str(metadata.get("artist_name") or ""), str(metadata.get("track_name") or "")
                )
                # Pages overlap by one second (see min_ts below) -- do not count
                # the same listen twice.
                marker = (fingerprint, listened_at)
                if marker in counted:
                    continue
                counted.add(marker)
                index.setdefault(fingerprint, []).append(listened_at)
                added += 1
            if len(listens) < LB_LISTENS_PAGE_SIZE:
                break
            stamps = [l["listened_at"] for l in listens if isinstance(l.get("listened_at"), int)]
            if not stamps or not added:
                # Nothing usable on the page, or no progress (a page full of
                # listens sharing one timestamp) -- do not loop forever.
                break
            # With min_ts set the endpoint anchors at the *bottom* of the range:
            # it returns the oldest listens above it, merely printed newest
            # first. Paging forward therefore means raising min_ts -- lowering
            # max_ts instead walks back into the range already covered and the
            # newest listens are never reached. min_ts is exclusive, so -1
            # re-fetches the boundary timestamp and listens sharing it across a
            # page break stay in the index.
            min_ts = max(stamps) - 1
        else:
            log.warning(
                "Stopped after %d pages of existing listens -- the cross-check window "
                "may be incomplete.",
                LB_MAX_LISTEN_PAGES,
            )
    except requests.RequestException as exc:
        log.warning("Could not fetch existing listens (%s) -- not filtering.", exc)
        return None
    except ValueError as exc:
        log.warning("ListenBrainz reply is unusable (%s) -- not filtering.", exc)
        return None

    log.debug("%d distinct tracks in the cross-check window.", len(index))
    return index


def is_duplicate(
    listen: dict[str, Any], existing: dict[tuple[str, str], list[int]], tolerance: int
) -> int | None:
    """Timestamp of a matching existing listen, otherwise ``None``.

    multi-scrobbler reports a track while it is playing, whereas Plex sets
    ``viewedAt`` when the track ends -- so the same play reaches ListenBrainz
    with timestamps one to two minutes apart, and its own de-duplication (exact
    timestamp match) does not catch it. Hence this comparison with a tolerance.

    A match is consumed from ``existing``: one existing listen may absorb only
    one Plex play, so a genuine repeat play right after it is still submitted.
    """
    metadata = listen["track_metadata"]
    fingerprint = listen_fingerprint(metadata["artist_name"], metadata["track_name"])
    candidates = existing.get(fingerprint)
    if not candidates:
        return None
    listened_at = listen["listened_at"]
    nearest = min(candidates, key=lambda stamp: abs(stamp - listened_at))
    if abs(nearest - listened_at) > tolerance:
        return None
    candidates.remove(nearest)
    return nearest


# --------------------------------------------------------------------------
# One pass
# --------------------------------------------------------------------------

def describe(entry: dict[str, Any]) -> str:
    viewed_at = entry.get("viewedAt")
    stamp = (
        datetime.fromtimestamp(viewed_at).strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(viewed_at, int)
        else "?"
    )
    album = entry.get("parentTitle")
    suffix = f" [{album}]" if album else ""
    return f"{stamp} | {entry.get('grandparentTitle', '?')} - {entry.get('title', '?')}{suffix}"


def run_once(config: Config, session: requests.Session, now: int | None = None) -> int:
    """A single pass. Returns the number of listens submitted."""
    now = int(time.time()) if now is None else now
    state = load_state(config.state_file)
    window_start = now - config.lookback_hours * 3600

    if state.is_fresh:
        log.info(
            "First run: looking back %d hours (from %s).",
            config.lookback_hours,
            datetime.fromtimestamp(window_start).strftime("%Y-%m-%d %H:%M:%S"),
        )

    try:
        entries = fetch_history(config, session, window_start)
    except PlexError as exc:
        log.error("%s -- the next run will try again.", exc)
        return 0

    candidates = select_new(entries, state, window_start, config.plex_account_id)
    log.info(
        "Plex history: %d track entries in the window, %d of them not submitted yet.",
        len(entries),
        len(candidates),
    )

    existing: dict[tuple[str, str], list[int]] | None = None
    if candidates and config.duplicate_window > 0:
        if config.listenbrainz_token:
            # Widen the range by the tolerance (and one second, min_ts is
            # exclusive): a counterpart listen recorded just before the window
            # edge must still take part in the cross-check.
            existing = fetch_existing_listens(
                config, session, window_start - config.duplicate_window - 1
            )
        else:
            log.warning(
                "No LISTENBRAINZ_TOKEN -- duplicate cross-check skipped; the dry-run "
                "preview may list plays another scrobbler already submitted."
            )

    skipped = 0
    duplicates = 0
    to_submit: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for entry in candidates:
        listen = build_listen(entry)
        if listen is None:
            log.warning("SKIP incomplete entry: %s", describe(entry))
            skipped += 1
            continue
        if listen["listened_at"] > now + 60:
            log.warning("SKIP timestamp lies in the future: %s", describe(entry))
            skipped += 1
            continue

        if existing is not None:
            match = is_duplicate(listen, existing, config.duplicate_window)
            if match is not None:
                log.info(
                    "DUP already scrobbled (%+ds): %s",
                    match - listen["listened_at"],
                    describe(entry),
                )
                # Remember it so the cross-check does not run over it every time.
                state.seen[entry_key(entry)] = entry["viewedAt"]
                duplicates += 1
                continue

        to_submit.append((entry, listen))

    if config.dry_run:
        for entry, _ in to_submit:
            log.info("DRY-RUN would submit: %s", describe(entry))
        log.info(
            "Dry run finished: %d listens would have been submitted, %d duplicates, %d skipped. "
            "State file left untouched.",
            len(to_submit),
            duplicates,
            skipped,
        )
        return len(to_submit)

    sent = 0
    failed = 0
    # Submit in batches. A rejected batch is bisected until the offending listen
    # is isolated: ListenBrainz validates the payload before storing anything,
    # so nothing of a rejected batch was persisted and re-submitting is safe.
    batches = [
        to_submit[i : i + LB_SUBMIT_BATCH_SIZE]
        for i in range(0, len(to_submit), LB_SUBMIT_BATCH_SIZE)
    ]
    while batches:
        if _stop:
            pending = sum(len(b) for b in batches)
            log.info("Stop requested -- %d listens will be retried on the next run.", pending)
            failed += pending
            break
        batch = batches.pop(0)
        result = submit_listens(config, session, [listen for _, listen in batch])
        if result == SubmitResult.SENT:
            for entry, _ in batch:
                log.info("SENT %s", describe(entry))
                state.seen[entry_key(entry)] = entry["viewedAt"]
            sent += len(batch)
            # Persist progress right away: a hard kill mid-pass must not lead to
            # already-sent listens being submitted a second time.
            try:
                save_state(config.state_file, state)
            except OSError as exc:
                log.error("State file %s is not writable: %s", config.state_file, exc)
        elif result == SubmitResult.AUTH_FAILURE:
            pending = len(batch) + sum(len(b) for b in batches)
            log.error(
                "Aborting the pass -- LISTENBRAINZ_TOKEN is not accepted; %d listens "
                "will be retried once the token is fixed.",
                pending,
            )
            failed += pending
            break
        elif result == SubmitResult.PERMANENT_FAILURE and len(batch) > 1:
            log.warning(
                "ListenBrainz rejected a batch of %d -- isolating the bad listen.", len(batch)
            )
            middle = len(batch) // 2
            batches.insert(0, batch[middle:])
            batches.insert(0, batch[:middle])
        elif result == SubmitResult.PERMANENT_FAILURE:
            entry = batch[0][0]
            log.error("DROP permanently rejected: %s", describe(entry))
            # Remember it so the entry does not fail again on every run.
            state.seen[entry_key(entry)] = entry["viewedAt"]
            failed += 1
        else:
            for entry, _ in batch:
                log.warning("RETRY on the next run: %s", describe(entry))
            failed += len(batch)

    pruned = prune_seen(state, window_start)
    state.last_run_at = now
    try:
        save_state(config.state_file, state)
    except OSError as exc:
        log.error("State file %s is not writable: %s", config.state_file, exc)
    else:
        if pruned:
            log.debug("Removed %d state entries outside the window.", pruned)

    log.info(
        "Run finished: %d submitted, %d duplicates, %d skipped, %d pending (retried next run).",
        sent,
        duplicates,
        skipped,
        failed,
    )
    return sent


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

_stop = False


def _handle_signal(signum: int, _frame: Any) -> None:
    global _stop
    _stop = True
    log.info("Received signal %s -- stopping after the current pass.", signal.Signals(signum).name)


def sleep_interruptible(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while not _stop and time.monotonic() < deadline:
        # max(0.0, ...): the deadline may pass between the loop condition and
        # this call, and time.sleep raises on negative values.
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))


def configure_logging() -> None:
    raw = os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    level = getattr(logging, raw, None)
    logging.basicConfig(
        level=level if isinstance(level, int) else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    if not isinstance(level, int):
        log.warning("Unknown LOG_LEVEL %r -- using INFO.", raw)


def main() -> int:
    configure_logging()
    try:
        config = Config.from_env()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Fail fast when the state file cannot be written (typical cause: the /data
    # bind mount is owned by root while the container runs as UID 1000) --
    # running stateless would re-process and re-submit the window every pass.
    if not config.dry_run:
        try:
            save_state(config.state_file, load_state(config.state_file))
        except OSError as exc:
            log.error(
                "State file %s is not writable: %s -- fix the /data volume "
                "permissions (see README) and restart.",
                config.state_file,
                exc,
            )
            return 2

    log.info(
        "%s %s started -- Plex %s, window %dh, state %s%s%s",
        CLIENT_NAME,
        __version__,
        config.plex_url,
        config.lookback_hours,
        config.state_file,
        f", accountID {config.plex_account_id}" if config.plex_account_id else "",
        ", DRY-RUN" if config.dry_run else "",
    )
    if config.duplicate_window:
        log.info(
            "Cross-check against existing listens is active (tolerance %ds). "
            "Set DUPLICATE_WINDOW_SECONDS=0 to disable it.",
            config.duplicate_window,
        )

    session = requests.Session()
    session.headers.update({"User-Agent": f"{CLIENT_NAME}/{__version__}"})

    while True:
        try:
            run_once(config, session)
        except Exception:  # a broken pass must not kill the service
            log.exception("Unexpected error during the pass -- continuing at the next interval.")

        if config.run_once or _stop:
            break

        log.info("Next run in %d minutes.", config.interval_minutes)
        sleep_interruptible(config.interval_minutes * 60)
        if _stop:
            break

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
