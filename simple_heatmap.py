"""proj_3006 — SimpleHeatmap

Snapshot the current bid/ask of every symbol listed in row 1 of the target
Google Sheet and append them as one new row (one run = one row).

Sheet layout (row 1 drives everything — add/remove symbols there, not here):

        A            B         C        D         E
row 1   Timestamp    XAUUSD             EURUSD
row 2                BID       ASK      BID       ASK
row 3   2026-09-04 10:00:01   3421.15  3421.45  1.0832  1.0833   <- appended

MT5 runs on GMT+0; the timestamp written to the sheet is Thai time (GMT+7).
"""

import argparse
import json
import logging
import os
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

import gspread
import MetaTrader5 as mt5
from dotenv import dotenv_values
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials as UserCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
from gspread.utils import rowcol_to_a1

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
GITHUB_DIR = os.path.normpath(os.path.join(PROJECT_DIR, ".."))
CRED_DIR = os.path.join(GITHUB_DIR, ".cred")
PATH_ENV = os.path.join(GITHUB_DIR, ".env", "Path.env")

AUTH_USER_FILE = os.path.join(CRED_DIR, "authorized_user.json")
CLIENT_SECRET_FILE = os.path.join(CRED_DIR, "client_secret.json")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

DEFAULT_MT5_PATH = r"C:\Program Files\MTS Capital MetaTrader 5\terminal64.exe"
FIRST_DATA_ROW = 3  # rows 1-2 are the symbol / BID-ASK header rows

logger = logging.getLogger("simple_heatmap")


def setup_logger(log_file: str) -> None:
    log_dir = os.path.join(PROJECT_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    logger.setLevel(logging.INFO)

    fh = RotatingFileHandler(
        os.path.join(log_dir, log_file), maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Under pythonw there is no console, so sys.stdout is None - and a bare
    # StreamHandler would fall back to sys.stderr, which is None too, and throw
    # on the first log line. The file handler is the one that matters for a
    # scheduled run; the console one is for when someone is watching.
    if sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)


def load_config() -> dict:
    with open(os.path.join(PROJECT_DIR, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)

    env = dotenv_values(PATH_ENV) if os.path.exists(PATH_ENV) else {}
    cfg["mt5_path"] = env.get("MT5_PATH") or DEFAULT_MT5_PATH
    return cfg


# --------------------------------------------------------------------------- #
# Google Sheets
# --------------------------------------------------------------------------- #
def _load_credentials() -> UserCredentials:
    """Reuse the repo-wide OAuth user token; fall back to a browser flow."""
    creds = None
    if os.path.exists(AUTH_USER_FILE):
        try:
            creds = UserCredentials.from_authorized_user_file(AUTH_USER_FILE, scopes=SCOPES)
        except Exception as exc:
            logger.warning("Could not load %s: %s", AUTH_USER_FILE, exc)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:
            logger.warning("Token refresh failed (%s); falling back to OAuth flow.", exc)
            creds = None

    if not creds or not creds.valid:
        # Needs a console + browser. Task Scheduler runs will fail here on purpose.
        flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
        creds = flow.run_local_server(port=0)

    with open(AUTH_USER_FILE, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    return creds


# Task Scheduler kills the run at ExecutionTimeLimit, which is a hard kill
# with no log line and no regard for what the run was in the middle of. These
# keep every blocking call well inside that window, so a stall ends as a
# logged failure of our own instead.
HTTP_TIMEOUT = (10, 30)  # (connect, read) seconds, per request
MT5_TIMEOUT_MS = 30_000


def sheets_client():
    client = gspread.authorize(_load_credentials())
    client.set_timeout(HTTP_TIMEOUT)
    return client


def get_worksheet(cfg: dict):
    sh = sheets_client().open_by_key(cfg["spreadsheet_id"])
    name = (cfg.get("sheet_name") or "").strip()
    ws = sh.worksheet(name) if name else sh.sheet1
    logger.info("Opened worksheet '%s'.", ws.title)
    return ws


META_HEADERS = {"DATE", "TIME", "TIMESTAMP", "DATETIME"}


def parse_header(row1: list[str]) -> tuple[list[str], list[tuple[str, int, str]]]:
    """The one place that knows how `Data` row 1 is laid out.

    Leading columns whose name is a meta header (Date / Time / Timestamp) are
    the timestamp block; every other non-empty cell starts a symbol that owns
    that column (BID) and the next one (ASK), with its TTM sitting in that
    second column of row 1. A symbol with no TTM is the spot leg.

    Returns (meta_headers_upper, [(symbol, bid_col, ttm)]), columns 1-based.
    """
    meta: list[str] = []
    col = 1
    while col <= len(row1) and row1[col - 1].strip().upper() in META_HEADERS:
        meta.append(row1[col - 1].strip().upper())
        col += 1

    legs: list[tuple[str, int, str]] = []
    while col <= len(row1):
        name = row1[col - 1].strip()
        if name:
            ttm = row1[col].strip() if col < len(row1) else ""
            legs.append((name, col, ttm))
            col += 2  # this symbol owns the BID and ASK columns
        else:
            col += 1

    return meta, legs


def read_layout(ws) -> tuple[list[str], list[tuple[str, int]]]:
    """(meta_headers, [(symbol, bid_col)]) — the daily job does not need TTMs."""
    meta, legs = parse_header(ws.row_values(1))
    return meta, [(name, col) for name, col, _ in legs]


def ensure_header_row2(ws, layout: list[tuple[str, int]]) -> None:
    """Write BID/ASK sub-headers if row 2 is missing any of them. Idempotent."""
    row2 = ws.row_values(2)

    def cell(idx: int) -> str:
        return row2[idx - 1].strip().upper() if idx - 1 < len(row2) else ""

    if all(cell(c) == "BID" and cell(c + 1) == "ASK" for _, c in layout):
        return

    last_col = layout[-1][1] + 1
    values = [""] * last_col
    for _, c in layout:
        values[c - 1] = "BID"
        values[c] = "ASK"

    rng = f"A2:{rowcol_to_a1(2, last_col)}"
    ws.update(values=[values], range_name=rng)
    logger.info("Wrote BID/ASK sub-headers to %s.", rng)


# --------------------------------------------------------------------------- #
# MetaTrader 5
# --------------------------------------------------------------------------- #
@contextmanager
def mt5_session(mt5_path: str):
    """Attach to the already-running, already-logged-in terminal."""
    if not mt5.initialize(path=mt5_path, timeout=MT5_TIMEOUT_MS):
        raise RuntimeError(f"mt5.initialize failed: {mt5.last_error()}")
    try:
        yield
    finally:
        mt5.shutdown()


def fetch_quotes(symbols: list[str], target_utc: datetime, lookback_minutes: int) -> dict:
    """Snapshot each symbol at `target_utc` — the last tick at or before it.

    Call inside an open `mt5_session`.

    copy_ticks_range interprets a naive datetime as MACHINE-LOCAL time and
    converts it to UTC internally, so the target is handed over as local
    wall-clock. Tick times come back as genuine UTC epochs (the MT5 feed is
    GMT+0). Returns {symbol: (bid, ask, tick_utc)}; symbols with no usable
    tick are absent.
    """
    naive_to = target_utc.astimezone().replace(tzinfo=None)
    naive_from = naive_to - timedelta(minutes=lookback_minutes)

    quotes: dict[str, tuple[float, float, datetime]] = {}
    for sym in symbols:
        if not mt5.symbol_select(sym, True):
            logger.warning("symbol_select('%s') failed: %s", sym, mt5.last_error())
            continue

        ticks = mt5.copy_ticks_range(sym, naive_from, naive_to, mt5.COPY_TICKS_ALL)
        if ticks is None or len(ticks) == 0:
            logger.warning(
                "No ticks for '%s' in the %d min before %s UTC.",
                sym,
                lookback_minutes,
                target_utc.strftime("%Y-%m-%d %H:%M:%S"),
            )
            continue

        # copy_ticks_range is time-ordered; walk back to the newest usable tick.
        for t in reversed(ticks):
            bid, ask = float(t["bid"]), float(t["ask"])
            if bid > 0 and ask > 0:
                quotes[sym] = (bid, ask, datetime.fromtimestamp(t["time"], tz=timezone.utc))
                break
        else:
            logger.warning(
                "Only zero-priced ticks for '%s' before %s UTC.",
                sym,
                target_utc.strftime("%Y-%m-%d %H:%M:%S"),
            )

    return quotes


def fetch_live(symbols: list[str]) -> dict:
    """Whatever the terminal is showing right now, for the live dashboard.

    Call inside an open `mt5_session`. Unlike fetch_quotes this reads the
    current tick rather than history, so there is no window and nothing to
    annualise against - the caller supplies the timestamp. Same return shape:
    {symbol: (bid, ask, tick_utc)}, absent when there is no usable tick.
    """
    quotes: dict[str, tuple[float, float, datetime]] = {}
    for sym in symbols:
        if not mt5.symbol_select(sym, True):
            logger.warning("symbol_select('%s') failed: %s", sym, mt5.last_error())
            continue
        tick = mt5.symbol_info_tick(sym)
        if tick is None or tick.bid <= 0 or tick.ask <= 0:
            logger.warning("No live tick for '%s'.", sym)
            continue
        quotes[sym] = (
            float(tick.bid),
            float(tick.ask),
            datetime.fromtimestamp(tick.time, tz=timezone.utc),
        )
    return quotes


# --------------------------------------------------------------------------- #
# Which days still need a row
# --------------------------------------------------------------------------- #
def clock_times(cfg: dict) -> list[dtime]:
    """The times of day to snapshot, earliest first."""
    raw = cfg.get("snapshot_times") or [cfg.get("snapshot_time", "10:00")]
    return sorted(dtime(*(int(p) for p in t.split(":"))) for t in raw)


def snapshot_times(cfg: dict, day: date, at: dtime) -> tuple[datetime, datetime]:
    """Return (local, utc) datetimes for one snapshot slot."""
    tz = ZoneInfo(cfg.get("timezone", "Asia/Bangkok"))
    local = datetime.combine(day, at, tzinfo=tz)
    return local, local.astimezone(timezone.utc)


def existing_slots(ws) -> set[tuple[str, str]]:
    """(date, HH:MM) already logged, from the Date and Time columns.

    Keyed on the pair, not the date: with more than one snapshot a day, a date
    alone would make the second slot look like a duplicate of the first.
    """
    dates = ws.col_values(1)[FIRST_DATA_ROW - 1 :]
    times = ws.col_values(2)[FIRST_DATA_ROW - 1 :]
    times += [""] * (len(dates) - len(times))
    return {(d.strip(), t.strip()[:5]) for d, t in zip(dates, times) if d.strip()}


def pending_slots(cfg: dict, ws, date_arg: str | None) -> list[tuple[date, dtime]]:
    """Slots whose time has passed but that have no row yet.

    A skipped slot (PC off, task disabled, MT5 down) is picked up by the next
    successful run instead of being lost, and re-running never duplicates one
    that is already in the sheet.
    """
    tz = ZoneInfo(cfg.get("timezone", "Asia/Bangkok"))
    now = datetime.now(tz)
    have = existing_slots(ws)
    clock = clock_times(cfg)

    if date_arg:
        days = [datetime.strptime(date_arg, "%Y-%m-%d").date()]
    else:
        max_back = cfg.get("max_backfill_days", 10)
        earliest = now.date() - timedelta(days=max_back)
        # Resume from the newest date present; clamp so a long gap (or an empty
        # sheet) does not reach past the terminal's tick history. Starting ON
        # the newest date rather than after it matters now that a date can be
        # half-filled - the 10:00 row logged while 21:00 is still to come.
        newest = max((datetime.strptime(d, "%Y-%m-%d").date() for d, _ in have), default=None)
        start = max(newest, earliest) if newest else earliest
        days = [
            start + timedelta(days=i)
            for i in range((now.date() - start).days + 1)
            if (start + timedelta(days=i)).weekday() < 5  # skip Sat/Sun
        ]

    pending = []
    for day in days:
        for at in clock:
            key = (day.strftime("%Y-%m-%d"), at.strftime("%H:%M"))
            if key in have:
                logger.info("%s %s already logged — skipping.", *key)
                continue
            if snapshot_times(cfg, day, at)[0] > now:
                logger.info("%s %s has not come round yet — skipping.", *key)
                continue
            pending.append((day, at))

    return pending


# --------------------------------------------------------------------------- #
# Append
# --------------------------------------------------------------------------- #
def build_row(
    meta: list[str], layout: list[tuple[str, int]], quotes: dict, target_local: datetime
) -> list:
    last_col = layout[-1][1] + 1
    row: list = [""] * last_col

    # The timestamp is the snapshot time we asked for, not the moment we ran.
    stamp = {
        "DATE": target_local.strftime("%Y-%m-%d"),
        "TIME": target_local.strftime("%H:%M:%S"),
        "TIMESTAMP": target_local.strftime("%Y-%m-%d %H:%M:%S"),
        "DATETIME": target_local.strftime("%Y-%m-%d %H:%M:%S"),
    }
    for i, name in enumerate(meta):
        row[i] = stamp[name]

    for sym, col in layout:
        q = quotes.get(sym)
        if q is None:
            continue  # leave blank rather than 0 so the heatmap ignores it
        bid, ask, tick_utc = q
        row[col - 1] = bid
        row[col] = ask
        logger.info("%s  bid=%s ask=%s  (tick %s UTC)", sym, bid, ask, tick_utc)

    return row


def append_rows(ws, rows: list[list]) -> str:
    # Rows 1-2 are the two header rows, so data always starts at row 3.
    start = max(len(ws.col_values(1)) + 1, FIRST_DATA_ROW)
    end = start + len(rows) - 1
    rng = f"A{start}:{rowcol_to_a1(end, len(rows[0]))}"
    ws.update(values=rows, range_name=rng, value_input_option="USER_ENTERED")
    return rng


# --------------------------------------------------------------------------- #
def report_latest(ws, legs, slot) -> None:
    """Post the newest slot to Chat. Front only ever shows the last row, so
    back-filling several slots at once still produces one report, for the last.

    A failure here is logged and swallowed: the numbers are already in the
    sheet, and losing them to a Chat outage would be the worse trade.
    """
    import report  # deferred: report imports this module, and pulls in matplotlib

    day, at, quotes = slot
    stamp = f"{day:%Y-%m-%d} {at:%H:%M}"
    try:
        missing = [s for s in report.expected_legs(legs, day) if s not in quotes]
        report.send(ws.spreadsheet, missing, stamp, os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        logger.exception("Posting the %s report failed; the sheet is still correct.", stamp)


def main() -> int:
    parser = argparse.ArgumentParser(description="Append an MT5 bid/ask snapshot to Google Sheets.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Read the layout and quotes but do not write."
    )
    parser.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        help="Log only this date (weekend/back-fill window checks aside), instead of "
        "auto-filling every missing weekday.",
    )
    args = parser.parse_args()

    cfg = load_config()
    setup_logger(cfg["log_file"])

    try:
        ws = get_worksheet(cfg)

        # parse_header rather than read_layout: the report needs the TTMs to
        # tell a contract that has rolled off from one that simply failed.
        meta, legs = parse_header(ws.row_values(1))
        layout = [(name, col) for name, col, _ in legs]
        if not layout:
            logger.error("No symbols found in row 1 of '%s'. Nothing to do.", ws.title)
            return 1
        logger.info("Meta columns: %s", ", ".join(meta) or "(none)")
        logger.info("Symbols: %s", ", ".join(f"{s}@col{c}" for s, c in layout))

        slots = pending_slots(cfg, ws, args.date)
        if not slots:
            logger.info("Nothing to log — the sheet is already up to date.")
            return 0

        symbols = [s for s, _ in layout]
        lookback = cfg.get("lookback_minutes", 3)
        rows: list[list] = []
        logged: list[tuple[date, dtime, dict]] = []

        with mt5_session(cfg["mt5_path"]):
            for day, at in slots:
                target_local, target_utc = snapshot_times(cfg, day, at)
                logger.info(
                    "Snapshot at %s local = %s UTC (MT5 feed time).",
                    target_local.strftime("%Y-%m-%d %H:%M:%S"),
                    target_utc.strftime("%Y-%m-%d %H:%M:%S"),
                )

                quotes = fetch_quotes(symbols, target_utc, lookback)
                if not quotes:
                    # Market holiday, or tick history no longer reaches back
                    # this far. Skip the slot rather than log an empty row.
                    logger.warning("No quotes at all for %s %s — skipping.", day, at)
                    continue

                rows.append(build_row(meta, layout, quotes, target_local))
                logged.append((day, at, quotes))
                logger.info("%s %s: %d/%d symbols.", day, at, len(quotes), len(layout))

        if not rows:
            logger.error("No quotes retrieved for any pending slot. Nothing written.")
            return 1

        if args.dry_run:
            for r in rows:
                logger.info("[dry-run] Would append: %s", r)
            return 0

        ensure_header_row2(ws, layout)
        rng = append_rows(ws, rows)
        logger.info("Appended %d row(s) to %s.", len(rows), rng)
        report_latest(ws, legs, logged[-1])
        return 0

    except Exception:
        logger.exception("Run failed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
