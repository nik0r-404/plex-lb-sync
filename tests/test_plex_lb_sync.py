"""Tests for the selection and mapping logic. No network access."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import plex_lb_sync as sync  # noqa: E402


def entry(**overrides):
    base = {
        "historyKey": "/status/sessions/history/1",
        "ratingKey": "12345",
        "title": "Dreams",
        "grandparentTitle": "Fleetwood Mac",
        "parentTitle": "Rumours",
        "viewedAt": 1_700_000_000,
        "accountID": 1,
        "type": "track",
    }
    base.update(overrides)
    return base


def config(**overrides):
    values = dict(
        plex_url="http://plex:32400",
        plex_token="t",
        plex_account_id="",
        plex_library_section="",
        listenbrainz_token="l",
        listenbrainz_url="https://api.listenbrainz.org",
        lookback_hours=72,
        duplicate_window=600,
        state_file="/tmp/state.json",
        dry_run=True,
        interval_minutes=15,
        run_once=True,
        request_timeout=30,
    )
    values.update(overrides)
    return sync.Config(**values)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeSession:
    """Minimal stand-in for requests.Session -- records every call."""

    def __init__(self, sections, history_pages):
        self.sections = sections
        self.history_pages = list(history_pages)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if url.endswith("/library/sections"):
            return FakeResponse({"MediaContainer": {"Directory": self.sections}})
        page = self.history_pages.pop(0) if self.history_pages else []
        return FakeResponse({"MediaContainer": {"Metadata": page}})


class SelectNewTest(unittest.TestCase):
    def test_returns_unseen_entries_oldest_first(self):
        entries = [
            entry(historyKey="c", viewedAt=300),
            entry(historyKey="a", viewedAt=100),
            entry(historyKey="b", viewedAt=200),
        ]
        result = sync.select_new(entries, sync.State(), window_start=0, account_id="")
        self.assertEqual([e["historyKey"] for e in result], ["a", "b", "c"])

    def test_skips_already_submitted(self):
        state = sync.State(seen={"a": 100})
        entries = [entry(historyKey="a", viewedAt=100), entry(historyKey="b", viewedAt=200)]
        result = sync.select_new(entries, state, window_start=0, account_id="")
        self.assertEqual([e["historyKey"] for e in result], ["b"])

    def test_backdated_offline_entry_is_still_picked_up(self):
        """Offline plays reported later can be older than the high-water mark."""
        state = sync.State(last_submitted_at=500, seen={"recent": 500})
        entries = [entry(historyKey="offline", viewedAt=200), entry(historyKey="recent", viewedAt=500)]
        result = sync.select_new(entries, state, window_start=0, account_id="")
        self.assertEqual([e["historyKey"] for e in result], ["offline"])

    def test_respects_window_start(self):
        entries = [entry(historyKey="old", viewedAt=50), entry(historyKey="new", viewedAt=150)]
        result = sync.select_new(entries, sync.State(), window_start=100, account_id="")
        self.assertEqual([e["historyKey"] for e in result], ["new"])

    def test_filters_by_account(self):
        entries = [entry(historyKey="mine", accountID=1), entry(historyKey="theirs", accountID=2)]
        result = sync.select_new(entries, sync.State(), window_start=0, account_id="1")
        self.assertEqual([e["historyKey"] for e in result], ["mine"])

    def test_ignores_entries_without_timestamp(self):
        entries = [entry(historyKey="broken", viewedAt=None)]
        self.assertEqual(sync.select_new(entries, sync.State(), 0, ""), [])


class EntryKeyTest(unittest.TestCase):
    def test_prefers_history_key(self):
        self.assertEqual(sync.entry_key(entry(historyKey="/x/1")), "/x/1")

    def test_falls_back_to_rating_key_and_timestamp(self):
        item = entry(viewedAt=42)
        del item["historyKey"]
        self.assertEqual(sync.entry_key(item), "12345:42")


class BuildListenTest(unittest.TestCase):
    def test_maps_plex_fields_to_listenbrainz(self):
        listen = sync.build_listen(entry())
        self.assertEqual(listen["listened_at"], 1_700_000_000)
        meta = listen["track_metadata"]
        self.assertEqual(meta["artist_name"], "Fleetwood Mac")
        self.assertEqual(meta["track_name"], "Dreams")
        self.assertEqual(meta["release_name"], "Rumours")
        self.assertEqual(meta["additional_info"]["submission_client"], "plex-lb-sync")

    def test_album_is_optional(self):
        listen = sync.build_listen(entry(parentTitle=""))
        self.assertNotIn("release_name", listen["track_metadata"])

    def test_rejects_entries_without_artist_or_track(self):
        self.assertIsNone(sync.build_listen(entry(grandparentTitle="")))
        self.assertIsNone(sync.build_listen(entry(title=" ")))
        self.assertIsNone(sync.build_listen(entry(viewedAt="yesterday")))


class PruneSeenTest(unittest.TestCase):
    def test_drops_entries_outside_window(self):
        state = sync.State(seen={"old": 10, "new": 100})
        removed = sync.prune_seen(state, window_start=50)
        self.assertEqual(removed, 1)
        self.assertEqual(state.seen, {"new": 100})


class StateRoundTripTest(unittest.TestCase):
    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sub", "state.json")
            sync.save_state(path, sync.State(last_submitted_at=5, last_run_at=7, seen={"a": 5}))
            loaded = sync.load_state(path)
            self.assertEqual(loaded.last_submitted_at, 5)
            self.assertEqual(loaded.last_run_at, 7)
            self.assertEqual(loaded.seen, {"a": 5})
            self.assertFalse(loaded.is_fresh)

    def test_missing_file_is_a_fresh_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(sync.load_state(os.path.join(tmp, "nope.json")).is_fresh)

    def test_corrupt_file_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{broken")
            self.assertTrue(sync.load_state(path).is_fresh)

    def test_atomic_write_leaves_no_temp_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            sync.save_state(path, sync.State(seen={"a": 1}))
            self.assertEqual(os.listdir(tmp), ["state.json"])
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["version"], sync.STATE_VERSION)


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self._backup = dict(os.environ)
        for key in list(os.environ):
            if key.startswith(("PLEX_", "LISTENBRAINZ_", "LOOKBACK_", "STATE_", "DRY_", "INTERVAL_", "RUN_", "DUPLICATE_")):
                del os.environ[key]

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._backup)

    def test_requires_plex_settings(self):
        with self.assertRaises(sync.ConfigError):
            sync.Config.from_env()

    def test_dry_run_needs_no_listenbrainz_token(self):
        os.environ.update({"PLEX_URL": "http://plex:32400/", "PLEX_TOKEN": "t", "DRY_RUN": "true"})
        config_from_env = sync.Config.from_env()
        self.assertEqual(config_from_env.plex_url, "http://plex:32400")
        self.assertTrue(config_from_env.dry_run)
        self.assertEqual(config_from_env.lookback_hours, 72)
        self.assertEqual(config_from_env.duplicate_window, 600)
        self.assertEqual(config_from_env.interval_minutes, 15)

    def test_rejects_non_numeric_lookback(self):
        os.environ.update({"PLEX_URL": "http://p", "PLEX_TOKEN": "t", "LISTENBRAINZ_TOKEN": "l", "LOOKBACK_HOURS": "lots"})
        with self.assertRaises(sync.ConfigError):
            sync.Config.from_env()


class ResolveLibrarySectionTest(unittest.TestCase):
    def setUp(self):
        sync._section_cache.clear()

    def test_finds_music_library_by_type(self):
        session = FakeSession(
            [{"key": 1, "type": "movie", "title": "Movies"}, {"key": 3, "type": "artist", "title": "Music"}], []
        )
        self.assertEqual(sync.resolve_library_section(config(), session), "3")

    def test_configured_value_wins_and_skips_the_request(self):
        session = FakeSession([], [])
        self.assertEqual(sync.resolve_library_section(config(plex_library_section="9"), session), "9")
        self.assertEqual(session.calls, [])

    def test_result_is_cached(self):
        session = FakeSession([{"key": 3, "type": "artist", "title": "Music"}], [])
        sync.resolve_library_section(config(), session)
        sync.resolve_library_section(config(), session)
        self.assertEqual(len(session.calls), 1)

    def test_raises_without_music_library(self):
        session = FakeSession([{"key": 1, "type": "movie", "title": "Movies"}], [])
        with self.assertRaises(sync.PlexError):
            sync.resolve_library_section(config(), session)


class FetchHistoryTest(unittest.TestCase):
    """The filter type=10 silently returned zero results on this endpoint."""

    def setUp(self):
        sync._section_cache.clear()
        self.sections = [{"key": 3, "type": "artist", "title": "Music"}]

    def test_filters_by_library_section_not_by_type(self):
        session = FakeSession(self.sections, [[entry()]])
        sync.fetch_history(config(), session, since=1_600_000_000)
        history_params = session.calls[-1][1]
        self.assertEqual(history_params["librarySectionID"], "3")
        self.assertNotIn("type", history_params)
        self.assertEqual(history_params["viewedAt>="], 1_600_000_000)

    def test_drops_non_track_entries(self):
        session = FakeSession(self.sections, [[entry(), entry(historyKey="/h/2", type="episode")]])
        result = sync.fetch_history(config(), session, since=1_600_000_000)
        self.assertEqual([e["historyKey"] for e in result], ["/status/sessions/history/1"])

    def test_drops_entries_before_the_window(self):
        old = entry(historyKey="/h/old", viewedAt=1_500_000_000)
        session = FakeSession(self.sections, [[entry(), old]])
        result = sync.fetch_history(config(), session, since=1_600_000_000)
        self.assertEqual([e["historyKey"] for e in result], ["/status/sessions/history/1"])

    def test_ignores_repeated_entries_across_pages(self):
        # Plex does not honour X-Plex-Container-Start in every combination.
        page = [entry(historyKey=f"/h/{i}") for i in range(sync.PLEX_PAGE_SIZE)]
        session = FakeSession(self.sections, [page, page, []])
        result = sync.fetch_history(config(), session, since=1_600_000_000)
        self.assertEqual(len(result), sync.PLEX_PAGE_SIZE)


class IsDuplicateTest(unittest.TestCase):
    """multi-scrobbler reports during playback, Plex when the track ends."""

    def listen(self, artist="Fleetwood Mac", track="Dreams", at=1_700_000_000):
        return {"listened_at": at, "track_metadata": {"artist_name": artist, "track_name": track}}

    def test_matches_within_tolerance(self):
        existing = {("fleetwood mac", "dreams"): [1_700_000_120]}
        self.assertEqual(sync.is_duplicate(self.listen(), existing, 600), 1_700_000_120)

    def test_ignores_match_outside_tolerance(self):
        existing = {("fleetwood mac", "dreams"): [1_700_000_700]}
        self.assertIsNone(sync.is_duplicate(self.listen(), existing, 600))

    def test_ignores_other_tracks(self):
        existing = {("fleetwood mac", "rhiannon"): [1_700_000_010]}
        self.assertIsNone(sync.is_duplicate(self.listen(), existing, 600))

    def test_comparison_ignores_case_and_spacing(self):
        existing = {("fleetwood mac", "dreams"): [1_700_000_060]}
        self.assertIsNotNone(sync.is_duplicate(self.listen(artist="  FLEETWOOD Mac "), existing, 600))

    def test_picks_the_nearest_of_several(self):
        existing = {("fleetwood mac", "dreams"): [1_699_990_000, 1_700_000_030, 1_700_009_000]}
        self.assertEqual(sync.is_duplicate(self.listen(), existing, 600), 1_700_000_030)

    def test_repeated_play_outside_tolerance_is_kept(self):
        # The same track an hour later is a genuine second play.
        existing = {("fleetwood mac", "dreams"): [1_700_000_000]}
        self.assertIsNone(sync.is_duplicate(self.listen(at=1_700_003_600), existing, 600))


class FetchExistingListensTest(unittest.TestCase):
    class Session(FakeSession):
        def __init__(self, user, pages):
            super().__init__([], [])
            self.user = user
            self.pages = list(pages)

        def get(self, url, params=None, headers=None, timeout=None):
            self.calls.append((url, dict(params or {})))
            if url.endswith("/validate-token"):
                return FakeResponse({"user_name": self.user})
            page = self.pages.pop(0) if self.pages else []
            return FakeResponse({"payload": {"listens": page}})

    def test_builds_index_from_listens(self):
        session = self.Session("testuser", [[
            {"listened_at": 100, "track_metadata": {"artist_name": "A", "track_name": "X"}},
            {"listened_at": 200, "track_metadata": {"artist_name": "A", "track_name": "X"}},
            {"listened_at": 300, "track_metadata": {"artist_name": "B", "track_name": "Y"}},
        ]])
        index = sync.fetch_existing_listens(config(), session, since=0)
        self.assertEqual(index, {("a", "x"): [100, 200], ("b", "y"): [300]})

    def test_returns_none_when_unreachable(self):
        class Broken(FakeSession):
            def get(self, *a, **k):
                raise sync.requests.RequestException("gone")

        # None means "do not filter" -- a failed lookup must not swallow anything.
        self.assertIsNone(sync.fetch_existing_listens(config(), Broken([], []), since=0))


if __name__ == "__main__":
    unittest.main()
