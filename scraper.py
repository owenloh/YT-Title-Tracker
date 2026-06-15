"""Fetch videos from channel using RSS (primary) with channel page scrape fallback."""
import re
import time
from datetime import datetime
from typing import List, Optional, Tuple
import xml.etree.ElementTree as ET
import requests

from youtube_comment import get_credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import youtube_innertube

RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
WATCH_URL = "https://www.youtube.com/watch?v={video_id}"
SHORTS_URL = "https://www.youtube.com/shorts/{video_id}"

_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")


def _looks_like_channel_id(value: str) -> bool:
    """True for a raw channel ID (UCxxxx...), False for an @handle or name."""
    return bool(_CHANNEL_ID_RE.match(value.strip()))

# Browser-like so we might get the same A/B variant as users
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Cache for handle -> channel_id resolution
_handle_to_channel_id_cache = {}


def _resolve_handle_to_channel_id(handle: str, expected_name: str = None) -> Optional[str]:
    """
    Resolve @handle to channel ID using externalId (most reliable).
    
    Args:
        handle: The @ handle (e.g., '@veritasium', '@MrBeast')
        expected_name: Optional channel name to validate against
    
    Returns:
        Channel ID (UCxxx) or None if failed/invalid
    """
    # Normalize handle
    if not handle.startswith('@'):
        handle = '@' + handle
    
    if handle in _handle_to_channel_id_cache:
        return _handle_to_channel_id_cache[handle]
    
    url = f"https://www.youtube.com/{handle}"
    
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        html = r.text
        
        # Use externalId - most reliable channel ID extraction
        match = re.search(r'"externalId":"(UC[a-zA-Z0-9_-]{22})"', html)
        
        if match:
            channel_id = match.group(1)
            
            # Validate channel name if provided
            if expected_name:
                og_match = re.search(r'<meta property="og:title" content="([^"]+)"', html)
                if og_match:
                    found_name = og_match.group(1)
                    # Loose match
                    if expected_name.lower() not in found_name.lower() and found_name.lower() not in expected_name.lower():
                        print(f"WARNING: Channel name mismatch for {handle}: expected '{expected_name}', found '{found_name}'")
            
            _handle_to_channel_id_cache[handle] = channel_id
            return channel_id
            
    except Exception as e:
        print(f"Failed to resolve handle {handle}: {e}")
    
    return None


def _get_videos_from_rss(channel_id: str, max_videos: int = 50) -> List[Tuple[str, datetime]]:
    """Try RSS feed first (free, no quota)."""
    url = RSS_URL.format(channel_id=channel_id)
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    
    ns = {"yt": "http://www.youtube.com/xml/schemas/2015", "atom": "http://www.w3.org/2005/Atom"}
    entries = root.findall(".//atom:entry", namespaces=ns)
    if not entries:
        entries = root.findall(".//{http://www.w3.org/2005/Atom}entry")
    
    videos = []
    for entry in entries[:max_videos]:
        video_id_elem = entry.find(".//yt:videoId", namespaces=ns)
        if video_id_elem is None:
            video_id_elem = entry.find(".//{http://www.youtube.com/xml/schemas/2015}videoId")
        if video_id_elem is None:
            continue
        
        video_id = (video_id_elem.text or "").strip()
        if not video_id:
            continue
        
        published_elem = entry.find(".//atom:published", namespaces=ns)
        if published_elem is None:
            published_elem = entry.find(".//{http://www.w3.org/2005/Atom}published")
        
        if published_elem is not None and published_elem.text:
            try:
                published_str = published_elem.text.strip()
                published_at = datetime.fromisoformat(published_str.replace("Z", "+00:00"))
                videos.append((video_id, published_at))
            except (ValueError, AttributeError):
                continue
    
    return videos


def _get_videos_from_api(channel_id: str, max_videos: int = 50) -> List[Tuple[str, datetime]]:
    """Fallback to YouTube Data API (costs quota)."""
    creds = get_credentials()
    if not creds:
        return []
    
    youtube = build("youtube", "v3", credentials=creds)
    
    # Uploads playlist ID = "UU" + channel_id without "UC" prefix
    uploads_playlist_id = "UU" + channel_id[2:]
    
    videos = []
    next_page_token = None
    
    while len(videos) < max_videos:
        request = youtube.playlistItems().list(
            part="snippet",
            playlistId=uploads_playlist_id,
            maxResults=min(50, max_videos - len(videos)),
            pageToken=next_page_token
        )
        response = request.execute()
        
        for item in response.get("items", []):
            snippet = item.get("snippet", {})
            video_id = snippet.get("resourceId", {}).get("videoId")
            published_str = snippet.get("publishedAt")
            
            if video_id and published_str:
                try:
                    published_at = datetime.fromisoformat(published_str.replace("Z", "+00:00"))
                    videos.append((video_id, published_at))
                except (ValueError, AttributeError):
                    continue
        
        next_page_token = response.get("nextPageToken")
        if not next_page_token:
            break
    
    return videos


def _get_videos_from_channel_page(handle: str, max_videos: int = 15) -> List[Tuple[str, datetime]]:
    """Scrape channel page directly using @handle/videos URL.
    
    NOTE: This method doesn't get publish dates, so we return None for dates.
    The caller must handle this - only use for detecting NEW videos by comparing
    against known IDs, not for date filtering.
    """
    # Normalize handle
    if not handle.startswith('@'):
        handle = '@' + handle
    
    url = f"https://www.youtube.com/{handle}/videos"
    
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        html = r.text
        
        # Validate we're on a channel page using externalId
        if '"externalId":"UC' not in html:
            print(f"WARNING: {handle}/videos did not resolve to a channel")
            return []
        
        # Extract video IDs from the page
        video_ids = re.findall(r'"videoId":"([a-zA-Z0-9_-]{11})"', html)
        # Dedupe while preserving order
        seen = set()
        unique_ids = []
        for vid in video_ids:
            if vid not in seen:
                seen.add(vid)
                unique_ids.append(vid)
                if len(unique_ids) >= max_videos:
                    break
        
        return [(vid, None) for vid in unique_ids]
    
    except Exception as e:
        print(f"Channel page scrape failed for {handle}: {e}")
        return []


def get_videos_from_rss(handle: str, expected_name: str = None, max_videos: int = 50) -> List[Tuple[str, datetime]]:
    """
    Get videos from channel using @handle format.
    
    Resolves handle to channel ID via externalId, then tries RSS (has dates).
    Falls back to channel page scrape if RSS fails.
    
    Args:
        handle: @ handle (e.g., '@veritasium', '@MrBeast')
        expected_name: Optional channel name for validation
        max_videos: Max videos to return
    
    Returns:
        [(video_id, published_at), ...] ordered newest first.
        published_at may be None if from channel page scrape.
    """
    # Accept either a raw channel ID (UCxxx...) or an @handle. The old code blindly
    # prepended '@' to everything, which turned a configured UC id into '@UCxxx'
    # and never resolved -- the cause of the broken default config.
    if _looks_like_channel_id(handle):
        channel_id = handle.strip()
    else:
        if not handle.startswith('@'):
            handle = '@' + handle
        channel_id = _resolve_handle_to_channel_id(handle, expected_name)
    
    if not channel_id:
        print(f"Could not resolve {handle} to channel ID, trying channel page directly...")
        videos = _get_videos_from_channel_page(handle, max_videos)
        if videos:
            print(f"Channel page scrape succeeded for {handle}")
            return videos
        print(f"All methods failed for {handle}")
        return []
    
    # Try RSS first (free, has publish dates)
    try:
        videos = _get_videos_from_rss(channel_id, max_videos)
        if videos:
            return videos
    except Exception as e:
        print(f"RSS failed for {handle} ({channel_id}): {e}, trying channel page...")
    
    # Fallback: scrape channel page (free, no publish dates)
    videos = _get_videos_from_channel_page(handle, max_videos)
    if videos:
        print(f"Channel page scrape succeeded for {handle}")
        return videos
    
    print(f"All methods failed for {handle}")
    return []


def is_short(video_id: str) -> bool:
    """
    Check if a video is a YouTube Short (efficient: tries lightest methods first).
    RSS feed doesn't tell us, so we need to check the video itself.
    """
    try:
        # Method 1: Try HEAD request to shorts URL (lightest check)
        # If video is a Short, /shorts/{id} stays as /shorts/{id}
        # If video is long-form, /shorts/{id} redirects to /watch?v={id}
        shorts_url = SHORTS_URL.format(video_id=video_id)
        r_head = requests.head(shorts_url, headers=HEADERS, timeout=5, allow_redirects=True)
        if r_head.ok:
            # Check if FINAL URL (after redirects) still contains /shorts/
            # Long-form videos redirect to /watch?v=, Shorts stay at /shorts/
            if '/shorts/' in r_head.url.lower():
                return True
        
        # Method 2: Check og:url meta tag from watch page (partial fetch, often cached)
        # We only need to check the <head> section, but requests.get gets full page
        # Still more efficient than checking full HTML content
        url = WATCH_URL.format(video_id=video_id)
        r = requests.get(url, headers=HEADERS, timeout=10, stream=True)
        if r.ok:
            # Read first 50KB to get <head> section with og:url
            content = b""
            for chunk in r.iter_content(chunk_size=8192):
                content += chunk
                if len(content) > 50000:  # Stop after 50KB (enough for <head>)
                    break
            
            html = content.decode('utf-8', errors='ignore').lower()
            
            # Check og:url meta tag first (most reliable indicator)
            og_url_match = re.search(
                r'<meta\s+property=["\']og:url["\']\s+content=["\']([^"\']+)["\']',
                html,
                re.IGNORECASE
            )
            if og_url_match and '/shorts/' in og_url_match.group(1).lower():
                return True
            
            # Check for shorts indicators in the partial HTML
            if '"isShorts":true' in html or '"isShort":true' in html:
                return True
        
    except Exception:
        pass
    
    return False


def _parse_title_from_html(html: str) -> Optional[str]:
    """Try several patterns to get title from watch page HTML."""
    import html as html_lib
    # 1. og:title (double-quoted)
    m = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html)
    if m:
        return html_lib.unescape(m.group(1).strip())
    # 2. og:title (content first, or single quotes)
    m = re.search(r'<meta\s+content="([^"]+)"\s+property="og:title"', html)
    if m:
        return html_lib.unescape(m.group(1).strip())
    m = re.search(r"<meta\s+property=['\"]og:title['\"]\s+content=['\"]([^'\"]+)['\"]", html)
    if m:
        return html_lib.unescape(m.group(1).strip())
    # 3. <title>... - YouTube</title>
    m = re.search(r"<title>([^<]+)\s*-\s*YouTube</title>", html, re.IGNORECASE | re.DOTALL)
    if m:
        return html_lib.unescape(m.group(1).strip())
    # 4. Embedded JSON: "runs":[{"text":"Title here"}] (videoPrimaryInfoRenderer)
    m = re.search(r'"runs":\s*\[\s*\{\s*"text":\s*"((?:[^"\\]|\\.)*)"', html)
    if m:
        return html_lib.unescape(m.group(1).strip())
    # 5. "simpleText":"Title"
    m = re.search(r'"simpleText":\s*"((?:[^"\\]|\\.)*)"', html)
    if m:
        return html_lib.unescape(m.group(1).strip())
    return None


def _scrape_title_via_html(video_id: str) -> Optional[str]:
    """Last-resort title fetch via watch-page HTML, then noembed."""
    url = WATCH_URL.format(video_id=video_id)
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        title = _parse_title_from_html(r.text)
        if title:
            return title
    except Exception:
        pass
    try:
        noembed_url = f"https://noembed.com/embed?url=https://www.youtube.com/watch?v={video_id}"
        r = requests.get(noembed_url, headers=HEADERS, timeout=10)
        if r.ok:
            data = r.json()
            if isinstance(data.get("title"), str):
                return data["title"].strip()
    except Exception:
        pass
    return None


def get_video_title(video_id: str) -> Optional[str]:
    """Best-effort single title fetch: InnerTube JSON first, then HTML/noembed."""
    title = youtube_innertube.fetch_one_title(video_id)
    if title:
        return title
    return _scrape_title_via_html(video_id)


def sample_titles(video_id: str, count: int, delay: float = 0.8, parallel: bool = False) -> List[str]:
    """Sample a video's title `count` times to catch A/B variants.

    Uses the InnerTube engine, which rotates a fresh viewer identity per sample so
    different requests can land in different experiment buckets. Returns the flat
    list of observed titles (may contain duplicates; callers dedupe as needed).

    parallel=True fires requests concurrently for a fast first comment.
    """
    titles = youtube_innertube.sample_variant_titles(
        video_id, samples=count, delay=delay, parallel=parallel
    )
    if titles:
        return titles

    # InnerTube returned nothing (host blocked / endpoint down): fall back to a
    # few HTML scrapes so we at least record the current title.
    fallback = []
    for i in range(min(count, 5)):
        title = _scrape_title_via_html(video_id)
        if title:
            fallback.append(title)
        if i < count - 1:
            time.sleep(delay)
    return fallback
