"""Flask API for dashboard - health and status tracking."""
import logging
from datetime import datetime, date
from flask import Flask, jsonify, send_file
from flask_cors import CORS

from storage import get_all_videos_summary, get_title_daily_counts, get_video_info, init_db

app = Flask(__name__)
CORS(app)  # Allow frontend to call from any origin

# Disable Flask request logging (clutters Railway logs)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)


@app.route("/", methods=["GET"])
def dashboard():
    """Serve the dashboard HTML."""
    return send_file("dashboard.html")


def serialize_value(value):
    """Convert datetime/date objects to ISO strings for JSON."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


@app.route("/api/reset", methods=["POST"])
def reset_database():
    """Clear all data from database. Use with caution!"""
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
        return jsonify({"status": "ok", "message": "Database cleared"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
        return jsonify({"error": str(e)}), 500


@app.route("/api/video/<video_id>", methods=["GET"])
def get_video(video_id: str):
    """Get detailed info for a specific video."""
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
        return jsonify({"error": str(e)}), 500


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
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=False)
