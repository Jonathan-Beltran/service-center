import os
import json
import secrets
import threading
from datetime import date, timedelta
from typing import Dict, List

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

from schedule import schedule

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
# store = {
#   "workers":     [{"initials": "HM", "points": 8516.38, "doubles": false}, ...],
#   "settings":    {"weekdays": [..8 ints..], "weekends": [..8 ints..], "yellows": bool},
#   "weeks":       ["2026-09-21", "2026-09-28", ...]          # ISO date of each Monday
#   "preferences": {"HM": {"2026-09-21": [[day, shift], ...], ...}, ...},
#   "results":     {"2026-09-21": {"error": bool, "unfilled": [...], "schedule": grid}, ...},
# }

DEFAULT_SETTINGS = {
    "weekdays": [1, 1, 2, 3, 3, 3, 2, 2],
    "weekends": [1, 1, 1, 2, 2, 2, 2, 2],
    "yellows": True,
}

def _empty_store() -> dict:
    return {
        "workers": [],
        "settings": dict(DEFAULT_SETTINGS),
        "weeks": [],
        "preferences": {},
        "results": {},
    }

def _load_store() -> dict:
    base = _empty_store()
    try:
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
        if isinstance(data, dict):
            base.update(data)
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

# ── Workers ───────────────────────────────────────────────────────────────────

class WorkerEntry(BaseModel):
    initials: str
    points: float
    doubles: bool

@app.get("/workers")
def get_workers():
    return store["workers"]

@app.post("/workers", dependencies=[Depends(require_auth)])
def save_workers(workers: List[WorkerEntry]):
    with store_lock:
        store["workers"] = [
            {"initials": w.initials.strip().upper(), "points": w.points, "doubles": w.doubles}
            for w in workers
        ]
        save_store()
    return {"status": "ok"}

# ── Shift-size settings ───────────────────────────────────────────────────────

class Settings(BaseModel):
    weekdays: List[int]
    weekends: List[int]
    yellows: bool

def _validate_settings(s: Settings) -> dict:
    if len(s.weekdays) != 8 or len(s.weekends) != 8:
        raise HTTPException(status_code=400, detail="weekdays and weekends must each have 8 entries")
    return s.model_dump()

@app.get("/settings")
def get_settings():
    return store["settings"]

@app.put("/settings", dependencies=[Depends(require_auth)])
def put_settings(body: Settings):
    with store_lock:
        store["settings"] = _validate_settings(body)
        save_store()
    return {"status": "ok"}

# ── Scheduling period (weeks) ─────────────────────────────────────────────────

def _monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())

class WeeksRequest(BaseModel):
    start: date
    end: date

@app.get("/weeks")
def get_weeks():
    return {"weeks": store["weeks"]}

@app.put("/weeks", dependencies=[Depends(require_auth)])
def set_weeks(body: WeeksRequest):
    if body.end < body.start:
        raise HTTPException(status_code=400, detail="End date must be on or after start date")

    weeks = []
    cur = _monday_of(body.start)
    last = _monday_of(body.end)
    while cur <= last:
        weeks.append(cur.isoformat())
        cur += timedelta(days=7)

    if len(weeks) > MAX_WEEKS:
        raise HTTPException(status_code=400, detail=f"Scheduling period can cover at most {MAX_WEEKS} weeks")

    keep = set(weeks)
    with store_lock:
        store["weeks"] = weeks
        # Drop preferences and results for weeks that are no longer in the period
        for initials in list(store["preferences"]):
            by_week = {w: t for w, t in store["preferences"][initials].items() if w in keep}
            if by_week:
                store["preferences"][initials] = by_week
            else:
                del store["preferences"][initials]
        store["results"] = {w: r for w, r in store["results"].items() if w in keep}
        save_store()

    return {"weeks": weeks}

# ── Worker preferences ────────────────────────────────────────────────────────

class PreferenceSubmission(BaseModel):
    initials: str
    weeks: Dict[str, List[List[int]]]   # ISO Monday -> ordered [[day, shift], ...]

@app.post("/submit_preference")
def submit_preference(pref: PreferenceSubmission):
    initials = pref.initials.strip().upper()
    if not initials:
        raise HTTPException(status_code=400, detail="Initials are required")

    open_weeks = set(store["weeks"])
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

# ── Run schedule ──────────────────────────────────────────────────────────────

@app.get("/results", dependencies=[Depends(require_auth)])
def get_results():
    return {"weeks": [store["results"][w] for w in store["weeks"] if w in store["results"]]}

@app.post("/run_schedule", dependencies=[Depends(require_auth)])
def run_schedule(body: Settings):
    if not store["weeks"]:
        raise HTTPException(status_code=400, detail="Set the scheduling period first")

    settings = _validate_settings(body)
    workers = store["workers"]
    priority_by_initials = {w["initials"]: w["points"] for w in workers}
    doubles_recipients = {w["initials"] for w in workers if w["doubles"]}

    results = []
    for week in store["weeks"]:
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
            result = {"week": week, "error": True, "unfilled": out["unfilled"], "schedule": out["schedule"]}
        else:
            result = {"week": week, "error": False, "unfilled": [], "schedule": out}
        results.append(result)

    with store_lock:
        store["settings"] = settings
        store["results"] = {r["week"]: r for r in results}
        save_store()

    return {"weeks": results}


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="static")
