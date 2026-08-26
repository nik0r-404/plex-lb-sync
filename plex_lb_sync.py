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

__version__ = "1.0.0"

CLIENT_NAME = "plex-lb-sync"
PLEX_TRACK_TYPE = "track"  # value of the "type" field in a history entry
PLEX_PAGE_SIZE = 100
PLEX_MAX_PAGES = 50  # safety net against never-ending pagination
LB_MAX_ATTEMPTS = 3
LB_LISTENS_PAGE_SIZE = 100  # maximum of the /1/user/<name>/listens endpoint
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
        )


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

@dataclass
class State:
    """What has already been submitted successfully.

    ``seen`` maps Plex history entries to their ``viewedAt`` timestamp and is
    only extended after a successful submission. ``last_submitted_at`` is a
    high-water mark kept for logging; the selection of entries to submit
    deliberately goes through ``seen`` instead: because Plex records the real
    listening time when a client reports offline plays, those entries appear
    *behind* the high-water mark and a plain timestamp comparison would swallow
    them.
    """

    last_submitted_at: int = 0
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
        last_submitted_at=as_int(raw.get("last_submitted_at")),
        last_run_at=as_int(raw.get("last_run_at")),
        seen=seen,
    )


def save_state(path: str, state: State) -> None:
    payload = {
        "version": STATE_VERSION,
        "last_submitted_at": state.last_submitted_at,
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
        outside_window = 0
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
            # Plex does not honour X-Plex-Container-Start in every combination;
            # without this guard the same entry would be collected repeatedly.
            key = entry_key(entry)
            if key in known:
                continue
            known.add(key)
            collected.append(entry)

        # The sort order is descending: once a page contains entries outside the
        # window, nothing relevant follows.
        if outside_window or len(page) < PLEX_PAGE_SIZE:
            break
        start += PLEX_PAGE_SIZE
        if start >= PLEX_PAGE_SIZE * PLEX_MAX_PAGES:
            log.warning(
                "Stopping after %d pages of Plex history -- LOOKBACK_HOURS may be too large.",
                PLEX_MAX_PAGES,
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
    # Submit in ascending order so the high-water mark grows monotonically and an
    # abort halfway through a run does not skip anything.
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


def submit_listen(config: Config, session: requests.Session, listen: dict[str, Any]) -> str:
    """Submit a single listen. Returns a ``SubmitResult`` value."""
    url = f"{config.listenbrainz_url}/1/submit-listens"
    body = {"listen_type": "single", "payload": [listen]}
    headers = {
        "Authorization": f"Token {config.listenbrainz_token}",
        "Content-Type": "application/json",
    }

    for attempt in range(1, LB_MAX_ATTEMPTS + 1):
        try:
            response = session.post(url, json=body, headers=headers, timeout=config.request_timeout)
        except requests.RequestException as exc:
            log.warning("ListenBrainz unreachable (attempt %d/%d): %s", attempt, LB_MAX_ATTEMPTS, exc)
            if attempt == LB_MAX_ATTEMPTS:
                return SubmitResult.TEMPORARY_FAILURE
            time.sleep(2 ** attempt)
            continue

        if response.status_code in (200, 201):
            return SubmitResult.SENT

        if response.status_code == 429:
            wait = 5
            try:
                wait = max(1, int(response.headers.get("X-RateLimit-Reset-In", "5")))
            except ValueError:
                pass
            log.info("ListenBrainz rate limit -- waiting %ds (attempt %d/%d).", wait, attempt, LB_MAX_ATTEMPTS)
            if attempt == LB_MAX_ATTEMPTS:
                return SubmitResult.TEMPORARY_FAILURE
            time.sleep(wait)
            continue

        if response.status_code in (401, 403):
            log.error("ListenBrainz rejected the token (HTTP %d): %s", response.status_code, response.text[:300])
            return SubmitResult.TEMPORARY_FAILURE

        if 400 <= response.status_code < 500:
            # The payload itself is wrong -- retrying will not change anything.
            log.error(
                "ListenBrainz permanently rejected the listen (HTTP %d): %s",
                response.status_code,
                response.text[:300],
            )
            return SubmitResult.PERMANENT_FAILURE

        log.warning(
            "ListenBrainz server error (HTTP %d, attempt %d/%d): %s",
            response.status_code,
            attempt,
            LB_MAX_ATTEMPTS,
            response.text[:200],
        )
        if attempt == LB_MAX_ATTEMPTS:
            return SubmitResult.TEMPORARY_FAILURE
        time.sleep(2 ** attempt)

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
        max_ts: int | None = None
        while True:
            params: dict[str, Any] = {"min_ts": since, "count": LB_LISTENS_PAGE_SIZE}
            if max_ts is not None:
                params["max_ts"] = max_ts
            response = session.get(
                f"{config.listenbrainz_url}/1/user/{user}/listens",
                params=params,
                headers=headers,
                timeout=config.request_timeout,
            )
            response.raise_for_status()
            listens = response.json().get("payload", {}).get("listens", [])
            if not listens:
                break
            for listen in listens:
                metadata = listen.get("track_metadata") or {}
                listened_at = listen.get("listened_at")
                if not isinstance(listened_at, int):
                    continue
                fingerprint = listen_fingerprint(
                    str(metadata.get("artist_name") or ""), str(metadata.get("track_name") or "")
                )
                index.setdefault(fingerprint, []).append(listened_at)
            if len(listens) < LB_LISTENS_PAGE_SIZE:
                break
            max_ts = min(l["listened_at"] for l in listens if isinstance(l.get("listened_at"), int))
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
    """
    metadata = listen["track_metadata"]
    fingerprint = listen_fingerprint(metadata["artist_name"], metadata["track_name"])
    candidates = existing.get(fingerprint)
    if not candidates:
        return None
    listened_at = listen["listened_at"]
    nearest = min(candidates, key=lambda stamp: abs(stamp - listened_at))
    return nearest if abs(nearest - listened_at) <= tolerance else None


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
        existing = fetch_existing_listens(config, session, window_start)

    sent = 0
    skipped = 0
    duplicates = 0
    failed = 0

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

        if config.dry_run:
            log.info("DRY-RUN would submit: %s", describe(entry))
            sent += 1
            continue

        result = submit_listen(config, session, listen)
        if result == SubmitResult.SENT:
            log.info("SENT %s", describe(entry))
            state.seen[entry_key(entry)] = entry["viewedAt"]
            state.last_submitted_at = max(state.last_submitted_at, entry["viewedAt"])
            sent += 1
        elif result == SubmitResult.PERMANENT_FAILURE:
            log.error("DROP permanently rejected: %s", describe(entry))
            # Remember it so the entry does not fail again on every run.
            state.seen[entry_key(entry)] = entry["viewedAt"]
            failed += 1
        else:
            log.warning("RETRY on the next run: %s", describe(entry))
            failed += 1

    if config.dry_run:
        log.info(
            "Dry run finished: %d listens would have been submitted, %d duplicates, %d skipped. "
            "State file left untouched.",
            sent,
            duplicates,
            skipped,
        )
        return sent

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


def sleep_interruptible(seconds: int) -> None:
    deadline = time.monotonic() + seconds
    while not _stop and time.monotonic() < deadline:
        time.sleep(min(1.0, deadline - time.monotonic()))


def configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def main() -> int:
    configure_logging()
    try:
        config = Config.from_env()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

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
