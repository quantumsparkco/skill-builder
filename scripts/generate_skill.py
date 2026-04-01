#!/usr/bin/env python3
"""
generate_skill.py — Generate a Claude Code skill directory from extracted content.

Reads the current best-practices.md and lessons-learned.md before generating
so every skill benefits from accumulated knowledge.

Usage:
  python3 generate_skill.py --source <url> [--output-dir ~/.claude/skills]
  python3 generate_skill.py --content-file <path.json> [--output-dir ...]
  echo "raw text" | python3 generate_skill.py --stdin
"""

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

import anthropic

SKILL_DIR = Path(__file__).parent.parent
SKILLS_ROOT = Path.home() / ".claude" / "skills"
_REF_DIR = SKILL_DIR / "knowledge" if (SKILL_DIR / "knowledge").exists() else SKILL_DIR / "references"


def read_knowledge_base():
    best = (_REF_DIR / "best-practices.md").read_text(encoding="utf-8")
    lessons = (_REF_DIR / "lessons-learned.md").read_text(encoding="utf-8")
    sources = (_REF_DIR / "sources.md").read_text(encoding="utf-8")
    return best, lessons, sources


SYSTEM_PROMPT = """\
You are an expert at building Claude Code skill files — structured Markdown guides
that give Claude everything it needs to perform a skill on behalf of a user.

You have access to a knowledge base of current best practices and lessons learned
from previously built skills. You MUST read and apply these before generating.

A skill is a directory with this structure:
  skill-name/
  ├── SKILL.md              (required — instructions for Claude)
  └── references/
      ├── sources.md        (required — domain-relevant sources to check)
      └── notes.md          (optional — supplemental content from source material)

---

## SKILL.md format

```
---
name: kebab-case-name
description: [What it enables Claude to do] + [specific trigger contexts — be "pushy",
             list keywords and phrases that should invoke this skill. Include edge cases.]
---

# Skill Title

Brief overview of what this skill is for and when to use it.

## Staying Current

This skill was built on {today}. Before executing tasks where recency matters,
consult `references/sources.md`. For fast-moving topics, use WebFetch to check
the top source for new patterns before proceeding.

## [Core sections — steps, patterns, examples, gotchas]

[Explain *why* behind every major instruction. Use imperative form.
Keep under 400 lines. Reference notes.md if supplemental detail is needed.]

## Security Checklist
[REQUIRED for any skill that produces, reviews, or deploys code.
Include domain-specific checks from the OWASP Top 10 and the security
best practices in the knowledge base. Format as a checkbox list the user
can run through before shipping. Omit only for purely non-code skills
like writing or research.]

## Verification
[REQUIRED in every skill. Define exactly how Claude should check its own
work after completing a task. This creates a self-correction loop:
execute → verify → fix → re-verify until passing (max 3 iterations).

The verification method must be domain-appropriate:
- Code skills: run tests, linter, security scan, check output against requirements
- API skills: make a real test request, check response shape and status
- Data skills: validate schema, check for nulls/types, spot-check values
- Content skills: review against the stated criteria, check word count/format
- Design skills: screenshot and compare against reference

Format as numbered steps Claude executes after completing the main task.
Always end with: "If any check fails, fix it and re-run verification.
Repeat up to 3 times. If still failing after 3 attempts, surface the
specific failure to the user with a clear description of what's wrong."]
```

---

## sources.md format (embedded in output skill)

```markdown
# Sources for <Skill Name>

When encountering edge cases or needing to verify current best practices,
consult the sources below. For fast-moving domains, fetch the top source
before executing complex tasks.

## Primary
- [Source Name](url) — what it covers and why it's authoritative here

## Secondary
- [Source Name](url) — for edge cases / deeper reference
```

---

## Contradiction Handling

When sources disagree, resolve contradictions using this priority order — work through
each rule before escalating to the user:

1. **More recent beats older** — if a newer video/article contradicts an older one on a
   fast-moving topic (frameworks, APIs, tools), prefer the newer source. Note it briefly.

2. **More specific beats more general** — a tutorial about exactly this use case beats
   a general overview that happens to mention it.

3. **Consensus beats outlier** — if 4 sources agree and 1 disagrees, follow the consensus
   and note the dissent exists.

4. **Practical beats theoretical** — if one source shows a working implementation and
   another describes a different approach theoretically, prefer the one that's been shown
   to work.

5. **Escalate only when genuinely unresolvable** — if two credible, recent, specific
   sources directly contradict each other on something important AND none of the above
   rules break the tie, flag it as a question for the user. Keep it to the most important
   conflicts only — don't ask about minor style differences.

For any contradiction you resolve automatically, add a brief note in `references/notes.md`
explaining what disagreed and why you chose what you chose. This builds a transparent
audit trail.

For conflicts you escalate, add them to the "questions" key in your JSON output so the
app can surface them to the user before finalizing the skill.

---

## Your output

Return a JSON object with these keys:
- "skill_name": kebab-case identifier
- "skill_md": full text of SKILL.md (including frontmatter)
- "sources_md": full text of references/sources.md
- "notes_md": full text of references/notes.md — REQUIRED if any contradictions were found, null otherwise
- "summary": 2-3 sentences describing what you built and what makes it strong
- "questions": list of strings — unresolvable contradictions that need user input, or [] if none.
  Keep this list short. Only include conflicts that genuinely change what the skill should recommend.
  Frame each question so a non-expert can answer it — e.g. "Two instructors disagree on X:
  [Person A] says do Y because [reason]. [Person B] says do Z because [reason]. Which approach
  should this skill follow?" Give a recommended default so the user can just say "go with your recommendation."

Output ONLY valid JSON. No markdown fences, no preamble.
"""


def build_user_message(content, source_title, source_url, best_practices, lessons, sources_catalog):
    today = date.today().isoformat()
    return f"""## Knowledge Base — Read This First

### Current Best Practices
{best_practices}

### Lessons Learned (apply these — user preferences and past mistakes)
{lessons}

### Available Sources Catalog (use keys to select domain-relevant sources)
{sources_catalog}

---

## Task

Build a Claude Code skill from this content.

**Source:** {source_title}
**URL:** {source_url or 'N/A'}
**Date:** {today}

**Content:**
\"\"\"
{content}
\"\"\"

Instructions:
1. Apply all best practices and lessons from the knowledge base above
2. Select 3–7 sources from the catalog that are most relevant to this skill's domain
3. Build a skill that is concise, actionable, and captures the most reusable knowledge
4. Make the description "pushy" — list the trigger phrases and contexts
5. Embed the sources so this skill can stay current after deployment
6. Return valid JSON as specified in the system prompt
"""


def save_skill(result: dict, output_dir: Path) -> Path:
    skill_name = result["skill_name"]
    skill_dir = output_dir / skill_name
    refs_dir = skill_dir / "references"
    refs_dir.mkdir(parents=True, exist_ok=True)

    (skill_dir / "SKILL.md").write_text(result["skill_md"], encoding="utf-8")
    (refs_dir / "sources.md").write_text(result["sources_md"], encoding="utf-8")

    if result.get("notes_md"):
        (refs_dir / "notes.md").write_text(result["notes_md"], encoding="utf-8")

    return skill_dir


def main():
    parser = argparse.ArgumentParser(description="Generate a Claude Code skill from content")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--source", metavar="URL", help="URL to fetch and convert")
    group.add_argument("--content-file", metavar="PATH", help="JSON file from fetch_content.py --json")
    group.add_argument("--stdin", action="store_true", help="Read raw text from stdin")

    parser.add_argument("--source-title", default="", help="Override source title")
    parser.add_argument("--source-url", default="", help="Override source URL")
    parser.add_argument("--output-dir", default=str(SKILLS_ROOT), help="Where to save the skill")
    parser.add_argument("--print", dest="print_only", action="store_true", help="Print SKILL.md without saving")
    args = parser.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    # Collect content
    if args.content_file:
        data = json.loads(Path(args.content_file).read_text())
        content = data["content"]
        title = args.source_title or data.get("title", "")
        url = args.source_url or data.get("url", "")
    elif args.source:
        # Import fetch inline
        sys.path.insert(0, str(Path(__file__).parent))
        from fetch_content import fetch
        print(f"Fetching: {args.source}")
        data = fetch(args.source)
        content = data["content"]
        title = args.source_title or data["title"]
        url = data["url"]
    elif args.stdin or not sys.stdin.isatty():
        content = sys.stdin.read()
        title = args.source_title or "Raw Input"
        url = args.source_url or ""
    else:
        parser.print_help()
        sys.exit(1)

    # Truncate if needed
    MAX = 55_000
    if len(content) > MAX:
        content = content[:MAX] + "\n\n[...truncated...]"

    # Load knowledge base
    print("Loading knowledge base...")
    best_practices, lessons, sources_catalog = read_knowledge_base()

    # Generate
    print("Generating skill with Claude Opus 4.6...")
    client = anthropic.Anthropic()
    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=6000,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": build_user_message(content, title, url, best_practices, lessons, sources_catalog),
        }],
    )

    raw = message.content[0].text.strip()

    # Parse JSON response
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        # Try to extract JSON if wrapped in fences
        m = __import__("re").search(r"\{[\s\S]+\}", raw)
        if m:
            result = json.loads(m.group(0))
        else:
            print("ERROR: Could not parse Claude response as JSON", file=sys.stderr)
            print(raw[:500], file=sys.stderr)
            sys.exit(1)

    if args.print_only:
        print("\n" + "─" * 60)
        print(result["skill_md"])
        print("─" * 60)
        print(f"\nSummary: {result.get('summary', '')}")
    else:
        output_dir = Path(args.output_dir)
        saved_to = save_skill(result, output_dir)
        print(f"\nSkill saved to: {saved_to}/")
        print(f"\nSummary: {result.get('summary', '')}")
        print(f"\nFiles created:")
        print(f"  {saved_to}/SKILL.md")
        print(f"  {saved_to}/references/sources.md")
        if result.get("notes_md"):
            print(f"  {saved_to}/references/notes.md")
        print(f"\nRestart Claude Code or reload skills to activate.")


if __name__ == "__main__":
    main()
