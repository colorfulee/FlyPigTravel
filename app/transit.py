"""240 小时过境免签规则引擎：纯函数，不联网，规则数据来自 data/transit_240h.json。

规则编号 R1–R9 与 docs/travelmate/240小时过境免签规则表.md 一一对应。
判定分三级：block（违反规则，行程不可行）、warn（官方原文没写明，需向 12367 确认）、info（提示）。
"""

import datetime as dt
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .config import ROOT

RULES_PATH = ROOT / "data" / "transit_240h.json"
STAY_HOURS = 240
UNILATERAL_DAYS = 30
WHOLE = "整个省（区、市）"
HK_MO_TW = ("香港", "澳门", "台湾")
MAINLAND = "中国内地"
NEEDS_VISA = ("工作", "学习", "留学", "新闻采访", "采访")  # R8：需事先批准、须办签证的事由

Level = Literal["block", "warn", "info"]
Policy = Literal["unilateral_30d", "transit_240h", "not_eligible"]


class Port(BaseModel):
    province: str
    name: str
    type: str
    status: str
    code: str | None = None

    @property
    def verified(self) -> bool:
        return self.status == "已核实"


class Stop(BaseModel):
    """行程里的一个地点，省 / 市由高德等地理服务给出。"""
    name: str
    province: str
    city: str = ""


class Trip(BaseModel):
    nationality: str
    origin: str  # 飞来中国之前所在的国家（地区）
    destination: str  # 离开中国后前往的国家（地区）
    entry_port: str  # 口岸名或机场三字码
    entry_time: dt.datetime  # 北京时间
    exit_port: str | None = None
    exit_time: dt.datetime | None = None  # 未提供即视为没有确定日期的联程票
    stops: list[Stop] = Field(default_factory=list)
    purpose: str = "旅游"


class Issue(BaseModel):
    rule: str  # R1–R9
    level: Level
    message: str
    subject: str = ""  # 涉及的地点 / 口岸，便于前端高亮


class Verdict(BaseModel):
    policy: Policy
    deadline: dt.datetime | None = None  # 最晚离境时间（北京时间）
    minutes_to_spare: int | None = None  # 离境时距截止还剩多少分钟，负数表示超期
    issues: list[Issue] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.policy != "not_eligible" and not any(i.level == "block" for i in self.issues)


class Rules:
    def __init__(self, data: dict):
        self.as_of: str = data["as_of"]
        self.transit_countries: set[str] = {c for v in data["eligible_countries"]["by_region"].values() for c in v}
        self.unilateral: set[str] = set(data["unilateral_visa_free_30d"]["countries"])
        self.areas: dict[str, str | list[str]] = data["stay_areas"]["provinces"]
        self.ports: list[Port] = [Port(**p) for p in data["ports"]["list"]]

    @classmethod
    def load(cls, path: Path = RULES_PATH) -> "Rules":
        return cls(json.loads(path.read_text(encoding="utf-8")))

    # ---- R5 截止时间 ----
    @staticmethod
    def deadline(entry_time: dt.datetime) -> dt.datetime:
        """自入境次日 0 时起算 240 小时。例：1 月 1 日入境 → 1 月 12 日 0 时（即 1 月 11 日 24 时）前离境。"""
        start = dt.datetime.combine(entry_time.date() + dt.timedelta(days=1), dt.time())
        return start + dt.timedelta(hours=STAY_HOURS)

    @staticmethod
    def unilateral_deadline(entry_time: dt.datetime) -> dt.datetime:
        """单方面免签：连续停留不超过 30 天，自入境次日起算（驻美使馆 FAQ）。"""
        start = dt.datetime.combine(entry_time.date() + dt.timedelta(days=1), dt.time())
        return start + dt.timedelta(days=UNILATERAL_DAYS)

    # ---- R4 / R7 口岸 ----
    def find_port(self, text: str) -> Port | None:
        """按三字码、全名或简称匹配口岸；简称有歧义时返回 None。"""
        text = text.strip()
        for p in self.ports:
            if p.code and text.upper() == p.code:
                return p
        for p in self.ports:
            if text == p.name:
                return p
        key = _port_key(text)
        if not key:
            return None
        # 只允许“输入是全名的简称”（天府机场 → 成都天府国际机场），反过来会把“上海虹桥站”认成虹桥机场
        hits = [p for p in self.ports if key in _port_key(p.name)]
        return hits[0] if len(hits) == 1 else None

    def ports_in_city(self, city: str) -> list[Port]:
        """城市里的已核实口岸，如“昆明” → [昆明长水国际机场]；用于用户只说了城市没说机场的情况。"""
        c = _city_key(city)
        return [p for p in self.ports if len(c) >= 2 and p.verified and p.name.startswith(c)] if c else []

    # ---- R6 停留区域 ----
    def in_area(self, province: str, city: str = "") -> bool | None:
        """True / False；该省只开放部分城市而城市未知时返回 None。"""
        area = self.areas.get(_province_key(province))
        if area is None:
            return False
        if area == WHOLE:
            return True
        c = _city_key(city)
        if len(c) < 2:
            return None
        return any(c.startswith(a) or a.startswith(c) for a in area)

    # ---- R1 / R9 适用哪项政策 ----
    def policy_for(self, nationality: str) -> Policy:
        if nationality in self.unilateral:
            return "unilateral_30d"
        if nationality in self.transit_countries:
            return "transit_240h"
        return "not_eligible"

    def check(self, trip: Trip) -> Verdict:
        issues: list[Issue] = []
        policy = self.policy_for(trip.nationality)
        if policy == "unilateral_30d":
            # 这些国家按 30 天单方面免签入境：不受第三国、口岸、24 省区域限制，过境免签的校验不适用
            deadline = self.unilateral_deadline(trip.entry_time)
            issues.append(Issue(rule="R9", level="info", subject=trip.nationality,
                                message=f"{trip.nationality}公民可单方面免签入境停留 30 天，不需要第三国联程票，比过境免签更宽松"))
            spare = int((deadline - trip.exit_time).total_seconds() // 60) if trip.exit_time else None
            if spare is not None and spare < 0:
                issues.append(Issue(rule="R9", level="block",
                                    message=f"单方面免签最多停留 30 天，最晚须在 {deadline:%m-%d %H:%M} 前离境"))
            return Verdict(policy=policy, deadline=deadline, minutes_to_spare=spare, issues=issues)
        if policy == "not_eligible":
            issues.append(Issue(rule="R1", level="block", subject=trip.nationality,
                                message=f"{trip.nationality}不在 240 小时过境免签适用的 {len(self.transit_countries)} 国名单内，需要办理签证"))
            return Verdict(policy=policy, issues=issues)

        deadline = self.deadline(trip.entry_time)
        issues += self._check_route(trip)
        issues += self._check_ports(trip)
        spare = None
        if trip.exit_time is None:
            issues.append(Issue(rule="R3", level="block",
                                message="需要持有确定日期和座位、前往第三国（地区）的联程客票"))
        else:
            spare = int((deadline - trip.exit_time).total_seconds() // 60)
            if trip.exit_time <= trip.entry_time:
                issues.append(Issue(rule="R3", level="block", message="离境时间早于入境时间，请检查日期"))
            elif spare < 0:
                over = trip.exit_time - deadline
                issues.append(Issue(rule="R5", level="block",
                                    message=f"离境时间超出 240 小时，最晚须在 {deadline:%m-%d %H:%M} 前离境，"
                                            f"超出 {_fmt_duration(over)}"))
        for s in trip.stops:
            inside = self.in_area(s.province, s.city)
            if inside is None:
                issues.append(Issue(rule="R6", level="warn", subject=s.name,
                                    message=f"{s.province}只开放部分城市，{s.name}所在城市未知，请确认"))
            elif not inside:
                issues.append(Issue(rule="R6", level="block", subject=s.name,
                                    message=f"{s.name}（{s.province}{s.city}）不在过境免签允许停留区域内"))
        if any(w in trip.purpose for w in NEEDS_VISA):
            issues.append(Issue(rule="R8", level="block", subject=trip.purpose,
                                message=f"过境免签只限旅游、商务、访问、探亲等短期活动，{trip.purpose}需要办理签证"))
        return Verdict(policy=policy, deadline=deadline, minutes_to_spare=spare, issues=issues)

    @staticmethod
    def _check_route(trip: Trip) -> list[Issue]:
        if trip.destination in (MAINLAND, "中国"):
            return [Issue(rule="R2", level="block", subject=trip.destination,
                          message="过境免签要求离开中国前往第三国（地区），目的地不能是中国内地")]
        if trip.destination == trip.origin:
            return [Issue(rule="R2", level="block", subject=trip.destination,
                          message=f"从{trip.origin}来、又回{trip.destination}，不属于前往第三国（地区）的过境")]
        if trip.destination in HK_MO_TW or trip.origin in HK_MO_TW:
            return [Issue(rule="R2", level="warn", subject=trip.destination,
                          message="经香港、澳门、台湾往来是否按“第三地区”认定，官方原文未写明，请向 12367 确认")]
        return []

    def _check_ports(self, trip: Trip) -> list[Issue]:
        issues = []
        entry = self.find_port(trip.entry_port)
        if entry is None:
            issues.append(Issue(rule="R4", level="block", subject=trip.entry_port,
                                message=f"{trip.entry_port}不是过境免签适用口岸，不能免签入境"))
        elif not entry.verified:
            issues.append(Issue(rule="R4", level="warn", subject=entry.name,
                                message=f"{entry.name}是否为适用口岸尚未找到官方名单，请向 12367 确认"))
        if trip.exit_port:
            exit_ = self.find_port(trip.exit_port)
            if exit_ is None:
                issues.append(Issue(rule="R7", level="block", subject=trip.exit_port,
                                    message=f"{trip.exit_port}不在过境免签适用口岸名单内，请改从名单内口岸离境"))
            elif not exit_.verified or exit_.province == "广东":
                # 广东“全省对外开放口岸均可离境”只见于南航汇总表；待核实口岸同样提示
                issues.append(Issue(rule="R7", level="warn", subject=exit_.name,
                                    message=f"从{exit_.name}离境的规定尚未在官方原文中核实，请向 12367 确认"))
        return issues


@lru_cache(maxsize=1)
def default_rules() -> Rules:
    return Rules.load()


def _fmt_duration(d: dt.timedelta) -> str:
    hours, rem = divmod(int(d.total_seconds()) // 60, 60)
    return f"{hours} 小时 {rem} 分钟" if hours else f"{rem} 分钟"


def _port_key(name: str) -> str:
    return re.sub(r"国际|机场|口岸|（客运）|\(客运\)|T\d$|\s", "", name)


def _province_key(name: str) -> str:
    return re.sub(r"(壮族|回族|维吾尔)?自治区$|特别行政区$|省$|市$", "", name.strip())


def _city_key(name: str) -> str:
    return re.sub(r"市$|地区$|盟$", "", name.strip())
