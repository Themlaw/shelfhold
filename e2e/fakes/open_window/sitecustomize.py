"""The maintenance window of the image under test, open all day (E2E only).

The periodic verifications (`reconcile.cycle`, ENF-OPS-014) run inside 03:00-07:00 Europe/Paris
and a job asked for by hand waits for the next night, so a journey that needs one would pass or
fail according to the hour the CI starts it. Mounted read-only and put on PYTHONPATH by
journey.yaml: the image itself is the one that ships, only the bounds of the window differ.
"""

from datetime import time

import shelfhold.jobs.window as window

window.DEFAULT_WINDOW = window.MaintenanceWindow(start=time(0, 0), end=time(23, 59, 59))
