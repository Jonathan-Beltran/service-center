import os
import secrets
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import List
from schedule import schedule

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, "..", "frontend")


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE", "PUT"],
    allow_headers=["Content-Type", "Authorization"],
)

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

# In-memory store: workers list
stored_workers: list = []

class WorkerEntry(BaseModel):
    initials: str
    points: float
    doubles: bool

@app.get("/workers")
def get_workers():
    return stored_workers

@app.post("/workers", dependencies=[Depends(require_auth)])
def save_workers(workers: List[WorkerEntry]):
    stored_workers.clear()
    stored_workers.extend(w.model_dump() for w in workers)
    return {"status": "ok"}

# In-memory store: initials -> times
submitted_preferences: dict = {}

class PreferenceSubmission(BaseModel):
    initials: str
    times: List[List[int]]

@app.post("/submit_preference")
def submit_preference(pref: PreferenceSubmission):
    submitted_preferences[pref.initials.upper()] = pref.times
    return {"status": "ok"}

@app.get("/preferences", dependencies=[Depends(require_auth)])
def get_preferences():
    return [{"initials": k, "times": v} for k, v in submitted_preferences.items()]

@app.delete("/preferences/{initials}", dependencies=[Depends(require_auth)])
def delete_preference(initials: str):
    key = initials.upper()
    if key not in submitted_preferences:
        raise HTTPException(status_code=404, detail="Not found")
    del submitted_preferences[key]
    return {"status": "ok"}

@app.delete("/preferences", dependencies=[Depends(require_auth)])
def clear_preferences():
    submitted_preferences.clear()
    return {"status": "ok"}

class WorkerPreference(BaseModel):
    worker_initials: str
    times: List[List[int]]

class AdminSettings(BaseModel):
    weekdays: List[int]
    weekends: List[int]
    yellows: bool
    doublesRecipients: List[str]
    all_initials: List[str]
    priority_points: List[float]

class ScheduleRequest(BaseModel):
    worker_preferences: List[WorkerPreference]
    admin_settings: AdminSettings

@app.post("/run_schedule", dependencies=[Depends(require_auth)])
def run_schedule(request: ScheduleRequest):
    preferences = request.worker_preferences
    settings = request.admin_settings

    priority_by_initials = {
        initials: points
        for initials, points in zip(settings.all_initials, settings.priority_points)
    }

    sorted_workers = sorted(
        preferences,
        key=lambda worker: priority_by_initials.get(worker.worker_initials, 0),
        reverse=True
    )

    doubles_recipients = set(settings.doublesRecipients)

    data = {
        "names": [w.worker_initials for w in sorted_workers],
        "priority_points": [priority_by_initials.get(w.worker_initials, 0) for w in sorted_workers],
        "selected_times": [w.times for w in sorted_workers],
        "doubles": [w.worker_initials in doubles_recipients for w in sorted_workers],
    }

    days = {
        "weekdays": settings.weekdays,
        "weekends": settings.weekends,
        "yellows": settings.yellows
    }

    return schedule(data, days)


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="static")
