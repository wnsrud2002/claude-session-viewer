"""~/.claude/projects 의 Claude Code 세션 기록을 브라우저로 보는 로컬 서버."""
import json
import os
import re
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

CLAUDE_DIR = Path(os.environ.get("SV_CLAUDE_DIR", Path.home() / ".claude"))
# ~/.claude 밖에 둬서 Claude Code 의 자동 정리 대상이 되지 않게 함
STORE_DIR = Path(os.environ.get("SV_STORE_DIR", Path.home() / ".claude-session-viewer"))
HERE = Path(__file__).resolve().parent
TEXT_LIMIT = 10_000  # ponytail: 긴 메시지는 잘라서 전송, 전문 보기가 필요해지면 별도 엔드포인트
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
LOCAL_HOSTS = ("127.0.0.1", "localhost")
PORT = 8765
LIVE_SECONDS = 300


def records(path):
    # 기록 중인 세션은 마지막 줄이 한글 중간에서 잘려 있을 수 있어 디코딩 오류도 그 줄만 버림
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(r, dict):
                yield r


def plain_title(text):
    # 슬래시 명령은 <command-name>/<command-args> 태그로 기록되므로 입력한 모양 그대로 되살림
    name = re.search(r"<command-name>(.*?)</command-name>", text, re.S)
    if name:
        args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
        text = name.group(1) + (" " + args.group(1) if args else "")
    return " ".join(re.sub(r"<[^>]+>", " ", text).split())


def to_message(r):
    if r.get("type") not in ("user", "assistant") or r.get("isMeta"):
        return None
    message = r.get("message") if isinstance(r.get("message"), dict) else {}
    content = message.get("content", [])
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    content = [c for c in content if isinstance(c, dict)]
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
    first_user = next((t for m in msgs if m["role"] == "user" and (t := plain_title(m["text"]))), "")
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
    # 파일 하나가 예상 밖 형태이거나 도중에 지워져도 목록·검색 전체가 죽지 않도록 그 파일만 제외
    try:
        mtime = path.stat().st_mtime
        hit = _cache.get(path)
        if not hit or hit[0] != mtime:
            hit = _cache[path] = (mtime, *load(path))
        return hit[1:]
    except (OSError, ValueError, TypeError, AttributeError):
        return None, [], ""


def list_sessions():
    found = [s for p in (CLAUDE_DIR / "projects").glob("*/*.jsonl") if (s := cached(p)[0])]
    return sorted(found, key=lambda s: s["end"] or "", reverse=True)


def find_path(sid):
    if not UUID_RE.fullmatch(sid):
        return None  # glob 에 ../ 나 와일드카드가 섞이지 않도록 UUID 만 허용
    return next((CLAUDE_DIR / "projects").glob(f"*/{sid}.jsonl"), None)


def search(q):
    q = q.strip().lower()
    if not q:
        return []
    hits = []
    for p in (CLAUDE_DIR / "projects").glob("*/*.jsonl"):
        summary, _, hay = cached(p)
        i = hay.find(q)
        if summary and i >= 0:
            hits.append({"id": summary["id"], "snippet": hay[max(0, i - 40): i + len(q) + 40]})
    return hits


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


def bundle_paths(sid, rel):
    # history.jsonl 은 모든 세션이 공유하는 파일이라 번들에 넣지 않음
    return [rel, rel.with_suffix(""), Path("file-history") / sid, Path("session-env") / sid]


def bundle_jsonl(kind, sid):
    return next((STORE_DIR / kind / sid / "projects").glob(f"*/{sid}.jsonl"), None)


def is_live(path):
    try:
        return time.time() - path.stat().st_mtime < LIVE_SECONDS
    except OSError:
        return False


def _copy(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dst)  # mtime 을 보존해야 보관본 갱신 여부를 비교할 수 있음


def _move(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(src, dst)


def _original_rel(sid):
    src = find_path(sid)
    if not src:
        raise NotFound(sid)
    return src.relative_to(CLAUDE_DIR)


def _bundle(kind, sid):
    j = bundle_jsonl(kind, sid)
    if not j:
        raise NotFound(sid)
    bundle = STORE_DIR / kind / sid
    return bundle, [r for r in bundle_paths(sid, j.relative_to(bundle)) if (bundle / r).exists()]


def archive(sid):
    rel = _original_rel(sid)
    dest = STORE_DIR / "archive" / sid
    shutil.rmtree(dest, ignore_errors=True)
    for r in bundle_paths(sid, rel):
        if (CLAUDE_DIR / r).exists():
            _copy(CLAUDE_DIR / r, dest / r)


def unarchive(sid):
    dest = STORE_DIR / "archive" / sid
    if not dest.is_dir():
        raise NotFound(sid)
    shutil.rmtree(dest)


def trash(sid, force=False):
    rel = _original_rel(sid)
    if not force and is_live(CLAUDE_DIR / rel):
        raise Conflict("최근 5분 안에 기록된 세션이라 진행 중일 수 있습니다")
    dest = STORE_DIR / "trash" / sid
    if dest.exists():
        raise Conflict("휴지통에 같은 세션이 이미 있습니다")
    for r in bundle_paths(sid, rel):
        if (CLAUDE_DIR / r).exists():
            _move(CLAUDE_DIR / r, dest / r)


def restore(sid):
    bundle, rels = _bundle("trash", sid)
    if any((CLAUDE_DIR / r).exists() for r in rels):
        raise Conflict("원래 위치에 같은 세션이 이미 있습니다")
    for r in rels:
        _move(bundle / r, CLAUDE_DIR / r)
    shutil.rmtree(bundle)


def revive(sid):
    bundle, rels = _bundle("archive", sid)
    if (CLAUDE_DIR / rels[0]).exists():
        raise Conflict("원본 세션이 이미 있습니다")
    for r in rels:
        if not (CLAUDE_DIR / r).exists():  # 자동 정리 뒤 일부 폴더만 남아 있을 수 있어 있는 건 그대로 둠
            _copy(bundle / r, CLAUDE_DIR / r)


def purge(sid):
    trash_dir = (STORE_DIR / "trash").resolve()
    dest = (trash_dir / sid).resolve()
    if dest.parent != trash_dir or not dest.is_dir():  # ../ 나 링크로 휴지통 밖을 가리키면 지우지 않음
        raise NotFound(sid)
    shutil.rmtree(dest)


_meta_lock = threading.Lock()


def read_meta():
    try:
        data = json.loads((STORE_DIR / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def set_meta(sid, **fields):
    with _meta_lock:
        f = STORE_DIR / "meta.json"
        # 깨진 meta.json 을 빈 dict 로 덮어쓰면 모든 메모가 사라지므로 읽기 실패는 그대로 올려보냄
        meta = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        if not isinstance(meta, dict):
            raise ValueError("meta.json 형식이 올바르지 않습니다")
        meta[sid] = {**meta.get(sid, {}), **fields}
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(f)  # 쓰는 도중 죽어도 기존 파일이 반쯤 덮이지 않게
        return meta[sid]


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
