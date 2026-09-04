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

# Symbol names sit on odd columns of `Data` row 1; ranges are the same width so
# FILTER lines them up. CA must exist as a real column (see simple_heatmap).
SYMS = f"{DATA}!$C$1:$BZ$1"
ALL_LEGS = f'FILTER({SYMS},ISODD(COLUMN({SYMS})),{SYMS}<>"")'

# Diverging blue <-> red about zero: spreads go negative whenever the leg being
# sold expires before the leg being bought. The red arm is luminance-matched to
# the blue arm step for step, so neither side reads as the heavier one, and
# every step keeps black text above 8:1.
NEUTRAL = "#f0efec"
BANDS = [
    ("<=-60", "#ec8786"),
    ("<=-20", "#f3b1b1"),
    ("<0", "#f9d8d8"),
    ("=0", NEUTRAL),
    ("<20", "#cde2fb"),
    ("<60", "#9ec5f4"),
    (">=60", "#6da7ec"),
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
    return {
        "guard": f'OR({x}="",{y}="")',
        "tx": tx,
        "spread": f"({y_bid}-{x_ask})",
        # Spot has no TTM, so for the spot row the near leg is simply Y.
        "days": f'IF({tx}="",{dy},MIN({tx}-$A$1,{dy}))',
        "pct": f"(({y_bid}/{x_ask})-1)*365/{dy}",
    }


def text_cell(row: int, col: str) -> str:
    p = parts(row, col)
    pct = f'IF({p["tx"]}="",TEXT({p["pct"]},"0.00%")&" ","")'
    body = f'{pct}&"("&TEXT({p["spread"]},"+0.00;-0.00")&") ("&{p["days"]}&"d)"'
    return f'=IF({p["guard"]},"",IFERROR({body},""))'


def value_cell(row: int, col: str) -> str:
    """The numeric mirror the conditional formatting reads."""
    p = parts(row, col)
    return f'=IF({p["guard"]},"",IFERROR({p["spread"]},""))'


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

    rules = []
    for i, (test, colour) in enumerate(BANDS):
        # ISNUMBER matters: a blank mirror cell holds "", and in Sheets text
        # compares as greater than any number, so ">=60" would match it.
        formula = f"=AND(ISNUMBER({mirror_ref}),{mirror_ref}{test})"
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
    logger.info("Applied %d diverging bands and hid the mirror.", len(BANDS))


def main() -> int:
    cfg = load_config()
    setup_logger(cfg["log_file"])

    try:
        data_ws = get_worksheet(cfg)
        front = data_ws.spreadsheet.worksheet(FRONT)

        build(front)
        style(front)

        logger.info("Front is ready.")
        return 0
    except Exception:
        logger.exception("Setup failed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
