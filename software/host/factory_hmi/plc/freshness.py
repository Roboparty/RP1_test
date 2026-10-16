"""Session-local evidence of actual PS1 status progress, independent of TCP polls."""

from .models import PlcStatus


class Ps1FeedbackTracker:
    TIMEOUT_SECONDS = 5.0

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.cycle: int | None = None
        self.sequence: int | None = None
        self.last_progress: float | None = None
        self.invalidated = True
        self.reason = "尚未观察到本会话PS1真实状态推进"
        self.main_ready: bool | None = None
        self.power_lost = False

    def observe(self, status: PlcStatus, now: float) -> bool:
        """Return True on a cycle change or counter regression (possible CPU restart)."""
        cycle, sequence = status.ps1_power_cycle, status.ps1_status_sequence
        boundary = self.cycle is not None and self.cycle != cycle
        power_loss = self.main_ready is True and not status.main_ready
        self.main_ready = status.main_ready
        if power_loss:
            self.power_lost = True
        if boundary and status.main_ready:
            self.power_lost = False
        delta = None if self.sequence is None else (sequence - self.sequence) & 0xFFFF
        regression = not boundary and delta is not None and delta >= 0x8000
        if self.cycle is None or boundary or regression or power_loss:
            self.cycle, self.sequence = cycle, sequence
            self.last_progress = None
            self.invalidated = True
            self.reason = "上电周期/状态序号变化：等待新的PS1状态确认"
        elif delta:
            self.sequence = sequence
            self.last_progress = now - status.ps1_status_age_ms / 1000.0 if status.ps1_status_valid else None
            self.invalidated = not status.ps1_status_valid
            self.reason = "" if not self.invalidated else "PLC未确认PS1状态有效"
        if not status.ps1_status_valid or self.power_lost:
            self.invalidated = True
            self.reason = "PS1状态无效或主回路未就绪"
        return boundary or regression or power_loss

    def fresh(self, status: PlcStatus | None, now: float) -> bool:
        return bool(status and status.ps1_status_valid and not self.power_lost
                    and status.ps1_power_cycle == self.cycle
                    and status.ps1_status_sequence == self.sequence
                    and not self.invalidated and self.last_progress is not None
                    and 0 <= now - self.last_progress < self.TIMEOUT_SECONDS)

    def age_ms(self, now: float) -> int | None:
        return None if self.last_progress is None else max(0, int((now - self.last_progress) * 1000))
