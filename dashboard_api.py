"""Flask API for dashboard - health and status tracking.

Security model
--------------
The dashboard at ``/`` and the read-only ``GET /api/*`` endpoints it calls are
public (they only expose already-public YouTube title data), so the site keeps
working with no credentials. Everything that can *change* state -- currently the
destructive ``POST /api/reset`` -- requires an admin token and is disabled unless
one is configured. On top of that every response carries hardening headers,
requests are rate limited per client, error bodies are generic (no internal
detail leaks), CORS is locked down by default, and request bodies are capped.
"""
import functools
import hmac
import logging
import os
import re
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, date

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

from config import parse_channels_str
from scraper import resolve_channel_id
from storage import (
    add_channel_admin,
    get_all_videos_summary,
    get_channels_with_metrics,
    get_title_daily_counts,
    get_video_info,
    init_db,
    set_channel_enabled,
)

app = Flask(__name__)

# Trust one layer of Railway's reverse proxy so request.remote_addr / scheme
# reflect the real client instead of the proxy. Keep this at 1 hop -- trusting
# more lets clients spoof X-Forwarded-For.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Cap request bodies (none of our endpoints need a payload) to blunt memory abuse.
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # 64 KB

# ---------------------------------------------------------------------------
# Configuration (from environment)
# ---------------------------------------------------------------------------

# Shared secret required to call state-changing/admin endpoints (e.g. /api/reset).
# If unset, those endpoints are DISABLED (fail closed) rather than left open.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()

# Cross-origin access. Empty (default) => same-origin only: the dashboard is
# served from this same host so it keeps working, while other sites can't read
# the API from a browser. Set to a comma-separated origin list, or "*", to open.
_cors_origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
if _cors_origins:
    CORS(app, resources={r"/api/*": {"origins": _cors_origins}})

# Per-client rate limits (requests per 60s window).
RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "240"))
RESET_RATE_LIMIT_PER_MINUTE = int(os.environ.get("RESET_RATE_LIMIT_PER_MINUTE", "5"))

# Plausible YouTube video id (validate path params before touching the DB).
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,20}$")

# Disable Flask request logging (clutters Railway logs)
logging.getLogger("werkzeug").setLevel(logging.ERROR)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rate limiting (in-process sliding window; app runs as a single process)
# ---------------------------------------------------------------------------
_rate_lock = threading.Lock()
_rate_buckets: dict[str, deque] = defaultdict(deque)


def _client_ip() -> str:
    """Best-effort client IP. ProxyFix has already normalized remote_addr from
    the trusted proxy's X-Forwarded-For, so prefer it."""
    return request.remote_addr or "unknown"


def _rate_limited(key: str, limit: int, window: int = 60) -> bool:
    """Record a hit for ``key`` and report whether it now exceeds ``limit``."""
    now = time.time()
    cutoff = now - window
    with _rate_lock:
        dq = _rate_buckets[key]
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= limit:
            return True
        dq.append(now)
        if not dq:
            _rate_buckets.pop(key, None)
        return False


@app.before_request
def _enforce_rate_limit():
    ip = _client_ip()
    # Tighter budget specifically for the destructive admin endpoint.
    if request.path == "/api/reset" and _rate_limited(
        f"reset:{ip}", RESET_RATE_LIMIT_PER_MINUTE
    ):
        return jsonify({"error": "rate limit exceeded"}), 429
    if _rate_limited(f"all:{ip}", RATE_LIMIT_PER_MINUTE):
        return jsonify({"error": "rate limit exceeded"}), 429
    return None


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------
@app.after_request
def _security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    # Railway serves over HTTPS; ask browsers to stick to it.
    resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # The dashboard uses inline <style>/<script> and loads YouTube thumbnails.
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' https://i.ytimg.com data:; "
        "connect-src 'self'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    )
    return resp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def require_admin(fn):
    """Guard state-changing endpoints with a shared admin token.

    Fails closed: if ADMIN_TOKEN isn't configured the endpoint is unavailable.
    Token may be sent as ``X-Admin-Token: <token>`` or ``Authorization: Bearer <token>``.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not ADMIN_TOKEN:
            logger.warning("Admin endpoint %s blocked: ADMIN_TOKEN not configured", request.path)
            return jsonify({"error": "admin endpoints are disabled"}), 503
        provided = request.headers.get("X-Admin-Token", "")
        if not provided:
            auth = request.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                provided = auth[7:].strip()
        # Constant-time compare to avoid leaking the token via timing.
        if not provided or not hmac.compare_digest(provided, ADMIN_TOKEN):
            logger.warning("Unauthorized admin attempt on %s from %s", request.path, _client_ip())
            return jsonify({"error": "unauthorized"}), 401
        return fn(*args, **kwargs)

    return wrapper


def serialize_value(value):
    """Convert datetime/date objects to ISO strings for JSON."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _server_error(exc):
    """Log the real error, return a generic message (no internal detail leak)."""
    logger.exception("Request to %s failed: %s", request.path, exc)
    return jsonify({"error": "internal server error"}), 500


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def dashboard():
    """Serve the dashboard HTML."""
    return send_file("dashboard.html")


@app.route("/admin", methods=["GET"])
def admin_page():
    """Serve the admin shell (channel management). The page itself is a static
    file with no data in it -- every API call it makes is gated by ADMIN_TOKEN,
    so serving the shell publicly leaks nothing."""
    return send_file("admin.html")


@app.route("/api/reset", methods=["POST"])
@require_admin
def reset_database():
    """Clear all data from database. Admin-only; use with caution!"""
    try:
        from storage import get_conn, get_pool
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM title_samples;")
        cur.execute("DELETE FROM title_history;")
        cur.execute("DELETE FROM videos;")
        cur.execute("DELETE FROM channels;")
        conn.commit()
        cur.close()
        get_pool().putconn(conn)
        logger.warning("Database reset performed by admin from %s", _client_ip())
        return jsonify({"status": "ok", "message": "Database cleared"})
    except Exception as e:
        return _server_error(e)


@app.route("/api/admin/channels", methods=["GET"])
@require_admin
def list_channels():
    """List all channels with metrics (videos tracked, comments posted,
    comments/month, avg likes+replies per comment) for the admin UI's
    include/exclude decision."""
    try:
        channels = get_channels_with_metrics()
        for ch in channels:
            for key, value in ch.items():
                ch[key] = serialize_value(value)
        return jsonify({"channels": channels})
    except Exception as e:
        return _server_error(e)


@app.route("/api/admin/channels", methods=["POST"])
@require_admin
def add_channel():
    """Add a single channel by @handle or channel ID.

    The identifier is stored AS GIVEN (the handle), exactly like the
    YOUTUBE_CHANNELS env seed does -- so adding a channel that's already tracked
    via env is a harmless no-op on the same primary key, never a duplicate row.
    We still resolve it once up front purely to validate it's a real channel and
    give immediate feedback on a typo. track_from_date defaults to today, so the
    channel only ever tracks videos published from today onward (no backfill)."""
    body = request.get_json(silent=True) or {}
    handle = str(body.get("handle") or "").strip()
    display_name = str(body.get("display_name") or "").strip() or handle
    if not handle:
        return jsonify({"error": "handle is required"}), 400

    try:
        if resolve_channel_id(handle, expected_name=display_name or None) is None:
            return jsonify({"error": f"could not resolve '{handle}' to a channel"}), 422

        created = add_channel_admin(handle, display_name)
        return jsonify({"status": "ok", "channel_id": handle, "created": created})
    except Exception as e:
        return _server_error(e)


@app.route("/api/admin/channels/bulk", methods=["POST"])
@require_admin
def add_channels_bulk():
    """Bulk-add channels from a comma-separated '@handle:Display Name' list
    (same format as the YOUTUBE_CHANNELS env var).

    Identifiers are stored as given (handles), matching the env seed, so this is
    just a fast batch of idempotent inserts -- already-tracked channels collide
    on their primary key and are left untouched (history preserved), new ones get
    track_from_date = today. No per-channel network calls, so it returns quickly;
    each new channel is picked up on the next new-video poll."""
    body = request.get_json(silent=True) or {}
    raw = str(body.get("list") or "")
    entries = parse_channels_str(raw)
    if not entries:
        return jsonify({"error": "no channels found in list"}), 400

    added = 0
    try:
        for handle, display_name in entries:
            if add_channel_admin(handle, display_name):
                added += 1
        return jsonify({"status": "ok", "submitted": len(entries), "new": added})
    except Exception as e:
        return _server_error(e)


# channel_id here is the stored identifier, which is normally an @handle (e.g.
# "@MrBeast") or a raw UC id -- so the '@' and '.' that handles allow must pass.
_CHANNEL_ID_ADMIN_RE = re.compile(r"^@?[A-Za-z0-9_.-]{1,100}$")


@app.route("/api/admin/channels/<channel_id>", methods=["PATCH"])
@require_admin
def update_channel(channel_id: str):
    """Enable or disable a channel. Enabling (incl. resuming after a pause) bumps
    the channel's track_from_date to today via set_channel_enabled, so its whole
    pause-window backlog is skipped by the date gate and only uploads from the
    resume day onward are ever tracked -- no backfill."""
    if not _CHANNEL_ID_ADMIN_RE.match(channel_id):
        return jsonify({"error": "invalid channel id"}), 400
    body = request.get_json(silent=True) or {}
    if "enabled" not in body:
        return jsonify({"error": "enabled is required"}), 400
    enabled = bool(body["enabled"])

    try:
        set_channel_enabled(channel_id, enabled)
        return jsonify({"status": "ok"})
    except Exception as e:
        return _server_error(e)


@app.route("/api/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})


@app.route("/api/videos", methods=["GET"])
def get_videos():
    """Get all videos (dashboard will filter by is_active)."""
    try:
        videos = get_all_videos_summary()
        for video in videos:
            for key, value in video.items():
                video[key] = serialize_value(value)
        return jsonify({"videos": videos})
    except Exception as e:
        return _server_error(e)


@app.route("/api/video/<video_id>", methods=["GET"])
def get_video(video_id: str):
    """Get detailed info for a specific video."""
    if not _VIDEO_ID_RE.match(video_id):
        return jsonify({"error": "invalid video id"}), 400
    try:
        video = get_video_info(video_id)
        if not video:
            return jsonify({"error": "Video not found"}), 404

        # Convert datetime/date objects
        for key, value in video.items():
            video[key] = serialize_value(value)

        # Per-day title breakdown for the history timeline.
        timeline = get_title_daily_counts(video_id)
        for row in timeline:
            row["day"] = serialize_value(row["day"])

        return jsonify({"video": video, "timeline": timeline})
    except Exception as e:
        return _server_error(e)


@app.route("/api/stats", methods=["GET"])
def get_stats():
    """Get overall statistics."""
    try:
        all_videos = get_all_videos_summary()

        # Active = is_active=TRUE (being tracked, shown in dashboard)
        # Inactive = is_active=FALSE (reference points / stagnated)
        active = [v for v in all_videos if v.get("is_active")]
        inactive = [v for v in all_videos if not v.get("is_active")]

        from config import RATIO_WINDOW_DAYS
        return jsonify({
            "active_videos": len(active),
            "with_comments": sum(1 for v in active if v.get("comment_id")),
            "multi_title": sum(1 for v in active if (v.get("unique_titles") or 0) >= 2),
            "inactive_videos": len(inactive),
            "total_in_db": len(all_videos),
            "ratio_window_days": RATIO_WINDOW_DAYS,
        })
    except Exception as e:
        return _server_error(e)


# ---------------------------------------------------------------------------
# Generic JSON error handlers (avoid leaking stack traces / HTML)
# ---------------------------------------------------------------------------
@app.errorhandler(404)
def _not_found(_e):
    return jsonify({"error": "not found"}), 404


@app.errorhandler(405)
def _method_not_allowed(_e):
    return jsonify({"error": "method not allowed"}), 405


@app.errorhandler(413)
def _too_large(_e):
    return jsonify({"error": "request too large"}), 413


@app.errorhandler(500)
def _internal(_e):
    return jsonify({"error": "internal server error"}), 500


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=False)
