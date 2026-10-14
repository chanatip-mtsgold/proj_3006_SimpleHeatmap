"""proj_3006 — render `Front` as an image and post it to a Google Chat space.

The daily job calls `send(...)` once, after the append, for the newest slot it
wrote. `Front` is pure spreadsheet formulas, so reading it back through the API
returns values Sheets has already recalculated against the row just added —
there is nothing to wait for.

Run it by hand to re-send the current state:

    uv run python report.py              # post to the configured space
    uv run python report.py --out x.png  # render only, post nothing
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta

import matplotlib

matplotlib.use("Agg")  # no display, and no window under pythonw

import matplotlib.pyplot as plt  # noqa: E402
from google.auth.transport.requests import AuthorizedSession  # noqa: E402
from google.oauth2.credentials import Credentials  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

import setup_front as sf  # noqa: E402
from simple_heatmap import GITHUB_DIR, HTTP_TIMEOUT, logger  # noqa: E402

FRONT = "Front"
DATA = "Data"

# The space id lives beside the other per-machine settings rather than in
# config.json, which is committed. The key name is the constant, not the id.
ENV_FILE = os.path.join(GITHUB_DIR, ".env", "Path.env")
SPACE_KEY = "HeatmapSpace"

# Posting needs the chat.messages.create scope, which the shared
# authorized_user.json does not carry — token.json is the one that does.
CHAT_TOKEN = os.path.join(GITHUB_DIR, ".cred", "token.json")

# The corner cell plus one column per leg; rows run to Front's last row, which
# includes the spot row's net-of-swap twin.
GRID_SPAN = sf.SPAN + 1
GRID_ROWS = sf.LAST_ROW

TFEX_RE = re.compile(r"^GO[A-Z]\d\d$")
# The yield can be negative - a pair whose column leg expires first reads
# below zero - so the sign is part of the number, as it is for the spread.
CELL_RE = re.compile(r"(?:(-?[\d.]+)%\s*)?\(([-+]?[\d.]+)\)\s*\((\d+)d\)")

CROSS_EDGE = "#1a2a4a"
INK, MUTED, FAINT, PAGE = "#1a1a1a", "#6b6b70", "#8a8a8f", "#ffffff"


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #
def venue(symbol: str) -> str:
    """Spot is its own class: spot-against-future is not a cross-market pair."""
    if TFEX_RE.match(symbol):
        return "TFEX"
    return "SPOT" if symbol.upper().startswith("XAU") else "CMX"


def read_front(spreadsheet) -> list[list[str]]:
    """The triangle as displayed, trimmed to the legs that are actually there."""
    ws = spreadsheet.worksheet(FRONT)
    last = sf.col_letter(1 + sf.SPAN)
    rows = ws.get(f"A1:{last}{GRID_ROWS}")
    rows = [r + [""] * (GRID_SPAN - len(r)) for r in rows]
    rows += [[""] * GRID_SPAN for _ in range(GRID_ROWS - len(rows))]

    # Keep a column or row only if at least one of its cells is priced. That
    # drops Front's unused tail (it is built out to MAX_LEGS) and also a leg
    # that has a slot but no quotes yet - on the sheet it is a placeholder, in
    # a report it would only be an empty stripe.
    keep_cols = [
        j for j in range(1, GRID_SPAN)
        if rows[0][j].strip() and any(rows[i][j].strip() for i in range(1, GRID_ROWS))
    ]
    keep_rows = [
        i for i in range(1, GRID_ROWS)
        if rows[i][0].strip() and any(rows[i][j].strip() for j in keep_cols)
    ]
    header = [rows[0][0]] + [rows[0][j] for j in keep_cols]
    body = [[rows[i][0]] + [rows[i][j] for j in keep_cols] for i in keep_rows]
    return [header] + body


def read_swap(spreadsheet) -> tuple[float, float] | None:
    """The swap Front's net row deducted: (points per day, fraction per year)."""
    got = spreadsheet.worksheet(FRONT).get(f"B{sf.SWAP_ROW}:C{sf.SWAP_ROW}",
                                           value_render_option="UNFORMATTED_VALUE")
    vals = (got[0] + ["", ""])[:2] if got else ["", ""]
    if all(isinstance(v, (int, float)) for v in vals):
        return float(vals[0]), float(vals[1])
    return None


def read_prices(spreadsheet) -> dict[str, tuple[str, str]]:
    """{leg: (bid, ask)} from the newest Data row - the same row Front prices.

    Kept as the sheet's own strings, so the image shows exactly the digits
    that went into the cells rather than a float re-rendered to some width.
    """
    from simple_heatmap import parse_header  # deferred: avoids an import cycle

    ws = spreadsheet.worksheet(DATA)
    _, legs = parse_header(ws.row_values(1))
    last = ws.row_values(len(ws.col_values(1)))
    cell = lambda c: last[c - 1].strip() if c - 1 < len(last) else ""  # noqa: E731
    return {name: (cell(col), cell(col + 1)) for name, col, _ in legs}


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #
def _rgb(colour: str) -> tuple[int, ...]:
    return tuple(int(colour[i : i + 2], 16) for i in (1, 3, 5))


def shade(yld: float | None) -> str:
    """Continuous walk over the same anchors the sheet steps through."""
    if yld is None:
        return sf.SUPPRESSED
    size = abs(yld)
    if size <= sf.GREY_BAND:
        return sf.NEUTRAL

    t = min((size - sf.GREY_BAND) / (sf.FULL_COLOUR - sf.GREY_BAND), 1.0)
    anchors = sf.RAMP_UP if yld > 0 else sf.RAMP_DOWN
    pos = t * (len(anchors) - 1)
    i = min(int(pos), len(anchors) - 2)
    lo, hi, f = _rgb(anchors[i]), _rgb(anchors[i + 1]), pos - i
    return "#%02x%02x%02x" % tuple(round(a + (b - a) * f) for a, b in zip(lo, hi))


CELL_W, CELL_H = 1.0, 0.62
HDR_W = 1.65  # row labels plus the LONG bar; wide enough for the swap note
PAD_X, FOOT_H = 0.28, 1.15
TOP_H = 1.45  # title, scale key, column labels + bid, and the SHORT bar

# Every cell is Y_BID - X_ASK: buy the row leg, sell the column leg. The bars
# say so on the matrix itself, so nobody has to remember which way round it
# goes. Red and green are the validated palette slots from setup_front, and
# each bar carries its word as well, so the sides never rest on colour alone.
LONG_COLOUR, SHORT_COLOUR = sf.CATEGORICAL[5], sf.CATEGORICAL[7]
BAR = 0.17  # bar thickness
BAR_GAP = 0.06  # bar to cells

# Below this the subtitle and the scale key collide in the header row.
MIN_FIG_W = 7.4


def render(grid: list[list[str]], path: str,
           prices: dict[str, tuple[str, str]] | None = None,
           swap: tuple[float, float] | None = None) -> str:
    stamp = grid[0][0]
    prices = prices or {}
    col_keys, row_keys = grid[0][1:], [r[0] for r in grid[1:]]
    cols = [c.replace("CMX.", "") for c in col_keys]
    rows = [r.replace("CMX.", "") for r in row_keys]
    # The net row is spot with a cost taken off, so it is spot for venue
    # purposes even though its label no longer starts with XAU.
    col_venue = [venue(c) for c in cols]
    row_venue = ["SPOT" if r.startswith(sf.NET_PREFIX) else venue(r) for r in row_keys]

    x0 = PAD_X + HDR_W
    # Width is built up from the parts rather than scaled, so the right-hand
    # column is never clipped however few legs there are.
    fig_w = max(x0 + len(cols) * CELL_W + PAD_X, MIN_FIG_W)
    fig_h = TOP_H + len(rows) * CELL_H + FOOT_H
    fig = plt.figure(figsize=(fig_w, fig_h), dpi=160)
    fig.patch.set_facecolor(PAGE)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    ax.set_xlim(0, fig_w)
    ax.set_ylim(0, fig_h)

    top = fig_h - TOP_H
    ax.text(PAD_X, fig_h - 0.42, "Gold carry", fontsize=15, fontweight="bold",
            color=CROSS_EDGE, va="center")
    ax.text(PAD_X + 1.62, fig_h - 0.44, "Front  -  " + stamp, fontsize=9.5,
            color=MUTED, va="center")

    _scale_key(ax, fig_w, fig_h)
    _side_bars(ax, x0, top, len(cols), len(rows))

    # Each label carries the price its side of the trade is done at: the
    # column is sold, so its bid; the row is bought, so its ask.
    label_y = top + BAR_GAP + BAR
    for j, name in enumerate(cols):
        cx = x0 + (j + 0.5) * CELL_W
        bid = prices.get(col_keys[j], ("", ""))[0]
        ax.text(cx, label_y + (0.37 if bid else 0.17), name, fontsize=9.5,
                fontweight="bold", color=INK, ha="center", va="center")
        if bid:
            ax.text(cx, label_y + 0.15, "bid " + bid, fontsize=7.8, color=MUTED,
                    ha="center", va="center")

    for i, name in enumerate(rows):
        y = top - (i + 1) * CELL_H
        lx = x0 - BAR_GAP - BAR - 0.10
        ask = prices.get(row_keys[i], ("", ""))[1]
        # The net row has no price of its own; it shows the swap it deducted.
        note = ("ask " + ask) if ask else ""
        if row_keys[i].startswith(sf.NET_PREFIX) and swap is not None:
            note = f"{swap[0]:.3f} pt/d · {swap[1] * 100:.2f}%/yr"
        ax.text(lx, y + CELL_H / 2 + (0.10 if note else 0), name, fontsize=9.5,
                fontweight="bold", color=INK, ha="right", va="center")
        if note:
            ax.text(lx, y + CELL_H / 2 - 0.13, note, fontsize=7.8,
                    color=MUTED, ha="right", va="center")
        for j in range(len(cols)):
            cross = (row_venue[i] != col_venue[j]
                     and "SPOT" not in (row_venue[i], col_venue[j]))
            _cell(ax, grid[1 + i][1 + j].strip(), x0 + j * CELL_W, y, cross)

    _footnotes(ax, top - len(rows) * CELL_H - 0.30)
    fig.savefig(path, facecolor=PAGE)
    plt.close(fig)
    return path


def _cell(ax, raw: str, x: float, y: float, cross: bool) -> None:
    m = CELL_RE.match(raw)
    if not m:
        return  # outside the triangle, or a shape we do not recognise
    pct, spread, days = m.groups()
    yld = float(pct) / 100 if pct else None

    ax.add_patch(Rectangle((x + 0.02, y + 0.03), CELL_W - 0.04, CELL_H - 0.06,
                           facecolor=shade(yld),
                           edgecolor=CROSS_EDGE if cross else PAGE,
                           lw=1.8 if cross else 1.2, zorder=2 if cross else 1))
    mid = x + CELL_W / 2
    if pct:
        ax.text(mid, y + CELL_H - 0.22, pct + "%", fontsize=11, fontweight="bold",
                color=INK, ha="center", va="center")
        ax.text(mid, y + 0.17, spread + "   " + days + "d", fontsize=7.8,
                color=MUTED, ha="center", va="center")
    else:
        ax.text(mid, y + CELL_H / 2 + 0.06, spread, fontsize=9.5, color=MUTED,
                ha="center", va="center")
        ax.text(mid, y + CELL_H / 2 - 0.13, f"{days}d   gap<{sf.MIN_GAP}",
                fontsize=7, color=FAINT, ha="center", va="center")


def _side_bars(ax, x0: float, top: float, n_cols: int, n_rows: int) -> None:
    """SHORT across the column labels, LONG down the row labels."""
    width = n_cols * CELL_W - 0.04
    ax.add_patch(Rectangle((x0 + 0.02, top + BAR_GAP), width, BAR,
                           facecolor=SHORT_COLOUR, edgecolor="none"))
    ax.text(x0 + 0.02 + width / 2, top + BAR_GAP + BAR / 2, "SHORT  (sell at bid)",
            fontsize=7.5, fontweight="bold", color=PAGE, ha="center", va="center")

    height = n_rows * CELL_H - 0.06
    left = x0 - BAR_GAP - BAR
    ax.add_patch(Rectangle((left, top - n_rows * CELL_H + 0.03), BAR, height,
                           facecolor=LONG_COLOUR, edgecolor="none"))
    ax.text(left + BAR / 2, top - n_rows * CELL_H + 0.03 + height / 2,
            "LONG  (buy at ask)", fontsize=7.5, fontweight="bold", color=PAGE,
            ha="center", va="center", rotation=90)


def _scale_key(ax, fig_w: float, fig_h: float) -> None:
    """One swatch per band, so a colour can be read back as a number."""
    sw_w, sw_h = 0.30, 0.15
    steps = sf.RAMP_UP[1:]
    lx, ly = fig_w - PAD_X - len(steps) * sw_w, fig_h - 0.52
    for k, colour in enumerate(steps):
        ax.add_patch(Rectangle((lx + k * sw_w, ly), sw_w, sw_h, facecolor=colour,
                               edgecolor=PAGE, lw=0.6))
        if k == len(steps) - 1:
            # The top band is open-ended: there is no boundary to tick.
            ax.text(lx + (k + 0.5) * sw_w, ly - 0.10,
                    f"{sf.FULL_COLOUR * 100:.0f}%+", fontsize=7, color=MUTED,
                    ha="center", va="center")
            continue
        edge = sf.GREY_BAND + k * sf.BAND_STEP
        ax.text(lx + k * sw_w, ly - 0.10, f"{edge * 100:.0f}", fontsize=7,
                color=MUTED, ha="center", va="center")
    ax.text(lx - 0.14, ly + sw_h / 2, "yield %/yr", fontsize=7.5, color=MUTED,
            ha="right", va="center")


def _footnotes(ax, fy: float) -> None:
    ax.text(PAD_X, fy, "each cell = buy the row leg, sell the column leg; yield is that spread annualized",
            fontsize=8.6, color=MUTED, va="center")
    ax.text(PAD_X, fy - 0.22, "yield  /  spread (points)  /  days to the farther expiry",
            fontsize=8.6, color=MUTED, va="center")
    ax.add_patch(Rectangle((PAD_X + 0.02, fy - 0.53), 0.22, 0.13, facecolor="none",
                           edgecolor=CROSS_EDGE, lw=1.8))
    ax.text(PAD_X + 0.34, fy - 0.465,
            "outlined = COMEX leg against TFEX leg (cross-market)",
            fontsize=8.6, color=MUTED, va="center")


# --------------------------------------------------------------------------- #
# Post
# --------------------------------------------------------------------------- #
def chat_space() -> str | None:
    try:
        with open(ENV_FILE, encoding="utf-8") as fh:
            for line in fh:
                key, _, value = line.partition("=")
                if key.strip() == SPACE_KEY and value.strip():
                    return value.strip().split("/")[-1]
    except OSError as exc:
        logger.warning("Cannot read %s (%s).", ENV_FILE, exc)
    return None


def post(space: str, text: str, png: str | None = None) -> str:
    session = AuthorizedSession(Credentials.from_authorized_user_file(CHAT_TOKEN))
    body: dict = {"text": text}

    if png:
        with open(png, "rb") as fh:
            blob = fh.read()
        name = os.path.basename(png)
        up = session.post(
            f"https://chat.googleapis.com/upload/v1/spaces/{space}"
            "/attachments:upload?uploadType=multipart",
            files={
                "metadata": (None, json.dumps({"filename": name}), "application/json"),
                "file": (name, blob, "image/png"),
            },
            timeout=HTTP_TIMEOUT,
        )
        up.raise_for_status()
        body["attachment"] = [{
            "contentName": name,
            "contentType": "image/png",
            "attachmentDataRef": up.json()["attachmentDataRef"],
        }]

    res = session.post(f"https://chat.googleapis.com/v1/spaces/{space}/messages",
                       json=body, timeout=HTTP_TIMEOUT)
    res.raise_for_status()
    return res.json()["name"]


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #
def expected_legs(legs: list[tuple[str, int, str]], day: date) -> list[str]:
    """Legs that should have quoted: spot, plus contracts Front still shows.

    Uses the same ROLL_OFF_DAYS cut-off as Front, so a leg is expected exactly
    while it is on the matrix. Without the cut-off the report would go silent
    the first time a contract rolled off — the broker stops quoting it a day
    or more before its TTM, so a run could never be 'complete' again.
    """
    horizon = day + timedelta(days=sf.ROLL_OFF_DAYS)
    live = []
    for name, _, ttm in legs:
        if not ttm:
            live.append(name)
            continue
        try:
            if datetime.strptime(ttm.strip(), "%Y-%m-%d").date() >= horizon:
                live.append(name)
        except ValueError:
            logger.warning("Leg %s has an unreadable TTM %r — treating it as live.", name, ttm)
            live.append(name)
    return live


def send(spreadsheet, missing: list[str], stamp: str, out_dir: str) -> bool:
    """Post the report, or a note naming what is missing. True if anything went."""
    space = chat_space()
    if not space:
        logger.warning("No %s in %s — not posting.", SPACE_KEY, ENV_FILE)
        return False

    if missing:
        # Silence would be worse: nobody can tell a quiet day from a dead feed.
        # Send the reason rather than a half-empty matrix.
        logger.warning("Incomplete snapshot (%s) — posting a note instead.",
                       ", ".join(missing))
        post(space, f"*Gold carry {stamp}* — report held back, no quote for: "
                    f"{', '.join(missing)}")
        return True

    png = os.path.join(out_dir, "front.png")
    render(read_front(spreadsheet), png, read_prices(spreadsheet), read_swap(spreadsheet))
    logger.info("Posted %s to spaces/%s.", post(space, f"*Gold carry* — Front, {stamp}", png),
                space)
    return True


def main() -> int:
    from simple_heatmap import load_config, setup_logger, sheets_client

    parser = argparse.ArgumentParser(description="Render Front and post it to Google Chat.")
    parser.add_argument("--out", metavar="PATH", help="Write the PNG here and post nothing.")
    args = parser.parse_args()

    cfg = load_config()
    setup_logger(cfg["log_file"])
    try:
        book = sheets_client().open_by_key(cfg["spreadsheet_id"])
        grid = read_front(book)
        if args.out:
            logger.info("Wrote %s.", render(grid, args.out, read_prices(book), read_swap(book)))
            return 0
        send(book, [], grid[0][0], os.path.dirname(os.path.abspath(__file__)))
        return 0
    except Exception:
        logger.exception("Report failed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
