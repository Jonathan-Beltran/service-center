from collections import deque

def rank_key(points, absent):
    """Scheduling order: everyone present by priority points (highest first),
    then everyone marked absent, also by priority points."""
    return (bool(absent), -points)

def schedule(data, days):
    absent = data.get("absent") or [False] * len(data["names"])
    users = []
    for index, name in enumerate(data["names"]):
        users.append([
            name,
            data["priority_points"][index],
            deque(data["selected_times"][index]),
            data["doubles"][index],
            absent[index],
        ])

    users.sort(key=lambda x: rank_key(x[1], x[4]))

    # Per-day capacities and yellows. Weekdays/weekends are the defaults;
    # days["overrides"] = {day_index: {"caps": [8 ints], "yellows": bool}}
    # replaces them for individual days (holidays etc.).
    overrides = days.get("overrides") or {}
    caps_by_day = []
    yellow_by_day = []
    for day_index in range(7):
        o = overrides.get(day_index) or overrides.get(str(day_index))
        if o:
            caps = list(o["caps"])
            yellow = bool(o["yellows"])
        else:
            caps = list(days["weekdays"] if day_index <= 4 else days["weekends"])
            yellow = bool(days["yellows"]) and day_index <= 4
        # 3am-6am (shift 1) always mirrors 12am-3am (shift 0)
        caps[1] = caps[0]
        caps_by_day.append(caps)
        yellow_by_day.append(yellow)

    def is_yellow(day, time):
        return yellow_by_day[day] and time in (3, 4, 5) and caps_by_day[day][time] > 0

    def required(day, time):
        """Seats that must be filled; the yellow seat is optional."""
        cap = caps_by_day[day][time]
        return cap - 1 if is_yellow(day, time) else cap

    # 7 days x 8 shifts
    schedule_grid = [[[] for _ in range(8)] for _ in range(7)]

    num_required_shifts = 0
    num_yellow_shifts = 0

    for day_index in range(7):
        for shift_index in range(8):
            if shift_index == 1:
                # 3am-6am is filled via pending graves, not counted upfront
                continue
            num_required_shifts += required(day_index, shift_index)
            if is_yellow(day_index, shift_index):
                num_yellow_shifts += 1

    # Snapshot preferences before the main loop mutates the deques
    original_prefs = {user[0]: list(user[2]) for user in users}

    # pending_graves[name] = list of day indices where 3am-6am is owed
    pending_graves = {user[0]: [] for user in users}

    # ── Main scheduling loop ──────────────────────────────────────────────────

    while num_required_shifts > 0:
        start = num_required_shifts

        for user in users:
            username  = user[0]
            sel_times = user[2]
            doubles   = user[3]
            num_picks = 3 if doubles else 2
            picks_left = num_picks

            # Resolve forced grave completions first
            graves = list(pending_graves[username])
            pending_graves[username] = []

            for grave_day in graves:
                if picks_left <= 0:
                    pending_graves[username].append(grave_day)
                    continue
                if len(schedule_grid[grave_day][1]) < caps_by_day[grave_day][1]:
                    schedule_grid[grave_day][1].append(username)
                    num_required_shifts -= 1
                picks_left -= 1

            # Free picks
            for _ in range(picks_left):
                if not sel_times:
                    break

                day, time = sel_times[0]
                limit = required(day, time)

                # Advance past full slots
                out_of_selections = False
                while len(schedule_grid[day][time]) >= limit:
                    sel_times.popleft()
                    if not sel_times:
                        out_of_selections = True
                        break
                    day, time = sel_times[0]
                    limit = required(day, time)

                if out_of_selections:
                    break

                schedule_grid[day][time].append(username)
                num_required_shifts -= 1
                sel_times.popleft()

                # Starting a grave: owe 3am-6am on this day next cycle
                if time == 0:
                    pending_graves[username].append(day)
                    num_required_shifts += 1  # add the matching 3am-6am requirement

        if num_required_shifts == start:
            break   # nobody's picks can fill what is left

    # A 12am-3am taken in the final pass leaves its 3am-6am owed (the pass
    # nets zero progress, so the loop stops before resolving it). Give it to
    # the worker who started the grave.
    for username, grave_days in pending_graves.items():
        for grave_day in list(grave_days):
            if len(schedule_grid[grave_day][1]) < caps_by_day[grave_day][1]:
                schedule_grid[grave_day][1].append(username)
                num_required_shifts -= 1
                grave_days.remove(grave_day)

    # ── Autofill (optional) ──────────────────────────────────────────────────
    # Seats nobody picked are forced onto workers: fewest shifts this week
    # first, then lowest ranking (absent workers rank lowest, then by priority
    # points, lowest first). Every worker on the list is eligible,
    # including those who did not submit picks for this week, except for
    # shifts they marked as unable to work (data["unavailable"]).

    forced = []   # [[day, shift, name], ...]
    if days.get("autofill") and num_required_shifts > 0:
        # (initials, points, absent)
        pool = data.get("all_workers") or [(user[0], user[1], user[4]) for user in users]
        blocked = {(name, d, s) for name, cells in (data.get("unavailable") or {}).items() for d, s in cells}

        def shift_count(name):
            # 12am-6am is one grave shift, so 3am-6am is not counted separately
            return sum(name in schedule_grid[d][s] for d in range(7) for s in range(8) if s != 1)

        for day_i in range(7):
            for shift_i in range(8):
                if shift_i == 1:
                    continue   # filled together with 12am-3am below
                while len(schedule_grid[day_i][shift_i]) < required(day_i, shift_i):
                    candidates = [w for w in pool
                                  if w[0] not in schedule_grid[day_i][shift_i]
                                  and (w[0], day_i, shift_i) not in blocked
                                  and not (shift_i == 0 and w[0] in schedule_grid[day_i][1])]
                    if not candidates:
                        break
                    # Lowest ranking first: absent workers, then fewest priority points
                    name = min(candidates, key=lambda w: (shift_count(w[0]), not (len(w) > 2 and w[2]), w[1]))[0]
                    schedule_grid[day_i][shift_i].append(name)
                    forced.append([day_i, shift_i, name])
                    # A forced grave carries its 3am-6am half with it
                    if shift_i == 0 and len(schedule_grid[day_i][1]) < caps_by_day[day_i][1]:
                        schedule_grid[day_i][1].append(name)
                        forced.append([day_i, 1, name])

    day_names   = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    shift_names = ["12am–3am","3am–6am","6am–9am","9am–12pm","12pm–3pm","3pm–6pm","6pm–9pm","9pm–12am"]
    unfilled = []
    for day_i in range(7):
        for shift_i in range(8):
            if shift_i == 1:
                continue
            short = required(day_i, shift_i) - len(schedule_grid[day_i][shift_i])
            for _ in range(short):
                unfilled.append(f"{day_names[day_i]} {shift_names[shift_i]}")
    for username, grave_days in pending_graves.items():
        for grave_day in grave_days:
            unfilled.append(f"{day_names[grave_day]} {shift_names[1]} (owed by {username})")
    if unfilled:
        return {"error": True, "unfilled": unfilled, "schedule": schedule_grid, "forced": forced}

    # ── Yellow slots (optional extra seats) ──────────────────────────────────
    # Use original_prefs so preferences popped during the main loop aren't lost.
    # Fill in priority order; stop each slot once capacity is reached.

    for day_i in range(7):
        for shift_i in (3, 4, 5):
            if not is_yellow(day_i, shift_i):
                continue
            cap = caps_by_day[day_i][shift_i]
            for user in users:
                if len(schedule_grid[day_i][shift_i]) >= cap:
                    break
                username = user[0]
                wanted = any(p[0] == day_i and p[1] == shift_i for p in original_prefs[username])
                if wanted and username not in schedule_grid[day_i][shift_i]:
                    schedule_grid[day_i][shift_i].append(username)

    return {"error": False, "unfilled": [], "schedule": schedule_grid, "forced": forced}
