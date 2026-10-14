"""proj_3006 — render dashboard.html from the accumulated `Data` sheet.

The same numbers as the `Front` tab, plus the one thing a spreadsheet cannot
show: how each yield has moved since logging began.

Colour here is continuous rather than the seven steps conditional formatting
is limited to. It uses the same scale and the same anchor colours as `Front`
(imported from setup_front, so the two cannot drift), interpolated between
them: flat neutral inside +/-GREY_BAND, fully saturated at +/-FULL_COLOUR.
That matters because most pairs sit inside a single band on the sheet - here
4.08% and 5.98% are visibly different.

    uv run python dashboard.py              # the last row logged at 10:00
    uv run python dashboard.py --snapshot   # priced off the terminal right now
"""

import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import plotly.graph_objects as go

from setup_front import (
    CATEGORICAL,
    FULL_COLOUR,
    GREY_BAND,
    DAY_COUNT,
    NEUTRAL,
    RAMP_DOWN,
    RAMP_UP,
    SUPPRESSED,
    VENUE_COLOURS,
    VENUE_NAMES,
)
from simple_heatmap import (
    PROJECT_DIR,
    fetch_live,
    get_worksheet,
    load_config,
    logger,
    mt5_session,
    parse_header,
    setup_logger,
)

OUT = Path(PROJECT_DIR) / "dashboard.html"
FIRST_DATA_ROW = 3
HISTORY_DAYS = 30  # most recent snapshots shown on the history chart


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Leg:
    name: str
    col: int  # 1-based BID column; ASK is the next one
    ttm: date | None  # None marks the spot leg

    @property
    def venue(self) -> int:
        """0 TFEX, 1 exchange-prefixed. Spot answers for whoever it is paired with."""
        return 1 if self.name.upper().startswith("CMX.") else 0


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float


@dataclass(frozen=True)
class Pair:
    x: Leg
    y: Leg
    spread: float
    gap: int  # days between the two expiries; signed
    near_days: int  # time left on whichever leg expires first
    far_days: int  # time left on whichever leg expires last - the yield's term
    yld: float | None  # None once the gap is too short to annualise

    @property
    def venue_class(self) -> int:
        if self.x.ttm is None:
            return self.y.venue
        return self.x.venue if self.x.venue == self.y.venue else 2


def parse_date(text: str) -> date | None:
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def read_sheet(cfg: dict) -> tuple[list[Leg], list[tuple[date, dict[str, Quote]]]]:
    ws = get_worksheet(cfg)
    values = ws.get_all_values()
    if len(values) < FIRST_DATA_ROW:
        raise RuntimeError(f"'{ws.title}' has no data rows yet.")

    _, header = parse_header(values[0])
    legs = [Leg(name, col, parse_date(ttm)) for name, col, ttm in header]

    history = []
    for row in values[FIRST_DATA_ROW - 1 :]:
        when = parse_date(row[0]) if row else None
        if when is None:
            continue
        quotes = {}
        for leg in legs:
            try:
                bid, ask = float(row[leg.col - 1]), float(row[leg.col])
            except (IndexError, ValueError):
                continue  # a leg that was not quoted that day
            if bid > 0 and ask > 0:
                quotes[leg.name] = Quote(bid, ask)
        if quotes:
            history.append((when, quotes))

    history.sort(key=lambda h: h[0])
    logger.info("Read %d legs over %d days.", len(legs), len(history))
    return legs, history


def annualised(y_bid: float, x_ask: float, gap: int, far_days: int) -> float | None:
    """Yield over the days left to the far leg - the same rule as Front, which
    no longer suppresses pairs whose expiries are close together."""
    return (y_bid / x_ask - 1) * DAY_COUNT / max(far_days, 1)


def build_pairs(legs: list[Leg], when: date, quotes: dict[str, Quote]) -> list[Pair]:
    """Every leg against every later leg: sell Y, buy X."""
    quoted = [leg for leg in legs if leg.name in quotes]
    out = []
    for i, x in enumerate(quoted):
        for y in quoted[i + 1 :]:
            # Spot behaves as a leg expiring today, which is what makes the
            # spot row fall out of the same rule as every other pair.
            tx = x.ttm or when
            ty = y.ttm or when
            gap = (ty - tx).days
            far_days = (max(tx, ty) - when).days
            spread = quotes[y.name].bid - quotes[x.name].ask
            out.append(
                Pair(
                    x=x,
                    y=y,
                    spread=spread,
                    gap=gap,
                    near_days=min((t - when).days for t in (tx, ty) if t != when)
                    if (tx != when or ty != when)
                    else 0,
                    far_days=far_days,
                    yld=annualised(quotes[y.name].bid, quotes[x.name].ask, gap, far_days),
                )
            )
    return out


# --------------------------------------------------------------------------- #
# Colour
# --------------------------------------------------------------------------- #
def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def shade(yld: float | None) -> str:
    """Continuous diverging scale over the same anchors the sheet steps through."""
    if yld is None:
        return SUPPRESSED
    size = abs(yld)
    if size <= GREY_BAND:
        return NEUTRAL

    t = min((size - GREY_BAND) / (FULL_COLOUR - GREY_BAND), 1.0)
    anchors = RAMP_UP if yld > 0 else RAMP_DOWN
    # Walk the anchor list rather than interpolating end to end, so the ramp
    # inherits the luminance matching that was validated step by step.
    pos = t * (len(anchors) - 1)
    i = min(int(pos), len(anchors) - 2)
    lo, hi, f = _hex_to_rgb(anchors[i]), _hex_to_rgb(anchors[i + 1]), pos - i
    return "#%02x%02x%02x" % tuple(round(a + (b - a) * f) for a, b in zip(lo, hi))


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #
STYLES = """
  :root { --ink:#1a1a1a; --navy:#1a2a4a; --page:#f5f5f7; --line:#e3e3e6;
          --muted:#6b6b70; }
  body { margin:0; background:var(--page); color:var(--ink);
         font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
  .wrap { max-width:1280px; margin:32px auto; padding:0 16px; }
  .header { padding:20px 24px; border-radius:8px; background:var(--navy);
            color:#fff; margin-bottom:20px; }
  .header h1 { margin:0 0 4px; font-size:22px; }
  .header .meta { opacity:.85; font-size:14px; }
  .snapshot { font-size:11px; letter-spacing:.08em; vertical-align:middle;
              background:#e34948; padding:3px 8px; border-radius:10px;
              margin-left:8px; }
  .header .formula { opacity:.85; font-size:13px; margin-top:8px; line-height:1.6;
                     font-family: ui-monospace, Consolas, monospace; }
  h2 { font-size:16px; margin:28px 0 10px; }
  .card { background:#fff; border-radius:8px; padding:12px; }
  table { border-collapse:separate; border-spacing:2px; font-size:13px; }
  th { font-weight:600; text-align:center; padding:6px 8px; white-space:nowrap; }
  th.row { text-align:right; }
  td { padding:6px 8px; text-align:center; white-space:nowrap; border-radius:3px;
       font-variant-numeric: tabular-nums; }
  td .sub { color:var(--muted); font-size:11px; }
  .scroll { overflow-x:auto; }
  .note { font-size:12px; color:var(--muted); margin-top:10px; line-height:1.7; }
  .swatches { display:flex; align-items:center; gap:0; margin-top:12px; }
  .swatches i { width:26px; height:14px; display:block; }
  .swatches span { font-size:11px; color:var(--muted); padding:0 8px; }
  .charts { display:flex; flex-wrap:wrap; gap:16px; }
  /* Explicit height is load-bearing: the plotly div inside is height:100%,
     which only resolves against a sized parent. */
  .chart { flex:1 1 460px; min-width:0; height:420px; background:#fff;
           border-radius:8px; padding:8px; box-sizing:border-box; }
"""

# Plotly measures its container at first paint; inside a flex box that can come
# back 0 and it keeps its 700px default forever, overflowing the panel.
RESIZE = """
  addEventListener('load', function () {
    if (!window.Plotly) return;
    document.querySelectorAll('.plotly-graph-div').forEach(Plotly.Plots.resize);
  });
"""


def render_matrix(legs: list[Leg], pairs: list[Pair]) -> str:
    by_key = {(p.x.name, p.y.name): p for p in pairs}
    names = [leg.name for leg in legs]
    rows, cols = names[:-1], names[1:]

    head = "".join(f"<th>{c}</th>" for c in cols)
    body = []
    for r in rows:
        cells = []
        for c in cols:
            p = by_key.get((r, c))
            if p is None:
                cells.append("<td></td>")
                continue
            top = "&mdash;" if p.yld is None else f"{p.yld * 100:.2f}%"
            cells.append(
                f'<td style="background:{shade(p.yld)}">{top}'
                f'<div class="sub">{p.spread:+.2f} &middot; {p.far_days}d</div></td>'
            )
        body.append(f'<tr><th class="row">{r}</th>{"".join(cells)}</tr>')

    legend = "".join(
        f'<i style="background:{shade(v)}"></i>'
        for v in [x / 1000 for x in range(-90, 91, 5)]
    )
    return f"""<div class="card scroll">
<table><thead><tr><th></th>{head}</tr></thead>
<tbody>{"".join(body)}</tbody></table>
<div class="swatches"><span>-9%</span>{legend}<span>+9%</span></div>
<div class="note">
  Cell = <b>yield</b> over the days left to the farther leg, then the dollar
  spread and that same day count &mdash; spread / price &times; 360 / days gives
  the yield back.
</div></div>"""


def scatter(pairs: list[Pair]) -> go.Figure:
    fig = go.Figure()
    for cls, (name, colour) in enumerate(zip(VENUE_NAMES, VENUE_COLOURS)):
        sel = [p for p in pairs if p.venue_class == cls and p.yld is not None]
        fig.add_trace(
            go.Scatter(
                x=[p.far_days for p in sel],
                y=[p.yld * 100 for p in sel],
                mode="markers",
                name=name,
                marker={"size": 10, "color": colour},
                text=[f"{p.y.name} &minus; {p.x.name}" for p in sel],
                hovertemplate="%{text}<br>%{y:.2f}%/yr over %{x} days<extra></extra>",
            )
        )
    # abs(): a negative gap only means the column leg expires first, which is
    # where the pair sits in the triangle, not the window's length.
    fig.update_layout(
        title="Implied carry by term",
        xaxis_title="Days between the two expiries",
        yaxis_title="Yield (%/yr)",
        template="plotly_white",
        margin={"l": 60, "r": 20, "t": 50, "b": 50},
        legend={"orientation": "h", "y": -0.2},
    )
    return fig


def history_chart(legs: list[Leg], history: list) -> go.Figure:
    # Only the tail: `Data` keeps growing, and a year of daily points would
    # squash the recent moves that anyone is actually reading the chart for.
    history = history[-HISTORY_DAYS:]
    spot = next((leg for leg in legs if leg.ttm is None), None)
    fig = go.Figure()
    if spot is None:
        return fig

    contracts = [leg for leg in legs if leg.ttm is not None]
    for slot, leg in enumerate(contracts):
        xs, ys = [], []
        for when, quotes in history:
            if leg.name not in quotes or spot.name not in quotes:
                continue
            y = annualised(quotes[leg.name].bid, quotes[spot.name].ask, (leg.ttm - when).days)
            if y is not None:
                xs.append(when)
                ys.append(y * 100)
        if xs:
            fig.add_trace(
                go.Scatter(
                    x=xs,
                    y=ys,
                    mode="lines+markers",
                    name=leg.name,
                    # Hue identifies the contract; the dash carries the venue,
                    # so that reading never depends on colour alone.
                    line={
                        "width": 2,
                        "dash": "solid" if leg.venue else "dot",
                        "color": CATEGORICAL[slot % len(CATEGORICAL)],
                    },
                    marker={"color": CATEGORICAL[slot % len(CATEGORICAL)]},
                )
            )

    fig.update_layout(
        title=f"Spot-referenced yield — last {len(history)} snapshots",
        xaxis_title="Snapshot date",
        yaxis_title="Yield (%/yr)",
        template="plotly_white",
        margin={"l": 60, "r": 20, "t": 50, "b": 50},
        legend={"orientation": "h", "y": -0.2},
    )
    return fig


def render(legs: list[Leg], history: list, snap: tuple | None = None) -> str:
    """`snap` is (timestamp, quotes) straight off the terminal; None uses the sheet."""
    when, quotes = snap if snap else history[-1]
    pairs = build_pairs(legs, when.date() if snap else when, quotes)
    spot = next((leg.name for leg in legs if leg.ttm is None), "spot")

    # Both modes write the same filename and a snapshot goes stale within
    # minutes, so the page has to say which one it is. The sheet mode says
    # "logged" rather than "snapshot" so the two words cannot be confused.
    badge = '<span class="snapshot">SNAPSHOT</span>' if snap else ""
    stamp = f"{when:%Y-%m-%d %H:%M:%S}" if snap else f"Logged {when:%Y-%m-%d}"

    charts = [scatter(pairs), history_chart(legs, history)]
    panels = "".join(
        # Only the first panel embeds plotly.js; the bundle is ~4.7MB.
        f'<div class="chart">'
        + fig.to_html(full_html=False, include_plotlyjs=(i == 0), config={"responsive": True})
        + "</div>"
        for i, fig in enumerate(charts)
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>SimpleHeatmap</title>
<style>{STYLES}</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <h1>Gold carry &mdash; pairwise matrix {badge}</h1>
    <div class="meta">{stamp} &middot; spot leg {spot}
      &middot; {len(legs)} legs, {len(pairs)} pairs</div>
    <div class="formula">
      spread = Y<sub>bid</sub> &minus; X<sub>ask</sub> &nbsp;(sell Y, buy X)<br>
      yield&nbsp; = (Y<sub>bid</sub> / X<sub>ask</sub> &minus; 1) &times; 360 / days to the far leg
      &nbsp;&nbsp;gap = TTM<sub>Y</sub> &minus; TTM<sub>X</sub>, spot counted as
      expiring on the snapshot date
    </div>
  </div>

  <h2>Matrix</h2>
  {render_matrix(legs, pairs)}

  <h2>Charts</h2>
  <div class="charts">{panels}</div>
</div>
<script>{RESIZE}</script>
</body>
</html>
"""


def take_snapshot(cfg: dict, legs: list[Leg]) -> tuple:
    """One read of the terminal's current ticks, stamped with the local time."""
    names = [leg.name for leg in legs]
    with mt5_session(cfg["mt5_path"]):
        ticks = fetch_live(names)
    if not ticks:
        raise RuntimeError("MT5 returned no quotes at all.")

    logger.info("Live quotes for %d/%d legs.", len(ticks), len(names))
    tz = ZoneInfo(cfg.get("timezone", "Asia/Bangkok"))
    return datetime.now(tz), {n: Quote(bid, ask) for n, (bid, ask, _) in ticks.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description="Render dashboard.html.")
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help="Price the matrix off the terminal's ticks as of now, instead of "
        "the last row logged at 10:00. One render. Needs MT5 running.",
    )
    args = parser.parse_args()

    cfg = load_config()
    setup_logger(cfg["log_file"])
    try:
        legs, history = read_sheet(cfg)
        snap = take_snapshot(cfg, legs) if args.snapshot else None
        OUT.write_text(render(legs, history, snap), encoding="utf-8")
        logger.info("Wrote %s (%.0f KB).", OUT, OUT.stat().st_size / 1024)
        return 0
    except Exception:
        logger.exception("Dashboard failed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
