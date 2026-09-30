import json
import tempfile
import unittest
from pathlib import Path

import sessions

SID = "11111111-2222-3333-4444-555555555555"


def rec(type_, **kw):
    return {"type": type_, "sessionId": SID, **kw}


def user(content, ts, **kw):
    return rec("user", timestamp=ts, cwd="/Users/jun/dev/demo",
               message={"role": "user", "content": content}, **kw)


def asst(blocks, ts, mid="msg_1", usage=None):
    return rec("assistant", timestamp=ts, cwd="/Users/jun/dev/demo",
               message={"id": mid, "role": "assistant", "content": blocks, "usage": usage or {}})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, lines, sid=SID, project="-Users-jun-dev-demo"):
        d = self.root / project
        d.mkdir(exist_ok=True)
        p = d / f"{sid}.jsonl"
        p.write_text("\n".join(l if isinstance(l, str) else json.dumps(l, ensure_ascii=False)
                               for l in lines) + "\n", encoding="utf-8")
        return p


class LoadTest(Base):
    def test_title_uses_last_ai_title(self):
        p = self.write([user("안녕", "2026-09-01T10:00:00Z"),
                        rec("ai-title", aiTitle="첫 제목"), rec("ai-title", aiTitle="바뀐 제목")])
        self.assertEqual(sessions.load(p)[0]["title"], "바뀐 제목")

    def test_title_falls_back_to_first_user_text(self):
        p = self.write([user("가" * 100, "2026-09-01T10:00:00Z")])
        self.assertEqual(sessions.load(p)[0]["title"], "가" * 60)

    def test_missing_cost_is_none(self):
        s = sessions.load(self.write([user("hi", "2026-09-01T10:00:00Z")]))[0]
        self.assertIsNone(s["costUSD"])
        self.assertIsNone(s["linesAdded"])

    def test_cost_from_cost_state(self):
        p = self.write([user("hi", "2026-09-01T10:00:00Z"),
                        rec("cost-state", totalCostUSD=0.5, totalLinesAdded=3, totalLinesRemoved=1)])
        s = sessions.load(p)[0]
        self.assertEqual((s["costUSD"], s["linesAdded"], s["linesRemoved"]), (0.5, 3, 1))

    def test_broken_line_skipped(self):
        p = self.write([user("hi", "2026-09-01T10:00:00Z"), '{"type": "assist'])
        self.assertEqual(sessions.load(p)[0]["messageCount"], 1)

    def test_internal_records_excluded(self):
        p = self.write([
            rec("mode", mode="default"),
            rec("attachment", attachment={}),
            user("<command-name>/clear</command-name>", "2026-09-01T09:59:00Z", isMeta=True),
            user("질문", "2026-09-01T10:00:00Z"),
            asst([{"type": "thinking", "thinking": "..."}], "2026-09-01T10:00:01Z"),
            asst([{"type": "text", "text": "답변"}], "2026-09-01T10:00:02Z"),
            asst([{"type": "tool_use", "name": "Bash", "input": {}}], "2026-09-01T10:00:03Z"),
            user([{"type": "tool_result", "content": "ok"}], "2026-09-01T10:00:04Z"),
        ])
        msgs = sessions.load(p)[1]
        self.assertEqual([(m["role"], m["text"], m["tools"]) for m in msgs],
                         [("user", "질문", []), ("assistant", "답변", []), ("assistant", "", ["Bash"])])

    def test_tokens_counted_once_per_response(self):
        u = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100}
        p = self.write([user("hi", "2026-09-01T10:00:00Z"),
                        asst([{"type": "thinking", "thinking": "."}], "2026-09-01T10:00:01Z", "msg_1", u),
                        asst([{"type": "text", "text": "a"}], "2026-09-01T10:00:02Z", "msg_1", u),
                        asst([{"type": "text", "text": "b"}], "2026-09-01T10:00:03Z", "msg_2", u)])
        self.assertEqual(sessions.load(p)[0]["tokens"], 230)

    def test_meta_only_session_has_no_summary(self):
        p = self.write([rec("mode", mode="default"), rec("ai-title", aiTitle="빈 세션")])
        self.assertEqual(sessions.load(p), (None, [], ""))

    def test_summary_fields(self):
        p = self.write([user("Hello World", "2026-09-01T10:00:00Z"),
                        asst([{"type": "text", "text": "ok"}], "2026-09-01T10:05:00Z")])
        s, _, hay = sessions.load(p)
        self.assertEqual((s["id"], s["project"], s["cwd"], s["start"], s["end"]),
                         (SID, "demo", "/Users/jun/dev/demo", "2026-09-01T10:00:00Z", "2026-09-01T10:05:00Z"))
        self.assertIn("hello world", hay)


if __name__ == "__main__":
    unittest.main()
