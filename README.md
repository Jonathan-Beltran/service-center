Service Center Shift Scheduler
==============================

A small web app for scheduling shift work. Supervisors set up a scheduling
period and post it; workers rank the shifts they want; the app assigns shifts
by priority points and exports the result as an Excel workbook.

  - frontend/index.html  worker page: pick your initials, rank shifts per week
  - frontend/admin.html  supervisor panel (password protected)
  - backend/             FastAPI server that serves both pages and the API

Running locally
---------------
Requires Python 3.10+.

  git clone https://github.com/Jonathan-Beltran/service-center.git
  cd service-center
  python3 -m venv backend/venv
  source backend/venv/bin/activate
  pip install -r requirements.txt
  cd backend
  ADMIN_PASSWORD='choose-a-password' uvicorn main:app --reload

Then open the pages through the server (opening the HTML files directly from
disk will not work, they need the API):

  http://127.0.0.1:8000/index.html   worker shift picker
  http://127.0.0.1:8000/admin.html   supervisor panel

Configuration
-------------
Environment variables:

  ADMIN_PASSWORD  Password for the supervisor panel. Always set this. If it
                  is not set, the server falls back to the default in
                  backend/main.py, which is public in this repository.
  DATA_FILE       Where all state is saved (JSON). Default: backend/store.json.

Supervisor sign-ins are kept in memory, so restarting the server signs
everyone out.

Deploying
---------
The Procfile starts the server on $PORT, so it runs as-is on Railway, Heroku
and similar hosts:

  1. Set ADMIN_PASSWORD in the host's environment variables.
  2. Attach a persistent volume and set DATA_FILE to a path on it
     (e.g. DATA_FILE=/data/store.json). Without a volume, every redeploy
     or restart wipes the draft, submissions and run history.

Using the supervisor panel
--------------------------
The panel has four tabs.

1. Create Schedule
   Set the period (start and end dates), shift sizes and the worker list.
   This is a draft: it saves as you type, but workers see none of it until
   it is published.
   - Workers: paste "INITIALS POINTS" lines to add them. "Bonus Shift" gives
     a worker an extra pick per round. "Absent" moves a worker to the bottom
     of the scheduling order (see Scheduling rules).
   - Can't-Work Shifts: mark shifts a worker absolutely cannot work. They are
     never scheduled there, by their own picks or by autofill. These save
     right away and workers never see them.
   - Yellows: on weekday 9am-6pm shifts, the last seat is an optional extra.
   - Autofill Empty Shifts (on by default): fills required seats nobody
     picked (see Scheduling rules).

2. Publish Schedule
   Shows the empty schedule built from the draft.
   - Click or drag over cells to mark incentive pay: once for +$1 (light
     green), twice for +$4 (dark green). Workers see the same colors.
   - Click a day's name to give that one day its own shift sizes, yellows
     and an optional note (e.g. a holiday). The note goes in the Excel
     notes row.
   - "Publish Schedule" posts it to staff. Publishing again replaces it;
     submissions for weeks no longer in the period are deleted.
   - "Remove from Staff View" takes it down. The draft and submissions are
     kept, and publishing again brings everything back.

3. Run Schedule
   Shows who has submitted, runs the scheduler and shows the result.
   Workers never see results in the app; "Download Excel" produces a
   workbook (one sheet per week, laid out from backend/template.xlsx) to
   share.

4. History
   Every run, newest first, with the date it ran, the period and whether
   every seat was filled. Each run can be viewed, downloaded as Excel or
   deleted. A run keeps the settings it ran with, so later changes don't
   alter it. The last 200 runs are kept.

Scheduling rules
----------------
Each week is scheduled on its own (backend/schedule.py).

  Order     Workers who submitted picks, highest priority points first.
            Workers marked Absent go after everyone else, also by points.
  Rounds    Going down the order, each worker gets 2 turns per round (3
            with Bonus Shift). A turn assigns their highest-ranked pick that
            still has an open required seat. Rounds repeat until every
            required seat is filled or nothing more can be filled.
  Graves    Taking 12am-3am also means working 3am-6am; that half is
            assigned in the worker's next round and uses one of their turns.
  Autofill  Required seats still empty go to the worker with the fewest
            shifts that week; ties go to the lowest-ranked worker. Shown as
            [JB] in the results and the Excel export.
  Yellows   Never forced. Once all required seats are filled, yellow seats
            go out one at a time down the order, to workers who picked them,
            in rounds until none are left.

Live updates
------------
Both pages check the server every 10 seconds while they are on screen, and
right away when you switch back to them. The supervisor's Submitted Shifts
list picks up new submissions, and the worker page shows or hides the
schedule as it is published or removed (keeping any unsaved picks).

Persistence
-----------
All state (draft, published schedule, submissions, can't-work marks, latest
results and run history) is saved to the JSON file at DATA_FILE.
