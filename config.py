"""Config from environment. Set these in Railway (or .env locally)."""
import os
from datetime import datetime, date
from typing import List

from dotenv import load_dotenv

load_dotenv()

# PostgreSQL connection (Railway provides DATABASE_URL)
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Multi-channel support: comma-separated channel IDs or names
# Format: "channel_id:display_name,channel_id:display_name" or just channel IDs
CHANNELS_STR = os.environ.get("YOUTUBE_CHANNELS", "UCHnyfMqiRRG1u-2MsSQLbXA:Veritasium")
CHANNELS: List[tuple[str, str]] = []
for ch in CHANNELS_STR.split(","):
    ch = ch.strip()
    if ":" in ch:
        ch_id, ch_name = ch.split(":", 1)
        CHANNELS.append((ch_id.strip(), ch_name.strip()))
    else:
        CHANNELS.append((ch.strip(), ch.strip()))

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

# Polling intervals (in seconds)
NEW_VIDEO_CHECK_INTERVAL = int(os.environ.get("NEW_VIDEO_CHECK_INTERVAL", "180"))  # 3 minutes
ACTIVE_VIDEO_CHECK_INTERVAL = int(os.environ.get("ACTIVE_VIDEO_CHECK_INTERVAL", "3600"))  # 1 hour

# Title sampling.
# YouTube assigns a fresh viewer identity to every cookieless request, so each
# sample already lands in an independent experiment bucket -- coverage is limited
# by how MANY samples we take, not by identity. A/B splits are long-tailed in
# practice (e.g. 94%/4%/2%), so a minority variant may not appear until ~sample
# 30-50. These are cumulative per hourly run; over a few hours a video accrues
# enough samples to surface ~2-4% variants. Catching a p% variant with 90%
# confidence needs ~ ln(0.1)/ln(1-p) samples (~56 for 4%, ~115 for 2%).
SAMPLES_PER_RUN = int(os.environ.get("SAMPLES_PER_RUN", "40"))
FAST_SAMPLES = int(os.environ.get("FAST_SAMPLES", "15"))  # Quick samples before posting comment

# The displayed A/B split is computed over a rolling window, not lifetime, so it
# reflects the experiment's CURRENT ratio (YouTube shifts traffic over time and
# ends tests). Distinct-variant detection still uses all-time samples.
RATIO_WINDOW_DAYS = int(os.environ.get("RATIO_WINDOW_DAYS", "3"))

# A comment is re-edited immediately when a new title variant appears, but for
# percentage-only drift we rate-limit re-edits to at most once per this many
# hours (keeps the displayed split current without burning API quota / spamming
# the "edited" marker every hour).
COMMENT_REFRESH_HOURS = int(os.environ.get("COMMENT_REFRESH_HOURS", "12"))

# Active/non-active logic: non-active if N days straight same single title
INACTIVE_DAYS_THRESHOLD = int(os.environ.get("INACTIVE_DAYS_THRESHOLD", "5"))

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
