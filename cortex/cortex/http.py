"""HTTP management surface (Phase 3a.1).

Runs alongside the WSS Glass endpoint on a separate port. Read-only by default,
with a few POST routes that proxy to the existing Tool Agent dispatch path.

Bind: tailnet IP (same as WSS) — security boundary IS the tailnet. Anyone on
Zack's tailnet (per Tailscale ACL) can hit these endpoints; in v1 that's just
the Linux edge proxy + Zack's own devices.

Priority routes (per Zack's emphasis on "see what the agent is running"):
  - GET  /api/cc/sessions             list Track A (claude -p) CC sessions
  - GET  /api/cc-archive              on-disk CC jsonl archive (list + detail)
  - SSE  /api/trace/stream            live event/router/dispatch stream

Twin / receipts / LLM inspector / dispatch log endpoints round out the picture.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from aiohttp import web

from . import ids
from .control_plane import ControlPlane
from .router import AVAILABLE_TOOLS
from .schema import Event

log = structlog.get_logger(__name__)


# ── helpers ────────────────────────────────────────────────────────────────

def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _json(payload: Any, status: int = 200) -> web.Response:
    return web.json_response(payload, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False))


def _err(msg: str, status: int = 400) -> web.Response:
    return _json({"error": msg}, status=status)


def _twin_path_safe(twin_root: Path, rel: str) -> Path | None:
    """Reject any path that escapes twin_root via .. or absolute."""
    p = (twin_root / rel).resolve()
    try:
        p.relative_to(twin_root.resolve())
        return p
    except ValueError:
        return None


# ── route definitions ──────────────────────────────────────────────────────

def make_app(plane: ControlPlane) -> web.Application:
    app = web.Application(client_max_size=10 * 1024 * 1024)  # 10 MB cap for twin writes

    # CORS preflight — tailnet trust; permissive (edge will add user auth)
    @web.middleware
    async def cors_mw(request: web.Request, handler):
        if request.method == "OPTIONS":
            return web.Response(status=204, headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization",
                "Access-Control-Max-Age": "600",
            })
        resp = await handler(request)
        resp.headers.setdefault("Access-Control-Allow-Origin", "*")
        return resp

    app.middlewares.append(cors_mw)

    # ── liveness ──
    async def health(_request: web.Request) -> web.Response:
        return _json({
            "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "stats": plane.stats(),
            "server_bound": bool(plane.server and plane.server._glass_conn is not None),
            "tool_conn": bool(plane.server and plane.server._tool_conn is not None),
        })

    async def ping(_request: web.Request) -> web.Response:
        """Lightweight liveness probe for the in-app TEST CONNECTION button
        (P-app.A D7). Doesn't touch the router / dispatcher / LLM cache —
        just confirms the HTTP server + the tool_agent socket are alive.
        Returns the same shape as /api/health minus the stats block (which
        the in-app UI doesn't need for a single click)."""
        return _json({
            "ok": True,
            "ts": datetime.now(timezone.utc).isoformat(),
            "server_bound": bool(plane.server and plane.server._glass_conn is not None),
            "tool_conn": bool(plane.server and plane.server._tool_conn is not None),
        })

    # ── claude_code (Track A: `claude -p` session list) ──
    async def cc_sessions(_request: web.Request) -> web.Response:
        if plane.server is None:
            return _err("server not bound", 503)
        try:
            track_a = await plane.server._dispatch_to_tool({
                "tool": "claude_code", "action": "list_sessions",
                "args": {}, "result_format": "query",
            })
            return _json({"track_a": track_a.result})
        except Exception as e:
            return _err(f"dispatch failed: {e}", 500)

    # ── twin browser ──
    async def twin_tree(_request: web.Request) -> web.Response:
        if plane.twin is None:
            return _err("twin not bound", 503)
        root = plane.twin.root
        items = []
        for p in sorted(root.rglob("*")):
            if p.is_dir():
                continue
            # Skip dotfiles and __pycache__ etc.
            if any(part.startswith(".") for part in p.relative_to(root).parts):
                continue
            rel = str(p.relative_to(root))
            st = p.stat()
            items.append({
                "path": rel,
                "size_bytes": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            })
        return _json({"root": str(root), "items": items})

    async def twin_read(request: web.Request) -> web.Response:
        rel = request.query.get("path")
        if not rel:
            return _err("path required")
        target = _twin_path_safe(plane.twin.root, rel)
        if not target or not target.exists():
            return _err("not found", 404)
        if not target.is_file():
            return _err("not a file")
        return _json({
            "path": rel,
            "content": target.read_text(encoding="utf-8"),
            "size_bytes": target.stat().st_size,
        })

    async def twin_write(request: web.Request) -> web.Response:
        body = await request.json()
        rel = body.get("path")
        content = body.get("content")
        if not rel or content is None:
            return _err("path + content required")
        target = _twin_path_safe(plane.twin.root, rel)
        if not target:
            return _err("path escapes twin root")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        plane.twin.changelog_append(
            summary=f"twin/{rel} edited via console",
            src="console",
            details=[f"{len(content)} chars written"],
        )
        return _json({"ok": True, "path": rel, "size_bytes": target.stat().st_size})

    # ── receipts / changelog ──
    async def receipts(request: web.Request) -> web.Response:
        date = request.query.get("date") or _today_str()
        path = plane.twin.root / "receipts" / f"{date}.md"
        if not path.exists():
            return _json({"date": date, "content": "", "exists": False})
        return _json({
            "date": date,
            "content": path.read_text(encoding="utf-8"),
            "exists": True,
            "mtime": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
        })

    async def changelog(request: web.Request) -> web.Response:
        limit = int(request.query.get("limit", 100))
        path = plane.twin.root / "CHANGELOG.md"
        if not path.exists():
            return _json({"entries": [], "total": 0})
        text = path.read_text(encoding="utf-8")
        # CHANGELOG entries are blocks separated by blank lines starting with `##`
        blocks = [b.strip() for b in text.split("\n\n") if b.strip().startswith("##")]
        tail = blocks[-limit:]
        return _json({"entries": tail, "total": len(blocks)})

    # ── tasks / dispatches / events / llm ──
    async def tasks_active(_request: web.Request) -> web.Response:
        srv = plane.server
        if srv is None:
            return _err("server not bound", 503)
        out = []
        for cmd_id, pending in srv._pending_previews.items():
            plan = pending.get("plan", {})
            evt = pending.get("event")
            out.append({
                "cmd_id": cmd_id,
                "event_id": getattr(evt, "id", None),
                "event_kind": getattr(evt, "kind", None),
                "primary_intent": plan.get("primary_intent"),
                "n_subtasks": len(plan.get("subtasks", [])),
                "hud_kind": plan.get("hud_response", {}).get("kind"),
                "hud_title": plan.get("hud_response", {}).get("title"),
                "is_checkpoint": bool(pending.get("is_checkpoint")),
                "is_reverse_wake": bool(pending.get("wake_response_map")),
            })
        return _json({"active": out})

    async def events_list(request: web.Request) -> web.Response:
        limit = int(request.query.get("limit", 50))
        since = request.query.get("since")
        return _json({"events": plane.list_events(limit=limit, since_ts=since)})

    async def dispatches_list(request: web.Request) -> web.Response:
        limit = int(request.query.get("limit", 100))
        since = request.query.get("since")
        tool = request.query.get("tool")
        return _json({"dispatches": plane.list_dispatches(limit=limit, since_ts=since, tool=tool)})

    async def llm_calls_list(request: web.Request) -> web.Response:
        limit = int(request.query.get("limit", 50))
        since = request.query.get("since")
        calls = plane.list_llm_calls(limit=limit, since_ts=since)
        # Strip messages from list view (saves bandwidth); full content via /api/llm/calls/{id}
        for c in calls:
            c.pop("messages", None)
            c.pop("response", None)
        return _json({"calls": calls})

    async def llm_call_detail(request: web.Request) -> web.Response:
        call_id = request.match_info["call_id"]
        c = plane.get_llm_call(call_id)
        if not c:
            return _err("not found", 404)
        return _json(c)

    async def llm_stats(_request: web.Request) -> web.Response:
        from .llm_cache import cache_stats
        s = plane.stats()
        try:
            cs = cache_stats()
        except Exception as e:
            cs = {"error": str(e)}
        return _json({"plane": s, "diskcache": cs})

    # ── adapters / system ──
    async def adapters_list(_request: web.Request) -> web.Response:
        enabled = plane.server._allowed_tools if plane.server else set()
        out = []
        for name, info in AVAILABLE_TOOLS.items():
            out.append({
                "name": name,
                "actions": info["actions"],
                "description": info["description"],
                "enabled": name in enabled,
            })
        return _json({"adapters": out})

    async def system_status(_request: web.Request) -> web.Response:
        try:
            rpc = await plane.server._dispatch_to_tool({
                "tool": "system_status", "action": "get",
                "args": {}, "result_format": "query",
            })
            return _json(rpc.result)
        except Exception as e:
            return _err(f"dispatch failed: {e}", 500)

    # ── test injection (developer aid) ──
    async def test_invoke(request: web.Request) -> web.Response:
        """Inject a synthetic user_invoke as if it came from Glass.

        This is the "Manual Plan / Replay" affordance — type a text intent in the
        console and see the full pipeline run. Bypasses the Glass WSS but goes
        through the same _process_event path.
        """
        body = await request.json()
        text = body.get("text", "")
        image = body.get("image")  # base64 if provided
        session_id = body.get("session_id")  # P1.9 / R-14: route to specific session for testing
        if not text and not image:
            return _err("text or image required")
        payload = {"text": text}
        if image:
            payload["image"] = image
        if session_id:
            payload["session_id"] = session_id
        mode = body.get("mode")  # People-Recall: face_recall | enroll_person
        if mode:
            payload["mode"] = mode
        event = Event(
            id=ids.event_id(),
            kind="user_invoke",
            ts=datetime.now(timezone.utc),
            payload=payload,
        )
        # Fire-and-return: pipeline runs async, console watches /api/trace/stream
        asyncio.create_task(plane.server._process_event(event))
        return _json({"ok": True, "event_id": event.id})

    # (Phase 5c refactor: the agent card-building helper moved to
    # CortexServer._send_agent_card_for_decision so it's shared with the
    # _handle_user_decision resume path. The dev endpoint above calls
    # plane.server._dispatch_complex_agent which now drives the whole flow.)

    # ── dev/test helpers ──
    async def dev_agent_invoke(request: web.Request) -> web.Response:
        """Dispatch an agentic task via the in-process SDK agent
        (_dispatch_complex_agent) directly, bypassing the Router. Used to test
        the streaming-progress + mid-flight-correction pipeline before the full
        Router classifier (Phase 5c) lands.

        POST body: {
          "text": "<user ask>",                  # what Zack said
          "output_schema_hint": <dict | str>?,   # optional shape for CC
          "add_dirs": [paths]?,                  # optional --add-dir paths
          "working_dir": "~/path"?,              # CC's cwd
        }

        Returns immediately with the synthesised parent_event_id; the agent
        runs in the background, streaming progress to Glass. The final
        preview card lands when CC finishes.
        """
        body = await request.json()
        text = (body.get("text") or "").strip()
        if not text:
            return _err("text required")
        if plane.server is None:
            return _err("server not bound", 503)

        # Synthesise an event so the agent's progress events have a
        # parent_event_id; we record it into the events ring too so /api/trace
        # picks it up.
        _payload = {"text": text, "_via": "dev_agent_invoke"}
        if body.get("image"):  # UC1: allow attaching a base64 photo for testing
            _payload["image"] = body["image"]
        event = Event(
            id=ids.event_id(),
            kind="user_invoke",
            ts=datetime.now(timezone.utc),
            payload=_payload,
        )
        plane.record_event(
            event_id=event.id, kind=event.kind,
            payload=event.payload or {}, source="dev_endpoint",
        )

        async def _run_agent() -> None:
            try:
                # Phase 5c refactor: the agent dispatch + card-building logic
                # now lives on CortexServer so /api/dev/agent_invoke and the
                # classifier's "complex" branch share the exact same code path.
                await plane.server._dispatch_complex_agent(
                    event,
                    add_dirs=body.get("add_dirs"),
                    working_dir=body.get("working_dir"),
                    timeout_s=float(body.get("timeout_s", 240)),
                    output_schema=body.get("output_schema_hint"),
                )
            except Exception as e:
                log.error("dev_agent_invoke.failed", error=str(e), exc_info=True)

        asyncio.create_task(_run_agent())
        return _json({"ok": True, "event_id": event.id})

    async def dev_distill_now(request: web.Request) -> web.Response:
        """Force the auto-distiller to dispatch RIGHT NOW, bypassing the
        cooldown + min-modify threshold. Used to dogfood-test the distill
        path; also useful as a user-facing 'review my Twin now' action.
        Returns immediately; distillation runs in the background and
        surfaces a card via HUD if it proposes anything.
        """
        if plane.server is None:
            return _err("server not bound", 503)
        if not getattr(plane.server, "distiller", None):
            return _err("distiller not initialised", 503)
        result = plane.server.distiller.force_run()
        return _json(result)

    async def dev_capture_image(request: web.Request) -> web.Response:
        """Directly trigger ONE glass camera capture (request_image) with a long
        timeout, in isolation — to test the camera. Returns whether a usable image
        came back, its b64 size, and how long the capture actually took."""
        if plane.server is None:
            return _err("server not bound", 503)
        srv = plane.server
        if not srv._glass_conn:
            return _err("no glass connected (server_bound false)", 503)
        try:
            body = await request.json()
        except Exception:
            body = {}
        timeout_s = float(body.get("timeout_s", 30.0))
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        img = await srv._request_image_from_glass(
            parent_event_id=ids.event_id(), hint="dev camera test",
            timeout_s=timeout_s,
        )
        return _json({
            "ok": True, "has_image": bool(img),
            "bytes_b64": len(img) if img else 0,
            "elapsed_s": round(loop.time() - t0, 2),
        })

    async def dev_face_recall(request: web.Request) -> web.Response:
        """People-Recall: run a deterministic face match directly (no glass, no
        LLM) and return the result synchronously. POST {image: <base64 jpeg>}.
        Used to verify the engine + gallery wiring offline. The real glass path
        goes through /api/test/invoke with mode=face_recall."""
        if plane.server is None:
            return _err("server not bound", 503)
        fi = getattr(plane.server, "face_index", None)
        if fi is None or not fi.available():
            return _err("face index unavailable (deps not installed?)", 503)
        body = await request.json()
        img_b64 = body.get("image")
        if not img_b64:
            return _err("image (base64 jpeg) required")
        import base64 as _b64
        try:
            img = _b64.b64decode(img_b64)
        except Exception as e:
            return _err(f"bad base64: {e}")
        entry, score = await asyncio.get_event_loop().run_in_executor(
            None, fi.match, img)
        return _json({
            "ok": True,
            "match": entry,            # None if no match / no face
            "score": round(float(score), 4),
            "no_face": score < 0,
            "gallery_size": len(fi._entries),
        })

    async def dev_face_enroll(request: web.Request) -> web.Response:
        """People-Recall dev: enroll a face directly into the gallery (skips the
        voice/STT bio). POST {image, name, recall?, slug?}. For seeding a test
        gallery offline; the real path is the enroll shortcut + spoken bio."""
        if plane.server is None:
            return _err("server not bound", 503)
        fi = getattr(plane.server, "face_index", None)
        if fi is None or not fi.available():
            return _err("face index unavailable", 503)
        body = await request.json()
        img_b64 = body.get("image")
        name = (body.get("name") or "").strip()
        if not img_b64 or not name:
            return _err("image + name required")
        from .enroll_parser import slugify
        slug = (body.get("slug") or slugify(name)).strip()
        recall = (body.get("recall") or name).strip()
        import base64 as _b64
        img = _b64.b64decode(img_b64)
        entry = await asyncio.get_event_loop().run_in_executor(
            None, lambda: fi.enroll(
                img, slug=slug, name=name, recall=recall,
                profile_relpath=f"people/encounters/{slug}.md"))
        if entry is None:
            return _err("no face detected in image", 422)
        return _json({"ok": True, "entry": entry, "gallery_size": len(fi._entries)})

    async def dev_insight_tick(request: web.Request) -> web.Response:
        """Force one tick of the Insight Engine, bypassing cron schedule
        + global cooldown. Used to dogfood-test insight providers; surfaces
        any matching insight via the standard hud_show path.

        Returns immediately; the tick runs in the background.
        """
        if plane.server is None:
            return _err("server not bound", 503)
        engine = getattr(plane.server, "insight_engine", None)
        if engine is None:
            return _err("insight engine not initialised", 503)
        # Bypass the global cooldown for the manual tick.
        engine._global_last = None

        async def _bg_tick() -> None:
            try:
                await engine._tick()
            except Exception as e:
                log.warning("dev_insight_tick.failed", error=str(e), exc_info=True)

        asyncio.create_task(_bg_tick())
        return _json({"ok": True, "queued": True, "n_providers": len(engine._providers),
                      "enabled": engine.enabled})

    # ── SSE trace stream ──
    async def trace_stream(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # disable nginx buffering if proxied
                "Access-Control-Allow-Origin": "*",
            },
        )
        await resp.prepare(request)
        # Optional ?kinds=event,llm_call,dispatch  to filter
        kinds_q = request.query.get("kinds")
        kinds = set(kinds_q.split(",")) if kinds_q else None  # type: ignore[arg-type]
        sub = plane.subscribe(kinds=kinds)  # type: ignore[arg-type]
        try:
            # Initial hello so client knows the stream is live
            await resp.write(b": connected\n\n")
            while True:
                try:
                    msg = await asyncio.wait_for(sub.queue.get(), timeout=15.0)
                    data = json.dumps(msg, ensure_ascii=False, default=str)
                    await resp.write(f"event: {msg['kind']}\ndata: {data}\n\n".encode("utf-8"))
                except asyncio.TimeoutError:
                    # keepalive comment per SSE spec
                    await resp.write(b": ping\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            log.info("trace_stream.client_gone")
        finally:
            plane.unsubscribe(sub)
        return resp

    # ── HUD sessions (conversation threads) ──
    async def sessions_list(request: web.Request) -> web.Response:
        if plane.server is None or plane.server.sessions is None:
            return _json({"sessions": []})
        # P2.1 — optional ?status=active|archived|killed|all (default: active)
        status = request.query.get("status", "active")
        try:
            return _json({
                "sessions": plane.server.sessions.list(status=status),
                "status_filter": status,
            })
        except Exception as e:
            return _err(f"sessions list failed: {e}", 500)

    async def session_detail(request: web.Request) -> web.Response:
        if plane.server is None or plane.server.sessions is None:
            return _err("server not bound", 503)
        sid = request.match_info["session_id"]
        try:
            records = list(plane.server.sessions.read(sid))
            if not records:
                return _err("session not found", 404)
            return _json({"session_id": sid, "records": records})
        except Exception as e:
            return _err(f"session read failed: {e}", 500)

    # ── Claude Code archive (every CC jsonl ever produced) ──
    async def cc_archive_list(_request: web.Request) -> web.Response:
        """List all CC sessions across all working-dir buckets, newest first.
        Each entry: {session_id, working_dir, created_at, size_bytes, n_lines}.
        """
        from pathlib import Path as _P
        root = _P.home() / ".claude" / "projects"
        out: list[dict[str, Any]] = []
        if root.exists():
            for bucket in root.iterdir():
                if not bucket.is_dir():
                    continue
                # bucket name is the sanitised working_dir
                wdir = "/" + bucket.name.lstrip("-").replace("-", "/")
                for jsonl in bucket.glob("*.jsonl"):
                    try:
                        st = jsonl.stat()
                    except OSError:
                        continue
                    sid = jsonl.stem
                    # Cheap line count
                    try:
                        with jsonl.open("rb") as f:
                            n_lines = sum(1 for _ in f)
                    except OSError:
                        n_lines = -1
                    out.append({
                        "session_id": sid,
                        "working_dir": wdir,
                        "created_at": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                        "size_bytes": st.st_size,
                        "n_lines": n_lines,
                        "bucket": bucket.name,
                    })
        out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return _json({"cc_sessions": out[:200]})  # cap for HUD render

    async def cc_archive_detail(request: web.Request) -> web.Response:
        """Distilled timeline of one CC jsonl: tool_uses + assistant texts.

        Returns: {session_id, working_dir, bucket, events: [{idx, kind, ...}]}
        Heavy events (full tool_result bodies) are truncated.
        """
        from pathlib import Path as _P
        sid = request.match_info["session_id"]
        # Locate the jsonl by searching all buckets.
        root = _P.home() / ".claude" / "projects"
        match: _P | None = None
        bucket = ""
        if root.exists():
            for b in root.iterdir():
                cand = b / f"{sid}.jsonl"
                if cand.exists():
                    match = cand
                    bucket = b.name
                    break
        if match is None:
            return _err("CC session not found", 404)
        events: list[dict[str, Any]] = []
        try:
            with match.open("r", encoding="utf-8") as f:
                for i, line in enumerate(f):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    t = ev.get("type", "?")
                    if t == "assistant":
                        msg = ev.get("message") or {}
                        sr = msg.get("stop_reason")
                        for c in msg.get("content") or []:
                            if not isinstance(c, dict):
                                continue
                            ct = c.get("type")
                            if ct == "text" and (c.get("text") or "").strip():
                                events.append({
                                    "idx": i, "kind": "assistant_text",
                                    "stop_reason": sr,
                                    "text": (c.get("text") or "")[:1200],
                                })
                            elif ct == "tool_use":
                                tin = c.get("input") or {}
                                desc = ""
                                if isinstance(tin, dict):
                                    desc = (
                                        tin.get("description")
                                        or tin.get("command")
                                        or tin.get("file_path")
                                        or tin.get("pattern")
                                        or ""
                                    )
                                    if not isinstance(desc, str):
                                        desc = str(desc)
                                events.append({
                                    "idx": i, "kind": "tool_use",
                                    "tool_name": c.get("name"),
                                    "description": desc[:300],
                                    "input": {k: (v if isinstance(v, (str, int, float, bool, type(None))) else str(v)[:400]) for k, v in (tin.items() if isinstance(tin, dict) else [])},
                                })
                            elif ct == "thinking":
                                events.append({
                                    "idx": i, "kind": "thinking",
                                    "preview": (c.get("thinking") or "")[:300],
                                })
                    elif t == "user":
                        msg = ev.get("message") or {}
                        content = msg.get("content")
                        if isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "tool_result":
                                    tc = c.get("content")
                                    tx = tc if isinstance(tc, str) else (
                                        " ".join(p.get("text", "") for p in tc if isinstance(p, dict)) if isinstance(tc, list) else ""
                                    )
                                    events.append({
                                        "idx": i, "kind": "tool_result",
                                        "tool_use_id": c.get("tool_use_id"),
                                        "result": (tx or "")[:1200],
                                        "is_error": bool(c.get("is_error")),
                                    })
                        elif isinstance(content, str):
                            events.append({
                                "idx": i, "kind": "user_text",
                                "text": content[:1200],
                            })
        except OSError as e:
            return _err(f"read failed: {e}", 500)
        wdir = "/" + bucket.lstrip("-").replace("-", "/")
        return _json({
            "session_id": sid, "working_dir": wdir, "bucket": bucket,
            "events": events,
        })

    # Shortcuts CRUD (/api/shortcuts) removed — shortcuts are now 3 fixed
    # app-local slots edited by voice (SoT Rev 16 / C-61). No client consumed
    # this server surface after that change (Glass + Console both checked).
    # `shortcuts_store.py` + `twin-seed/skills/shortcuts.md` deleted with it.

    # ── route table ──
    app.router.add_get("/api/health", health)
    app.router.add_post("/api/ping", ping)
    app.router.add_get("/api/ping", ping)  # support both — clients may use either verb

    app.router.add_get("/api/sessions", sessions_list)
    app.router.add_get("/api/sessions/{session_id}", session_detail)
    app.router.add_get("/api/cc-archive", cc_archive_list)
    app.router.add_get("/api/cc-archive/{session_id}", cc_archive_detail)

    app.router.add_get("/api/cc/sessions", cc_sessions)

    app.router.add_get("/api/twin/tree", twin_tree)
    app.router.add_get("/api/twin/read", twin_read)
    app.router.add_post("/api/twin/write", twin_write)

    app.router.add_get("/api/receipts", receipts)
    app.router.add_get("/api/changelog", changelog)

    app.router.add_get("/api/tasks/active", tasks_active)
    app.router.add_get("/api/events", events_list)
    app.router.add_get("/api/dispatches", dispatches_list)
    app.router.add_get("/api/llm/calls", llm_calls_list)
    app.router.add_get("/api/llm/calls/{call_id}", llm_call_detail)
    app.router.add_get("/api/llm/stats", llm_stats)

    app.router.add_get("/api/adapters", adapters_list)
    app.router.add_get("/api/system/status", system_status)

    app.router.add_post("/api/test/invoke", test_invoke)
    app.router.add_post("/api/dev/agent_invoke", dev_agent_invoke)
    app.router.add_post("/api/dev/distill_now", dev_distill_now)
    app.router.add_post("/api/dev/capture_image", dev_capture_image)
    app.router.add_post("/api/dev/face_recall", dev_face_recall)
    app.router.add_post("/api/dev/face_enroll", dev_face_enroll)
    app.router.add_post("/api/dev/insight_tick", dev_insight_tick)
    app.router.add_get("/api/trace/stream", trace_stream)

    return app


async def serve_http(host: str, port: int, plane: ControlPlane) -> None:
    """Start aiohttp server in same event loop as the WSS server."""
    app = make_app(plane)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    log.info("http.listening", host=host, port=port)
    # Keep the runner alive; cancelled when main loop exits
    try:
        await asyncio.Future()
    finally:
        await runner.cleanup()
