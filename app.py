#!/usr/bin/env python3
"""
Skill Builder Web App
Run: python3 app.py
Open: http://localhost:5173
"""

import io
import json
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import uuid
import zipfile
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context

# Use bundled scripts/ dir (works locally and on Railway)
SKILL_SCRIPTS = Path(__file__).parent / "scripts"
sys.path.insert(0, str(SKILL_SCRIPTS))

app = Flask(__name__)
jobs = {}  # job_id -> {status, queue, result, zip_path, skill_dir, skill_name, error}


# ──────────────────────────────────────────────
# Background build worker
# ──────────────────────────────────────────────

def run_build(job_id, sources, max_videos, raw_text, intention=""):
    q = jobs[job_id]["queue"]

    def log(msg, kind="info"):
        q.put({"type": kind, "message": msg})

    try:
        from fetch_content import fetch
        from generate_skill import SYSTEM_PROMPT, build_user_message, read_knowledge_base, save_skill
        import anthropic

        all_parts = []

        # Process each URL
        for url in sources:
            if jobs[job_id].get("cancelled"):
                return
            log(f"Fetching: {url}")
            try:
                def _fwd(msg):
                    if jobs[job_id].get("cancelled"):
                        return
                    q.put(msg)
                result = fetch(url, max_videos=max_videos, verbose=False,
                               intention=intention, log_fn=_fwd)
                all_parts.append({
                    "title": result["title"],
                    "content": result["content"],
                    "url": url,
                    "video_count": result.get("video_count", 1),
                })
                vids = result.get("video_count", 1)
                chars = len(result["content"])
                label = f"{vids} videos, {chars:,} chars" if vids > 1 else f"{chars:,} chars"
                log(f"Got: {result['title']} ({label})", "success")
            except Exception as e:
                log(f"Failed: {url} — {e}", "error")

        # Add uploaded / pasted text
        if raw_text:
            all_parts.append({
                "title": "Uploaded Content",
                "content": raw_text,
                "url": "",
                "video_count": 1,
            })
            log(f"Got: Uploaded content ({len(raw_text):,} chars)", "success")

        if not all_parts:
            raise ValueError("No content was successfully fetched. Check URLs and try again.")

        # Combine content
        if len(all_parts) == 1:
            combined = all_parts[0]["content"]
            source_title = all_parts[0]["title"]
            source_url = all_parts[0]["url"]
        else:
            parts_text = []
            for p in all_parts:
                header = f"# {p['title']}"
                if p["url"]:
                    header += f"\nSource: {p['url']}"
                parts_text.append(f"{header}\n\n{p['content']}")
            combined = "\n\n---\n\n".join(parts_text)
            source_title = "Combined: " + ", ".join(p["title"][:25] for p in all_parts[:3])
            source_url = sources[0] if sources else ""

        # Truncate for API
        MAX = 55_000
        if len(combined) > MAX:
            log(f"Content truncated to {MAX:,} chars for processing")
            combined = combined[:MAX] + "\n\n[...truncated...]"

        if jobs[job_id].get("cancelled"):
            return

        log("Loading knowledge base...")
        best_practices, lessons, sources_catalog = read_knowledge_base()

        log("Generating skill with Claude Opus 4.6 ...")
        client = anthropic.Anthropic()
        message = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=6000,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": build_user_message(
                    combined, source_title, source_url,
                    best_practices, lessons, sources_catalog,
                    intention=intention,
                ),
            }],
        )

        raw = message.content[0].text.strip()
        try:
            skill_result = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{[\s\S]+\}", raw)
            skill_result = json.loads(m.group(0)) if m else None
        if not skill_result:
            raise ValueError("Could not parse Claude response as JSON")

        # If Claude flagged unresolvable contradictions, pause for user input
        questions = skill_result.get("questions", [])
        if questions:
            log(f"Found {len(questions)} conflict(s) that need your input", "warn")
            jobs[job_id].update({
                "status": "needs_input",
                "result": skill_result,
                "questions": questions,
                "combined_content": combined,
                "source_title": source_title,
                "source_url": source_url,
            })
            q.put({"type": "needs_input", "questions": questions})
            return  # Wait for user answers via /answer endpoint

        # No questions — save and finish
        _finalize_skill(job_id, skill_result, q)

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
        log(f"Error: {e}", "error")
        q.put({"type": "complete", "skill_name": None})


def _write_watchlist(target_dir: Path, sources: list, intention: str, target_type: str, name: str):
    """Write watchlist.json and initial CHANGELOG.md if any sources have a schedule."""
    from datetime import date as _date
    recurring = [s for s in sources if s.get("schedule") and s["schedule"] != "never"]
    watchlist = {
        "name": name,
        "type": target_type,
        "intention": intention,
        "sources": [
            {
                "url": s["url"],
                "schedule": s.get("schedule", "never"),
                "auto_accept": False,
                "last_fetched": _date.today().isoformat(),
            }
            for s in sources
        ],
        "created": _date.today().isoformat(),
        "last_updated": _date.today().isoformat(),
    }
    (target_dir / "watchlist.json").write_text(
        json.dumps(watchlist, indent=2), encoding="utf-8"
    )
    # Initial changelog
    cl = target_dir / "CHANGELOG.md"
    source_lines = "\n".join(
        f"- {s['url']}" + (f" ({s['schedule']})" if s.get("schedule") and s["schedule"] != "never" else "")
        for s in sources
    )
    cl.write_text(
        f"# {name} — Changelog\n\n"
        f"## {_date.today().isoformat()} (created)\n"
        f"Initial build from:\n{source_lines}\n",
        encoding="utf-8",
    )


def _finalize_skill(job_id, skill_result, q):
    """Save skill to disk, build zip, mark done."""
    from generate_skill import save_skill
    tmpdir = Path(tempfile.mkdtemp())
    skill_dir = save_skill(skill_result, tmpdir)

    # Write watchlist.json + CHANGELOG.md if recurring sources exist
    job = jobs[job_id]
    watch_sources = job.get("watch_sources", [])
    intention = job.get("intention", "")
    skill_name = skill_result["skill_name"]
    if watch_sources:
        _write_watchlist(skill_dir, watch_sources, intention, "skill", skill_name)

    zip_path = tmpdir / f"{skill_name}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in skill_dir.rglob("*"):
            if file.is_file():
                zf.write(file, file.relative_to(tmpdir))

    jobs[job_id].update({
        "status": "done",
        "result": skill_result,
        "zip_path": str(zip_path),
        "skill_dir": str(skill_dir),
        "skill_name": skill_name,
    })

    q = jobs[job_id]["queue"]
    q.put({"type": "complete", "skill_name": skill_name})


# ──────────────────────────────────────────────
# Persona background worker
# ──────────────────────────────────────────────

def run_build_persona(job_id, sources, max_videos, raw_text, intention=""):
    q = jobs[job_id]["queue"]

    def log(msg, kind="info"):
        q.put({"type": kind, "message": msg})

    try:
        from fetch_content import fetch
        from generate_persona import generate_persona

        all_parts = []

        for url in sources:
            if jobs[job_id].get("cancelled"):
                return
            log(f"Fetching: {url}")
            try:
                def _fwd_p(msg):
                    if jobs[job_id].get("cancelled"):
                        return
                    q.put(msg)
                result = fetch(url, max_videos=max_videos, verbose=False,
                               intention=intention, log_fn=_fwd_p)
                all_parts.append({
                    "title": result["title"],
                    "content": result["content"],
                    "url": url,
                    "video_count": result.get("video_count", 1),
                })
                vids = result.get("video_count", 1)
                chars = len(result["content"])
                label = f"{vids} videos, {chars:,} chars" if vids > 1 else f"{chars:,} chars"
                log(f"Got: {result['title']} ({label})", "success")
            except Exception as e:
                log(f"Failed: {url} — {e}", "error")

        if raw_text:
            all_parts.append({
                "title": "Uploaded Content",
                "content": raw_text,
                "url": "",
                "video_count": 1,
            })
            log(f"Got: Uploaded content ({len(raw_text):,} chars)", "success")

        if not all_parts:
            raise ValueError("No content was successfully fetched. Check URLs and try again.")

        if jobs[job_id].get("cancelled"):
            return

        if len(all_parts) == 1:
            combined = all_parts[0]["content"]
            source_title = all_parts[0]["title"]
            source_url = all_parts[0]["url"]
        else:
            parts_text = []
            for p in all_parts:
                header = f"# {p['title']}"
                if p["url"]:
                    header += f"\nSource: {p['url']}"
                parts_text.append(f"{header}\n\n{p['content']}")
            combined = "\n\n---\n\n".join(parts_text)
            source_title = "Combined: " + ", ".join(p["title"][:25] for p in all_parts[:3])
            source_url = sources[0] if sources else ""

        log("Generating persona with Claude Opus 4.6...")
        result = generate_persona(combined, source_title, source_url, intention=intention)

        persona_name = result["persona_name"]

        # Save persona to ~/.claude/personas/<slug>/
        import re as _re
        slug = _re.sub(r"[^a-z0-9]+", "-", persona_name.lower()).strip("-")
        persona_root = Path.home() / ".claude" / "personas" / slug
        persona_root.mkdir(parents=True, exist_ok=True)
        persona_file = persona_root / f"{slug}.md"
        persona_file.write_text(result["persona_md"], encoding="utf-8")

        # Write watchlist + changelog if recurring sources
        watch_sources = jobs[job_id].get("watch_sources", [])
        intention_val = jobs[job_id].get("intention", "")
        if watch_sources:
            _write_watchlist(persona_root, watch_sources, intention_val, "persona", slug)
        else:
            # Always write a basic changelog
            from datetime import date as _date
            cl = persona_root / "CHANGELOG.md"
            cl.write_text(
                f"# {persona_name} — Changelog\n\n"
                f"## {_date.today().isoformat()} (created)\n"
                f"Initial build from: {source_url or source_title}\n",
                encoding="utf-8",
            )

        jobs[job_id].update({
            "status": "done",
            "result": result,
            "skill_name": persona_name,
            "persona_dir": str(persona_root),
        })
        q.put({"type": "complete", "skill_name": persona_name})

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
        log(f"Error: {e}", "error")
        q.put({"type": "complete", "skill_name": None})


# ──────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────

@app.route("/")
def landing():
    return render_template("landing.html")


@app.route("/app")
def index():
    return render_template("index.html")


@app.route("/build", methods=["POST"])
def build():
    if not os.getenv("ANTHROPIC_API_KEY"):
        return jsonify({"error": "ANTHROPIC_API_KEY is not set. Export it in your terminal before starting this app."}), 400

    data = request.json
    sources = [s.strip() for s in data.get("sources", []) if s.strip()]
    raw_text = data.get("raw_text", "").strip()
    intention = data.get("intention", "").strip()
    max_videos = int(data.get("max_videos", 50))
    files = data.get("files", [])  # [{name, content}]
    # watch_sources: [{url, schedule}] — sources with recurring schedules
    watch_sources = data.get("watch_sources", [])

    # Merge uploaded file content into raw_text
    if files:
        file_blocks = [f"# {f['name']}\n\n{f['content']}" for f in files]
        if raw_text:
            file_blocks.append(raw_text)
        raw_text = "\n\n---\n\n".join(file_blocks)

    if not sources and not raw_text:
        return jsonify({"error": "Add at least one URL, file, or paste some text."}), 400

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "status": "running",
        "queue": queue.Queue(),
        "result": None, "zip_path": None,
        "skill_dir": None, "skill_name": None, "error": None,
        "cancelled": False,
        "watch_sources": watch_sources,
        "intention": intention,
    }

    t = threading.Thread(target=run_build, args=(job_id, sources, max_videos, raw_text, intention))
    t.daemon = True
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/build-persona", methods=["POST"])
def build_persona_route():
    if not os.getenv("ANTHROPIC_API_KEY"):
        return jsonify({"error": "ANTHROPIC_API_KEY is not set. Export it in your terminal before starting this app."}), 400

    data = request.json
    sources = [s.strip() for s in data.get("sources", []) if s.strip()]
    raw_text = data.get("raw_text", "").strip()
    intention = data.get("intention", "").strip()
    max_videos = int(data.get("max_videos", 50))
    files = data.get("files", [])
    watch_sources = data.get("watch_sources", [])

    if files:
        file_blocks = [f"# {f['name']}\n\n{f['content']}" for f in files]
        if raw_text:
            file_blocks.append(raw_text)
        raw_text = "\n\n---\n\n".join(file_blocks)

    if not sources and not raw_text:
        return jsonify({"error": "Add at least one URL, file, or paste some text."}), 400

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "status": "running",
        "queue": queue.Queue(),
        "result": None, "zip_path": None,
        "skill_dir": None, "skill_name": None, "error": None,
        "job_type": "persona",
        "cancelled": False,
        "watch_sources": watch_sources,
        "intention": intention,
    }

    t = threading.Thread(target=run_build_persona, args=(job_id, sources, max_videos, raw_text, intention))
    t.daemon = True
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/stream/<job_id>")
def stream(job_id):
    if job_id not in jobs:
        return "Not found", 404

    def generate():
        q = jobs[job_id]["queue"]
        while True:
            try:
                msg = q.get(timeout=120)
                yield f"data: {json.dumps(msg)}\n\n"
                if msg.get("type") == "complete":
                    break
            except queue.Empty:
                yield "data: {\"type\":\"ping\"}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/result/<job_id>")
def result(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    r = job.get("result") or {}
    is_persona = job.get("job_type") == "persona"
    return jsonify({
        "status": job["status"],
        "job_type": job.get("job_type", "skill"),
        "skill_name": job.get("skill_name"),
        "skill_md": r.get("skill_md") if not is_persona else None,
        "sources_md": r.get("sources_md") if not is_persona else None,
        "persona_name": r.get("persona_name") if is_persona else None,
        "persona_md": r.get("persona_md") if is_persona else None,
        "topics_covered": r.get("topics_covered") if is_persona else None,
        "summary": r.get("summary"),
        "questions": job.get("questions", []),
        "error": job.get("error"),
    })


@app.route("/answer/<job_id>", methods=["POST"])
def answer(job_id):
    """Receive user answers to contradiction questions, regenerate skill with context."""
    job = jobs.get(job_id)
    if not job or job["status"] != "needs_input":
        return jsonify({"error": "Job not in needs_input state"}), 400

    answers = request.json.get("answers", [])  # [{question, answer}]

    # Build answers context string
    answers_text = "\n".join(
        f"Q: {a['question']}\nA: {a['answer']}" for a in answers
    )

    # Re-run generation with answers injected
    job["status"] = "running"
    job["questions"] = []

    def regenerate():
        q = job["queue"]
        try:
            from generate_skill import SYSTEM_PROMPT, build_user_message, read_knowledge_base
            import anthropic

            q.put({"type": "info", "message": "Regenerating skill with your answers..."})
            best_practices, lessons, sources_catalog = read_knowledge_base()

            # Inject answers into the user message
            base_msg = build_user_message(
                job["combined_content"], job["source_title"], job["source_url"],
                best_practices, lessons, sources_catalog,
            )
            msg_with_answers = base_msg + f"\n\n## User Answers to Contradictions\n{answers_text}\n\nApply these answers when resolving the conflicts you identified."

            client = anthropic.Anthropic()
            message = client.messages.create(
                model="claude-opus-4-6",
                max_tokens=6000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": msg_with_answers}],
            )
            raw = message.content[0].text.strip()
            try:
                skill_result = json.loads(raw)
            except json.JSONDecodeError:
                m = re.search(r"\{[\s\S]+\}", raw)
                skill_result = json.loads(m.group(0)) if m else None
            if not skill_result:
                raise ValueError("Could not parse regenerated skill")

            job["result"] = skill_result
            q.put({"type": "success", "message": f"Skill updated with your answers"})
            _finalize_skill(job_id, skill_result, q)

        except Exception as e:
            job["status"] = "error"
            job["error"] = str(e)
            q.put({"type": "error", "message": str(e)})
            q.put({"type": "complete", "skill_name": None})

    t = threading.Thread(target=regenerate)
    t.daemon = True
    t.start()

    return jsonify({"ok": True})


@app.route("/append/<job_id>", methods=["POST"])
def append_source(job_id):
    """Fetch a new URL and merge it into an existing skill."""
    job = jobs.get(job_id)
    if not job or job["status"] != "done" or job.get("job_type") == "persona":
        return jsonify({"error": "No completed skill found for this session."}), 400

    data = request.json
    url = data.get("url", "").strip()
    max_videos = int(data.get("max_videos", 50))
    if not url:
        return jsonify({"error": "No URL provided."}), 400

    try:
        from fetch_content import fetch

        result = fetch(url, max_videos=max_videos, verbose=False)
        new_content = result["content"]
        source_title = result["title"]

        MAX = 40_000
        if len(new_content) > MAX:
            new_content = new_content[:MAX] + "\n\n[...truncated...]"

        existing_skill_md = job["result"]["skill_md"]

        import anthropic, re as _re
        APPEND_PROMPT = """\
You are updating an existing Claude Code skill with new content.

Your job:
1. Identify genuinely new information, techniques, or updated recommendations in the new content
2. Update the existing skill to incorporate it — revise sections, add bullets, update examples
3. Do NOT bloat the skill — remove outdated information if the new content supersedes it
4. Keep the skill under 400 lines
5. Return the complete updated SKILL.md text only — no JSON, no preamble

If the new content adds nothing meaningful, return the existing skill unchanged.
"""
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=6000,
            system=APPEND_PROMPT,
            messages=[{"role": "user", "content":
                f"## Existing SKILL.md\n\n{existing_skill_md}\n\n"
                f"---\n\n## New Content from: {source_title}\nURL: {url}\n\n{new_content}"
            }],
        )
        updated_skill_md = msg.content[0].text.strip()

        # Update in-memory result
        job["result"]["skill_md"] = updated_skill_md

        # Rebuild zip with updated skill
        from generate_skill import save_skill
        tmpdir = Path(tempfile.mkdtemp())
        skill_result = dict(job["result"])
        skill_dir = save_skill(skill_result, tmpdir)
        zip_path = tmpdir / f"{job['skill_name']}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in skill_dir.rglob("*"):
                if file.is_file():
                    zf.write(file, file.relative_to(tmpdir))
        job["zip_path"] = str(zip_path)
        job["skill_dir"] = str(skill_dir)

        return jsonify({"skill_md": updated_skill_md})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/cancel/<job_id>", methods=["POST"])
def cancel(job_id):
    job = jobs.get(job_id)
    if job:
        job["cancelled"] = True
        job["status"] = "cancelled"
    return jsonify({"ok": True})


@app.route("/download/<job_id>/<file_type>")
def download(job_id, file_type):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return "Not ready", 404

    if file_type == "zip":
        return send_file(
            job["zip_path"], as_attachment=True,
            download_name=f"{job['skill_name']}.zip",
        )
    elif file_type == "skill_md":
        content = job["result"]["skill_md"].encode("utf-8")
        return send_file(
            io.BytesIO(content), as_attachment=True,
            download_name="SKILL.md", mimetype="text/markdown",
        )
    elif file_type == "sources_md":
        content = job["result"]["sources_md"].encode("utf-8")
        return send_file(
            io.BytesIO(content), as_attachment=True,
            download_name="sources.md", mimetype="text/markdown",
        )
    elif file_type == "persona_md":
        content = job["result"]["persona_md"].encode("utf-8")
        name = (job["result"].get("persona_name") or "persona").replace(" ", "-") + ".md"
        return send_file(
            io.BytesIO(content), as_attachment=True,
            download_name=name, mimetype="text/markdown",
        )
    return "Unknown type", 404


@app.route("/parse-file", methods=["POST"])
def parse_file():
    """Accept a binary file upload and return extracted plain text."""
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file provided"}), 400

    filename = f.filename or ""
    ext = Path(filename).suffix.lower()

    try:
        if ext in (".txt", ".md"):
            text = f.read().decode("utf-8", errors="ignore")

        elif ext == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(f)
            text = "\n".join(page.extract_text() or "" for page in reader.pages)

        elif ext in (".docx",):
            from docx import Document
            doc = Document(f)
            text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())

        elif ext in (".pptx",):
            from pptx import Presentation
            prs = Presentation(f)
            parts = []
            for slide in prs.slides:
                for shape in slide.shapes:
                    if hasattr(shape, "text") and shape.text.strip():
                        parts.append(shape.text.strip())
            text = "\n\n".join(parts)

        elif ext in (".xlsx", ".xls"):
            import openpyxl, anthropic as _anthropic
            wb = openpyxl.load_workbook(f, data_only=False)
            sections = []
            all_formulas = []

            for sheet in wb.worksheets:
                rows_text = []
                sheet_formulas = []

                for row in sheet.iter_rows():
                    row_vals = []
                    for cell in row:
                        val = cell.value
                        if val is None:
                            row_vals.append("")
                        elif isinstance(val, str) and val.startswith("="):
                            placeholder = f"[FORMULA:{cell.coordinate}]"
                            row_vals.append(placeholder)
                            sheet_formulas.append({
                                "cell": cell.coordinate,
                                "formula": val,
                                "sheet": sheet.title,
                            })
                        else:
                            row_vals.append(str(val))
                    # Skip fully empty rows
                    if any(v for v in row_vals):
                        rows_text.append(" | ".join(row_vals))

                section = f"## Sheet: {sheet.title}\n" + "\n".join(rows_text)
                sections.append(section)
                all_formulas.extend(sheet_formulas)

            raw_text = "\n\n".join(sections)

            # Ask Claude to explain all formulas in one call
            if all_formulas:
                formula_list = "\n".join(
                    f"- {f['sheet']}!{f['cell']}: {f['formula']}" for f in all_formulas
                )
                client = _anthropic.Anthropic()
                msg = client.messages.create(
                    model="claude-opus-4-6",
                    max_tokens=2000,
                    messages=[{"role": "user", "content":
                        f"Explain each of these spreadsheet formulas in plain English. "
                        f"Be specific about what each one calculates and why it's useful. "
                        f"Format as a list with the cell reference, then the explanation.\n\n{formula_list}"
                    }],
                )
                formula_explanations = msg.content[0].text.strip()
                text = raw_text + f"\n\n## Formula Explanations\n\n{formula_explanations}"
            else:
                text = raw_text

        else:
            return jsonify({"error": f"Unsupported file type: {ext}"}), 400

        return jsonify({"name": filename, "content": text, "chars": len(text)})

    except Exception as e:
        return jsonify({"error": f"Could not parse {filename}: {e}"}), 500


@app.route("/install/<job_id>", methods=["POST"])
def install(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Job not ready"}), 400

    src = Path(job["skill_dir"])
    dest = Path.home() / ".claude" / "skills" / job["skill_name"]
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)

    return jsonify({
        "success": True,
        "installed_to": str(dest),
        "message": f"Installed to {dest}. Restart Claude Code to activate.",
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5173))
    print(f"\n  Skill Builder → http://localhost:{port}\n")
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("  WARNING: ANTHROPIC_API_KEY not set\n")
    app.run(host="0.0.0.0", debug=False, port=port, threaded=True)
