#!/usr/bin/env python3
"""
Hermes Agent Desktop Client

Launches a pywebview desktop window with a built-in lightweight API server
that wraps the Hermes AIAgent. No gateway infrastructure required.

Usage:
    python desktop/app.py
"""

import asyncio
import json
import logging
import os
import queue
import sys
import threading
import time
import uuid
from pathlib import Path

# Ensure the project root is importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("hermes-desktop")

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
HOST = "127.0.0.1"
PORT = 8642
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS, DELETE",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Hermes-Session-Id",
    "Access-Control-Expose-Headers": "X-Hermes-Session-Id",
}


# --------------------------------------------------------------------------
# CORS middleware
# --------------------------------------------------------------------------
@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response(status=204, headers=CORS_HEADERS)
        return resp
    resp = await handler(request)
    resp.headers.update(CORS_HEADERS)
    return resp


# --------------------------------------------------------------------------
# Agent factory — lazily resolve config once
# --------------------------------------------------------------------------
_agent_kwargs_cache = None


def _resolve_agent_kwargs():
    """Load model/api_key/base_url from hermes config.yaml + env."""
    global _agent_kwargs_cache
    if _agent_kwargs_cache is not None:
        return _agent_kwargs_cache

    kwargs = {}
    try:
        from hermes_cli.config import load_config
        config = load_config()
        model = config.get("model", os.getenv("MODEL", ""))
        # model can be a dict like {"default": "qwen-plus", "provider": "..."} — extract string
        if isinstance(model, dict):
            model = model.get("default", model.get("model", ""))
        kwargs["model"] = model
        kwargs["api_key"] = config.get("api_key", os.getenv("OPENAI_API_KEY", os.getenv("ANTHROPIC_API_KEY", "")))
        kwargs["base_url"] = config.get("base_url", os.getenv("BASE_URL", ""))

        # Provider
        provider = config.get("provider", os.getenv("PROVIDER", ""))
        if provider:
            kwargs["provider"] = provider
    except Exception as e:
        logger.warning("Failed to load hermes config, falling back to env: %s", e)
        kwargs["model"] = os.getenv("MODEL", "")
        kwargs["api_key"] = os.getenv("OPENAI_API_KEY", os.getenv("ANTHROPIC_API_KEY", ""))
        kwargs["base_url"] = os.getenv("BASE_URL", "")

    # Remove empty strings
    kwargs = {k: v for k, v in kwargs.items() if v}
    _agent_kwargs_cache = kwargs
    return kwargs


def create_agent(session_id=None, stream_delta_callback=None, tool_progress_callback=None):
    """Create an AIAgent instance."""
    from run_agent import AIAgent

    kwargs = _resolve_agent_kwargs()

    agent = AIAgent(
        **kwargs,
        max_iterations=30,
        quiet_mode=True,
        verbose_logging=False,
        session_id=session_id,
        platform="desktop",
        stream_delta_callback=stream_delta_callback,
        tool_progress_callback=tool_progress_callback,
    )
    return agent


# --------------------------------------------------------------------------
# API handlers
# --------------------------------------------------------------------------
async def handle_health(request):
    return web.json_response({"status": "ok", "platform": "hermes-desktop"})


async def handle_models(request):
    kwargs = _resolve_agent_kwargs()
    model_name = kwargs.get("model", "hermes-agent")
    if isinstance(model_name, dict):
        model_name = model_name.get("default", str(model_name))
    return web.json_response({
        "object": "list",
        "data": [{
            "id": model_name,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "hermes",
        }],
    })


async def handle_chat_completions(request):
    """POST /v1/chat/completions — OpenAI-compatible streaming."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": {"message": "Invalid JSON"}}, status=400)

    messages = body.get("messages", [])
    stream = body.get("stream", False)

    # Extract messages
    system_prompt = None
    conversation_messages = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "system":
            system_prompt = (system_prompt + "\n" + content) if system_prompt else content
        elif role in ("user", "assistant"):
            conversation_messages.append({"role": role, "content": content})

    if not conversation_messages:
        return web.json_response({"error": {"message": "No user message"}}, status=400)

    user_message = conversation_messages[-1].get("content", "")
    history = conversation_messages[:-1]

    session_id = request.headers.get("X-Hermes-Session-Id", str(uuid.uuid4()))
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    model_name = body.get("model", "hermes-agent")

    if stream:
        stream_q = queue.Queue()

        def on_delta(delta):
            if delta is not None:
                stream_q.put(delta)

        def on_tool_progress(event_type, name=None, preview=None, args=None, **kw):
            if event_type == "tool.started" and name and not name.startswith("_"):
                label = preview or name
                stream_q.put(f"\n<!--tool:{label}-->\n")

        loop = asyncio.get_event_loop()
        agent_task = loop.run_in_executor(
            None,
            lambda: _run_agent_sync(user_message, history, session_id, on_delta, on_tool_progress),
        )

        # SSE response
        sse_headers = {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Hermes-Session-Id": session_id,
        }
        sse_headers.update(CORS_HEADERS)
        response = web.StreamResponse(status=200, headers=sse_headers)
        await response.prepare(request)

        # Role chunk
        role_chunk = {
            "id": completion_id, "object": "chat.completion.chunk",
            "created": created, "model": model_name,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
        await response.write(f"data: {json.dumps(role_chunk)}\n\n".encode())

        # Stream content
        while True:
            try:
                delta = await loop.run_in_executor(None, lambda: stream_q.get(timeout=0.3))
            except queue.Empty:
                if agent_task.done():
                    # Drain
                    while True:
                        try:
                            d = stream_q.get_nowait()
                            if d is None:
                                break
                            chunk = {
                                "id": completion_id, "object": "chat.completion.chunk",
                                "created": created, "model": model_name,
                                "choices": [{"index": 0, "delta": {"content": d}, "finish_reason": None}],
                            }
                            await response.write(f"data: {json.dumps(chunk)}\n\n".encode())
                        except queue.Empty:
                            break
                    break
                continue

            if delta is None:
                break

            chunk = {
                "id": completion_id, "object": "chat.completion.chunk",
                "created": created, "model": model_name,
                "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}],
            }
            await response.write(f"data: {json.dumps(chunk)}\n\n".encode())

        # Finish
        finish_chunk = {
            "id": completion_id, "object": "chat.completion.chunk",
            "created": created, "model": model_name,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        await response.write(f"data: {json.dumps(finish_chunk)}\n\n".encode())
        await response.write(b"data: [DONE]\n\n")
        return response

    else:
        # Non-streaming
        result, usage = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _run_agent_sync(user_message, history, session_id),
        )
        final = result.get("final_response", result.get("error", "(No response)"))
        return web.json_response({
            "id": completion_id, "object": "chat.completion",
            "created": created, "model": model_name,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": final}, "finish_reason": "stop"}],
            "usage": usage,
        })


def _run_agent_sync(user_message, history, session_id, stream_cb=None, tool_cb=None):
    """Run agent synchronously (called from executor)."""
    agent = create_agent(
        session_id=session_id,
        stream_delta_callback=stream_cb,
        tool_progress_callback=tool_cb,
    )
    result = agent.run_conversation(
        user_message=user_message,
        conversation_history=history,
    )
    usage = {
        "input_tokens": getattr(agent, "session_prompt_tokens", 0) or 0,
        "output_tokens": getattr(agent, "session_completion_tokens", 0) or 0,
        "total_tokens": getattr(agent, "session_total_tokens", 0) or 0,
    }
    return result, usage


# --------------------------------------------------------------------------
# Static file serving for the frontend
# --------------------------------------------------------------------------
DESKTOP_DIR = Path(__file__).resolve().parent


async def handle_index(request):
    return web.FileResponse(DESKTOP_DIR / "index.html")


_webview_window = None  # Set by main() after window creation


async def handle_choose_folder(request):
    """POST /api/choose-folder — open native folder picker dialog."""
    loop = asyncio.get_event_loop()

    def _pick():
        global _webview_window
        try:
            if _webview_window is not None:
                import webview
                result = _webview_window.create_file_dialog(
                    webview.FOLDER_DIALOG,
                    directory=str(Path.home()),
                )
                if result and len(result) > 0:
                    return result[0]
            return ""
        except Exception as e:
            logger.warning("Folder picker failed: %s", e)
            return ""

    folder = await loop.run_in_executor(None, _pick)
    return web.json_response({"folder": folder})


async def handle_list_folder(request):
    """GET /api/list-folder?path=... — list subdirectories for quick access."""
    base = request.query.get("path", str(Path.home()))
    try:
        p = Path(base).expanduser().resolve()
        if not p.is_dir():
            return web.json_response({"error": "Not a directory"}, status=400)
        dirs = sorted([
            {"name": d.name, "path": str(d)}
            for d in p.iterdir()
            if d.is_dir() and not d.name.startswith('.')
        ], key=lambda x: x["name"].lower())[:20]
        return web.json_response({"parent": str(p.parent), "current": str(p), "dirs": dirs})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# --------------------------------------------------------------------------
# Server lifecycle
# --------------------------------------------------------------------------
def create_app():
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/", handle_index)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/v1/health", handle_health)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_post("/v1/chat/completions", handle_chat_completions)
    app.router.add_post("/api/choose-folder", handle_choose_folder)
    app.router.add_get("/api/list-folder", handle_list_folder)
    # Serve static assets
    if (DESKTOP_DIR / "assets").exists():
        app.router.add_static("/assets", DESKTOP_DIR / "assets")
    return app


def run_server(started_event: threading.Event):
    """Run the aiohttp server in its own thread with its own event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = create_app()
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())

    site = web.TCPSite(runner, HOST, PORT)
    loop.run_until_complete(site.start())
    logger.info("API server listening on http://%s:%d", HOST, PORT)
    started_event.set()

    try:
        loop.run_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        loop.run_until_complete(runner.cleanup())
        loop.close()


# --------------------------------------------------------------------------
# Main — pywebview desktop window
# --------------------------------------------------------------------------
def main():
    # Start API server in background thread
    started = threading.Event()
    server_thread = threading.Thread(target=run_server, args=(started,), daemon=True)
    server_thread.start()
    started.wait(timeout=10)

    url = f"http://{HOST}:{PORT}"

    try:
        import webview
        logger.info("Opening desktop window...")
        window = webview.create_window(
            "Hermes Agent",
            url,
            width=1100,
            height=750,
            min_size=(800, 500),
            background_color="#f5f5f7",
            text_select=True,
        )
        global _webview_window
        _webview_window = window
        webview.start(debug=False)
    except ImportError:
        logger.warning("pywebview not installed — opening in browser instead")
        import webbrowser
        webbrowser.open(url)
        logger.info("Press Ctrl+C to stop the server")
        try:
            server_thread.join()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
