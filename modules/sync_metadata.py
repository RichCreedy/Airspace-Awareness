"""
sync_metadata.py
----------------
Normalizes heterogeneous freshness/status info from multiple modules
(GPS, ADS-B poller, OpenAIP remote sync, FRZ regen, manual NOTAM
import) into a single SourceStatus shape, for consumption by
freshness_badge.py (the top-bar widget + detail popup).

This module holds NO state of its own beyond registered adapter
callbacks — it's a pure aggregation/normalization layer, computed
fresh on each get_summary() call.
"""

import time
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional, List, Dict, Any

logger = logging.getLogger("sync_metadata")


class Status(Enum):
    OK = "ok"
    STALE = "stale"
    ERROR = "error"
    NEVER = "never"       # no successful sync/fix has ever occurred
    DISABLED = "disabled"  # source intentionally turned off in config


# Ordering used to compute "worst" overall status for the badge color.
_SEVERITY = {
    Status.OK: 0,
    Status.DISABLED: 0,   # disabled sources shouldn't drag the badge red
    Status.STALE: 1,
    Status.NEVER: 2,
    Status.ERROR: 3,
}


@dataclass
class SourceStatus:
    name: str                      # e.g. "gps", "adsb", "openaip:essex_notams"
    category: str                  # e.g. "gps" | "adsb" | "remote_zone" | "frz" | "manual_import"
    status: Status
    age_s: Optional[float] = None
    last_success_at: Optional[float] = None
    last_error: Optional[str] = None
    message: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def humanized_age(self) -> str:
        return humanize_age(self.age_s)


def humanize_age(age_s: Optional[float]) -> str:
    """'3m ago' / '2h ago' / 'never' style formatting for popups."""
    if age_s is None:
        return "never"
    if age_s < 0:
        age_s = 0
    if age_s < 60:
        return f"{int(age_s)}s ago"
    if age_s < 3600:
        return f"{int(age_s // 60)}m ago"
    if age_s < 86400:
        return f"{int(age_s // 3600)}h ago"
    return f"{int(age_s // 86400)}d ago"


# ---------------------------------------------------------------------
# Generic status computation
# ---------------------------------------------------------------------

def compute_status(
    last_success_at: Optional[float],
    last_attempt_at: Optional[float],
    last_error: Optional[str],
    refresh_interval_s: Optional[float],
    stale_multiplier: float = 2.0,
    disabled: bool = False,
) -> Status:
    """
    Shared decision logic:
      - DISABLED short-circuits everything.
      - NEVER succeeded at all -> NEVER.
      - Most recent attempt was AFTER most recent success AND failed
        -> ERROR (i.e. we are *currently* failing, not just stale).
      - Otherwise, judge staleness purely on age vs refresh_interval.
    """
    if disabled:
        return Status.DISABLED

    if last_success_at is None:
        return Status.NEVER

    now = time.time()
    currently_failing = (
        last_error is not None
        and last_attempt_at is not None
        and last_attempt_at >= last_success_at
    )
    if currently_failing:
        return Status.ERROR

    if refresh_interval_s is None:
        return Status.OK  # event-based source with at least one success

    age_s = now - last_success_at
    if age_s > refresh_interval_s * stale_multiplier:
        return Status.STALE

    return Status.OK


# ---------------------------------------------------------------------
# Per-source-type adapters
# ---------------------------------------------------------------------
# Each adapter takes whatever raw status dict/object that module
# already exposes, and returns one or more SourceStatus entries.
# Add new adapters here as new modules gain get_status() methods.

def adapt_gps_fix(gps_fix: Optional[dict], expected_interval_s: float = 2.0) -> SourceStatus:
    """
    Adapts Fusion.get_gps_fix() output.
    Shape: {lat, lon, altitude_m, heading, speed_mps, fix_quality,
            last_seen, last_seen_iso, age_s, stale}  (no `last_error`)
    """
    if gps_fix is None:
        return SourceStatus(name="gps", category="gps", status=Status.NEVER)

    age_s = gps_fix.get("age_s")
    stale = gps_fix.get("stale", False)
    fix_quality = gps_fix.get("fix_quality")

    if age_s is None:
        status = Status.NEVER
    elif stale:
        status = Status.STALE
    else:
        status = Status.OK

    return SourceStatus(
        name="gps",
        category="gps",
        status=status,
        age_s=age_s,
        last_success_at=gps_fix.get("last_seen"),
        last_error=None,
        message=f"fix_quality={fix_quality}",
        extra={"last_seen_iso": gps_fix.get("last_seen_iso")},
    )


def adapt_adsb_poll(last_success_at: Optional[float], last_error: Optional[str],
                     poll_interval_s: float = 2.0) -> SourceStatus:
    """Adapts a simple tar1090 poller status (age-based, periodic)."""
    now = time.time()
    status = compute_status(
        last_success_at=last_success_at,
        last_attempt_at=now,          # poller ticks constantly; treat "now" as latest attempt
        last_error=last_error,
        refresh_interval_s=poll_interval_s,
    )
    age_s = (now - last_success_at) if last_success_at else None

    return SourceStatus(
        name="adsb",
        category="adsb",
        status=status,
        age_s=age_s,
        last_success_at=last_success_at,
        last_error=last_error,
        message="tar1090 aircraft.json poll",
    )


def adapt_openaip_source(raw: dict, disabled: bool = False) -> SourceStatus:
    """
    Adapts one entry from OpenAIPSyncManager.get_status(), i.e.:
    {name, last_attempt_at, last_success_at, last_error,
     backoff_s, next_attempt_in_s, zone_count_cached}
    """
    # refresh_interval_s isn't directly in this dict today — derive a
    # conservative estimate from next_attempt_in_s + backoff_s when
    # healthy, else fall back to a generic default. If you want exact
    # staleness thresholds, add `refresh_interval_s` to
    # OpenAIPSync.get_status()'s returned dict.
    refresh_interval_s = raw.get("refresh_interval_s", 3600)

    status = compute_status(
        last_success_at=raw.get("last_success_at"),
        last_attempt_at=raw.get("last_attempt_at"),
        last_error=raw.get("last_error"),
        refresh_interval_s=refresh_interval_s,
        disabled=disabled,
    )

    age_s = (time.time() - raw["last_success_at"]) if raw.get("last_success_at") else None

    return SourceStatus(
        name=f"openaip:{raw.get('name', 'unknown')}",
        category="remote_zone",
        status=status,
        age_s=age_s,
        last_success_at=raw.get("last_success_at"),
        last_error=raw.get("last_error"),
        message=f"{raw.get('zone_count_cached', 0)} zones cached",
        extra={
            "backoff_s": raw.get("backoff_s"),
            "next_attempt_in_s": raw.get("next_attempt_in_s"),
        },
    )


def adapt_frz_regen(last_regen_at: Optional[float], last_error: Optional[str]) -> SourceStatus:
    """
    FRZ regen is event-based (triggered by mtime change), not periodic —
    so we never mark it STALE purely on age. It's OK if it's ever
    succeeded and isn't currently erroring, NEVER if it hasn't run yet,
    ERROR if the last attempt failed.
    """
    if last_regen_at is None and last_error is None:
        status = Status.NEVER
    elif last_error is not None:
        status = Status.ERROR
    else:
        status = Status.OK

    age_s = (time.time() - last_regen_at) if last_regen_at else None

    return SourceStatus(
        name="frz_regen",
        category="frz",
        status=status,
        age_s=age_s,
        last_success_at=last_regen_at,
        last_error=last_error,
        message="regenerated on source file change" if status == Status.OK else "",
    )


def adapt_manual_import(last_import_at: Optional[float], last_error: Optional[str],
                         imported_count: int = 0) -> SourceStatus:
    """Manual NOTAM import — also event-based, not periodic."""
    if last_import_at is None and last_error is None:
        status = Status.NEVER
    elif last_error is not None:
        status = Status.ERROR
    else:
        status = Status.OK

    age_s = (time.time() - last_import_at) if last_import_at else None

    return SourceStatus(
        name="manual_import",
        category="manual_import",
        status=status,
        age_s=age_s,
        last_success_at=last_import_at,
        last_error=last_error,
        message=f"{imported_count} zone(s) from manual import",
    )


# ---------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------

class SyncMetadataAggregator:
    """
    Register zero-argument callables that each return a SourceStatus
    (or list of SourceStatus, for multi-source modules like OpenAIP).
    Call get_summary() on demand from the freshness badge widget.
    """

    def __init__(self):
        self._providers: List[Callable[[], Any]] = []

    def register(self, provider: Callable[[], Any]) -> None:
        """
        provider() must return either a SourceStatus or a
        List[SourceStatus] (for modules with multiple sub-sources).
        """
        self._providers.append(provider)

    def get_summary(self) -> Dict[str, Any]:
        sources: List[SourceStatus] = []

        for provider in self._providers:
            try:
                result = provider()
            except Exception as e:
                logger.warning(f"[sync_metadata] provider raised: {e}")
                continue

            if isinstance(result, list):
                sources.extend(result)
            elif result is not None:
                sources.append(result)

        worst = Status.OK
        for s in sources:
            if _SEVERITY[s.status] > _SEVERITY[worst]:
                worst = s.status

        return {
            "overall_status": worst.value,
            "generated_at": time.time(),
            "sources": [
                {
                    "name": s.name,
                    "category": s.category,
                    "status": s.status.value,
                    "age_s": s.age_s,
                    "age_human": s.humanized_age(),
                    "last_error": s.last_error,
                    "message": s.message,
                    "extra": s.extra,
                }
                for s in sources
            ],
        }
