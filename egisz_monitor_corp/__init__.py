"""EGISZ corporate monitoring: parse LOGTEXT, normalize, load PostgreSQL."""

from egisz_monitor_corp.etl import run_sync
from egisz_monitor_corp.parser import EgiszMonitorParser

__all__ = ["EgiszMonitorParser", "run_sync"]
