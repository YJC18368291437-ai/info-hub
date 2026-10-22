"""Newspaper layout, classification fallback, forward parsing and dispatch idempotency."""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from info_hub.db import Database
from info_hub.newspaper import (NewspaperDispatcher, TZ, fallback_notice_item, latest_slot,
                                render_notice, render_tech, sample_notice_items,
                                sample_tech_report, validate_html, _one_sentence, _two_col,
                                _tg_originals)
from info_hub.vision import clean_transcript, compare
from info_hub.opencode_runner import RUN_PROMPT, agent_markdown, validate_report
from info_hub.outbox import DeliveryError, Outbox
from info_hub.qq_collect import _forward_text, _resolve_role, _role_allowed
from info_hub.settings import _validate_newspaper

TMP = Path(__file__).resolve().parents[1] / "data" / "test-tmp"
TMP.mkdir(parents=True, exist_ok=True)


def notice_settings(**kw):
    s = SimpleNamespace(
        base_dir=Path("."), mail_enabled=True, mail_address="fixture@example.invalid",
        mail_auth_code="fake", mail_from_name="fixture", mail_send_mode="smtp",
        newspaper={
            "start_at": "2026-10-19T00:00:00+08:00",
            "notice": {
                "hours": [8, 12, 18], "max_items": 20, "classify_batch_size": 5,
                "course_groups": {"123456781": "微积分"},
                "course_hint_groups": {"123456790": "网球"},
                "mandatory_groups": {"123456791": "某大类"},
                "role_filter_groups": {"123456789": ["owner", "admin"]},
                "image_forward_groups": {"123456795": {"name": "某校消息",
                                                         "max_images": 9,
                                                         "max_bytes": 10485760}},
            },
            "tech": {
                "hour": 18, "max_wait_hours": 24, "max_messages": 50,
                "groups": {"123456796": "AIA", "123456797": "AI小屋"},
                "special_groups": {"123456798": {"name": "某技术群",
                                                   "user_id": "123456799",
                                                   "column_title": "某技术群 · 群户成峰"}},
                "keywords": ["模型", "代码"],
            },
            "opencode": {"agent": "info-hub-research", "timeout_seconds": 60},
        },
        newspaper_enabled=True, newspaper_official_send=True, newspaper_legacy_events=False,
    )
    for k, v in kw.items():
        setattr(s, k, v)
    return s


class LayoutTests(unittest.TestCase):
    def test_notice_is_table_layout_with_two_categories(self):
        now = _dt.datetime(2026, 10, 19, 8, 0, tzinfo=TZ)
        items = sample_notice_items(now)
        html, text = render_notice(now, items, len(items))
        self.assertIn("<!doctype html>", html)
        self.assertIn("<table", html)
        self.assertIn("第一类 · 我的课程", html)
        self.assertIn("第二类 · 每个同学都要做", html)
        self.assertIn("考试 / 测验", html)
        self.assertIn("2026年10月19日", html)
        self.assertNotIn("<script", html.lower())
        self.assertIn("某校消息 · 通知报", text)

    def test_tech_has_guotie_separate_column_and_labels(self):
        now = _dt.datetime(2026, 10, 19, 18, 0, tzinfo=TZ)
        html, text = render_tech(now, sample_tech_report(), forced=False, s=notice_settings())
        self.assertIn("技 术 情 报 报", html)
        self.assertIn("某技术群 · 群户成峰", html)
        self.assertIn("AI 补充", html)
        self.assertIn("未验证", html)
        self.assertNotIn("<script", html.lower())
        self.assertIn("== 某技术群 · 群户成峰 ==", text)

    def test_validate_html_rejects_scripts_and_handlers(self):
        self.assertIn("<table", validate_html("<table></table>"))
        for bad in ("<script>x</script><table></table>",
                    '<table><td onclick="x()"></td></table>',
                    "<table><td>javascript:alert(1)</td></table>"):
            with self.assertRaises(ValueError):
                validate_html(bad)

    def test_incomplete_tech_has_pending_banner(self):
        report = sample_tech_report()
        report["ready"] = False
        report["missing"] = ["缺少显存实测数据"]
        html, _ = render_tech(_dt.datetime.now(TZ), report, forced=True, s=notice_settings())
        self.assertIn("本期资料尚不完整", html)
        self.assertIn("缺少显存实测数据", html)

    def test_notice_two_columns_have_at_most_two_cells_per_row(self):
        now = _dt.datetime(2026, 10, 19, 8, 0, tzinfo=TZ)
        base = sample_notice_items(now)[0]
        items = [dict(base, id=i, title=f"item-{i}") for i in range(5)]
        html = _two_col(items, "#000")
        rows = __import__("re").findall(r"<tr>(.*?)</tr>", html, __import__("re").S)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row.count("<td ") == 2 for row in rows))

    def test_group_file_without_url_is_marked_for_group_view(self):
        now = _dt.datetime(2026, 10, 19, 8, 0, tzinfo=TZ)
        base = sample_notice_items(now)[0]
        item = dict(base, id=99, category="course", subcategory="other",
                    title="普通生物学群文件", summary="群上传文件一则",
                    images=[], files=[{"url": "ch-6.slides.pdf",
                                       "name": "ch-6.slides.pdf", "kind": "file"}])
        html, text = render_notice(now, [item], 1)
        self.assertIn("群内查看", html)
        self.assertIn("群内查看", text)


class ClassificationTests(unittest.TestCase):
    def block(self, text="", title="", images="[]", files="[]", bid=1):
        return {"id": bid, "text": text, "title": title, "images": images,
                "files": files, "created_ts": 123456815, "end_ts": 123456815}

    def test_course_change_is_urgent_and_subcategorized(self):
        item = fallback_notice_item(self.block("明天停课，改到 306 教室"), "course", "化学实验")
        self.assertEqual(item["category"], "course")
        self.assertEqual(item["subcategory"], "class_change")
        self.assertTrue(item["urgent"])

    def test_mandatory_survey_and_tennis_activity(self):
        survey = fallback_notice_item(self.block("请完成心理测评问卷"), "everyone", "某大类")
        self.assertEqual((survey["category"], survey["subcategory"]), ("everyone", "survey"))
        tennis = fallback_notice_item(self.block("周六下午约球，来吗"), "course_hint", "网球")
        self.assertEqual(tennis["category"], "activity")

    def test_latest_slot_before_first_hour_uses_previous_evening(self):
        now = _dt.datetime(2026, 10, 19, 7, 30, tzinfo=TZ)
        self.assertEqual(latest_slot(now, [8, 12, 18]).day, 18)
        now = _dt.datetime(2026, 10, 19, 12, 1, tzinfo=TZ)
        self.assertEqual(latest_slot(now, [8, 12, 18]).hour, 12)

    def test_role_filter_only_owner_admin(self):
        s = notice_settings()
        self.assertTrue(_role_allowed(s, "123456789", "owner"))
        self.assertTrue(_role_allowed(s, "123456789", "admin"))
        self.assertFalse(_role_allowed(s, "123456789", "member"))
        self.assertTrue(_role_allowed(s, "123456791", "member"))


class ReportValidationTests(unittest.TestCase):
    def test_valid_report_and_invalid_shapes(self):
        good = sample_tech_report()
        clean = validate_report(good)
        self.assertTrue(clean["ready"])
        self.assertEqual(clean["sections"][0]["key"], "aia")
        for bad in ({}, {"ready": "yes", "sections": []},
                    {"ready": True, "sections": [{"key": "hack", "items": []}]}):
            with self.assertRaises(ValueError):
                validate_report(bad)

    def test_html_in_strings_is_escaped_by_renderer(self):
        report = sample_tech_report()
        report["sections"][0]["items"][0]["title"] = "<script>alert(1)</script>"
        html, _ = render_tech(_dt.datetime.now(TZ), report, True, notice_settings())
        self.assertNotIn("<script", html.lower())
        self.assertIn("&lt;script&gt;", html)

    def test_fallback_report_marks_pending(self):
        d = NewspaperDispatcher(notice_settings(), None, None, None)
        report = d._fallback_tech([{"id": 1, "column": "guotie", "group": "某技术群",
                                    "time": "10-19 12:00", "text": "模型部署报错"}])
        self.assertFalse(report["ready"])
        self.assertEqual(report["sections"][0]["items"][0]["labels"], ["待补充"])

    def test_opencode_model_front_matter_is_validated(self):
        self.assertIn("model: vendor/model-1", agent_markdown("vendor/model-1"))
        for bad in ("", "model\ninjected: true", "../../escape", 123):
            with self.assertRaises(ValueError):
                agent_markdown(bad)
        self.assertIn("当前目录的 task.md", RUN_PROMPT)
        self.assertNotIn("newspaper_runs", RUN_PROMPT)


class RoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_role_is_resolved_from_group_member_info(self):
        class Bot:
            async def call_api(self, action, **kw):
                self.action = action
                return {"role": "admin"}
        event = SimpleNamespace(user_id=123)
        with patch("info_hub.qq_collect.get_settings", return_value=notice_settings()):
            role = await _resolve_role(Bot(), event, "123456789", "")
        self.assertEqual(role, "admin")

    async def test_non_filtered_group_skips_api(self):
        class Bot:
            async def call_api(self, *a, **k):
                raise AssertionError("should not call")
        event = SimpleNamespace(user_id=123)
        with patch("info_hub.qq_collect.get_settings", return_value=notice_settings()):
            role = await _resolve_role(Bot(), event, "123456791", "")
        self.assertEqual(role, "")


class ForwardTests(unittest.IsolatedAsyncioTestCase):
    async def test_napcat_forward_shapes_and_evidence(self):
        class Bot:
            def __init__(self, payload):
                self.payload = payload
            async def call_api(self, *a, **k):
                return self.payload

        seg = SimpleNamespace(data={"id": "fwd"})
        shapes = [
            ({"messages": [{"type": "node", "data": {
                "sender": {"nickname": "A"},
                "content": [{"type": "text", "data": {"text": "hello"}}]}}]}, "A: hello"),
            ({"messages": [{"sender": {"nickname": "B"}, "content": "plain"}]}, "B: plain"),
            ({"data": {"messages": [{"content": [
                {"type": "text", "data": {"text": "nested"}}]}]}}, "nested"),
            ({"data": [{"type": "node", "data": {"sender": {"nickname": "C"},
                "content": [{"type": "text", "data": {"text": "direct-list"}}]}}]},
             "C: direct-list"),
            ({"foo": "bar"}, "无法解析"),
        ]
        for payload, expected in shapes:
            text = await _forward_text(Bot(payload), seg)
            self.assertIn(expected, text)


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TMP)
        self.db = Database(Path(self.temp.name) / "db.sqlite")
        self.db._conn.execute("PRAGMA synchronous=OFF")
        self.mailer = SimpleNamespace(ready=True, sent=[])
        self.mailer.deliver = AsyncMock(side_effect=self._deliver)
        self.s = notice_settings(base_dir=Path(self.temp.name))
        self.s.newspaper = dict(self.s.newspaper)
        self.clf = SimpleNamespace(newspaper_notice=AsyncMock(return_value=None))
        self.d = NewspaperDispatcher(self.s, self.db, self.clf, self.mailer)

    async def _deliver(self, msgs):
        self.mailer.sent.extend(msgs)
        return len(msgs)

    async def asyncTearDown(self):
        self.db.close()
        self.temp.cleanup()

    def add_block(self, text, group="123456781", mid=1, role="member",
                  images=None, ts=None):
        now = int(ts if ts is not None else _dt.datetime.now(TZ).timestamp())
        self.db.add_message({"msg_id": mid, "group_id": group, "account_id": "x",
                             "user_id": "u", "role": role, "nickname": "n",
                             "ts": now, "text": text, "images": images or []})
        m = self.db._query("SELECT * FROM messages WHERE msg_id=?", (mid,))[0]
        bid = self.db.create_block(group, [m], text, images=images or [])
        self.db.set_block_topic(bid, "notice", "test")
        return bid

    async def test_first_notice_alignment_does_not_send_backlog(self):
        self.s.newspaper["notice"]["hours"] = [0]
        self.add_block("明天停课")
        self.assertEqual(await self.d._notice_tick(), 0)
        self.assertFalse(self.mailer.sent)

    async def test_notice_sends_once_and_marks_item(self):
        self.s.newspaper["notice"]["hours"] = [0]
        built = self.add_block("明天停课，改到 306")
        self.db.kv_set("newspaper:notice:last_slot",
                       (_dt.datetime.now(TZ) - _dt.timedelta(days=1)).isoformat())
        self.assertEqual(await self.d._notice_tick(), 1)
        self.assertEqual(len(self.mailer.sent), 1)
        self.assertIn("紧急", self.mailer.sent[0].get_body(preferencelist=("html",)).get_content())
        self.assertEqual(self.db.newspaper_sent_ids("notice"), set())
        self.assertEqual(self.db.finalize_newspaper_manifests(), 1)
        self.assertEqual(self.db.newspaper_sent_ids("notice"), {built})
        self.assertEqual(await self.d._notice_tick(), 0)

    async def test_urgent_marks_block_so_regular_paper_skips_it(self):
        self.s.newspaper["notice"]["hours"] = [0]
        bid = self.add_block("临时停课")
        self.assertEqual(await self.d.urgent(), 1)
        self.assertEqual(self.db.finalize_newspaper_manifests(), 1)
        self.assertEqual(self.db.newspaper_sent_ids("notice"), {bid})
        self.db.kv_set("newspaper:notice:last_slot",
                       (_dt.datetime.now(TZ) - _dt.timedelta(days=1)).isoformat())
        self.assertEqual(await self.d._notice_tick(), 0)

    async def test_urgent_ignores_question_about_homework(self):
        self.add_block("请问明天要交哪些作业", mid=1)
        self.assertEqual(await self.d.urgent(), 0)
        self.assertFalse(self.mailer.sent)

    async def test_urgent_merges_adjacent_context_and_images(self):
        now = int(_dt.datetime.now(TZ).timestamp())
        self.add_block("前面布置的作业都要交", mid=1, ts=now - 120)
        self.add_block("", mid=2, ts=now - 30,
                       images=["https://example.invalid/homework.png"])
        bid = self.add_block("明天交这些作业", mid=3, ts=now)
        self.assertEqual(await self.d.urgent(), 1)
        self.assertEqual(len(self.mailer.sent), 1)
        html = self.mailer.sent[0].get_body(preferencelist=("html",)).get_content()
        self.assertIn("前面布置的作业都要交", html)
        self.assertIn("查看原图", html)
        self.assertEqual(self.db.finalize_newspaper_manifests(), 1)
        self.assertEqual(self.db.newspaper_sent_ids("notice"), {bid})

    async def test_urgent_batches_triggers_into_one_mail(self):
        now = int(_dt.datetime.now(TZ).timestamp())
        first = self.add_block("明天停课", mid=1, ts=now)
        second = self.add_block("后天补课", mid=2, ts=now)
        self.assertEqual(await self.d.urgent(), 1)
        self.assertEqual(len(self.mailer.sent), 1)
        self.assertEqual(self.db.finalize_newspaper_manifests(), 1)
        self.assertEqual(self.db.newspaper_sent_ids("notice"), {first, second})

    async def test_official_gate_off_sends_nothing(self):
        self.s.newspaper_official_send = False
        self.add_block("明天停课")
        self.assertEqual(await self.d.tick(), 0)
        self.assertFalse(self.mailer.sent)

    async def test_role_filter_blocks_old_member_rows(self):
        member = self.add_block("成员发言", group="123456789", mid=1, role="member")
        admin = self.add_block("管理员通知", group="123456789", mid=2, role="admin")
        self.assertIsNone(self.d._filter_role_block(self.db.get_block(member)))
        self.assertIsNotNone(self.d._filter_role_block(self.db.get_block(admin)))

    async def test_role_filter_rebuilds_mixed_block_from_admin_only(self):
        now = int(_dt.datetime.now(TZ).timestamp())
        for mid, role, text in ((11, "admin", "管理员通知"), (12, "member", "成员闲聊")):
            self.db.add_message({"msg_id": mid, "group_id": "123456789", "account_id": "x",
                                 "user_id": str(mid), "role": role, "nickname": "n",
                                 "ts": now + mid, "text": text})
        rows = self.db._query("SELECT * FROM messages WHERE msg_id IN (11,12) ORDER BY id")
        bid = self.db.create_block("123456789", rows, "管理员通知\n成员闲聊")
        self.db.set_block_topic(bid, "notice", "test")
        filtered = self.d._filter_role_block(self.db.get_block(bid))
        self.assertIsNotNone(filtered)
        self.assertIn("管理员通知", filtered["text"])
        self.assertNotIn("成员闲聊", filtered["text"])

    async def test_role_filter_empty_role_is_skipped(self):
        bid = self.add_block("未知角色", group="123456789", mid=13, role="")
        with self.assertLogs("info_hub.newspaper", level="WARNING"):
            self.assertIsNone(self.d._filter_role_block(self.db.get_block(bid)))
        rows = self.db._query("SELECT state FROM newspaper_items WHERE kind='notice' AND item_id=?", (bid,))
        self.assertEqual(rows[0]["state"], "skipped")

    async def test_nanan_images_forwarded_as_attachments_once(self):
        now = int(_dt.datetime.now(TZ).timestamp())
        self.db.add_message({"msg_id": 77, "group_id": "123456795", "account_id": "x",
                             "user_id": "u", "role": "member", "nickname": "n", "ts": now,
                             "text": "选课通知", "images": ["https://media.example/a.jpg"]})
        self.d._download = AsyncMock(return_value=b"jpg-bytes")
        self.assertEqual(await self.d.forward_images(), 1)
        self.assertEqual(len(self.mailer.sent), 1)
        self.assertEqual(len(list(self.mailer.sent[0].iter_attachments())), 1)
        self.db.finalize_newspaper_manifests()
        self.assertEqual(await self.d.forward_images(), 0)

    async def test_nanan_images_use_telegram_without_mail_attachments(self):
        now = int(_dt.datetime.now(TZ).timestamp())
        self.db.add_message({"msg_id": 80, "group_id": "123456795", "account_id": "x",
                             "user_id": "u", "role": "member", "nickname": "n", "ts": now,
                             "text": "选课通知", "images": ["https://media.example/tg.jpg"]})
        self.s.tg_image_enabled = True
        self.s.newspaper["notice"]["image_text"] = {"mode": "preview", "tg_originals": True}
        with patch("info_hub.newspaper._tg_originals", return_value=True), \
                patch.object(self.d, "forward_originals", new=AsyncMock(return_value=1)) as fwd:
            self.assertEqual(await self.d.forward_images(), 1)
        self.assertEqual(fwd.await_count, 1)
        self.assertEqual(len(list(self.mailer.sent[0].iter_attachments())), 0)
        body = self.mailer.sent[0].get_body(preferencelist=("plain",)).get_content()
        self.assertIn("Telegram", body)

    async def test_nanan_image_download_failure_keeps_link(self):
        now = int(_dt.datetime.now(TZ).timestamp())
        self.db.add_message({"msg_id": 78, "group_id": "123456795", "account_id": "x",
                             "user_id": "u", "role": "member", "nickname": "n", "ts": now,
                             "text": "", "images": ["https://media.example/b.jpg"]})
        self.d._download = AsyncMock(side_effect=ValueError("blocked"))
        self.assertEqual(await self.d.forward_images(), 1)
        body = self.mailer.sent[0].get_body(preferencelist=("plain",)).get_content()
        self.assertIn("media.example", body)

    async def test_nanan_image_limit_keeps_overflow_links_and_passes_byte_limit(self):
        now = int(_dt.datetime.now(TZ).timestamp())
        self.s.newspaper["notice"]["image_forward_groups"]["123456795"].update(
            max_images=2, max_bytes=1234)
        urls = [f"https://media.example/{i}.jpg" for i in range(4)]
        self.db.add_message({"msg_id": 79, "group_id": "123456795", "account_id": "x",
                             "user_id": "u", "role": "member", "nickname": "n", "ts": now,
                             "text": "", "images": urls})
        with patch("info_hub.safe_download.download", new=AsyncMock(return_value=b"jpg")) as dl:
            self.assertEqual(await self.d.forward_images(), 1)
        self.assertEqual(dl.await_count, 2)
        self.assertTrue(all(c.kwargs["max_bytes"] == 1234 for c in dl.await_args_list))
        plain = self.mailer.sent[0].get_body(preferencelist=("plain",)).get_content()
        self.assertIn("超过附件数量上限", plain)
        self.assertIn(urls[2], plain)
        self.assertIn(urls[3], plain)

    async def test_cache_images_schedules_extraction_and_writes_sidecar(self):
        now = int(_dt.datetime.now(TZ).timestamp())
        url = "https://media.example/e.jpg"
        self.s.newspaper["notice"]["image_text"] = {"mode": "preview", "tg_originals": True}
        self.s.glm_api_key = "fake"
        self.db.add_message({"msg_id": 84, "group_id": "123456781", "account_id": "x",
                             "user_id": "u", "role": "member", "nickname": "n", "ts": now,
                             "text": "", "images": [url]})
        self.d._download = AsyncMock(return_value=b"jpg-bytes")
        fake_vision = SimpleNamespace(enabled=True, extract=AsyncMock(return_value={
            "ok": True, "model": "fake", "transcript": "习题3.1\n6 7(2)",
            "compare": {"same": True}, "models": {}, "kind": "blackboard"}))
        with patch("info_hub.vision.get_vision", return_value=fake_vision):
            self.assertEqual(await self.d.cache_images(), 1)
            await asyncio.sleep(0.05)
        path = self.d._media_path(url)
        self.assertTrue(path.exists())
        self.assertTrue(path.with_suffix(".json").exists())
        self.assertEqual(fake_vision.extract.await_count, 1)

    async def test_forward_originals_sends_cached_image_and_marks(self):
        now = int(_dt.datetime.now(TZ).timestamp())
        url = "https://media.example/c.jpg"
        self.s.tg_image_enabled = True
        self.s.newspaper["notice"]["image_text"] = {"mode": "preview", "tg_originals": True}
        self.db.add_message({"msg_id": 83, "group_id": "123456781", "account_id": "x",
                             "user_id": "u", "role": "member", "nickname": "n", "ts": now,
                             "text": "", "images": [url]})
        path = self.d._media_path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"jpg-bytes")
        self.d._save_extraction(url, {
            "ok": True, "model": "fake", "transcript": "习题3.1\n6 7(2) 8",
            "compare": {"same": False}, "models": {}, "kind": "blackboard"})
        fake = SimpleNamespace(enabled=True, send_document=AsyncMock(return_value=123))
        with patch("info_hub.telegram_files.get_tg_files", return_value=fake):
            self.assertEqual(await self.d.forward_originals(), 1)
        self.assertTrue(path.with_suffix(".tg").exists())
        caption = fake.send_document.await_args.args[2]
        self.assertIn("AI 试读", caption)
        self.assertIn("两版识别不一致", caption)
        with patch("info_hub.telegram_files.get_tg_files", return_value=fake):
            self.assertEqual(await self.d.forward_originals(), 0)

    async def test_cache_and_forward_mandatory_group_images(self):
        now = int(_dt.datetime.now(TZ).timestamp())
        url = "https://media.example/mandatory.jpg"
        self.s.tg_image_enabled = True
        self.s.newspaper["notice"]["image_text"] = {"mode": "preview", "tg_originals": True}
        self.db.add_message({"msg_id": 90, "group_id": "123456791", "account_id": "x",
                             "user_id": "u", "role": "member", "nickname": "n", "ts": now,
                             "text": "", "images": [url]})
        self.d._download = AsyncMock(return_value=b"jpg-bytes")
        with patch("info_hub.vision.get_vision",
                   return_value=SimpleNamespace(enabled=False)):
            self.assertEqual(await self.d.cache_images(), 1)
        path = self.d._media_path(url)
        self.assertTrue(path.exists())
        fake = SimpleNamespace(enabled=True, send_document=AsyncMock(return_value=124))
        with patch("info_hub.telegram_files.get_tg_files", return_value=fake):
            self.assertEqual(await self.d.forward_originals(), 1)
        self.assertTrue(path.with_suffix(".tg").exists())

    async def test_mandatory_group_image_gets_ai_preview(self):
        now = int(_dt.datetime.now(TZ).timestamp())
        url = "https://media.example/mandatory-ai.jpg"
        self.s.newspaper["notice"]["image_text"] = {"mode": "preview", "tg_originals": True}
        self.db.add_message({"msg_id": 91, "group_id": "123456791", "account_id": "x",
                             "user_id": "u", "role": "member", "nickname": "n", "ts": now,
                             "text": "", "images": [url]})
        self.d._download = AsyncMock(return_value=b"jpg-bytes")
        fake_vision = SimpleNamespace(enabled=True, extract=AsyncMock(return_value={
            "ok": True, "model": "fake", "transcript": "10月20日 18:30",
            "compare": {"same": True}, "models": {}, "kind": "poster"}))
        with patch("info_hub.vision.get_vision", return_value=fake_vision):
            self.assertEqual(await self.d.cache_images(), 1)
            await asyncio.sleep(0.05)
        self.assertTrue(self.d._media_path(url).with_suffix(".json").exists())
        self.assertEqual(fake_vision.extract.await_count, 1)

    async def test_manifest_crash_recovery_does_not_finalize_new_same_slot_item(self):
        self.s.newspaper["notice"]["hours"] = [0]
        first = self.add_block("作业提交", mid=81)
        self.db.kv_set("newspaper:notice:last_slot",
                       (_dt.datetime.now(TZ) - _dt.timedelta(days=1)).isoformat())
        self.assertEqual(await self.d._notice_tick(), 1)
        second = self.add_block("考试通知", mid=82)
        restarted = NewspaperDispatcher(self.s, self.db, self.clf, self.mailer)
        self.assertEqual(restarted.finalize_sweep(), 1)
        self.assertEqual(self.db.newspaper_sent_ids("notice"), {first})
        self.assertNotIn(second, self.db.newspaper_sent_ids("notice"))
        slot = _dt.datetime.fromisoformat(self.db.kv_get("newspaper:notice:last_slot"))
        with patch("info_hub.newspaper._tz_now", return_value=slot + _dt.timedelta(days=1)):
            self.assertEqual(await restarted._notice_tick(), 1)
        self.assertEqual(len(self.mailer.sent), 2)

    async def test_manifest_unknown_reconcile_then_finalize(self):
        msg = EmailMessage()
        box = Outbox(self.db, SimpleNamespace(deliver=AsyncMock(
            side_effect=DeliveryError("append_write", True))))
        box.queue("newspaper:test", msg, kind="newspaper", newspaper_manifest={
            "kind": "notice", "ids": [41],
            "cursor": {"newspaper:notice:after_id": "41"}})
        self.assertEqual(await box.send("newspaper:test"), 0)
        mid = self.db._query("SELECT message_id FROM deliveries WHERE key='newspaper:test'")[0][0]
        box.mailer.fetcher = SimpleNamespace(locate_messages=lambda: {
            mid: ("inbox", False, None, None)})
        self.assertEqual(await box.reconcile(), 1)
        self.assertEqual(self.db.finalize_newspaper_manifests(), 1)
        self.assertEqual(self.db.newspaper_sent_ids("notice"), {41})
        self.assertEqual(self.db.kv_get("newspaper:notice:after_id"), "41")

    async def test_existing_delivery_does_not_overwrite_payload_or_manifest(self):
        box = Outbox(self.db, self.mailer)
        first = EmailMessage(); first.set_content("first")
        second = EmailMessage(); second.set_content("second")
        box.queue("immutable", first, newspaper_manifest={
            "kind": "notice", "ids": [1], "cursor": {"cursor": "1"}})
        before = self.db._query("SELECT payload FROM deliveries WHERE key='immutable'")[0][0]
        box.queue("immutable", second, newspaper_manifest={
            "kind": "notice", "ids": [2], "cursor": {"cursor": "2"}})
        after = self.db._query("SELECT payload FROM deliveries WHERE key='immutable'")[0][0]
        manifest = self.db._query(
            "SELECT ids_json,cursor_json FROM newspaper_delivery_manifest WHERE delivery_key='immutable'")[0]
        self.assertEqual(before, after)
        self.assertEqual(json.loads(manifest["ids_json"]), [1])
        self.assertEqual(json.loads(manifest["cursor_json"]), {"cursor": "1"})


class NewspaperSettingsTests(unittest.TestCase):
    def test_nested_newspaper_types_and_ranges_fail_closed(self):
        base = notice_settings()
        invalid = [
            ("notice", "max_items", "many"),
            ("notice", "classify_batch_size", 0),
            ("tech", "max_wait_hours", -1),
            ("tech", "max_messages", []),
        ]
        for section, key, value in invalid:
            with self.subTest(section=section, key=key):
                s = notice_settings()
                s.newspaper = json.loads(json.dumps(s.newspaper))
                s.newspaper[section][key] = value
                with self.assertRaises(ValueError):
                    _validate_newspaper(s)
        for value in ("admin", [], ["root"]):
            s = notice_settings()
            s.newspaper = json.loads(json.dumps(s.newspaper))
            s.newspaper["notice"]["role_filter_groups"]["123456789"] = value
            with self.assertRaises(ValueError):
                _validate_newspaper(s)


class ImagePreviewTests(unittest.TestCase):
    def test_one_sentence_keeps_only_first_sentence(self):
        self.assertEqual(_one_sentence("第一句。第二句。"), "第一句。")
        self.assertTrue(_one_sentence("很长" * 100).endswith("…"))

    def test_ai_summary_adds_missing_date_and_time(self):
        from info_hub.newspaper import NewspaperDispatcher
        d = NewspaperDispatcher(notice_settings(), None, None, None)
        text = d._preview_text({
            "summary": "人工智能通识课开课。",
            "facts": {"date": "2026年10月20日", "time": "18:30-20:20"},
        })
        self.assertEqual(text, "2026年10月20日，18:30-20:20，人工智能通识课开课。")

    def test_clock_and_spacing_do_not_trigger_diff(self):
        a = "2026.10.18 17:44\n习题3.2\n1.(奇数)\n2"
        b = "习题3.2\n1.(奇数)\n2"
        self.assertTrue(compare(a, b)["same"])

    def test_critical_term_diff_is_flagged(self):
        a = "习题2.2\n1. 2(奇数)"
        b = "习题2.2\n1,2(待发)"
        diff = compare(a, b)
        self.assertFalse(diff["same"])
        self.assertIn("(奇数)", diff["terms_only_a"])
        self.assertIn("(待发)", diff["terms_only_b"])

    def test_circled_and_fullwidth_parens_normalize(self):
        a = "19(①)(④)\n12（偶数）"
        b = "19(1)(4)\n12(偶数)"
        self.assertTrue(compare(a, b)["same"])

    def test_truncated_second_model_is_tolerated(self):
        a = "习题1.4\n11(2)(3)"
        b = "习题1.4\n11(2)(3"
        self.assertTrue(compare(a, b)["same"])

    def test_clean_transcript_drops_clock(self):
        self.assertEqual(clean_transcript("2026.10.18 17:44\n习题3.2"), "习题3.2")

    def test_render_notice_shows_one_sentence_ai_preview(self):
        item = {
            "id": 1, "category": "course", "subcategory": "homework",
            "title": "习题3.1", "summary": "6、7(2)、8", "urgent": False,
            "group": "微积分", "time": "10-18 17:44", "images": [], "files": [],
            "ai_preview": {"text": "习题3.1 第6、7(2)、8题。", "agree": False,
                           "verified": True, "alt": "6.7(2)8", "kinds": ["blackboard"]},
        }
        html, text = render_notice(_dt.datetime(2026, 10, 19, 12, tzinfo=TZ), [item], 1)
        validate_html(html)
        self.assertIn("AI 试读", html)
        self.assertIn("两版识别不一致", html)
        self.assertNotIn("另一版", html)
        self.assertIn("AI 试读", text)
        self.assertNotIn("另一版", text)

    def test_tg_originals_requires_both_switch_and_image_flag(self):
        s = notice_settings(tg_image_enabled=True)
        s.newspaper["notice"]["image_text"] = {"tg_originals": True}
        self.assertTrue(_tg_originals(s))
        s.tg_image_enabled = False
        self.assertFalse(_tg_originals(s))


if __name__ == "__main__":
    unittest.main()
