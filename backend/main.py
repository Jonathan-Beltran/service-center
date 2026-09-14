import os
import json
import secrets
import threading
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

from schedule import schedule
from export_xlsx import build_workbook

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, "..", "frontend")

# Where persistent state lives. On Railway, point this at a mounted volume
# (e.g. DATA_FILE=/data/store.json) so submissions survive redeploys.
DATA_FILE = os.environ.get("DATA_FILE", os.path.join(BASE_DIR, "store.json"))

MAX_WEEKS = 26

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE", "PUT"],
    allow_headers=["Content-Type", "Authorization"],
)

# ── Auth ──────────────────────────────────────────────────────────────────────

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")
active_tokens: set = set()
security = HTTPBearer()

def require_auth(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if credentials.credentials not in active_tokens:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

class LoginRequest(BaseModel):
    password: str

@app.post("/admin/login")
def admin_login(body: LoginRequest):
    if body.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Wrong password")
    token = secrets.token_hex(32)
    active_tokens.add(token)
    return {"token": token}

# ── Persistent store ──────────────────────────────────────────────────────────
#
# A "schedule setup" is {start, end, weeks, settings, workers}:
#   start/end  ISO dates chosen by the supervisor
#   weeks      ISO date of every Monday in that range (computed)
#   settings   {"weekdays": [8 ints], "weekends": [8 ints], "yellows": bool}
#   workers    [{"initials", "points", "doubles"}, ...]
#   incentives {"2026-09-21": [[day, shift], ...]}   shifts marked as incentive pay
#
# store = {
#   "draft":          setup being edited on the Create Schedule tab (supervisor only)
#   "published":      setup workers see, plus "created_at"; None until Create Schedule
#   "preferences":    {"HM": {"2026-09-21": [[day, shift], ...], ...}, ...}
#   "results":        last run of the algorithm, supervisor only: {"run_at", "weeks": [...]}
#   "posted_results": results workers can see: {"posted_at", "weeks": [...]}; None until posted
# }

DEFAULT_SETTINGS = {
    "weekdays": [1, 1, 2, 3, 3, 3, 2, 2],
    "weekends": [1, 1, 1, 2, 2, 2, 2, 2],
    "yellows": True,
}

def _empty_setup() -> dict:
    return {"start": None, "end": None, "weeks": [], "settings": deepcopy(DEFAULT_SETTINGS), "workers": [], "incentives": {}}

def _empty_store() -> dict:
    return {
        "draft": _empty_setup(),
        "published": None,
        "preferences": {},
        "results": None,
        "posted_results": None,
    }

def _load_store() -> dict:
    base = _empty_store()
    try:
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for key in base:
                if key in data:
                    base[key] = data[key]
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return base

store = _load_store()
store_lock = threading.Lock()

def save_store():
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(store, f, indent=2)
    os.replace(tmp, DATA_FILE)

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

# ── Models ────────────────────────────────────────────────────────────────────

class Settings(BaseModel):
    weekdays: List[int]
    weekends: List[int]
    yellows: bool

class WorkerEntry(BaseModel):
    initials: str
    points: float
    doubles: bool

class SetupRequest(BaseModel):
    start: Optional[date] = None
    end: Optional[date] = None
    settings: Settings
    workers: List[WorkerEntry]
    incentives: Dict[str, List[List[int]]] = {}

def _monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())

def _weeks_between(start: Optional[date], end: Optional[date]) -> List[str]:
    if start is None or end is None:
        return []
    if end < start:
        raise HTTPException(status_code=400, detail="End date must be on or after start date")
    weeks = []
    cur = _monday_of(start)
    last = _monday_of(end)
    while cur <= last:
        weeks.append(cur.isoformat())
        cur += timedelta(days=7)
    if len(weeks) > MAX_WEEKS:
        raise HTTPException(status_code=400, detail=f"Scheduling period can cover at most {MAX_WEEKS} weeks")
    return weeks

def _build_setup(body: SetupRequest) -> dict:
    if len(body.settings.weekdays) != 8 or len(body.settings.weekends) != 8:
        raise HTTPException(status_code=400, detail="weekdays and weekends must each have 8 entries")
    seen = set()
    workers = []
    for w in body.workers:
        initials = w.initials.strip().upper()
        if not initials or initials in seen:
            continue
        seen.add(initials)
        workers.append({"initials": initials, "points": w.points, "doubles": w.doubles})

    weeks = _weeks_between(body.start, body.end)
    incentives: Dict[str, List[List[int]]] = {}
    for week, cells in body.incentives.items():
        if week not in weeks:
            continue
        cleaned = []
        for c in cells:
            if len(c) != 2 or not (0 <= c[0] <= 6) or not (0 <= c[1] <= 7):
                raise HTTPException(status_code=400, detail=f"Invalid incentive cell {c}")
            cell = [int(c[0]), int(c[1])]
            if cell not in cleaned:
                cleaned.append(cell)
        if cleaned:
            incentives[week] = cleaned

    return {
        "start": body.start.isoformat() if body.start else None,
        "end": body.end.isoformat() if body.end else None,
        "weeks": weeks,
        "settings": body.settings.model_dump(),
        "workers": workers,
        "incentives": incentives,
    }

# ── Draft (supervisor only) ───────────────────────────────────────────────────

@app.get("/draft", dependencies=[Depends(require_auth)])
def get_draft():
    return store["draft"]

@app.put("/draft", dependencies=[Depends(require_auth)])
def put_draft(body: SetupRequest):
    """Save the supervisor's working copy. Workers never see this."""
    setup = _build_setup(body)
    with store_lock:
        store["draft"] = setup
        save_store()
    return setup

# ── Publish ("Create Schedule") ───────────────────────────────────────────────

@app.post("/publish", dependencies=[Depends(require_auth)])
def publish():
    """Make the draft the live schedule that workers can see and pick shifts on."""
    draft = store["draft"]
    if not draft["weeks"]:
        raise HTTPException(status_code=400, detail="Set a start and end date before creating the schedule")
    if not draft["workers"]:
        raise HTTPException(status_code=400, detail="Add workers before creating the schedule")

    published = deepcopy(draft)
    published["created_at"] = _now()
    keep = set(published["weeks"])

    with store_lock:
        store["published"] = published
        # Drop submissions and results for weeks that are no longer in the period
        for initials in list(store["preferences"]):
            by_week = {w: t for w, t in store["preferences"][initials].items() if w in keep}
            if by_week:
                store["preferences"][initials] = by_week
            else:
                del store["preferences"][initials]
        for key in ("results", "posted_results"):
            if store[key]:
                store[key]["weeks"] = [r for r in store[key]["weeks"] if r["week"] in keep]
        save_store()
    return published

@app.get("/published")
def get_published():
    """What workers see: the open weeks, the worker list, and when it was created."""
    pub = store["published"]
    if not pub:
        return {"created_at": None, "weeks": [], "workers": [], "settings": None}
    return pub

# ── Worker preferences ────────────────────────────────────────────────────────

class PreferenceSubmission(BaseModel):
    initials: str
    weeks: Dict[str, List[List[int]]]   # ISO Monday -> ordered [[day, shift], ...]

@app.post("/submit_preference")
def submit_preference(pref: PreferenceSubmission):
    initials = pref.initials.strip().upper()
    if not initials:
        raise HTTPException(status_code=400, detail="Initials are required")

    pub = store["published"]
    if not pub:
        raise HTTPException(status_code=400, detail="The schedule has not been created yet")
    if initials not in {w["initials"] for w in pub["workers"]}:
        raise HTTPException(status_code=400, detail=f"{initials} is not on the worker list")

    open_weeks = set(pub["weeks"])
    cleaned: Dict[str, List[List[int]]] = {}
    for week, times in pref.weeks.items():
        if week not in open_weeks:
            raise HTTPException(status_code=400, detail=f"Week {week} is not open for scheduling")
        for t in times:
            if len(t) != 2 or not (0 <= t[0] <= 6) or not (0 <= t[1] <= 7):
                raise HTTPException(status_code=400, detail=f"Invalid shift selection {t}")
        if times:
            cleaned[week] = [[int(t[0]), int(t[1])] for t in times]

    with store_lock:
        if cleaned:
            store["preferences"][initials] = cleaned
        else:
            store["preferences"].pop(initials, None)
        save_store()
    return {"status": "ok"}

@app.get("/preferences/{initials}")
def get_preference(initials: str):
    key = initials.strip().upper()
    return {"initials": key, "weeks": store["preferences"].get(key, {})}

@app.get("/preferences", dependencies=[Depends(require_auth)])
def get_preferences():
    return [{"initials": k, "weeks": v} for k, v in store["preferences"].items()]

@app.delete("/preferences/{initials}", dependencies=[Depends(require_auth)])
def delete_preference(initials: str):
    key = initials.strip().upper()
    with store_lock:
        if key not in store["preferences"]:
            raise HTTPException(status_code=404, detail="Not found")
        del store["preferences"][key]
        save_store()
    return {"status": "ok"}

@app.delete("/preferences", dependencies=[Depends(require_auth)])
def clear_preferences():
    with store_lock:
        store["preferences"] = {}
        save_store()
    return {"status": "ok"}

# ── Run the algorithm ─────────────────────────────────────────────────────────

@app.post("/run_schedule", dependencies=[Depends(require_auth)])
def run_schedule():
    """Run the scheduler on the published setup and the submitted preferences."""
    pub = store["published"]
    if not pub:
        raise HTTPException(status_code=400, detail="Create the schedule before running it")

    settings = pub["settings"]
    priority_by_initials = {w["initials"]: w["points"] for w in pub["workers"]}
    doubles_recipients = {w["initials"] for w in pub["workers"] if w["doubles"]}

    results = []
    for week in pub["weeks"]:
        entries = [
            (initials, by_week[week])
            for initials, by_week in store["preferences"].items()
            if by_week.get(week)
        ]
        entries.sort(key=lambda e: priority_by_initials.get(e[0], 0), reverse=True)

        data = {
            "names": [e[0] for e in entries],
            "priority_points": [priority_by_initials.get(e[0], 0) for e in entries],
            "selected_times": [e[1] for e in entries],
            "doubles": [e[0] in doubles_recipients for e in entries],
        }
        # schedule() mutates its inputs, so hand it fresh copies per week
        days = {
            "weekdays": list(settings["weekdays"]),
            "weekends": list(settings["weekends"]),
            "yellows": settings["yellows"],
        }

        out = schedule(data, days)
        if isinstance(out, dict):
            results.append({"week": week, "error": True, "unfilled": out["unfilled"], "schedule": out["schedule"]})
        else:
            results.append({"week": week, "error": False, "unfilled": [], "schedule": out})

    run = {"run_at": _now(), "yellows": settings["yellows"], "weeks": results}
    with store_lock:
        store["results"] = run
        save_store()
    return run

@app.get("/results", dependencies=[Depends(require_auth)])
def get_results():
    return store["results"] or {"run_at": None, "yellows": False, "weeks": []}

@app.get("/results.xlsx", dependencies=[Depends(require_auth)])
def download_results():
    """The last run as an Excel workbook, one sheet per week."""
    if not store["results"] or not store["published"]:
        raise HTTPException(status_code=400, detail="Run the schedule first")
    data = build_workbook(store["results"], store["published"]["settings"])
    first = store["results"]["weeks"][0]["week"] if store["results"]["weeks"] else "schedule"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="schedule-{first}.xlsx"'},
    )

@app.post("/post_results", dependencies=[Depends(require_auth)])
def post_results():
    """Make the last run visible to workers."""
    if not store["results"]:
        raise HTTPException(status_code=400, detail="Run the schedule first")
    posted = deepcopy(store["results"])
    posted["posted_at"] = _now()
    with store_lock:
        store["posted_results"] = posted
        save_store()
    return posted

@app.get("/schedule")
def get_schedule():
    """The final schedule workers can see. Empty until results are posted."""
    return store["posted_results"] or {"posted_at": None, "yellows": False, "weeks": []}


# The pages are plain files that change on every deploy. Tell browsers to
# re-check them on each load so nobody runs a stale copy against a new API.
@app.middleware("http")
async def no_cache_static(request, call_next):
    response = await call_next(request)
    if not request.url.path.startswith(("/admin/", "/draft", "/publish", "/published", "/preferences",
                                        "/submit_preference", "/run_schedule", "/results", "/post_results", "/schedule")):
        response.headers["Cache-Control"] = "no-cache"
    return response

app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="static")
