"""proj_3006 — build the `Front` carry heatmap.

Front is pure spreadsheet formulas over `Data`, so it recalculates on its own
whenever the daily job appends a row or backfills a gap — nothing here runs
again after setup. Re-running is safe: every write is idempotent.

    carry = (fut_BID / spot_ASK - 1) * 365 / (TTM - row date)

Executable cash-and-carry: buy spot at ASK, sell the future at BID.

`Data` row 1 drives everything. A symbol with a TTM beside it is a futures
contract and gets a column here; the one without a TTM is the spot leg and
becomes the denominator. Contracts are matched BY NAME, so appending a new
contract to `Data` grows this sheet on its own.
"""

import sys

from simple_heatmap import get_worksheet, load_config, logger, setup_logger

FRONT = "Front"
DATA = "Data"

LAST_ROW = 400  # formulas are pre-filled this far down
LAST_COL = "P"  # room for 15 contracts

# The two header rows of `Data` hold symbol names on odd columns (C, E, G, ...)
# and each symbol's TTM in the column immediately to its right. Both ranges are
# the same width so FILTER can line them up; the second is shifted by one.
# CA must exist as a real column or FILTER returns #N/A, hence DATA_COLS below.
SYMS = f"{DATA}!$C$1:$BZ$1"
TTMS = f"{DATA}!$D$1:$CA$1"
DATA_COLS = 79  # through CA — room for ~38 contracts as expired ones accumulate

# Sequential single-hue ramp, light -> dark (dataviz skill, blue steps 100/400).
# Capped at step 400 rather than 700: the cell value sits on top of the fill, and
# black text on #3987e5 still clears 5.8:1.
RAMP_MIN = {"red": 0.804, "green": 0.886, "blue": 0.984}  # #cde2fb
RAMP_MAX = {"red": 0.224, "green": 0.529, "blue": 0.898}  # #3987e5
SCALE_MIN = "0"
SCALE_MAX = "0.06"

# GOZ26 is "Gold Online Futures (Dec26)" per its MT5 description, so its TTM is
# December 2026. The sheet had 2027-12-29, which read as 1.32%/yr instead of
# 5.49% — an outlier that looked like an arbitrage rather than a typo.
TTM_FIXES = {"P1": "2026-12-29"}


def fix_ttm(data_ws) -> None:
    for cell, want in TTM_FIXES.items():
        got = data_ws.acell(cell, value_render_option="FORMATTED_VALUE").value
        if got == want:
            logger.info("%s!%s already %s.", DATA, cell, want)
            continue
        data_ws.update_acell(cell, want)
        logger.info("%s!%s: %s -> %s", DATA, cell, got, want)


def carry_formula(row: int, col: str) -> str:
    """Look the row up by date and the leg up by contract name.

    Both lookups are by value rather than by position, so the formula survives
    columns being appended to `Data`. A symbol's own column IS its BID column
    (row 2 reads BID under the name), which makes ASK the column after it.
    """
    date = f"$A{row}"
    hit = f"MATCH({date},{DATA}!$A:$A,0)"
    fut = f"INDEX({DATA}!$A:$BZ,{hit},MATCH({col}$1,{DATA}!$1:$1,0))"
    spot = f"INDEX({DATA}!$A:$BZ,{hit},MATCH($A$1,{DATA}!$1:$1,0)+1)"
    return (
        f'=IF(OR({date}="",{col}$1=""),"",'
        f"IFERROR(({fut}/{spot}-1)*365/({col}$2-{date}),\"\"))"
    )


def build(front, cols: list[str]) -> None:
    front.update(
        [
            [
                # The spot leg identifies itself: named, but with no TTM.
                f'=INDEX(FILTER({SYMS},ISODD(COLUMN({SYMS})),{SYMS}<>"",{TTMS}=""),1)',
                f'=FILTER({SYMS},ISODD(COLUMN({SYMS})),{TTMS}<>"")',
            ],
            ["TTM", f'=FILTER({TTMS},ISODD(COLUMN({SYMS})),{TTMS}<>"")'],
            [f'=ARRAYFORMULA(IF({DATA}!A3:A="","",{DATA}!A3:A))'],
        ],
        range_name="A1:B3",
        value_input_option="USER_ENTERED",
    )

    grid = [[carry_formula(r, c) for c in cols] for r in range(3, LAST_ROW + 1)]
    front.update(
        grid,
        range_name=f"B3:{LAST_COL}{LAST_ROW}",
        value_input_option="USER_ENTERED",
    )
    logger.info("Wrote %d x %d carry formulas.", len(grid), len(cols))


def style(front) -> None:
    front.format(
        f"B3:{LAST_COL}{LAST_ROW}",
        {"numberFormat": {"type": "NUMBER", "pattern": "0.00%"}},
    )
    front.format("A1:P2", {"textFormat": {"bold": True}})
    # A3 down is an ARRAYFORMULA of date serials; without this it reads as 46269.
    front.format(
        f"A3:A{LAST_ROW}",
        {"numberFormat": {"type": "DATE", "pattern": "yyyy-mm-dd"}},
    )
    front.format("B2:P2", {"numberFormat": {"type": "DATE", "pattern": "yyyy-mm-dd"}})
    front.freeze(rows=2, cols=1)

    sid = front.id
    rng = {
        "sheetId": sid,
        "startRowIndex": 2,
        "endRowIndex": LAST_ROW,
        "startColumnIndex": 1,
        "endColumnIndex": 16,
    }
    # Drop rules left by an earlier run so re-running does not stack duplicates
    # on the same range. Delete back-to-front: each removal reindexes the rest.
    meta = front.spreadsheet.fetch_sheet_metadata(
        {"fields": "sheets(properties/sheetId,conditionalFormats)"}
    )
    existing = next(
        (
            len(s.get("conditionalFormats", []))
            for s in meta["sheets"]
            if s["properties"]["sheetId"] == sid
        ),
        0,
    )

    front.spreadsheet.batch_update(
        {
            "requests": [
                *(
                    {"deleteConditionalFormatRule": {"sheetId": sid, "index": i}}
                    for i in reversed(range(existing))
                ),
                {
                    "addConditionalFormatRule": {
                        "index": 0,
                        "rule": {
                            "ranges": [rng],
                            "gradientRule": {
                                "minpoint": {
                                    "color": RAMP_MIN,
                                    "type": "NUMBER",
                                    "value": SCALE_MIN,
                                },
                                "maxpoint": {
                                    "color": RAMP_MAX,
                                    "type": "NUMBER",
                                    "value": SCALE_MAX,
                                },
                            },
                        },
                    }
                },
            ]
        }
    )
    logger.info("Locked the colour scale to %s-%s.", SCALE_MIN, SCALE_MAX)


def main() -> int:
    cfg = load_config()
    setup_logger(cfg["log_file"])

    try:
        data_ws = get_worksheet(cfg)
        front = data_ws.spreadsheet.worksheet(FRONT)

        if data_ws.col_count < DATA_COLS:
            data_ws.add_cols(DATA_COLS - data_ws.col_count)
            logger.info("Widened %s to %d columns.", DATA, DATA_COLS)

        fix_ttm(data_ws)

        cols = [chr(ord("B") + i) for i in range(ord(LAST_COL) - ord("B") + 1)]
        build(front, cols)
        style(front)

        logger.info("Front is ready.")
        return 0
    except Exception:
        logger.exception("Setup failed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
