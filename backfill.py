"""proj_3006 — build the `Back` tab: historical 10:00 opens and spread charts.

Separate from the daily job. `Data` logs bid/ask for the contracts trading
today; this pulls the 10:00 Asia/Bangkok M1 open for the 2026 gold contracts
that have already come and gone, so the calendar spreads can be looked at over
a stretch of time rather than one snapshot.

Twelve contracts across two venues:

    F.US.GCE*26   COMEX 2026, physically delivered, two-month cycle
    CMX.GC*7      COMEX 2027 - the F.US.GCE family stops at Z26
    GO*26, GOH27  TFEX Gold Online, cash settled, quarterly

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

from setup_front import CATEGORICAL, MIN_GAP, rgb
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
    # The broker's F.US.GCE family has nothing past Z26, so 2027 comes from
    # the CMX family the daily job uses. Same exchange contract under another
    # name: both quote 4462.6 for Z26 at 10:00 on 2026-09-10.
    "CMX.GCG7": dt.date(2027, 1, 29),
    "CMX.GCJ27": dt.date(2027, 3, 31),
    "GOH27": dt.date(2027, 3, 30),
}


def is_comex(symbol: str) -> bool:
    return symbol.startswith(("F.US.", "CMX."))


# Expiry order within each venue, since adjacent pairs are taken in list order
# and the two families would otherwise interleave by when they were added.
COMEX = sorted((s for s in EXPIRY if is_comex(s)), key=EXPIRY.get)
TFEX = sorted((s for s in EXPIRY if not is_comex(s)), key=EXPIRY.get)
SYMBOLS = COMEX + TFEX

# Second name to try when the first has no bar at the minute asked for. The
# two families each have their own holes - on 2026-10-05 F.US.GCEZ26 had the
# 10:00 bar and CMX.GCZ6 stopped at 09:23, on 2026-08-03 the other way round -
# so one contract read through both is more complete than either alone.
ALIASES = {"F.US.GCEZ26": "CMX.GCZ6"}


def short(symbol: str) -> str:
    """Month code plus two-digit year whichever family it is: Z26, G27, H27."""
    name = symbol.replace("F.US.GCE", "").replace("CMX.GC", "")
    return name[0] + "2" + name[1:] if len(name) == 2 else name

# The chart series live on their own hidden tab, not in hidden columns of
# Back. Twice a column was inserted or deleted on Back by hand after a
# rebuild; the series block shifted with it and both charts came up empty.
# Nothing done to Back's columns can reach a separate tab.
SERIES_TAB = "Back_series"

# One blank column after the price table. Derived, not fixed: at a fixed L the
# charts sat on top of GOZ26 and GOH27 once the table grew to twelve contracts.
CHART_COL = len(SYMBOLS) + 2  # 0-based anchor index


# Darkest first. Every shade clears 3:1 against the white chart background,
# the floor for a 2px line - the lightest blue (#3f8ee6) is 3.4:1 and the
# lightest yellow (#c48400) 3.2:1; anything paler disappears. The yellows are
# amber rather than true yellow for the same reason.
COMEX_SHADES = ["#0b2e66", "#123f80", "#1a539f", "#2368be", "#2a78d6", "#3f8ee6"]
TFEX_SHADES = ["#6b4700", "#8a5c00", "#a86f00", "#c48400"]


# Cross-market pairs take the palette's other hues, one each, so none of them
# reads as belonging to either venue: orange, aqua, magenta, green, violet,
# red - the validated slots that are neither blue nor yellow.
CROSS_COLOURS = [CATEGORICAL[i] for i in (1, 2, 4, 5, 6, 7)]


def venue_shades(ramp: list[str], n: int) -> list[str]:
    """n shades, oldest pair palest and the newest darkest - the pair being
    watched now is the one that should stand out. Past the ramp's length the
    oldest pairs share its palest shade rather than wrapping round."""
    return [ramp[min(n - 1 - i, len(ramp) - 1)] for i in range(n)]


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
        for s in [*SYMBOLS, *ALIASES.values()]:
            if not mt5.symbol_select(s, True):
                logger.warning("symbol_select('%s') failed: %s", s, mt5.last_error())

        for n, day in enumerate(days, 1):
            target = dt.datetime.combine(day, SNAP_UTC, tzinfo=dt.timezone.utc)
            # copy_rates_from reads a naive datetime as machine-local and
            # converts, so hand it local wall-clock rather than UTC.
            naive = target.astimezone().replace(tzinfo=None)
            row = {}
            for s in SYMBOLS:
                for source in (s, ALIASES.get(s)):
                    if source is None:
                        continue
                    r = mt5.copy_rates_from(source, mt5.TIMEFRAME_M1, naive, 1)
                    if r is None or not len(r):
                        continue
                    if dt.datetime.fromtimestamp(int(r[0]["time"]), tz=dt.timezone.utc) != target:
                        continue
                    row[s] = round(float(r[0]["open"]), 2)
                    break
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
def pair_points(rows: dict, near: str, far: str) -> dict:
    """{x: (spread, yield)} for long `near`, short `far`."""
    point = {}
    for key in sorted(rows):
        row = rows[key]
        if near not in row or far not in row:
            continue
        day = dt.date.fromisoformat(key)
        # Past the near leg's own expiry the pair is no longer a calendar
        # spread - the near leg is deliverable, so what is left is a basis
        # against the far leg and the window carry is earned over shrinks with
        # the far leg's remaining life. Held at the fixed gap instead, a flat
        # 4.4% market reads as a collapse to 2.4%.
        window = (EXPIRY[far] - max(day, EXPIRY[near])).days
        if window <= 0:
            continue
        spread = row[far] - row[near]
        point[(day - EXPIRY[near]).days] = (
            round(spread, 2),
            round(spread / row[near] * 365 / window * 100, 3),
        )
    return point


def pair_series(rows: dict) -> dict:
    """{label: {x: (spread, yield)}} for each adjacent pair inside a venue."""
    return {
        f"{short(far)} - {short(near)}": pair_points(rows, near, far)
        for legs in (COMEX, TFEX)
        for near, far in zip(legs, legs[1:])
    }


def cross_pairs() -> list[tuple[str, str]]:
    """Each TFEX contract against the last COMEX one expiring MIN_GAP or more
    before it - long COMEX near, short TFEX far, the same way round for all.

    Not the nearest expiry: GOH26/J26 and GOH27/J27 expire a day apart, and a
    day's gap turns a few points of venue premium into a meaningless annual
    rate - the reason Front blanks the yield under the same seven days.
    """
    out = []
    for far in TFEX:
        cutoff = EXPIRY[far] - dt.timedelta(days=MIN_GAP)
        before = [c for c in COMEX if EXPIRY[c] <= cutoff]
        if before:
            out.append((max(before, key=EXPIRY.get), far))
    return out


def cross_series(rows: dict) -> dict:
    return {
        f"{short(far)} - {short(near)}": pair_points(rows, near, far)
        for near, far in cross_pairs()
    }


# --------------------------------------------------------------------------- #
# Sheet
# --------------------------------------------------------------------------- #
def series_tab(sh, cols: int):
    """The hidden tab the charts read from, created on first use."""
    try:
        ws = sh.worksheet(SERIES_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(SERIES_TAB, rows=1000, cols=cols)
    if ws.col_count < cols:
        ws.add_cols(cols - ws.col_count)
    sh.batch_update({"requests": [{
        "updateSheetProperties": {
            "properties": {"sheetId": ws.id, "hidden": True},
            "fields": "hidden",
        }
    }]})
    return ws


def write_tab(ws, data_ws, rows: dict, series: dict) -> tuple[int, int]:
    names = list(series)
    ws.clear()
    data_ws.clear()
    # Columns left hidden by the old layout (series block on Back itself), or
    # hidden by hand since, would otherwise survive the rebuild.
    ws.spreadsheet.batch_update({"requests": [{
        "updateDimensionProperties": {
            "range": {"sheetId": ws.id, "dimension": "COLUMNS",
                      "startIndex": 0, "endIndex": ws.col_count},
            "properties": {"hiddenByUser": False},
            "fields": "hiddenByUser",
        }
    }]})

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
    if data_ws.row_count < len(grid):
        data_ws.add_rows(len(grid) - data_ws.row_count)
    data_ws.update(
        grid,
        range_name=f"A1:{a1(1 + 2 * len(names))}{len(grid)}",
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


def add_charts(sh, ws, data_ws, names: list, n_cross: int, grid_rows: int) -> int:
    sid = ws.id
    data_sid = data_ws.id

    cross_subtitle = (
        "Long the COMEX leg, short the TFEX leg expiring after it. "
        "x=0 is the COMEX leg's first notice"
    )

    def source(col: int) -> dict:
        return {
            "sourceRange": {
                "sources": [
                    {
                        "sheetId": data_sid,
                        "startRowIndex": 0,
                        "endRowIndex": grid_rows,
                        "startColumnIndex": col - 1,
                        "endColumnIndex": col,
                    }
                ]
            }
        }

    # One hue per venue, so the market reads at a glance, and a shade per pair
    # inside it, so the lines that overlap near x = 0 can still be told apart.
    # pair_series emits COMEX pairs first, then TFEX, each oldest first.
    n_comex = len(COMEX) - 1
    n_within = len(names) - n_cross
    colours = (
        venue_shades(COMEX_SHADES, n_comex)
        + venue_shades(TFEX_SHADES, n_within - n_comex)
        + [CROSS_COLOURS[i % len(CROSS_COLOURS)] for i in range(n_cross)]
    )
    within, cross = list(range(n_within)), list(range(n_within, len(names)))
    pts, pct = 1, 1 + len(names)  # where each metric's block of columns starts

    def chart(title, y_title, offset, anchor_row, y_range=None, idx=within,
              subtitle="x=0 is the near leg's first notice (COMEX) or last "
                       "trading day (TFEX). Blue COMEX, yellow TFEX") -> dict:
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
                        "subtitle": subtitle,
                        # The series sit on a hidden tab; without this the
                        # chart reads them as absent and draws nothing.
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
                            "domains": [{"domain": source(1)}],
                            "series": [
                                {
                                    "series": source(1 + offset + i),
                                    "targetAxis": "LEFT_AXIS",
                                    "color": rgb(colours[i]),
                                    "lineStyle": {"width": 2, "type": "SOLID"},
                                    "pointStyle": {"size": 3, "shape": "CIRCLE"},
                                }
                                for i in idx
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
                chart("Calendar spread", "points", pts, 1),
                chart("Implied carry", "%/yr", pct, 23, (0, 8)),
                chart("Cross-market spread", "points", pts, 45,
                      idx=cross, subtitle=cross_subtitle),
                chart("Cross-market implied carry", "%/yr", pct, 67, (0, 12),
                      idx=cross, subtitle=cross_subtitle),
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

        cross = cross_series(rows)
        series = {**pair_series(rows), **cross}
        sh = gspread.authorize(_load_credentials()).open_by_key(cfg["spreadsheet_id"])
        ws = sh.worksheet(TAB)

        data_ws = series_tab(sh, 1 + 2 * len(series))

        dates, grid_rows = write_tab(ws, data_ws, rows, series)
        replaced = add_charts(sh, ws, data_ws, list(series), len(cross), grid_rows)

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
