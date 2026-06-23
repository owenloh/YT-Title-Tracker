"""
YouTube InnerTube client for A/B title-variant detection.

Why this module exists
----------------------
YouTube serves a *sticky* title variant per viewer identity (the `visitorData`
token), not a random one per request. So fetching the same watch URL repeatedly
from the same identity returns the *same* title every time -- which is why the
old "scrape the page 21 times" approach almost never saw more than one variant.

This module talks to YouTube's internal "InnerTube" API (the same JSON API the
website and apps use) and does two things the old scraper could not:

1. Reads titles from structured JSON instead of fragile HTML regex, which is far
   more reliable from datacenter IPs (fewer consent/bot walls, no 403 HTML).
2. Sends each request cookieless, so YouTube assigns it a fresh `visitorData`
   automatically -- i.e. every sample is already a *different viewer* in an
   independent experiment bucket, with no identity bookkeeping on our side.
   (Verified empirically: N cookieless requests come back with N distinct
   server-assigned identities, sequentially and in parallel.) We also rotate the
   client surface (WEB/MWEB) to widen coverage a little further.

What actually limits coverage is the NUMBER of samples, not identity. A/B splits
are long-tailed in practice -- e.g. a real video measured here served
94%/4%/2% across three titles, and the minority variants did not appear until
samples ~32 and ~48. So catching them is a matter of sampling enough times
(cumulatively, across the hourly re-sampling runs), not of "rotating identities".

Honest limitation: no external tool can guarantee seeing every variant, because
YouTube also buckets experiments partly by IP, and a single host sees only its
own IP's slice. More samples over time gives the best achievable coverage.

Quick check (run where YouTube is reachable; use a high count -- minority
variants are rare, so ~50 samples is the floor for a skewed split):

    python youtube_innertube.py dQw4w9WgXcQ 50
"""
import html as _html
import json
import random
import re as _re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Set

import requests

_INNERTUBE_URL = "https://www.youtube.com/youtubei/v1/{endpoint}?prettyPrint=false"
# Public InnerTube key used by the YouTube web client. Not a secret -- it ships
# in every youtube.com page's ytcfg.
_INNERTUBE_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"

# Client "surfaces". YouTube buckets different surfaces into experiments
# differently, so rotating them widens variant coverage. hl/gl are kept fixed at
# en/US on purpose: we want A/B variants, not localized titles.
_CLIENTS = {
    "WEB": {
        "clientName": "WEB",
        "clientVersion": "2.20240726.00.00",
        "clientNameId": "1",
        "userAgent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
    },
    "MWEB": {
        "clientName": "MWEB",
        "clientVersion": "2.20240726.01.00",
        "clientNameId": "2",
        "userAgent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
        ),
    },
}
_CLIENT_KEYS = list(_CLIENTS.keys())


# --------------------------------------------------------------------------- #
# Pure helpers (no network -- unit tested in test_logic.py)
# --------------------------------------------------------------------------- #
def normalize_title(text: str) -> str:
    """Unescape HTML entities and collapse whitespace so variants compare cleanly."""
    return _re.sub(r"\s+", " ", _html.unescape(text or "")).strip()


def _find_all(obj, key: str) -> List:
    """Recursively collect every value stored under `key` anywhere in the JSON."""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                found.append(v)
            else:
                found.extend(_find_all(v, key))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_find_all(item, key))
    return found


def _text_of(node) -> Optional[str]:
    """Extract display text from a YouTube text node ({simpleText} or {runs:[...]})."""
    if not isinstance(node, dict):
        return None
    if isinstance(node.get("simpleText"), str):
        return node["simpleText"]
    runs = node.get("runs")
    if isinstance(runs, list):
        text = "".join(r.get("text", "") for r in runs if isinstance(r, dict))
        return text or None
    return None


def extract_titles_from_next(data: dict) -> Set[str]:
    """Titles shown on the watch page header -- the strongest A/B-variant signal."""
    titles = set()
    for renderer in _find_all(data, "videoPrimaryInfoRenderer"):
        if isinstance(renderer, dict):
            text = _text_of(renderer.get("title"))
            if text:
                titles.add(normalize_title(text))
    return titles


def extract_titles_from_player(data: dict) -> Set[str]:
    """Title from the player response (reliable, sometimes the canonical title)."""
    titles = set()
    details = data.get("videoDetails")
    if isinstance(details, dict) and isinstance(details.get("title"), str):
        titles.add(normalize_title(details["title"]))
    for mf in _find_all(data, "playerMicroformatRenderer"):
        if isinstance(mf, dict):
            text = _text_of(mf.get("title"))
            if text:
                titles.add(normalize_title(text))
    return titles


# --------------------------------------------------------------------------- #
# Network
# --------------------------------------------------------------------------- #
def _context(client_key: str) -> dict:
    c = _CLIENTS[client_key]
    return {"client": {
        "clientName": c["clientName"],
        "clientVersion": c["clientVersion"],
        "hl": "en",
        "gl": "US",
    }}


def _headers(client_key: str) -> dict:
    c = _CLIENTS[client_key]
    return {
        "User-Agent": c["userAgent"],
        "Content-Type": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.youtube.com",
        "X-Youtube-Client-Name": c["clientNameId"],
        "X-Youtube-Client-Version": c["clientVersion"],
    }


def _post(endpoint: str, video_id: str, client_key: str,
          timeout: float = 15.0) -> Optional[dict]:
    """One InnerTube POST. Returns parsed JSON or None on any failure.

    Sent cookieless and with no visitorData, so YouTube mints a fresh viewer
    identity for this request -- that is what puts each sample in an independent
    experiment bucket.
    """
    url = _INNERTUBE_URL.format(endpoint=endpoint) + f"&key={_INNERTUBE_KEY}"
    payload = {"context": _context(client_key), "videoId": video_id}
    try:
        r = requests.post(url, headers=_headers(client_key),
                          data=json.dumps(payload), timeout=timeout)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _sample_once(video_id: str, client_key: str) -> Set[str]:
    """One fresh viewer's view of the title(s).

    The request carries no visitor identity, so YouTube assigns a new viewer to
    it -- that is what lands each sample in an independent experiment bucket.
    """
    titles: Set[str] = set()

    # /next carries the displayed watch-page title (the A/B variant the viewer sees).
    nxt = _post("next", video_id, client_key)
    if nxt:
        titles |= extract_titles_from_next(nxt)

    # /player is a reliable backstop and a second experiment surface.
    if not titles:
        ply = _post("player", video_id, client_key)
        if ply:
            titles |= extract_titles_from_player(ply)

    return titles


def fetch_one_title(video_id: str) -> Optional[str]:
    """Single best-effort title fetch (no rotation). Used for quick lookups."""
    titles = _sample_once(video_id, "WEB")
    if titles:
        return sorted(titles, key=len, reverse=True)[0]
    return None


def sample_variant_titles(video_id: str, samples: int = 40, delay: float = 0.8,
                          jitter: float = 0.6, parallel: bool = False) -> List[str]:
    """
    Sample a video's title `samples` times, each as a fresh viewer, and return the
    flat list of observed titles (one or more per sample).

    Each request is cookieless, so YouTube assigns it a new viewer and buckets it
    into experiments independently; the client surface is rotated too. Because A/B
    splits are skewed, the count of observed variants grows with `samples` -- a
    rare (~2-4%) variant typically needs dozens of samples to appear, so callers
    should sample generously (and rely on cumulative re-sampling over time).
    `parallel=True` fires requests concurrently for the fast first comment.
    """
    if samples <= 0:
        return []

    if parallel:
        observed: List[str] = []
        with ThreadPoolExecutor(max_workers=min(samples, 8)) as ex:
            futures = [
                ex.submit(_sample_once, video_id, _CLIENT_KEYS[i % len(_CLIENT_KEYS)])
                for i in range(samples)
            ]
            for f in futures:
                try:
                    observed.extend(f.result())
                except Exception:
                    pass
        return observed

    observed = []
    for i in range(samples):
        observed.extend(_sample_once(video_id, _CLIENT_KEYS[i % len(_CLIENT_KEYS)]))
        if i < samples - 1:
            time.sleep(delay + random.random() * jitter)
    return observed


if __name__ == "__main__":
    import sys

    vid = sys.argv[1] if len(sys.argv) > 1 else "dQw4w9WgXcQ"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 50
    print(f"Sampling {vid} x{n} (fresh viewer per sample)...")
    counts = Counter(sample_variant_titles(vid, samples=n))
    if not counts:
        print("No titles returned -- is YouTube reachable from this host?")
    else:
        print(f"\n{len(counts)} distinct title(s) seen:")
        for title, count in counts.most_common():
            print(f"  {count:>2}x  {title}")
