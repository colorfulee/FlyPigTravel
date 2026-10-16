import datetime as dt

import pytest

from app.transit import Rules, Stop, Trip, default_rules

R = default_rules()
ENTRY = dt.datetime(2026, 10, 26, 14, 30)  # 周一下午落地浦东


def trip(**kw) -> Trip:
    base = dict(nationality="美国", origin="美国", destination="韩国", entry_port="PVG", entry_time=ENTRY,
                exit_port="PVG", exit_time=dt.datetime(2026, 11, 4, 10, 0),
                stops=[Stop(name="外滩", province="上海市", city="上海市"),
                       Stop(name="西湖", province="浙江省", city="杭州市")])
    base.update(kw)
    return Trip(**base)


def rules_hit(v, level=None):
    return {i.rule for i in v.issues if level is None or i.level == level}


# ---- 数据完整性：与规则表文档一致 ----

def test_rules_data_counts():
    assert len(R.transit_countries) == 57
    assert len(R.unilateral) == 50
    assert len(R.areas) == 24
    assert len(R.ports) == 65
    assert sum(p.verified for p in R.ports) == 60
    assert all(p.province in R.areas for p in R.ports)


def test_target_users_are_the_15_countries():
    need = sorted(c for c in R.transit_countries if R.policy_for(c) == "transit_240h")
    assert len(need) == 15
    assert {"美国", "墨西哥", "印度尼西亚", "越南", "吉尔吉斯斯坦"} <= set(need)


# ---- R1 / R9 适用政策 ----

@pytest.mark.parametrize("country, policy", [
    ("美国", "transit_240h"), ("印度尼西亚", "transit_240h"),
    ("法国", "unilateral_30d"), ("日本", "unilateral_30d"), ("英国", "unilateral_30d"),
    ("秘鲁", "unilateral_30d"),  # 只在单方面免签名单
    ("印度", "not_eligible"), ("泰国", "not_eligible"),
])
def test_policy_for(country, policy):
    assert R.policy_for(country) == policy


def test_not_eligible_blocks_and_stops_early():
    v = R.check(trip(nationality="印度"))
    assert not v.ok and v.policy == "not_eligible" and rules_hit(v) == {"R1"}
    assert v.deadline is None


def test_unilateral_country_gets_info_not_block():
    v = R.check(trip(nationality="德国", origin="德国"))
    assert v.ok and v.policy == "unilateral_30d"
    assert rules_hit(v, "info") == {"R9"}
    assert v.deadline == dt.datetime(2026, 11, 26, 0, 0)  # 10-27 起算 30 天


def test_unilateral_country_skips_transit_checks():
    """德国人回德国、去九寨沟、从虹桥站离境都不违规：30 天免签不受过境免签的限制。"""
    v = R.check(trip(nationality="德国", origin="德国", destination="德国", exit_port="上海虹桥站",
                     stops=[Stop(name="九寨沟", province="四川省", city="阿坝藏族羌族自治州")]))
    assert v.ok and rules_hit(v) == {"R9"}


def test_unilateral_country_over_30_days_blocks():
    v = R.check(trip(nationality="日本", origin="日本", exit_time=dt.datetime(2026, 11, 26, 0, 1)))
    assert not v.ok and "R9" in rules_hit(v, "block")


# ---- R5 截止时间：入境次日 0 时起算 ----

@pytest.mark.parametrize("entry, deadline", [
    (dt.datetime(2026, 1, 1, 8, 0), dt.datetime(2026, 1, 12, 0, 0)),  # 签证中心 FAQ 的例子
    (dt.datetime(2026, 1, 1, 23, 59), dt.datetime(2026, 1, 12, 0, 0)),  # 深夜落地不吃亏
    (dt.datetime(2026, 1, 2, 0, 1), dt.datetime(2026, 1, 13, 0, 0)),
    (dt.datetime(2026, 12, 25, 10, 0), dt.datetime(2027, 1, 5, 0, 0)),  # 跨年
])
def test_deadline(entry, deadline):
    assert Rules.deadline(entry) == deadline


def test_naive_entry_plus_240h_would_be_wrong():
    entry = dt.datetime(2026, 10, 26, 14, 30)
    assert Rules.deadline(entry) - (entry + dt.timedelta(hours=240)) == dt.timedelta(hours=9, minutes=30)


def test_exit_after_deadline_blocks():
    v = R.check(trip(exit_time=dt.datetime(2026, 11, 6, 0, 1)))  # 截止 11-06 00:00
    assert not v.ok and "R5" in rules_hit(v, "block")
    assert v.minutes_to_spare == -1


def test_exit_right_before_deadline_ok():
    v = R.check(trip(exit_time=dt.datetime(2026, 11, 5, 23, 55)))
    assert v.ok and v.minutes_to_spare == 5


def test_missing_onward_ticket_blocks():
    v = R.check(trip(exit_time=None))
    assert not v.ok and "R3" in rules_hit(v, "block")


def test_exit_before_entry_blocks():
    v = R.check(trip(exit_time=ENTRY - dt.timedelta(hours=1)))
    assert "R3" in rules_hit(v, "block")


def test_happy_path():
    v = R.check(trip())
    assert v.ok and v.policy == "transit_240h" and v.issues == []
    assert v.deadline == dt.datetime(2026, 11, 6, 0, 0)


# ---- R2 第三国（地区） ----

def test_back_to_origin_blocks():
    v = R.check(trip(destination="美国"))
    assert not v.ok and "R2" in rules_hit(v, "block")


def test_destination_mainland_blocks():
    assert "R2" in rules_hit(R.check(trip(destination="中国内地")), "block")


def test_hong_kong_is_warn_not_block():
    v = R.check(trip(destination="香港"))
    assert v.ok and "R2" in rules_hit(v, "warn")


# ---- R4 / R7 口岸 ----

@pytest.mark.parametrize("text, name", [
    ("PVG", "上海浦东国际机场"), ("pvg", "上海浦东国际机场"),
    ("上海浦东国际机场", "上海浦东国际机场"), ("浦东国际机场", "上海浦东国际机场"),
    ("天府机场", "成都天府国际机场"),  # flyai search-flight 返回的写法
    ("TFU", "成都天府国际机场"), ("双流国际机场", "成都双流国际机场"),
    ("上海港", "上海港（客运）"), ("磨憨铁路口岸", "磨憨铁路口岸"),
])
def test_find_port(text, name):
    assert R.find_port(text).name == name


@pytest.mark.parametrize("text", ["成都", "上海", "北海", "拉萨贡嘎国际机场", "上海虹桥站", ""])
def test_find_port_ambiguous_or_missing(text):
    assert R.find_port(text) is None


def test_entry_port_not_listed_blocks():
    v = R.check(trip(entry_port="拉萨贡嘎国际机场"))
    assert not v.ok and "R4" in rules_hit(v, "block")


def test_entry_via_unverified_port_warns():
    v = R.check(trip(entry_port="广深港高铁西九龙站口岸", exit_port="CAN",
                     stops=[Stop(name="广州塔", province="广东省", city="广州市")]))
    assert v.ok and "R4" in rules_hit(v, "warn")


def test_exit_port_not_listed_blocks():
    v = R.check(trip(exit_port="上海虹桥站"))
    assert not v.ok and "R7" in rules_hit(v, "block")


def test_exit_from_guangdong_warns():
    v = R.check(trip(exit_port="广州白云国际机场"))
    assert v.ok and "R7" in rules_hit(v, "warn")


def test_cross_province_entry_exit_ok():
    """2024-12 起可跨省：浦东入境、成都天府离境。"""
    v = R.check(trip(exit_port="天府机场", stops=[Stop(name="大熊猫基地", province="四川省", city="成都市")]))
    assert v.ok


# ---- R6 停留区域 ----

@pytest.mark.parametrize("province, city, inside", [
    ("上海市", "上海市", True), ("北京市", "", True), ("广东省", "韶关市", True),
    ("四川省", "成都市", True), ("四川省", "乐山市", True),
    ("四川省", "阿坝藏族羌族自治州", False),  # 九寨沟
    ("云南省", "西双版纳傣族自治州", True), ("云南省", "大理白族自治州", True),
    ("云南省", "迪庆藏族自治州", False),  # 香格里拉
    ("山西省", "大同市", True), ("山西省", "忻州市", False),  # 五台山
    ("黑龙江省", "哈尔滨市", True), ("黑龙江省", "大庆市", False),
    ("广西壮族自治区", "桂林市", True), ("广西壮族自治区", "百色市", False),
    ("江西省", "景德镇市", True), ("江西省", "九江市", False),  # 庐山
    ("西藏自治区", "拉萨市", False), ("新疆维吾尔自治区", "乌鲁木齐市", False), ("甘肃省", "敦煌市", False),
])
def test_in_area(province, city, inside):
    assert R.in_area(province, city) is inside


def test_in_area_unknown_city_in_partial_province():
    assert R.in_area("四川省", "") is None


def test_stop_outside_area_blocks_and_names_it():
    v = R.check(trip(stops=[Stop(name="九寨沟", province="四川省", city="阿坝藏族羌族自治州"),
                            Stop(name="乐山大佛", province="四川省", city="乐山市")]))
    blocks = [i for i in v.issues if i.level == "block"]
    assert not v.ok and [i.subject for i in blocks] == ["九寨沟"]


def test_stop_with_unknown_city_warns():
    v = R.check(trip(stops=[Stop(name="某景点", province="云南省")]))
    assert v.ok and "R6" in rules_hit(v, "warn")


# ---- R8 活动事由 ----

@pytest.mark.parametrize("purpose, blocked", [("旅游", False), ("商务", False), ("探亲", False),
                                              ("工作", True), ("留学", True), ("新闻采访", True)])
def test_purpose(purpose, blocked):
    assert ("R8" in rules_hit(R.check(trip(purpose=purpose)), "block")) is blocked


# ---- 演示剧本：一次给出全部问题 ----

def test_demo_trip_collects_every_problem():
    v = R.check(trip(destination="美国", exit_time=dt.datetime(2026, 11, 7, 9, 0), exit_port="上海虹桥站",
                     stops=[Stop(name="九寨沟", province="四川省", city="阿坝藏族羌族自治州")]))
    assert rules_hit(v, "block") == {"R2", "R5", "R6", "R7"}
