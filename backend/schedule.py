from collections import deque

def schedule(data, days):
    users = []
    for index, name in enumerate(data["names"]):
        users.append([
            name,
            data["priority_points"][index],
            deque(data["selected_times"][index]),
            data["doubles"][index]
        ])

    users.sort(key=lambda x: x[1], reverse=True)

    # 3am-6am (shift 1) always mirrors 12am-3am (shift 0)
    days["weekdays"][1] = days["weekdays"][0]
    days["weekends"][1] = days["weekends"][0]

    # 7 days x 8 shifts
    schedule_grid = [[[] for _ in range(8)] for _ in range(7)]

    num_required_shifts = 0
    num_yellow_shifts = 0

    for day_index in range(7):
        caps = days["weekdays"] if day_index <= 4 else days["weekends"]
        for shift_index, cap in enumerate(caps):
            if shift_index == 1:
                # 3am-6am is filled via pending graves, not counted upfront
                continue
            if days["yellows"] and day_index <= 4 and shift_index in (3, 4, 5):
                num_required_shifts += cap - 1
                num_yellow_shifts += 1
            else:
                num_required_shifts += cap

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
                cap = days["weekdays"][1] if grave_day <= 4 else days["weekends"][1]
                if len(schedule_grid[grave_day][1]) < cap:
                    schedule_grid[grave_day][1].append(username)
                    num_required_shifts -= 1
                picks_left -= 1

            # Free picks
            for _ in range(picks_left):
                if not sel_times:
                    break

                day, time = sel_times[0]

                if day <= 4:
                    if not days["yellows"]:
                        limit = days["weekdays"][time]
                    else:
                        limit = days["weekdays"][time] - 1 if time in (3, 4, 5) else days["weekdays"][time]
                else:
                    limit = days["weekends"][time]

                # Advance past full slots
                out_of_selections = False
                while len(schedule_grid[day][time]) >= limit:
                    sel_times.popleft()
                    if not sel_times:
                        out_of_selections = True
                        break
                    day, time = sel_times[0]
                    if day <= 4:
                        if not days["yellows"]:
                            limit = days["weekdays"][time]
                        else:
                            limit = days["weekdays"][time] - 1 if time in (3, 4, 5) else days["weekdays"][time]
                    else:
                        limit = days["weekends"][time]

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
            day_names   = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
            shift_names = ["12am–3am","3am–6am","6am–9am","9am–12pm","12pm–3pm","3pm–6pm","6pm–9pm","9pm–12am"]
            unfilled = []
            for day_i in range(7):
                caps = days["weekdays"] if day_i <= 4 else days["weekends"]
                for shift_i, cap in enumerate(caps):
                    if shift_i == 1:
                        continue
                    required = cap - 1 if (days["yellows"] and day_i <= 4 and shift_i in (3, 4, 5)) else cap
                    short = required - len(schedule_grid[day_i][shift_i])
                    for _ in range(short):
                        unfilled.append(f"{day_names[day_i]} {shift_names[shift_i]}")
            for username, grave_days in pending_graves.items():
                for grave_day in grave_days:
                    unfilled.append(f"{day_names[grave_day]} {shift_names[1]} (owed by {username})")
            return {"error": True, "unfilled": unfilled, "schedule": schedule_grid}

    # ── Yellow slots (optional extra seats) ──────────────────────────────────
    # Use original_prefs so preferences popped during the main loop aren't lost.
    # Fill in priority order; stop each slot once capacity is reached.

    if days["yellows"]:
        for day_i in range(5):  # weekdays only
            for shift_i in (3, 4, 5):
                cap = days["weekdays"][shift_i]
                for user in users:
                    if len(schedule_grid[day_i][shift_i]) >= cap:
                        break
                    username = user[0]
                    wanted = any(p[0] == day_i and p[1] == shift_i for p in original_prefs[username])
                    if wanted and username not in schedule_grid[day_i][shift_i]:
                        schedule_grid[day_i][shift_i].append(username)

    return schedule_grid
