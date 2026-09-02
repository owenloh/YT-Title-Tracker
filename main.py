"""
Main scheduler: polls channels for new videos, re-samples active videos on a
slower cadence, and keeps their comments up to date.

Master switch
-------------
Everything in here is gated on storage.operations_enabled() -- the runtime
"tracking on/off" switch flipped from the admin UI. When it is OFF the scheduler
does no outbound work at all: no RSS polls, no title sampling, no comment posts
or edits, no YouTube Data API calls. The Flask app keeps serving the dashboard,
so the site and its history stay online while the tracker itself costs nothing
beyond an idle container.

The switch is deliberately INDEPENDENT of the per-channel enabled flags: pausing
never reads or writes them, so the channel selection survives a pause untouched.
"""
import ctypes
import gc
import hashlib
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import List

from config import (
    CHANNELS,
    COMMENT_INTROS,
    COMMENT_REFRESH_HOURS,
    CUTOFF_DATE,
    INACTIVE_DAYS_THRESHOLD,
    MAX_TRACK_DAYS,
    META_REFRESH_INTERVAL,
    RATIO_WINDOW_DAYS,
    SCHEDULER_WORKERS,
    SKIP_COMMENT,
    profile_settings,
)
from scraper import get_videos_from_rss, is_short, sample_titles
import youtube_innertube
from storage import (
    COMMENTING_ENABLED_KEY,
    add_title_sample,
    add_video,
    bump_all_track_from_dates,
    get_active_videos,
    get_comment_id,
    get_comment_state,
    get_enabled_channels,
    get_known_video_ids_for_channel,
    get_recent_title_stats,
    get_title_stats,
    get_total_samples,
    get_videos_without_comments,
    init_db,
    is_video_active,
    commenting_enabled,
    mark_video_ignored,
    mark_video_inactive,
    operations_enabled,
    sampling_profile,
    seed_channel_if_missing,
    seed_setting_if_missing,
    set_comment_id,
    update_comment_edited,
    update_comment_meta,
    update_last_checked,
    update_title_history,
)
from youtube_comment import fetch_comment_meta, post_comment, update_comment

# Thread pool for background processing (channel checks, video sampling).
# I/O-bound work, so this can comfortably exceed CPU core count -- sized via
# SCHEDULER_WORKERS to give headroom as more channels are tracked.
executor = ThreadPoolExecutor(max_workers=SCHEDULER_WORKERS)

def _sampling() -> dict:
    """The sampling settings for the profile that is active right now.

    Read per use rather than captured at import, so flipping the profile in the
    admin UI changes the next sweep instead of needing a redeploy.
    """
    return profile_settings(sampling_profile())


def _past_tracking_age(published_at) -> bool:
    """Whether a video is older than MAX_TRACK_DAYS (0 disables the cap).

    INACTIVE_DAYS_THRESHOLD only retires videos that settled on ONE title, and
    it needs a run of consecutive sampled days to fire at all -- so a video with
    gaps in its sampling history, or a test that keeps flip-flopping, stayed in
    the active set indefinitely. This is the backstop.
    """
    if MAX_TRACK_DAYS <= 0 or published_at is None:
        return False
    # Stored as a naive TIMESTAMP, but RSS parsing has produced aware datetimes
    # before -- compare on the same footing either way.
    if published_at.tzinfo is not None:
        published_at = published_at.replace(tzinfo=None)
    return (datetime.now() - published_at).days >= MAX_TRACK_DAYS


def _commenting_allowed() -> bool:
    """Whether a comment may be posted or edited right now.

    Three independent gates, narrowest last: SKIP_COMMENT (deploy-level, needs a
    redeploy), the master switch (stops everything), and the runtime commenting
    switch (stops writes to YouTube while sampling and the dashboard continue).
    """
    return not SKIP_COMMENT and operations_enabled() and commenting_enabled()


def _release_memory() -> None:
    """Hand freed heap back to the OS after a sweep.

    Sampling allocates and frees large JSON documents. CPython returns those to
    its allocator, and glibc keeps them in per-thread arenas, so the process's
    resident set stays at its high-water mark forever even while idle -- and
    resident memory is what the host bills for, around the clock. malloc_trim
    releases the free arenas; it is a no-op on non-glibc platforms.
    """
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass  # not glibc (macOS/musl) -- nothing to do


# NOTE on pause/resume with no backfill: there is deliberately no "anchor
# resync" step. A channel's per-channel track_from_date cutoff is bumped to
# today whenever it's added or (re)enabled (see storage.set_channel_enabled /
# add_channel_admin), and check_channel skips any candidate published before
# that cutoff. So a channel paused for months and then re-enabled simply has
# its entire pause-window backlog skipped by the date gate -- nothing to
# process, no back-catalogue crawl -- and only genuinely new uploads (published
# on/after the resume day) are ever picked up.


def reprocess_videos_without_comments():
    """Re-sample active videos that never got a comment, so they can earn one.

    Runs once at startup. Its only purpose is backfilling a MISSING COMMENT, so
    it is skipped entirely when commenting is off -- otherwise every restart
    kicked off a sampling burst for every commentless video to produce a comment
    that would never be posted, which is the most expensive thing this process
    does and it happens on every redeploy.
    """
    if not operations_enabled():
        print("Tracking is paused - skipping reprocess of videos without comments")
        return
    if not _commenting_allowed():
        print("Commenting is off - skipping reprocess of videos without comments")
        return
    videos = get_videos_without_comments()
    if not videos:
        print("No videos without comments to reprocess")
        return

    print(f"Found {len(videos)} videos without comments - reprocessing...")
    spawned = 0
    for video in videos:
        video_id = video["video_id"]
        channel_id = video["channel_id"]
        channel_name = video["channel_name"]
        published_at = video["published_at"]

        # Same age cap the hourly sweep applies. Without it, a restart re-sampled
        # the entire backlog of old commentless videos -- videos whose title test
        # is long over -- before the sweep ever got a chance to retire them.
        if _past_tracking_age(published_at):
            mark_video_inactive(video_id)
            continue

        executor.submit(process_video, video_id, channel_id, channel_name, published_at)
        spawned += 1
        print(f"[{channel_name}] Spawned reprocess task for {video_id}")
    print(f"Reprocess: {spawned} spawned, {len(videos) - spawned} retired as too old")


_MAX_VARIANTS_SHOWN = 6

# A few body openers, picked deterministically per video so comments don't read
# like copy-paste while staying stable across re-renders (see _pick).
_BODY_LEADS = [
    "Different people are being shown different titles on this one — right now it's roughly:",
    "YouTube's quietly testing a few titles here. At the moment it's about:",
    "Heads up: the title you see depends on who you are. Right now it's roughly:",
    "Caught YouTube swapping the title around on this video. Lately it's about:",
]


def _pick(options: list, video_id: str, salt: str = "") -> str:
    """Stable per-video choice from `options` (same video -> same pick)."""
    idx = int(hashlib.md5((salt + video_id).encode()).hexdigest(), 16) % len(options)
    return options[idx]


def render_comment(intro: str, recent_stats: list, all_time_stats: list,
                   video_id: str = "") -> str:
    """Pure comment formatter (no DB) so it can be unit tested.

    recent_stats:   [(title, count), ...] within the rolling window (current split)
    all_time_stats: [(title, count), ...] over all samples (which titles exist)

    Percentages come from the RECENT window so they reflect the experiment's
    current ratio. Reads like a person sharing an observation -- no robotic
    footer/signature. Titles seen earlier but not lately are mentioned casually.
    """
    all_titles = [t for t, _ in all_time_stats]
    if not all_titles:
        return intro
    if len(all_titles) == 1:
        return f"{intro}\n\nOnly one title so far: {all_titles[0]}"

    # Current split: the recent window; fall back to all-time if the window is
    # empty (e.g. the video stopped being sampled).
    using_window = bool(recent_stats)
    basis = recent_stats if using_window else all_time_stats
    ordered = sorted(basis, key=lambda x: x[1], reverse=True)
    total = sum(count for _, count in ordered)

    lead = _pick(_BODY_LEADS, video_id, "body") if using_window \
        else "The different titles I've seen on this one:"
    lines = [intro, "", lead, ""]
    for title, count in ordered[:_MAX_VARIANTS_SHOWN]:
        pct = max(1, round(100 * count / total)) if total else None
        lines.append(f"  • “{title}”" + (f" — about {pct}% of viewers" if pct else ""))
    extra = len(ordered) - _MAX_VARIANTS_SHOWN
    if extra > 0:
        lines.append(f"  • …and {extra} more")

    # Titles seen historically but not in the current window.
    shown = {t for t, _ in ordered}
    retired = [t for t in all_titles if t not in shown]
    if retired:
        rstr = ", ".join(f"“{t}”" for t in retired[:3])
        if len(retired) > 3:
            rstr += f" and {len(retired) - 3} more"
        lines += ["", f"It was also testing {rstr} earlier on."]

    return "\n".join(lines)


def _intro_for(video_id: str) -> str:
    """Pick an intro deterministically per video (stable across re-renders so the
    hourly job doesn't re-edit just because an intro was re-randomized)."""
    return _pick(COMMENT_INTROS, video_id)


def build_comment_text(video_id: str) -> str:
    """Build comment text from this video's stored title samples."""
    return render_comment(
        _intro_for(video_id),
        get_recent_title_stats(video_id, RATIO_WINDOW_DAYS),
        get_title_stats(video_id),
        video_id,
    )


def _distinct_titles(video_id: str) -> frozenset:
    """The set of distinct titles observed for a video so far."""
    return frozenset(title for title, _ in get_title_stats(video_id))


def _maybe_update_comment(video_id: str, channel_name: str, before_titles) -> None:
    """Re-edit the comment when its rendered text actually changed.

    A NEW title variant updates immediately (timely). Percentage-only drift also
    updates -- so the displayed split stays current -- but is rate-limited to once
    per COMMENT_REFRESH_HOURS so we don't re-edit every hour (quota / "edited"
    spam). Identical text never triggers an edit.
    """
    if not _commenting_allowed():
        return
    state = get_comment_state(video_id, COMMENT_REFRESH_HOURS)
    if not state or not state["comment_id"]:
        return
    new_text = build_comment_text(video_id)
    if new_text == state["comment_text"]:
        return  # nothing visibly changed
    set_changed = before_titles is None or _distinct_titles(video_id) != before_titles
    if not set_changed and not state["refresh_due"]:
        return  # only % drift, and we refreshed recently -> wait
    try:
        if update_comment(state["comment_id"], new_text):
            update_comment_edited(video_id, new_text)
            print(f"[{channel_name}] Updated comment for {video_id}", flush=True)
    except Exception:
        # 404/403 -> comment was deleted by the uploader; stop tracking it.
        print(f"[{channel_name}] Comment deleted for {video_id} - marking ignored", flush=True)
        mark_video_ignored(video_id)


def _ensure_comment(video_id: str, channel_name: str, before_titles=None) -> None:
    """Post a comment, or update an existing one.

    A new comment is only posted once we've actually observed >= 2 distinct
    titles -- otherwise we'd be claiming an A/B test we have no evidence for
    (and most "first 15 samples" only ever see the dominant title). Existing
    comments are refreshed when a new variant turns up.
    """
    if not _commenting_allowed():
        return
    if get_comment_id(video_id):
        _maybe_update_comment(video_id, channel_name, before_titles)
        return
    if len(_distinct_titles(video_id)) < 2:
        return  # not enough evidence yet -- wait for more samples to accrue
    text = build_comment_text(video_id)
    new_id, status = post_comment(video_id, text)
    if new_id:
        set_comment_id(video_id, new_id, status, text)
        print(f"[{channel_name}] Comment posted for {video_id} (status: {status})", flush=True)
    elif status == "quota_exceeded":
        print(f"[{channel_name}] Quota exceeded - no comment for {video_id}", flush=True)
    else:
        print(f"[{channel_name}] Failed to post comment for {video_id}", flush=True)


def _record_samples(video_id: str, titles: list) -> None:
    """Persist raw samples and roll them into today's title history."""
    if not titles:
        return
    for title in titles:
        add_title_sample(video_id, title)
    update_title_history(video_id, sorted(set(titles)), date.today())


def process_video(video_id: str, channel_id: str, channel_name: str, published_at: datetime, fast_first: bool = True):
    """Sample a video's titles, store them, and post or update its comment.

    fast_first: when True and no comment exists yet, post quickly from a small
                parallel burst, then keep sampling and update the comment only if
                new variants turn up.
    """
    # Tasks are queued on a shared executor and can start after the master
    # switch was flipped off, so re-check here rather than only at submit time.
    if not operations_enabled():
        return

    print(f"[{channel_name}] Processing {video_id} (published {published_at.date()})", flush=True)

    new_video = not get_comment_id(video_id)

    # FAST PATH: brand-new video -> sample a quick burst, then deepen. We only
    # actually post once >= 2 variants are seen (see _ensure_comment), so a video
    # that isn't being A/B tested never gets a misleading "testing titles" comment.
    cfg = _sampling()
    if fast_first and new_video and _commenting_allowed():
        try:
            quick = sample_titles(video_id, cfg["fast_samples"], parallel=True)
        except Exception as e:
            print(f"[{channel_name}] ERROR sampling {video_id}: {e}", flush=True)
            quick = []

        if quick:
            _record_samples(video_id, quick)
            _ensure_comment(video_id, channel_name)

        # Deepen sampling, then post/update if a new variant turned up.
        remaining = max(0, cfg["samples_per_run"] - cfg["fast_samples"])
        if remaining:
            before = _distinct_titles(video_id)
            _record_samples(video_id, sample_titles(video_id, remaining))
            _ensure_comment(video_id, channel_name, before)

        total = get_total_samples(video_id)
        print(f"[{channel_name}] {video_id}: {total} samples, "
              f"{len(get_title_stats(video_id))} distinct titles", flush=True)
        return

    # FULL PATH: existing comment, or commenting disabled.
    before = None if new_video else _distinct_titles(video_id)
    titles = sample_titles(video_id, cfg["samples_per_run"])
    if not titles:
        print(f"[{channel_name}] No titles found for {video_id}", flush=True)
        return
    _record_samples(video_id, titles)

    total = get_total_samples(video_id)
    print(f"[{channel_name}] {video_id}: {total} samples, "
          f"{len(get_title_stats(video_id))} distinct titles", flush=True)

    _ensure_comment(video_id, channel_name, before)


def check_new_videos():
    """
    Check all enabled channels (read fresh from Postgres, not the static env
    list -- so admin UI enable/disable/add takes effect without a redeploy) for
    new videos IN PARALLEL. When new video found, spawn background task to
    process it immediately.
    """
    if not operations_enabled():
        return

    print(f"\n=== Checking for new videos at {datetime.now()} ===")

    def check_channel(channel_slug: str, channel_name: str, track_from_date) -> List[tuple]:
        """Check single channel, return list of new videos to process."""
        new_videos = []
        # Per-channel cutoff (set on add / resume) takes precedence; legacy
        # channels seeded before this existed fall back to the global cutoff.
        effective_cutoff = track_from_date or CUTOFF_DATE
        try:
            rss_videos = get_videos_from_rss(channel_slug, expected_name=channel_name, max_videos=50)
            if not rss_videos:
                return []

            # Check if we have dates (RSS) or not (HTTP fallback)
            has_dates = rss_videos[0][1] is not None

            known_ids = set(get_known_video_ids_for_channel(channel_slug, limit=50))
            
            if has_dates:
                # RSS MODE: We have publish dates.
                #
                # Anchor = the newest video we already know. We deliberately find
                # it in the RAW feed WITHOUT classifying shorts first: shorts are
                # never stored, so a short can never match known_ids and can never
                # be mistaken for the anchor. This lets us run the (expensive,
                # 1-2 HTTP calls each) is_short() check only on the handful of
                # genuinely-new candidates instead of on all ~50 feed items every
                # cycle -- the difference between a few and thousands of extra
                # requests per minute once many channels are tracked.
                anchor_index = None
                for i, (video_id, _) in enumerate(rss_videos):
                    if video_id in known_ids:
                        anchor_index = i
                        break

                # Only consider videos newer than the anchor (before it in the list).
                candidates = rss_videos[:anchor_index] if anchor_index is not None else rss_videos

                processed_count = 0
                for video_id, published_at in candidates:
                    if video_id in known_ids:
                        continue
                    # Cheap date gate BEFORE the costly shorts check: anything
                    # before this channel's cutoff (incl. a resumed channel's
                    # whole pause-window backlog) is dropped without a network call.
                    if published_at.date() < effective_cutoff:
                        continue
                    # Now pay for the shorts classification, only for new in-window videos.
                    if is_short(video_id):
                        continue
                    if add_video(video_id, channel_slug, published_at):
                        new_videos.append((video_id, channel_slug, channel_name, published_at))
                        processed_count += 1
                        print(f"[{channel_name}] NEW VIDEO: {video_id} (published {published_at.date()})")

                # First run for this channel: make sure at least one long-form
                # video is stored as an inactive anchor, so subsequent cycles have
                # a reference point and never re-crawl the back catalogue.
                if not known_ids and processed_count == 0:
                    for video_id, published_at in rss_videos:
                        if not is_short(video_id):
                            add_video(video_id, channel_slug, published_at, is_active=False)
                            break
            
            else:
                # HTTP MODE: No dates, already filtered to long-form only
                # Find newest known video in the list
                anchor_index = None
                for i, (video_id, _) in enumerate(rss_videos):
                    if video_id in known_ids:
                        anchor_index = i
                        break
                
                if anchor_index is not None:
                    # Only process videos newer than anchor (before it in list)
                    candidates = rss_videos[:anchor_index]
                    
                    for video_id, _ in candidates:
                        if video_id in known_ids:
                            continue
                        
                        if add_video(video_id, channel_slug, datetime.now()):
                            new_videos.append((video_id, channel_slug, channel_name, datetime.now()))
                            print(f"[{channel_name}] NEW VIDEO: {video_id} (HTTP, no date)")
                else:
                    # No anchor - first run via HTTP
                    # Store first video as anchor (inactive)
                    vid_id, _ = rss_videos[0]
                    add_video(vid_id, channel_slug, datetime.now(), is_active=False)
        
        except Exception as e:
            print(f"[{channel_name}] Error checking channel: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc()
        
        return new_videos
    
    # Check all enabled channels (from Postgres) in parallel
    channels = get_enabled_channels()
    futures = {
        executor.submit(check_channel, ch["channel_id"], ch["display_name"], ch["track_from_date"]):
            (ch["channel_id"], ch["display_name"])
        for ch in channels
    }
    
    # Collect new videos and spawn processing tasks
    for future in as_completed(futures):
        ch_slug, ch_name = futures[future]
        try:
            new_videos = future.result()
            if new_videos:
                for video_id, channel_slug, channel_name, published_at in new_videos:
                    # Process in background - don't block other channels
                    executor.submit(process_video, video_id, channel_slug, channel_name, published_at)
                    print(f"[{channel_name}] Spawned background task for {video_id}")
        except Exception as e:
            print(f"[{ch_name}] Channel check failed: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc()


def _check_one_active_video(video_info: dict, refresh_meta: bool = True) -> None:
    """Body of the hourly per-video check, run concurrently across all active
    videos (see check_active_videos) rather than one at a time -- with a few
    hundred active videos across many channels, a sequential loop here could
    run longer than the re-sampling interval and delay new-video checks,
    since both run on the same scheduler thread.

    refresh_meta: whether to also poll the comment's engagement metrics this
    pass (1 Data API unit each). Sampling + comment posting/editing always run;
    only this metrics poll is gated to a slower cadence."""
    if not operations_enabled():
        return

    video_id = video_info["video_id"]
    channel_name = video_info.get("channel_name") or video_info["channel_id"]

    try:
        # Aged out -> stop tracking, whatever the title is still doing. The
        # stagnation rule below only retires videos that settled on ONE title,
        # so a video whose experiment keeps flip-flopping stayed in the active
        # set indefinitely and the sweep grew without bound. Title experiments
        # are decided within days of upload; MAX_TRACK_DAYS = 0 disables this.
        if _past_tracking_age(video_info.get("published_at")):
            print(f"[{channel_name}] {video_id} is past the {MAX_TRACK_DAYS}d "
                  f"tracking cap - marking inactive")
            mark_video_inactive(video_id)
            return

        # Stagnated (same single title for N days straight) -> stop tracking.
        # The comment already reflects the latest titles from prior checks, so
        # there's nothing new to post here.
        if not is_video_active(video_id, INACTIVE_DAYS_THRESHOLD):
            print(f"[{channel_name}] {video_id} stagnated ({INACTIVE_DAYS_THRESHOLD}+ days) - marking inactive")
            mark_video_inactive(video_id)
            return

        update_last_checked(video_id)

        # Always re-sample so new variants are caught on every hourly pass.
        # _ensure_comment posts the first comment if this pass is what finally
        # pushes the video to >= 2 distinct titles, otherwise it updates. This
        # (posting/editing on a new variant) is NEVER throttled -- it runs every
        # hour and is the timely part.
        before = _distinct_titles(video_id)
        titles = sample_titles(video_id, _sampling()["samples_per_run"], parallel=True)
        if not titles:
            return
        _record_samples(video_id, titles)
        _ensure_comment(video_id, channel_name, before)

        # Engagement/status refresh (likes, replies, held->published) costs 1
        # Data API unit per comment and does NOT affect posting -- so it runs on
        # a slower cadence (refresh_meta) to keep the daily quota in check at
        # scale, rather than every hourly pass.
        if refresh_meta:
            cid = get_comment_id(video_id)
            if cid:
                meta = fetch_comment_meta(cid)
                if meta:
                    update_comment_meta(video_id, meta["status"], meta["likes"], meta["replies"])

    except Exception as e:
        print(f"Error checking video {video_id}: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()


def check_active_videos(refresh_meta: bool = True):
    """Check active videos for title changes (hourly task).

    Videos are already filtered by is_active = TRUE in the database.
    If a video has stagnated (same single title for N days), mark it inactive permanently.

    Runs across the shared executor (same pattern as check_new_videos) so the
    sweep finishes in roughly one video's worth of wall-clock time instead of
    len(active_videos) times that -- necessary once many channels/videos are
    tracked, since this and check_new_videos share the scheduler's main thread.

    refresh_meta: forwarded per-video; when False this pass samples + posts/edits
    comments as usual but skips the engagement-metric API poll (see the scheduler
    loop, which only enables it every META_REFRESH_INTERVAL).
    """
    if not operations_enabled():
        return

    print(f"\n=== Checking active videos at {datetime.now()} (refresh_meta={refresh_meta}) ===")

    active_videos = get_active_videos()
    print(f"Found {len(active_videos)} active videos to check")

    futures = [executor.submit(_check_one_active_video, v, refresh_meta) for v in active_videos]
    for future in as_completed(futures):
        try:
            future.result()  # errors are caught/logged inside; this catches bugs in the wrapper
        except Exception as e:
            # Must never escape: this runs on the scheduler thread, and an
            # exception here would kill the loop and silently stop all tracking
            # while the web process carried on serving.
            print(f"Active-video check crashed: {e}", file=sys.stderr)

    # The sweep just allocated and freed a lot of large documents; give the
    # memory back rather than sitting on the high-water mark until restart.
    _release_memory()


def run_scheduler():
    """Run the main scheduler loop.

    Honours the master switch: while tracking is paused the loop does nothing
    but tick, so the process idles and the dashboard stays up.
    """
    print("Initializing database...")
    init_db()

    # Seed channels from the env-var list on first boot only -- this never
    # overwrites a channel that already exists (see seed_channel_if_missing),
    # so it's safe to leave YOUTUBE_CHANNELS set permanently. From here on,
    # Postgres (managed via the admin UI) is the source of truth.
    for ch_id, ch_name in CHANNELS:
        seed_channel_if_missing(ch_id, ch_name)

    # Reprocess any videos that have no comments (from failed earlier runs)
    reprocess_videos_without_comments()

    # Seed the runtime commenting switch from the deploy-time env var the first
    # time this database sees it; after that the admin UI owns it (same pattern
    # as seeding channels from YOUTUBE_CHANNELS).
    seed_setting_if_missing(COMMENTING_ENABLED_KEY, "0" if SKIP_COMMENT else "1")

    enabled_count = len(get_enabled_channels())
    running = operations_enabled(fresh=True)
    profile = sampling_profile()
    cfg = profile_settings(profile)
    youtube_innertube.set_sample_concurrency(cfg["sample_concurrency"])
    print(f"Starting scheduler:")
    print(f"  - Tracking (master switch): {'ON' if running else 'OFF (paused)'}")
    print(f"  - Commenting: {'ON' if _commenting_allowed() else 'OFF'}"
          f"{' (forced off by SKIP_COMMENT)' if SKIP_COMMENT else ''}")
    print(f"  - Sampling profile: {profile}")
    print(f"  - New video check: every {cfg['new_video_check_interval']}s")
    print(f"  - Active video check: every {cfg['active_video_check_interval']}s")
    print(f"  - Samples per run: {cfg['samples_per_run']} "
          f"(first burst {cfg['fast_samples']}, "
          f"max {cfg['sample_concurrency']} concurrent)")
    print(f"  - Channels enabled: {enabled_count}")
    print(f"  - Fallback cutoff date (legacy channels only): {CUTOFF_DATE}")
    print(f"  - Inactive threshold: {INACTIVE_DAYS_THRESHOLD} days")
    print(f"  - Max tracking age: {MAX_TRACK_DAYS or 'unlimited'} days")
    print(f"  - Scheduler workers: {SCHEDULER_WORKERS}")

    last_new_check = 0
    last_active_check = time.time()  # Don't run the sampling sweep immediately on startup
    last_meta_check = 0  # Refresh engagement metrics on the first active sweep
    was_running = running
    was_profile = profile

    try:
        while True:
            now = time.time()

            # Master switch. Checked first and on every tick so a pause from the
            # admin UI takes effect within seconds, without a redeploy.
            running = operations_enabled()
            if running != was_running:
                if running:
                    # Resuming: move every channel's cutoff to today so the
                    # backlog that piled up during the pause is skipped by the
                    # date gate. Without this, resuming after weeks off would
                    # process every upload since the pause in one burst and
                    # comment on videos whose title tests ended long ago. This
                    # touches track_from_date ONLY -- the per-channel enabled
                    # flags are never read or written by the master switch.
                    bumped = bump_all_track_from_dates()
                    print(f"\n*** Tracking RESUMED - cutoff moved to today for "
                          f"{bumped} channels (no backfill) ***", flush=True)
                    # Don't fire both sweeps the instant we resume.
                    last_new_check = now
                    last_active_check = now
                else:
                    print("\n*** Tracking PAUSED - no polling, sampling or "
                          "comments until re-enabled ***", flush=True)
                    _release_memory()
                was_running = running

            if not running:
                time.sleep(10)
                continue

            # Sampling profile, same deal: re-read every tick so a switch in the
            # admin UI applies to the next sweep, not the next deploy.
            profile = sampling_profile()
            cfg = profile_settings(profile)
            if profile != was_profile:
                youtube_innertube.set_sample_concurrency(cfg["sample_concurrency"])
                print(f"\n*** Sampling profile -> {profile}: check every "
                      f"{cfg['new_video_check_interval']}s, re-sample every "
                      f"{cfg['active_video_check_interval']}s, "
                      f"{cfg['samples_per_run']} samples/sweep ***", flush=True)
                if cfg["sample_concurrency"] < profile_settings(was_profile)["sample_concurrency"]:
                    _release_memory()  # stepping down -> give the headroom back
                was_profile = profile

            # Check for new videos
            if now - last_new_check >= cfg["new_video_check_interval"]:
                check_new_videos()
                last_new_check = now

            # Check active videos. Sampling + comment posting/editing run every
            # ACTIVE_VIDEO_CHECK_INTERVAL, but the (1 Data API unit each)
            # engagement-metric poll only piggybacks on this sweep every
            # META_REFRESH_INTERVAL -- keeping the daily quota in check at scale.
            if now - last_active_check >= cfg["active_video_check_interval"]:
                refresh_meta = now - last_meta_check >= META_REFRESH_INTERVAL
                check_active_videos(refresh_meta=refresh_meta)
                last_active_check = now
                if refresh_meta:
                    last_meta_check = now

            # Sleep for a short time to avoid busy loop
            time.sleep(10)
    
    except KeyboardInterrupt:
        print("\nShutting down scheduler...")
        sys.exit(0)


if __name__ == "__main__":
    run_scheduler()
