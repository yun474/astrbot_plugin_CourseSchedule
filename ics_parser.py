# -*- coding: utf-8 -*-
"""
本模块负责解析 .ics 日历文件，把课程展开为按周缓存的课程列表。
"""
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Dict, List

from dateutil.rrule import rrulestr
from icalendar import Calendar

from astrbot.api import logger

SHANGHAI_TZ = timezone(timedelta(hours=8))


def _to_local_datetime(value, tz: timezone) -> datetime:
    """把 icalendar 给出的 date / datetime 统一成带时区的 datetime。"""
    if isinstance(value, datetime):
        return value.astimezone(tz) if value.tzinfo else value.replace(tzinfo=tz)
    return datetime.combine(value, dt_time.min, tzinfo=tz)


class ICSParser:
    """ICS 课表解析器，每个文件只缓存当前周的展开结果。"""

    def __init__(self):
        # file_path -> (本周一日期, 课程列表)
        self.course_cache: Dict[str, tuple[date, List[Dict]]] = {}

    @staticmethod
    def count_events(file_path: str) -> int:
        """统计文件中的 VEVENT 数量，解析失败返回 -1。用于绑定时校验文件。"""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                cal = Calendar.from_ical(f.read())
        except Exception as e:
            logger.warning(f"ICS 文件校验失败 {file_path}: {e}")
            return -1
        return sum(1 for component in cal.walk() if component.name == "VEVENT")

    def parse_ics_file(self, file_path: str) -> List[Dict]:
        """解析 .ics 文件并返回从本周一开始一年内的课程列表（含重复事件展开）。"""
        today = datetime.now(SHANGHAI_TZ).date()
        parse_start_date = today - timedelta(days=today.weekday())
        cached = self.course_cache.get(file_path)
        if cached and cached[0] == parse_start_date:
            return cached[1]

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                cal = Calendar.from_ical(f.read())
        except Exception as e:
            logger.error(f"无法解析 ICS 文件 {file_path}: {e}")
            return []

        courses: List[Dict] = []
        for component in cal.walk():
            if component.name != "VEVENT":
                continue
            try:
                courses.extend(self._expand_event(component, parse_start_date))
            except Exception as e:
                logger.warning(f"跳过无法解析的课程事件（{file_path}）: {e}")

        self.course_cache[file_path] = (parse_start_date, courses)
        return courses

    def _expand_event(self, component, parse_start_date: date) -> List[Dict]:
        """把单个 VEVENT 展开成课程字典列表。"""
        dtstart_prop = component.get("dtstart")
        if dtstart_prop is None:
            return []
        dtstart = _to_local_datetime(dtstart_prop.dt, SHANGHAI_TZ)

        dtend_prop = component.get("dtend")
        if dtend_prop is not None:
            dtend = _to_local_datetime(dtend_prop.dt, SHANGHAI_TZ)
        elif component.get("duration") is not None:
            dtend = dtstart + component.get("duration").dt
        else:
            dtend = dtstart
        course_duration = dtend - dtstart

        base = {
            "summary": str(component.get("summary") or "未命名课程"),
            "description": str(component.get("description") or ""),
            "location": str(component.get("location") or ""),
        }

        rrule_prop = component.get("rrule")
        if not rrule_prop:
            if dtstart.date() < parse_start_date:
                return []
            return [{**base, "start_time": dtstart, "end_time": dtend}]

        if "UNTIL" in rrule_prop:
            until_dt = rrule_prop["UNTIL"][0]
            if not isinstance(until_dt, datetime):
                until_dt = datetime.combine(until_dt, dt_time.max)
            if until_dt.tzinfo is None:
                until_dt = until_dt.replace(tzinfo=SHANGHAI_TZ)
            rrule_prop["UNTIL"][0] = until_dt.astimezone(timezone.utc)

        rrule = rrulestr(
            rrule_prop.to_ical().decode(), dtstart=dtstart.astimezone(timezone.utc)
        )
        window_start = datetime.combine(
            parse_start_date, dt_time.min, tzinfo=SHANGHAI_TZ
        ).astimezone(timezone.utc)
        window_end = window_start + timedelta(days=365)

        courses = []
        for occurrence in rrule.between(window_start, window_end, inc=True):
            start_local = occurrence.astimezone(SHANGHAI_TZ)
            courses.append(
                {**base, "start_time": start_local, "end_time": start_local + course_duration}
            )
        return courses

    def clear_cache(self, file_path: str):
        """清除指定文件的缓存"""
        self.course_cache.pop(file_path, None)
