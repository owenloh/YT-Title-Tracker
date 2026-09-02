"""
Unit tests for the pure logic that doesn't touch the network or the database:
  - InnerTube JSON parsing + cookieless variant sampler (youtube_innertube)
  - comment rendering (main.render_comment)
  - channel-id vs @handle detection (scraper._looks_like_channel_id)

Run:  python test_logic.py

These tests stub psycopg2/google/dotenv ONLY when they're not installed (e.g. this
sandbox), so the suite runs both here and in the full Railway environment.
"""
import sys
import types
import unittest
from datetime import date


# --------------------------------------------------------------------------- #
# Make main.py / scraper.py importable without the heavy runtime deps installed.
# Each stub is registered only if the real module is missing, so it is a no-op
# wherever the real dependencies exist.
# --------------------------------------------------------------------------- #
def _stub(name: str, **attrs):
    if name in sys.modules:
        return
    try:
        __import__(name)
        return  # real module exists -> never stub it
    except ImportError:
        pass
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod


_HttpError = type("HttpError", (Exception,), {})
_placeholder = type("_Placeholder", (), {})

_stub("dotenv", load_dotenv=lambda *a, **k: None)
_stub("psycopg2")
_stub("psycopg2.extras", RealDictCursor=_placeholder)
_stub("psycopg2.pool", SimpleConnectionPool=_placeholder)
_stub("google")
_stub("google.oauth2")
_stub("google.oauth2.credentials", Credentials=_placeholder)
_stub("google.auth")
_stub("google.auth.transport")
_stub("google.auth.transport.requests", Request=_placeholder)
_stub("googleapiclient")
_stub("googleapiclient.discovery", build=lambda *a, **k: None)
_stub("googleapiclient.errors", HttpError=_HttpError)

import youtube_innertube as it  # noqa: E402


def _next_json(*titles):
    """Build a minimal /next response carrying the given watch-page title runs."""
    return {
        "contents": {
            "twoColumnWatchNextResults": {
                "results": {"results": {"contents": [
                    {"videoPrimaryInfoRenderer": {"title": {"runs": [{"text": t}]}}}
                    for t in titles
                ]}}
            }
        }
    }


def _player_json(title):
    return {"videoDetails": {"title": title}}


class TestNormalize(unittest.TestCase):
    def test_unescape_and_collapse(self):
        self.assertEqual(it.normalize_title("Hello &amp;  World\n"), "Hello & World")
        self.assertEqual(it.normalize_title("  a   b  "), "a b")
        self.assertEqual(it.normalize_title(""), "")
        self.assertEqual(it.normalize_title(None), "")


class TestExtract(unittest.TestCase):
    def test_next_runs_joined(self):
        data = _next_json("Part One ", "Part Two")  # two renderers, one each
        self.assertEqual(it.extract_titles_from_next(data), {"Part One", "Part Two"})

    def test_next_multi_run_title(self):
        data = {"x": {"videoPrimaryInfoRenderer": {"title": {"runs": [
            {"text": "Big "}, {"text": "Title"}]}}}}
        self.assertEqual(it.extract_titles_from_next(data), {"Big Title"})

    def test_player_videodetails_and_microformat(self):
        data = {
            "videoDetails": {"title": "Canonical &quot;Quoted&quot;"},
            "microformat": {"playerMicroformatRenderer": {"title": {"simpleText": "Micro Title"}}},
        }
        self.assertEqual(
            it.extract_titles_from_player(data),
            {'Canonical "Quoted"', "Micro Title"},
        )

    def test_empty(self):
        self.assertEqual(it.extract_titles_from_next({}), set())
        self.assertEqual(it.extract_titles_from_player({}), set())


class TestSampler(unittest.TestCase):
    def test_collects_multiple_variants_across_samples(self):
        """Sampling repeatedly accumulates every variant, and rotates client surface."""
        variants = ["Variant A", "Variant B", "Variant C"]
        calls = {"n": 0, "clients": set()}

        def fake_post(endpoint, video_id, client_key, timeout=15.0):
            calls["n"] += 1
            calls["clients"].add(client_key)
            return _next_json(variants[calls["n"] % len(variants)])

        orig = it._post
        it._post = fake_post
        try:
            observed = it.sample_variant_titles("vid", samples=9, delay=0, jitter=0)
        finally:
            it._post = orig

        self.assertEqual(set(observed), set(variants))  # all variants captured
        self.assertGreater(len(calls["clients"]), 1)    # client surface rotates

    def test_falls_back_to_player_when_next_empty(self):
        def fake_post(endpoint, video_id, client_key, timeout=15.0):
            if endpoint == "next":
                return {}  # no watch-page title available
            return _player_json("Player Title")

        orig = it._post
        it._post = fake_post
        try:
            observed = it.sample_variant_titles("vid", samples=2, delay=0, jitter=0)
        finally:
            it._post = orig
        self.assertEqual(set(observed), {"Player Title"})

    def test_parallel_path(self):
        def fake_post(endpoint, video_id, client_key, timeout=15.0):
            return _next_json("Only Title")

        orig = it._post
        it._post = fake_post
        try:
            observed = it.sample_variant_titles("vid", samples=5, parallel=True)
        finally:
            it._post = orig
        self.assertEqual(set(observed), {"Only Title"})
        self.assertEqual(len(observed), 5)

    def test_no_network_returns_empty(self):
        orig = it._post
        it._post = lambda *a, **k: None  # simulate total failure / blocked host
        try:
            self.assertEqual(it.sample_variant_titles("vid", samples=3, delay=0, jitter=0), [])
        finally:
            it._post = orig


class TestRenderComment(unittest.TestCase):
    def setUp(self):
        import main
        self.render = main.render_comment

    def test_uses_recent_window_for_percentages(self):
        # Lifetime is 50/50 but the recent window is 90/10 -> show the CURRENT split.
        out = self.render("INTRO",
                          [("A", 90), ("B", 10)],            # recent window
                          [("A", 500), ("B", 500)],          # all-time (50/50)
                          "vid1")
        self.assertTrue(out.startswith("INTRO"))
        self.assertIn("90%", out)
        self.assertIn("10%", out)

    def test_no_robotic_footer(self):
        out = self.render("INTRO", [("A", 90), ("B", 10)], [("A", 90), ("B", 10)], "vid1")
        self.assertNotIn("automated", out)
        self.assertNotIn("tracker", out)
        self.assertNotIn("First spotted", out)

    def test_variants_sorted_by_frequency(self):
        out = self.render("INTRO", [("Rare", 2), ("Common", 98)],
                          [("Rare", 2), ("Common", 98)], "vid1")
        self.assertLess(out.index("Common"), out.index("Rare"))

    def test_retired_titles_listed_separately(self):
        # "Old" was tested historically but not in the recent window.
        out = self.render("INTRO",
                          [("A", 80), ("B", 20)],
                          [("A", 100), ("B", 40), ("Old", 30)],
                          "vid1")
        self.assertIn("also testing", out)
        self.assertIn("Old", out)

    def test_falls_back_to_all_time_when_window_empty(self):
        out = self.render("INTRO", [], [("A", 30), ("B", 10)], "vid1")
        self.assertIn("A", out)
        self.assertIn("B", out)

    def test_single_title_makes_no_ab_claim(self):
        out = self.render("INTRO", [("Only Title", 9)], [("Only Title", 40)], "vid1")
        self.assertIn("Only one title so far: Only Title", out)

    def test_caps_to_six_variants(self):
        stats = [(f"T{i}", 20 - i) for i in range(8)]
        out = self.render("INTRO", stats, stats, "vid1")
        self.assertIn("2 more", out)             # 8 variants -> 6 shown + "and 2 more"

    def test_empty(self):
        self.assertEqual(self.render("INTRO", [], [], "vid1"), "INTRO")


class TestChannelIdDetection(unittest.TestCase):
    def setUp(self):
        import scraper
        self.fn = scraper._looks_like_channel_id

    def test_channel_id(self):
        self.assertTrue(self.fn("UCHnyfMqiRRG1u-2MsSQLbXA"))

    def test_handle_and_name(self):
        self.assertFalse(self.fn("@veritasium"))
        self.assertFalse(self.fn("veritasium"))
        self.assertFalse(self.fn("UCtooShort"))


class TestMasterSwitch(unittest.TestCase):
    """The master switch's value parsing and the guards that read it.

    storage.get_setting is stubbed, so no database is involved.
    """

    def setUp(self):
        import storage
        self.storage = storage
        self._orig_get = storage.get_setting
        storage._settings_cache.clear()

    def tearDown(self):
        self.storage.get_setting = self._orig_get
        self.storage._settings_cache.clear()

    def _set(self, raw):
        self.storage.get_setting = lambda key, default=None: raw if raw is not None else default
        self.storage._settings_cache.clear()

    def test_defaults_to_on_when_never_set(self):
        # An existing deployment that has never seen the switch keeps running.
        self._set(None)
        self.assertTrue(self.storage.operations_enabled(fresh=True))

    def test_off_values(self):
        for raw in ("0", "false", "FALSE", "no", "off", " off "):
            self._set(raw)
            self.assertFalse(self.storage.operations_enabled(fresh=True), raw)

    def test_on_values(self):
        for raw in ("1", "true", "yes", "on"):
            self._set(raw)
            self.assertTrue(self.storage.operations_enabled(fresh=True), raw)

    def test_cached_read_survives_db_failure(self):
        """A DB blip must not silently flip the operating mode."""
        self._set("0")
        self.assertFalse(self.storage.operations_enabled())  # populates the cache

        def boom(key, default=None):
            raise RuntimeError("db down")

        self.storage.get_setting = boom
        self.storage._settings_cache[self.storage.OPERATIONS_ENABLED_KEY] = (
            0, "0")  # stale timestamp -> forces a re-read, which now fails
        self.assertFalse(self.storage.operations_enabled())

    def test_sweeps_do_nothing_while_paused(self):
        import main
        self._set("0")
        called = []
        orig_active, orig_channels = main.get_active_videos, main.get_enabled_channels
        main.get_active_videos = lambda: called.append("active") or []
        main.get_enabled_channels = lambda: called.append("channels") or []
        try:
            main.check_active_videos()
            main.check_new_videos()
        finally:
            main.get_active_videos, main.get_enabled_channels = orig_active, orig_channels
        self.assertEqual(called, [])  # no DB reads, no network, no comments


class TestCommentingGate(unittest.TestCase):
    """main._commenting_allowed: three independent gates, narrowest last."""

    def setUp(self):
        import main
        self.m = main
        self._orig = (main.SKIP_COMMENT, main.operations_enabled, main.commenting_enabled)

    def tearDown(self):
        (self.m.SKIP_COMMENT, self.m.operations_enabled,
         self.m.commenting_enabled) = self._orig

    def _gates(self, skip, master, commenting):
        self.m.SKIP_COMMENT = skip
        self.m.operations_enabled = lambda *a, **k: master
        self.m.commenting_enabled = lambda *a, **k: commenting

    def test_all_on(self):
        self._gates(False, True, True)
        self.assertTrue(self.m._commenting_allowed())

    def test_env_kill_switch_outranks_runtime_toggle(self):
        self._gates(True, True, True)
        self.assertFalse(self.m._commenting_allowed())

    def test_master_pause_stops_comments_too(self):
        self._gates(False, False, True)
        self.assertFalse(self.m._commenting_allowed())

    def test_commenting_off_while_still_tracking(self):
        # The point of the narrower switch: sampling continues, writes stop.
        self._gates(False, True, False)
        self.assertFalse(self.m._commenting_allowed())


class TestSamplingProfiles(unittest.TestCase):
    """The runtime fast/relaxed switch and the gate it resizes."""

    def test_profiles_differ_in_the_direction_advertised(self):
        import config
        fast = config.profile_settings("fast")
        relaxed = config.profile_settings("relaxed")
        self.assertLess(fast["new_video_check_interval"], relaxed["new_video_check_interval"])
        self.assertLess(fast["active_video_check_interval"], relaxed["active_video_check_interval"])
        self.assertGreater(fast["samples_per_run"], relaxed["samples_per_run"])
        self.assertGreater(fast["fast_samples"], relaxed["fast_samples"])
        self.assertGreaterEqual(fast["sample_concurrency"], relaxed["sample_concurrency"])

    def test_unknown_profile_falls_back_to_the_default(self):
        import config
        self.assertEqual(config.profile_settings("nonsense"),
                         config.SAMPLING_PROFILES[config.DEFAULT_SAMPLING_PROFILE])

    def test_stored_garbage_falls_back(self):
        import storage
        orig = storage.get_setting
        try:
            for raw in ("turbo", "", None):
                storage.get_setting = lambda k, d=None, r=raw: r if r is not None else d
                storage._settings_cache.clear()
                self.assertEqual(storage.sampling_profile(),
                                 storage.DEFAULT_SAMPLING_PROFILE, raw)
        finally:
            storage.get_setting = orig
            storage._settings_cache.clear()

    def test_set_rejects_unknown_name(self):
        import storage
        with self.assertRaises(ValueError):
            storage.set_sampling_profile("ludicrous")

    def test_scheduler_reads_the_live_profile(self):
        import main, config, storage
        orig = storage.get_setting
        try:
            storage.get_setting = lambda k, d=None: "fast"
            storage._settings_cache.clear()
            self.assertEqual(main._sampling(), config.profile_settings("fast"))
            storage.get_setting = lambda k, d=None: "relaxed"
            storage._settings_cache.clear()
            self.assertEqual(main._sampling(), config.profile_settings("relaxed"))
        finally:
            storage.get_setting = orig
            storage._settings_cache.clear()

    def test_gate_caps_concurrency_and_can_be_resized(self):
        """A plain Semaphore cannot grow, which is why _Gate exists."""
        import threading, time
        import youtube_innertube as it

        def peak_under(gate, limit, threads=10):
            gate.set_limit(limit)
            state = {"now": 0, "peak": 0}
            lock = threading.Lock()

            def work():
                with gate:
                    with lock:
                        state["now"] += 1
                        state["peak"] = max(state["peak"], state["now"])
                    time.sleep(0.02)
                    with lock:
                        state["now"] -= 1

            ts = [threading.Thread(target=work) for _ in range(threads)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            return state["peak"]

        gate = it._Gate(2)
        self.assertEqual(peak_under(gate, 2), 2)
        self.assertEqual(peak_under(gate, 5), 5)   # raised past its initial value
        self.assertEqual(peak_under(gate, 1), 1)   # and back down


class TestTrackingAgeCap(unittest.TestCase):
    """MAX_TRACK_DAYS retires videos the stagnation rule would keep forever."""

    def setUp(self):
        import main
        self.main = main

    def test_age_helper(self):
        from datetime import datetime, timedelta, timezone
        m = self.main
        orig, m.MAX_TRACK_DAYS = m.MAX_TRACK_DAYS, 7
        try:
            self.assertTrue(m._past_tracking_age(datetime.now() - timedelta(days=8)))
            self.assertFalse(m._past_tracking_age(datetime.now() - timedelta(days=2)))
            self.assertFalse(m._past_tracking_age(None))
            # An aware datetime must not raise -- RSS parsing has produced them.
            self.assertTrue(m._past_tracking_age(
                datetime.now(timezone.utc) - timedelta(days=30)))
            m.MAX_TRACK_DAYS = 0  # cap disabled
            self.assertFalse(m._past_tracking_age(datetime.now() - timedelta(days=999)))
        finally:
            m.MAX_TRACK_DAYS = orig

    def test_video_past_the_cap_is_retired_without_sampling(self):
        from datetime import datetime, timedelta
        m = self.main
        retired, sampled = [], []
        orig = (m.operations_enabled, m.mark_video_inactive, m.sample_titles, m.MAX_TRACK_DAYS)
        m.operations_enabled = lambda *a, **k: True
        m.mark_video_inactive = retired.append
        m.sample_titles = lambda *a, **k: sampled.append(a) or []
        m.MAX_TRACK_DAYS = 7
        try:
            m._check_one_active_video({
                "video_id": "old1",
                "channel_id": "@c",
                "channel_name": "C",
                "published_at": datetime.now() - timedelta(days=9),
            }, refresh_meta=False)
        finally:
            (m.operations_enabled, m.mark_video_inactive,
             m.sample_titles, m.MAX_TRACK_DAYS) = orig
        self.assertEqual(retired, ["old1"])
        self.assertEqual(sampled, [])  # retired before any sampling cost is paid


if __name__ == "__main__":
    unittest.main(verbosity=2)
