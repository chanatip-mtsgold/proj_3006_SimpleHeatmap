@echo OFF

:: ============================================================================
:: proj_3006 — SimpleHeatmap launcher (uv)
:: Register this .bat in Windows Task Scheduler.
:: Cadence: ONCE DAILY, weekdays, 10:00 Thai time (= 03:00 GMT on the MT5 feed).
:: Requires the MT5 terminal to be already running and logged in.
:: ============================================================================

set "PROJECT_DIR=C:\Users\%USERNAME%\Documents\GitHub\proj_3006_SimpleHeatmap"

cd /d "%PROJECT_DIR%"

:: --frozen = use uv.lock exactly; fail loudly if it has drifted from pyproject.toml
uv run --frozen python simple_heatmap.py %*
