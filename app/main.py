import datetime as dt
import hashlib
import json

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import ROOT, settings
from .flyai import FlyaiClient, FlyaiError
from .geo import Geocoder
from .llm import Bailian, LLMError
from .pipeline import plan, read_screenshot
from .route import Itinerary, plan_route
from .schemas import PlanResponse
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
    text: str


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


@app.get("/transit")
def transit_page():
    return FileResponse(STATIC / "transit.html")


@app.post("/api/transit", response_model=TransitResponse)
def api_transit(req: PlanRequest):
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "Tell us about your trip")
    if len(text) > 2000:
        raise HTTPException(413, "Please keep it under 2000 characters")
    try:
        a = assess(llm(), geo, text, dt.date.today())
        result = TransitResponse(assessment=a, itinerary=plan_route(a, fly, geo))
    except LLMError as e:
        raise HTTPException(502, f"Our AI service is busy, please try again ({e})") from e
    # 每个需求单独存一份：演示兜底，也供离线预览页录制
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(text.encode()).hexdigest()[:12]
    for name in (f"transit-{key}.json", "transit-last.json"):
        (SNAPSHOT_DIR / name).write_text(result.model_dump_json(), encoding="utf-8")
    return result


@app.get("/api/transit/snapshot", response_model=TransitResponse)
def api_transit_snapshot():
    path = SNAPSHOT_DIR / "transit-last.json"
    if not path.exists():
        raise HTTPException(404, "No saved result yet")
    return json.loads(path.read_text(encoding="utf-8"))
