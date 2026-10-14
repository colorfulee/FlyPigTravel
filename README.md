# 住进去 · 高品质酒店入住旅程设计师

> 2026 飞猪 AI 旅行创新大赛参赛作品 · 命题方向：高品质酒店 staycation

告诉它你是谁、和谁去、想要什么。它基于**飞猪真实酒店与万豪套餐数据**选出最适合你的酒店，
用**多模态**读懂酒店（套餐、评价、图片），按**空间**把酒店内外的体验分层编排成一段完整旅程，
并在旅程中**主动提醒**你下一步该做什么。同一家酒店，亲子和情侣会收到完全不同的旅程。

## 流程

```
一句话需求 / 种草截图
  └─ 百炼 qwen-plus：解析城市、日期、人群、预算、想要的体验      （截图走 qwen3-vl-plus）
       └─ 飞猪 flyai：search-hotel + search-marriott-hotel + search-marriott-package
            └─ 代码排序：评分、预算、人群关键词、是否有套餐
                 ├─ 百炼 qwen-plus：从套餐和评价原文提取体验 → 代码校验“证据必须逐字出现在原文”
                 ├─ 百炼 qwen3-vl-plus：从酒店/套餐图片识别设施 → 自动进入“待确认”
                 └─ 飞猪 search-poi + 坐标距离：步行圈 / 车程圈
                      └─ 百炼 qwen-plus：为两种人群编排旅程与主动提醒
                           → 代码校验 experience_id、入住/退房时间窗；不合格则确定性兜底
```

**分工原则**：百炼负责理解与表达；价格、距离、时间窗、来源全部来自飞猪数据和代码，
模型引用不到真实数据的内容一律丢弃。每条体验都标注依据：套餐权益 / 用户评价 / 图片识别 / 周边数据。

## 本地运行

```bash
# 1. 依赖
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
npm i -g @fly-ai/flyai-cli

# 2. 配置（.env 已被 gitignore，切勿提交）
cp .env.example .env
#   填写 DASHSCOPE_API_KEY（百炼）和 FLYAI_API_KEY（飞猪）；AMAP_KEY 可选

# 3. 启动
.venv/bin/uvicorn app.main:app --port 8000
# 打开 http://localhost:8000
```

离线演示：`FLYAI_MODE=fixture` 使用 `fixtures/` 下的样例数据（页面会明确标出“样例数据”），百炼仍需真实 Key。
样例数据是飞猪线上真实返回的快照，用 `python scripts/refresh_fixtures.py 上海 2026-10-31 2026-11-01 上海漕河泾万丽酒店` 重新抓取。

数据实测：`bash scripts/probe_flyai.sh 上海 2026-10-31 2026-11-01`

测试：`.venv/bin/python -m pytest -q`

## 目录

| 路径 | 说明 |
|---|---|
| `app/pipeline.py` | 主流程：需求解析、召回排序、体验挖掘、旅程编排、校验与兜底 |
| `app/flyai.py` | flyai CLI 封装、结果缓存、样例模式 |
| `app/llm.py` | 百炼（DashScope OpenAI 兼容接口）文本与视觉调用 |
| `app/geo.py` | 距离、方位、空间分层、可选高德地理编码 |
| `app/static/index.html` | 移动端 H5 前端 |
| `docs/` | 选题调研、参赛提交稿 |
