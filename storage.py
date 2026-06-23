"""PostgreSQL storage for channels, videos, title samples, and comments."""
import os
from datetime import date, datetime
from typing import List, Optional, Tuple

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import SimpleConnectionPool

from config import DATABASE_URL, RATIO_WINDOW_DAYS

# Connection pool (min 1, max 20 connections)
_pool: Optional[SimpleConnectionPool] = None


def get_pool():
    """Get or create connection pool."""
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise ValueError("DATABASE_URL not set")
        _pool = SimpleConnectionPool(1, 20, DATABASE_URL)
    return _pool


def get_conn():
    """Get a connection from the pool."""
    pool = get_pool()
    return pool.getconn()


def return_conn(conn):
    """Return connection to the pool."""
    pool = get_pool()
    pool.putconn(conn)


def init_db():
    """Initialize database schema."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS channels (
                    channel_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS videos (
                    video_id TEXT PRIMARY KEY,
                    channel_id TEXT NOT NULL REFERENCES channels(channel_id),
                    published_at TIMESTAMP NOT NULL,
                    is_ignored BOOLEAN DEFAULT FALSE,
                    is_deleted BOOLEAN DEFAULT FALSE,
                    is_active BOOLEAN DEFAULT TRUE,
                    comment_id TEXT,
                    comment_posted_at TIMESTAMP,
                    comment_last_edited_at TIMESTAMP,
                    last_checked_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS title_samples (
                    id SERIAL PRIMARY KEY,
                    video_id TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
                    title_text TEXT NOT NULL,
                    sampled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS title_history (
                    id SERIAL PRIMARY KEY,
                    video_id TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
                    title_text TEXT NOT NULL,
                    first_seen_date DATE NOT NULL,
                    last_seen_date DATE NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(video_id, title_text, first_seen_date)
                );

                CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel_id);
                CREATE INDEX IF NOT EXISTS idx_videos_published ON videos(published_at);
                CREATE INDEX IF NOT EXISTS idx_videos_ignored ON videos(is_ignored);
                CREATE INDEX IF NOT EXISTS idx_title_samples_video ON title_samples(video_id);
                CREATE INDEX IF NOT EXISTS idx_title_samples_at ON title_samples(sampled_at);
                CREATE INDEX IF NOT EXISTS idx_title_history_video ON title_history(video_id);
                CREATE INDEX IF NOT EXISTS idx_title_history_date ON title_history(first_seen_date);
            """)
            
            # Migration: add is_deleted column if it doesn't exist (for existing databases)
            cur.execute("""
                DO $$ 
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns 
                        WHERE table_name='videos' AND column_name='is_deleted'
                    ) THEN
                        ALTER TABLE videos ADD COLUMN is_deleted BOOLEAN DEFAULT FALSE;
                    END IF;
                END $$;
            """)
            
            # Create index for is_deleted (safe to run even if column already exists)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_videos_deleted ON videos(is_deleted)")
            
            # Migration: add is_active column if it doesn't exist
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='videos' AND column_name='is_active'
                    ) THEN
                        ALTER TABLE videos ADD COLUMN is_active BOOLEAN DEFAULT TRUE;
                    END IF;
                END $$;
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_videos_active ON videos(is_active)")
            
            # Migration: add last_seen_date column to title_history if it doesn't exist
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='title_history' AND column_name='last_seen_date'
                    ) THEN
                        ALTER TABLE title_history ADD COLUMN last_seen_date DATE;
                        UPDATE title_history SET last_seen_date = first_seen_date WHERE last_seen_date IS NULL;
                        ALTER TABLE title_history ALTER COLUMN last_seen_date SET NOT NULL;
                    END IF;
                END $$;
            """)
            
            # Migration: add comment_status column to videos
            cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='videos' AND column_name='comment_status'
                    ) THEN
                        ALTER TABLE videos ADD COLUMN comment_status TEXT;
                    END IF;
                END $$;
            """)

            # Migration: store the rendered comment text (to detect real changes
            # before re-editing) and engagement counts (likes/replies = virality).
            cur.execute("""
                ALTER TABLE videos
                    ADD COLUMN IF NOT EXISTS comment_text TEXT,
                    ADD COLUMN IF NOT EXISTS comment_like_count INTEGER DEFAULT 0,
                    ADD COLUMN IF NOT EXISTS comment_reply_count INTEGER DEFAULT 0;
            """)
        conn.commit()
    finally:
        return_conn(conn)


def add_channel(channel_id: str, display_name: str):
    """Add or update a channel."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO channels (channel_id, display_name) VALUES (%s, %s) "
                "ON CONFLICT (channel_id) DO UPDATE SET display_name = EXCLUDED.display_name",
                (channel_id, display_name),
            )
        conn.commit()
    finally:
        return_conn(conn)


def video_exists(video_id: str) -> bool:
    """Check if video exists in database."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM videos WHERE video_id = %s", (video_id,))
            return cur.fetchone() is not None
    finally:
        return_conn(conn)


def get_latest_video_id_for_channel(channel_id: str) -> Optional[str]:
    """Get the most recent video ID for a channel (by published_at)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT video_id FROM videos WHERE channel_id = %s ORDER BY published_at DESC LIMIT 1",
                (channel_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        return_conn(conn)


def get_known_video_ids_for_channel(channel_id: str, limit: int = 50) -> List[str]:
    """Get known video IDs for a channel, newest first (for slice anchoring)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT video_id FROM videos WHERE channel_id = %s ORDER BY published_at DESC LIMIT %s",
                (channel_id, limit),
            )
            return [row[0] for row in cur.fetchall()]
    finally:
        return_conn(conn)


def add_video(
    video_id: str,
    channel_id: str,
    published_at: datetime,
    is_active: bool = True,
) -> bool:
    """Add a new video. Returns True if added, False if already exists.
    
    Args:
        is_active: If True, video is tracked (shown in dashboard, checked hourly).
                   If False, video is just a reference point for anchor detection.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO videos (video_id, channel_id, published_at, is_active) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (video_id) DO NOTHING",
                (video_id, channel_id, published_at, is_active),
            )
            added = cur.rowcount > 0
        conn.commit()
        return added
    finally:
        return_conn(conn)


def add_title_sample(video_id: str, title: str):
    """Add a title sample for a video."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO title_samples (video_id, title_text) VALUES (%s, %s)",
                (video_id, title),
            )
        conn.commit()
    finally:
        return_conn(conn)


def get_title_stats(video_id: str) -> List[Tuple[str, int]]:
    """Get title statistics: [(title, count), ...] ordered by count desc."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT title_text, COUNT(*) as count "
                "FROM title_samples WHERE video_id = %s "
                "GROUP BY title_text ORDER BY count DESC",
                (video_id,),
            )
            return [(r["title_text"], r["count"]) for r in cur.fetchall()]
    finally:
        return_conn(conn)


def get_recent_title_stats(video_id: str, days: int) -> List[Tuple[str, int]]:
    """Title counts over the last `days` days only: [(title, count), ...] desc.

    Used for the displayed A/B split so percentages reflect the experiment's
    CURRENT ratio rather than a lifetime average (the split shifts over time).
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT title_text, COUNT(*) as count "
                "FROM title_samples "
                "WHERE video_id = %s AND sampled_at >= NOW() - make_interval(days => %s) "
                "GROUP BY title_text ORDER BY count DESC",
                (video_id, days),
            )
            return [(r["title_text"], r["count"]) for r in cur.fetchall()]
    finally:
        return_conn(conn)


def get_title_daily_counts(video_id: str) -> List[dict]:
    """Per-day, per-title sample counts for the history timeline:
    [{day, title, count}, ...] ordered oldest day first."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT DATE(sampled_at) AS day, title_text AS title, COUNT(*) AS count "
                "FROM title_samples WHERE video_id = %s "
                "GROUP BY day, title_text ORDER BY day ASC",
                (video_id,),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        return_conn(conn)


def get_total_samples(video_id: str) -> int:
    """Get total number of samples for a video."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM title_samples WHERE video_id = %s",
                (video_id,),
            )
            return cur.fetchone()[0] or 0
    finally:
        return_conn(conn)


def get_comment_id(video_id: str) -> Optional[str]:
    """Get comment ID for a video."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT comment_id FROM videos WHERE video_id = %s AND comment_id IS NOT NULL",
                (video_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        return_conn(conn)


def set_comment_id(video_id: str, comment_id: str, status: str = None, text: str = None):
    """Set comment ID, status, and the rendered text for a video."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE videos SET comment_id = %s, comment_posted_at = CURRENT_TIMESTAMP, "
                "comment_status = %s, comment_text = %s "
                "WHERE video_id = %s",
                (comment_id, status, text, video_id),
            )
        conn.commit()
    finally:
        return_conn(conn)


def get_comment_state(video_id: str, refresh_hours: int) -> Optional[dict]:
    """Return {comment_id, comment_text, refresh_due} for the comment refresh logic.

    refresh_due is True when the last edit/post is older than refresh_hours, used
    to rate-limit percentage-only refreshes so we don't re-edit every hour.
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT comment_id, comment_text, "
                "  (COALESCE(comment_last_edited_at, comment_posted_at) IS NULL "
                "   OR COALESCE(comment_last_edited_at, comment_posted_at) "
                "      < NOW() - make_interval(hours => %s)) AS refresh_due "
                "FROM videos WHERE video_id = %s",
                (refresh_hours, video_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        return_conn(conn)


def update_comment_meta(video_id: str, status: str, likes: int, replies: int):
    """Refresh a comment's moderation status and engagement counts (likes/replies)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE videos SET comment_status = %s, comment_like_count = %s, "
                "comment_reply_count = %s WHERE video_id = %s",
                (status, likes, replies, video_id),
            )
        conn.commit()
    finally:
        return_conn(conn)


def update_comment_edited(video_id: str, text: str = None):
    """Mark comment as edited and store its new rendered text."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE videos SET comment_last_edited_at = CURRENT_TIMESTAMP, "
                "comment_text = COALESCE(%s, comment_text) WHERE video_id = %s",
                (text, video_id),
            )
        conn.commit()
    finally:
        return_conn(conn)


def mark_video_ignored(video_id: str):
    """Mark video as ignored (comment was deleted)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE videos SET is_ignored = TRUE WHERE video_id = %s",
                (video_id,),
            )
        conn.commit()
    finally:
        return_conn(conn)


def mark_video_deleted(video_id: str):
    """Mark video as deleted (not found in RSS feed)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE videos SET is_deleted = TRUE WHERE video_id = %s",
                (video_id,),
            )
        conn.commit()
    finally:
        return_conn(conn)


def mark_video_inactive(video_id: str):
    """Mark video as inactive (stagnated - same title for N days). Permanent."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE videos SET is_active = FALSE WHERE video_id = %s",
                (video_id,),
            )
        conn.commit()
    finally:
        return_conn(conn)


def update_last_checked(video_id: str):
    """Update last checked timestamp."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE videos SET last_checked_at = CURRENT_TIMESTAMP WHERE video_id = %s",
                (video_id,),
            )
        conn.commit()
    finally:
        return_conn(conn)


def get_active_videos() -> List[dict]:
    """Get all active videos for hourly checks.

    Returns every active (non-stagnated, non-ignored) video -- including ones
    without a comment yet -- so they keep accruing samples and can surface a 2nd
    variant (and earn their first comment) over time, not just at startup.
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT v.video_id, v.channel_id, c.display_name AS channel_name, "
                "       v.published_at, v.comment_id, v.comment_status "
                "FROM videos v JOIN channels c ON v.channel_id = c.channel_id "
                "WHERE v.is_active = TRUE AND v.is_ignored = FALSE AND v.is_deleted = FALSE "
                "ORDER BY v.published_at DESC"
            )
            return [dict(row) for row in cur.fetchall()]
    finally:
        return_conn(conn)


def get_title_history_by_date(video_id: str) -> List[Tuple[date, List[str]]]:
    """Get title history grouped by first_seen_date: [(date, [titles]), ...]."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT first_seen_date, title_text "
                "FROM title_history WHERE video_id = %s "
                "ORDER BY first_seen_date DESC, title_text",
                (video_id,),
            )
            rows = cur.fetchall()
            # Group by date
            history = {}
            for row_date, title in rows:
                if row_date not in history:
                    history[row_date] = []
                history[row_date].append(title)
            return sorted(history.items(), key=lambda x: x[0], reverse=True)
    finally:
        return_conn(conn)


def update_title_history(video_id: str, unique_titles: List[str], check_date: date):
    """Update title history: record which titles were seen on which dates."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for title in unique_titles:
                # Check if this title was already recorded for this date
                cur.execute(
                    "SELECT id FROM title_history "
                    "WHERE video_id = %s AND title_text = %s AND first_seen_date = %s",
                    (video_id, title, check_date),
                )
                if not cur.fetchone():
                    # New title for this date, or update last_seen_date
                    cur.execute(
                        "INSERT INTO title_history (video_id, title_text, first_seen_date, last_seen_date) "
                        "VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT (video_id, title_text, first_seen_date) "
                        "DO UPDATE SET last_seen_date = EXCLUDED.last_seen_date",
                        (video_id, title, check_date, check_date),
                    )
        conn.commit()
    finally:
        return_conn(conn)


def get_unique_titles_for_date(video_id: str, check_date: date) -> set:
    """Get unique titles seen on a specific date."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT title_text FROM title_samples "
                "WHERE video_id = %s AND DATE(sampled_at) = %s",
                (video_id, check_date),
            )
            return {row[0] for row in cur.fetchall()}
    finally:
        return_conn(conn)


def is_video_active(video_id: str, inactive_days: int) -> bool:
    """
    Check if video is active.
    Non-active if: same single title for N days straight.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Get distinct titles per day for last N days
            cur.execute(
                """
                SELECT DATE(sampled_at) as sample_date, COUNT(DISTINCT title_text) as title_count
                FROM title_samples
                WHERE video_id = %s
                  AND sampled_at >= CURRENT_DATE - make_interval(days => %s)
                GROUP BY DATE(sampled_at)
                ORDER BY sample_date DESC
                LIMIT %s
                """,
                (video_id, inactive_days, inactive_days),
            )
            rows = cur.fetchall()
            if len(rows) < inactive_days:
                return True  # Not enough data
            
            # Check if all last N days have exactly 1 title
            return not all(row[1] == 1 for row in rows)
    finally:
        return_conn(conn)


def get_video_info(video_id: str) -> Optional[dict]:
    """Get full video info for dashboard."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT v.*, c.display_name as channel_name,
                       COUNT(DISTINCT ts.title_text) as unique_titles,
                       COUNT(ts.id) as total_samples
                FROM videos v
                JOIN channels c ON v.channel_id = c.channel_id
                LEFT JOIN title_samples ts ON v.video_id = ts.video_id
                WHERE v.video_id = %s
                GROUP BY v.video_id, c.display_name
                """,
                (video_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        return_conn(conn)


def get_all_videos_summary() -> List[dict]:
    """Get summary of all videos for the dashboard.

    Includes a `variants` array per video: [{title, count, recent}, ...] ordered
    by all-time count, where `recent` is the count within the rolling window so
    the UI can show the current split (matching the comment).
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                WITH title_agg AS (
                    SELECT video_id, title_text,
                           COUNT(*) AS cnt,
                           COUNT(*) FILTER (
                               WHERE sampled_at >= NOW() - make_interval(days => %s)
                           ) AS recent_cnt
                    FROM title_samples
                    GROUP BY video_id, title_text
                )
                SELECT v.video_id, v.channel_id, c.display_name as channel_name,
                       v.published_at, v.is_ignored, v.is_deleted, v.is_active,
                       v.comment_id, v.comment_status, v.comment_posted_at, v.comment_last_edited_at,
                       v.comment_like_count, v.comment_reply_count,
                       v.last_checked_at,
                       COUNT(ta.title_text) as unique_titles,
                       COALESCE(SUM(ta.cnt), 0) as total_samples,
                       COALESCE(
                           json_agg(
                               json_build_object('title', ta.title_text,
                                                 'count', ta.cnt,
                                                 'recent', ta.recent_cnt)
                               ORDER BY ta.cnt DESC
                           ) FILTER (WHERE ta.title_text IS NOT NULL),
                           '[]'
                       ) as variants
                FROM videos v
                JOIN channels c ON v.channel_id = c.channel_id
                LEFT JOIN title_agg ta ON v.video_id = ta.video_id
                GROUP BY v.video_id, c.display_name
                ORDER BY v.published_at DESC
                """,
                (RATIO_WINDOW_DAYS,),
            )
            return [dict(row) for row in cur.fetchall()]
    finally:
        return_conn(conn)


def get_active_videos_for_dashboard() -> List[dict]:
    """Get tracked videos for dashboard (is_active=TRUE only)."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT v.video_id, v.channel_id, c.display_name as channel_name,
                       v.published_at, v.is_ignored, v.is_deleted, v.is_active,
                       v.comment_id, v.comment_posted_at, v.comment_last_edited_at,
                       v.last_checked_at,
                       COUNT(DISTINCT ts.title_text) as unique_titles,
                       COUNT(ts.id) as total_samples
                FROM videos v
                JOIN channels c ON v.channel_id = c.channel_id
                LEFT JOIN title_samples ts ON v.video_id = ts.video_id
                WHERE v.is_active = TRUE
                GROUP BY v.video_id, c.display_name
                ORDER BY v.published_at DESC
                """,
            )
            return [dict(row) for row in cur.fetchall()]
    finally:
        return_conn(conn)


def get_videos_without_comments() -> List[dict]:
    """Get active videos that have no comment_id (need reprocessing)."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT v.video_id, v.channel_id, c.display_name as channel_name, v.published_at
                FROM videos v
                JOIN channels c ON v.channel_id = c.channel_id
                WHERE v.is_active = TRUE 
                  AND v.is_ignored = FALSE 
                  AND v.is_deleted = FALSE 
                  AND v.comment_id IS NULL
                ORDER BY v.published_at DESC
                """
            )
            return [dict(row) for row in cur.fetchall()]
    finally:
        return_conn(conn)
