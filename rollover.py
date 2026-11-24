"""proj_3006 — COMEX gold calendar-spread matrix (and roll cost), posted to Chat.

Reads the Airtable base the Comex-FWD strategy keeps (contract calendar plus a
daily outright bid/ask per contract), works out the annualised cost of rolling
the near leg into every later active month (logged), renders a near − far spread
matrix of every quoted contract and posts it
to a Google Chat space.

    cost% = (Far_ASK - Near_BID) / Near_BID x 360 / (Far_Expiry - Near_Expiry)

Long position: sell the near leg at its bid, buy the far leg at its ask. The
divisor is the gap between the two expiries, because the spread paid is the
carry for that stretch only - until the near leg expires we still hold it and
pay nothing extra.

The near leg is picked automatically: the first active month whose first
notice day has not yet arrived, so the report moves on to the next contract by
itself after each roll.

    uv run python rollover.py              # post to the configured space
    uv run python rollover.py --out x.png  # render only, post nothing
    uv run python rollover.py --from G27   # pretend the book is in G27
    uv run python rollover.py --dry-run    # log the table, no image, no post
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
from matplotlib.patches import Rectangle  # noqa: E402

import report  # noqa: E402
from simple_heatmap import (  # noqa: E402
    GITHUB_DIR,
    HTTP_TIMEOUT,
    PROJECT_DIR,
    load_config,
    logger,
    setup_logger,
)

BASE_ID = "appXoUtj6dYJesMRG"
CONTRACTS_TABLE = "tblpNjJNiZiZgMHiN"
QUOTES_TABLE = "tblvLVz9OZr3y9yPA"
API = f"https://api.airtable.com/v0/{BASE_ID}"

# A file of its own beside GitToken.env / FundingRate.env, not in Path.env:
# the token is a secret, Path.env is plain machine settings.
TOKEN_ENV = os.path.join(GITHUB_DIR, ".env", "Airtable.env")
TOKEN_KEY = "AIRTABLE_TOKEN"

STATE_FILE = os.path.join(PROJECT_DIR, "logs", "rollover_last.json")
LOG_FILE = "rollover.log"

DAY_COUNT = 360
BKK = ZoneInfo("Asia/Bangkok")
MONTH_CODES = "FGHJKMNQUVXZ"  # Jan..Dec



# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Contract:
    symbol: str
    expiry: date
    active: bool


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    stale: bool


@dataclass(frozen=True)
class Row:
    far: str
    near_bid: float | None
    far_ask: float | None
    days: int
    stale: bool

    @property
    def spread(self) -> float | None:
        if self.near_bid is None or self.far_ask is None:
            return None
        return self.far_ask - self.near_bid

    @property
    def cost_pct(self) -> float | None:
        if self.spread is None or self.days <= 0:
            return None
        return self.spread / self.near_bid * DAY_COUNT / self.days * 100

    def hurdle_spread(self, hurdle_pct: float) -> float | None:
        """The spread that would cost exactly `hurdle_pct` - the level to bid the roll at."""
        if self.near_bid is None or self.days <= 0:
            return None
        return self.near_bid * hurdle_pct / 100 * self.days / DAY_COUNT


# --------------------------------------------------------------------------- #
# Calendar
# --------------------------------------------------------------------------- #
def delivery_month(symbol: str) -> tuple[int, int]:
    """'Z26' -> (2026, 12)."""
    return 2000 + int(symbol[1:3]), MONTH_CODES.index(symbol[0].upper()) + 1


def approx_fnd(symbol: str) -> date:
    """First notice day: last weekday of the month before delivery.

    Exchange holidays are ignored - close enough to decide which contract the
    book should be in, which is all this is used for.
    """
    year, month = delivery_month(symbol)
    year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    day = date(year, month, calendar.monthrange(year, month)[1])
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def approx_fpd(symbol: str) -> date:
    """First position day: the weekday before the first notice day.

    Matches the CME gold calendar (GCZ26: FPD 2026-11-27, FND 2026-11-30;
    GCF27: 2026-12-30 / 2026-12-31). Holidays ignored, as for approx_fnd.
    """
    day = approx_fnd(symbol) - timedelta(days=1)
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


def roll_rows(near: Contract, contracts: list[Contract],
              quotes: dict[str, Quote]) -> list[Row]:
    nq = quotes.get(near.symbol)
    targets = sorted((c for c in contracts if c.active and c.expiry > near.expiry),
                     key=lambda c: c.expiry)
    rows = []
    for far in targets:
        fq = quotes.get(far.symbol)
        rows.append(Row(
            far=far.symbol,
            near_bid=nq.bid if nq else None,
            far_ask=fq.ask if fq else None,
            days=(far.expiry - near.expiry).days,
            stale=bool((nq and nq.stale) or (fq and fq.stale)),
        ))
    return rows


# --------------------------------------------------------------------------- #
# Airtable
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
    """Every contract's quote on the newest snapshot date, and when the newest
    of those rows was written to Airtable (Bangkok time).

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
    created = [r["_created"] for r in rows
               if r["Snapshot Date"] == latest and r.get("_created")]
    last_record = (datetime.fromisoformat(max(created).replace("Z", "+00:00"))
                   .astimezone(BKK) if created else None)
    quotes = {}
    for r in rows:
        if r["Snapshot Date"] != latest or r.get("Bid") is None or r.get("Ask") is None:
            continue
        # The table has the two columns the wrong way round: "Bid" is above
        # "Ask" on every row ever written (Z26 2026-11-10: Bid 4119.1,
        # Ask 4118.8). Read them swapped, explicitly, rather than min/max -
        # if someone fixes the table this line is the one to change.
        quotes[r["Contract"].strip()] = Quote(
            bid=float(r["Ask"]), ask=float(r["Bid"]), stale=bool(r.get("Stale Flag")))
    return date.fromisoformat(latest), quotes, last_record


# --------------------------------------------------------------------------- #
# Render - spread matrix, dark, in the layout of the exchange's spread window
# --------------------------------------------------------------------------- #
BG = "#1b1c1e"
CELL = "#232427"
HEAD = "#2b2c30"
GRID = "#34363a"
TEXT = "#e8e8e8"
DIM = "#8b8d92"
ASK_INK = "#e5534b"
BID_INK = "#3fb950"
NEAR_MARK = "#3fb950"  # the leg the book holds now
STALE_MARK = "#d29922"  # amber, so it is not read as an ask price
CHEAP = "#1f6f3a"  # lowest roll yield; fades to CELL over the top TOP_N
TOP_N = 10
SUGGEST_ON_SHADE = "#a9c7b2"  # the recommend line on a green cell, still fainter than TEXT

LABEL_W, CELL_W, HEAD_H, CELL_H, TITLE_H, FOOT_H = 1.15, 1.05, 0.62, 0.62, 0.62, 1.05


def _fmt(v: float | None, spec: str) -> str:
    return "—" if v is None else format(v, spec)


def _mix(a: str, b: str, t: float) -> tuple[float, float, float]:
    """`a` at t=0, `b` at t=1."""
    ca, cb = matplotlib.colors.to_rgb(a), matplotlib.colors.to_rgb(b)
    return tuple(x + (y - x) * t for x, y in zip(ca, cb))


def _quote_block(ax, x_right: float, y_mid: float, q: Quote | None, size: float) -> None:
    ax.text(x_right, y_mid - 0.1, _fmt(q.ask if q else None, ".1f"),
            fontsize=size, color=ASK_INK, ha="right", va="center")
    ax.text(x_right, y_mid + 0.1, _fmt(q.bid if q else None, ".1f"),
            fontsize=size, color=BID_INK, ha="right", va="center")


def render_matrix(legs: list[Contract], quotes: dict[str, Quote], near: str | None,
                  snapshot: date, path: str, hurdle: float,
                  last_record: datetime | None = None) -> None:
    """Upper-triangle matrix: row = near leg, column = far leg.

    Each cell is the roll from row to column - sell the near at its bid, buy
    the far at its ask - as points and as an annualised yield (Row.cost_pct).
    """
    rows, cols = legs[:-1], legs[1:]
    cells: dict[tuple[int, int], Row] = {}
    for i, n in enumerate(rows):
        for j, f in enumerate(cols):
            if j < i:
                continue
            nq, fq = quotes.get(n.symbol), quotes.get(f.symbol)
            cells[i, j] = Row(far=f.symbol, near_bid=nq.bid if nq else None,
                              far_ask=fq.ask if fq else None,
                              days=(f.expiry - n.expiry).days,
                              stale=bool((nq and nq.stale) or (fq and fq.stale)))
    ranked = sorted((k for k, r in cells.items() if r.cost_pct is not None),
                    key=lambda k: cells[k].cost_pct)
    shade = {k: _mix(CHEAP, CELL, rank / TOP_N) for rank, k in enumerate(ranked[:TOP_N])}

    width = LABEL_W + CELL_W * len(cols)
    height = TITLE_H + HEAD_H + CELL_H * len(rows) + FOOT_H
    fig = plt.figure(figsize=(width, height), dpi=160)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, width)
    ax.set_ylim(height, 0)
    ax.axis("off")
    fig.patch.set_facecolor(BG)

    ax.text(0.1, 0.2, "COMEX GC rollover cost", fontsize=10, fontweight="bold",
            color=TEXT, va="center")
    stamp = f"snapshot {snapshot:%Y-%m-%d}"
    if last_record is not None:
        stamp += f" · last record {last_record:%Y-%m-%d %H:%M} BKK"
    ax.text(width - 0.1, 0.2, stamp, fontsize=7, color=DIM, ha="right", va="center")
    ax.text(0.1, 0.42, "far ask − near bid (annualised yield)", fontsize=7,
            color=DIM, va="center")
    ax.text(width - 0.1, 0.42, f"recommend yield at ({hurdle:.2f}%)", fontsize=7,
            color=DIM, ha="right", va="center")

    def mark(x: float, y: float, h: float, sym: str) -> None:
        q = quotes.get(sym)
        colour = STALE_MARK if q and q.stale else NEAR_MARK if sym == near else None
        if colour:
            ax.add_patch(Rectangle((x, y + 0.04), 0.045, h - 0.08,
                                   facecolor=colour, edgecolor="none"))

    # Column headers.
    y0 = TITLE_H
    ax.add_patch(Rectangle((0, y0), width, HEAD_H, facecolor=HEAD, edgecolor="none"))
    for j, c in enumerate(cols):
        x = LABEL_W + CELL_W * j
        ax.text(x + CELL_W - 0.08, y0 + 0.13, c.symbol, fontsize=7.5, fontweight="bold",
                color=TEXT, ha="right", va="center")
        _quote_block(ax, x + CELL_W - 0.08, y0 + 0.4, quotes.get(c.symbol), 7.5)
        mark(x, y0, HEAD_H, c.symbol)

    # Rows.
    for i, n in enumerate(rows):
        y = TITLE_H + HEAD_H + CELL_H * i
        ax.add_patch(Rectangle((0, y), LABEL_W, CELL_H, facecolor=HEAD, edgecolor="none"))
        ax.text(0.12, y + 0.13, n.symbol, fontsize=7.5, fontweight="bold",
                color=TEXT, va="center")
        _quote_block(ax, 0.12 + 0.6, y + 0.4, quotes.get(n.symbol), 7.5)
        mark(0, y, CELL_H, n.symbol)
        for j, f in enumerate(cols):
            if (i, j) not in cells:
                continue
            r = cells[i, j]
            x = LABEL_W + CELL_W * j
            ax.add_patch(Rectangle((x, y), CELL_W, CELL_H, facecolor=shade.get((i, j), CELL),
                                   edgecolor=GRID, lw=0.5))
            ax.text(x + 0.08, y + 0.13, f"{n.symbol}-{f.symbol} ({r.days}d)", fontsize=5.5,
                    color=DIM if (i, j) not in shade else TEXT, va="center")
            pct = r.cost_pct
            ax.text(x + CELL_W - 0.08, y + 0.33,
                    f"{_fmt(r.spread, '+.1f')} ({_fmt(pct, '.2f')}%)" if pct is not None else "—",
                    fontsize=7.5, color=TEXT, ha="right", va="center",
                    fontweight="bold" if (i, j) in shade else "normal")
            ax.text(x + CELL_W - 0.08, y + 0.5,
                    f"{_fmt(r.hurdle_spread(hurdle), '+.1f')} ({hurdle:.2f}%)",
                    fontsize=6.5, color=DIM if (i, j) not in shade else SUGGEST_ON_SHADE,
                    ha="right", va="center")

    fy = TITLE_H + HEAD_H + CELL_H * len(rows) + 0.2
    for k in range(TOP_N):
        ax.add_patch(Rectangle((0.1 + 0.07 * k, fy - 0.06), 0.07, 0.12,
                               facecolor=_mix(CHEAP, CELL, k / TOP_N), edgecolor="none"))
    ax.text(0.1 + 0.07 * TOP_N + 0.08, fy, f"lowest yield (top {TOP_N})",
            fontsize=6.5, color=DIM, va="center")
    lx = 0.1 + 0.07 * TOP_N + 1.4
    marks = [(NEAR_MARK, "current leg")]
    if any(q.stale for q in quotes.values()):  # Airtable flags this rarely (1 in ~1,000 rows)
        marks.append((STALE_MARK, "stale quote"))
    for colour, label in marks:
        ax.add_patch(Rectangle((lx, fy - 0.06), 0.045, 0.12, facecolor=colour,
                               edgecolor="none"))
        ax.text(lx + 0.1, fy, label, fontsize=6.5, color=DIM, va="center")
        lx += 0.95
    notes = [
        "cell = far ask − near bid (yield = spread / near bid × 360 / days between expiries)",
        f"recommend yield ({hurdle:.2f}%) = the most we want to pay to roll, annualised;"
        " a roll at or below it is worth doing",
        "faint line = spread that gives exactly the recommend yield"
        " - bid the roll at this spread or tighter",
        "header: ask red, bid green",
    ]
    for k, note in enumerate(notes):
        ax.text(0.1, fy + 0.22 + 0.17 * k, note, fontsize=6.5, color=DIM, va="center")

    fig.savefig(path, facecolor=BG)
    plt.close(fig)


# --------------------------------------------------------------------------- #
# Entry point
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", help="render to this PNG and post nothing")
    ap.add_argument("--from", dest="near", help="near leg override, e.g. G27")
    ap.add_argument("--force", action="store_true", help="post even if already sent")
    ap.add_argument("--dry-run", action="store_true", help="log the table only")
    args = ap.parse_args()

    setup_logger(LOG_FILE)
    cfg = load_config()
    hurdle = float(cfg.get("roll_hurdle_pct", 4.80))

    try:
        token = load_token()
        contracts = fetch_contracts(token)
        snapshot, quotes, last_record = fetch_latest_quotes(token)
    except Exception as exc:  # noqa: BLE001 - one log line beats a traceback under pythonw
        logger.error("Rollover: cannot read Airtable (%s).", exc)
        return 1

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

    rows = roll_rows(near, contracts, quotes)
    # Every contract quoted on the snapshot, from the current leg on, as columns.
    legs = [c for c in sorted(contracts, key=lambda c: c.expiry)
            if c.symbol in quotes and c.expiry >= near.expiry]
    logger.info("Rollover %s on %s (last record %s):", near.symbol, snapshot,
                last_record.isoformat(timespec="minutes") if last_record else "?")
    for r in rows:
        logger.info("  %s→%-4s bid %s ask %s spread %s @hurdle %s %3dd cost %s%s",
                    near.symbol, r.far, _fmt(r.near_bid, ".1f"), _fmt(r.far_ask, ".1f"),
                    _fmt(r.spread, "+.1f"), _fmt(r.hurdle_spread(hurdle), "+.1f"),
                    r.days, _fmt(r.cost_pct, ".2f"),
                    " (stale)" if r.stale else "")
    if args.dry_run:
        return 0

    if args.out:
        render_matrix(legs, quotes, near.symbol, snapshot, args.out, hurdle, last_record)
        logger.info("Rollover: rendered %s.", args.out)
        return 0

    key = f"{snapshot.isoformat()}_{near.symbol}"
    if not args.force and _last_sent() == key:
        logger.info("Rollover: %s already posted, skipping.", key)
        return 0

    space_key = cfg.get("rollover_space_key", "TestSpace")
    space = report.chat_space(space_key)
    if not space:
        logger.error("Rollover: %s not set in %s.", space_key, report.ENV_FILE)
        return 1

    png = os.path.join(PROJECT_DIR, "logs", f"rollover_{key}.png")
    render_matrix(legs, quotes, near.symbol, snapshot, png, hurdle, last_record)
    try:
        report.post(space, f"Roll cost from {near.symbol} · snapshot {snapshot:%Y-%m-%d}", png)
    except Exception as exc:  # noqa: BLE001
        logger.error("Rollover: posting to %s failed (%s).", space_key, exc)
        return 1
    _mark_sent(key)
    logger.info("Rollover: posted %s to %s.", key, space_key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
