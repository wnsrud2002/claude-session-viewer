"""~/.claude/projects 의 Claude Code 세션 기록을 브라우저로 보는 로컬 서버."""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path.home() / ".claude" / "projects"
HERE = Path(__file__).resolve().parent
TEXT_LIMIT = 10_000  # ponytail: 긴 메시지는 잘라서 전송, 전문 보기가 필요해지면 별도 엔드포인트
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
LOCAL_HOSTS = ("127.0.0.1", "localhost")
PORT = 8765


def records(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue  # 기록 중인 세션은 마지막 줄이 잘려 있을 수 있음


def to_message(r):
    if r.get("type") not in ("user", "assistant") or r.get("isMeta"):
        return None
    content = r.get("message", {}).get("content", [])
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    text = "\n".join(c.get("text", "") for c in content if c.get("type") == "text").strip()
    tools = [c.get("name", "?") for c in content if c.get("type") == "tool_use"]
    if not text and not tools:
        return None  # thinking·tool_result 만 있는 레코드
    return {"role": r["type"], "timestamp": r.get("timestamp"), "text": text, "tools": tools}


def load(path):
    msgs, title, cwd, cost, tokens, seen = [], None, None, None, 0, set()
    for r in records(path):
        kind = r.get("type")
        if kind == "ai-title":
            title = r.get("aiTitle") or title
        elif kind == "cost-state":
            cost = r
        cwd = cwd or r.get("cwd")
        message = r.get("message") if isinstance(r.get("message"), dict) else {}
        if kind == "assistant" and message.get("usage") and message.get("id") not in seen:
            seen.add(message.get("id"))  # 한 응답이 블록별로 여러 줄에 나뉘며 같은 usage 를 반복함
            tokens += sum(message["usage"].get(k) or 0 for k in USAGE_KEYS)
        if m := to_message(r):
            msgs.append(m)
    if not msgs:
        return None, [], ""
    stamps = [m["timestamp"] for m in msgs if m["timestamp"]]
    first_user = next((m["text"] for m in msgs if m["role"] == "user" and m["text"]), "")
    summary = {
        "id": path.stem,
        "title": title or first_user[:60] or "(제목 없음)",
        "project": Path(cwd).name if cwd else path.parent.name,
        "cwd": cwd,
        "start": min(stamps, default=None),
        "end": max(stamps, default=None),
        "messageCount": len(msgs),
        "costUSD": cost.get("totalCostUSD") if cost else None,
        "tokens": tokens,
        "linesAdded": cost.get("totalLinesAdded") if cost else None,
        "linesRemoved": cost.get("totalLinesRemoved") if cost else None,
    }
    haystack = "\n".join(m["text"] for m in msgs).lower()
    for m in msgs:
        m["text"] = m["text"][:TEXT_LIMIT]
    return summary, msgs, haystack


_cache = {}  # path -> (mtime, summary, msgs, haystack)


def cached(path):
    mtime = path.stat().st_mtime
    hit = _cache.get(path)
    if not hit or hit[0] != mtime:
        hit = _cache[path] = (mtime, *load(path))
    return hit[1:]


def list_sessions(root=None):
    root = root or ROOT
    found = [s for p in root.glob("*/*.jsonl") if (s := cached(p)[0])]
    return sorted(found, key=lambda s: s["end"] or "", reverse=True)


def find_path(sid, root=None):
    if not UUID_RE.fullmatch(sid):
        return None  # glob 에 ../ 나 와일드카드가 섞이지 않도록 UUID 만 허용
    return next((root or ROOT).glob(f"*/{sid}.jsonl"), None)


def search(q, root=None):
    q = q.strip().lower()
    if not q:
        return []
    hits = []
    for p in (root or ROOT).glob("*/*.jsonl"):
        summary, _, hay = cached(p)
        i = hay.find(q)
        if summary and i >= 0:
            hits.append({"id": summary["id"], "snippet": hay[max(0, i - 40): i + len(q) + 40]})
    return hits


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # 127.0.0.1 바인딩만으로는 DNS rebinding 으로 외부 사이트가 읽어갈 수 있어 Host 도 확인
        if self.headers.get("Host", "").rsplit(":", 1)[0] not in LOCAL_HOSTS:
            return self.reply_json({"error": "forbidden"}, 403)
        url = urlparse(self.path)
        if url.path == "/":
            return self.reply(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if url.path == "/api/sessions":
            return self.reply_json(list_sessions())
        if url.path.startswith("/api/session/"):
            path = find_path(url.path.removeprefix("/api/session/"))
            return self.reply_json(cached(path)[1]) if path else self.reply_json({"error": "not found"}, 404)
        if url.path == "/api/search":
            return self.reply_json(search(parse_qs(url.query).get("q", [""])[0]))
        self.reply_json({"error": "not found"}, 404)

    def reply_json(self, data, status=200):
        self.reply(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def reply(self, status, body, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
