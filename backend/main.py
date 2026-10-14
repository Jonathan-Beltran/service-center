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
#   settings   {"weekdays": [8 ints], "weekends": [8 ints], "yellows": bool, "autofill": bool}
#   workers    [{"initials", "points", "doubles"}, ...]
#   incentives {"2026-09-21": [[day, shift, level], ...]}   level 1 = incentive pay, 2 = double incentive
#   overrides  {"2026-09-21": {"0": {"caps": [8 ints], "yellows": bool, "note": str}}}
#              per-day shift sizes that replace settings for that one day (holidays etc.)
#
# store = {
#   "draft":          setup being edited on the Create Schedule tab (supervisor only)
#   "published":      setup workers see, plus "created_at"; None until Create Schedule
#   "preferences":    {"HM": {"2026-09-21": [[day, shift], ...], ...}, ...}
#   "unavailable":    same shape; shifts a worker cannot work, set by supervisors only.
#                     They are never scheduled there, by their own picks or by autofill
#   "results":        last run of the algorithm, supervisor only:
#                     {"id", "run_at", "yellows", "submitted", "weeks": [...], "setup": published setup it ran on}
#   "runs":           every run, oldest first, same shape as "results" (the History tab)
# }

DEFAULT_SETTINGS = {
    "weekdays": [1, 1, 2, 3, 3, 3, 2, 2],
    "weekends": [1, 1, 1, 2, 2, 2, 2, 2],
    "yellows": True,
    "autofill": True,
}

def _empty_setup() -> dict:
    return {"start": None, "end": None, "weeks": [], "settings": deepcopy(DEFAULT_SETTINGS), "workers": [], "incentives": {}, "overrides": {}}

def _empty_store() -> dict:
    return {
        "draft": _empty_setup(),
        "published": None,
        "preferences": {},
        "unavailable": {},
        "results": None,
        "runs": [],
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
    # Stores from before the run log existed: start it with the last run
    if not base["runs"] and base["results"] and base["results"].get("run_at"):
        last = base["results"]
        last.setdefault("id", secrets.token_hex(6))
        last.setdefault("setup", deepcopy(base["published"]))
        base["runs"].append(last)
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

MAX_RUNS = 200   # oldest runs drop off the History tab past this

# ── Models ────────────────────────────────────────────────────────────────────

class Settings(BaseModel):
    weekdays: List[int]
    weekends: List[int]
    yellows: bool
    autofill: bool = True   # force unpicked seats onto workers

class WorkerEntry(BaseModel):
    initials: str
    points: float
    doubles: bool

class DayOverride(BaseModel):
    caps: List[int]
    yellows: bool
    note: str = ""

class SetupRequest(BaseModel):
    start: Optional[date] = None
    end: Optional[date] = None
    settings: Settings
    workers: List[WorkerEntry]
    incentives: Dict[str, List[List[int]]] = {}
    overrides: Dict[str, Dict[str, DayOverride]] = {}

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
            level = c[2] if len(c) == 3 else 1        # older drafts stored [day, shift]
            if len(c) not in (2, 3) or not (0 <= c[0] <= 6) or not (0 <= c[1] <= 7) or level not in (1, 2):
                raise HTTPException(status_code=400, detail=f"Invalid incentive cell {c}")
            cell = [int(c[0]), int(c[1]), int(level)]
            cleaned = [x for x in cleaned if x[:2] != cell[:2]]   # one level per cell
            cleaned.append(cell)
        if cleaned:
            incentives[week] = cleaned

    overrides: Dict[str, Dict[str, dict]] = {}
    for week, by_day in body.overrides.items():
        if week not in weeks:
            continue
        cleaned_days = {}
        for day, o in by_day.items():
            if day not in {str(i) for i in range(7)}:
                raise HTTPException(status_code=400, detail=f"Invalid override day {day}")
            if len(o.caps) != 8 or any(c < 0 or c > 20 for c in o.caps):
                raise HTTPException(status_code=400, detail="Day override needs 8 shift sizes between 0 and 20")
            caps = [int(c) for c in o.caps]
            caps[1] = caps[0]   # 3am-6am mirrors 12am-3am
            cleaned_days[day] = {"caps": caps, "yellows": o.yellows, "note": o.note.strip()[:60]}
        if cleaned_days:
            overrides[week] = cleaned_days

    return {
        "start": body.start.isoformat() if body.start else None,
        "end": body.end.isoformat() if body.end else None,
        "weeks": weeks,
        "settings": body.settings.model_dump(),
        "workers": workers,
        "incentives": incentives,
        "overrides": overrides,
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
        for key in ("preferences", "unavailable"):
            for initials in list(store[key]):
                by_week = {w: t for w, t in store[key][initials].items() if w in keep}
                if by_week:
                    store[key][initials] = by_week
                else:
                    del store[key][initials]
        if store["results"]:
            store["results"]["weeks"] = [r for r in store["results"]["weeks"] if r["week"] in keep]
        save_store()
    return published

@app.delete("/publish", dependencies=[Depends(require_auth)])
def unpublish():
    """Take the schedule down so workers see nothing. The draft, submissions and
    last results are kept, so Create Schedule brings everything back."""
    with store_lock:
        store["published"] = None
        save_store()
    return {"status": "ok"}

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

def _clean_shift_lists(by_week: Dict[str, List[List[int]]], open_weeks: set) -> Dict[str, List[List[int]]]:
    cleaned: Dict[str, List[List[int]]] = {}
    for week, times in by_week.items():
        if week not in open_weeks:
            raise HTTPException(status_code=400, detail=f"Week {week} is not open for scheduling")
        for t in times:
            if len(t) != 2 or not (0 <= t[0] <= 6) or not (0 <= t[1] <= 7):
                raise HTTPException(status_code=400, detail=f"Invalid shift selection {t}")
        if times:
            cleaned[week] = [[int(t[0]), int(t[1])] for t in times]
    return cleaned

def _set_or_drop(bucket: dict, initials: str, by_week: dict):
    if by_week:
        bucket[initials] = by_week
    else:
        bucket.pop(initials, None)

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
    cleaned = _clean_shift_lists(pref.weeks, open_weeks)

    with store_lock:
        _set_or_drop(store["preferences"], initials, cleaned)
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

# ── Can't-work shifts (supervisor only) ───────────────────────────────────────

class UnavailableUpdate(BaseModel):
    weeks: Dict[str, List[List[int]]]   # ISO Monday -> [[day, shift], ...]; replaces that worker's marks

@app.get("/unavailable", dependencies=[Depends(require_auth)])
def get_unavailable():
    return store["unavailable"]

@app.put("/unavailable/{initials}", dependencies=[Depends(require_auth)])
def put_unavailable(initials: str, body: UnavailableUpdate):
    key = initials.strip().upper()
    pub = store["published"]
    if not pub:
        raise HTTPException(status_code=400, detail="Create the schedule first")
    if key not in {w["initials"] for w in pub["workers"]}:
        raise HTTPException(status_code=400, detail=f"{key} is not on the worker list")
    cleaned = _clean_shift_lists(body.weeks, set(pub["weeks"]))
    with store_lock:
        _set_or_drop(store["unavailable"], key, cleaned)
        save_store()
    return {"initials": key, "weeks": cleaned}

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
        cant_work = {i: by_week[week] for i, by_week in store["unavailable"].items() if by_week.get(week)}
        # A supervisor's can't-work mark overrides the worker's own pick
        entries = [
            (initials, [t for t in by_week[week] if t not in cant_work.get(initials, [])])
            for initials, by_week in store["preferences"].items()
            if by_week.get(week)
        ]
        entries = [e for e in entries if e[1]]
        entries.sort(key=lambda e: priority_by_initials.get(e[0], 0), reverse=True)

        data = {
            "names": [e[0] for e in entries],
            "priority_points": [priority_by_initials.get(e[0], 0) for e in entries],
            "selected_times": [e[1] for e in entries],
            "doubles": [e[0] in doubles_recipients for e in entries],
            "all_workers": [(w["initials"], w["points"]) for w in pub["workers"]],
            "unavailable": cant_work,
        }
        # schedule() mutates its inputs, so hand it fresh copies per week
        days = {
            "weekdays": list(settings["weekdays"]),
            "weekends": list(settings["weekends"]),
            "yellows": settings["yellows"],
            "autofill": settings.get("autofill", True),
            "overrides": {int(d): o for d, o in pub.get("overrides", {}).get(week, {}).items()},
        }

        out = schedule(data, days)
        results.append({"week": week, **out})

    run = {
        "id": secrets.token_hex(6),
        "run_at": _now(),
        "yellows": settings["yellows"],
        "submitted": sum(1 for by_week in store["preferences"].values() if any(by_week.get(w) for w in pub["weeks"])),
        "weeks": results,
        "setup": deepcopy(pub),   # so History can show and export it after the schedule changes
    }
    with store_lock:
        store["results"] = run
        store["runs"].append(run)
        del store["runs"][:-MAX_RUNS]
        save_store()
    return run

@app.get("/results", dependencies=[Depends(require_auth)])
def get_results():
    return store["results"] or {"run_at": None, "yellows": False, "weeks": []}

def _xlsx_response(run: dict) -> Response:
    setup = run.get("setup") or store["published"]
    if not setup:
        raise HTTPException(status_code=400, detail="This run has no saved schedule settings to export")
    data = build_workbook(run, setup)
    first = run["weeks"][0]["week"] if run["weeks"] else "schedule"
    ran = (run.get("run_at") or "")[:10]
    name = f"schedule-{first}" + (f"-run-{ran}" if ran else "")
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'},
    )

@app.get("/results.xlsx", dependencies=[Depends(require_auth)])
def download_results():
    """The last run as an Excel workbook, one sheet per week."""
    if not store["results"]:
        raise HTTPException(status_code=400, detail="Run the schedule first")
    return _xlsx_response(store["results"])

def _find_run(run_id: str) -> dict:
    for run in store["runs"]:
        if run.get("id") == run_id:
            return run
    raise HTTPException(status_code=404, detail="That run is no longer in the history")

@app.get("/runs", dependencies=[Depends(require_auth)])
def list_runs():
    """Every run, newest first, summarised for the History tab."""
    out = []
    for run in reversed(store["runs"]):
        setup = run.get("setup") or {}
        out.append({
            "id": run["id"],
            "run_at": run["run_at"],
            "start": setup.get("start"),
            "end": setup.get("end"),
            "weeks": len(run["weeks"]),
            "short_weeks": sum(1 for w in run["weeks"] if w.get("error")),
            "unfilled": sum(len(w.get("unfilled", [])) for w in run["weeks"]),
            "forced": sum(1 for w in run["weeks"] for f in w.get("forced", []) if f[1] != 1),
            "workers": len(setup.get("workers", [])),
            "submitted": run.get("submitted"),
        })
    return out

@app.get("/runs/{run_id}", dependencies=[Depends(require_auth)])
def get_run(run_id: str):
    return _find_run(run_id)

@app.get("/runs/{run_id}/excel", dependencies=[Depends(require_auth)])
def download_run(run_id: str):
    return _xlsx_response(_find_run(run_id))

@app.delete("/runs/{run_id}", dependencies=[Depends(require_auth)])
def delete_run(run_id: str):
    """Remove a run from the history. If it was the latest run, the Run Schedule
    tab falls back to the run before it (or to nothing)."""
    with store_lock:
        run = _find_run(run_id)
        store["runs"].remove(run)
        if store["results"] and store["results"].get("id") == run_id:
            store["results"] = store["runs"][-1] if store["runs"] else None
        save_store()
    return {"status": "ok"}

# The pages are plain files that change on every deploy. Tell browsers to
# re-check them on each load so nobody runs a stale copy against a new API.
@app.middleware("http")
async def no_cache_static(request, call_next):
    response = await call_next(request)
    if not request.url.path.startswith(("/admin/", "/draft", "/publish", "/published", "/preferences", "/runs", "/unavailable",
                                        "/submit_preference", "/run_schedule", "/results")):
        response.headers["Cache-Control"] = "no-cache"
    return response

app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="static")
