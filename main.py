import secrets
import shutil
import time
from datetime import date, timedelta

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, File, Plain
from astrbot.core.star import Context, Star, star_map
from astrbot.core.utils.io import download_file

from . import qq_markdown as md
from . import wakeup_client as wakeup
from .data_manager import DataManager
from .ics_parser import ICSParser
from .image_generator import ImageGenerator
from .schedule_helper import ScheduleHelper, today

BIND_TIMEOUT = 60
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class Main(Star):
    """课程表插件"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.context = context
        self.config = config
        self.data_manager = DataManager(star_map[self.__module__])
        self.ics_parser = ICSParser()
        self.image_generator = ImageGenerator()
        self.user_data = self.data_manager.load_user_data()
        self.helper = ScheduleHelper(self.data_manager, self.ics_parser, self.user_data)
        # request_key -> {"timestamp": float, "nickname": str}
        self.binding_requests: dict[str, dict] = {}

    # ---------- 通用工具 ----------

    def _request_key(self, event: AstrMessageEvent) -> str:
        return f"{self.helper.get_scope_id(event)}-{event.get_sender_id()}"

    @staticmethod
    def _session_label(event: AstrMessageEvent) -> str:
        return "当前私聊" if event.is_private_chat() else "当前群聊"

    @staticmethod
    def _display_name(event: AstrMessageEvent, nickname: str = "") -> str:
        """优先使用用户指定昵称；官bot 拿不到昵称时退化为 用户+ID 后四位。"""
        return nickname.strip() or event.get_sender_name() or f"用户{event.get_sender_id()[-4:]}"

    def _mention(self, event: AstrMessageEvent) -> list:
        """群聊里艾特发起人：官bot 用文本标签，其他平台用 At 组件。"""
        if event.is_private_chat():
            return []
        if md.is_qq_official(event):
            return [Plain(md.at_user(event.get_sender_id()) + " ")]
        return [At(qq=event.get_sender_id()), Plain(" ")]

    def _avatar_urls(self, event: AstrMessageEvent, user_ids: list[str]) -> list[str | None]:
        """官bot 的用户 ID 是 openid，要走 qqapp 头像接口；其他平台按 QQ 号取头像。"""
        if md.is_qq_official(event):
            platform = self.context.get_platform_inst(event.get_platform_id())
            appid = getattr(platform, "appid", None)
            if not appid:
                return [None] * len(user_ids)
            return [f"https://q.qlogo.cn/qqapp/{appid}/{uid}/640" for uid in user_ids]
        return [f"https://q1.qlogo.cn/g?b=qq&nk={uid}&s=640" for uid in user_ids]

    def _command(self, name: str) -> str:
        prefix = (self.context.get_config().get("wake_prefix") or ["/"])[0]
        return f"{prefix}{name}"

    # ---------- 官bot Markdown ----------

    def _md_enabled(self, event: AstrMessageEvent, feature: str) -> bool:
        return (
            md.is_qq_official(event)
            and self.config.get("markdown_mode", False)
            and self.config.get("markdown_commands", {}).get(feature, False)
        )

    def _card_buttons(self) -> list[list[dict]]:
        return [
            [
                md.command_button("📅 今日课表", self._command("查看课表")),
                md.command_button("📚 课表帮助", self._command("课表帮助"), style=0),
            ]
        ]

    def _help_buttons(self) -> list[list[dict]]:
        return [
            [
                md.command_button("📥 导入课表", self._command("绑定课表")),
                md.command_button("📅 今日课表", self._command("查看课表")),
            ],
            [
                md.command_button("📆 明日课表", self._command("查看明日课表")),
                md.command_button("👥 群友在上什么课", self._command("群友在上什么课")),
            ],
        ]

    async def _reply_markdown(
        self, event: AstrMessageEvent, content: str, buttons: list[list[dict]]
    ) -> bool:
        """发送 Markdown 卡片；失败时返回 False 让调用方回退到图片/文本。"""
        keyboard = md.build_keyboard(buttons) if self.config.get("markdown_buttons", True) else None
        try:
            await md.send_markdown(event, content, keyboard)
        except Exception as exc:
            logger.warning(f"[CourseSchedule] Markdown 卡片发送失败，回退为普通消息: {exc}")
            return False
        # 卡片已直接发出，终止事件避免 AstrBot 再把指令交给 LLM
        event.stop_event()
        return True

    # ---------- 绑定 ----------

    def _finish_binding(
        self, scope_id: str, user_id: str, nickname: str, code: str | None = None
    ) -> str:
        """写入绑定记录并返回绑定码。重新绑定时沿用原有绑定码。"""
        record = self.helper.get_user_record(scope_id, user_id) or {}
        code = code or record.get("code") or "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        self.helper.save_user_record(scope_id, user_id, {"nickname": nickname, "code": code})
        self.ics_parser.clear_cache(str(self.data_manager.get_ics_file_path(user_id, scope_id)))
        return code

    @filter.command("绑定课表")
    async def bind_schedule(self, event: AstrMessageEvent, nickname: str = ""):
        """绑定课表，可附带显示昵称：/绑定课表 昵称"""
        deadline = time.time() - BIND_TIMEOUT
        self.binding_requests = {k: v for k, v in self.binding_requests.items() if v["timestamp"] > deadline}
        self.binding_requests[self._request_key(event)] = {
            "timestamp": time.time(),
            "nickname": self._display_name(event, nickname),
        }
        where = self._session_label(event)
        prompt = f"请在 {BIND_TIMEOUT} 秒内，在{where}直接发送你的 .ics 课表文件或 WakeUp 分享口令。"
        if md.is_qq_official(event) and not event.is_private_chat():
            prompt += "\n群里发不了文件的话，可以发 WakeUp 口令，或先私聊绑定再回群发送 /关联课表 绑定码。"
        yield event.chain_result(self._mention(event) + [Plain(prompt)])

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_binding_message(self, event: AstrMessageEvent):
        """绑定流程第二步：接收 .ics 文件或 WakeUp 分享口令"""
        request_key = self._request_key(event)
        request = self.binding_requests.get(request_key)
        if not request:
            return
        if time.time() - request["timestamp"] > BIND_TIMEOUT:
            del self.binding_requests[request_key]
            return

        file_component = next((m for m in event.get_messages() if isinstance(m, File)), None)
        share_code = wakeup.parse_share_code(event.message_str)
        if not file_component and not share_code:
            return
        del self.binding_requests[request_key]

        scope_id = self.helper.get_scope_id(event)
        user_id = event.get_sender_id()
        ics_file_path = self.data_manager.get_ics_file_path(user_id, scope_id)
        try:
            if file_component:
                source = await file_component.get_file(allow_return_url=True)
                if source.startswith("http"):
                    await download_file(source, str(ics_file_path))
                else:
                    shutil.copyfile(source, ics_file_path)
            else:
                parts = await wakeup.fetch_wakeup_schedule(share_code)
                ics_file_path.write_text(wakeup.convert_to_ics(parts), encoding="utf-8")
        except Exception as exc:
            logger.error(f"获取课表失败: {exc}")
            yield event.plain_result(f"获取课表失败，绑定未完成：{exc}")
            return

        if self.ics_parser.count_events(str(ics_file_path)) <= 0:
            ics_file_path.unlink(missing_ok=True)
            yield event.plain_result(
                "没有解析到任何课程，请确认发送的是有效的 .ics 课表文件或 WakeUp 分享口令，然后重新 /绑定课表。"
            )
            return

        code = self._finish_binding(scope_id, user_id, request["nickname"])
        yield event.plain_result(
            f"课表绑定成功！已绑定到{self._session_label(event)}。\n"
            f"绑定码：{code}，在其他群或私聊发送 /关联课表 {code} 即可复用这份课表。"
        )

    @filter.command("关联课表")
    async def link_schedule(self, event: AstrMessageEvent, code: str = "", nickname: str = ""):
        """用绑定码把已绑定的课表复用到当前会话：/关联课表 绑定码 [昵称]"""
        scope_id = self.helper.get_scope_id(event)
        user_id = event.get_sender_id()
        code = code.strip().upper()

        if not code:
            record = self.helper.get_user_record(scope_id, user_id)
            if record is None:
                yield event.plain_result(self.helper.get_bind_hint(scope_id))
                return
            code = record.get("code") or self._finish_binding(scope_id, user_id, record.get("nickname", user_id))
            yield event.plain_result(
                f"你的绑定码：{code}，在其他群或私聊发送 /关联课表 {code} 即可复用课表。"
            )
            return

        found = self.helper.find_record_by_code(code)
        if not found:
            yield event.plain_result("没有找到这个绑定码，请检查后重试。")
            return
        src_scope, src_user, src_record = found
        src_path = self.data_manager.get_ics_file_path(src_user, src_scope)
        if not src_path.exists():
            yield event.plain_result("这个绑定码对应的课表文件已不存在，请重新绑定。")
            return

        dst_path = self.data_manager.get_ics_file_path(user_id, scope_id)
        if src_path != dst_path:
            shutil.copyfile(src_path, dst_path)
        display_name = nickname.strip() or src_record.get("nickname") or self._display_name(event)
        self._finish_binding(scope_id, user_id, display_name, code=code)
        yield event.plain_result(f"关联成功！课表已复用到{self._session_label(event)}，显示昵称：{display_name}。")

    @filter.command("解绑课表")
    async def unbind_schedule(self, event: AstrMessageEvent):
        """解除当前会话的课表绑定"""
        scope_id = self.helper.get_scope_id(event)
        user_id = event.get_sender_id()
        if not self.helper.remove_user_record(scope_id, user_id):
            yield event.plain_result(f"你在{self._session_label(event)}还没有绑定课表。")
            return
        ics_file_path = self.data_manager.get_ics_file_path(user_id, scope_id)
        self.ics_parser.clear_cache(str(ics_file_path))
        ics_file_path.unlink(missing_ok=True)
        yield event.plain_result(f"已解除{self._session_label(event)}的课表绑定。")

    # ---------- 帮助 ----------

    @filter.command("课表帮助")
    async def show_help(self, event: AstrMessageEvent):
        """课表功能菜单"""
        if self._md_enabled(event, "help") and await self._reply_markdown(
            event, md.render_help(), self._help_buttons()
        ):
            return
        yield event.plain_result(
            "📚 课表助手\n"
            "/绑定课表 [昵称] - 用 .ics 文件或 WakeUp 口令绑定课表\n"
            "/关联课表 绑定码 - 复用已绑定的课表\n"
            "/解绑课表 - 解除当前会话的绑定\n"
            "/查看课表 - 今天还有什么课\n"
            "/查看明日课表 - 明天有什么课\n"
            "/群友在上什么课 - 群友当前 / 下一节课程\n"
            "/群友明天上什么课 - 群友明天第一节课\n"
            "/本周上课排行 - 本周上课时长排行\n"
            "也可以直接问我“我今天还有什么课”“这周课多不多”。"
        )

    # ---------- 个人课表 ----------

    async def _show_personal_schedule(self, event: AstrMessageEvent, target: date, title: str):
        courses, error_msg = await self.helper.get_personal_courses(event, target)
        if error_msg:
            yield event.plain_result(error_msg)
            return

        nickname = courses[0]["nickname"]
        if self._md_enabled(event, "personal") and await self._reply_markdown(
            event, md.render_personal_schedule(courses, nickname, title, target), self._card_buttons()
        ):
            return

        image_path = await self.image_generator.generate_user_schedule_image(courses, nickname, title)
        event.track_temporary_local_file(image_path)
        yield event.image_result(image_path)

    @filter.command("查看课表")
    async def show_today_schedule(self, event: AstrMessageEvent):
        """查看今天还有什么课"""
        async for result in self._show_personal_schedule(event, today(), "的今日课程"):
            yield result

    @filter.command("查看明日课表")
    async def show_tomorrow_schedule(self, event: AstrMessageEvent):
        """查看明天有什么课"""
        async for result in self._show_personal_schedule(
            event, today() + timedelta(days=1), "的明日课程"
        ):
            yield result

    # ---------- 群友课表 ----------

    async def _show_group_schedule(self, event: AstrMessageEvent, target: date, is_today: bool):
        if event.is_private_chat():
            yield event.plain_result(self.helper.get_group_only_message())
            return

        rows, error_msg = await self.helper.get_group_schedule_for_date(event, target, is_today)
        if error_msg:
            yield event.plain_result(error_msg)
            return

        title = "群友在上什么课？" if is_today else "群友明天上什么课？"
        if self._md_enabled(event, "group") and await self._reply_markdown(
            event, md.render_group_schedule(rows, title, target), self._card_buttons()
        ):
            return

        image_path = await self.image_generator.generate_schedule_image(
            rows,
            date_type="today" if is_today else "tomorrow",
            avatar_urls=self._avatar_urls(event, [row["user_id"] for row in rows]),
        )
        event.track_temporary_local_file(image_path)
        yield event.image_result(image_path)

    @filter.command("群友在上什么课")
    async def show_group_now_schedule(self, event: AstrMessageEvent):
        """查看群友当前 / 下一节课程"""
        async for result in self._show_group_schedule(event, today(), True):
            yield result

    @filter.command("群友明天上什么课")
    async def show_group_tomorrow_schedule(self, event: AstrMessageEvent):
        """查看群友明天第一节课"""
        async for result in self._show_group_schedule(event, today() + timedelta(days=1), False):
            yield result

    # ---------- 排行 ----------

    @filter.command("本周上课排行")
    async def weekly_course_ranking(self, event: AstrMessageEvent):
        """本周上课时长排行榜"""
        ranking, start_date, end_date, error_msg = await self.helper.get_weekly_ranking(event)
        if error_msg:
            yield event.plain_result(error_msg)
            return

        if self._md_enabled(event, "ranking") and await self._reply_markdown(
            event, md.render_ranking(ranking, start_date, end_date), self._card_buttons()
        ):
            return

        image_path = await self.image_generator.generate_ranking_image(
            ranking,
            start_date,
            end_date,
            title="本周上课排行榜",
            subtitle=f"统计时间：{start_date:%Y/%m/%d} - {end_date:%Y/%m/%d}",
            avatar_urls=self._avatar_urls(event, [item["user_id"] for item in ranking]),
        )
        event.track_temporary_local_file(image_path)
        yield event.image_result(image_path)

    # ---------- LLM 工具 ----------

    @filter.llm_tool(name="course_schedule_query")
    async def query_schedule_tool(self, event: AstrMessageEvent, scope: str = "today") -> str:
        """查询当前用户绑定的课表。返回指定范围内的全部课程（含每节课的起止时间、地点）以及当前日期时间，请据此自行判断每节课是已结束、正在进行还是尚未开始，再回答用户。用户询问今天/明天/本周/周几有什么课、还有几节课、几点上课时调用。

        Args:
            scope(string): 查询范围，只能是 today（今天）、tomorrow（明天）、week（本周，周一到周日）之一
        """
        scope = (scope or "today").strip().lower()
        if scope == "week":
            courses, start, end, error_msg = await self.helper.get_personal_week_courses(event)
            if error_msg:
                return error_msg
            return self.helper.format_week_for_llm(courses, start, end)

        target = today() + timedelta(days=1 if scope == "tomorrow" else 0)
        courses, error_msg = await self.helper.get_personal_courses(event, target, include_finished=True)
        if error_msg:
            return error_msg
        return self.helper.format_day_for_llm(courses, "明天" if scope == "tomorrow" else "今天", target)

    async def terminate(self):
        logger.info("Course Schedule plugin terminated.")
