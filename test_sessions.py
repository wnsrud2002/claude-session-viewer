import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
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


SID2 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class IndexTest(Base):
    def setUp(self):
        super().setUp()
        sessions._cache.clear()
        self.old = self.write([user("Old 세션", "2026-09-01T10:00:00Z")])
        self.new = self.write([user("새 세션 Python", "2026-09-10T10:00:00Z")], sid=SID2, project="-Users-jun-x")

    def test_list_sorted_newest_first(self):
        self.assertEqual([s["id"] for s in sessions.list_sessions(self.root)], [SID2, SID])

    def test_list_skips_empty_session(self):
        self.write([rec("mode")], sid="cccccccc-bbbb-cccc-dddd-eeeeeeeeeeee")
        self.assertEqual(len(sessions.list_sessions(self.root)), 2)

    def test_search_case_insensitive(self):
        self.assertEqual([h["id"] for h in sessions.search("PYTHON", self.root)], [SID2])
        self.assertEqual(sorted(h["id"] for h in sessions.search("세션", self.root)), sorted([SID, SID2]))

    def test_search_snippet_contains_query(self):
        self.assertIn("python", sessions.search("python", self.root)[0]["snippet"])

    def test_search_empty_query(self):
        self.assertEqual(sessions.search("   ", self.root), [])

    def test_find_path(self):
        self.assertEqual(sessions.find_path(SID, self.root), self.old)
        self.assertIsNone(sessions.find_path("../../etc/passwd", self.root))
        self.assertIsNone(sessions.find_path("99999999-2222-3333-4444-555555555555", self.root))

    def test_line_cut_mid_utf8_char_skipped(self):
        p = self.write([user("완성된 줄", "2026-09-01T10:00:00Z")])
        with open(p, "ab") as f:
            f.write(json.dumps(user("잘린 한글", "2026-09-01T10:01:00Z"), ensure_ascii=False).encode()[:-5][:40] + "한".encode()[:2])
        self.assertEqual(sessions.cached(p)[0]["messageCount"], 1)

    def test_odd_record_shapes_skipped(self):
        p = self.write([
            user("정상", "2026-09-01T10:00:00Z"),
            '[1, 2]',
            rec("user", timestamp="2026-09-01T10:01:00Z", message="hi"),
            rec("assistant", timestamp="2026-09-01T10:02:00Z", message=None),
            user(["x", {"type": "text", "text": "섞인 내용"}], "2026-09-01T10:03:00Z"),
        ])
        self.assertEqual([m["text"] for m in sessions.cached(p)[1]], ["정상", "섞인 내용"])

    def test_bad_file_does_not_break_list(self):
        self.write([rec("user", timestamp="2026-09-20T10:00:00Z", cwd=123,
                        message={"role": "user", "content": "x"})], sid="dddddddd-bbbb-cccc-dddd-eeeeeeeeeeee")
        self.assertEqual([s["id"] for s in sessions.list_sessions(self.root)], [SID2, SID])
        self.assertEqual(len(sessions.search("세션", self.root)), 2)

    def test_cache_reloads_after_change(self):
        self.assertEqual(sessions.cached(self.old)[0]["messageCount"], 1)
        self.write([user("a", "2026-09-01T10:00:00Z"), user("b", "2026-09-01T10:01:00Z")])
        st = self.old.stat()
        os.utime(self.old, (st.st_atime, st.st_mtime + 5))  # 같은 초 안의 재기록도 감지되는지 보장
        self.assertEqual(sessions.cached(self.old)[0]["messageCount"], 2)


class HttpTest(Base):
    def setUp(self):
        super().setUp()
        sessions._cache.clear()
        self.write([user("hi <img src=x onerror=alert(1)>", "2026-09-01T10:00:00Z")])
        self.orig_root, sessions.ROOT = sessions.ROOT, self.root
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), sessions.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        sessions.ROOT = self.orig_root
        super().tearDown()

    def get(self, path, host=None):
        c = HTTPConnection("127.0.0.1", self.port)
        c.request("GET", path, headers={"Host": host or f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body

    def test_http_sessions(self):
        status, body = self.get("/api/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)[0]["id"], SID)

    def test_http_session_detail(self):
        status, body = self.get(f"/api/session/{SID}")
        self.assertEqual((status, json.loads(body)[0]["text"]), (200, "hi <img src=x onerror=alert(1)>"))

    def test_http_path_traversal_rejected(self):
        self.assertEqual(self.get("/api/session/..%2F..%2Fetc%2Fpasswd")[0], 404)
        self.assertEqual(self.get("/api/session/../../etc/passwd")[0], 404)

    def test_http_foreign_host_rejected(self):
        self.assertEqual(self.get("/api/sessions", host="evil.com")[0], 403)
        self.assertEqual(self.get("/api/sessions", host=f"localhost:{self.port}")[0], 200)

    def test_http_search(self):
        status, body = self.get("/api/search?q=HI")
        self.assertEqual((status, [h["id"] for h in json.loads(body)]), (200, [SID]))


if __name__ == "__main__":
    unittest.main()
