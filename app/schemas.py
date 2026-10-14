from typing import Literal

from pydantic import BaseModel, Field

Persona = Literal["亲子", "情侣", "独处", "闺蜜", "带父母"]
PERSONAS: tuple[str, ...] = ("亲子", "情侣", "独处", "闺蜜", "带父母")

# 每条体验必须说明“我们凭什么这么说”
Source = Literal["套餐权益", "用户评价", "图片识别", "周边数据", "待确认"]
Ring = Literal["酒店内", "步行圈", "车程圈", "距离待确认"]


class Intent(BaseModel):
    city: str
    check_in: str
    check_out: str
    persona: Persona = "情侣"
    budget: int | None = None  # 每晚预算，元
    min_star: int | None = None  # 用户要求的最低星级，如“五星”=5
    wants: list[str] = Field(default_factory=list)  # 如：泳池、温泉、下午茶
    hotel_hint: str | None = None  # 用户指定或截图识别出的酒店名


class Hotel(BaseModel):
    id: str
    name: str
    price: int | None = None
    price_text: str = ""
    score: float | None = None
    star: str = ""
    review: str = ""
    interests_poi: str = ""
    address: str = ""
    lat: float | None = None
    lng: float | None = None
    pic: str = ""
    url: str = ""
    brand: str = ""
    decorated: int | None = None  # 装修年份
    recall_tags: list[str] = Field(default_factory=list)  # 哪些需求关键词把它召回来的
    reasons: list[str] = Field(default_factory=list)  # 推荐理由，由排序代码生成


class Package(BaseModel):
    name: str
    hotel_name: str
    price_text: str = ""
    selling_point: str = ""
    pic: str = ""
    url: str = ""


class Poi(BaseModel):
    id: str
    name: str
    address: str = ""
    category: str = ""
    ticket_price: str | None = None
    pic: str = ""
    url: str = ""
    lat: float | None = None
    lng: float | None = None


class Experience(BaseModel):
    id: str
    title: str
    detail: str = ""
    source: Source
    evidence: str = ""  # 原文片段：套餐名 / 评价句 / 图片 URL / POI 名
    ring: Ring = "酒店内"
    distance_m: int | None = None
    minutes: int | None = None
    bearing: int | None = None  # 相对酒店的方位角，前端空间图使用
    url: str = ""
    personas: list[str] = Field(default_factory=list)  # 适合的人群，空表示通用


class TimelineItem(BaseModel):
    day: int  # 0 = 入住日，1 = 次日
    time: str  # HH:MM
    experience_id: str
    title: str
    note: str = ""


class Reminder(BaseModel):
    when: str  # 如 “入住前 2 天 20:00”
    offset_minutes: int  # 相对入住日 00:00 的分钟偏移，用于排序与推送
    message: str
    experience_id: str | None = None


class ToConfirm(BaseModel):
    question: str
    script: str  # 可直接发给酒店前台的话术
    experience_id: str | None = None


class Journey(BaseModel):
    persona: str
    headline: str
    timeline: list[TimelineItem]
    reminders: list[Reminder]


class PlanResponse(BaseModel):
    intent: Intent
    hotel: Hotel
    alternatives: list[Hotel]
    packages: list[Package]
    experiences: list[Experience]
    journeys: list[Journey]
    to_confirm: list[ToConfirm]
    data_mode: str
    warnings: list[str] = Field(default_factory=list)
