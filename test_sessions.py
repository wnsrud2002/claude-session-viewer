import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

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
        base = Path(self.tmp.name)
        self.claude, self.store = base / "claude", base / "store"
        self.root = self.claude / "projects"
        self.root.mkdir(parents=True)
        patcher = mock.patch.multiple(sessions, CLAUDE_DIR=self.claude, STORE_DIR=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        sessions._cache.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, lines, sid=SID, project="-Users-jun-dev-demo"):
        d = self.root / project
        d.mkdir(exist_ok=True)
        p = d / f"{sid}.jsonl"
        p.write_text("\n".join(l if isinstance(l, str) else json.dumps(l, ensure_ascii=False)
                               for l in lines) + "\n", encoding="utf-8")
        return p

    def session(self, sid=SID, project="-Users-jun-dev-demo", age=3600):
        # age 초만큼 mtime 을 과거로 돌려 '진행 중'으로 판정되지 않게 함
        p = self.write([user("관리 대상", "2026-09-01T10:00:00Z")], sid=sid, project=project)
        for f in (p.with_suffix("") / "tool-results" / "r.txt",
                  self.claude / "file-history" / sid / "f", self.claude / "session-env" / sid / "e"):
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x")
        old = time.time() - age
        os.utime(p, (old, old))
        return p


class LoadTest(Base):
    def test_title_uses_last_ai_title(self):
        p = self.write([user("안녕", "2026-09-01T10:00:00Z"),
                        rec("ai-title", aiTitle="첫 제목"), rec("ai-title", aiTitle="바뀐 제목")])
        self.assertEqual(sessions.load(p)[0]["title"], "바뀐 제목")

    def test_title_falls_back_to_first_user_text(self):
        p = self.write([user("가" * 100, "2026-09-01T10:00:00Z")])
        self.assertEqual(sessions.load(p)[0]["title"], "가" * 60)

    def test_title_from_slash_command(self):
        p = self.write([user("<command-name>/plugin</command-name>\n            <command-message>plugin</command-message>\n"
                             "            <command-args>install frontend-design</command-args>", "2026-09-01T10:00:00Z")])
        self.assertEqual(sessions.load(p)[0]["title"], "/plugin install frontend-design")

    def test_title_strips_other_tags(self):
        p = self.write([user("<local-command-stdout>✓ Installed</local-command-stdout>", "2026-09-01T10:00:00Z")])
        self.assertEqual(sessions.load(p)[0]["title"], "✓ Installed")

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

    def test_tokens_without_message_id_all_counted(self):
        u = {"output_tokens": 5}
        p = self.write([user("hi", "2026-09-01T10:00:00Z"),
                        rec("assistant", timestamp="2026-09-01T10:00:01Z",
                            message={"role": "assistant", "content": [{"type": "text", "text": "a"}], "usage": u}),
                        rec("assistant", timestamp="2026-09-01T10:00:02Z",
                            message={"role": "assistant", "content": [{"type": "text", "text": "b"}], "usage": u})])
        self.assertEqual(sessions.load(p)[0]["tokens"], 10)

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
        self.assertEqual([s["id"] for s in sessions.list_sessions()], [SID2, SID])

    def test_list_skips_empty_session(self):
        self.write([rec("mode")], sid="cccccccc-bbbb-cccc-dddd-eeeeeeeeeeee")
        self.assertEqual(len(sessions.list_sessions()), 2)

    def test_search_case_insensitive(self):
        self.assertEqual([h["id"] for h in sessions.search("PYTHON")], [SID2])
        self.assertEqual(sorted(h["id"] for h in sessions.search("세션")), sorted([SID, SID2]))

    def test_search_snippet_contains_query(self):
        self.assertIn("python", sessions.search("python")[0]["snippet"])

    def test_search_empty_query(self):
        self.assertEqual(sessions.search("   "), [])

    def test_find_path(self):
        self.assertEqual(sessions.find_path(SID), self.old)
        self.assertIsNone(sessions.find_path("../../etc/passwd"))
        self.assertIsNone(sessions.find_path("99999999-2222-3333-4444-555555555555"))

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
        self.assertEqual([s["id"] for s in sessions.list_sessions()], [SID2, SID])
        self.assertEqual(len(sessions.search("세션")), 2)

    def test_cache_reloads_after_change(self):
        self.assertEqual(sessions.cached(self.old)[0]["messageCount"], 1)
        self.write([user("a", "2026-09-01T10:00:00Z"), user("b", "2026-09-01T10:01:00Z")])
        st = self.old.stat()
        os.utime(self.old, (st.st_atime, st.st_mtime + 5))  # 같은 초 안의 재기록도 감지되는지 보장
        self.assertEqual(sessions.cached(self.old)[0]["messageCount"], 2)


class ListTest(Base):
    def by_id(self):
        return {s["id"]: s for s in sessions.list_sessions()}

    def test_flags_default(self):
        self.session()
        s = self.by_id()[SID]
        self.assertEqual({k: s[k] for k in ("star", "note", "archived", "archiveOnly", "trashed", "live")},
                         {"star": False, "note": "", "archived": False, "archiveOnly": False, "trashed": False, "live": False})

    def test_meta_and_archive_reflected(self):
        self.session()
        sessions.set_meta(SID, star=True, note="중요")
        sessions.archive(SID)
        s = self.by_id()[SID]
        self.assertEqual((s["star"], s["note"], s["archived"]), (True, "중요", True))

    def test_live_flag(self):
        self.session(age=0)
        self.assertTrue(self.by_id()[SID]["live"])

    def test_archive_refreshed_when_original_changes(self):
        p = self.session()
        sessions.archive(SID)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(user("추가된 대화", "2026-09-02T10:00:00Z"), ensure_ascii=False) + "\n")
        sessions.list_sessions()
        self.assertIn("추가된 대화", sessions.bundle_jsonl("archive", SID).read_text(encoding="utf-8"))

    def test_archive_only_after_original_removed(self):
        p = self.session()
        sessions.archive(SID)
        p.unlink()
        s = self.by_id()[SID]
        self.assertEqual((s["archiveOnly"], s["archived"]), (True, True))
        self.assertEqual(sessions.locate(SID), sessions.bundle_jsonl("archive", SID))
        self.assertEqual([h["id"] for h in sessions.search("관리")], [SID])

    def test_trashed_listed_but_not_searched(self):
        self.session()
        sessions.trash(SID)
        self.assertTrue(self.by_id()[SID]["trashed"])
        self.assertEqual(sessions.locate(SID), sessions.bundle_jsonl("trash", SID))
        self.assertEqual(sessions.search("관리"), [])

    def test_failed_refresh_keeps_previous_archive(self):
        p = self.session()
        sessions.archive(SID)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(user("새 대화", "2026-09-02T10:00:00Z"), ensure_ascii=False) + "\n")
        with mock.patch.object(sessions.shutil, "copy2", side_effect=OSError(28, "No space left")):
            sessions.list_sessions()
        copy = sessions.bundle_jsonl("archive", SID)
        self.assertIsNotNone(copy)
        self.assertIn("관리 대상", copy.read_text(encoding="utf-8"))

    def test_refresh_repairs_archive_missing_jsonl(self):
        self.session()
        (self.store / "archive" / SID / "projects" / "-Users-jun-dev-demo").mkdir(parents=True)
        sessions.list_sessions()
        self.assertIsNotNone(sessions.bundle_jsonl("archive", SID))

    def test_refresh_keeps_archived_folder_removed_from_original(self):
        p = self.session()
        sessions.archive(SID)
        shutil.rmtree(self.claude / "file-history" / SID)
        os.utime(p)
        sessions.list_sessions()
        self.assertTrue((self.store / "archive" / SID / "file-history" / SID / "f").exists())

    def test_trash_wins_over_archive(self):
        self.session()
        sessions.archive(SID)
        sessions.trash(SID)
        s = self.by_id()[SID]
        self.assertEqual((s["trashed"], s["archiveOnly"], s["archived"]), (True, False, True))


class StoreTest(Base):
    def rels(self, sid=SID, project="-Users-jun-dev-demo"):
        return [Path("projects") / project / f"{sid}.jsonl", Path("projects") / project / sid / "tool-results" / "r.txt",
                Path("file-history") / sid / "f", Path("session-env") / sid / "e"]

    def test_archive_copies_bundle_and_keeps_original(self):
        self.session()
        sessions.archive(SID)
        for r in self.rels():
            self.assertTrue((self.store / "archive" / SID / r).exists(), r)
            self.assertTrue((self.claude / r).exists(), r)

    def test_archive_preserves_mtime(self):
        p = self.session()
        sessions.archive(SID)
        self.assertEqual(sessions.bundle_jsonl("archive", SID).stat().st_mtime, p.stat().st_mtime)

    def test_trash_then_restore_roundtrip(self):
        self.session()
        sessions.trash(SID)
        for r in self.rels():
            self.assertFalse((self.claude / r).exists(), r)
            self.assertTrue((self.store / "trash" / SID / r).exists(), r)
        sessions.restore(SID)
        for r in self.rels():
            self.assertTrue((self.claude / r).exists(), r)
        self.assertFalse((self.store / "trash" / SID).exists())

    def test_restore_can_retry_after_partial_failure(self):
        self.session()
        sessions.trash(SID)
        real_move, calls = sessions._move, []

        def flaky(src, dst):
            calls.append(src)
            if len(calls) == 2:
                raise OSError(5, "I/O error")
            real_move(src, dst)

        with mock.patch.object(sessions, "_move", flaky):
            with self.assertRaises(OSError):
                sessions.restore(SID)
        # 대화 본체가 아직 휴지통에 있어야 목록에서 '휴지통'으로 보이고 다시 복원할 수 있음
        self.assertIsNotNone(sessions.bundle_jsonl("trash", SID))
        sessions.restore(SID)
        for r in self.rels():
            self.assertTrue((self.claude / r).exists(), r)
        self.assertFalse((self.store / "trash" / SID).exists())

    def test_restore_refuses_when_original_back(self):
        self.session()
        sessions.trash(SID)
        self.session()
        with self.assertRaises(sessions.Conflict):
            sessions.restore(SID)
        self.assertTrue(sessions.bundle_jsonl("trash", SID).exists())

    def test_trash_live_session_needs_force(self):
        self.session(age=0)
        with self.assertRaises(sessions.Conflict):
            sessions.trash(SID)
        sessions.trash(SID, force=True)
        self.assertIsNone(sessions.find_path(SID))

    def test_purge_only_from_trash(self):
        self.session()
        with self.assertRaises(sessions.NotFound):
            sessions.purge(SID)
        sessions.trash(SID)
        sessions.purge(SID)
        self.assertFalse((self.store / "trash" / SID).exists())
        self.assertIsNone(sessions.find_path(SID))

    def test_purge_rejects_escape(self):
        with self.assertRaises(sessions.NotFound):
            sessions.purge("../archive")

    def test_revive_copies_archive_back(self):
        p = self.session()
        sessions.archive(SID)
        p.unlink()
        sessions.revive(SID)
        self.assertTrue(p.exists())
        self.assertTrue(sessions.bundle_jsonl("archive", SID).exists())
        with self.assertRaises(sessions.Conflict):
            sessions.revive(SID)

    def test_unarchive(self):
        self.session()
        sessions.archive(SID)
        sessions.unarchive(SID)
        self.assertFalse((self.store / "archive" / SID).exists())
        with self.assertRaises(sessions.NotFound):
            sessions.unarchive(SID)

    def test_unarchive_only_copy_needs_force(self):
        p = self.session()
        sessions.archive(SID)
        p.unlink()
        with self.assertRaises(sessions.Conflict):
            sessions.unarchive(SID)
        self.assertIsNotNone(sessions.bundle_jsonl("archive", SID))
        sessions.unarchive(SID, force=True)
        self.assertFalse((self.store / "archive" / SID).exists())

    def test_missing_session_not_found(self):
        for fn in (sessions.archive, sessions.trash, sessions.restore, sessions.revive):
            with self.assertRaises(sessions.NotFound):
                fn(SID)

    def test_meta_roundtrip(self):
        sessions.set_meta(SID, star=True)
        sessions.set_meta(SID, note="메모")
        self.assertEqual(sessions.read_meta()[SID], {"star": True, "note": "메모"})

    def test_corrupt_meta_is_not_overwritten(self):
        self.store.mkdir()
        (self.store / "meta.json").write_text("{broken")
        with self.assertRaises(ValueError):
            sessions.set_meta(SID, star=True)
        self.assertEqual((self.store / "meta.json").read_text(), "{broken")
        self.assertEqual(sessions.read_meta(), {})

    def test_non_dict_meta_entry_ignored(self):
        self.session()
        self.store.mkdir()
        (self.store / "meta.json").write_text(json.dumps({SID: "x"}))
        s = sessions.list_sessions()[0]
        self.assertEqual((s["star"], s["note"]), (False, ""))
        self.assertEqual(sessions.set_meta(SID, star=True), {"star": True})

    def test_history_jsonl_untouched(self):
        h = self.claude / "history.jsonl"
        h.write_text('{"display":"x"}\n')
        self.session()
        sessions.archive(SID)
        sessions.trash(SID)
        sessions.restore(SID)
        self.assertEqual(h.read_text(), '{"display":"x"}\n')


class HttpTest(Base):
    def setUp(self):
        super().setUp()
        sessions._cache.clear()
        self.write([user("hi <img src=x onerror=alert(1)>", "2026-09-01T10:00:00Z")])
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), sessions.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
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

    def post(self, path, body=None, header=True, host=None):
        c = HTTPConnection("127.0.0.1", self.port)
        headers = {"Host": host or f"127.0.0.1:{self.port}", "Content-Type": "application/json"}
        if header:
            headers["X-Session-Viewer"] = "1"
        c.request("POST", path, body=json.dumps(body or {}), headers=headers)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, json.loads(data or b"{}")

    def test_post_requires_custom_header(self):
        self.assertEqual(self.post(f"/api/session/{SID}/meta", {"star": True}, header=False)[0], 403)
        self.assertEqual(self.post(f"/api/session/{SID}/meta", {"star": True}, host="evil.com")[0], 403)
        self.assertEqual(sessions.read_meta(), {})

    def test_post_meta(self):
        self.assertEqual(self.post(f"/api/session/{SID}/meta", {"star": True, "note": "메모"}),
                         (200, {"star": True, "note": "메모"}))
        self.assertEqual(json.loads(self.get("/api/sessions")[1])[0]["note"], "메모")

    def test_post_meta_rejects_bad_fields(self):
        for body in ({"star": "yes"}, {"note": "x" * 2001}, {"other": 1}):
            self.assertEqual(self.post(f"/api/session/{SID}/meta", body)[0], 400, body)
        self.assertEqual(sessions.read_meta(), {})

    def test_post_trash_live_then_force(self):
        # setUp 이 방금 쓴 세션이라 '진행 중'으로 판정됨
        status, body = self.post(f"/api/session/{SID}/trash")
        self.assertEqual(status, 409)
        self.assertIn("진행 중", body["error"])
        self.assertEqual(self.post(f"/api/session/{SID}/trash", {"force": True})[0], 200)
        self.assertTrue(json.loads(self.get("/api/sessions")[1])[0]["trashed"])
        self.assertEqual(self.post(f"/api/session/{SID}/restore")[0], 200)
        self.assertFalse(json.loads(self.get("/api/sessions")[1])[0]["trashed"])

    def test_post_unarchive_only_copy_needs_force(self):
        sessions.archive(SID)
        sessions.find_path(SID).unlink()
        status, body = self.post(f"/api/session/{SID}/unarchive")
        self.assertEqual(status, 409)
        self.assertIn("유일한 사본", body["error"])
        self.assertEqual(self.post(f"/api/session/{SID}/unarchive", {"force": True})[0], 200)

    def test_post_errors(self):
        self.assertEqual(self.post(f"/api/session/{SID}/purge")[0], 404)
        self.assertEqual(self.post(f"/api/session/{SID}/revive")[0], 404)
        self.assertEqual(self.post(f"/api/session/{SID}/explode")[0], 404)
        self.assertEqual(self.post("/api/session/../../x/trash")[0], 404)
        self.assertEqual(self.post("/api/session/not-a-uuid/archive")[0], 404)


class LifecycleTest(unittest.TestCase):
    def wait_http(self, port, want_up, seconds=8):
        for _ in range(int(seconds / 0.2)):
            try:
                c = HTTPConnection("127.0.0.1", port, timeout=1)
                c.request("GET", "/api/search", headers={"Host": f"127.0.0.1:{port}"})
                up = c.getresponse().status == 200
                c.close()
            except OSError:
                up = False
            if up == want_up:
                return True
            time.sleep(0.2)
        return False

    def test_server_uses_sv_port_and_exits_when_parent_dies(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "SV_PORT": str(port), "SV_WATCH_PARENT": "1",
                   "SV_CLAUDE_DIR": tmp, "SV_STORE_DIR": tmp}
            # 앱 역할을 하는 중간 프로세스가 서버를 띄우고 자기 pid 를 알려준 뒤 대기
            parent = subprocess.Popen(
                [sys.executable, "-c",
                 "import subprocess, sys, time; p = subprocess.Popen([sys.executable, sys.argv[1]], "
                 "stdout=subprocess.DEVNULL); print(p.pid, flush=True); time.sleep(60)",
                 sessions.__file__], env=env, stdout=subprocess.PIPE, text=True)
            child = int(parent.stdout.readline())
            try:
                self.assertTrue(self.wait_http(port, True), "SV_PORT 로 서버가 뜨지 않음")
                parent.kill()
                parent.wait()
                self.assertTrue(self.wait_http(port, False), "부모가 죽었는데 서버가 남아 있음")
            finally:
                parent.kill()
                try:
                    os.kill(child, signal.SIGTERM)  # 실패한 경우에도 서버가 남지 않게 정리
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    unittest.main()
