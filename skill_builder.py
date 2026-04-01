#!/usr/bin/env python3
"""
Skill Builder — Build Claude Code skills from any learning source.

Accepts:
  YouTube video     https://youtube.com/watch?v=xxx
  YouTube playlist  https://youtube.com/playlist?list=xxx
  YouTube channel   https://youtube.com/@channelname
  Website / article https://any-site.com/tutorial
  Local file        /path/to/transcript.txt
  Piped text        cat notes.txt | python3 skill_builder.py

Usage:
  python3 skill_builder.py <url_or_path>
  python3 skill_builder.py --max-videos 30 https://youtube.com/@channel
  python3 skill_builder.py --list https://youtube.com/playlist?list=xxx
  python3 skill_builder.py --output ~/my-skill.md <url>
  python3 skill_builder.py --print <url>
"""

import argparse
import os
import sys
from pathlib import Path

# Point to the skill-builder scripts
SKILL_SCRIPTS = Path.home() / ".claude" / "skills" / "skill-builder" / "scripts"
sys.path.insert(0, str(SKILL_SCRIPTS))


def check_api_key():
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY is not set.")
        print("  export ANTHROPIC_API_KEY=sk-ant-...")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Build a Claude Code skill from a YouTube channel, playlist, video, or website.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single video
  python3 skill_builder.py https://youtube.com/watch?v=dQw4w9WgXcQ

  # Full playlist (up to 50 videos)
  python3 skill_builder.py https://youtube.com/playlist?list=PLxxxx

  # YouTube channel (first 30 videos)
  python3 skill_builder.py --max-videos 30 https://youtube.com/@channelname

  # Just list videos without fetching (to preview)
  python3 skill_builder.py --list https://youtube.com/playlist?list=PLxxxx

  # Website tutorial
  python3 skill_builder.py https://docs.example.com/tutorial

  # Local transcript file
  python3 skill_builder.py my_notes.txt

  # Preview output without saving
  python3 skill_builder.py --print https://youtube.com/watch?v=xxx

  # Save to custom location
  python3 skill_builder.py -o ~/my-skill/ https://youtube.com/watch?v=xxx
        """
    )

    parser.add_argument("source", nargs="?", help="YouTube URL, website URL, or file path")
    parser.add_argument("--max-videos", type=int, default=200, metavar="N",
                        help="Max videos from a playlist or channel (default: 50)")
    parser.add_argument("--list", dest="list_only", action="store_true",
                        help="List videos in a playlist/channel without processing")
    parser.add_argument("--output", "-o", metavar="PATH",
                        help="Save skill to this directory (default: ~/.claude/skills/<name>/)")
    parser.add_argument("--print", dest="print_only", action="store_true",
                        help="Print generated SKILL.md to stdout without saving")

    args = parser.parse_args()

    # ── List mode ───────────────────────────────────────────────
    if args.list_only:
        if not args.source:
            print("ERROR: Provide a YouTube playlist or channel URL.")
            sys.exit(1)
        from fetch_content import get_video_list, get_channel_title, classify_youtube_url
        url_type, _ = classify_youtube_url(args.source)
        title = get_channel_title(args.source)
        print(f"\n{title}")
        print(f"Type: {url_type}")
        print(f"Scanning for up to {args.max_videos} videos...\n")
        videos = get_video_list(args.source, max_videos=args.max_videos, verbose=True)
        print(f"\nFound {len(videos)} video(s):")
        for i, (vid_id, title) in enumerate(videos, 1):
            print(f"  {i:3}. {title}")
            print(f"       https://youtube.com/watch?v={vid_id}")
        print(f"\nRun without --list to build a skill from all {len(videos)} videos.")
        return

    # ── Collect content ─────────────────────────────────────────
    check_api_key()

    from fetch_content import fetch

    source = args.source
    if not source:
        if not sys.stdin.isatty():
            # Piped input
            content = sys.stdin.read()
            result = {
                "title": "Piped Input",
                "source_type": "text",
                "url": "",
                "content": content,
                "char_count": len(content),
            }
        else:
            parser.print_help()
            sys.exit(1)
    else:
        print(f"\nSource: {source}")

        if any(k in source for k in ["youtube.com", "youtu.be"]):
            from fetch_content import classify_youtube_url
            url_type, _ = classify_youtube_url(source)
            if url_type in ("playlist", "channel"):
                print(f"Type:   {url_type} (max {args.max_videos} videos)")
                print("Tip:    Use --list first to preview what will be included.\n")

        try:
            result = fetch(source, max_videos=args.max_videos, verbose=True)
        except Exception as e:
            print(f"\nERROR: {e}", file=sys.stderr)
            sys.exit(1)

    # ── Report what we got ──────────────────────────────────────
    print(f"\nContent fetched:")
    print(f"  Title:   {result['title']}")
    print(f"  Type:    {result['source_type']}")
    print(f"  Length:  {result['char_count']:,} chars")
    if result.get("video_count", 1) > 1:
        print(f"  Videos:  {result['video_count']}")

    # ── Generate skill ──────────────────────────────────────────
    print("\nGenerating skill...")

    from generate_skill import read_knowledge_base, build_user_message, save_skill
    import anthropic, json, re

    # Check knowledge base freshness
    kb_file = Path.home() / ".claude" / "skills" / "skill-builder" / "references" / "best-practices.md"
    if kb_file.exists():
        import time
        age_days = (time.time() - kb_file.stat().st_mtime) / 86400
        if age_days > 7:
            print(f"  Note: Knowledge base is {age_days:.0f} days old.")
            print("  Run: python3 ~/.claude/skills/skill-builder/scripts/harvest.py")

    best_practices, lessons, sources_catalog = read_knowledge_base()

    content = result["content"]
    MAX = 55_000
    if len(content) > MAX:
        print(f"  Content truncated from {len(content):,} to {MAX:,} chars")
        content = content[:MAX] + "\n\n[...truncated...]"

    SYSTEM_PROMPT = open(SKILL_SCRIPTS / "generate_skill.py").read()
    # Extract just the SYSTEM_PROMPT constant from generate_skill.py
    import importlib.util
    spec = importlib.util.spec_from_file_location("generate_skill", SKILL_SCRIPTS / "generate_skill.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    client = anthropic.Anthropic()
    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=6000,
        system=mod.SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": build_user_message(
                content, result["title"], result["url"],
                best_practices, lessons, sources_catalog
            ),
        }],
    )

    raw = message.content[0].text.strip()
    try:
        skill_result = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]+\}", raw)
        if m:
            skill_result = json.loads(m.group(0))
        else:
            print("ERROR: Could not parse Claude response.", file=sys.stderr)
            print(raw[:500], file=sys.stderr)
            sys.exit(1)

    # ── Output ──────────────────────────────────────────────────
    if args.print_only:
        print("\n" + "─" * 60)
        print(skill_result["skill_md"])
        print("─" * 60)
        if skill_result.get("summary"):
            print(f"\nSummary: {skill_result['summary']}")
    else:
        output_dir = Path(args.output) if args.output else Path.home() / ".claude" / "skills"
        saved_to = save_skill(skill_result, output_dir)

        print(f"\nSkill saved to: {saved_to}/")
        print(f"\nFiles:")
        print(f"  {saved_to}/SKILL.md")
        print(f"  {saved_to}/references/sources.md")
        if skill_result.get("notes_md"):
            print(f"  {saved_to}/references/notes.md")

        if skill_result.get("summary"):
            print(f"\n{skill_result['summary']}")

        print(f"\nRestart Claude Code in VS Code to activate the skill.")


if __name__ == "__main__":
    main()
