"""~/.claude/projects 의 Claude Code 세션 기록을 브라우저로 보는 로컬 서버."""
import json
from pathlib import Path

ROOT = Path.home() / ".claude" / "projects"
HERE = Path(__file__).resolve().parent
TEXT_LIMIT = 10_000  # ponytail: 긴 메시지는 잘라서 전송, 전문 보기가 필요해지면 별도 엔드포인트
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


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
