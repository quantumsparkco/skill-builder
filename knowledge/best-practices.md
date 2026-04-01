# Best Practices — Skill Builder Knowledge Base

Synthesized from monitored sources. Updated weekly by `harvest.py`.
Last updated: 2026-03-31 (initial seed)

---

## Patterns Section
*(Added after recurring themes emerge from lessons-learned)*

---

## Skill Structure & Design

### Progressive disclosure is the core architectural principle
Skills load in three layers: metadata (~100 words, always in context) → SKILL.md body (triggered on use, keep under 500 lines) → bundled resources (loaded as needed, unlimited). Design skills to reveal complexity gradually. Don't dump everything into the body.
*Source: anthropic-skills*

### Descriptions drive triggering — make them "pushy"
The description field is the primary trigger mechanism. Claude undertriggers skills when descriptions are passive. Write descriptions that list keywords, contexts, and edge cases. Instead of "helps with X", write "use this when the user mentions X, Y, or Z, or when they ask about [specific phrases]."
*Source: anthropic-skills, skill-creator*

### Explain *why*, not just *what*
Instructions that explain reasoning produce better outcomes than rigid rules. LLMs have good theory of mind — when they understand why something matters they generalize correctly to edge cases. Avoid ALL CAPS MUST directives when you can explain the underlying principle instead.
*Source: anthropic-skills*

### The SKILL.md body should stay under 400–500 lines
Beyond that, use references/ files. The body is loaded into every invocation — bloated bodies waste context. Move domain-specific detail, large examples, and reference tables to references/ and point to them explicitly.
*Source: anthropic-skills*

---

## Knowledge & Sources Integration

### Fast-moving domains need embedded source references
Skills in domains that change rapidly (LLM APIs, agent patterns, framework APIs) should carry source references and instruct Claude to do a quick freshness check before executing complex tasks. Static domains (math, writing style, historical knowledge) don't need this.
*Source: dair-prompt-guide, anthropic-cookbooks*

### The best sources to embed depend on domain
- Agent design → ms-autogen, ms-multiagent-arch, volt-agent-papers
- Prompt optimization → dspy, instructor, prompt-report-paper
- Claude-specific → anthropic-cookbooks, anthropic-skills
- Research → lil-log, ahead-of-ai
- Code → swe-agent, openai-cookbook-web
*Source: sources.md domain tag reference*

---

## Prompting Patterns (from research)

### Chain-of-thought improves multi-step reasoning significantly
For tasks requiring planning, analysis, or debugging, instruct Claude to think step-by-step before answering. This isn't just style — it materially changes output quality on complex tasks.
*Source: prompt-report-paper (technique #12 of 58)*

### Few-shot examples outperform zero-shot instructions for format-sensitive tasks
When output format is critical (specific JSON structure, particular writing style, precise code patterns), include 1–3 examples rather than relying on description alone. Examples anchor the model far more reliably than instructions.
*Source: dair-prompt-guide, nir-prompt-eng*

### Role + context + task + format + constraints is the complete prompt anatomy
Effective prompts specify: who Claude is acting as, what background context is relevant, the specific task, the expected output format, and any constraints. Missing any of these produces lower quality. For skill instructions, all five should be present.
*Source: dair-prompt-guide*

### DSPy-style: treat prompts as programs, not strings
Prompts that are composed programmatically (with typed signatures, assertions, and optimizer passes) outperform hand-crafted prompts on complex tasks. For skills that involve structured data transformation, use the instructor/pydantic pattern.
*Source: dspy, instructor*

---

## Agent Design Patterns

### Tool-first design beats instruction-first design
Agents work best when you define their tools precisely before writing their instructions. The tools define what's possible; the instructions shape how possibilities are used.
*Source: agentic-workflows-paper*

### Single-responsibility agents > generalist agents
Each agent should do one thing well. Orchestrate multiple focused agents rather than building one agent with a long list of capabilities. This applies directly to skill design — narrow skills trigger more reliably and execute more accurately.
*Source: ms-multiagent-arch, designing-multiagent*

### Parallel subagents with explicit output contracts
When spawning subagents, define their output format before spawning. Agents that write to well-defined output locations and formats can be parallelized and composed reliably. Agents with fuzzy output contracts create integration problems.
*Source: ms-autogen, agentic-workflows-paper*

---

## Evaluation

### Run with-skill vs. without-skill baseline for every new skill
The only way to know a skill is adding value is to compare outputs with and without it. This is the standard evaluation pattern in the skill-creator framework.
*Source: anthropic-skills (skill-creator)*

### Non-discriminating assertions are worse than no assertions
An assertion that always passes regardless of skill quality doesn't measure anything. Good assertions fail on bad outputs and pass on good ones. Write assertions that would actually catch the kinds of failures you care about.
*Source: anthropic-skills (skill-creator)*

---

## Security — Non-Negotiable Requirements for All Code Skills

Every skill that produces, reviews, or deploys code MUST include a Security section. This is not optional. Vibe-coded apps are a primary attack surface — the skill is the last line of defense before code ships.

### The OWASP Top 10 — always check these in any web/API skill

1. **Broken Access Control** — never trust client-supplied roles/IDs; verify server-side
2. **Cryptographic Failures** — never store plaintext secrets; use env vars, never hardcode keys
3. **Injection** (SQL, command, XSS) — always use parameterized queries; never interpolate user input into queries or shell commands
4. **Insecure Design** — threat model before building; assume hostile input at every boundary
5. **Security Misconfiguration** — disable debug mode in prod; remove default credentials; set security headers
6. **Vulnerable Components** — pin dependency versions; check `npm audit` / `pip-audit` before shipping
7. **Auth & Session Failures** — use established auth libraries (never roll your own); set secure/httpOnly cookie flags
8. **Integrity Failures** — verify package integrity; don't execute code from untrusted CDNs without SRI hashes
9. **Logging Failures** — log auth events and failures; never log passwords or tokens
10. **SSRF** — validate and restrict URLs before making server-side requests

### Security section template for generated skills

Every skill that touches code must include this section:

```markdown
## Security Checklist

Before shipping any code produced by this skill, verify:

- [ ] No secrets, API keys, or passwords in code or version control — use env vars
- [ ] All user input is validated and sanitized before use
- [ ] Database queries use parameterized statements, never string interpolation
- [ ] Dependencies are pinned and checked with `npm audit` or `pip-audit`
- [ ] Auth/session handling uses a trusted library, not custom implementation
- [ ] Error messages don't leak stack traces or internal paths to users
- [ ] Security headers set (CSP, X-Frame-Options, HSTS for HTTPS apps)
- [ ] [domain-specific check — e.g., for APIs: rate limiting enabled]

Consult `references/sources.md` → security sources for current vulnerability patterns.
```

### Domain-specific security rules

**Frontend/React/JS:**
- Always sanitize before `dangerouslySetInnerHTML`; prefer React's built-in escaping
- Use Content Security Policy headers
- Load third-party scripts with Subresource Integrity (SRI) hashes

**Backend/API:**
- Rate limit all auth endpoints
- Use HTTPS everywhere; redirect HTTP → HTTPS
- Validate Content-Type on all POST/PUT endpoints

**Database:**
- Parameterized queries always — no exceptions
- Principle of least privilege on DB user permissions
- Never expose DB connection strings in client-side code

**Authentication:**
- Use established libraries (NextAuth, Passport, Supabase Auth)
- Bcrypt/argon2 for password hashing — never MD5/SHA1
- Short-lived JWTs with refresh token rotation

---

## What to Monitor Going Forward

The following sources are most likely to produce actionable updates:
1. `dair-prompt-guide` — new papers added frequently, often contains novel techniques
2. `volt-agent-papers` — weekly AI agent paper digest
3. `anthropic-skills` — Anthropic's own skill patterns evolve directly
4. `lil-log` — infrequent but each post is deeply influential
5. `ms-autogen` — multi-agent orchestration patterns advancing rapidly

*Run `harvest.py` weekly to pull updates from all sources.*
