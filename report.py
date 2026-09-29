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

# The space id lives beside the other per-machine settings rather than in
# config.json, which is committed. The key name is the constant, not the id.
ENV_FILE = os.path.join(GITHUB_DIR, ".env", "Path.env")
SPACE_KEY = "HeatmapSpace"

# Posting needs the chat.messages.create scope, which the shared
# authorized_user.json does not carry — token.json is the one that does.
CHAT_TOKEN = os.path.join(GITHUB_DIR, ".cred", "token.json")

# The corner cell plus one row and one column per leg.
GRID_SPAN = sf.SPAN + 1

TFEX_RE = re.compile(r"^GO[A-Z]\d\d$")
CELL_RE = re.compile(r"(?:([\d.]+)%\s*)?\(([-+]?[\d.]+)\)\s*\((\d+)d\)")

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
    rows = ws.get(f"A1:{last}{sf.FIRST_ROW + sf.SPAN - 1}")
    rows = [r + [""] * (GRID_SPAN - len(r)) for r in rows]
    rows += [[""] * GRID_SPAN for _ in range(GRID_SPAN - len(rows))]

    # Keep a column or row only if at least one of its cells is priced. That
    # drops Front's unused tail (it is built out to MAX_LEGS) and also a leg
    # that has a slot but no quotes yet - on the sheet it is a placeholder, in
    # a report it would only be an empty stripe.
    keep_cols = [
        j for j in range(1, GRID_SPAN)
        if rows[0][j].strip() and any(rows[i][j].strip() for i in range(1, GRID_SPAN))
    ]
    keep_rows = [
        i for i in range(1, GRID_SPAN)
        if rows[i][0].strip() and any(rows[i][j].strip() for j in keep_cols)
    ]
    header = [rows[0][0]] + [rows[0][j] for j in keep_cols]
    body = [[rows[i][0]] + [rows[i][j] for j in keep_cols] for i in keep_rows]
    return [header] + body


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
HDR_W, HDR_H = 1.15, 0.5
PAD_X, FOOT_H = 0.28, 1.80


def render(grid: list[list[str]], path: str) -> str:
    stamp = grid[0][0]
    cols = [c.replace("CMX.", "") for c in grid[0][1:]]
    rows = [r[0].replace("CMX.", "") for r in grid[1:]]
    col_venue, row_venue = [venue(c) for c in cols], [venue(r) for r in rows]

    fig_w = (HDR_W + len(cols) * CELL_W) * 1.05
    fig_h = HDR_H + len(rows) * CELL_H + FOOT_H
    fig = plt.figure(figsize=(fig_w, fig_h), dpi=160)
    fig.patch.set_facecolor(PAGE)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    ax.set_xlim(0, fig_w)
    ax.set_ylim(0, fig_h)

    top = fig_h - 0.95
    ax.text(PAD_X, fig_h - 0.42, "Gold carry", fontsize=15, fontweight="bold",
            color=CROSS_EDGE, va="center")
    ax.text(PAD_X + 1.62, fig_h - 0.44, "Front  -  " + stamp, fontsize=9.5,
            color=MUTED, va="center")

    _scale_key(ax, fig_w, fig_h)

    x0 = PAD_X + HDR_W
    for j, name in enumerate(cols):
        ax.text(x0 + (j + 0.5) * CELL_W, top + 0.16, name, fontsize=9.5,
                fontweight="bold", color=INK, ha="center", va="center")

    for i, name in enumerate(rows):
        y = top - (i + 1) * CELL_H
        ax.text(x0 - 0.12, y + CELL_H / 2, name, fontsize=9.5, fontweight="bold",
                color=INK, ha="right", va="center")
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
    ax.text(PAD_X, fy, "each cell = annualized carry of the column leg over the row leg",
            fontsize=8.6, color=MUTED, va="center")
    ax.text(PAD_X, fy - 0.22, "yield  /  spread (points)  /  days to the nearer expiry",
            fontsize=8.6, color=MUTED, va="center")
    ax.add_patch(Rectangle((PAD_X + 0.02, fy - 0.53), 0.22, 0.13, facecolor="none",
                           edgecolor=CROSS_EDGE, lw=1.8))
    ax.text(PAD_X + 0.34, fy - 0.465,
            "outlined = COMEX leg against TFEX leg (cross-market)",
            fontsize=8.6, color=MUTED, va="center")
    ax.text(PAD_X, fy - 0.70,
            f"grey = the two legs expire under {sf.MIN_GAP} days apart, "
            "so the yield is not annualizable",
            fontsize=8.6, color=FAINT, va="center")


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
    render(read_front(spreadsheet), png)
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
            logger.info("Wrote %s.", render(grid, args.out))
            return 0
        send(book, [], grid[0][0], os.path.dirname(os.path.abspath(__file__)))
        return 0
    except Exception:
        logger.exception("Report failed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
