"""proj_3006 — what it costs to roll the COMEX gold position, posted to Chat.

Reads the Airtable base the Comex-FWD strategy keeps (contract calendar plus a
daily outright bid/ask per contract), works out the annualised cost of rolling
the near leg into every later active month, renders it as a table and posts it
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

INK, MUTED, FAINT, PAGE = report.INK, report.MUTED, report.FAINT, report.PAGE
BELOW_HURDLE = "#cdebd3"  # soft green; black text stays well above 7:1
RULE = "#e4e3df"


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
# Render
# --------------------------------------------------------------------------- #
COLS = [("Far", 0.9), ("Near BID", 1.15), ("Far ASK", 1.15),
        ("Spread", 1.0), ("@hurdle", 1.4), ("Days", 0.8), ("Cost %", 1.05)]
ROW_H, HEAD_H, TOP_H, FOOT_H, PAD = 0.42, 0.42, 0.95, 1.2, 0.3


def _fmt(v: float | None, spec: str) -> str:
    return "—" if v is None else format(v, spec)


def render(near: Contract, snapshot: date, rows: list[Row], hurdle: float,
           path: str, last_record: datetime | None = None) -> None:
    width = sum(w for _, w in COLS) + 2 * PAD
    height = TOP_H + HEAD_H + ROW_H * len(rows) + FOOT_H
    fig = plt.figure(figsize=(width, height), dpi=160)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, width)
    ax.set_ylim(height, 0)
    ax.axis("off")
    fig.patch.set_facecolor(PAGE)

    ax.text(PAD, 0.38, f"Roll {near.symbol} → later months",
            fontsize=13, fontweight="bold", color=INK, va="center")
    ax.text(PAD, 0.72,
            f"COMEX GC · {near.symbol} expiry "
            f"{near.expiry:%Y-%m-%d} · FND ~{approx_fnd(near.symbol):%Y-%m-%d}",
            fontsize=8.6, color=MUTED, va="center")
    if last_record is not None:
        ax.text(width - PAD, 0.38, f"last record {last_record:%Y-%m-%d %H:%M} BKK",
                fontsize=8.6, color=MUTED, va="center", ha="right")

    y = TOP_H
    x = PAD
    for name, w in COLS:
        label = f"Spread @{hurdle:.2f}%" if name == "@hurdle" else name
        ax.text(x + w - 0.08 if name != "Far" else x + 0.08, y + HEAD_H / 2, label,
                fontsize=9, color=MUTED, fontweight="bold", va="center",
                ha="right" if name != "Far" else "left")
        x += w
    y += HEAD_H
    ax.plot([PAD, width - PAD], [y, y], color=INK, lw=0.8)

    for r in rows:
        cost = r.cost_pct
        cells = [r.far + ("*" if r.stale else ""), _fmt(r.near_bid, ".1f"),
                 _fmt(r.far_ask, ".1f"), _fmt(r.spread, "+.1f"),
                 _fmt(r.hurdle_spread(hurdle), "+.1f"), str(r.days),
                 "—" if cost is None else f"{cost:.2f}%"]
        x = PAD
        for i, ((name, w), text) in enumerate(zip(COLS, cells)):
            last = i == len(COLS) - 1
            if last and cost is not None and cost < hurdle:
                ax.add_patch(Rectangle((x, y + 0.04), w, ROW_H - 0.08,
                                       facecolor=BELOW_HURDLE, edgecolor="none"))
            ax.text(x + 0.08 if i == 0 else x + w - 0.08, y + ROW_H / 2, text,
                    fontsize=10, va="center", ha="left" if i == 0 else "right",
                    color=INK, fontweight="bold" if last or i == 0 else "normal")
            x += w
        y += ROW_H
        ax.plot([PAD, width - PAD], [y, y], color=RULE, lw=0.6)

    fy = y + 0.3
    ax.add_patch(Rectangle((PAD, fy - 0.08), 0.22, 0.16,
                           facecolor=BELOW_HURDLE, edgecolor="none"))
    ax.text(PAD + 0.32, fy, f"below hurdle {hurdle:.2f}%",
            fontsize=8.4, color=MUTED, va="center")
    ax.text(PAD, fy + 0.27,
            "cost = (Far ASK − Near BID) / Near BID × 360 / (Far expiry − Near expiry)",
            fontsize=8.0, color=FAINT, va="center")
    ax.text(PAD, fy + 0.5,
            f"spread @{hurdle:.2f}% = Near BID × {hurdle:.2f}% × days / 360  (the spread that costs exactly the hurdle)",
            fontsize=8.0, color=FAINT, va="center")
    if any(r.stale for r in rows):
        ax.text(PAD, fy + 0.73, "* stale quote on one of the legs",
                fontsize=8.0, color=FAINT, va="center")

    fig.savefig(path, facecolor=PAGE)
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
    hurdle = float(cfg.get("roll_hurdle_pct", 4.50))

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
        render(near, snapshot, rows, hurdle, args.out, last_record)
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
    render(near, snapshot, rows, hurdle, png, last_record)
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
