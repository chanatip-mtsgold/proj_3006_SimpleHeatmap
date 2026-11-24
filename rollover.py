"""proj_3006 — COMEX gold rollover cost matrix, from Airtable to Google Chat.

Pipeline (one function each, in this order):

    1. read      fetch_contracts / fetch_latest_quotes   Airtable -> Contract, Quote
    2. choose    pick_near                               which leg the book holds now
    3. compute   make_roll / build_matrix / cheapest     pure maths, no I/O
    4. draw      render                                  matrix -> PNG
    5. publish   main                                    PNG -> Chat, once per snapshot

Every cell is one roll, row leg (near) into column leg (far), for a long book:
sell the near at its bid, buy the far at its ask.

    spread = far ask − near bid
    yield% = spread / near bid × 360 / (far expiry − near expiry)

The divisor is the gap between the expiries: the spread pays the carry for that
stretch only. The faint line under each cell is the spread that would cost
exactly the recommend yield (`roll_hurdle_pct` in config.json).

    uv run python rollover.py                    # post to rollover_space_key (once per snapshot)
    uv run python rollover.py --space TestSpace  # post elsewhere; not recorded as sent
    uv run python rollover.py --out x.png        # render only, post nothing
    uv run python rollover.py --from G27         # pretend the book is in G27
    uv run python rollover.py --dry-run          # log the near leg's rolls only
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")  # no display, and no window under pythonw

import matplotlib.pyplot as plt  # noqa: E402
import requests  # noqa: E402
from dotenv import dotenv_values  # noqa: E402
from matplotlib.colors import to_rgb  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

import chat  # noqa: E402
from simple_heatmap import (  # noqa: E402
    GITHUB_DIR,
    HTTP_TIMEOUT,
    PROJECT_DIR,
    load_config,
    logger,
    setup_logger,
)

# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
BASE_ID = "appXoUtj6dYJesMRG"
CONTRACTS_TABLE = "tblpNjJNiZiZgMHiN"
QUOTES_TABLE = "tblvLVz9OZr3y9yPA"
API = f"https://api.airtable.com/v0/{BASE_ID}"

# A file of its own beside GitToken.env, not in Path.env: the token is a
# secret, Path.env is plain machine settings.
TOKEN_ENV = os.path.join(GITHUB_DIR, ".env", "Airtable.env")
TOKEN_KEY = "AIRTABLE_TOKEN"

LOG_FILE = "rollover.log"
STATE_FILE = os.path.join(PROJECT_DIR, "logs", "rollover_last.json")
PNG_FILE = os.path.join(PROJECT_DIR, "logs", "rollover.png")  # overwritten each run

DEFAULT_HURDLE_PCT = 4.80
DEFAULT_SPACE_KEY = "TestSpace"
DAY_COUNT = 360
BKK = ZoneInfo("Asia/Bangkok")
MONTH_CODES = "FGHJKMNQUVXZ"  # Jan..Dec


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Contract:
    symbol: str  # "Z26"
    expiry: date
    active: bool


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    stale: bool


@dataclass(frozen=True)
class Roll:
    """Sell `near` at its bid, buy `far` at its ask."""

    near: str
    far: str
    near_bid: float | None
    far_ask: float | None
    days: int  # far expiry − near expiry
    stale: bool

    @property
    def spread(self) -> float | None:
        if self.near_bid is None or self.far_ask is None:
            return None
        return self.far_ask - self.near_bid

    @property
    def yield_pct(self) -> float | None:
        if self.spread is None or self.days <= 0:
            return None
        return self.spread / self.near_bid * DAY_COUNT / self.days * 100

    def spread_at(self, yield_pct: float) -> float | None:
        """The spread that costs exactly `yield_pct` — the level to bid the roll at."""
        if self.near_bid is None or self.days <= 0:
            return None
        return self.near_bid * yield_pct / 100 * self.days / DAY_COUNT


# --------------------------------------------------------------------------- #
# 1. Read — Airtable
# --------------------------------------------------------------------------- #
def load_token() -> str:
    if not os.path.exists(TOKEN_ENV):
        raise RuntimeError(f"{TOKEN_ENV} not found")
    token = (dotenv_values(TOKEN_ENV).get(TOKEN_KEY) or "").strip()
    if not token:
        # Also accept the file holding nothing but the token itself.
        with open(TOKEN_ENV, encoding="utf-8") as fh:
            bare = fh.read().strip()
        if bare.startswith("pat") and "=" not in bare and "\n" not in bare:
            token = bare
    if not token:
        raise RuntimeError(f"{TOKEN_KEY} not found in {TOKEN_ENV}")
    return token


def _records(token: str, table: str, params: list[tuple[str, str]],
             all_pages: bool) -> list[dict]:
    """Record fields, plus `_created` (Airtable's createdTime)."""
    out, offset = [], None
    while True:
        page = params + ([("offset", offset)] if offset else [])
        res = requests.get(f"{API}/{table}", params=page, timeout=HTTP_TIMEOUT,
                           headers={"Authorization": f"Bearer {token}"})
        res.raise_for_status()
        body = res.json()
        out.extend({**r["fields"], "_created": r.get("createdTime")}
                   for r in body.get("records", []))
        offset = body.get("offset")
        if not offset or not all_pages:
            return out


def fetch_contracts(token: str) -> list[Contract]:
    out = []
    for f in _records(token, CONTRACTS_TABLE, [], all_pages=True):
        sym, exp = f.get("Symbol"), f.get("Expiry Date")
        if not sym or not exp:
            continue
        # An unticked checkbox is simply absent from the record.
        out.append(Contract(sym.strip(), date.fromisoformat(exp), bool(f.get("Active Month"))))
    return out


def fetch_latest_quotes(token: str) -> tuple[date, dict[str, Quote], datetime | None]:
    """(newest snapshot date, {symbol: Quote} on that date, when its newest row
    was written to Airtable in Bangkok time).

    About ten contracts are quoted a day, so the newest 100 rows always hold
    a full day.
    """
    rows = _records(token, QUOTES_TABLE, [
        ("sort[0][field]", "Snapshot Date"),
        ("sort[0][direction]", "desc"),
        ("pageSize", "100"),
    ], all_pages=False)
    rows = [r for r in rows if r.get("Snapshot Date") and r.get("Contract")]
    if not rows:
        raise RuntimeError("Outright Quotes returned no rows")

    latest = max(r["Snapshot Date"] for r in rows)
    today = [r for r in rows if r["Snapshot Date"] == latest]

    created = [r["_created"] for r in today if r.get("_created")]
    last_record = (datetime.fromisoformat(max(created).replace("Z", "+00:00")).astimezone(BKK)
                   if created else None)

    quotes = {}
    for r in today:
        if r.get("Bid") is None or r.get("Ask") is None:
            continue
        # The table has the two columns the wrong way round: "Bid" is above
        # "Ask" on every row ever written (Z26 2026-11-10: Bid 4119.1,
        # Ask 4118.8). Read them swapped, explicitly, rather than min/max -
        # if someone fixes the table this line is the one to change.
        quotes[r["Contract"].strip()] = Quote(
            bid=float(r["Ask"]), ask=float(r["Bid"]), stale=bool(r.get("Stale Flag")))
    return date.fromisoformat(latest), quotes, last_record


# --------------------------------------------------------------------------- #
# 2. Choose — the leg the book holds now
# --------------------------------------------------------------------------- #
def approx_fnd(symbol: str) -> date:
    """First notice day: last weekday of the month before delivery ('Z26' -> Nov 2026).

    Exchange holidays are ignored - close enough to decide which contract the
    book should be in, which is all this is used for.
    """
    year, month = 2000 + int(symbol[1:3]), MONTH_CODES.index(symbol[0].upper()) + 1
    year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    day = date(year, month, calendar.monthrange(year, month)[1])
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def pick_near(contracts: list[Contract], on: date) -> Contract | None:
    """First active month whose first notice day is still ahead of `on`.

    On FND itself the roll is already done (Q26 finished on its FND,
    2026-07-31), so FND has to be strictly after the snapshot.
    """
    live = [c for c in contracts if c.active and approx_fnd(c.symbol) > on]
    return min(live, key=lambda c: c.expiry, default=None)


# --------------------------------------------------------------------------- #
# 3. Compute — pure, no I/O
# --------------------------------------------------------------------------- #
Cell = tuple[int, int]  # (row index, column index) in the matrix


def make_roll(near: Contract, far: Contract, quotes: dict[str, Quote]) -> Roll:
    nq, fq = quotes.get(near.symbol), quotes.get(far.symbol)
    return Roll(
        near=near.symbol,
        far=far.symbol,
        near_bid=nq.bid if nq else None,
        far_ask=fq.ask if fq else None,
        days=(far.expiry - near.expiry).days,
        stale=bool((nq and nq.stale) or (fq and fq.stale)),
    )


def matrix_legs(contracts: list[Contract], quotes: dict[str, Quote],
                near: Contract) -> list[Contract]:
    """Every contract quoted on the snapshot, from the current leg on, by expiry."""
    return [c for c in sorted(contracts, key=lambda c: c.expiry)
            if c.symbol in quotes and c.expiry >= near.expiry]


def build_matrix(legs: list[Contract], quotes: dict[str, Quote]) -> dict[Cell, Roll]:
    """Upper triangle: rows are legs[:-1], columns legs[1:], cell (i, j) for j >= i."""
    rows, cols = legs[:-1], legs[1:]
    return {(i, j): make_roll(n, f, quotes)
            for i, n in enumerate(rows)
            for j, f in enumerate(cols) if j >= i}


def cheapest(cells: dict[Cell, Roll], n: int) -> dict[Cell, int]:
    """{cell: rank} for the `n` lowest yields, rank 0 = cheapest. Unpriced cells skipped."""
    priced = [k for k, r in cells.items() if r.yield_pct is not None]
    priced.sort(key=lambda k: cells[k].yield_pct)
    return {k: rank for rank, k in enumerate(priced[:n])}


# --------------------------------------------------------------------------- #
# 4. Draw — dark matrix, in the layout of the exchange's spread window
# --------------------------------------------------------------------------- #
BG, CELL_BG, HEAD_BG, GRID = "#1b1c1e", "#232427", "#2b2c30", "#34363a"
TEXT, DIM = "#e8e8e8", "#8b8d92"
ASK_INK, BID_INK = "#e5534b", "#3fb950"
NEAR_MARK = "#3fb950"  # the leg the book holds now
STALE_MARK = "#d29922"  # amber, so it is not read as an ask price
CHEAP = "#1f6f3a"  # rank 0; fades towards CELL_BG down to rank TOP_N
TOP_N = 10
FAINT_ON_GREEN = "#a9c7b2"  # the recommend line on a shaded cell, still fainter than TEXT

# Inches. Row/column labels, one cell, header band, title block, notes block.
LABEL_W, CELL_W, HEAD_H, CELL_H, TITLE_H, FOOT_H = 1.15, 1.05, 0.62, 0.62, 0.62, 1.05
PAD = 0.1  # inner margin from a box edge to its text


def _fmt(v: float | None, spec: str) -> str:
    return "—" if v is None else format(v, spec)


def _shade(rank: int) -> tuple[float, float, float]:
    """CHEAP at rank 0, blending linearly towards CELL_BG at rank TOP_N."""
    t = rank / TOP_N
    return tuple(a + (b - a) * t for a, b in zip(to_rgb(CHEAP), to_rgb(CELL_BG)))


def _box(ax, x: float, y: float, w: float, h: float, colour, edge: str = "none") -> None:
    ax.add_patch(Rectangle((x, y), w, h, facecolor=colour, edgecolor=edge, lw=0.5))


def _quote(ax, x_right: float, y_mid: float, q: Quote | None) -> None:
    """Ask (red) above bid (green)."""
    for dy, v, ink in ((-0.1, q.ask if q else None, ASK_INK), (0.1, q.bid if q else None, BID_INK)):
        ax.text(x_right, y_mid + dy, _fmt(v, ".1f"), fontsize=7.5, color=ink,
                ha="right", va="center")


def _leg_mark(ax, x: float, y: float, h: float, q: Quote | None, is_near: bool) -> None:
    """Thin bar at the left edge of a leg label: amber if stale, green if held now."""
    colour = STALE_MARK if q and q.stale else NEAR_MARK if is_near else None
    if colour:
        _box(ax, x, y + 0.04, 0.045, h - 0.08, colour)


def _draw_title(ax, width: float, snapshot: date, last_record: datetime | None,
                hurdle: float) -> None:
    stamp = f"snapshot {snapshot:%Y-%m-%d}"
    if last_record is not None:
        stamp += f" · last record {last_record:%Y-%m-%d %H:%M} BKK"
    ax.text(PAD, 0.2, "COMEX GC rollover cost", fontsize=10, fontweight="bold",
            color=TEXT, va="center")
    ax.text(width - PAD, 0.2, stamp, fontsize=7, color=DIM, ha="right", va="center")
    ax.text(PAD, 0.42, "far ask − near bid (annualised yield)", fontsize=7,
            color=DIM, va="center")
    ax.text(width - PAD, 0.42, f"recommend yield at ({hurdle:.2f}%)", fontsize=7,
            color=DIM, ha="right", va="center")


def _draw_column_heads(ax, width: float, cols: list[Contract], quotes: dict[str, Quote],
                       near: str) -> None:
    y = TITLE_H
    _box(ax, 0, y, width, HEAD_H, HEAD_BG)
    for j, c in enumerate(cols):
        x = LABEL_W + CELL_W * j
        right = x + CELL_W - 0.08
        ax.text(right, y + 0.13, c.symbol, fontsize=7.5, fontweight="bold",
                color=TEXT, ha="right", va="center")
        _quote(ax, right, y + 0.4, quotes.get(c.symbol))
        _leg_mark(ax, x, y, HEAD_H, quotes.get(c.symbol), c.symbol == near)


def _draw_row_head(ax, y: float, leg: Contract, quotes: dict[str, Quote], near: str) -> None:
    _box(ax, 0, y, LABEL_W, CELL_H, HEAD_BG)
    ax.text(0.12, y + 0.13, leg.symbol, fontsize=7.5, fontweight="bold", color=TEXT,
            va="center")
    _quote(ax, 0.72, y + 0.4, quotes.get(leg.symbol))
    _leg_mark(ax, 0, y, CELL_H, quotes.get(leg.symbol), leg.symbol == near)


def _draw_cell(ax, x: float, y: float, roll: Roll, rank: int | None, hurdle: float) -> None:
    """Label "Z26-F27 (29d)", then spread (yield), then the faint spread at the hurdle."""
    shaded = rank is not None
    right = x + CELL_W - 0.08
    _box(ax, x, y, CELL_W, CELL_H, _shade(rank) if shaded else CELL_BG, edge=GRID)

    ax.text(x + 0.08, y + 0.13, f"{roll.near}-{roll.far} ({roll.days}d)", fontsize=5.5,
            color=TEXT if shaded else DIM, va="center")
    main = ("—" if roll.yield_pct is None
            else f"{_fmt(roll.spread, '+.1f')} ({roll.yield_pct:.2f}%)")
    ax.text(right, y + 0.33, main, fontsize=7.5, color=TEXT, ha="right", va="center",
            fontweight="bold" if shaded else "normal")
    ax.text(right, y + 0.5, f"{_fmt(roll.spread_at(hurdle), '+.1f')} ({hurdle:.2f}%)",
            fontsize=6.5, color=FAINT_ON_GREEN if shaded else DIM, ha="right", va="center")


def _draw_footer(ax, y: float, quotes: dict[str, Quote], hurdle: float) -> None:
    # Legend: shade ramp, then the leg marks in use.
    for k in range(TOP_N):
        _box(ax, PAD + 0.07 * k, y - 0.06, 0.07, 0.12, _shade(k))
    ax.text(PAD + 0.07 * TOP_N + 0.08, y, f"lowest yield (top {TOP_N})",
            fontsize=6.5, color=DIM, va="center")
    marks = [(NEAR_MARK, "current leg")]
    if any(q.stale for q in quotes.values()):  # Airtable flags this rarely (1 in ~1,000 rows)
        marks.append((STALE_MARK, "stale quote"))
    x = PAD + 0.07 * TOP_N + 1.4
    for colour, label in marks:
        _box(ax, x, y - 0.06, 0.045, 0.12, colour)
        ax.text(x + 0.1, y, label, fontsize=6.5, color=DIM, va="center")
        x += 0.95

    notes = [
        "cell = far ask − near bid (yield = spread / near bid × 360 / days between expiries)",
        f"recommend yield ({hurdle:.2f}%) = the most we want to pay to roll, annualised;"
        " a roll at or below it is worth doing",
        "faint line = spread that gives exactly the recommend yield"
        " - bid the roll at this spread or tighter",
        "header: ask red, bid green",
    ]
    for k, note in enumerate(notes):
        ax.text(PAD, y + 0.22 + 0.17 * k, note, fontsize=6.5, color=DIM, va="center")


def render(legs: list[Contract], quotes: dict[str, Quote], near: str, snapshot: date,
           hurdle: float, path: str, last_record: datetime | None = None) -> None:
    """Draw the roll matrix for `legs` (sorted by expiry) and save it to `path`."""
    cells = build_matrix(legs, quotes)
    ranks = cheapest(cells, TOP_N)
    rows, cols = legs[:-1], legs[1:]

    width = LABEL_W + CELL_W * len(cols)
    height = TITLE_H + HEAD_H + CELL_H * len(rows) + FOOT_H
    fig = plt.figure(figsize=(width, height), dpi=160)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, width)
    ax.set_ylim(height, 0)  # y grows downwards, like the table
    ax.axis("off")
    fig.patch.set_facecolor(BG)

    _draw_title(ax, width, snapshot, last_record, hurdle)
    _draw_column_heads(ax, width, cols, quotes, near)
    for i, leg in enumerate(rows):
        y = TITLE_H + HEAD_H + CELL_H * i
        _draw_row_head(ax, y, leg, quotes, near)
        for (ci, cj), roll in cells.items():
            if ci == i:
                _draw_cell(ax, LABEL_W + CELL_W * cj, y, roll, ranks.get((ci, cj)), hurdle)
    _draw_footer(ax, TITLE_H + HEAD_H + CELL_H * len(rows) + 0.2, quotes, hurdle)

    fig.savefig(path, facecolor=BG)
    plt.close(fig)


# --------------------------------------------------------------------------- #
# 5. Publish
# --------------------------------------------------------------------------- #
def _last_sent() -> str | None:
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh).get("snapshot")
    except (OSError, ValueError):
        return None


def _mark_sent(key: str) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump({"snapshot": key}, fh)


def _log_near_rolls(near: Contract, contracts: list[Contract], quotes: dict[str, Quote],
                    snapshot: date, last_record: datetime | None, hurdle: float) -> None:
    """One log line per roll out of the held leg into each later active month."""
    logger.info("Rollover %s on %s (last record %s):", near.symbol, snapshot,
                last_record.isoformat(timespec="minutes") if last_record else "?")
    targets = sorted((c for c in contracts if c.active and c.expiry > near.expiry),
                     key=lambda c: c.expiry)
    for far in targets:
        r = make_roll(near, far, quotes)
        logger.info("  %s→%-4s bid %s ask %s spread %s @hurdle %s %3dd cost %s%s",
                    r.near, r.far, _fmt(r.near_bid, ".1f"), _fmt(r.far_ask, ".1f"),
                    _fmt(r.spread, "+.1f"), _fmt(r.spread_at(hurdle), "+.1f"),
                    r.days, _fmt(r.yield_pct, ".2f"), " (stale)" if r.stale else "")


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", help="render to this PNG and post nothing")
    ap.add_argument("--from", dest="near", help="near leg override, e.g. G27")
    ap.add_argument("--space", help="post to this Path.env key instead; not recorded as sent")
    ap.add_argument("--force", action="store_true", help="post even if already sent")
    ap.add_argument("--dry-run", action="store_true", help="log the near leg's rolls only")
    return ap.parse_args()


def main() -> int:
    args = _parse_args()
    setup_logger(LOG_FILE)
    cfg = load_config()
    hurdle = float(cfg.get("roll_hurdle_pct", DEFAULT_HURDLE_PCT))

    # 1. read
    try:
        token = load_token()
        contracts = fetch_contracts(token)
        snapshot, quotes, last_record = fetch_latest_quotes(token)
    except Exception as exc:  # noqa: BLE001 - one log line beats a traceback under pythonw
        logger.error("Rollover: cannot read Airtable (%s).", exc)
        return 1

    # 2. choose
    if args.near:
        near = next((c for c in contracts if c.symbol == args.near.upper()), None)
    else:
        near = pick_near(contracts, snapshot)
    if near is None:
        logger.error("Rollover: no near leg found (override=%s).", args.near)
        return 1
    if near.symbol not in quotes:
        logger.error("Rollover: no %s quote on %s.", near.symbol, snapshot)
        return 1

    _log_near_rolls(near, contracts, quotes, snapshot, last_record, hurdle)
    if args.dry_run:
        return 0

    # 3-4. compute + draw
    legs = matrix_legs(contracts, quotes, near)
    if args.out:
        render(legs, quotes, near.symbol, snapshot, hurdle, args.out, last_record)
        logger.info("Rollover: rendered %s.", args.out)
        return 0

    # 5. publish - the scheduled run posts once per snapshot; --space is a side copy
    key = f"{snapshot.isoformat()}_{near.symbol}"
    if not args.space and not args.force and _last_sent() == key:
        logger.info("Rollover: %s already posted, skipping.", key)
        return 0

    space_key = args.space or cfg.get("rollover_space_key", DEFAULT_SPACE_KEY)
    space = chat.space_id(space_key)
    if not space:
        logger.error("Rollover: %s not set in %s.", space_key, chat.ENV_FILE)
        return 1

    render(legs, quotes, near.symbol, snapshot, hurdle, PNG_FILE, last_record)
    try:
        chat.post(space, f"Roll cost from {near.symbol} · snapshot {snapshot:%Y-%m-%d}", PNG_FILE)
    except Exception as exc:  # noqa: BLE001
        logger.error("Rollover: posting to %s failed (%s).", space_key, exc)
        return 1
    if not args.space:
        _mark_sent(key)
    logger.info("Rollover: posted %s to %s.", key, space_key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
