"""A tiny OpenAI-compatible chat-completions server on 127.0.0.1 that plays a fixed script.

It lets the Hermes agent loop (``run_agent.AIAgent``) run end to end with no LLM credentials:
turn 1 asks for the ``nano_x402_pay`` tool, turn 2 reports the tool result. Used by the README
example and by the offline tests. Point Hermes at a real model instead with ``--model``.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, List


class ScriptedModel:
    def __init__(self, tool_args: dict, tool_name: str = "nano_x402_pay"):
        self.tool_args = tool_args
        self.tool_name = tool_name
        self.requests: List[dict] = []
        self._server = None

    # each reply is chosen by how many tool results are already in the conversation
    def _reply(self, body: dict) -> dict:
        msgs = body.get("messages", [])
        tool_msgs = [m for m in msgs if m.get("role") == "tool"]
        tools = [t.get("function", {}).get("name") for t in body.get("tools") or []]
        if not tool_msgs:
            if self.tool_name not in tools:
                return {"role": "assistant", "content": f"tool {self.tool_name} is not available; offered: {tools}"}
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_" + uuid.uuid4().hex[:12],
                    "type": "function",
                    "function": {"name": self.tool_name, "arguments": json.dumps(self.tool_args)},
                }],
            }
        result = tool_msgs[-1].get("content")
        if isinstance(result, list):
            result = "".join(p.get("text", "") for p in result if isinstance(p, dict))
        return {"role": "assistant", "content": "Tool result: " + str(result)}

    def _completion(self, body: dict) -> dict:
        msg = self._reply(body)
        return {
            "id": "chatcmpl-" + uuid.uuid4().hex[:12],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model", "scripted"),
            "choices": [{"index": 0, "message": msg,
                         "finish_reason": "tool_calls" if msg.get("tool_calls") else "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    def _chunks(self, comp: dict):
        msg = comp["choices"][0]["message"]
        base = {k: comp[k] for k in ("id", "created", "model")}
        base["object"] = "chat.completion.chunk"
        delta = {"role": "assistant"}
        if msg.get("content"):
            delta["content"] = msg["content"]
        if msg.get("tool_calls"):
            delta["tool_calls"] = [dict(tc, index=i) for i, tc in enumerate(msg["tool_calls"])]
        yield dict(base, choices=[{"index": 0, "delta": delta, "finish_reason": None}])
        yield dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": comp["choices"][0]["finish_reason"]}],
                   usage=comp["usage"])

    def start(self) -> str:
        model = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.rstrip("/").endswith("/models"):
                    return self._json(200, {"object": "list", "data": [{"id": "scripted", "object": "model"}]})
                self._json(404, {"error": "not found"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                model.requests.append(body)
                if not self.path.rstrip("/").endswith("/chat/completions"):
                    return self._json(404, {"error": "not found"})
                comp = model._completion(body)
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for c in model._chunks(comp):
                        self.wfile.write(b"data: " + json.dumps(c).encode() + b"\n\n")
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                self._json(200, comp)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
