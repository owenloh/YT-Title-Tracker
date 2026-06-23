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

    def test_lists_variants_with_frequency(self):
        out = self.render("INTRO",
                          [(date(2026, 2, 7), ["Common", "Rare"])],
                          [("Common", 90), ("Rare", 10)])
        self.assertTrue(out.startswith("INTRO"))
        self.assertIn("Common", out)
        self.assertIn("Rare", out)
        self.assertIn("~90%", out)              # observed frequency shown
        self.assertIn("~10%", out)
        self.assertIn("Feb 07", out)            # first-spotted date from history

    def test_variants_sorted_by_frequency(self):
        # input order shouldn't matter; the dominant title is listed first
        out = self.render("INTRO", [], [("Rare", 2), ("Common", 98)])
        self.assertLess(out.index("Common"), out.index("Rare"))

    def test_single_title_makes_no_ab_claim(self):
        out = self.render("INTRO", [], [("Only Title", 9)])
        self.assertIn("Only one title observed so far: Only Title", out)

    def test_caps_to_six_variants(self):
        stats = [(f"T{i}", 20 - i) for i in range(8)]
        out = self.render("INTRO", [], stats)
        self.assertIn("2 more", out)            # 8 variants -> shows 6 + "and 2 more"

    def test_empty(self):
        self.assertEqual(self.render("INTRO", [], []), "INTRO")


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
