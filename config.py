"""Config from environment. Set these in Railway (or .env locally)."""
import os
from datetime import datetime, date
from typing import List

from dotenv import load_dotenv

load_dotenv()

# PostgreSQL connection (Railway provides DATABASE_URL)
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Multi-channel support: comma-separated channel IDs or @handles.
# Format: "channel_id_or_handle:display_name,..." or just IDs/handles.
# This is only the SEED list used to populate the `channels` table on first boot
# (see storage.seed_channel_if_missing) -- after that, Postgres is the source of
# truth and channels are managed via the admin UI (enable/disable/add), not by
# editing this env var and redeploying.
def parse_channels_str(raw: str) -> List[tuple]:
    channels = []
    for ch in raw.split(","):
        ch = ch.strip()
        if not ch:
            continue
        if ":" in ch:
            ch_id, ch_name = ch.split(":", 1)
            channels.append((ch_id.strip(), ch_name.strip()))
        else:
            channels.append((ch.strip(), ch.strip()))
    return channels


CHANNELS_STR = os.environ.get("YOUTUBE_CHANNELS", "UCHnyfMqiRRG1u-2MsSQLbXA:Veritasium")
CHANNELS: List[tuple[str, str]] = parse_channels_str(CHANNELS_STR)

# Thread pool size for the scheduler (channel checks + active-video sampling).
# I/O-bound work, so this can exceed CPU core count -- but NOT by much, because
# the real constraint here is MEMORY, not CPU. Each in-flight InnerTube /next
# response is a multi-megabyte JSON document that balloons ~5-10x once parsed
# into Python objects, and CPython never returns that memory to the OS once the
# high-water mark is set. The old value (24 workers, each fanning out to 8 more
# sampling threads = ~192 concurrent parses) is what drove the deployment to a
# permanent ~2.9 GB resident set -- which is what the hosting bill is actually
# made of, since measured CPU use is near zero. Keep this small.
SCHEDULER_WORKERS = int(os.environ.get("SCHEDULER_WORKERS", "6"))

# DB connection pool ceiling. Must exceed SCHEDULER_WORKERS (each worker may hold
# a connection briefly) plus headroom for concurrent Flask API requests, or the
# pool raises "connection pool exhausted" under load. Keep below the Postgres
# server's max_connections (Railway Postgres defaults to ~100).
DB_POOL_MAX = int(os.environ.get("DB_POOL_MAX", "12"))

# OAuth for posting/editing comments
YOUTUBE_CLIENT_ID = os.environ.get("YOUTUBE_CLIENT_ID", "")
YOUTUBE_CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET", "")
YOUTUBE_REFRESH_TOKEN = os.environ.get("YOUTUBE_REFRESH_TOKEN", "")

# Date cutoff: only process videos from this date onwards (checked on every call)
CUTOFF_DATE_STR = os.environ.get("CUTOFF_DATE", "2026-02-08")
try:
    CUTOFF_DATE = datetime.strptime(CUTOFF_DATE_STR, "%Y-%m-%d").date()
except ValueError:
    CUTOFF_DATE = date(2026, 2, 8)  # Default: Feb 8, 2026

# Polling intervals (in seconds).
#
# These were originally tuned to be FIRST in the comments section, which meant
# polling every channel's RSS feed every 3 minutes and re-sampling every active
# video every hour. Being first is no longer the goal, so both cadences are much
# slower now: a title experiment runs for days, so a 30-minute discovery lag and
# a 6-hourly re-sample lose essentially no data while cutting outbound request
# volume (and the peak memory that comes with it) by ~10x and ~6x respectively.
NEW_VIDEO_CHECK_INTERVAL = int(os.environ.get("NEW_VIDEO_CHECK_INTERVAL", "1800"))  # 30 minutes
ACTIVE_VIDEO_CHECK_INTERVAL = int(os.environ.get("ACTIVE_VIDEO_CHECK_INTERVAL", "21600"))  # 6 hours

# How often to refresh each comment's engagement metrics (likes/replies/moderation
# status) via the YouTube Data API. This is DECOUPLED from the hourly sampling
# sweep: sampling + comment posting/editing still run every hour (a new variant is
# still commented immediately), but the metrics poll -- which costs 1 Data API
# unit per comment and does NOT affect posting -- runs only this often, to keep
# the daily quota (10k units) from being dominated by engagement polling at scale.
META_REFRESH_INTERVAL = int(os.environ.get("META_REFRESH_INTERVAL", "21600"))  # 6 hours

# Title sampling.
# YouTube assigns a fresh viewer identity to every cookieless request, so each
# sample already lands in an independent experiment bucket -- coverage is limited
# by how MANY samples we take, not by identity. A/B splits are long-tailed in
# practice (e.g. 94%/4%/2%), so a minority variant may not appear until ~sample
# 30-50. Samples are CUMULATIVE across runs, which is what makes a smaller
# per-run count affordable: a video sampled 15x every 6 hours still accrues 60
# samples a day and ~180 over the 3-day ratio window, enough to surface a ~2%
# variant (catching a p% variant with 90% confidence needs ~ln(0.1)/ln(1-p)
# samples: ~56 for 4%, ~115 for 2%). It arrives over hours instead of minutes,
# which only matters if the goal is to comment first -- it no longer is.
SAMPLES_PER_RUN = int(os.environ.get("SAMPLES_PER_RUN", "15"))

# Samples taken in the immediate burst when a NEW video is first detected, BEFORE
# posting the first comment. Kept a bit above SAMPLES_PER_RUN because the first
# comment should not claim an A/B test on the strength of two samples, but well
# below the old 90: that burst fired 90 concurrent requests per new video, and
# several new videos landing together was the single worst memory spike in the
# process. Missed minority variants are picked up by later runs and the comment
# is edited then.
FAST_SAMPLES = int(os.environ.get("FAST_SAMPLES", "30"))  # Quick burst before first comment

# Hard ceiling on how many InnerTube sampling requests are in flight AT ONCE,
# process-wide (not per video). Every in-flight response is a multi-megabyte
# JSON blob, so this -- not the sample count -- is what sets the process's peak
# memory, and CPython keeps that high-water mark for the life of the process.
# Previously each video fanned out to its own pool of 8 with no global ceiling,
# so N videos sampling at once meant 8N concurrent parses.
SAMPLE_CONCURRENCY = int(os.environ.get("SAMPLE_CONCURRENCY", "4"))

# The displayed A/B split is computed over a rolling window, not lifetime, so it
# reflects the experiment's CURRENT ratio (YouTube shifts traffic over time and
# ends tests). Distinct-variant detection still uses all-time samples.
RATIO_WINDOW_DAYS = int(os.environ.get("RATIO_WINDOW_DAYS", "3"))

# A comment is re-edited IMMEDIATELY when a new title variant appears; this only
# rate-limits percentage-only drift re-edits to at most once per this many hours
# (keeps the displayed split current without burning API quota -- each edit costs
# 50 Data API units -- or spamming the "edited" marker). Raised to 24h at scale;
# it does NOT delay first-post or new-variant edits, only cosmetic %-drift updates.
COMMENT_REFRESH_HOURS = int(os.environ.get("COMMENT_REFRESH_HOURS", "24"))

# Active/non-active logic: non-active if N days straight same single title
INACTIVE_DAYS_THRESHOLD = int(os.environ.get("INACTIVE_DAYS_THRESHOLD", "5"))

# Hard age cap on tracking: stop sampling a video once it is this many days old,
# whatever its title is still doing. INACTIVE_DAYS_THRESHOLD only retires videos
# that have settled on ONE title, so a video whose test keeps flip-flopping was
# previously sampled forever, and the set of "active" videos only ever grew --
# the main reason steady-state cost crept up over time. Title experiments are
# decided in the first days, so this loses little. Set to 0 to disable the cap.
MAX_TRACK_DAYS = int(os.environ.get("MAX_TRACK_DAYS", "7"))

# Random intro lines for comments
COMMENT_INTROS = [
    "I noticed YouTube is testing different titles on this video",
    "Interesting, this video seems to have multiple titles being tested",
    "Anyone else seeing a different title? YouTube A/B testing perhaps",
    "The title on this video keeps changing for me",
    "YouTube appears to be running a title experiment here",
    "Different people are seeing different titles on this one",
    "Caught this video with multiple title variations",
    "This video has different titles showing for different viewers",
    "Title A/B test spotted on this video",
    "YouTube is definitely testing titles on this one",
]

# Set to 1 to run without posting/updating YouTube comment
SKIP_COMMENT = os.environ.get("SKIP_COMMENT", "0").strip().lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Sampling profiles
#
# Two named speeds, switchable at RUNTIME from the admin UI (the choice lives in
# app_settings.sampling_profile, like the other switches) rather than at deploy
# time. Everything above is the "relaxed" profile and stays env-overridable
# exactly as before; "fast" restores the original aggressive cadence for when a
# video is worth watching closely.
#
# Fast is ~10x the discovery requests and ~16x the sampling requests of relaxed
# (6x as many sweeps, 2.7x the samples in each). It is no longer a memory
# question -- peak memory is bounded by SAMPLE_CONCURRENCY either way -- so the
# cost is request volume and a modestly higher (still small) resident set.
# ---------------------------------------------------------------------------
def _fast(name: str, default: int) -> int:
    """A fast-profile knob, overridable as FAST_MODE_<name>."""
    return int(os.environ.get(f"FAST_MODE_{name}", str(default)))


SAMPLING_PROFILES = {
    "relaxed": {
        "new_video_check_interval": NEW_VIDEO_CHECK_INTERVAL,
        "active_video_check_interval": ACTIVE_VIDEO_CHECK_INTERVAL,
        "samples_per_run": SAMPLES_PER_RUN,
        "fast_samples": FAST_SAMPLES,
        "sample_concurrency": SAMPLE_CONCURRENCY,
    },
    # The original settings, before the cost work: poll every 3 minutes,
    # re-sample hourly, 40 samples a sweep and a 90-sample opening burst.
    "fast": {
        "new_video_check_interval": _fast("NEW_VIDEO_CHECK_INTERVAL", 180),
        "active_video_check_interval": _fast("ACTIVE_VIDEO_CHECK_INTERVAL", 3600),
        "samples_per_run": _fast("SAMPLES_PER_RUN", 40),
        "fast_samples": _fast("FAST_SAMPLES", 90),
        "sample_concurrency": _fast("SAMPLE_CONCURRENCY", 8),
    },
}

DEFAULT_SAMPLING_PROFILE = "relaxed"

# The concurrency gate is sized once at import for the widest profile, then
# narrowed at runtime -- a semaphore cannot grow past its initial value.
MAX_SAMPLE_CONCURRENCY = max(
    p["sample_concurrency"] for p in SAMPLING_PROFILES.values()
)


def profile_settings(name: str) -> dict:
    """Effective sampling settings for a profile name (unknown -> the default)."""
    return SAMPLING_PROFILES.get(name, SAMPLING_PROFILES[DEFAULT_SAMPLING_PROFILE])
