"""Build an Excel workbook from a schedule run, one sheet per week.

The layout is not drawn here. backend/template.xlsx (the supervisor's own
spreadsheet) is the source of truth. One of its sheets is copied once per week
and only the title, the date row, the notes row and the initials are changed.
The master is the sheet with the fewest blacked-out slot cells, i.e. the most
"normal" week, so a holiday week in the template does not black out slots
for every exported week.
Fonts, borders, merges, fills, column widths and row heights all come from
the template untouched.

Template geometry (rows on the sheet):
  1      title, merged A1:I1
  2      dates, B..H
  3      notes (holidays etc.), filled from the day overrides' notes
  4      day names
  5..21  shift blocks, see SHIFT_ROWS
Column A and I carry the shift labels; B..H are Monday..Sunday.
"""
import os
from copy import copy
from datetime import date, timedelta
from io import BytesIO

from openpyxl import load_workbook
from openpyxl.styles import PatternFill

TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "template.xlsx")

# shift index -> rows in the template that hold that shift's slots
SHIFT_ROWS = {
    0: [5],            # 12am-3am
    1: [6],            # 3am-6am
    2: [7, 8],         # 6am-9am
    3: [9, 10, 11],    # 9am-12pm
    4: [12, 13, 14],   # 12pm-3pm
    5: [15, 16, 17],   # 3pm-6pm
    6: [18, 19],       # 6pm-9pm
    7: [20, 21],       # 9pm-12am
}
FIRST_DAY_COL = 2   # B = Monday
DATE_ROW, NOTES_ROW = 2, 3

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
MONTHS_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "June", "July", "Aug", "Sept", "Oct", "Nov", "Dec"]

NO_FILL = PatternFill(fill_type=None)
# Colours taken from the supervisor's own workbook (template2)
YELLOW_FILL = PatternFill("solid", fgColor="FFFFFF00")
INCENTIVE_FILL = {1: PatternFill("solid", fgColor="FF99CC00"), 2: PatternFill("solid", fgColor="FF008000")}
YELLOW_SHIFTS = (3, 4, 5)   # 9am-12pm, 12pm-3pm, 3pm-6pm

def _short(d: date) -> str:
    return f"{MONTHS_SHORT[d.month - 1]} {d.day}"

def _title(monday: date) -> str:
    sunday = monday + timedelta(days=6)
    return f"Weekly Schedule for {MONTHS[monday.month - 1]} {monday.day} - {MONTHS[sunday.month - 1]} {sunday.day}, {sunday.year}"

def _sheet_name(monday: date) -> str:
    return f"{_short(monday)} - {_short(monday + timedelta(days=6))}"[:31]

def _is_black(cell) -> bool:
    """Solid black fill, whether stored as RGB or as the 'Text' theme colour (theme 1)."""
    if cell.fill.fill_type != "solid":
        return False
    c = cell.fill.fgColor
    if c.type == "rgb":
        return str(c.rgb).upper().endswith("000000")
    if c.type == "theme":
        return c.theme == 1 and (c.tint or 0) == 0
    return False

def _merge_containing(ws, cell):
    for rng in ws.merged_cells.ranges:
        if cell.coordinate in rng:
            return rng
    return None

def _incentive_level(published: dict, week_iso: str, di: int, si: int) -> int:
    for c in (published or {}).get("incentives", {}).get(week_iso, []):
        if c[0] == di and c[1] == si:
            return c[2] if len(c) > 2 else 1
    return 0

def _day_setup(published: dict, week_iso: str, di: int):
    """(caps, yellows, note) for one day: its override if it has one, else the defaults."""
    o = (published or {}).get("overrides", {}).get(week_iso, {}).get(str(di))
    if o:
        return o["caps"], bool(o["yellows"]), o.get("note") or None
    settings = (published or {}).get("settings") or {}
    caps = settings.get("weekdays" if di <= 4 else "weekends") or []
    return caps, bool(settings.get("yellows")) and di <= 4, None

def _fill_week(ws, week_iso: str, grid, published: dict, forced=()):
    monday = date.fromisoformat(week_iso)
    forced = {(d, s, n) for d, s, n in forced}
    ws["A1"].value = _title(monday)

    for i in range(7):
        col = FIRST_DAY_COL + i
        ws.cell(row=DATE_ROW, column=col).value = _short(monday + timedelta(days=i))
        ws.cell(row=NOTES_ROW, column=col).value = _day_setup(published, week_iso, i)[2]

    for si, rows in SHIFT_ROWS.items():
        for di in range(7):
            col = FIRST_DAY_COL + di
            # Autofilled (forced) seats are shown as [JB]
            names = [f"[{n}]" if (di, si, n) in forced else n for n in (grid[di][si] or [])]

            # Slots available for this day in the template: skip cells hidden
            # inside a vertical merge (e.g. weekend 6am-9am is one tall cell).
            slots = []
            for r in rows:
                cell = ws.cell(row=r, column=col)
                rng = _merge_containing(ws, cell)
                if rng and (r, col) != (rng.min_row, rng.min_col):
                    continue
                slots.append(cell)

            # More names than the template has room for: split the merge or
            # reopen blacked-out cells so nothing is silently dropped.
            if len(names) > len(slots):
                for r in rows:
                    cell = ws.cell(row=r, column=col)
                    rng = _merge_containing(ws, cell)
                    if rng:
                        ws.unmerge_cells(str(rng))
                slots = [ws.cell(row=r, column=col) for r in rows]
                for cell in slots[:len(names)]:
                    if _is_black(cell):
                        cell.fill = copy(NO_FILL)

            level = _incentive_level(published, week_iso, di, si)
            caps, yellows, _ = _day_setup(published, week_iso, di)
            yellow_slot = -1
            if yellows and si in YELLOW_SHIFTS and si < len(caps) and caps[si] > 0:
                yellow_slot = caps[si] - 1      # the optional extra seat is the last one

            for k, cell in enumerate(slots):
                if k < len(names):
                    if _is_black(cell):
                        cell.fill = copy(NO_FILL)
                    # last slot absorbs any overflow
                    cell.value = " / ".join(names[k:]) if k == len(slots) - 1 else names[k]
                elif not _is_black(cell):
                    cell.value = None

                if _is_black(cell):
                    continue
                if level:
                    cell.fill = copy(INCENTIVE_FILL[level])
                elif k == yellow_slot:
                    cell.fill = copy(YELLOW_FILL)

def build_workbook(results: dict, published: dict = None) -> bytes:
    wb = load_workbook(TEMPLATE)
    originals = list(wb.worksheets)

    def black_count(ws):
        return sum(1 for r in range(5, 22) for c in range(FIRST_DAY_COL, FIRST_DAY_COL + 7)
                   if _is_black(ws.cell(row=r, column=c)))
    master = min(originals, key=black_count)
    # Park the template sheets under names that cannot collide with a real week
    for i, ws in enumerate(originals):
        ws.title = f"__template{i}"

    for week in results["weeks"]:
        ws = wb.copy_worksheet(master)
        ws.title = _sheet_name(date.fromisoformat(week["week"]))
        # copy_worksheet keeps cell styles, merges and dimensions but not view/print settings
        ws.sheet_view.showGridLines = master.sheet_view.showGridLines
        ws.page_setup = copy(master.page_setup)
        ws.page_margins = copy(master.page_margins)
        ws.print_options = copy(master.print_options)
        ws.sheet_properties.pageSetUpPr = copy(master.sheet_properties.pageSetUpPr)
        _fill_week(ws, week["week"], week["schedule"], published, week.get("forced", []))

    for ws in originals:
        wb.remove(ws)
    if not wb.sheetnames:
        wb.create_sheet(title="Empty")

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
