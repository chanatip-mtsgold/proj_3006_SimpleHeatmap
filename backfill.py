"""proj_3006 — build the `Back` tab: historical 10:00 opens and spread charts.

Separate from the daily job. `Data` logs bid/ask for the contracts trading
today; this pulls the 10:00 Asia/Bangkok M1 open for the 2026 gold contracts
that have already come and gone, so the calendar spreads can be looked at over
a stretch of time rather than one snapshot.

Nine contracts across two venues:

    F.US.GCE*26   COMEX, physically delivered, two-month cycle
    GO*26         TFEX Gold Online, cash settled, quarterly

    uv run python backfill.py             # reuse the cached pull
    uv run python backfill.py --refetch   # pull from MT5 again (~10 min)

Fetching is slow and the sheet write is not, so the pull is cached; only
--refetch goes back to the terminal.
"""

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import gspread
import MetaTrader5 as mt5

from setup_front import CATEGORICAL, rgb
from simple_heatmap import (
    PROJECT_DIR,
    _load_credentials,
    load_config,
    logger,
    mt5_session,
    setup_logger,
)

CACHE = Path(PROJECT_DIR) / "backfill_cache.json"
TAB = "Back"
START = dt.date(2026, 1, 1)
SNAP_UTC = dt.time(3, 0)  # 10:00 Asia/Bangkok

# The date each contract stops being a forward, which is what the charts put
# at x = 0. The two venues reach it differently:
#
#   COMEX  first notice day, the last business day of the month before
#          delivery - the contract goes deliverable and keeps trading for
#          about another 26 days. Same convention as `Data` row 1, checked
#          against CMX.GCZ6 -> 2026-11-30.
#   TFEX   last trading day, the business day before the contract month's
#          last. Cash settled, so there is no delivery period at all.
#          GOH26/GOM26 confirmed by where their history stops, GOU26/GOZ26
#          by `Data` row 1.
EXPIRY = {
    "F.US.GCEG26": dt.date(2026, 1, 30),
    "F.US.GCEJ26": dt.date(2026, 3, 31),
    "F.US.GCEM26": dt.date(2026, 5, 29),
    "F.US.GCEQ26": dt.date(2026, 7, 31),
    "F.US.GCEZ26": dt.date(2026, 11, 30),
    "GOH26": dt.date(2026, 3, 30),
    "GOM26": dt.date(2026, 6, 29),
    "GOU26": dt.date(2026, 9, 29),
    "GOZ26": dt.date(2026, 12, 29),
}
COMEX = [s for s in EXPIRY if s.startswith("F.US.")]
TFEX = [s for s in EXPIRY if not s.startswith("F.US.")]
SYMBOLS = COMEX + TFEX

HELP_COL = 27  # AA - clear of the data and of where the charts float
CHART_COL = 11  # L


def a1(col: int) -> str:
    out = ""
    while col:
        col, rem = divmod(col - 1, 26)
        out = chr(65 + rem) + out
    return out


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #
def fetch(cfg: dict) -> dict:
    """The 10:00 open per contract per weekday, as far back as MT5 will go.

    copy_rates_from never comes back empty for these symbols: past the end of
    a symbol's history it returns the last bar it holds, and before the start
    the first, each with a price that looks entirely reasonable. Every bar is
    therefore checked against the minute that was asked for and dropped if it
    does not match - without that the sheet fills with repeated prices that
    read as real data.
    """
    end = dt.datetime.now(dt.timezone.utc).date()
    days = [
        START + dt.timedelta(days=i)
        for i in range((end - START).days + 1)
        if (START + dt.timedelta(days=i)).weekday() < 5
    ]

    rows: dict[str, dict] = {}
    with mt5_session(cfg["mt5_path"]):
        for s in SYMBOLS:
            if not mt5.symbol_select(s, True):
                logger.warning("symbol_select('%s') failed: %s", s, mt5.last_error())

        for n, day in enumerate(days, 1):
            target = dt.datetime.combine(day, SNAP_UTC, tzinfo=dt.timezone.utc)
            # copy_rates_from reads a naive datetime as machine-local and
            # converts, so hand it local wall-clock rather than UTC.
            naive = target.astimezone().replace(tzinfo=None)
            row = {}
            for s in SYMBOLS:
                r = mt5.copy_rates_from(s, mt5.TIMEFRAME_M1, naive, 1)
                if r is None or not len(r):
                    continue
                if dt.datetime.fromtimestamp(int(r[0]["time"]), tz=dt.timezone.utc) != target:
                    continue
                row[s] = round(float(r[0]["open"]), 2)
            if row:
                rows[day.isoformat()] = row
            if n % 40 == 0:
                logger.info("  %d/%d weekdays, %d with data", n, len(days), len(rows))

    CACHE.write_text(json.dumps({"rows": rows}, indent=1), encoding="utf-8")
    logger.info("Cached %d rows to %s.", len(rows), CACHE.name)
    return rows


# --------------------------------------------------------------------------- #
# Pairs
# --------------------------------------------------------------------------- #
def pair_series(rows: dict) -> dict:
    """{label: {x: (spread, yield)}} for each adjacent pair inside a venue."""
    out = {}
    for legs in (COMEX, TFEX):
        for near, far in zip(legs, legs[1:]):
            point = {}
            for key in sorted(rows):
                row = rows[key]
                if near not in row or far not in row:
                    continue
                day = dt.date.fromisoformat(key)
                # Past the near leg's own expiry the pair is no longer a
                # calendar spread - the near leg is deliverable, so what is
                # left is a basis against the far leg and the window carry is
                # earned over shrinks with the far leg's remaining life. Held
                # at the fixed gap instead, a flat 4.4% market reads as a
                # collapse to 2.4%.
                window = (EXPIRY[far] - max(day, EXPIRY[near])).days
                if window <= 0:
                    continue
                spread = row[far] - row[near]
                point[(day - EXPIRY[near]).days] = (
                    round(spread, 2),
                    round(spread / row[near] * 365 / window * 100, 3),
                )
            label = f"{far.replace('F.US.GCE', '')} - {near.replace('F.US.GCE', '')}"
            out[label] = point
    return out


# --------------------------------------------------------------------------- #
# Sheet
# --------------------------------------------------------------------------- #
def write_tab(ws, rows: dict, series: dict) -> tuple[int, int]:
    names = list(series)
    need = HELP_COL + 2 * len(names)
    if ws.col_count < need:
        ws.add_cols(need - ws.col_count)
    ws.clear()

    table = [["Date", *SYMBOLS]]
    for day in sorted(rows):
        table.append([day, *(rows[day].get(s, "") for s in SYMBOLS)])
    ws.update(
        table,
        range_name=f"A1:{a1(1 + len(SYMBOLS))}{len(table)}",
        value_input_option="USER_ENTERED",
    )

    # One contiguous block of rows per series rather than a shared x column.
    # A line only joins consecutive non-blank rows, and on a common axis every
    # series is full of holes where the other pairs have points, so sharing
    # rows left all seven lines in fragments. Grouping gives each an unbroken
    # run; the charts are scatters, so points still land on their own x
    # whatever order the rows come in.
    grid = [["x", *(f"{n} pts" for n in names), *(f"{n} %" for n in names)]]
    for i, name in enumerate(names):
        for x in sorted(series[name]):
            pts, pct = series[name][x]
            grid.append(
                [x]
                + ["" if j != i else pts for j in range(len(names))]
                + ["" if j != i else pct for j in range(len(names))]
            )
    ws.update(
        grid,
        range_name=f"{a1(HELP_COL)}1:{a1(HELP_COL + 2 * len(names))}{len(grid)}",
        value_input_option="USER_ENTERED",
    )

    ws.format(f"A1:{a1(1 + len(SYMBOLS))}1", {"textFormat": {"bold": True}})
    ws.format(f"A2:A{len(table)}", {"numberFormat": {"type": "DATE", "pattern": "yyyy-mm-dd"}})
    ws.format(
        f"B2:{a1(1 + len(SYMBOLS))}{len(table)}",
        {"numberFormat": {"type": "NUMBER", "pattern": "0.00"}},
    )
    ws.freeze(rows=1, cols=1)
    return len(table) - 1, len(grid)


def add_charts(sh, ws, names: list, grid_rows: int) -> int:
    sid = ws.id

    def source(col: int) -> dict:
        return {
            "sourceRange": {
                "sources": [
                    {
                        "sheetId": sid,
                        "startRowIndex": 0,
                        "endRowIndex": grid_rows,
                        "startColumnIndex": col - 1,
                        "endColumnIndex": col,
                    }
                ]
            }
        }

    def chart(title, y_title, offset, anchor_row, y_range=None) -> dict:
        left = {"position": "LEFT_AXIS", "title": y_title}
        if y_range:
            left["viewWindowOptions"] = {
                "viewWindowMin": y_range[0],
                "viewWindowMax": y_range[1],
            }
        return {
            "addChart": {
                "chart": {
                    "spec": {
                        "title": title,
                        "subtitle": "x=0 is the near leg's first notice (COMEX) "
                        "or last trading day (TFEX)",
                        # The series columns are hidden; without this the chart
                        # reads them as absent and draws nothing.
                        "hiddenDimensionStrategy": "SHOW_ALL",
                        "basicChart": {
                            # SCATTER, not LINE: LINE spaces the domain evenly
                            # and these x values are not evenly spaced.
                            "chartType": "SCATTER",
                            "legendPosition": "BOTTOM_LEGEND",
                            "headerCount": 1,
                            "axis": [
                                {
                                    "position": "BOTTOM_AXIS",
                                    "title": "Days from the near leg leaving forward",
                                },
                                left,
                            ],
                            "domains": [{"domain": source(HELP_COL)}],
                            "series": [
                                {
                                    "series": source(HELP_COL + offset + i),
                                    "targetAxis": "LEFT_AXIS",
                                    "color": rgb(CATEGORICAL[i % len(CATEGORICAL)]),
                                    "lineStyle": {"width": 2, "type": "SOLID"},
                                    "pointStyle": {"size": 3, "shape": "CIRCLE"},
                                }
                                for i in range(len(names))
                            ],
                        },
                    },
                    "position": {
                        "overlayPosition": {
                            "anchorCell": {
                                "sheetId": sid,
                                "rowIndex": anchor_row,
                                "columnIndex": CHART_COL,
                            },
                            "widthPixels": 900,
                            "heightPixels": 440,
                        }
                    },
                }
            }
        }

    meta = sh.fetch_sheet_metadata({"fields": "sheets(properties/sheetId,charts/chartId)"})
    old = next(
        (
            [c["chartId"] for c in s.get("charts", [])]
            for s in meta["sheets"]
            if s["properties"]["sheetId"] == sid
        ),
        [],
    )
    sh.batch_update(
        {
            "requests": [
                *({"deleteEmbeddedObject": {"objectId": c}} for c in old),
                chart("Calendar spread", "points", 1, 1),
                chart("Implied carry", "%/yr", 1 + len(names), 23, (0, 8)),
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sid,
                            "dimension": "COLUMNS",
                            "startIndex": HELP_COL - 1,
                            "endIndex": HELP_COL + 2 * len(names),
                        },
                        "properties": {"hiddenByUser": True},
                        "fields": "hiddenByUser",
                    }
                },
            ]
        }
    )
    return len(old)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Back tab.")
    parser.add_argument(
        "--refetch",
        action="store_true",
        help="Pull the history from MT5 again instead of using the cache.",
    )
    args = parser.parse_args()

    cfg = load_config()
    setup_logger(cfg["log_file"])
    try:
        if args.refetch or not CACHE.exists():
            rows = fetch(cfg)
        else:
            rows = json.loads(CACHE.read_text(encoding="utf-8"))["rows"]
            logger.info("Using %d cached rows (--refetch to pull again).", len(rows))

        series = pair_series(rows)
        sh = gspread.authorize(_load_credentials()).open_by_key(cfg["spreadsheet_id"])
        ws = sh.worksheet(TAB)

        dates, grid_rows = write_tab(ws, rows, series)
        replaced = add_charts(sh, ws, list(series), grid_rows)

        logger.info("%d dates x %d contracts.", dates, len(SYMBOLS))
        logger.info("%d pairs charted, %d old chart(s) replaced.", len(series), replaced)
        for name, pts in series.items():
            xs = sorted(pts)
            logger.info("  %-15s %3d pts   x %4d .. %-4d", name, len(xs), xs[0], xs[-1])
        return 0
    except Exception:
        logger.exception("Backfill failed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
