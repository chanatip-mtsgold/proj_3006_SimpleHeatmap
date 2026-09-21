@echo OFF

:: ============================================================================
:: proj_3006 — SimpleHeatmap, manual run.
::
:: The scheduled task does NOT use this file. A .bat always flashes a cmd
:: window and `uv run` opens its own console on top, so the task calls
:: .venv\Scripts\pythonw.exe directly instead - see SimpleHeatmap_task.xml.
:: This is here for running it by hand, where seeing the output is the point.
::
:: Requires the MT5 terminal to be already running and logged in.
:: ============================================================================

set "PROJECT_DIR=C:\Users\%USERNAME%\Documents\GitHub\proj_3006_SimpleHeatmap"

cd /d "%PROJECT_DIR%"

:: --frozen = use uv.lock exactly; fail loudly if it has drifted from pyproject.toml
uv run --frozen python simple_heatmap.py %*
