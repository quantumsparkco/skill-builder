#!/usr/bin/env python3
"""
fetch_content.py — Extract content from any YouTube URL, website, or local file.

Handles:
  - Single YouTube video    https://youtube.com/watch?v=xxx
  - YouTube playlist        https://youtube.com/playlist?list=xxx
  - YouTube channel         https://youtube.com/@channelname
  - Website / article       https://any-site.com/tutorial
  - Local file              /path/to/transcript.txt

Usage:
  python3 fetch_content.py <url_or_path>
  python3 fetch_content.py --json <url_or_path>
  python3 fetch_content.py --max-videos 20 <channel_url>
"""

import argparse
import json
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound
    TRANSCRIPT_API = True
except ImportError:
    TRANSCRIPT_API = False

try:
    import yt_dlp
    YTDLP = True
except ImportError:
    YTDLP = False


# ──────────────────────────────────────────────
# YouTube URL classification
# ──────────────────────────────────────────────

def classify_youtube_url(url):
    """Return ('video'|'playlist'|'channel', id_or_url)."""
    if re.search(r"[?&]list=", url):
        return "playlist", url
    if re.search(r"(?:youtube\.com/@|youtube\.com/c/|youtube\.com/user/|youtube\.com/channel/)", url):
        return "channel", url
    if re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", url):
        m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", url)
        return "video", m.group(1)
    return "unknown", url


# ──────────────────────────────────────────────
# Video list extraction via yt-dlp
# ──────────────────────────────────────────────

def get_video_list(url, max_videos=50, verbose=True):
    """
    Extract a list of (video_id, title) tuples from any YouTube URL.
    Works for single videos, playlists, and channels.
    """
    if not YTDLP:
        raise RuntimeError("yt-dlp not installed. Run: pip3 install yt-dlp")

    if verbose:
        print(f"  Scanning for videos (max {max_videos})...", flush=True)

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,         # Don't download, just list
        "playlistend": max_videos,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        return []

    # Playlist / channel → multiple entries
    if "entries" in info:
        entries = [e for e in info["entries"] if e and e.get("id")]
        return [(e["id"], e.get("title", "Untitled")) for e in entries]

    # Single video
    return [(info["id"], info.get("title", "Untitled"))]


def get_channel_title(url):
    """Best-effort: get a human-readable channel/playlist name."""
    if not YTDLP:
        return "YouTube Content"
    ydl_opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "playlistend": 1}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return info.get("channel") or info.get("uploader") or info.get("title") or "YouTube Content"
    except Exception:
        return "YouTube Content"


# ──────────────────────────────────────────────
# Transcript extraction
# ──────────────────────────────────────────────

def get_transcript_text(video_id, verbose=False):
    """
    Try youtube-transcript-api first (cleanest text).
    Fall back to yt-dlp auto-subtitle extraction.
    Returns (text, method) or (None, reason).
    """
    # Method 1: youtube-transcript-api
    if TRANSCRIPT_API:
        try:
            entries = YouTubeTranscriptApi.get_transcript(video_id)
            text = " ".join(e["text"] for e in entries)
            return text, "transcript-api"
        except (TranscriptsDisabled, NoTranscriptFound):
            pass
        except Exception as e:
            if verbose:
                print(f"    transcript-api error: {e}", flush=True)

    # Method 2: yt-dlp subtitle extraction
    if YTDLP:
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmpdir:
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "writeautomaticsub": True,
                "writesubtitles": True,
                "subtitleslangs": ["en"],
                "subtitlesformat": "vtt",
                "outtmpl": str(Path(tmpdir) / "%(id)s.%(ext)s"),
            }
            try:
                url = f"https://www.youtube.com/watch?v={video_id}"
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])

                # Find the .vtt file
                vtt_files = list(Path(tmpdir).glob("*.vtt"))
                if vtt_files:
                    raw = vtt_files[0].read_text(encoding="utf-8", errors="ignore")
                    text = parse_vtt(raw)
                    return text, "yt-dlp-vtt"
            except Exception as e:
                if verbose:
                    print(f"    yt-dlp subtitle error: {e}", flush=True)

    return None, "no-transcript-available"


def parse_vtt(vtt_text):
    """Strip VTT timing lines and deduplicate caption text."""
    lines = []
    for line in vtt_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("WEBVTT") or line.startswith("NOTE") or line.startswith("Kind:") or line.startswith("Language:"):
            continue
        if re.match(r"^\d{2}:\d{2}", line) or re.match(r"^\d+$", line):
            continue
        # Strip HTML tags
        line = re.sub(r"<[^>]+>", "", line)
        if line:
            lines.append(line)

    # Deduplicate adjacent duplicate lines
    deduped = []
    for line in lines:
        if not deduped or line != deduped[-1]:
            deduped.append(line)
    return " ".join(deduped)


# ──────────────────────────────────────────────
# Multi-video fetch (playlist / channel)
# ──────────────────────────────────────────────

def fetch_youtube_collection(url, max_videos=50, verbose=True):
    """
    Fetch all transcripts from a playlist or channel.
    Returns a dict with combined content and per-video breakdown.
    """
    url_type, _ = classify_youtube_url(url)
    channel_title = get_channel_title(url)

    if verbose:
        print(f"  Collection: {channel_title}")

    videos = get_video_list(url, max_videos=max_videos, verbose=verbose)

    if not videos:
        raise ValueError("No videos found at that URL.")

    if verbose:
        print(f"  Found {len(videos)} video(s). Fetching transcripts...")

    results = []
    success = 0
    for i, (vid_id, title) in enumerate(videos, 1):
        if verbose:
            print(f"  [{i}/{len(videos)}] {title[:60]}", end=" ", flush=True)

        text, method = get_transcript_text(vid_id, verbose=False)
        if text:
            results.append({
                "video_id": vid_id,
                "title": title,
                "transcript": text,
                "method": method,
                "url": f"https://www.youtube.com/watch?v={vid_id}",
            })
            success += 1
            if verbose:
                print(f"✓ ({len(text):,} chars)", flush=True)
        else:
            if verbose:
                print(f"✗ (no transcript)", flush=True)

    if not results:
        raise ValueError("No transcripts were available for any video in this collection.")

    # Build combined content — structured so Claude understands the context
    parts = [f"# {channel_title}\n\nSource: {url}\nVideos with transcripts: {success}/{len(videos)}\n"]
    for r in results:
        parts.append(f"\n---\n\n## {r['title']}\nURL: {r['url']}\n\n{r['transcript']}")

    combined = "\n".join(parts)
    total_chars = sum(len(r["transcript"]) for r in results)

    if verbose:
        print(f"\n  Total content: {total_chars:,} chars across {success} videos")

    return {
        "title": channel_title,
        "source_type": url_type,
        "url": url,
        "content": combined,
        "char_count": len(combined),
        "video_count": success,
        "videos": results,
    }


# ──────────────────────────────────────────────
# Single video
# ──────────────────────────────────────────────

def fetch_youtube_video(video_id, original_url=""):
    """Fetch a single YouTube video transcript."""
    # Get title
    title = "YouTube Video"
    if YTDLP:
        try:
            ydl_opts = {"quiet": True, "no_warnings": True, "extract_flat": True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(
                    f"https://www.youtube.com/watch?v={video_id}", download=False
                )
                title = info.get("title", title)
        except Exception:
            pass

    text, method = get_transcript_text(video_id, verbose=True)
    if not text:
        raise ValueError(f"No transcript available for video {video_id}")

    url = original_url or f"https://www.youtube.com/watch?v={video_id}"
    return {
        "title": title,
        "source_type": "youtube_video",
        "url": url,
        "content": text,
        "char_count": len(text),
        "video_count": 1,
    }


# ──────────────────────────────────────────────
# Web scraping
# ──────────────────────────────────────────────

def fetch_website(url):
    headers = {"User-Agent": "Mozilla/5.0 (compatible; SkillBuilder/1.0)"}
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()

    title = soup.title.string.strip() if soup.title and soup.title.string else url
    container = soup.find("article") or soup.find("main") or soup.body
    text = container.get_text(separator="\n", strip=True) if container else soup.get_text()
    text = re.sub(r"\n{3,}", "\n\n", text)

    return {
        "title": title,
        "source_type": "website",
        "url": url,
        "content": text,
        "char_count": len(text),
    }


def fetch_file(path):
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")
    content = p.read_text(encoding="utf-8")
    return {
        "title": p.stem.replace("-", " ").replace("_", " ").title(),
        "source_type": "file",
        "url": str(p.resolve()),
        "content": content,
        "char_count": len(content),
    }


# ──────────────────────────────────────────────
# Main dispatcher
# ──────────────────────────────────────────────

def fetch(source, max_videos=50, verbose=True):
    """Dispatch to the right fetcher based on URL type."""
    if not source.startswith("http"):
        return fetch_file(source)

    if "youtube.com" in source or "youtu.be" in source:
        url_type, vid_id = classify_youtube_url(source)
        if url_type == "video":
            return fetch_youtube_video(vid_id, original_url=source)
        elif url_type in ("playlist", "channel"):
            return fetch_youtube_collection(source, max_videos=max_videos, verbose=verbose)
        else:
            raise ValueError(f"Unrecognized YouTube URL format: {source}")

    return fetch_website(source)


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Extract content from YouTube (video/playlist/channel), websites, or files"
    )
    parser.add_argument("source", help="URL or file path")
    parser.add_argument("--json", action="store_true", help="Output as JSON (for scripts)")
    parser.add_argument("--max-videos", type=int, default=50,
                        help="Max videos to process from a playlist/channel (default: 50)")
    parser.add_argument("--list-only", action="store_true",
                        help="For playlists/channels: list video titles without fetching transcripts")
    args = parser.parse_args()

    # List-only mode
    if args.list_only:
        if "youtube.com" not in args.source and "youtu.be" not in args.source:
            print("--list-only only works with YouTube URLs")
            sys.exit(1)
        url_type, _ = classify_youtube_url(args.source)
        if url_type == "video":
            print("Single video — nothing to list")
        else:
            videos = get_video_list(args.source, max_videos=args.max_videos, verbose=False)
            print(f"Found {len(videos)} video(s):")
            for i, (vid_id, title) in enumerate(videos, 1):
                print(f"  {i:3}. [{vid_id}] {title}")
        return

    try:
        result = fetch(args.source, max_videos=args.max_videos, verbose=not args.json)
    except Exception as e:
        if args.json:
            print(json.dumps({"error": str(e)}))
        else:
            print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        # Truncate content for safe embedding in prompts
        MAX = 60_000
        output = {k: v for k, v in result.items() if k != "videos"}
        if len(output.get("content", "")) > MAX:
            output["content"] = output["content"][:MAX] + "\n\n[...truncated...]"
            output["truncated"] = True
        print(json.dumps(output, ensure_ascii=False))
    else:
        print(f"\nTitle:     {result['title']}")
        print(f"Type:      {result['source_type']}")
        print(f"Length:    {result['char_count']:,} chars")
        if result.get("video_count", 1) > 1:
            print(f"Videos:    {result['video_count']}")
        print(f"Source:    {result['url']}")


if __name__ == "__main__":
    main()
