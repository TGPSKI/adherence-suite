#!/usr/bin/env python3
"""tools/proxy.py — recording proxy in front of the inference endpoint.

Ground truth for token counts and round trips (design §3.2). Sits between
the harness and the OpenAI-compatible endpoint, forwards everything
untouched, and appends one JSONL line per upstream call with that call's
usage block.

Why not trust the adapter: `opencode_export_to_transcript.py` already
documents export-schema drift as a recurring problem, and §0.3 records
that the semantics of the one scalar the suite stores today are
unverified. A proxy counts calls by construction — it cannot miscount a
round trip, because a round trip is a request it handled. **On
disagreement the proxy is authoritative** (H4 gate).

    tools/proxy.py --port 8010 --upstream http://127.0.0.1:8000 \
        --log runs/proxy.jsonl

Then point the harness at it — one line in bench/opencode-bench.json:
    provider.local.options.baseURL = http://127.0.0.1:8010/v1

Run partitioning: the runner marks trial boundaries by POSTing to
/__proxy/mark, which appends a `mark` line. Without that, proxy lines
from concurrent trials are indistinguishable. Stdlib only.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Headers we must not copy verbatim: framing is re-derived, and hop-by-hop
# headers are per-connection by definition.
_SKIP_REQ = {"host", "content-length", "connection", "accept-encoding",
             "transfer-encoding"}
_SKIP_RESP = {"content-length", "transfer-encoding", "connection",
              "content-encoding"}


class Recorder:
    """Append-only JSONL sink with a monotonic call counter."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.seq = 0
        self.current_mark = ""

    def mark(self, label: str) -> None:
        with self.lock:
            self.current_mark = label
            self._write({"type": "mark", "label": label, "ts": time.time()})

    def record(self, rec: dict) -> int:
        with self.lock:
            seq = self.seq
            self.seq += 1
            rec["type"] = "call"
            rec["seq"] = seq
            # A run id carried on the request path beats the global mark and
            # is the only one of the two that survives concurrency: the mark
            # is one piece of shared state, so two trials in flight write
            # into whichever label happened to be set last. `mark` stays for
            # serial runs and for reading old logs.
            rec.setdefault("mark", self.current_mark)
            self._write(rec)
            return seq

    def _write(self, obj) -> None:
        if not self.path:
            print(json.dumps(obj), file=sys.stderr)
            return
        with open(self.path, "a") as f:
            f.write(json.dumps(obj) + "\n")
            f.flush()

    def stats(self) -> dict:
        with self.lock:
            return {"calls": self.seq, "mark": self.current_mark,
                    "log": self.path}


def _usage_from(obj: dict) -> dict | None:
    """Pull an OpenAI-compatible usage block into flat, schema-named keys.

    `prompt_tokens_details.cached_tokens` is vLLM's prefix-cache hit count
    when automatic prefix caching is on. It is recorded because it is
    free to record, but it is a COMPUTE saving on a local endpoint, not a
    billing one — §16.1. Do not report it as cost."""
    u = obj.get("usage")
    if not isinstance(u, dict):
        return None
    det = u.get("prompt_tokens_details") or {}
    return {
        "input_tokens": u.get("prompt_tokens", 0) or 0,
        "output_tokens": u.get("completion_tokens", 0) or 0,
        "total_tokens": u.get("total_tokens", 0) or 0,
        "cached_tokens": (det.get("cached_tokens", 0) or 0
                          if isinstance(det, dict) else 0),
    }


def _fingerprint(body: dict) -> dict:
    """Cheap, stable identity for a request: which model, how many
    messages, and a hash of the system prompt. The system-prompt hash is
    what lets parent and subagent calls be separated post-hoc (§7) —
    the proxy cannot see an 'agent' field, but a subagent's system prompt
    differs from its parent's."""
    msgs = body.get("messages") or []
    sysmsg = next((m.get("content") for m in msgs
                   if isinstance(m, dict) and m.get("role") == "system"), "")
    if not isinstance(sysmsg, str):
        sysmsg = json.dumps(sysmsg, sort_keys=True)
    return {
        "model": body.get("model", ""),
        "n_messages": len(msgs),
        "n_tools": len(body.get("tools") or []),
        "stream": bool(body.get("stream")),
        "system_sha8": hashlib.sha256(sysmsg.encode()).hexdigest()[:8],
        "system_chars": len(sysmsg),
    }


# Disconnects are normal traffic, not faults. opencode opens keep-alive
# connections and drops them when a session ends, and http.server's default
# response is to dump a full traceback per connection -- which buried a
# live run's own output under hundreds of ConnectionResetErrors and made
# the proxy look like it was failing while it was working correctly.
_BENIGN = (ConnectionResetError, BrokenPipeError, ConnectionAbortedError,
           TimeoutError)


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True
    # Long generations hold a connection open; the default would give up.
    timeout = None

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, _BENIGN):
            return                      # the client hung up; nothing to say
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    recorder: Recorder = None       # set on the server class
    upstream: str = ""
    inject_usage: bool = True

    def log_message(self, fmt, *a):   # quiet; the JSONL is the log
        pass

    def handle_one_request(self):
        """Treat a hung-up client as the end of the conversation.

        Without this the reset propagates out of the handler and
        socketserver logs it as an unhandled error. It also ends the
        thread mid-request, which is what made a selftest sender die
        silently on 3.10 and get reported as cross-attribution."""
        try:
            super().handle_one_request()
        except _BENIGN:
            self.close_connection = True

    # ---- control plane -------------------------------------------------

    def _split_run(self) -> str:
        """Pull a `/__run/<id>` prefix off the request path.

        This is how a trial identifies itself under --jobs>1. The runner
        gives each trial a provider baseURL carrying its own run id, so the
        attribution arrives with the request instead of being inferred from
        proxy state that two concurrent trials would fight over. Nothing in
        an inference request body identifies its trial -- measured; the
        sandbox path is not even in the system prompt -- so the path is the
        only place left to put it.

        Returns the id and rewrites self.path to what upstream should see.
        """
        if not self.path.startswith("/__run/"):
            return ""
        rest = self.path[len("/__run/"):]
        rid, sep, tail = rest.partition("/")
        if not sep:
            return ""
        self.path = "/" + tail
        return urllib.parse.unquote(rid)

    def _control(self, body: bytes) -> bool:
        if not self.path.startswith("/__proxy/"):
            return False
        what = self.path[len("/__proxy/"):].split("?")[0]
        if what == "health":
            self._json(200, {"ok": True, "upstream": self.upstream})
        elif what == "stats":
            self._json(200, self.recorder.stats())
        elif what == "mark":
            try:
                label = json.loads(body or b"{}").get("label", "")
            except json.JSONDecodeError:
                label = ""
            self.recorder.mark(label)
            self._json(200, {"marked": label})
        else:
            self._json(404, {"error": "unknown control endpoint"})
        return True

    def _json(self, code: int, obj: dict) -> None:
        payload = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # ---- proxy ---------------------------------------------------------

    def do_GET(self):
        self._proxy("GET")

    def do_POST(self):
        self._proxy("POST")

    def do_DELETE(self):
        self._proxy("DELETE")

    def _proxy(self, method: str) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        if self._control(body):
            return
        # Strip before anything reads self.path: upstream must never see
        # the routing prefix, and every record below should carry the id.
        run_id = self._split_run()

        parsed, fp = None, {}
        if body:
            try:
                parsed = json.loads(body)
                fp = _fingerprint(parsed)
            except json.JSONDecodeError:
                pass

        # Streaming responses only carry a usage block if the client asked
        # for one. The harness may not; the measurement needs it either
        # way, so ask on its behalf. Additive — an extra final SSE chunk
        # carrying usage, which OpenAI-compatible clients already tolerate.
        if (self.inject_usage and isinstance(parsed, dict)
                and parsed.get("stream")):
            so = parsed.get("stream_options")
            if not isinstance(so, dict) or not so.get("include_usage"):
                parsed["stream_options"] = {**(so or {}), "include_usage": True}
                body = json.dumps(parsed).encode()
                fp["usage_injected"] = True

        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in _SKIP_REQ}
        if body:
            headers["Content-Length"] = str(len(body))
        req = urllib.request.Request(self.upstream + self.path, data=body or None,
                                     headers=headers, method=method)

        t0 = time.time()
        try:
            resp = urllib.request.urlopen(req, timeout=1800)
            status = resp.status
        except urllib.error.HTTPError as e:
            resp, status = e, e.code
        except Exception as e:                       # upstream down/refused
            self.recorder.record({**fp, "status": 0, "error": str(e),
                                  **({"mark": run_id} if run_id else {}),
                                  "duration_ms": int((time.time() - t0) * 1000)})
            self._json(502, {"error": f"proxy upstream: {e}"})
            return

        ctype = resp.headers.get("Content-Type", "")
        if "text/event-stream" in ctype:
            usage, finish = self._pump_stream(resp)
        else:
            usage, finish = self._pump_body(resp)

        rec = {**fp, "status": status, "path": self.path,
               "duration_ms": int((time.time() - t0) * 1000),
               "finish_reason": finish}
        if run_id:
            rec["mark"] = run_id
            rec["run_id"] = run_id
        rec.update(usage or {"input_tokens": 0, "output_tokens": 0,
                             "total_tokens": 0, "cached_tokens": 0,
                             "usage_missing": True})
        # Only chat/completions calls are inference round trips. /v1/models
        # and friends are recorded but must not be counted as calls.
        rec["inference"] = "completions" in self.path
        self.recorder.record(rec)

    def _send_head(self, resp, chunked: bool, length: int | None) -> None:
        self.send_response(getattr(resp, "status", 200))
        for k, v in resp.headers.items():
            if k.lower() in _SKIP_RESP:
                continue
            self.send_header(k, v)
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def _pump_body(self, resp):
        data = resp.read()
        self._send_head(resp, chunked=False, length=len(data))
        self.wfile.write(data)
        try:
            obj = json.loads(data)
        except Exception:
            return None, ""
        finish = ""
        ch = obj.get("choices") or []
        if ch and isinstance(ch[0], dict):
            finish = ch[0].get("finish_reason") or ""
        return _usage_from(obj), finish

    def _pump_stream(self, resp):
        """Forward SSE line by line, teeing every `data:` payload through
        the usage parser. Forwarding must not wait on the parse: the
        client's latency is part of what the suite measures."""
        self._send_head(resp, chunked=True, length=None)
        usage, finish, gone = None, "", False
        try:
            for raw in resp:
                # opencode hangs up on its own auxiliary calls (session
                # title generation) as soon as it has what it wants. The
                # call still ran and still cost tokens upstream, and the
                # usage block rides in the LAST chunk -- so keep draining
                # after the client leaves, or those calls are recorded as
                # zero-token, which is a measurement that reads as real.
                if not gone:
                    try:
                        self.wfile.write(b"%x\r\n" % len(raw) + raw + b"\r\n")
                    except (BrokenPipeError, ConnectionResetError):
                        gone, finish = True, finish or "client_disconnect"
                if not raw.startswith(b"data: "):
                    continue
                payload = raw[6:].strip()
                if payload in (b"[DONE]", b""):
                    continue
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                u = _usage_from(obj)
                if u:
                    usage = u
                ch = obj.get("choices") or []
                if ch and isinstance(ch[0], dict) and ch[0].get("finish_reason"):
                    finish = ch[0]["finish_reason"] if not gone else finish
            if not gone:
                self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            finish = finish or "client_disconnect"
        return usage, finish


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8010)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--upstream", default="http://127.0.0.1:8000")
    ap.add_argument("--log", default="proxy.jsonl")
    ap.add_argument("--no-inject-usage", action="store_true",
                    help="do not add stream_options.include_usage to "
                         "streaming requests (streaming usage will be lost)")
    args = ap.parse_args()

    Handler.recorder = Recorder(args.log)
    Handler.upstream = args.upstream.rstrip("/")
    Handler.inject_usage = not args.no_inject_usage

    srv = QuietServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"proxy: {args.host}:{args.port} -> {Handler.upstream} "
          f"log={args.log}", flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        srv.serve_forever()


if __name__ == "__main__":
    main()
