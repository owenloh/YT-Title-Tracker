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
- **Master switch** in the admin console to stop all tracking work while keeping
  the site online (see [Running cost & the master switch](#running-cost--the-master-switch))

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
storage.py           # PostgreSQL database operations + runtime settings (master switch)
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
| `NEW_VIDEO_CHECK_INTERVAL` | No | 1800 | Seconds between new video checks |
| `ACTIVE_VIDEO_CHECK_INTERVAL` | No | 21600 | Seconds between re-sampling sweeps |
| `SAMPLES_PER_RUN` | No | 15 | Title samples per video per sweep (cumulative across sweeps) |
| `FAST_SAMPLES` | No | 30 | Quick samples before posting the first comment |
| `SAMPLE_CONCURRENCY` | No | 4 | Max sampling requests in flight process-wide (caps peak memory) |
| `SCHEDULER_WORKERS` | No | 6 | Scheduler thread-pool size |
| `INACTIVE_DAYS_THRESHOLD` | No | 5 | Days of same title = finalized |
| `MAX_TRACK_DAYS` | No | 7 | Stop sampling a video once it's this old (0 = no cap) |
| `SKIP_COMMENT` | No | 0 | Set to 1 to disable commenting |
| `ADMIN_TOKEN` | No | — | Secret to authorize admin endpoints (e.g. `/api/reset`). Unset = admin endpoints disabled |
| `CORS_ORIGINS` | No | — | Comma-separated allowed origins for `/api/*`. Unset = same-origin only |
| `RATE_LIMIT_PER_MINUTE` | No | 240 | Max requests per client IP per minute |
| `RESET_RATE_LIMIT_PER_MINUTE` | No | 5 | Max `/api/reset` attempts per client IP per minute |

## Running cost & the master switch

The deployment is billed mostly on **resident memory**, around the clock —
measured CPU use is near zero, so it is peak memory, not how hard the sampler
works, that sets the bill. Peak memory comes from how many InnerTube responses
are being parsed *at the same instant*: each `/next` response is a multi-megabyte
JSON document that expands several-fold as Python objects, and neither CPython
nor glibc hands that memory back afterwards — the process keeps its high-water
mark until it restarts.

What keeps it down:

- `SAMPLE_CONCURRENCY` caps in-flight sampling requests process-wide (previously
  each video fanned out to its own pool of 8, with no global ceiling).
- `SCHEDULER_WORKERS` is small for the same reason.
- `MALLOC_ARENA_MAX=2` in the start command stops glibc fragmenting the heap
  across per-thread arenas, and `malloc_trim` runs after each sweep.
- `MAX_TRACK_DAYS` retires old videos, so the active set stops growing forever.
- The polling cadences are slow (30 min discovery, 6-hourly re-sampling). They
  were originally tuned to comment first; that is no longer the goal, and samples
  accumulate across sweeps, so a slower cadence loses coverage, not accuracy.

### Master switch

`/admin` has a single **Tracking** toggle at the top that stops *all* outbound
work — channel polling, title sampling, and comment posting/editing — while the
web process keeps serving the dashboard, so the site and its history stay online.

It is a **separate axis from the per-channel toggles** and never reads or writes
them: the channel selection survives a pause exactly as it was. Switching
tracking back on moves every channel's cutoff to today, so uploads published
during the pause are skipped rather than backfilled and commented on late.

The state lives in the `app_settings` table (key `operations_enabled`), so it
survives restarts and redeploys and takes effect within ~15 seconds without one.
Unset means ON, so an existing deployment is unaffected until the switch is used.

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

- `GET /api/admin/operations` - Master switch state
- `POST /api/admin/operations` - Start/stop all tracking: `{"enabled": false}`
- `GET /api/admin/channels`, `POST /api/admin/channels`,
  `POST /api/admin/channels/bulk`, `PATCH /api/admin/channels/<id>` - Channel management
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

0. Nothing below runs at all while the master switch is off
1. Checks RSS feeds every `NEW_VIDEO_CHECK_INTERVAL` (default 30 min) for new videos
2. New videos get `FAST_SAMPLES` quick samples (rotated identities); a comment is
   posted as soon as 2+ distinct titles have actually been observed
3. Re-sampling sweeps every `ACTIVE_VIDEO_CHECK_INTERVAL` (default 6 h) add
   `SAMPLES_PER_RUN` more samples per active video — samples are cumulative, so
   minority variants surface over hours
4. Comments are updated only when the visible title history actually changes (saves API quota)
5. Videos are retired after `INACTIVE_DAYS_THRESHOLD` days of the same single
   title, or once they pass `MAX_TRACK_DAYS` regardless

`YOUTUBE_CHANNELS` accepts either `@handle:Name` or a raw `UCxxxx...:Name` channel ID.

## Local Dev

```bash
pip install -r requirements.txt
python app.py  # Runs scheduler + dashboard on port 8080
```
