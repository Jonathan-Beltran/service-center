"""Build an Excel workbook from a schedule run: one sheet per week.

Layout per sheet (mirrors the supervisor's existing spreadsheet):
  row 1   title, merged across the day columns
  row 2   dates ("Sept 14" ...)
  row 3   blank, for notes such as holidays
  row 4   day names
  then one block per shift, as many rows as the shift's largest capacity.
  Column A and the column after Sunday both carry the shift label.
  Days with fewer slots than the block has rows get black cells; a day with a
  single slot in a multi-row block is merged vertically instead.
"""
from datetime import date, timedelta
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

SHIFT_LABELS = ["12am-3am", "3am-6am", "6am-9am", "9am-12pm", "12pm-3pm", "3pm-6pm", "6pm-9pm", "9pm-12am"]
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
MONTHS_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "June", "July", "Aug", "Sept", "Oct", "Nov", "Dec"]

FIRST_DAY_COL = 2                 # column B
LAST_DAY_COL = FIRST_DAY_COL + 6  # column H
RIGHT_LABEL_COL = LAST_DAY_COL + 1

thin = Side(style="thin", color="000000")
thick = Side(style="medium", color="000000")
BLACK = PatternFill("solid", fgColor="000000")
CENTER = Alignment(horizontal="center", vertical="center")

def _long_range(monday: date) -> str:
    sunday = monday + timedelta(days=6)
    if monday.month == sunday.month:
        return f"{MONTHS[monday.month - 1]} {monday.day} - {MONTHS[sunday.month - 1]} {sunday.day}, {sunday.year}"
    if monday.year == sunday.year:
        return f"{MONTHS[monday.month - 1]} {monday.day} - {MONTHS[sunday.month - 1]} {sunday.day}, {sunday.year}"
    return f"{MONTHS[monday.month - 1]} {monday.day}, {monday.year} - {MONTHS[sunday.month - 1]} {sunday.day}, {sunday.year}"

def _short(d: date) -> str:
    return f"{MONTHS_SHORT[d.month - 1]} {d.day}"

def _sheet_title(monday: date) -> str:
    # Excel sheet names: max 31 chars, no / \ ? * [ ] :
    return f"{_short(monday)} - {_short(monday + timedelta(days=6))}"[:31]

def _border(cell, left=thin, right=thin, top=thin, bottom=thin):
    cell.border = Border(left=left, right=right, top=top, bottom=bottom)

def _write_week(ws, week_iso: str, grid, settings: dict):
    monday = date.fromisoformat(week_iso)
    weekdays, weekends = settings["weekdays"], settings["weekends"]

    # Column widths / base row heights
    ws.column_dimensions["A"].width = 13
    for c in range(FIRST_DAY_COL, LAST_DAY_COL + 1):
        ws.column_dimensions[get_column_letter(c)].width = 13
    ws.column_dimensions[get_column_letter(RIGHT_LABEL_COL)].width = 13

    # Row 1: title
    ws.merge_cells(start_row=1, start_column=FIRST_DAY_COL, end_row=1, end_column=LAST_DAY_COL)
    t = ws.cell(row=1, column=FIRST_DAY_COL, value=f"Weekly Schedule for {_long_range(monday)}")
    t.font = Font(name="Arial", size=20, underline="single")
    t.alignment = CENTER
    ws.row_dimensions[1].height = 30

    # Row 2: dates, Row 3: blank notes row, Row 4: day names
    for i in range(7):
        col = FIRST_DAY_COL + i
        d = ws.cell(row=2, column=col, value=_short(monday + timedelta(days=i)))
        d.font = Font(name="Arial", bold=True, size=12)
        d.alignment = CENTER
        n = ws.cell(row=4, column=col, value=DAY_NAMES[i])
        n.font = Font(name="Arial", bold=True, size=11)
        n.alignment = CENTER
        _border(n, top=thick, bottom=thick, left=thick if i == 0 else thin, right=thick if i == 6 else thin)
    ws.row_dimensions[2].height = 22
    ws.row_dimensions[3].height = 22
    ws.row_dimensions[4].height = 20

    row = 5
    for si, label in enumerate(SHIFT_LABELS):
        rows = max(weekdays[si], weekends[si], 1)
        top_row, bottom_row = row, row + rows - 1

        # Shift labels on both sides, merged down the block
        for col in (1, RIGHT_LABEL_COL):
            if rows > 1:
                ws.merge_cells(start_row=top_row, start_column=col, end_row=bottom_row, end_column=col)
            c = ws.cell(row=top_row, column=col, value=label)
            c.font = Font(name="Arial", bold=True, size=13)
            c.alignment = CENTER
            for r in range(top_row, bottom_row + 1):
                _border(ws.cell(row=r, column=col), left=thick, right=thick,
                        top=thick if r == top_row else thin, bottom=thick if r == bottom_row else thin)

        for di in range(7):
            col = FIRST_DAY_COL + di
            cap = weekdays[si] if di <= 4 else weekends[si]
            names = list(grid[di][si]) if grid[di][si] else []

            if cap == 1 and rows > 1:
                # One slot in a taller block: merge the day's cells vertically
                ws.merge_cells(start_row=top_row, start_column=col, end_row=bottom_row, end_column=col)
                c = ws.cell(row=top_row, column=col, value=names[0] if names else None)
                c.font = Font(name="Arial", bold=True, size=22)
                c.alignment = CENTER
            else:
                for k in range(rows):
                    c = ws.cell(row=top_row + k, column=col)
                    if k < cap:
                        c.value = names[k] if k < len(names) else None
                        c.font = Font(name="Arial", bold=True, size=22)
                        c.alignment = CENTER
                    else:
                        c.fill = BLACK

            for r in range(top_row, bottom_row + 1):
                _border(ws.cell(row=r, column=col),
                        left=thick if di == 0 else thin, right=thick if di == 6 else thin,
                        top=thick if r == top_row else thin, bottom=thick if r == bottom_row else thin)

        for r in range(top_row, bottom_row + 1):
            ws.row_dimensions[r].height = 36
        row = bottom_row + 1

    ws.sheet_view.showGridLines = True
    ws.freeze_panes = None

def build_workbook(results: dict, settings: dict) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for week in results["weeks"]:
        ws = wb.create_sheet(title=_sheet_title(date.fromisoformat(week["week"])))
        _write_week(ws, week["week"], week["schedule"], settings)
    if not wb.sheetnames:
        wb.create_sheet(title="Empty")
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
