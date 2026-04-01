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

def run_build(job_id, sources, max_videos, raw_text):
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
            log(f"Fetching: {url}")
            try:
                result = fetch(url, max_videos=max_videos, verbose=False)
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

        # Save skill to temp dir
        tmpdir = Path(tempfile.mkdtemp())
        skill_dir = save_skill(skill_result, tmpdir)

        # Build zip
        zip_path = tmpdir / f"{skill_result['skill_name']}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in skill_dir.rglob("*"):
                if file.is_file():
                    zf.write(file, file.relative_to(tmpdir))

        jobs[job_id].update({
            "status": "done",
            "result": skill_result,
            "zip_path": str(zip_path),
            "skill_dir": str(skill_dir),
            "skill_name": skill_result["skill_name"],
        })

        log(f"Skill '{skill_result['skill_name']}' ready!", "success")
        q.put({"type": "complete", "skill_name": skill_result["skill_name"]})

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
        log(f"Error: {e}", "error")
        q.put({"type": "complete", "skill_name": None})


# ──────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/build", methods=["POST"])
def build():
    if not os.getenv("ANTHROPIC_API_KEY"):
        return jsonify({"error": "ANTHROPIC_API_KEY is not set. Export it in your terminal before starting this app."}), 400

    data = request.json
    sources = [s.strip() for s in data.get("sources", []) if s.strip()]
    raw_text = data.get("raw_text", "").strip()
    max_videos = int(data.get("max_videos", 50))
    files = data.get("files", [])  # [{name, content}]

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
    }

    t = threading.Thread(target=run_build, args=(job_id, sources, max_videos, raw_text))
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
    return jsonify({
        "status": job["status"],
        "skill_name": job.get("skill_name"),
        "skill_md": job.get("result", {}).get("skill_md") if job.get("result") else None,
        "sources_md": job.get("result", {}).get("sources_md") if job.get("result") else None,
        "summary": job.get("result", {}).get("summary") if job.get("result") else None,
        "error": job.get("error"),
    })


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
    return "Unknown type", 404


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
