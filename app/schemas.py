"""数据模型：一段火车或航班。"""

import datetime as dt
from typing import Literal

from pydantic import BaseModel


class Leg(BaseModel):
    """一段火车或航班（飞猪 search-train / search-flight 的一条结果）。"""
    mode: Literal["火车", "航班"]
    no: str  # 车次 / 航班号，中转时用“ + ”连接
    dep_station: str
    dep_code: str = ""  # 机场三字码 / 车站电报码，用于口岸校验
    arr_station: str
    dep_time: dt.datetime
    arr_time: dt.datetime
    minutes: int
    transfers: int = 0
    price: float | None = None
    url: str = ""
