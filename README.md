# TravelMate · 240 小时过境免签行程规划助手

> 2026 飞猪 AI 旅行创新大赛参赛作品 · 命题方向：入境游助手 · 参赛说明见 [docs/SUBMISSION.md](docs/SUBMISSION.md)

外国游客用英文说一句话，或者上传机票截图，TravelMate 会按移民局的 240 小时过境免签规则核对这段中国行程：
判断适用哪项政策；把不在允许区域的景点移出行程，并推荐区域内的替代；
用飞猪实时数据排出高铁和航班，选一班从适用口岸、在截止时间前离境的航班；
最后按行程时间线生成主动提醒，可以一键导入手机日历。

## 流程

```
英文一句话 + 机票截图（可选）
  ├─ 百炼 qwen-plus：解析国籍、来去国家、口岸、时刻、想去的地方（英文 → 中文）
  └─ 百炼 qwen3-vl-plus：读出每段航班 → 代码判定入境段 / 离境段 / 真实出发地          （并行）
       └─ 高德：每个地点 → 省 / 市
            └─ 规则引擎 R1–R9（data/transit_240h.json，出自移民局公告）→ 先推送结论
                 ├─ 飞猪 search-train / search-flight：按城市排程 + 离境航班 → 整份行程再判一次
                 └─ 百炼 qwen-plus：出界景点的同类替代 → 代码定位并校验区域、距离        （并行）
                      └─ 主动提醒（.ics）
```

**分工原则**：百炼负责理解（英文、机票截图）和推荐（相似景点）；能不能这样走，由规则引擎按公告判定。
模型给出的时刻、景点都要经过代码校验：原话里没有钟点就不采用模型补的时刻，推荐的景点必须定位在允许区域内。

## 本地运行

```bash
# 1. 依赖
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
npm i -g @fly-ai/flyai-cli

# 2. 配置（.env 已被 gitignore，切勿提交）
cp .env.example .env
#   填写 DASHSCOPE_API_KEY（百炼）、FLYAI_API_KEY（飞猪）、AMAP_KEY（高德“Web服务”类型）
.venv/bin/python scripts/check_keys.py   # 三个 Key 是否可用

# 3. 启动
.venv/bin/uvicorn app.main:app --port 8000
# 打开 http://localhost:8000
```

- 测试：`.venv/bin/pytest`（规则引擎、解析、排程、提醒、识图、接口，不联网）
- 全链路实测：`.venv/bin/python scripts/probe_route.py "I'm American, landing at Shanghai Pudong on Oct 26 at 2:30pm ..."`
- 离线预览（真实结果录制成单文件页面）：`.venv/bin/python scripts/build_transit_preview.py`，生成 `preview_transit/preview.html`
- 示例机票截图（虚构乘客，标 SAMPLE）：`.venv/bin/python scripts/make_sample_tickets.py <输出目录>`

飞猪有风控：15 分钟内约 90 次请求会被拦截。查询结果缓存在 `.cache/flyai/`，演示请用录制的结果。

## 目录

| 路径 | 说明 |
|---|---|
| `app/transit.py` | 规则引擎 R1–R9：适用政策、240 小时截止（入境次日 0 时起算）、区域、口岸、第三国 |
| `data/transit_240h.json` | 规则数据：57 国、24 省停留区域、65 个口岸、30 天单方面免签名单，均标出处 |
| `app/travelmate.py` | 英文需求解析、地点定位、合并机票信息、首轮判定 |
| `app/ticket.py` | 机票截图识别：抄写航段，判定入境段 / 离境段与真实出发地 |
| `app/route.py` | 按城市排程，火车 / 航班选择，离境航班与枢纽兜底 |
| `app/alternatives.py` | 出界景点的同类替代（模型推荐，代码校验） |
| `app/reminders.py` | 主动提醒与 .ics 日历 |
| `app/flyai.py` | 飞猪 flyai CLI 封装与缓存 |
| `app/geo.py` | 高德地点搜索 / 地理编码，两点距离 |
| `app/llm.py` | 百炼（DashScope OpenAI 兼容接口）文本与视觉调用 |
| `app/main.py` | FastAPI 接口，`/api/transit/stream` 分阶段返回 |
| `app/static/transit.html` | 英文 H5 页面 |
| `docs/travelmate/` | 规则表与出处、演示剧本、效果截图 |
| `docs/SUBMISSION.md` | 参赛提交稿 |
