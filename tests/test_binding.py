"""Run with `python -m unittest discover -s tests -v` in an AstrBot environment."""

import asyncio
import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, Plain
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.star_handler import star_handlers_registry

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT.parent))
plugin = importlib.import_module(f"{PLUGIN_ROOT.name}.main")

ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:test-course
DTSTART:20261015T080000Z
DTEND:20261015T090000Z
SUMMARY:Test course
END:VEVENT
END:VCALENDAR
"""


def event(user="bob", group="group", *, admin=False, role="member", platform="aiocqhttp", mentions=()):
    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
    message.group_id = group
    message.self_id = "bot"
    message.sender = MessageMember(user_id=user, nickname=user)
    message.message = [At(qq=uid) for uid in mentions]
    message.message_str = ""
    message.raw_message = {"sender": {"role": role}}
    result = AstrMessageEvent("", message, PlatformMetadata(platform, "test", "test"), group or user)
    result.role = "admin" if admin else "member"
    return result


async def replies(generator):
    return "\n".join([
        component.text
        async for result in generator
        for component in result.chain
        if isinstance(component, Plain)
    ])


class BindingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = plugin.Main.__new__(plugin.Main)
        manager = plugin.DataManager.__new__(plugin.DataManager)
        manager.data_path = Path(self.temp.name)
        manager.ics_path = manager.data_path / "ics"
        manager.user_data_file = manager.data_path / "userdata.json"
        manager._init_data()
        self.app.data_manager = manager
        self.app.ics_parser = plugin.ICSParser()
        self.app.user_data = {}
        self.app.binding_requests = {}
        self.app.helper = plugin.ScheduleHelper(manager, self.app.ics_parser, self.app.user_data)

    def bind(self, group="group", user="alice", code="ABC234", nickname="Alice"):
        path = self.app.data_manager.get_ics_file_path(user, group)
        path.write_text(ICS, encoding="utf-8")
        self.app._finish_binding(group, user, nickname, code)
        return path

    async def test_same_group_reuse_preserves_existing_schedule(self):
        self.bind()
        path = self.bind(user="bob", code="XYZ789", nickname="Bob")
        before = path.read_bytes(), self.app.data_manager.user_data_file.read_bytes()
        text = await replies(self.app.link_schedule(event(), " abc234 "))
        self.assertIn("不能重复关联", text)
        self.assertEqual(before, (path.read_bytes(), self.app.data_manager.user_data_file.read_bytes()))

    async def test_first_link_from_private_then_reuse_is_rejected(self):
        self.bind("private")
        self.assertIn("关联成功", await replies(self.app.link_schedule(event(user="alice-group"), "ABC234")))
        self.assertIn("不能重复关联", await replies(self.app.link_schedule(event(), "ABC234")))
        self.assertIsNone(self.app.helper.get_user_record("group", "bob"))

    async def test_concurrent_links_only_one_succeeds(self):
        self.bind("private")
        results = await asyncio.gather(*(
            replies(self.app.link_schedule(event(user=user), "ABC234")) for user in ("one", "two")
        ))
        self.assertEqual(sum("关联成功" in text for text in results), 1)
        self.assertEqual(len(self.app.user_data["group"]["users"]), 1)

    async def test_own_repeat_can_update_nickname_without_duplicate_or_same_file_error(self):
        path = self.bind()
        text = await replies(self.app.link_schedule(event(user="alice"), "ABC234", "New name"))
        self.assertIn("关联成功", text)
        self.assertEqual(path.read_text(encoding="utf-8"), ICS)
        self.assertEqual(len(self.app.user_data["group"]["users"]), 1)
        self.assertEqual(self.app.helper.get_user_record("group", "alice")["nickname"], "New name")

    async def test_own_repeat_can_restore_missing_destination_file(self):
        self.bind("private")
        path = self.bind()
        path.unlink()
        self.assertIn("关联成功", await replies(self.app.link_schedule(event(user="alice"), "ABC234")))
        self.assertTrue(path.exists())

    async def test_cross_group_and_private_reuse(self):
        source = self.bind()
        for group in ("other-group", ""):
            with self.subTest(group=group):
                self.assertIn("关联成功", await replies(self.app.link_schedule(event(group=group), "ABC234")))
                self.assertEqual(source.read_bytes(), self.app.data_manager.get_ics_file_path("bob", group or "private").read_bytes())

    async def test_private_scope_is_not_treated_as_one_shared_group(self):
        self.bind("private")
        self.assertIn("关联成功", await replies(self.app.link_schedule(event(group=""), "ABC234")))

    async def test_invalid_or_missing_source_code_does_not_bind(self):
        self.assertIn("没有找到", await replies(self.app.link_schedule(event(), "INVALID")))
        self.bind("private").unlink()
        self.assertIn("已不存在", await replies(self.app.link_schedule(event(), "ABC234")))
        self.assertIsNone(self.app.helper.get_user_record("group", "bob"))

    async def test_existing_duplicates_are_rejected_without_auto_deletion(self):
        self.bind()
        self.bind(user="bob")
        self.assertIn("不能重复关联", await replies(self.app.link_schedule(event(), "ABC234")))
        self.assertEqual(len(self.app.user_data["group"]["users"]), 2)

    async def test_conflict_survives_reload(self):
        self.bind()
        self.app.user_data = self.app.data_manager.load_user_data()
        self.app.helper.user_data = self.app.user_data
        self.assertIn("不能重复关联", await replies(self.app.link_schedule(event(), "ABC234")))

    def test_new_code_avoids_collision(self):
        self.bind(code="AAAAAA")
        with patch.object(plugin.secrets, "choice", side_effect=list("AAAAAABBBBBB")):
            self.assertEqual(self.app._finish_binding("group", "bob", "Bob"), "BBBBBB")

    async def test_member_cannot_delete(self):
        path = self.bind()
        self.assertIn("仅 AstrBot 管理员", await replies(self.app.delete_schedule(event(), "alice")))
        self.assertTrue(path.exists())
        self.assertIsNotNone(self.app.helper.get_user_record("group", "alice"))

    async def test_delete_cleans_only_target_group_and_pending_request(self):
        path = self.bind()
        other_path = self.bind("other-group")
        private_path = self.bind("private")
        self.app.ics_parser.parse_ics_file(str(path))
        self.app.ics_parser.parse_ics_file(str(other_path))
        self.app.binding_requests.update({"group-alice": {}, "other-group-alice": {}})
        text = await replies(self.app.delete_schedule(event(admin=True), "alice"))
        self.assertIn("已删除", text)
        self.assertFalse(path.exists())
        self.assertNotIn(str(path), self.app.ics_parser.course_cache)
        self.assertNotIn("group-alice", self.app.binding_requests)
        self.assertTrue(other_path.exists())
        self.assertTrue(private_path.exists())
        self.assertIn(str(other_path), self.app.ics_parser.course_cache)
        self.assertIn("other-group-alice", self.app.binding_requests)
        saved = self.app.data_manager.load_user_data()
        self.assertNotIn("alice", saved["group"]["users"])
        self.assertIn("alice", saved["private"]["users"])

    async def test_onebot_group_admin_and_owner_can_delete(self):
        for role in ("owner", "admin"):
            with self.subTest(role=role):
                self.bind()
                self.assertIn("已删除", await replies(self.app.delete_schedule(event(role=role), "alice")))

    async def test_official_bot_requires_astrbot_admin(self):
        for platform in ("qq_official", "qq_official_webhook"):
            with self.subTest(platform=platform):
                self.bind()
                self.assertIn("仅 AstrBot 管理员", await replies(self.app.delete_schedule(event(platform=platform, role="owner"), "alice")))
                self.assertIn("已删除", await replies(self.app.delete_schedule(event(platform=platform, admin=True), "<@!alice>")))

    async def test_malformed_permission_metadata_fails_closed(self):
        self.bind()
        for raw in (None, {}, {"sender": None}, {"sender": "admin"}):
            with self.subTest(raw=raw):
                request = event()
                request.message_obj.raw_message = raw
                self.assertIn("仅 AstrBot 管理员", await replies(self.app.delete_schedule(request, "alice")))

    async def test_private_admin_cannot_delete_other_users(self):
        path = self.bind("private")
        self.assertIn("仅支持群聊", await replies(self.app.delete_schedule(event(group="", admin=True), "alice")))
        self.assertTrue(path.exists())

    async def test_missing_target_and_bot_or_all_mentions_do_not_delete_self(self):
        path = self.bind(user="bob")
        for mentions in ((), ("bot",), ("all",)):
            with self.subTest(mentions=mentions):
                self.assertIn("请指定", await replies(self.app.delete_schedule(event(admin=True, mentions=mentions))))
                self.assertTrue(path.exists())

    async def test_mention_and_nickname_targets(self):
        for token, mentions in (("", ("bot", "alice")), ("Alice", ())):
            with self.subTest(token=token):
                path = self.bind()
                self.assertIn("已删除", await replies(self.app.delete_schedule(event(admin=True, mentions=mentions), token)))
                self.assertFalse(path.exists())

    async def test_ambiguous_nickname_is_not_deleted(self):
        self.bind()
        self.bind(user="bob")
        self.assertIn("多位群友", await replies(self.app.delete_schedule(event(admin=True), "Alice")))
        self.assertEqual(len(self.app.user_data["group"]["users"]), 2)

    async def test_command_dispatch_resolves_onebot_mentions_with_spaces(self):
        path = self.bind(user="123456")
        request = event(admin=True, mentions=("bot", "123456"))
        request.message_str = "删除课表 @Alice Example(123456)"
        request.is_at_or_wake_command = True
        handler = next(
            item for item in star_handlers_registry.star_handlers_map.values()
            if item.handler == plugin.Main.delete_schedule
        )
        command = next(item for item in handler.event_filters if isinstance(item, CommandFilter))
        self.assertTrue(command.filter(request, {}))
        text = await replies(self.app.delete_schedule(request, **request.get_extra("parsed_params")))
        self.assertIn("已删除", text)
        self.assertFalse(path.exists())

    async def test_literal_at_text_does_not_impersonate_a_real_mention(self):
        path = self.bind(user="123456")
        text = await replies(self.app.delete_schedule(event(admin=True), "@Alice(123456)"))
        self.assertIn("还没有", text)
        self.assertTrue(path.exists())

    async def test_missing_binding_or_file_is_handled(self):
        self.assertIn("还没有", await replies(self.app.delete_schedule(event(admin=True), "alice")))
        self.bind().unlink()
        self.assertIn("已删除", await replies(self.app.delete_schedule(event(admin=True), "alice")))

    async def test_self_unbind_still_works(self):
        path = self.bind(user="bob")
        self.assertIn("已解除", await replies(self.app.unbind_schedule(event())))
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
