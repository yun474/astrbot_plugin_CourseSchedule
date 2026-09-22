import asyncio
from datetime import date, datetime, timedelta, timezone

PRIVATE_SCOPE_ID = "private"
SHANGHAI_TZ = timezone(timedelta(hours=8))
WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def now() -> datetime:
    return datetime.now(SHANGHAI_TZ)


def today() -> date:
    return now().date()


def format_date(value: date, with_weekday: bool = True) -> str:
    text = value.strftime("%Y-%m-%d")
    return f"{text} {WEEKDAY_NAMES[value.weekday()]}" if with_weekday else text


def format_time_range(course: dict) -> str:
    return f"{course['start_time']:%H:%M}-{course['end_time']:%H:%M}"


def format_minutes(total_minutes: int) -> str:
    hours, minutes = divmod(max(total_minutes, 0), 60)
    if hours and minutes:
        return f"{hours} 小时 {minutes} 分钟"
    return f"{hours} 小时" if hours else f"{minutes} 分钟"


class ScheduleHelper:
    """课表查询辅助类，负责绑定校验、课程读取与筛选。"""

    def __init__(self, data_manager, ics_parser, user_data: dict):
        self.data_manager = data_manager
        self.ics_parser = ics_parser
        self.user_data = user_data

    # ---------- 作用域与绑定 ----------

    @staticmethod
    def get_scope_id(event) -> str:
        """群聊使用群号作为作用域，私聊统一使用 private。"""
        return event.get_group_id() or PRIVATE_SCOPE_ID

    @staticmethod
    def get_group_only_message() -> str:
        return "该功能仅支持群聊使用。"

    @staticmethod
    def get_bind_hint(scope_id: str) -> str:
        where = "当前私聊" if scope_id == PRIVATE_SCOPE_ID else "本群"
        return (
            f"你还没有在{where}绑定课表哦。请先发送 /绑定课表，然后在 60 秒内发送 .ics 课表文件；"
            "如果已经在别处绑定过，也可以发送 /关联课表 绑定码 直接复用。"
        )

    def get_user_record(self, scope_id: str, user_id: str) -> dict | None:
        return self.user_data.get(scope_id, {}).get("users", {}).get(user_id)

    def save_user_record(self, scope_id: str, user_id: str, record: dict) -> None:
        self.user_data.setdefault(scope_id, {"users": {}}).setdefault("users", {})[user_id] = record
        self.data_manager.save_user_data(self.user_data)

    def remove_user_record(self, scope_id: str, user_id: str) -> bool:
        users = self.user_data.get(scope_id, {}).get("users", {})
        if user_id not in users:
            return False
        del users[user_id]
        self.data_manager.save_user_data(self.user_data)
        return True

    def find_record_by_code(self, code: str) -> tuple[str, str, dict] | None:
        """按绑定码查找记录，返回 (scope_id, user_id, record)。"""
        for scope_id, scope in self.user_data.items():
            for user_id, record in scope.get("users", {}).items():
                if record.get("code") == code:
                    return scope_id, user_id, record
        return None

    # ---------- 课程读取 ----------

    async def load_courses(self, user_id: str, scope_id: str) -> list[dict]:
        ics_file_path = self.data_manager.get_ics_file_path(user_id, scope_id)
        if not ics_file_path.exists():
            return []
        return await asyncio.to_thread(self.ics_parser.parse_ics_file, str(ics_file_path))

    async def _load_personal_courses(self, event) -> tuple[list[dict] | None, str | None]:
        """读取当前用户的全部课程并附上昵称，未绑定时返回提示。"""
        user_id = event.get_sender_id()
        scope_id = self.get_scope_id(event)
        record = self.get_user_record(scope_id, user_id)
        if record is None:
            return None, self.get_bind_hint(scope_id)
        if not self.data_manager.get_ics_file_path(user_id, scope_id).exists():
            return None, "课表文件不存在，可能已被删除。请重新绑定。"

        courses = await self.load_courses(user_id, scope_id)
        for course in courses:
            course["nickname"] = record.get("nickname", user_id)
        return courses, None

    async def get_personal_courses(
        self, event, target_date: date, include_finished: bool = False
    ) -> tuple[list[dict] | None, str | None]:
        """获取用户某天的课程。默认过滤掉今天已经结束的课。"""
        courses, error_msg = await self._load_personal_courses(event)
        if error_msg:
            return None, error_msg

        current = now()
        target_courses = [
            c
            for c in courses
            if c["start_time"].date() == target_date
            and (include_finished or target_date != current.date() or c["end_time"] > current)
        ]
        if not target_courses:
            label = {0: "今天", 1: "明天"}.get((target_date - current.date()).days, format_date(target_date))
            return None, f"你{label}没有课啦！"

        target_courses.sort(key=lambda c: c["start_time"])
        return target_courses, None

    async def get_personal_week_courses(
        self, event
    ) -> tuple[list[dict] | None, date, date, str | None]:
        """获取用户本周（周一到周日）的全部课程。"""
        current_date = today()
        start = current_date - timedelta(days=current_date.weekday())
        end = start + timedelta(days=6)
        courses, error_msg = await self._load_personal_courses(event)
        if error_msg:
            return None, start, end, error_msg

        week_courses = sorted(
            (c for c in courses if start <= c["start_time"].date() <= end),
            key=lambda c: c["start_time"],
        )
        return week_courses, start, end, None

    async def get_group_schedule_for_date(
        self, event, target_date: date, is_today: bool = True
    ) -> tuple[list[dict] | None, str | None]:
        """获取群友某天的课程状态：今天取正在上/下一节，其他日期取第一节。

        每位已绑定的群友都会有一行，无课时 start_time / end_time 为 None。
        """
        group_id = event.get_group_id()
        if not group_id or not self.user_data.get(group_id, {}).get("users"):
            return None, "本群还没有人绑定课表哦。"

        current = now()
        rows = []
        for user_id, record in self.user_data[group_id]["users"].items():
            courses = [
                c
                for c in await self.load_courses(user_id, group_id)
                if c["start_time"].date() == target_date
            ]
            if is_today:
                courses = [c for c in courses if c["end_time"] > current]
            picked = min(courses, key=lambda c: c["start_time"], default=None)

            rows.append(
                {
                    "summary": picked["summary"] if picked else f"{'今天' if is_today else '明天'}无课",
                    "description": picked["description"] if picked else "",
                    "location": picked["location"] if picked else "",
                    "start_time": picked["start_time"] if picked else None,
                    "end_time": picked["end_time"] if picked else None,
                    "user_id": user_id,
                    "nickname": record.get("nickname", user_id),
                }
            )

        if not rows:
            return None, "本群还没有人绑定课表哦。"

        # 无课的群友排在最后
        rows.sort(key=lambda r: (r["start_time"] is None, r["start_time"] or current))
        return rows, None

    async def get_weekly_ranking(self, event) -> tuple[list[dict] | None, date, date, str | None]:
        """统计本周一到现在为止每位群友已经上过的课时。"""
        current = now()
        end_date = current.date()
        start_date = end_date - timedelta(days=end_date.weekday())

        group_id = event.get_group_id()
        if not group_id:
            return None, start_date, end_date, self.get_group_only_message()
        if not self.user_data.get(group_id, {}).get("users"):
            return None, start_date, end_date, "本群还没有人绑定课表哦。"

        ranking = []
        for user_id, record in self.user_data[group_id]["users"].items():
            total_duration = timedelta()
            course_count = 0
            for course in await self.load_courses(user_id, group_id):
                if not (start_date <= course["start_time"].date() <= end_date):
                    continue
                if course["start_time"] >= current:
                    continue
                total_duration += min(course["end_time"], current) - course["start_time"]
                course_count += 1
            if course_count:
                ranking.append(
                    {
                        "user_id": user_id,
                        "nickname": record.get("nickname", user_id),
                        "total_duration": total_duration,
                        "course_count": course_count,
                    }
                )

        if not ranking:
            return None, start_date, end_date, "本周大家都没有课呢！"
        ranking.sort(key=lambda item: item["total_duration"], reverse=True)
        return ranking, start_date, end_date, None

    # ---------- LLM 文本 ----------

    @staticmethod
    def format_course_line(course: dict) -> str:
        line = f"{course['summary']} {format_time_range(course)}"
        if course.get("location"):
            line += f" @ {course['location']}"
        if course.get("description"):
            line += f"（{course['description']}）"
        return line

    def format_day_for_llm(self, courses: list[dict], label: str, target_date: date) -> str:
        current = now()
        lines = [
            f"现在是 {format_date(current.date())} {current:%H:%M}。",
            f"{label}（{format_date(target_date)}）共 {len(courses)} 节课：",
        ]
        lines += [f"{i}. {self.format_course_line(c)}" for i, c in enumerate(courses, 1)]
        return "\n".join(lines)

    def format_week_for_llm(self, courses: list[dict], start: date, end: date) -> str:
        current = now()
        lines = [
            f"现在是 {format_date(current.date())} {current:%H:%M}。",
            f"本周（{format_date(start, False)} ~ {format_date(end, False)}）共 {len(courses)} 节课：",
        ]
        for offset in range(7):
            day = start + timedelta(days=offset)
            day_courses = [c for c in courses if c["start_time"].date() == day]
            tag = "（今天）" if day == current.date() else ""
            if not day_courses:
                lines.append(f"{WEEKDAY_NAMES[offset]} {day:%m-%d}{tag}：无课")
                continue
            lines.append(f"{WEEKDAY_NAMES[offset]} {day:%m-%d}{tag}：")
            lines += [f"  {i}. {self.format_course_line(c)}" for i, c in enumerate(day_courses, 1)]
        return "\n".join(lines)
