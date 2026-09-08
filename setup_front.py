"""proj_3006 — build the `Front` pairwise spread matrix.

An upper-triangular grid of every leg against every later leg, for the most
recent row in `Data`. Cell [row X, col Y] is the spread of selling Y and
buying X:

    spread = Y_BID - X_ASK

The spot row additionally shows that spread annualized, since spot-vs-future
is a carry with a well-defined holding period. Future-vs-future pairs show the
spread alone — annualizing those needs a convention we have not settled.

    spot row   4.94% (+15.16) (25d)
    other row  (+34.50) (26d)

The trailing figure is days to expiry of whichever leg expires first: the near
leg is what forces the position to be unwound.

Front is pure spreadsheet formulas over `Data`, so it follows the daily job on
its own. Everything is keyed to leg NAMES read out of `Data` row 1, so adding a
contract there grows the matrix. Re-running this script is safe.
"""

import sys
from string import ascii_uppercase

from simple_heatmap import get_worksheet, load_config, logger, setup_logger

FRONT = "Front"
DATA = "Data"

# Legs = spot + every contract. Rows carry legs 1..N-1, columns legs 2..N, so
# each holds MAX_LEGS-1 entries. The hidden numeric mirror sits COLOUR_OFFSET
# columns to the right and must still fit inside the sheet's 26 columns.
MAX_LEGS = 12
SPAN = MAX_LEGS - 1
FIRST_ROW = 2
LAST_ROW = FIRST_ROW + SPAN - 1  # 12
COLOUR_OFFSET = 14  # B -> P

# Chart scaffolding, in the hidden columns below the mirror. One row per pair
# in the triangle, so no sorting is needed - these are points, not lines.
#   P,Q,R  term (days) | yield | venue class
#   S,T,U  that yield split into one column per class
CHART_ROW = 14  # header row; data runs CHART_ROW+1 .. CHART_ROW+MAX_PAIRS
RAW_COL = 16  # P
SERIES_COL = 19  # S
MAX_PAIRS = SPAN * (SPAN + 1) // 2

# The dataviz categorical palette. Slots are assigned in this fixed order and
# never cycled - a series keeps its hue when the set around it changes.
CATEGORICAL = [
    "#2a78d6",  # blue
    "#eb6834",  # orange
    "#1baf7a",  # aqua
    "#eda100",  # yellow
    "#e87ba4",  # magenta
    "#008300",  # green
    "#4a3aa7",  # violet
    "#e34948",  # red
]

# Class order must match: COMEX, TFEX, then pairs that straddle the two.
VENUE_COLOURS = CATEGORICAL[:3]
VENUE_NAMES = ["COMEX", "TFEX", "cross-venue"]

# Fixed y-axis so the cloud is comparable day to day. Widen it if implied carry
# ever leaves this band - points outside are clipped, not rescaled.
Y_AXIS = (0.0, 0.10)

# Symbol names sit on odd columns of `Data` row 1; ranges are the same width so
# FILTER lines them up. CA must exist as a real column (see simple_heatmap).
SYMS = f"{DATA}!$C$1:$BZ$1"
ALL_LEGS = f'FILTER({SYMS},ISODD(COLUMN({SYMS})),{SYMS}<>"")'

# Diverging blue <-> red on the YIELD, with a grey core rather than a single
# neutral point: anything inside +/-4% is ordinary carry and should recede, the
# ramp runs from there to its darkest at +/-8%. The red arm is luminance-matched
# to the blue arm step for step, so neither side reads as the heavier one, and
# every step keeps black text above 8:1.
NEUTRAL = "#f0efec"

# Below this many days between the two expiries the annualised yield stops
# meaning anything: dividing by a one-day gap turned a $3.80 venue difference
# between GCV6 and GOU26 into -32%/yr. Those cells drop the yield and go grey.
MIN_GAP = 7
SUPPRESSED = "#dcdbd6"  # distinctly greyer than NEUTRAL so it is not read as zero

GREY_BAND = 0.04  # |yield| below this is ordinary carry and stays neutral
FULL_COLOUR = 0.08  # and at this it is fully saturated

# Ramp anchors, palest first. Sheets can only step between them; dashboard.py
# interpolates across them for a continuous scale. Same endpoints either way.
RAMP_UP = [NEUTRAL, "#cde2fb", "#9ec5f4", "#6da7ec"]
RAMP_DOWN = [NEUTRAL, "#f9d8d8", "#f3b1b1", "#ec8786"]

BANDS = [
    ("<=-0.08", RAMP_DOWN[3]),
    ("<=-0.06", RAMP_DOWN[2]),
    ("<-0.04", RAMP_DOWN[1]),
    ("<0.04", NEUTRAL),
    ("<0.06", RAMP_UP[1]),
    ("<0.08", RAMP_UP[2]),
    (">=0.08", RAMP_UP[3]),
]


def col_letter(idx: int) -> str:
    """1-based column index -> letter (A..Z is enough for this sheet)."""
    return ascii_uppercase[idx - 1]


def rgb(hex_colour: str) -> dict:
    h = hex_colour.lstrip("#")
    return {k: int(h[i : i + 2], 16) / 255 for k, i in (("red", 0), ("green", 2), ("blue", 4))}


def parts(row: int, col: str) -> dict:
    """The spreadsheet sub-expressions a cell is built from.

    Prices and TTMs are looked up by leg NAME and by the snapshot DATE, never
    by position, so `Data` can gain columns without breaking this.
    """
    x, y = f"$A{row}", f"{col}$1"
    dr = f"MATCH($A$1,{DATA}!$A:$A,0)"
    cx, cy = f"MATCH({x},{DATA}!$1:$1,0)", f"MATCH({y},{DATA}!$1:$1,0)"
    # A symbol's own column is its BID (row 2 reads BID under the name), so ASK
    # is the column after it, and the TTM likewise sits one column along row 1.
    tx = f"INDEX({DATA}!$1:$1,1,{cx}+1)"
    ty = f"INDEX({DATA}!$1:$1,1,{cy}+1)"
    x_ask = f"INDEX({DATA}!$A:$CA,{dr},{cx}+1)"
    y_bid = f"INDEX({DATA}!$A:$CA,{dr},{cy})"
    dy = f"({ty}-$A$1)"
    # The carry window the yield is earned over is the distance between the two
    # expiries, not the time left on either leg. Spot behaves as a leg expiring
    # on the snapshot date, which makes the spot row fall out of the same rule.
    gap = f'IF({tx}="",{dy},{ty}-{tx})'
    # Never divide by a gap we are going to suppress anyway - a pair of
    # contracts sharing an expiry would otherwise raise #DIV/0! and take the
    # spread down with it.
    safe_gap = f"IF(ABS({gap})>={MIN_GAP},{gap},1)"
    return {
        "guard": f'OR({x}="",{y}="")',
        "tx": tx,
        "gap": gap,
        "spread": f"({y_bid}-{x_ask})",
        # Days shown is the NEAR leg's time left - that is what forces the
        # position to be unwound - which is not the yield's denominator.
        "days": f'IF({tx}="",{dy},MIN({tx}-$A$1,{dy}))',
        "pct": f"(({y_bid}/{x_ask})-1)*365/{safe_gap}",
    }


def text_cell(row: int, col: str) -> str:
    p = parts(row, col)
    pct = f'IF(ABS({p["gap"]})>={MIN_GAP},TEXT({p["pct"]},"0.00%")&" ","")'
    body = f'{pct}&"("&TEXT({p["spread"]},"+0.00;-0.00")&") ("&{p["days"]}&"d)"'
    return f'=IF({p["guard"]},"",IFERROR({body},""))'


def value_cell(row: int, col: str) -> str:
    """The numeric mirror: the yield, which is what the colour keys off.

    Left blank for a suppressed cell, which is what the grey rule keys off -
    no third grid needed, and the sheet stays inside its 26 columns. The chart
    block reads its y-values straight out of here rather than recomputing them.
    """
    p = parts(row, col)
    live = f'IF(ABS({p["gap"]})<{MIN_GAP},"",{p["pct"]})'
    return f'=IF({p["guard"]},"",IFERROR({live},""))'


def venue(name: str) -> str:
    """1 for an exchange-prefixed contract, 0 for a bare TFEX one.

    Tested on the CMX. prefix rather than on the presence of a dot, because
    the spot leg is XAUUSD.s and a dot test would file it as an exchange.
    """
    return f'IF(LEFT({name},4)="CMX.",1,0)'


def curve_block(front, cols: list[str]) -> None:
    """Feed the scatter: every cell of the triangle, one row per pair.

    x is the term the yield is actually earned over - the distance between the
    two expiries - which is the only x that keeps a point self-consistent with
    its own denominator. On the spot row that term is just the contract's TTM.

    Points, not lines, so nothing here needs sorting.
    """
    first = CHART_ROW + 1
    last = CHART_ROW + MAX_PAIRS
    mirror = [col_letter(2 + COLOUR_OFFSET + j) for j in range(SPAN)]

    raw = []
    for i in range(1, SPAN + 1):
        row = FIRST_ROW + i - 1
        for j in range(i, SPAN + 1):
            col = cols[j - 1]
            p = parts(row, col)
            x, y = f"$A{row}", f"{col}$1"
            # Row 1 of the triangle is the spot leg, so a pair there simply
            # takes the contract's venue; otherwise two venues that disagree
            # make the pair cross-venue.
            if i == 1:
                cls = f'IF({y}="","",{venue(y)})'
            else:
                same = f"{venue(x)}={venue(y)}"
                cls = f'IF(OR({x}="",{y}=""),"",IF({same},{venue(y)},2))'
            raw.append(
                [
                    # ABS: a negative gap only means the column leg expires
                    # first, which is an artefact of where a pair sits in the
                    # triangle. The window's length is what belongs on the axis.
                    f'=IF({p["guard"]},"",IFERROR(ABS({p["gap"]}),""))',
                    # The mirror already holds this pair's yield, blanked when
                    # the gap is too short - reuse it rather than recompute.
                    f"={mirror[j - 1]}{row}",
                    f"={cls}",
                ]
            )

    front.update(
        raw,
        range_name=f"{col_letter(RAW_COL)}{first}:{col_letter(RAW_COL + 2)}{first + len(raw) - 1}",
        value_input_option="USER_ENTERED",
    )

    q, r = col_letter(RAW_COL + 1), col_letter(RAW_COL + 2)
    split = [
        [f'=IF({r}{n}={k},{q}{n},"")' for k in (1, 0, 2)] for n in range(first, last + 1)
    ]
    front.update(
        split,
        range_name=f"{col_letter(SERIES_COL)}{first}:{col_letter(SERIES_COL + 2)}{last}",
        value_input_option="USER_ENTERED",
    )
    front.update(
        [["term", "yield", "class", *VENUE_NAMES]],
        range_name=f"{col_letter(RAW_COL)}{CHART_ROW}:{col_letter(SERIES_COL + 2)}{CHART_ROW}",
    )

    # Set every column's format explicitly: front.clear() wipes values but not
    # formats, so an inherited percent pattern turned 21 days into "2100.00%"
    # and would have carried straight onto the chart's x-axis.
    plain = {"numberFormat": {"type": "NUMBER", "pattern": "0"}}
    pct = {"numberFormat": {"type": "NUMBER", "pattern": "0.00%"}}
    for offset, fmt in (
        (RAW_COL, plain),
        (RAW_COL + 1, pct),
        (RAW_COL + 2, plain),
        (SERIES_COL, pct),
        (SERIES_COL + 1, pct),
        (SERIES_COL + 2, pct),
    ):
        front.format(f"{col_letter(offset)}{first}:{col_letter(offset)}{last}", fmt)


def add_chart(front) -> None:
    sid = front.id
    last = CHART_ROW + MAX_PAIRS

    def source(col_idx: int) -> dict:
        return {
            "sourceRange": {
                "sources": [
                    {
                        "sheetId": sid,
                        "startRowIndex": CHART_ROW - 1,
                        "endRowIndex": last,
                        "startColumnIndex": col_idx - 1,
                        "endColumnIndex": col_idx,
                    }
                ]
            }
        }

    meta = front.spreadsheet.fetch_sheet_metadata(
        {"fields": "sheets(properties/sheetId,charts/chartId)"}
    )
    old = next(
        (
            [c["chartId"] for c in s.get("charts", [])]
            for s in meta["sheets"]
            if s["properties"]["sheetId"] == sid
        ),
        [],
    )

    spec = {
        "title": "Implied carry",
        "subtitle": "every pair in the matrix, yield against the term it is earned over",
        # The mirror and its scaffolding live in hidden columns; without this the
        # chart would read them as absent and draw nothing.
        "hiddenDimensionStrategy": "SHOW_ALL",
        "basicChart": {
            "chartType": "SCATTER",
            "legendPosition": "BOTTOM_LEGEND",
            "headerCount": 1,
            "axis": [
                {"position": "BOTTOM_AXIS", "title": "Term (days between the two expiries)"},
                {
                    "position": "LEFT_AXIS",
                    "title": "Implied carry",
                    # Pinned rather than auto-fitted, for the same reason the
                    # colour bands are: the curve's height has to mean the same
                    # thing tomorrow. Left at the default the axis based itself
                    # at 0 and squeezed a 3.7-5.6% spread into the top third.
                    "viewWindowOptions": {
                        "viewWindowMin": Y_AXIS[0],
                        "viewWindowMax": Y_AXIS[1],
                    },
                },
            ],
            "domains": [{"domain": source(RAW_COL)}],
            "series": [
                {
                    "series": source(SERIES_COL + i),
                    "targetAxis": "LEFT_AXIS",
                    "color": rgb(colour),
                    # Points only. A line would have to interpolate between
                    # pairs that share nothing but an axis.
                    "lineStyle": {"width": 0, "type": "INVISIBLE"},
                    "pointStyle": {"size": 7, "shape": "CIRCLE"},
                }
                for i, colour in enumerate(VENUE_COLOURS)
            ],
        },
    }

    front.spreadsheet.batch_update(
        {
            "requests": [
                *({"deleteEmbeddedObject": {"objectId": c}} for c in old),
                {
                    "addChart": {
                        "chart": {
                            "spec": spec,
                            "position": {
                                "overlayPosition": {
                                    "anchorCell": {
                                        "sheetId": sid,
                                        "rowIndex": LAST_ROW + 1,
                                        "columnIndex": 0,
                                    },
                                    "widthPixels": 760,
                                    "heightPixels": 420,
                                }
                            },
                        }
                    }
                },
            ]
        }
    )
    logger.info("Replaced %d chart(s) with the carry curve.", len(old))


def build(front) -> None:
    front.clear()

    cols = [col_letter(2 + j) for j in range(SPAN)]
    last_col = cols[-1]

    front.update(
        [
            [f"=INDEX({DATA}!$A$3:$A,COUNTA({DATA}!$A$3:$A))"]
            + [f'=IFERROR(INDEX({ALL_LEGS},1,COLUMN()),"")' for _ in cols]
        ],
        range_name=f"A1:{last_col}1",
        value_input_option="USER_ENTERED",
    )
    # The final leg never labels a row: nothing sits after it to pair against.
    row_label = (
        f'=IFERROR(IF(ROW()-1>=COLUMNS({ALL_LEGS}),"",INDEX({ALL_LEGS},1,ROW()-1)),"")'
    )
    front.update(
        [[row_label] for _ in range(SPAN)],
        range_name=f"A{FIRST_ROW}:A{LAST_ROW}",
        value_input_option="USER_ENTERED",
    )

    # Upper triangle only: the cell at row leg i, column leg j+1 exists when
    # j >= i. Masked cells are written blank rather than left to a formula.
    text, mirror = [], []
    for i in range(1, SPAN + 1):
        row = FIRST_ROW + i - 1
        text.append(["" if j < i else text_cell(row, cols[j - 1]) for j in range(1, SPAN + 1)])
        mirror.append(["" if j < i else value_cell(row, cols[j - 1]) for j in range(1, SPAN + 1)])

    front.update(
        text,
        range_name=f"B{FIRST_ROW}:{last_col}{LAST_ROW}",
        value_input_option="USER_ENTERED",
    )
    hidden_first = col_letter(2 + COLOUR_OFFSET)
    hidden_last = col_letter(2 + COLOUR_OFFSET + SPAN - 1)
    front.update(
        mirror,
        range_name=f"{hidden_first}{FIRST_ROW}:{hidden_last}{LAST_ROW}",
        value_input_option="USER_ENTERED",
    )
    logger.info("Wrote a %dx%d triangle plus its numeric mirror.", SPAN, SPAN)

    curve_block(front, cols)
    logger.info("Wrote the term-structure chart data.")


def style(front) -> None:
    sid = front.id
    last_col = col_letter(1 + SPAN)

    front.format("A1", {"numberFormat": {"type": "DATE", "pattern": "yyyy-mm-dd"}})
    front.format(f"A1:{last_col}1", {"textFormat": {"bold": True}})
    front.format(f"A{FIRST_ROW}:A{LAST_ROW}", {"textFormat": {"bold": True}})
    front.format(
        f"B{FIRST_ROW}:{last_col}{LAST_ROW}", {"horizontalAlignment": "CENTER"}
    )
    front.freeze(rows=1, cols=1)

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

    grid = {
        "sheetId": sid,
        "startRowIndex": FIRST_ROW - 1,
        "endRowIndex": LAST_ROW,
        "startColumnIndex": 1,
        "endColumnIndex": 1 + SPAN,
    }
    mirror_ref = f"{col_letter(2 + COLOUR_OFFSET)}{FIRST_ROW}"

    visible_ref = f"B{FIRST_ROW}"
    # A written cell whose mirror is blank is a suppressed one; a cell that is
    # blank on both sides is just outside the triangle and stays unpainted.
    tests = [
        (f"=AND(NOT(ISNUMBER({mirror_ref})),{visible_ref}<>\"\")", SUPPRESSED),
        # ISNUMBER matters: a blank mirror cell holds "", and in Sheets text
        # compares as greater than any number, so ">=60" would match it.
        *(
            (f"=AND(ISNUMBER({mirror_ref}),{mirror_ref}{test})", colour)
            for test, colour in BANDS
        ),
    ]

    rules = []
    for i, (formula, colour) in enumerate(tests):
        rules.append(
            {
                "addConditionalFormatRule": {
                    "index": i,
                    "rule": {
                        "ranges": [grid],
                        "booleanRule": {
                            "condition": {
                                "type": "CUSTOM_FORMULA",
                                "values": [{"userEnteredValue": formula}],
                            },
                            "format": {"backgroundColor": rgb(colour)},
                        },
                    },
                }
            }
        )

    front.spreadsheet.batch_update(
        {
            "requests": [
                *(
                    {"deleteConditionalFormatRule": {"sheetId": sid, "index": i}}
                    for i in reversed(range(existing))
                ),
                *rules,
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sid,
                            "dimension": "COLUMNS",
                            "startIndex": 1 + COLOUR_OFFSET,
                            "endIndex": 1 + COLOUR_OFFSET + SPAN,
                        },
                        "properties": {"hiddenByUser": True},
                        "fields": "hiddenByUser",
                    }
                },
            ]
        }
    )
    logger.info("Applied %d colour rules and hid the mirror.", len(tests))


def main() -> int:
    cfg = load_config()
    setup_logger(cfg["log_file"])

    try:
        data_ws = get_worksheet(cfg)
        front = data_ws.spreadsheet.worksheet(FRONT)

        build(front)
        style(front)
        add_chart(front)

        logger.info("Front is ready.")
        return 0
    except Exception:
        logger.exception("Setup failed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
