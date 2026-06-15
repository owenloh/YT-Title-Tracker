"""
Main scheduler: checks for new videos every 3 minutes, checks active videos every hour.
Processes videos immediately and tracks title history.
"""
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import List

from config import (
    ACTIVE_VIDEO_CHECK_INTERVAL,
    CHANNELS,
    COMMENT_INTROS,
    CUTOFF_DATE,
    FAST_SAMPLES,
    INACTIVE_DAYS_THRESHOLD,
    NEW_VIDEO_CHECK_INTERVAL,
    SAMPLES_PER_RUN,
    SKIP_COMMENT,
)
from scraper import get_videos_from_rss, is_short, sample_titles
from storage import (
    add_channel,
    add_title_sample,
    add_video,
    get_active_videos,
    get_comment_id,
    get_known_video_ids_for_channel,
    get_title_history_by_date,
    get_title_stats,
    get_total_samples,
    get_videos_without_comments,
    init_db,
    is_video_active,
    mark_video_ignored,
    mark_video_inactive,
    set_comment_id,
    update_comment_edited,
    update_last_checked,
    update_title_history,
)
from youtube_comment import post_comment, update_comment

# Thread pool for background processing
executor = ThreadPoolExecutor(max_workers=10)


def reprocess_videos_without_comments():
    """Find and reprocess any active videos that don't have comments yet."""
    videos = get_videos_without_comments()
    if not videos:
        print("No videos without comments to reprocess")
        return
    
    print(f"Found {len(videos)} videos without comments - reprocessing...")
    for video in videos:
        video_id = video["video_id"]
        channel_id = video["channel_id"]
        channel_name = video["channel_name"]
        published_at = video["published_at"]
        
        executor.submit(process_video, video_id, channel_id, channel_name, published_at)
        print(f"[{channel_name}] Spawned reprocess task for {video_id}")


def render_comment(intro: str, history: list, stats: list) -> str:
    """Pure comment formatter (no DB) so it can be unit tested.

    history: [(date, [titles]), ...]   stats: [(title, count), ...]
    """
    if history:
        # Timeline oldest -> newest, like a changelog: "Feb 07: Title A | Title B".
        lines = [intro, ""]
        for hist_date, titles in sorted(history, key=lambda x: x[0]):
            date_str = hist_date.strftime("%b %d")
            if len(titles) == 1:
                lines.append(f"{date_str}: {titles[0][:90]}")
            else:
                shown = " | ".join(t[:60] for t in titles[:4])
                if len(titles) > 4:
                    shown += f" (+{len(titles) - 4} more)"
                lines.append(f"{date_str}: {shown}")
        return "\n".join(lines)

    if stats:
        titles = [t for t, _ in stats]
        if len(titles) == 1:
            return f"{intro}\n\nCurrent title: {titles[0][:100]}"
        shown = " | ".join(t[:60] for t in titles[:4])
        if len(titles) > 4:
            shown += f" (+{len(titles) - 4} more)"
        return f"{intro}\n\nTitles seen: {shown}"

    return intro


def build_comment_text(video_id: str) -> str:
    """Build comment text from this video's stored title history."""
    intro = random.choice(COMMENT_INTROS)
    return render_comment(intro, get_title_history_by_date(video_id), get_title_stats(video_id))


def _maybe_update_comment(video_id: str, channel_name: str, before_text) -> None:
    """Rebuild the comment and push an update only if its visible text changed.

    Passing before_text=None forces an update regardless. Comparing against the
    previously rendered text keeps YouTube API quota proportional to real changes.
    """
    if SKIP_COMMENT:
        return
    comment_id = get_comment_id(video_id)
    if not comment_id:
        return
    after_text = build_comment_text(video_id)
    if before_text is not None and after_text == before_text:
        return
    try:
        if update_comment(comment_id, after_text):
            update_comment_edited(video_id)
            print(f"[{channel_name}] Updated comment for {video_id}", flush=True)
    except Exception:
        # 404/403 -> comment was deleted by the uploader; stop tracking it.
        print(f"[{channel_name}] Comment deleted for {video_id} - marking ignored", flush=True)
        mark_video_ignored(video_id)


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
    print(f"[{channel_name}] Processing {video_id} (published {published_at.date()})", flush=True)

    new_video = not get_comment_id(video_id)

    # FAST PATH: brand-new video -> get a comment up fast, then deepen.
    if fast_first and new_video and not SKIP_COMMENT:
        try:
            quick = sample_titles(video_id, FAST_SAMPLES, parallel=True)
        except Exception as e:
            print(f"[{channel_name}] ERROR sampling {video_id}: {e}", flush=True)
            quick = []

        if quick:
            _record_samples(video_id, quick)
            new_id, status = post_comment(video_id, build_comment_text(video_id))
            if new_id:
                set_comment_id(video_id, new_id, status)
                print(f"[{channel_name}] Comment posted for {video_id} (status: {status})", flush=True)
            elif status == "quota_exceeded":
                print(f"[{channel_name}] Quota exceeded - no comment for {video_id}", flush=True)
            else:
                print(f"[{channel_name}] Failed to post comment for {video_id}", flush=True)

        # Deepen sampling, then update the comment only if the content changed.
        remaining = max(0, SAMPLES_PER_RUN - FAST_SAMPLES)
        if remaining:
            before = build_comment_text(video_id)
            _record_samples(video_id, sample_titles(video_id, remaining))
            _maybe_update_comment(video_id, channel_name, before)

        total = get_total_samples(video_id)
        print(f"[{channel_name}] {video_id}: {total} samples, "
              f"{len(get_title_stats(video_id))} distinct titles", flush=True)
        return

    # FULL PATH: existing comment, or commenting disabled.
    before = None if new_video else build_comment_text(video_id)
    titles = sample_titles(video_id, SAMPLES_PER_RUN)
    if not titles:
        print(f"[{channel_name}] No titles found for {video_id}", flush=True)
        return
    _record_samples(video_id, titles)

    total = get_total_samples(video_id)
    print(f"[{channel_name}] {video_id}: {total} samples, "
          f"{len(get_title_stats(video_id))} distinct titles", flush=True)

    if SKIP_COMMENT:
        return

    comment_id = get_comment_id(video_id)
    if comment_id:
        _maybe_update_comment(video_id, channel_name, before)
    else:
        new_id, status = post_comment(video_id, build_comment_text(video_id))
        if new_id:
            set_comment_id(video_id, new_id, status)
            print(f"[{channel_name}] Posted comment for {video_id}", flush=True)
        else:
            print(f"[{channel_name}] Failed to post comment for {video_id}", flush=True)


def check_new_videos():
    """
    Check all channels for new videos IN PARALLEL.
    When new video found, spawn background task to process it immediately.
    """
    print(f"\n=== Checking for new videos at {datetime.now()} ===")
    
    def check_channel(channel_slug: str, channel_name: str) -> List[tuple]:
        """Check single channel, return list of new videos to process."""
        new_videos = []
        try:
            rss_videos = get_videos_from_rss(channel_slug, expected_name=channel_name, max_videos=50)
            if not rss_videos:
                return []
            
            # Check if we have dates (RSS) or not (HTTP fallback)
            has_dates = rss_videos[0][1] is not None
            
            add_channel(channel_slug, channel_name)
            known_ids = set(get_known_video_ids_for_channel(channel_slug, limit=50))
            
            if has_dates:
                # RSS MODE: We have publish dates
                # RSS includes shorts, so filter them out first before anchor detection
                # NEVER store shorts - they can't be anchors for HTTP fallback
                
                # Filter to long-form videos only
                long_form_videos = []
                for video_id, published_at in rss_videos:
                    if not is_short(video_id):
                        long_form_videos.append((video_id, published_at))
                
                if not long_form_videos:
                    print(f"[{channel_name}] No long-form videos found")
                    return new_videos
                
                # Find anchor (first known video) in long-form list
                anchor_index = None
                for i, (video_id, _) in enumerate(long_form_videos):
                    if video_id in known_ids:
                        anchor_index = i
                        break
                
                # Only consider videos before anchor (newer)
                candidates = long_form_videos[:anchor_index] if anchor_index is not None else long_form_videos
                
                processed_count = 0
                for video_id, published_at in candidates:
                    # Skip if before cutoff - don't store at all
                    if published_at.date() < CUTOFF_DATE:
                        continue
                    
                    # Skip if already known
                    if video_id in known_ids:
                        continue
                    
                    # Store and process long-form video
                    if add_video(video_id, channel_slug, published_at):
                        new_videos.append((video_id, channel_slug, channel_name, published_at))
                        processed_count += 1
                        print(f"[{channel_name}] NEW VIDEO: {video_id} (published {published_at.date()})")
                
                # First run: ensure at least 1 long-form video exists for HTTP fallback anchor
                if not known_ids and processed_count == 0:
                    # No videos after cutoff - store newest long-form as anchor (inactive)
                    vid_id, vid_date = long_form_videos[0]
                    add_video(vid_id, channel_slug, vid_date, is_active=False)
            
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
    
    # Check all channels in parallel
    futures = {
        executor.submit(check_channel, ch_slug, ch_name): (ch_slug, ch_name)
        for ch_slug, ch_name in CHANNELS
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


def check_active_videos():
    """Check active videos for title changes (hourly task).
    
    Videos are already filtered by is_active = TRUE in the database.
    If a video has stagnated (same single title for N days), mark it inactive permanently.
    """
    print(f"\n=== Checking active videos at {datetime.now()} ===")

    active_videos = get_active_videos()
    print(f"Found {len(active_videos)} active videos to check")

    for video_info in active_videos:
        video_id = video_info["video_id"]
        channel_name = video_info.get("channel_name") or video_info["channel_id"]

        try:
            # Stagnated (same single title for N days straight) -> stop tracking.
            # The comment already reflects the latest titles from prior checks, so
            # there's nothing new to post here.
            if not is_video_active(video_id, INACTIVE_DAYS_THRESHOLD):
                print(f"[{channel_name}] {video_id} stagnated ({INACTIVE_DAYS_THRESHOLD}+ days) - marking inactive")
                mark_video_inactive(video_id)
                continue

            update_last_checked(video_id)

            # Always re-sample so new variants are caught on every hourly pass.
            before = build_comment_text(video_id)
            titles = sample_titles(video_id, SAMPLES_PER_RUN)
            if not titles:
                continue
            _record_samples(video_id, titles)
            _maybe_update_comment(video_id, channel_name, before)

        except Exception as e:
            print(f"Error checking video {video_id}: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc()


def run_scheduler():
    """Run the main scheduler loop."""
    print("Initializing database...")
    init_db()
    
    # Reprocess any videos that have no comments (from failed earlier runs)
    reprocess_videos_without_comments()
    
    print(f"Starting scheduler:")
    print(f"  - New video check: every {NEW_VIDEO_CHECK_INTERVAL}s")
    print(f"  - Active video check: every {ACTIVE_VIDEO_CHECK_INTERVAL}s")
    print(f"  - Channels: {len(CHANNELS)}")
    print(f"  - Cutoff date: {CUTOFF_DATE}")
    print(f"  - Inactive threshold: {INACTIVE_DAYS_THRESHOLD} days")
    
    last_new_check = 0
    last_active_check = time.time()  # Don't run hourly check immediately on startup
    
    try:
        while True:
            now = time.time()
            
            # Check for new videos
            if now - last_new_check >= NEW_VIDEO_CHECK_INTERVAL:
                check_new_videos()
                last_new_check = now
            
            # Check active videos
            if now - last_active_check >= ACTIVE_VIDEO_CHECK_INTERVAL:
                check_active_videos()
                last_active_check = now
            
            # Sleep for a short time to avoid busy loop
            time.sleep(10)
    
    except KeyboardInterrupt:
        print("\nShutting down scheduler...")
        sys.exit(0)


if __name__ == "__main__":
    run_scheduler()
