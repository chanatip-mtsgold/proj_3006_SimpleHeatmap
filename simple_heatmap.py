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
from datetime import datetime, timedelta, timezone
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


def get_worksheet(cfg: dict):
    client = gspread.authorize(_load_credentials())
    sh = client.open_by_key(cfg["spreadsheet_id"])
    name = (cfg.get("sheet_name") or "").strip()
    ws = sh.worksheet(name) if name else sh.sheet1
    logger.info("Opened worksheet '%s'.", ws.title)
    return ws


META_HEADERS = {"DATE", "TIME", "TIMESTAMP", "DATETIME"}


def read_layout(ws) -> tuple[list[str], list[tuple[str, int]]]:
    """Parse the two header rows.

    Leading columns whose row-1 name is a meta header (Date / Time / Timestamp)
    are the timestamp block; every other non-empty row-1 cell starts a symbol
    that owns that column (BID) and the next one (ASK).

    Returns (meta_headers_upper, [(symbol, bid_col)]) with 1-based columns.
    """
    row1 = ws.row_values(1)

    meta: list[str] = []
    col = 1
    while col <= len(row1) and row1[col - 1].strip().upper() in META_HEADERS:
        meta.append(row1[col - 1].strip().upper())
        col += 1

    layout: list[tuple[str, int]] = []
    while col <= len(row1):
        name = row1[col - 1].strip()
        if name:
            layout.append((name, col))
            col += 2  # this symbol owns the BID and ASK columns
        else:
            col += 1

    return meta, layout


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
def fetch_quotes(
    mt5_path: str, symbols: list[str], target_utc: datetime, lookback_minutes: int
) -> dict:
    """Snapshot each symbol at `target_utc` — the last tick at or before it.

    copy_ticks_range interprets a naive datetime as MACHINE-LOCAL time and
    converts it to UTC internally, so the target is handed over as local
    wall-clock. Tick times come back as genuine UTC epochs (the MT5 feed is
    GMT+0). Returns {symbol: (bid, ask, tick_utc)}; symbols with no usable
    tick are absent.
    """
    if not mt5.initialize(path=mt5_path):
        raise RuntimeError(f"mt5.initialize failed: {mt5.last_error()}")

    naive_to = target_utc.astimezone().replace(tzinfo=None)
    naive_from = naive_to - timedelta(minutes=lookback_minutes)

    quotes: dict[str, tuple[float, float, datetime]] = {}
    try:
        for sym in symbols:
            if not mt5.symbol_select(sym, True):
                logger.warning("symbol_select('%s') failed: %s", sym, mt5.last_error())
                continue

            ticks = mt5.copy_ticks_range(sym, naive_from, naive_to, mt5.COPY_TICKS_ALL)
            if ticks is None or len(ticks) == 0:
                logger.warning(
                    "No ticks for '%s' in the %d min before %s UTC: %s",
                    sym,
                    lookback_minutes,
                    target_utc.strftime("%Y-%m-%d %H:%M:%S"),
                    mt5.last_error(),
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
    finally:
        mt5.shutdown()

    return quotes


def resolve_target(cfg: dict, date_arg: str | None) -> tuple[datetime, datetime]:
    """Return (target_local, target_utc) for the configured snapshot time-of-day."""
    tz = ZoneInfo(cfg.get("timezone", "Asia/Bangkok"))
    hh, mm = (int(p) for p in cfg.get("snapshot_time", "10:00").split(":"))

    day = (
        datetime.strptime(date_arg, "%Y-%m-%d").date() if date_arg else datetime.now(tz).date()
    )
    target_local = datetime.combine(day, dtime(hh, mm), tzinfo=tz)

    if target_local > datetime.now(tz):
        raise RuntimeError(f"Snapshot time {target_local:%Y-%m-%d %H:%M %Z} is in the future.")

    return target_local, target_local.astimezone(timezone.utc)


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


def append_row(ws, row: list) -> str:
    # Rows 1-2 are the two header rows, so data always starts at row 3.
    target = max(len(ws.col_values(1)) + 1, FIRST_DATA_ROW)
    rng = f"A{target}:{rowcol_to_a1(target, len(row))}"
    ws.update(values=[row], range_name=rng, value_input_option="USER_ENTERED")
    return rng


# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="Append an MT5 bid/ask snapshot to Google Sheets.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Read the layout and quotes but do not write."
    )
    parser.add_argument(
        "--date", metavar="YYYY-MM-DD", help="Backfill this date instead of today."
    )
    args = parser.parse_args()

    cfg = load_config()
    setup_logger(cfg["log_file"])

    try:
        ws = get_worksheet(cfg)

        meta, layout = read_layout(ws)
        if not layout:
            logger.error("No symbols found in row 1 of '%s'. Nothing to do.", ws.title)
            return 1
        logger.info("Meta columns: %s", ", ".join(meta) or "(none)")
        logger.info("Symbols: %s", ", ".join(f"{s}@col{c}" for s, c in layout))

        target_local, target_utc = resolve_target(cfg, args.date)
        logger.info(
            "Snapshot at %s local = %s UTC (MT5 feed time).",
            target_local.strftime("%Y-%m-%d %H:%M:%S"),
            target_utc.strftime("%Y-%m-%d %H:%M:%S"),
        )

        quotes = fetch_quotes(
            cfg["mt5_path"],
            [s for s, _ in layout],
            target_utc,
            cfg.get("lookback_minutes", 3),
        )
        if not quotes:
            logger.error("No quotes retrieved from MT5. Not writing a row.")
            return 1

        row = build_row(meta, layout, quotes, target_local)

        if args.dry_run:
            logger.info("[dry-run] Would append: %s", row)
            return 0

        ensure_header_row2(ws, layout)
        rng = append_row(ws, row)
        logger.info("Appended %d/%d symbols to %s.", len(quotes), len(layout), rng)
        return 0

    except Exception:
        logger.exception("Run failed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
