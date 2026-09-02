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
        """Offline plays reported later can be older than the newest submission."""
        state = sync.State(seen={"recent": 500})
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
            sync.save_state(path, sync.State(last_run_at=7, seen={"a": 5}))
            loaded = sync.load_state(path)
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
        # Plex does not honour X-Plex-Container-Start in every combination; a
        # page without any new entries must also end the pagination.
        page = [entry(historyKey=f"/h/{i}") for i in range(sync.PLEX_PAGE_SIZE)]
        session = FakeSession(self.sections, [page, page, page])
        result = sync.fetch_history(config(), session, since=1_600_000_000)
        self.assertEqual(len(result), sync.PLEX_PAGE_SIZE)
        # 1x /library/sections + 2x history: the repeated page stops the loop.
        self.assertEqual(len(session.calls), 3)

    def test_single_outlier_does_not_abort_pagination(self):
        page1 = [
            entry(historyKey=f"/h/a{i}", viewedAt=1_700_000_000 - i)
            for i in range(sync.PLEX_PAGE_SIZE - 1)
        ]
        page1.append(entry(historyKey="/h/outlier", viewedAt=1_500_000_000))
        page2 = [
            entry(historyKey=f"/h/b{i}", viewedAt=1_690_000_000 - i)
            for i in range(sync.PLEX_PAGE_SIZE)
        ]
        old_page = [
            entry(historyKey=f"/h/c{i}", viewedAt=1_400_000_000 - i)
            for i in range(sync.PLEX_PAGE_SIZE)
        ]
        session = FakeSession(self.sections, [page1, page2, old_page])
        result = sync.fetch_history(config(), session, since=1_600_000_000)
        self.assertEqual(len(result), 2 * sync.PLEX_PAGE_SIZE - 1)


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

    def test_match_is_consumed(self):
        # One existing listen may absorb only one Plex play: a genuine repeat
        # play right after it must still be submitted.
        existing = {("fleetwood mac", "dreams"): [1_700_000_030]}
        self.assertEqual(sync.is_duplicate(self.listen(), existing, 600), 1_700_000_030)
        self.assertIsNone(sync.is_duplicate(self.listen(at=1_700_000_180), existing, 600))


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

    class ApiSession(FakeSession):
        """Serves listens the way the real endpoint does.

        With ``min_ts`` set, /1/user/<name>/listens anchors at the *bottom* of
        the range: it returns the oldest listens above it, merely printed newest
        first. Paging therefore has to raise min_ts, not lower max_ts.
        """

        def __init__(self, user, listens):
            super().__init__([], [])
            self.user = user
            self.listens = sorted(listens, key=lambda item: item["listened_at"])

        def get(self, url, params=None, headers=None, timeout=None):
            self.calls.append((url, dict(params or {})))
            if url.endswith("/validate-token"):
                return FakeResponse({"user_name": self.user})
            above = [l for l in self.listens if l["listened_at"] > params["min_ts"]]
            page = above[: params["count"]]
            return FakeResponse({"payload": {"listens": list(reversed(page))}})

    def test_pagination_reaches_the_newest_listens(self):
        # Regression: paging by lowering max_ts walked back into the range
        # already covered, so the index stopped after the first page and every
        # listen beyond it stayed invisible to the cross-check.
        total = sync.LB_LISTENS_PAGE_SIZE * 2 + 20
        session = self.ApiSession("testuser", [
            {"listened_at": 1000 + i, "track_metadata": {"artist_name": "A", "track_name": f"T{i}"}}
            for i in range(total)
        ])
        index = sync.fetch_existing_listens(config(), session, since=0)
        self.assertEqual(sum(len(stamps) for stamps in index.values()), total)
        self.assertEqual(index[("a", f"t{total - 1}")], [1000 + total - 1])
        self.assertNotIn("max_ts", session.calls[-1][1])

    def test_pagination_keeps_boundary_timestamps(self):
        # min_ts is exclusive: the boundary timestamp is re-fetched so two
        # listens sharing it across a page break both land in the index.
        boundary = 1000 + sync.LB_LISTENS_PAGE_SIZE - 1
        listens = [
            {"listened_at": 1000 + i, "track_metadata": {"artist_name": "A", "track_name": f"T{i}"}}
            for i in range(sync.LB_LISTENS_PAGE_SIZE - 1)
        ]
        listens += [
            {"listened_at": boundary, "track_metadata": {"artist_name": "B", "track_name": "Y"}},
            {"listened_at": boundary, "track_metadata": {"artist_name": "C", "track_name": "Z"}},
            {"listened_at": 9999, "track_metadata": {"artist_name": "D", "track_name": "Last"}},
        ]
        session = self.ApiSession("testuser", listens)
        index = sync.fetch_existing_listens(config(), session, since=0)
        self.assertEqual(index[("b", "y")], [boundary])
        self.assertEqual(index[("c", "z")], [boundary])
        self.assertEqual(index[("d", "last")], [9999])

    def test_pagination_stops_at_the_page_cap(self):
        # A page full of listens sharing one timestamp makes no progress; the
        # loop must not spin forever.
        session = self.ApiSession("testuser", [
            {"listened_at": 1000, "track_metadata": {"artist_name": "A", "track_name": f"T{i}"}}
            for i in range(sync.LB_LISTENS_PAGE_SIZE * 3)
        ])
        sync.fetch_existing_listens(config(), session, since=0)
        self.assertLessEqual(len(session.calls), sync.LB_MAX_LISTEN_PAGES + 1)


class FakePostResponse:
    def __init__(self, status_code, headers=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text


class PostSession:
    """Stand-in for requests.Session covering only POST."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json))
        return self.responses.pop(0)


class SubmitListensTest(unittest.TestCase):
    def listen(self, at=1_700_000_000):
        return {"listened_at": at, "track_metadata": {"artist_name": "A", "track_name": "X"}}

    def test_single_listen_uses_listen_type_single(self):
        session = PostSession([FakePostResponse(200)])
        result = sync.submit_listens(config(), session, [self.listen()])
        self.assertEqual(result, sync.SubmitResult.SENT)
        self.assertEqual(session.calls[0][1]["listen_type"], "single")

    def test_batch_uses_listen_type_import(self):
        session = PostSession([FakePostResponse(200)])
        result = sync.submit_listens(config(), session, [self.listen(1), self.listen(2)])
        self.assertEqual(result, sync.SubmitResult.SENT)
        self.assertEqual(session.calls[0][1]["listen_type"], "import")

    def test_rejected_token_stops_immediately(self):
        session = PostSession([FakePostResponse(401)])
        result = sync.submit_listens(config(), session, [self.listen()])
        self.assertEqual(result, sync.SubmitResult.AUTH_FAILURE)
        self.assertEqual(len(session.calls), 1)

    def test_client_error_is_permanent(self):
        session = PostSession([FakePostResponse(400)])
        result = sync.submit_listens(config(), session, [self.listen()])
        self.assertEqual(result, sync.SubmitResult.PERMANENT_FAILURE)
        self.assertEqual(len(session.calls), 1)


class RunOnceTest(unittest.TestCase):
    class Session(FakeSession):
        def __init__(self, sections, history_pages, post_responses):
            super().__init__(sections, history_pages)
            self.post_responses = list(post_responses)
            self.posts = []

        def post(self, url, json=None, headers=None, timeout=None):
            self.posts.append((url, json))
            return self.post_responses.pop(0)

    def setUp(self):
        sync._section_cache.clear()
        self.sections = [{"key": 3, "type": "artist", "title": "Music"}]
        self.now = 1_700_003_600

    def history(self):
        return [
            entry(historyKey="/h/1", viewedAt=1_700_000_000),
            entry(historyKey="/h/2", viewedAt=1_700_000_100),
        ]

    def test_submits_batch_and_persists_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            session = self.Session(self.sections, [self.history()], [FakePostResponse(200)])
            cfg = config(dry_run=False, duplicate_window=0, state_file=path)
            sent = sync.run_once(cfg, session, now=self.now)
            self.assertEqual(sent, 2)
            self.assertEqual(session.posts[0][1]["listen_type"], "import")
            self.assertEqual(sorted(sync.load_state(path).seen), ["/h/1", "/h/2"])

    def test_bad_listen_is_isolated_and_dropped(self):
        # The rejected batch is bisected; the bad listen is dropped, the good
        # one is still submitted.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            session = self.Session(
                self.sections,
                [self.history()],
                [FakePostResponse(400), FakePostResponse(200), FakePostResponse(400)],
            )
            cfg = config(dry_run=False, duplicate_window=0, state_file=path)
            sent = sync.run_once(cfg, session, now=self.now)
            self.assertEqual(sent, 1)
            self.assertEqual(len(session.posts), 3)
            # Both are remembered: one as sent, one as permanently rejected.
            self.assertEqual(sorted(sync.load_state(path).seen), ["/h/1", "/h/2"])

    def test_rejected_token_aborts_the_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            session = self.Session(self.sections, [self.history()], [FakePostResponse(401)])
            cfg = config(dry_run=False, duplicate_window=0, state_file=path)
            sent = sync.run_once(cfg, session, now=self.now)
            self.assertEqual(sent, 0)
            self.assertEqual(len(session.posts), 1)
            # Nothing is marked seen -- everything is retried after the fix.
            self.assertEqual(sync.load_state(path).seen, {})


if __name__ == "__main__":
    unittest.main()
