"""Edge detection for a polled key (pure Python, testable anywhere).

ScreenPilot never hooks the keyboard. When it needs Esc outside its own
windows (during a recording, a countdown or an interval capture) it polls
that single key's current state; :class:`KeyLatch` turns those samples into
one "pressed" event per press.
"""

from __future__ import annotations

from typing import Callable


class KeyLatch:
    """Reports a press once, on the up → down transition.

    A key already held when the latch is created (or reset) does not count,
    so the Esc that closed a dialog is not read again as a new press.
    """

    def __init__(self, read: Callable[[], bool]) -> None:
        self._read = read
        self.down = bool(read())

    def poll(self) -> bool:
        down = bool(self._read())
        pressed = down and not self.down
        self.down = down
        return pressed

    def reset(self) -> None:
        self.down = bool(self._read())
