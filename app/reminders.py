"""主动提醒：从已排好的行程推出“什么时候该做什么”。纯函数，不联网。

时间一律是北京时间、不带时区；页面可导出 .ics，让游客手机日历到点推送。
"""

import datetime as dt

from pydantic import BaseModel

from .route import Itinerary, en as city_en
from .travelmate import Assessment

H = dt.timedelta(hours=1)


class Reminder(BaseModel):
    at: dt.datetime
    kind: str  # before_trip / arrival / clock / leg / exit / deadline / area
    title: str  # 英文，页面与日历标题
    body: str
    zh: str = ""  # 中文对照，方便出示给工作人员或核对


def build_reminders(a: Assessment, it: Itinerary) -> list[Reminder]:
    req, v = a.request, it.verdict
    if v is None or v.deadline is None or req.entry_time is None or v.policy == "not_eligible":
        return []
    if not v.ok:  # 行程本身不允许（如回出发国）：给提醒等于鼓励按这个行程出发
        return []
    out: list[Reminder] = []
    transit = v.policy == "transit_240h"
    dest = req.destination_city or req.destination or "your next country"
    dest_en = city_en(dest) if req.destination_city else "your next country"

    if transit:
        out.append(Reminder(
            at=req.entry_time - 24 * H, kind="before_trip", title="Print your onward ticket",
            body=f"Airlines check it at check-in for 240-hour visa-free transit. Bring a printout or screenshot of "
                 f"your ticket to {dest_en} with a fixed date and seat.",
            zh=f"登机前航司会查验前往第三国（地区）的联程客票：{dest}"))
        out.append(Reminder(
            at=req.entry_time, kind="arrival", title="At immigration: say \"240-hour visa-free transit\"",
            body=f"Use the visa-free transit lane if there is one. Show your passport and onward ticket to {dest_en}.",
            zh="入境时说明申请 240 小时过境免签，出示护照和联程客票"))
    start = v.deadline - (240 * H if transit else dt.timedelta(days=30))
    out.append(Reminder(
        at=start, kind="clock", title="Your visa-free clock starts now",
        body=f"Leave China no later than {_last_minute(v.deadline)} (Beijing time).",
        zh=f"免签停留期自此起算，最晚 {v.deadline:%m-%d %H:%M} 前离境"))

    for i, leg in enumerate(it.legs):
        if leg is None:
            continue
        to = it.cities[i + 1]
        early = 60 if leg.mode == "火车" else 120
        mode = "train" if leg.mode == "火车" else "flight"
        out.append(Reminder(
            at=dt.datetime.combine(leg.dep_time.date() - dt.timedelta(days=1), dt.time(20, 0)), kind="leg",
            title=f"Tomorrow: {mode} {leg.no} to {to.name_en or to.name}",
            body=f"Departs {leg.dep_time:%H:%M} from {leg.dep_station}. Your passport is your ticket: bring it.",
            zh=f"明天 {leg.dep_time:%H:%M} {leg.dep_station} 出发，{leg.no}，请带好护照"))
        out.append(Reminder(
            at=leg.dep_time - early * dt.timedelta(minutes=1) - H, kind="leg",
            title=f"Leave now for {leg.dep_station}",
            body=f"{leg.no} departs {leg.dep_time:%H:%M}. Allow time for security; be at the "
                 f"{'station' if leg.mode == '火车' else 'airport'} {early} min early. "
                 f"Show this to your driver: {leg.dep_station}",
            zh=f"请送我去 {leg.dep_station}"))

    exit_at = it.exit_leg.dep_time if it.exit_leg else (req.exit_time if req.exit_time_known else None)
    exit_from = it.exit_leg.dep_station if it.exit_leg else (req.exit_port or "")
    if exit_at:
        for hours, title in ((48, "2 days until you leave China"), (24, "Check in for your flight out")):
            out.append(Reminder(
                at=exit_at - hours * H, kind="exit", title=title,
                body=f"Your flight to {dest_en} leaves {exit_at:%b %d, %H:%M} from {exit_from}. "
                     f"Stay inside the allowed area until then.",
                zh=f"离境航班 {exit_at:%m-%d %H:%M} 从 {exit_from} 起飞"))
        out.append(Reminder(
            at=exit_at - 6 * H, kind="exit", title=f"Head to {exit_from} by {exit_at - 3 * H:%H:%M}",
            body="International flights: be at the airport 3 hours early for exit immigration. "
                 f"Show this to your driver: {exit_from}",
            zh=f"请送我去 {exit_from}（国际出发）"))
    # 截止前最后 6 小时：航班延误或取消时游客最需要知道怎么办（早已离境就不必提醒）
    if exit_at is None or exit_at > v.deadline - 6 * H:
        out.append(Reminder(
            at=v.deadline - 6 * H, kind="deadline", title="6 hours left on your visa-free stay",
            body="If your flight is delayed or cancelled, tell the airline and border police right away, "
                 "or call the immigration hotline 12367.",
            zh="免签停留期还剩 6 小时；如航班延误或取消，请立即联系航司、边检或拨打 12367"))

    # 到达与出界景点同省的城市时提醒一次：不要顺路去
    skipped = {s.ask.zh: s for s in a.stops if s.ask.zh in it.skipped and s.place}
    for c in it.cities:
        near = [s for s in skipped.values() if s.place.province == c.province]
        if near and c.arrive and c.nights:
            names = ", ".join(s.ask.text for s in near)
            out.append(Reminder(
                at=max(dt.datetime.combine(c.arrive, dt.time(9, 0)), req.entry_time + H), kind="area",
                title=f"No day trips to {names}",
                body=f"{names} is outside the area your visa-free entry allows, even as a day trip from "
                     f"{c.name_en or c.name}. Tours sold locally may not check this.",
                zh=f"{'、'.join(s.ask.zh for s in near)} 不在过境免签允许停留区域内，请勿前往"))
            for s in near:
                skipped.pop(s.ask.zh)
    # 落地前只保留行前提醒（还在国外时提醒“明天坐火车”没有意义）
    out = [r for r in out if r.kind == "before_trip" or r.at >= req.entry_time]
    return sorted(out, key=lambda r: r.at)


def _last_minute(deadline: dt.datetime) -> str:
    if deadline.time() == dt.time(0, 0):
        return f"{deadline - dt.timedelta(minutes=1):%a, %b %d} 23:59"
    return f"{deadline:%a, %b %d %H:%M}"


def to_ics(reminders: list[Reminder]) -> str:
    """日历文件：每条提醒一个 15 分钟事件，带到点提醒（VALARM）。"""
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//TravelMate//Visa-free planner//EN", "CALSCALE:GREGORIAN",
             "BEGIN:VTIMEZONE", "TZID:Asia/Shanghai", "BEGIN:STANDARD", "DTSTART:19700101T000000",
             "TZOFFSETFROM:+0800", "TZOFFSETTO:+0800", "TZNAME:CST", "END:STANDARD", "END:VTIMEZONE"]
    for i, r in enumerate(reminders):
        end = r.at + dt.timedelta(minutes=15)
        lines += ["BEGIN:VEVENT", f"UID:travelmate-{r.at:%Y%m%d%H%M}-{i}@travelmate", f"DTSTAMP:{stamp}",
                  f"DTSTART;TZID=Asia/Shanghai:{r.at:%Y%m%dT%H%M%S}", f"DTEND;TZID=Asia/Shanghai:{end:%Y%m%dT%H%M%S}",
                  f"SUMMARY:{_ics_text(r.title)}", f"DESCRIPTION:{_ics_text(r.body + (chr(10) + r.zh if r.zh else ''))}",
                  "BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_ics_text(r.title)}", "TRIGGER:PT0M", "END:VALARM",
                  "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def _ics_text(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC 5545：每行不超过 75 字节，续行以空格开头。"""
    out, cur = [], b""
    for ch in line:
        b = ch.encode()
        if len(cur) + len(b) > 75:
            out.append(cur.decode())
            cur = b" "
        cur += b
    out.append(cur.decode())
    return "\r\n".join(out)
