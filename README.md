# YouTube Title A/B Test Compiler

Tracks YouTube title A/B tests across multiple channels. Samples each video's title
from many rotated viewer identities, detects changes over time, and posts comments
with the historical title data.

## Features

- Monitors 18+ YouTube channels for new videos
- Samples titles via YouTube's InnerTube API, rotating a fresh viewer identity per
  sample to surface different A/B variants (see [How variant detection works](#how-variant-detection-works))
- Posts timestamped comments showing title history
- Dashboard to view all tracked videos
- Auto-detects when titles stabilize (marks inactive after 5 days)
- Skips Shorts automatically

## How variant detection works

YouTube assigns each viewer a **sticky** title variant keyed on their visitor
identity (`visitorData`) — it does not pick a random one per page load. So fetching
the same watch URL repeatedly from one identity returns the *same* title every time.

To see different variants this tool talks to YouTube's internal **InnerTube** API
(the JSON API the site itself uses) and, for each sample, rotates a fresh visitor
identity + client surface so every request looks like a different viewer. Titles are
read from structured JSON (the watch-page `videoPrimaryInfoRenderer`, with the player
response as a backstop) rather than fragile HTML scraping, which is far more reliable
from datacenter IPs.

**Limitation:** no external tool can guarantee capturing *every* variant, because
YouTube also buckets experiments partly by IP. Rotating identities + sampling over
time gives the best achievable coverage from a single host.

Validate it against a real video (run where YouTube is reachable):

```bash
python youtube_innertube.py <video_id> 15   # samples 15x, prints distinct titles
python test_logic.py                        # unit tests for the pure logic
```

## File Structure

```
app.py               # Entry point (Railway)
main.py              # Scheduler + video processing
storage.py           # PostgreSQL database operations
scraper.py           # Video discovery (RSS) + title fetching
youtube_innertube.py # InnerTube client: identity-rotating title sampler
youtube_comment.py   # YouTube API for comments
config.py            # Environment settings
dashboard_api.py     # Flask API endpoints
dashboard.html       # Web dashboard UI (dark "instrument" theme + anime.js motion)
admin.html           # Channel management UI (same theme)
static/              # Self-hosted front-end assets (served at /static by Flask)
  anime.min.js       #   anime.js (vendored, MIT) — powers count-ups, bar fills, reveals
  fonts/             #   Space Grotesk + IBM Plex Mono woff2 (vendored, OFL)
get_refresh_token.py # OAuth setup helper
test_logic.py        # Unit tests for the pure (no network/DB) logic
```

The front end is fully self-hosted: anime.js and the webfonts live under `static/`
so nothing loads from a CDN, which keeps the strict `Content-Security-Policy`
(`script-src 'self'`, `default-src 'self'`) intact — no external runtime deps.

## Setup

### 1. Google OAuth (one-time)

1. [Google Cloud Console](https://console.cloud.google.com/) → APIs & Services → Credentials
2. Create OAuth 2.0 Client ID (Desktop app)
3. Enable YouTube Data API v3
4. Add your email as test user in OAuth consent screen

### 2. Get Refresh Token (local)

```bash
# .env
YOUTUBE_CLIENT_ID=your_client_id
YOUTUBE_CLIENT_SECRET=your_client_secret

python get_refresh_token.py
# Copy the printed YOUTUBE_REFRESH_TOKEN
```

### 3. Deploy to Railway

Add PostgreSQL service, connect repo, set env vars:

```env
DATABASE_URL=<from Railway PostgreSQL>
YOUTUBE_CLIENT_ID=<from step 1>
YOUTUBE_CLIENT_SECRET=<from step 1>
YOUTUBE_REFRESH_TOKEN=<from step 2>
YOUTUBE_CHANNELS=@veritasium:Veritasium,@kurzgesagt:Kurzgesagt,@MrBeast:MrBeast
CUTOFF_DATE=2026-02-07
```

Dashboard available at your Railway public URL.

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DATABASE_URL` | Yes | — | PostgreSQL connection string |
| `YOUTUBE_CLIENT_ID` | Yes | — | OAuth client ID |
| `YOUTUBE_CLIENT_SECRET` | Yes | — | OAuth client secret |
| `YOUTUBE_REFRESH_TOKEN` | Yes | — | From `get_refresh_token.py` |
| `YOUTUBE_CHANNELS` | No | Veritasium | `@handle:name,@handle:name` format |
| `CUTOFF_DATE` | No | 2026-02-08 | Only process videos after this date |
| `NEW_VIDEO_CHECK_INTERVAL` | No | 180 | Seconds between new video checks |
| `ACTIVE_VIDEO_CHECK_INTERVAL` | No | 3600 | Seconds between active video checks |
| `SAMPLES_PER_RUN` | No | 21 | Total title samples per video |
| `FAST_SAMPLES` | No | 5 | Quick samples before posting comment |
| `INACTIVE_DAYS_THRESHOLD` | No | 5 | Days of same title = finalized |
| `SKIP_COMMENT` | No | 0 | Set to 1 to disable commenting |
| `ADMIN_TOKEN` | No | — | Secret to authorize admin endpoints (e.g. `/api/reset`). Unset = admin endpoints disabled |
| `CORS_ORIGINS` | No | — | Comma-separated allowed origins for `/api/*`. Unset = same-origin only |
| `RATE_LIMIT_PER_MINUTE` | No | 240 | Max requests per client IP per minute |
| `RESET_RATE_LIMIT_PER_MINUTE` | No | 5 | Max `/api/reset` attempts per client IP per minute |

## Comment Format

Comments show title history by date:

```
I noticed YouTube is testing different titles on this video

Feb 07: Original Title
Feb 08: Original Title | New Test Title
Feb 09: New Test Title | Another Variant | Third Option
```

## API Endpoints

Public (read-only — these power the dashboard website):

- `GET /` - Dashboard
- `GET /api/videos` - All videos with stats
- `GET /api/video/<id>` - One video + title timeline
- `GET /api/stats` - Summary counts
- `GET /api/health` - Health check

Admin (requires the `ADMIN_TOKEN` secret):

- `POST /api/reset` - Clear database. Send the token as `X-Admin-Token: <token>`
  or `Authorization: Bearer <token>`. **Disabled** (returns 503) when
  `ADMIN_TOKEN` is unset, so it can never be triggered anonymously.

  ```bash
  curl -X POST https://your-app.up.railway.app/api/reset \
       -H "X-Admin-Token: $ADMIN_TOKEN"
  ```

### HTTP hardening

All endpoints share these protections (the public site keeps working unchanged):

- State-changing endpoints require `ADMIN_TOKEN` (constant-time compared); they
  fail closed when no token is configured.
- Security headers on every response: `Content-Security-Policy`,
  `Strict-Transport-Security`, `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`.
- Per-client rate limiting (`RATE_LIMIT_PER_MINUTE`, tighter on `/api/reset`).
- Error responses are generic — internal exceptions are logged, never returned.
- CORS is same-origin only unless `CORS_ORIGINS` is set.
- Path params validated; request bodies capped at 64 KB; runs behind Railway's
  proxy via `ProxyFix` so rate limits see the real client IP.

## How It Works

1. Checks RSS feeds every 3 minutes for new videos
2. New videos get `FAST_SAMPLES` quick samples (rotated identities) and a comment is posted immediately
3. The rest of `SAMPLES_PER_RUN` samples are collected in the background
4. Hourly checks re-sample active videos to detect new title variants
5. Comments are updated only when the visible title history actually changes (saves API quota)
6. Videos marked inactive after `INACTIVE_DAYS_THRESHOLD` days of the same single title

`YOUTUBE_CHANNELS` accepts either `@handle:Name` or a raw `UCxxxx...:Name` channel ID.

## Local Dev

```bash
pip install -r requirements.txt
python app.py  # Runs scheduler + dashboard on port 8080
```
