#!/usr/bin/env python3
"""
generate_persona.py — Generate a persona file from any content about a person.

The output is a single markdown file that can be pasted into any LLM
(Claude, ChatGPT, Gemini, etc.) to simulate that person's thinking.
"""

import json
import os
import re
from datetime import date
from pathlib import Path

import anthropic

SKILL_DIR = Path(__file__).parent.parent
_REF_DIR = SKILL_DIR / "knowledge" if (SKILL_DIR / "knowledge").exists() else SKILL_DIR / "references"


SYSTEM_PROMPT = """\
You are an expert at analyzing content about real people and distilling it into
rich, accurate persona files that allow any LLM to simulate how that person thinks,
speaks, and reasons.

A great persona file has two layers:
1. DIRECT KNOWLEDGE — specific positions, quotes, and views the person has documented
2. FIRST PRINCIPLES — their core reasoning frameworks, so the LLM can extrapolate
   to topics they've never directly addressed, thinking *as* them rather than just quoting them

The output is a single markdown file structured as follows:

---

# [Full Name] — Persona

> One-sentence essence of who this person is and what they stand for.

## Quick Start

Paste the following block into the "System Prompt" or "Custom Instructions" field
of any LLM (Claude, ChatGPT, Gemini, etc.):

```
[READY-TO-USE SYSTEM PROMPT — self-contained, 200-400 words.
Must include: who they are, how they speak, their core mental models,
and an instruction to cite sources when drawing on specific documented positions.
Should end with: "When asked about topics I haven't directly addressed,
reason from my first principles rather than refusing to answer."]
```

---

## Identity & Background

[Who they are, their history, what shaped them, their domain of expertise,
major achievements and failures that inform their worldview.]

## Communication Style

[How they actually speak and write — tone, vocabulary, pace, use of analogy,
tendency toward brevity or detail, humor, directness. Include characteristic
phrases or patterns if known. Be specific enough that the LLM can mimic it.]

## Mental Models & First Principles

[The core frameworks they use to think about problems. These are the extrapolation
engine — when someone asks about a topic they haven't addressed, these principles
are what the LLM uses to reason as them. Include 5-10 specific, named mental models
with brief explanations of how they apply them.]

## Known Positions — Topic Repository

[Documented views organized by topic. For each:
**Topic:** Their position
*Source context: [video/book/interview title if known]*

Include as many specific positions as the source material supports.
This is the direct knowledge layer — cite it when relevant.]

## How to Use This Persona

### In Claude (claude.ai or Claude Code)
1. Start a new conversation
2. Paste the Quick Start system prompt above as your first message, prefixed with "For this conversation:"
3. Or in Claude.ai → Settings → Custom Instructions → paste the system prompt there

### In ChatGPT
1. Go to ChatGPT → your profile → Customize ChatGPT
2. Paste the system prompt in the "How would you like ChatGPT to respond?" field
3. Or start a conversation and paste it as the first message

### Creating a Board of Multiple Personas
To simulate a board of advisors:
1. Download each persona file you want on the board
2. Combine them into one file under a section called "## Board Members"
3. Add this instruction at the top: "You are facilitating a board discussion. When asked a question, give each board member's perspective in their own voice, then provide a synthesis."
4. Paste the combined file as your system prompt

---

Return a JSON object with these keys:
- "persona_name": the person's full name
- "persona_md": the complete persona markdown file (as described above)
- "summary": 2-3 sentences describing what was captured and what makes this persona strong
- "topics_covered": list of 5-10 topic strings showing what direct knowledge was captured
- "questions": list of important contradictions or gaps that need user clarification (or [])

Output ONLY valid JSON. No markdown fences, no preamble.
"""


def build_persona_message(content, source_title, source_url):
    today = date.today().isoformat()
    return f"""Build a persona file from this content.

**Source:** {source_title}
**URL:** {source_url or 'N/A'}
**Date:** {today}

Instructions:
1. Extract everything about this person's identity, style, and thinking
2. Build a rich topic repository from their documented positions — be specific
3. Distill their reasoning into first principles that enable extrapolation
4. Make the Quick Start system prompt genuinely usable — self-contained and vivid
5. If the content covers multiple people, build the primary persona around the most prominent one
   and note others in the identity section
6. Flag any significant contradictions or gaps in the "questions" field

**Content:**
\"\"\"
{content}
\"\"\"
"""


def generate_persona(content, source_title, source_url):
    MAX = 55_000
    if len(content) > MAX:
        content = content[:MAX] + "\n\n[...truncated...]"

    client = anthropic.Anthropic()
    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": build_persona_message(content, source_title, source_url),
        }],
    )

    raw = message.content[0].text.strip()
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]+\}", raw)
        result = json.loads(m.group(0)) if m else None
    if not result:
        raise ValueError("Could not parse Claude response as JSON")
    return result
