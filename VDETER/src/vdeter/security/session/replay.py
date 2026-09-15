"""Two-phase 64-packet sliding replay window, adapted from the feature branch."""

from dataclasses import dataclass

from ...common.errors import ErrorCode
from ..constants import MAX_COUNTER, REPLAY_WINDOW
from ..errors import SecurityFault


@dataclass(frozen=True, slots=True)
class ReplaySnapshot:
    highest_counter: int
    replay_bitmap: int


class ReplayWindow:
    def __init__(self, highest_counter: int = 0, replay_bitmap: int = 0):
        if (type(highest_counter) is not int or not 0 <= highest_counter <= MAX_COUNTER
                or type(replay_bitmap) is not int or not 0 <= replay_bitmap < (1 << REPLAY_WINDOW)
                or (highest_counter == 0) != (replay_bitmap == 0)):
            raise ValueError("Invalid replay snapshot")
        self.highest_counter = highest_counter
        self.replay_bitmap = replay_bitmap

    def precheck(self, counter: int) -> None:
        if type(counter) is not int or not 1 <= counter <= MAX_COUNTER:
            raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_COUNTER_INVALID)
        if counter > self.highest_counter:
            return
        delta = self.highest_counter - counter
        if delta >= REPLAY_WINDOW:
            raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_REPLAY_TOO_OLD)
        if self.replay_bitmap & (1 << delta):
            raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_REPLAY_DUPLICATE)

    def candidate(self, counter: int) -> ReplaySnapshot:
        self.precheck(counter)
        if counter > self.highest_counter:
            shift = counter - self.highest_counter
            bitmap = 1 if shift >= REPLAY_WINDOW else ((self.replay_bitmap << shift) | 1) & ((1 << REPLAY_WINDOW) - 1)
            return ReplaySnapshot(counter, bitmap)
        return ReplaySnapshot(self.highest_counter, self.replay_bitmap | (1 << (self.highest_counter - counter)))

    def commit(self, snapshot: ReplaySnapshot) -> None:
        self.highest_counter, self.replay_bitmap = snapshot.highest_counter, snapshot.replay_bitmap

    def snapshot(self) -> ReplaySnapshot:
        return ReplaySnapshot(self.highest_counter, self.replay_bitmap)
