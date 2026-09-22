# -*- coding: utf-8 -*-
"""
QQ 官方机器人（官bot）Markdown 卡片：渲染课表 / 帮助菜单，并通过 botpy 接口发送（可附带按钮）。
"""
import random
from datetime import date

import botpy.message

from astrbot.api.event import AstrMessageEvent

from .schedule_helper import WEEKDAY_NAMES, format_minutes, format_time_range, now

QQ_OFFICIAL_PLATFORMS = {"qq_official", "qq_official_webhook"}


def is_qq_official(event: AstrMessageEvent) -> bool:
    return event.get_platform_name() in QQ_OFFICIAL_PLATFORMS


def at_user(user_id: str) -> str:
    """官bot 群聊里艾特用户的文本标签，纯文本与 Markdown 消息均可用。"""
    return f'<qqbot-at-user id="{user_id}" />'


# ---------- 按钮 ----------


def command_button(label: str, command: str, style: int = 1) -> dict:
    """指令按钮：点击后自动以用户身份发送指令（群聊会自动 @机器人）。"""
    return {
        "render_data": {"label": label, "visited_label": label, "style": style},
        "action": {
            "type": 2,
            "permission": {"type": 2},
            "data": command,
            "enter": True,
            "unsupport_tips": "请升级 QQ 版本后重试",
        },
    }


def build_keyboard(rows: list[list[dict]]) -> dict:
    """把按钮二维列表包装成 QQ keyboard.content 结构，最多 5 行 x 5 个。"""
    for index, button in enumerate(b for row in rows for b in row):
        button["id"] = str(index + 1)
    return {"rows": [{"buttons": row} for row in rows[:5]]}


# ---------- Markdown 渲染 ----------


def _header(title: str, subtitle: str) -> list[str]:
    return [f"## {title}", f"> {subtitle}", "***"]


def _time_subtitle(target: date) -> str:
    current = now()
    day_text = f"{target:%m月%d日} {WEEKDAY_NAMES[target.weekday()]}"
    if target == current.date():
        return f"{day_text} · 现在 {current:%H:%M}"
    return day_text


def _course_status(course: dict) -> str:
    """今天的课程状态：进行中 / 多久后开始 / 已结束。"""
    current = now()
    start, end = course["start_time"], course["end_time"]
    if start <= current < end:
        return f"🟢 进行中，剩余 {format_minutes(int((end - current).total_seconds() // 60))}"
    if start > current:
        return f"🔵 {format_minutes(int((start - current).total_seconds() // 60))}后开始"
    return "✅ 已结束"


def render_personal_schedule(courses: list[dict], nickname: str, title: str, target: date) -> str:
    is_today = target == now().date()
    lines = _header(f"📅 {nickname}{title}", _time_subtitle(target))
    for index, course in enumerate(courses, 1):
        detail = f"⏰ {format_time_range(course)}"
        if course.get("location"):
            detail += f" · 📍 {course['location']}"
        if is_today:
            detail += f" · {_course_status(course)}"
        lines += ["", f"**{index}. {course['summary']}**", f"> {detail}"]
    lines += ["", "***", f"> 共 {len(courses)} 节课"]
    return "\n".join(lines)


def render_group_schedule(rows: list[dict], title: str, target: date) -> str:
    is_today = target == now().date()
    lines = _header(f"👥 {title}", _time_subtitle(target))
    for row in rows:
        lines.append("")
        if row["start_time"] is None:
            lines.append(f"**{row['nickname']}** ⚪ {row['summary']}")
            continue
        detail = f"{row['summary']} · {format_time_range(row)}"
        if row.get("location"):
            detail += f" · 📍 {row['location']}"
        status = _course_status(row) if is_today else "🔵 第一节"
        lines += [f"**{row['nickname']}** {status}", f"> {detail}"]
    return "\n".join(lines)


def render_ranking(ranking: list[dict], start: date, end: date) -> str:
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = _header("🏆 本周上课排行榜", f"统计时间：{start:%m月%d日} - {end:%m月%d日} {now():%H:%M}")
    for rank, item in enumerate(ranking, 1):
        medal = medals.get(rank, f"{rank}.")
        duration = format_minutes(int(item["total_duration"].total_seconds() // 60))
        lines += ["", f"{medal} **{item['nickname']}** · {duration} · {item['course_count']} 节"]
    return "\n".join(lines)


def render_help() -> str:
    return "\n".join(
        [
            *_header("📚 课表助手", "点击下方按钮即可快速操作"),
            "",
            "**📥 导入课表** · 用 .ics 文件或 WakeUp 口令绑定课表",
            "",
            "**📅 今日课表** · 看看今天还有什么课",
            "",
            "**📆 明日课表** · 提前看看明天的安排",
            "",
            "**👥 群友在上什么课** · 群友现在都在干嘛",
            "",
            "**🏆 本周上课排行** · 谁是本周卷王",
            "",
            "***",
            "> 导入方法：发送 /绑定课表 后 60 秒内，把 WakeUp 课程表的分享口令或导出的 .ics 文件发过来即可。",
            "> 已经绑定过的话，发送 /关联课表 绑定码 即可在其他群复用。",
        ]
    )


# ---------- 发送 ----------


async def send_markdown(event: AstrMessageEvent, content: str, keyboard: dict | None) -> None:
    """通过 botpy 接口回复 Markdown 卡片。频道场景不支持，抛出 ValueError。"""
    source = event.message_obj.raw_message
    payload = {
        "msg_type": 2,
        "markdown": {"content": content},
        "msg_id": event.message_obj.message_id,
        "msg_seq": random.randint(1, 10000),
    }
    if keyboard:
        payload["keyboard"] = {"content": keyboard}

    if isinstance(source, botpy.message.GroupMessage):
        await event.bot.api.post_group_message(group_openid=source.group_openid, **payload)
    elif isinstance(source, botpy.message.C2CMessage):
        await event.bot.api.post_c2c_message(openid=source.author.user_openid, **payload)
    else:
        raise ValueError(f"当前消息来源不支持 Markdown 卡片: {type(source).__name__}")
