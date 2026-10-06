"""Factory-aging HMI control core.

UI processes should call :class:`FactoryController`; the package itself has no
desktop-framework dependency and keeps high-rate control in a worker thread.
"""

from .core import (
    FakeMotorBackend,
    FactoryController,
    FactoryService,
    LinuxMotorsPyBackend,
    MotorBackend,
    MotorZeroingSession,
    PlaybackController,
    StationStateMachine,
    TrajectoryValidationError,
    build_station_config,
    diagnose_faults,
    import_trajectory,
    load_station_config,
    preflight_trajectory,
)
from .models import (
    FaultDiagnosis,
    FaultStatus,
    MotorSample,
    PlaybackOptions,
    PlaybackProgress,
    PreflightReport,
    SafetyLimits,
    StationConfig,
    StationState,
    TrajectoryData,
    ZeroingState,
)

__all__ = [
    "FakeMotorBackend",
    "FactoryController",
    "FactoryService",
    "FaultDiagnosis",
    "FaultStatus",
    "LinuxMotorsPyBackend",
    "MotorBackend",
    "MotorSample",
    "MotorZeroingSession",
    "PlaybackController",
    "PlaybackOptions",
    "PlaybackProgress",
    "PreflightReport",
    "SafetyLimits",
    "StationConfig",
    "StationState",
    "StationStateMachine",
    "TrajectoryData",
    "TrajectoryValidationError",
    "ZeroingState",
    "build_station_config",
    "diagnose_faults",
    "import_trajectory",
    "load_station_config",
    "preflight_trajectory",
]
