"""Onboarding subpackage — FTUE, sample tasks, walkthroughs (PLATFORM §17)."""
from agent_system.onboarding.ftue import (
    FTUEStep,
    FTUESampleTask,
    FTUEState,
    FTUEManager,
    DEFAULT_SAMPLES,
    get_ftue_manager,
)

__all__ = [
    "FTUEStep", "FTUESampleTask", "FTUEState", "FTUEManager",
    "DEFAULT_SAMPLES", "get_ftue_manager",
]
