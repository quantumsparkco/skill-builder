#!/usr/bin/env python3
"""
skill-updater.py — Automatically update skills and personas with new content.

Scans ~/.claude/skills/ and ~/.claude/personas/ for watchlist.json files,
checks if any sources are due for an update, fetches new content, and
rebuilds the skill/persona with Claude.

Usage:
  python3 skill-updater.py              # run all due updates (interactive)
  python3 skill-updater.py --dry-run    # show what would update, don't fetch
  python3 skill-updater.py --force      # ignore schedule, update everything now
  python3 skill-updater.py --name amazon-listing-agent  # update one skill by name

Setup (run once to schedule weekly):
  python3 skill-updater.py --install
"""

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────

SKILLS_ROOT   = Path.home() / ".claude" / "skills"
PERSONAS_ROOT = Path.home() / ".claude" / "personas"
SCRIPTS_DIR   = Path(__file__).parent

# Skill Builder scripts must be importable
sys.path.insert(0, str(SCRIPTS_DIR))


# ── Schedule helpers ───────────────────────────────────────────────────────────

SCHEDULE_DAYS = {"daily": 1, "weekly": 7, "monthly": 30}

def is_due(source: dict, force: bool = False) -> bool:
    if force:
        return True
    schedule = source.get("schedule")
    if not schedule or schedule == "never":
        return False
    days = SCHEDULE_DAYS.get(schedule, 7)
    last = source.get("last_fetched")
    if not last:
        return True
    last_date = datetime.strptime(last, "%Y-%m-%d").date()
    return (date.today() - last_date).days >= days


# ── Changelog helpers ──────────────────────────────────────────────────────────

def append_changelog(target_dir: Path, entry: str):
    cl = target_dir / "CHANGELOG.md"
    name = target_dir.name
    today = date.today().isoformat()
    block = f"\n## {today} (auto-update)\n{entry}\n"
    if cl.exists():
        existing = cl.read_text(encoding="utf-8")
        cl.write_text(existing.rstrip() + "\n" + block, encoding="utf-8")
    else:
        cl.write_text(f"# {name} — Changelog\n{block}", encoding="utf-8")


def write_initial_changelog(target_dir: Path, sources: list, skill_name: str):
    cl = target_dir / "CHANGELOG.md"
    today = date.today().isoformat()
    source_lines = "\n".join(f"- {s['url']}" for s in sources)
    cl.write_text(
        f"# {skill_name} — Changelog\n\n"
        f"## {today} (created)\n"
        f"Initial build from:\n{source_lines}\n",
        encoding="utf-8",
    )


# ── YouTube: fetch only new videos ────────────────────────────────────────────

def fetch_new_youtube_videos(url: str, since_date: str, max_videos: int = 200,
                              intention: str = "", log_fn=None):
    """
    Fetch only videos uploaded after since_date from a YouTube channel/playlist.
    Returns combined transcript text or raises if nothing new.
    """
    from fetch_content import get_video_list, get_transcript_text, score_relevance, get_channel_title

    def _log(msg, kind="info"):
        if log_fn:
            log_fn(msg, kind)
        else:
            print(f"  [{kind}] {msg}")

    since = datetime.strptime(since_date, "%Y-%m-%d").date() if since_date else None
    channel_title = get_channel_title(url)
    _log(f"Scanning {channel_title} for new videos...")

    all_videos = get_video_list(url, max_videos=max_videos, verbose=False)
    if not all_videos:
        raise ValueError("No videos found.")

    # Filter by upload date using yt-dlp metadata
    new_videos = []
    try:
        import yt_dlp
        ydl_opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
                    "playlistend": max_videos}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        entries = info.get("entries", []) if info else []
        for e in entries:
            if not e or not e.get("id") or len(e.get("id", "")) != 11:
                continue
            upload_str = e.get("upload_date") or e.get("timestamp") or ""
            if upload_str and since:
                try:
                    upload_date = datetime.strptime(str(upload_str)[:8], "%Y%m%d").date()
                    if upload_date <= since:
                        continue
                except Exception:
                    pass
            new_videos.append({
                "id": e["id"],
                "title": e.get("title", "Untitled"),
                "description": e.get("description") or "",
            })
    except Exception:
        new_videos = all_videos  # fallback: treat all as new

    if not new_videos:
        return None, []

    # Apply relevance filter if intention provided
    if intention:
        scored = [(v, score_relevance(v, intention)) for v in new_videos]
        new_videos = [v for v, s in sorted(scored, key=lambda x: -x[1]) if s > 0] or new_videos

    results = []
    for i, video in enumerate(new_videos, 1):
        _log(f"[{i}/{len(new_videos)}] {video['title'][:60]}")
        text, _ = get_transcript_text(video["id"])
        if text:
            results.append({
                "title": video["title"],
                "url": f"https://www.youtube.com/watch?v={video['id']}",
                "transcript": text,
            })

    if not results:
        return None, new_videos

    combined = "\n\n".join(
        f"## {r['title']}\nURL: {r['url']}\n\n{r['transcript']}" for r in results
    )
    return combined, new_videos


# ── Website: re-scrape ─────────────────────────────────────────────────────────

def fetch_website_content(url: str):
    from fetch_content import fetch_website
    result = fetch_website(url)
    return result["content"]


# ── Claude: append new content to existing skill ──────────────────────────────

APPEND_SKILL_PROMPT = """\
You are updating an existing Claude Code skill with new content.

The existing skill is provided below. New source content follows.

Your job:
1. Identify genuinely new information, techniques, or updated recommendations in the new content
2. Update the existing skill to incorporate it — revise sections, add bullets, update examples
3. Do NOT bloat the skill — remove outdated information if the new content supersedes it
4. Keep the skill under 400 lines
5. Return the complete updated SKILL.md text only — no JSON, no preamble

If the new content adds nothing meaningful, return the existing skill unchanged.
"""

APPEND_PERSONA_PROMPT = """\
You are updating an existing persona file with new content.

The existing persona is provided below. New source content follows.

Your job:
1. Identify new positions, quotes, mental models, or topics in the new content
2. Add them to the appropriate sections of the persona
3. Update the Quick Start system prompt if the new content meaningfully changes understanding of the person
4. Do NOT remove existing content unless it's directly contradicted
5. Return the complete updated persona markdown only — no JSON, no preamble

If the new content adds nothing meaningful, return the existing persona unchanged.
"""


def append_to_skill(skill_dir: Path, new_content: str, source_title: str) -> str:
    import anthropic
    existing = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=6000,
        system=APPEND_SKILL_PROMPT,
        messages=[{"role": "user", "content":
            f"## Existing SKILL.md\n\n{existing}\n\n"
            f"---\n\n## New Content from: {source_title}\n\n{new_content}"
        }],
    )
    return msg.content[0].text.strip()


def append_to_persona(persona_dir: Path, persona_file: Path,
                       new_content: str, source_title: str) -> str:
    import anthropic
    existing = persona_file.read_text(encoding="utf-8")
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=8000,
        system=APPEND_PERSONA_PROMPT,
        messages=[{"role": "user", "content":
            f"## Existing Persona\n\n{existing}\n\n"
            f"---\n\n## New Content from: {source_title}\n\n{new_content}"
        }],
    )
    return msg.content[0].text.strip()


# ── Core update logic ──────────────────────────────────────────────────────────

def update_target(target_dir: Path, target_type: str, force: bool = False, dry_run: bool = False):
    """
    Process one skill or persona directory.
    target_type: 'skill' or 'persona'
    """
    watchlist_path = target_dir / "watchlist.json"
    if not watchlist_path.exists():
        return

    watchlist = json.loads(watchlist_path.read_text(encoding="utf-8"))
    sources = watchlist.get("sources", [])
    intention = watchlist.get("intention", "")
    name = target_dir.name

    due_sources = [s for s in sources if is_due(s, force=force)]
    if not due_sources:
        return

    print(f"\n{'─'*60}")
    print(f"  {target_type.upper()}: {name}")
    print(f"{'─'*60}")

    all_new_content = []
    changelog_lines = []

    for source in due_sources:
        url = source["url"]
        last_fetched = source.get("last_fetched", "")
        is_youtube = "youtube.com" in url or "youtu.be" in url

        if dry_run:
            print(f"  [dry-run] Would fetch: {url}")
            continue

        print(f"\n  Checking: {url}")

        try:
            if is_youtube:
                content, new_videos = fetch_new_youtube_videos(
                    url, since_date=last_fetched, intention=intention
                )
                if not content:
                    print(f"  No new videos since {last_fetched or 'ever'}")
                    continue
                video_titles = [v["title"] for v in new_videos[:10]]
                summary = f"{len(new_videos)} new video(s) from {url}:\n" + \
                          "\n".join(f"  - {t}" for t in video_titles)
            else:
                content = fetch_website_content(url)
                if not content:
                    print(f"  Could not fetch: {url}")
                    continue
                summary = f"Updated content from: {url}"

            print(f"\n  {summary}\n")

            # Ask user (unless auto_accept)
            auto_accept = source.get("auto_accept", False)
            if not auto_accept:
                choice = input("  Accept? [y] Yes  [n] Skip  [a] Always accept: ").strip().lower()
                if choice == "n":
                    print("  Skipped.")
                    continue
                if choice == "a":
                    source["auto_accept"] = True
                    print("  Set to auto-accept for future updates.")

            all_new_content.append((content, url))
            changelog_lines.append(summary)

        except Exception as e:
            print(f"  Error: {e}")
            continue

    if dry_run or not all_new_content:
        return

    # Combine all new content
    combined_new = "\n\n---\n\n".join(
        f"Source: {url}\n\n{content}" for content, url in all_new_content
    )

    # Truncate
    MAX = 40_000
    if len(combined_new) > MAX:
        combined_new = combined_new[:MAX] + "\n\n[...truncated...]"

    print(f"\n  Updating {target_type} with Claude Opus 4.6...")

    try:
        if target_type == "skill":
            updated_text = append_to_skill(target_dir, combined_new, "Multiple Sources")
            (target_dir / "SKILL.md").write_text(updated_text, encoding="utf-8")
            print(f"  SKILL.md updated.")
        else:
            # Find the persona markdown file
            md_files = [f for f in target_dir.glob("*.md")
                        if f.name not in ("CHANGELOG.md",)]
            if not md_files:
                print(f"  No persona .md file found in {target_dir}")
                return
            persona_file = md_files[0]
            updated_text = append_to_persona(
                target_dir, persona_file, combined_new, "Multiple Sources"
            )
            persona_file.write_text(updated_text, encoding="utf-8")
            print(f"  {persona_file.name} updated.")

    except Exception as e:
        print(f"  Claude update failed: {e}")
        return

    # Update last_fetched for each source that ran
    today = date.today().isoformat()
    for source in due_sources:
        source["last_fetched"] = today
    watchlist["last_updated"] = today
    watchlist_path.write_text(json.dumps(watchlist, indent=2), encoding="utf-8")

    # Append changelog
    cl_entry = "\n".join(changelog_lines)
    append_changelog(target_dir, cl_entry)
    print(f"  CHANGELOG.md updated.")
    print(f"  Done. ✓")


# ── Install launchd plist ──────────────────────────────────────────────────────

def install_launchd():
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.skillbuilder.updater.plist"
    python_path = sys.executable
    script_path = Path(__file__).resolve()

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.skillbuilder.updater</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_path}</string>
        <string>{script_path}</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key>
        <integer>1</integer>
        <key>Hour</key>
        <integer>9</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>EnvironmentVariables</key>
    <dict>
        <key>ANTHROPIC_API_KEY</key>
        <string>{os.getenv("ANTHROPIC_API_KEY", "")}</string>
    </dict>
    <key>StandardOutPath</key>
    <string>{Path.home()}/.claude/skill-updater.log</string>
    <key>StandardErrorPath</key>
    <string>{Path.home()}/.claude/skill-updater-error.log</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
"""
    plist_path.write_text(plist_content, encoding="utf-8")
    os.system(f"launchctl load {plist_path}")
    print(f"\nInstalled: {plist_path}")
    print("Skill Updater will run every Monday at 9:00 AM.")
    print(f"Logs: ~/.claude/skill-updater.log")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Update skills and personas with new content")
    parser.add_argument("--dry-run",  action="store_true", help="Show what would update without fetching")
    parser.add_argument("--force",    action="store_true", help="Ignore schedule, update everything now")
    parser.add_argument("--name",     metavar="NAME",      help="Update one specific skill or persona by name")
    parser.add_argument("--install",  action="store_true", help="Install as a weekly launchd job")
    args = parser.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    if args.install:
        install_launchd()
        return

    targets = []
    for root, target_type in [(SKILLS_ROOT, "skill"), (PERSONAS_ROOT, "persona")]:
        if not root.exists():
            continue
        for d in sorted(root.iterdir()):
            if d.is_dir() and (d / "watchlist.json").exists():
                if args.name and d.name != args.name:
                    continue
                targets.append((d, target_type))

    if not targets:
        print("No watchlists found. Add recurring sources in Skill Builder to get started.")
        return

    print(f"\nSkill Updater — {date.today().isoformat()}")
    print(f"Checking {len(targets)} skill(s)/persona(s)...")

    for target_dir, target_type in targets:
        update_target(target_dir, target_type, force=args.force, dry_run=args.dry_run)

    print("\nAll done.")


if __name__ == "__main__":
    main()
