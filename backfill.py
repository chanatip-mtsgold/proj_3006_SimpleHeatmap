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


def darken(colour: str, by: float = 0.4) -> str:
    """The same hue pulled toward black - a second shade that still reads as
    the same family, and only gains contrast against the white background."""
    return "#%02x%02x%02x" % tuple(
        round(int(colour[i : i + 2], 16) * (1 - by)) for i in (1, 3, 5)
    )


def cross_colours(names: list[str], carry: set[str]) -> list[str]:
    """One hue per TFEX contract, so every line on the same TFEX leg reads as
    one family: its carry pair in the base hue, its spread-only pairs
    (same-expiry or named extras) in a darker shade of it."""
    tfex = [short(t) for t in TFEX]
    out = []
    for name in names:
        base = CROSS_COLOURS[tfex.index(name.split(" - ")[0]) % len(CROSS_COLOURS)]
        out.append(base if name in carry else darken(base))
    return out


def tfex_order(names: list[str], carry: set[str]) -> list[str]:
    """Group the cross pairs by TFEX contract, carry pair first, so the legend
    reads in the same families as the colours."""
    tfex = [short(t) for t in TFEX]
    return sorted(names, key=lambda n: (tfex.index(n.split(" - ")[0]), n not in carry, n))


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
    """{label: {date: (spread, yield)}}, only while the pair is still a pair.

    Cut at the COMEX leg's first notice. Past it the COMEX leg is deliverable
    and the TFEX leg trades on to its own expiry a month or two later, so what
    is left is a basis over a shrinking window - GOM26-M26 climbed to 11% on
    that tail without the market having changed.
    """
    out = {}
    for near, far in cross_pairs():
        points = pair_points(rows, near, far)
        out[f"{short(far)} - {short(near)}"] = {
            (EXPIRY[near] + dt.timedelta(days=x)).isoformat(): v
            for x, v in points.items()
            if x <= 0
        }
    return out


def matched_pairs() -> list[tuple[str, str]]:
    """(COMEX, TFEX) pairs expiring inside MIN_GAP of each other - GOH26/J26
    and GOH27/J27, a day apart. cross_pairs leaves them out because a day's
    gap cannot be annualized; here they are kept for the spread alone."""
    return [
        (c, t)
        for t in TFEX
        for c in COMEX
        if abs((EXPIRY[c] - EXPIRY[t]).days) < MIN_GAP
    ]


def tfex_minus_comex(rows: dict, comex: str, tfex: str, first: dt.date,
                     last: dt.date) -> dict:
    """{date: (TFEX - COMEX, "")} on the days both legs have a price. The
    yield is left for the caller: most spread-only pairs have no window worth
    annualizing over."""
    return {
        key: (round(row[tfex] - row[comex], 2), "")
        for key, row in sorted(rows.items())
        if tfex in row and comex in row
        and first <= dt.date.fromisoformat(key) <= last
    }


def matched_series(rows: dict) -> dict:
    """{label: {date: (TFEX - COMEX, "")}} for each matched pair.

    With the expiries a day apart the carry on the two legs all but cancels,
    so the spread is close to pure venue premium - the cleanest read of how
    far TFEX sits above COMEX. TFEX minus COMEX, the same sign as every other
    cross pair, and stopped at the earlier expiry for the same reason they
    are. The yield is left blank: there is no window to annualize over.
    """
    return {
        f"{short(tfex)} - {short(comex)}": tfex_minus_comex(
            rows, comex, tfex, dt.date.min, min(EXPIRY[comex], EXPIRY[tfex]))
        for comex, tfex in matched_pairs()
    }


# Spread-only cross pairs asked for by name, each over a fixed window. These
# have the TFEX leg expiring first - the reverse of cross_pairs - so they are
# not annualized and appear on the spread chart only.
#   GOM26 - Q26   the June contract against August, from the first day Q26
#                 has a price to the end of June (GOM26 last traded 29 Jun).
#   GOU26 - Z26   September against December, over August and September
#                 (GOU26 last traded 29 Sep).
#
# The last field puts a pair on the carry chart too. GOU26 - Z26 qualifies:
# 62 days between GOU26's last trade and Z26's first notice is a window that
# annualizes cleanly. Its yield is the carry of long GOU26, short Z26 - near
# leg long, far leg short, like every other line on that chart - so it is
# positive there while its TFEX - COMEX spread is negative.
EXTRA_SPREADS = [
    ("F.US.GCEQ26", "GOM26", dt.date(2026, 5, 28), dt.date(2026, 6, 30), False),
    ("F.US.GCEZ26", "GOU26", dt.date(2026, 8, 1), dt.date(2026, 9, 30), True),
]


def extra_series(rows: dict) -> dict:
    out = {}
    for comex, tfex, first, last, annualize in EXTRA_SPREADS:
        end = min(last, EXPIRY[comex], EXPIRY[tfex])
        spread = tfex_minus_comex(rows, comex, tfex, first, end)
        if annualize:
            # TFEX expires first here, so it is the near leg.
            carry = {
                (EXPIRY[tfex] + dt.timedelta(days=x)).isoformat(): pct
                for x, (_, pct) in pair_points(rows, tfex, comex).items()
            }
            spread = {d: (pts, carry.get(d, "")) for d, (pts, _) in spread.items()}
        out[f"{short(tfex)} - {short(comex)}"] = spread
    return out


def annualized_extras() -> set[str]:
    return {f"{short(t)} - {short(c)}" for c, t, *_, annualize in EXTRA_SPREADS if annualize}


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


def cross_col(n_within: int) -> int:
    """1-based first column of the cross block: after x, pts and % of the venue
    pairs, plus one blank column between the two blocks."""
    return 1 + (1 + 2 * n_within) + 1


def grid_block(key: str, series: dict) -> list[list]:
    """Domain column, then each series' points, then each series' yields.

    One contiguous run of rows per series rather than shared rows: a line only
    joins consecutive non-blank cells, and sharing rows left every line in
    fragments wherever another pair had a point. The charts are scatters, so
    points still land on their own x whatever order the rows come in.
    """
    names = list(series)
    grid = [[key, *(f"{n} pts" for n in names), *(f"{n} %" for n in names)]]
    for i, name in enumerate(names):
        for x in sorted(series[name]):
            pts, pct = series[name][x]
            grid.append(
                [x]
                + ["" if j != i else pts for j in range(len(names))]
                + ["" if j != i else pct for j in range(len(names))]
            )
    return grid


def write_tab(ws, data_ws, rows: dict, within: dict, cross: dict) -> tuple[int, int, int]:
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

    # Two blocks side by side, each with its own domain column: the venue
    # charts run on days-from-expiry (about -210..30) and the cross-market ones
    # on calendar dates (serials near 46000). In one shared column every chart
    # would stretch its axis across both ranges.
    left = grid_block("x", within)
    right = grid_block("Date", cross)
    blocks = ((1, left), (cross_col(len(within)), right))
    rows_needed = max(len(left), len(right))
    if data_ws.row_count < rows_needed:
        data_ws.add_rows(rows_needed - data_ws.row_count)
    for col, block in blocks:
        width = len(block[0])
        data_ws.update(
            block,
            range_name=f"{a1(col)}1:{a1(col + width - 1)}{len(block)}",
            value_input_option="USER_ENTERED",
        )
    # A date domain left as a bare serial puts 46,000 on the axis labels.
    c = cross_col(len(within))
    data_ws.format(f"{a1(c)}2:{a1(c)}{len(right)}",
                   {"numberFormat": {"type": "DATE", "pattern": "d mmm"}})

    ws.format(f"A1:{a1(1 + len(SYMBOLS))}1", {"textFormat": {"bold": True}})
    ws.format(f"A2:A{len(table)}", {"numberFormat": {"type": "DATE", "pattern": "yyyy-mm-dd"}})
    ws.format(
        f"B2:{a1(1 + len(SYMBOLS))}{len(table)}",
        {"numberFormat": {"type": "NUMBER", "pattern": "0.00"}},
    )
    ws.freeze(rows=1, cols=1)
    return len(table) - 1, len(left), len(right)


def add_charts(sh, ws, data_ws, within: list, cross: list, carry: set,
               has_yield: set, within_rows: int, cross_rows: int) -> int:
    sid = ws.id
    data_sid = data_ws.id

    def source(col: int, rows: int) -> dict:
        return {
            "sourceRange": {
                "sources": [
                    {
                        "sheetId": data_sid,
                        "startRowIndex": 0,
                        "endRowIndex": rows,
                        "startColumnIndex": col - 1,
                        "endColumnIndex": col,
                    }
                ]
            }
        }

    # One hue per venue, so the market reads at a glance, and a shade per pair
    # inside it, so the lines that overlap near x = 0 can still be told apart.
    # pair_series emits COMEX pairs first, then TFEX, each oldest first. Cross
    # pairs take the other hues, so none reads as belonging to either venue.
    n_comex = len(COMEX) - 1
    venue = {
        "start": 1, "rows": within_rows, "n": len(within),
        "colours": venue_shades(COMEX_SHADES, n_comex)
        + venue_shades(TFEX_SHADES, len(within) - n_comex),
        "x_title": "Days from the near leg leaving forward",
        "subtitle": "x=0 is the near leg's first notice (COMEX) or last "
                    "trading day (TFEX). Blue COMEX, yellow TFEX",
    }
    market = {
        "start": cross_col(len(within)), "rows": cross_rows, "n": len(cross),
        "colours": cross_colours(cross, carry),
        # Only the carry pairs have a yield, so the carry chart draws those and
        # leaves out the rest instead of listing them as empty series.
        # `carry` decides the shade (base vs dark); `has_yield` decides which
        # lines the carry chart draws - an annualized extra keeps its dark
        # shade so it is not mistaken for its contract's main carry pair.
        "pct_idx": [i for i, n in enumerate(cross) if n in has_yield],
        "x_title": "Date",
        "subtitle": "TFEX minus COMEX, one colour per TFEX contract. Darker: "
                    "spread-only pairs (same expiry, or TFEX leg expiring first)",
    }

    def chart(title, y_title, block, metric, anchor_row, y_range=None) -> dict:
        """metric 0 = points, 1 = %/yr - which half of the block to read."""
        left = {"position": "LEFT_AXIS", "title": y_title}
        if y_range:
            left["viewWindowOptions"] = {
                "viewWindowMin": y_range[0],
                "viewWindowMax": y_range[1],
            }
        first = block["start"] + 1 + metric * block["n"]
        return {
            "addChart": {
                "chart": {
                    "spec": {
                        "title": title,
                        "subtitle": block["subtitle"],
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
                                {"position": "BOTTOM_AXIS", "title": block["x_title"]},
                                left,
                            ],
                            "domains": [{"domain": source(block["start"], block["rows"])}],
                            "series": [
                                {
                                    "series": source(first + i, block["rows"]),
                                    "targetAxis": "LEFT_AXIS",
                                    "color": rgb(block["colours"][i]),
                                    "lineStyle": {"width": 2, "type": "SOLID"},
                                    "pointStyle": {"size": 3, "shape": "CIRCLE"},
                                }
                                for i in (block.get("pct_idx", range(block["n"]))
                                          if metric else range(block["n"]))
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
                chart("Calendar spread", "points", venue, 0, 1),
                chart("Implied carry", "%/yr", venue, 1, 23, (0, 8)),
                chart("Cross-market spread", "points", market, 0, 45),
                chart("Cross-market implied carry", "%/yr",
                      {**market, "subtitle": "Long the near leg, short the far leg. "
                       "Each pair ends when its near leg stops being a forward"},
                      1, 67, (0, 12)),
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

        within = pair_series(rows)
        carry_pairs = cross_series(rows)
        spread_only = {**extra_series(rows), **matched_series(rows)}
        merged = {**carry_pairs, **spread_only}
        cross = {n: merged[n] for n in tfex_order(list(merged), set(carry_pairs))}
        series = {**within, **cross}
        sh = gspread.authorize(_load_credentials()).open_by_key(cfg["spreadsheet_id"])
        ws = sh.worksheet(TAB)

        data_ws = series_tab(sh, cross_col(len(within)) + 2 * len(cross))

        dates, within_rows, cross_rows = write_tab(ws, data_ws, rows, within, cross)
        replaced = add_charts(sh, ws, data_ws, list(within), list(cross),
                              set(carry_pairs), set(carry_pairs) | annualized_extras(),
                              within_rows, cross_rows)

        logger.info("%d dates x %d contracts.", dates, len(SYMBOLS))
        logger.info("%d pairs charted, %d old chart(s) replaced.", len(series), replaced)
        # Venue pairs are keyed by days from expiry, cross pairs by date.
        for name, pts in series.items():
            xs = sorted(pts)
            span = f"{xs[0]} .. {xs[-1]}" if xs else "no points"
            logger.info("  %-15s %3d pts   %s", name, len(xs), span)
        return 0
    except Exception:
        logger.exception("Backfill failed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
