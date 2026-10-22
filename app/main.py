import datetime as dt
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import ROOT, settings
from .flyai import FlyaiClient, FlyaiError
from .geo import Geocoder
from .llm import Bailian, LLMError
from .pipeline import plan, read_screenshot
from .alternatives import AlternativeSet, suggest
from .reminders import Reminder, build_reminders, to_ics
from .route import Itinerary, plan_route
from .schemas import PlanResponse
from .ticket import MAX_IMAGES
from .travelmate import Assessment, assess

STATIC = ROOT / "app" / "static"
SNAPSHOT_DIR = settings.cache_dir / "snapshots"

app = FastAPI(title="住进去 · 高品质酒店入住旅程设计师")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

fly = FlyaiClient()
geo = Geocoder()


def llm() -> Bailian:
    try:
        return Bailian()
    except LLMError as e:
        raise HTTPException(503, str(e)) from e


class PlanRequest(BaseModel):
    text: str = ""


class TransitRequest(BaseModel):
    text: str = ""
    images: list[str] = []  # 机票截图（data:image/...;base64,...），最多 2 张


class ScreenshotRequest(BaseModel):
    image: str  # data:image/...;base64,...


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    return {
        "bailian": bool(settings.dashscope_api_key),
        "text_model": settings.text_model,
        "vl_model": settings.vl_model,
        "flyai_mode": fly.mode,
        "flyai_key": bool(settings.flyai_api_key),
        "amap": geo.enabled,
    }


@app.post("/api/plan", response_model=PlanResponse)
def api_plan(req: PlanRequest):
    if not req.text.strip():
        raise HTTPException(400, "请描述你的需求")
    try:
        result = plan(llm(), fly, geo, req.text)
    except (FlyaiError, LLMError) as e:
        raise HTTPException(502, str(e)) from e
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    (SNAPSHOT_DIR / "last.json").write_text(result.model_dump_json(), encoding="utf-8")
    return result


@app.get("/api/snapshot", response_model=PlanResponse)
def api_snapshot():
    """演示兜底：返回最近一次成功的真实结果。"""
    path = SNAPSHOT_DIR / "last.json"
    if not path.exists():
        raise HTTPException(404, "还没有成功的规划记录")
    return json.loads(path.read_text(encoding="utf-8"))


@app.post("/api/screenshot")
def api_screenshot(req: ScreenshotRequest):
    if not req.image.startswith("data:image/"):
        raise HTTPException(400, "请上传图片")
    if len(req.image) > 8_000_000:
        raise HTTPException(413, "图片过大，请压缩到 6MB 以内")
    try:
        return read_screenshot(llm(), req.image)
    except LLMError as e:
        raise HTTPException(502, str(e)) from e


# ---------------------------------------------------------------- 入境游客：240 小时过境免签行程


class TransitResponse(BaseModel):
    assessment: Assessment
    itinerary: Itinerary
    reminders: list[Reminder] = []
    alternatives: AlternativeSet = AlternativeSet()
    ics: str = ""  # 提醒的日历文件，页面一键下载


@app.get("/transit")
def transit_page():
    return FileResponse(STATIC / "transit.html")


def _check_input(req: TransitRequest) -> tuple[str, list[str]]:
    text = req.text.strip()
    if not text and not req.images:
        raise HTTPException(400, "Tell us about your trip or add a ticket screenshot")
    if len(text) > 2000:
        raise HTTPException(413, "Please keep it under 2000 characters")
    if len(req.images) > MAX_IMAGES:
        raise HTTPException(400, f"Please add at most {MAX_IMAGES} screenshots")
    for img in req.images:
        if not img.startswith("data:image/"):
            raise HTTPException(400, "Screenshots must be images")
        if len(img) > 8_000_000:
            raise HTTPException(413, "Each screenshot must be under 6 MB")
    return text, req.images


def _transit_stages(text: str, images: list[str] | None = None):
    """依次产出 (阶段名, 数据)：先给判定结论（约 15 秒），再给行程与替代推荐（两者并行）。"""
    model = llm()
    a = assess(model, geo, text, dt.date.today(), images=images or None)
    yield "assessment", a
    with ThreadPoolExecutor(max_workers=2) as pool:
        # 替代推荐（一次百炼调用）和查火车航班（飞猪）互不依赖，并行省 10-20 秒
        alts = pool.submit(suggest, model, geo, a)
        it = plan_route(a, fly, geo)
        reminders = build_reminders(a, it)
        yield "itinerary", (it, reminders)
        yield "alternatives", alts.result()


def _save_snapshot(text: str, images: list[str], result: TransitResponse) -> None:
    """每个需求单独存一份：演示兜底，也供离线预览页录制。"""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1("\n".join([text, *images]).encode()).hexdigest()[:12]
    for name in (f"transit-{key}.json", "transit-last.json"):
        (SNAPSHOT_DIR / name).write_text(result.model_dump_json(), encoding="utf-8")


def _collect(stages) -> TransitResponse:
    got = dict(stages)
    it, reminders = got["itinerary"]
    return TransitResponse(assessment=got["assessment"], itinerary=it, reminders=reminders,
                           alternatives=got["alternatives"], ics=to_ics(reminders) if reminders else "")


@app.post("/api/transit", response_model=TransitResponse)
def api_transit(req: TransitRequest):
    text, images = _check_input(req)
    try:
        result = _collect(_transit_stages(text, images))
    except LLMError as e:
        raise HTTPException(502, f"Our AI service is busy, please try again ({e})") from e
    _save_snapshot(text, images, result)
    return result


@app.post("/api/transit/stream")
def api_transit_stream(req: TransitRequest):
    """同样的结果按阶段推送（每行一个 JSON），页面先显示结论，不必干等 30 秒。"""
    text, images = _check_input(req)

    def events():
        got = []
        try:
            for stage, data in _transit_stages(text, images):
                got.append((stage, data))
                if stage == "itinerary":
                    it, reminders = data
                    body = {"itinerary": it.model_dump(mode="json"),
                            "reminders": [r.model_dump(mode="json") for r in reminders],
                            "ics": to_ics(reminders) if reminders else ""}
                else:
                    body = {stage: data.model_dump(mode="json")}
                yield json.dumps({"stage": stage, **body}, ensure_ascii=False) + "\n"
            _save_snapshot(text, images, _collect(got))
            yield json.dumps({"stage": "done"}) + "\n"
        except (LLMError, FlyaiError, HTTPException) as e:
            detail = getattr(e, "detail", None) or str(e)
            yield json.dumps({"stage": "error", "detail": f"Our AI service is busy, please try again ({detail})"},
                             ensure_ascii=False) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


@app.get("/api/transit/snapshot", response_model=TransitResponse)
def api_transit_snapshot():
    path = SNAPSHOT_DIR / "transit-last.json"
    if not path.exists():
        raise HTTPException(404, "No saved result yet")
    return json.loads(path.read_text(encoding="utf-8"))
