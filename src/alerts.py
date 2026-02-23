from __future__ import annotations

import os
import platform
import sys


def play_trade_alert(enabled: bool) -> None:
    if not enabled:
        return

    # Terminal bell fallback.
    sys.stdout.write("\a")
    sys.stdout.flush()

    system = platform.system().lower()
    if "darwin" in system:
        os.system("afplay /System/Library/Sounds/Glass.aiff >/dev/null 2>&1 &")
    elif "windows" in system:
        try:
            import winsound

            winsound.Beep(880, 300)
        except Exception:
            pass
