"""Background health monitoring for MekiCopy-owned companion processes.

The desktop UI must never make a blocking health request.  ``CompanionWatchdog``
therefore runs probes on a daemon thread and publishes recovery requests through
an ordinary :class:`queue.Queue`.  The owning UI can drain that queue from its
own event loop, decide how to terminate/restart the child, and then acknowledge
the outcome.

This module deliberately has no Tk or MekiCopy imports.  It can also be used by
command-line launchers and is safe to unit-test without starting child
processes.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol


class ProcessHandle(Protocol):
    """The small portion of ``subprocess.Popen`` used by the watchdog."""

    def poll(self) -> int | None:
        """Return ``None`` while the child is alive, otherwise its exit code."""


HealthEvaluator = Callable[[Mapping[str, Any]], str | None]
"""Return ``None`` for a usable health payload, otherwise a failure detail."""


@dataclass(frozen=True)
class HealthProbeResult:
    """A single ``/health`` probe outcome.

    ``payload`` is retained for diagnostics only.  Callers should not mutate it.
    A service can report an application-level ``ERROR`` state while its process
    and control endpoint are still healthy, so the default evaluator validates
    identity and responsiveness rather than treating every state as a crash.
    """

    healthy: bool
    detail: str = ""
    payload: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class RecoveryRequest:
    """A request for the owner to recover one tracked companion.

    ``generation`` protects against stale queued requests.  Before changing a
    child, owners should use :meth:`CompanionWatchdog.claim_recovery`: it
    atomically rejects a request whose target was replaced, untracked, or
    recovered before the UI got a chance to act.
    """

    name: str
    generation: int
    reason: str
    detail: str
    consecutive_failures: int
    observed_at: float
    exit_code: int | None = None


@dataclass(frozen=True)
class WatchdogStatus:
    """A read-only current state snapshot for diagnostics/UI logging."""

    name: str
    generation: int
    tracked: bool
    recovering: bool
    consecutive_failures: int
    last_detail: str
    next_recovery_at: float


@dataclass
class _Target:
    name: str
    expected_app: str
    base_url: str
    process: ProcessHandle
    evaluator: HealthEvaluator | None
    generation: int
    consecutive_failures: int = 0
    recovering: bool = False
    recovery_claimed: bool = False
    next_recovery_at: float = 0.0
    last_detail: str = ""
    startup_grace_until: float = 0.0


def default_health_evaluator(
    expected_app: str,
    *,
    require_ui_responsive: bool = False,
    max_ui_heartbeat_age_seconds: float | None = None,
) -> HealthEvaluator:
    """Build the default identity/UI responsiveness policy.

    GUI companions may add ``uiResponsive`` and
    ``uiHeartbeatAgeSeconds`` to their health JSON.  The fields are only
    required when requested, which keeps older/external companion versions
    compatible with normal process monitoring.
    """

    max_age = (
        None
        if max_ui_heartbeat_age_seconds is None
        else max(0.0, float(max_ui_heartbeat_age_seconds))
    )

    def evaluate(payload: Mapping[str, Any]) -> str | None:
        if payload.get("ok") is not True:
            return "health endpoint did not return ok=true"
        actual_app = str(payload.get("app") or "")
        if actual_app != expected_app:
            return f"expected {expected_app}, received {actual_app or 'unknown service'}"

        ui_responsive = payload.get("uiResponsive")
        if ui_responsive is False:
            return "GUI event loop is not responding"
        if require_ui_responsive and ui_responsive is not True:
            return "health endpoint did not report GUI responsiveness"

        if max_age is not None and "uiHeartbeatAgeSeconds" in payload:
            try:
                age = float(payload["uiHeartbeatAgeSeconds"])
            except (TypeError, ValueError):
                return "health endpoint reported an invalid GUI heartbeat age"
            if age > max_age:
                return f"GUI heartbeat is stale ({age:.1f}s)"
        return None

    return evaluate


def probe_health(
    base_url: str,
    *,
    timeout_seconds: float = 0.75,
    evaluator: HealthEvaluator | None = None,
) -> HealthProbeResult:
    """Make one bounded loopback ``/health`` request without raising errors."""

    url = f"{base_url.rstrip('/')}/health"
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=max(0.05, float(timeout_seconds))) as response:
            raw = response.read().decode("utf-8")
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
        return HealthProbeResult(False, f"health request failed: {exc}")
    except Exception as exc:  # Defensive: a broken transport must not kill the watchdog.
        return HealthProbeResult(False, f"health request failed: {exc}")

    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        return HealthProbeResult(False, f"health response is not JSON: {exc}")
    if not isinstance(payload, dict):
        return HealthProbeResult(False, "health response is not a JSON object")

    if evaluator is not None:
        try:
            detail = evaluator(payload)
        except Exception as exc:
            return HealthProbeResult(False, f"health policy failed: {exc}", payload)
        if detail:
            return HealthProbeResult(False, str(detail), payload)
    return HealthProbeResult(True, payload=payload)


class CompanionWatchdog:
    """Monitor owned child processes and request bounded recovery.

    The watchdog never starts, terminates, or touches a UI process itself.
    ``track`` should only receive children owned by the caller; this makes
    automatic recovery safe when another MekiCopy suite happens to be running
    on a nearby port.  A dead tracked child creates an immediate request.  A
    live process needs ``failures_before_recovery`` consecutive failed health
    probes, avoiding restarts from one transient busy response.
    """

    def __init__(
        self,
        *,
        interval_seconds: float = 3.0,
        health_timeout_seconds: float = 0.75,
        failures_before_recovery: int = 3,
        cooldown_seconds: float = 20.0,
        startup_grace_seconds: float = 0.0,
        health_probe: Callable[..., HealthProbeResult] = probe_health,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if health_timeout_seconds <= 0:
            raise ValueError("health_timeout_seconds must be positive")
        if failures_before_recovery < 1:
            raise ValueError("failures_before_recovery must be at least 1")
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds cannot be negative")
        if startup_grace_seconds < 0:
            raise ValueError("startup_grace_seconds cannot be negative")
        self.interval_seconds = float(interval_seconds)
        self.health_timeout_seconds = float(health_timeout_seconds)
        self.failures_before_recovery = int(failures_before_recovery)
        self.cooldown_seconds = float(cooldown_seconds)
        self.startup_grace_seconds = float(startup_grace_seconds)
        self._health_probe = health_probe
        self._clock = clock
        self._lock = threading.RLock()
        self._targets: dict[str, _Target] = {}
        self._events: queue.Queue[RecoveryRequest] = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_generation = 1

    def track(
        self,
        name: str,
        *,
        expected_app: str,
        base_url: str,
        process: ProcessHandle,
        evaluator: HealthEvaluator | None = None,
    ) -> int:
        """Start monitoring an owned process and return its generation.

        Repeating the same registration is intentionally idempotent, so a UI
        can refresh its launch bookkeeping without resetting a failure streak.
        Replacing the process, URL, expected identity, or policy creates a new
        generation and makes any older queued recovery request stale.
        """

        clean_name = str(name).strip()
        clean_app = str(expected_app).strip()
        clean_url = str(base_url).rstrip("/")
        if not clean_name:
            raise ValueError("name is required")
        if not clean_app:
            raise ValueError("expected_app is required")
        if not clean_url.startswith(("http://", "https://")):
            raise ValueError("base_url must be an HTTP(S) URL")
        if process is None:
            raise ValueError("process is required; untrack instead when no child is owned")

        with self._lock:
            previous = self._targets.get(clean_name)
            if (
                previous is not None
                and previous.process is process
                and previous.expected_app == clean_app
                and previous.base_url == clean_url
                and previous.evaluator is evaluator
            ):
                return previous.generation
            generation = self._next_generation
            self._next_generation += 1
            self._targets[clean_name] = _Target(
                name=clean_name,
                expected_app=clean_app,
                base_url=clean_url,
                process=process,
                evaluator=evaluator,
                generation=generation,
                startup_grace_until=self._clock() + self.startup_grace_seconds,
            )
            return generation

    def untrack(self, name: str) -> None:
        """Stop monitoring a child (normally before intentional shutdown)."""

        with self._lock:
            self._targets.pop(str(name), None)

    def is_current(self, request: RecoveryRequest) -> bool:
        """Whether a queued request still belongs to the tracked process."""

        with self._lock:
            target = self._targets.get(request.name)
            return bool(target and target.generation == request.generation)

    def claim_recovery(self, request: RecoveryRequest) -> bool:
        """Atomically reserve a queued recovery request for its owner.

        A companion can recover naturally after the watchdog queued a request
        but before Tk drains it.  In that case :meth:`_record_success` clears
        the unclaimed request and this method returns ``False``.  Once claimed,
        a later probe cannot cancel a recovery already being performed by the
        owner.
        """

        with self._lock:
            target = self._targets.get(request.name)
            if (
                target is None
                or target.generation != request.generation
                or not target.recovering
                or target.recovery_claimed
            ):
                return False
            target.recovery_claimed = True
            return True

    def matches_process(self, request: RecoveryRequest, process: ProcessHandle | None) -> bool:
        """Whether ``process`` is still the owned child named by a request."""

        with self._lock:
            target = self._targets.get(request.name)
            return bool(
                target
                and target.generation == request.generation
                and target.process is process
            )

    def acknowledge_recovery(self, request: RecoveryRequest, *, succeeded: bool) -> bool:
        """Release a recovery request after the owner attempted its action.

        Failed recovery attempts retain the failure streak and obey the
        configured cooldown before the next request.  Successful recovery
        clears it; callers commonly follow this with ``track`` for the new
        process handle.
        """

        with self._lock:
            target = self._targets.get(request.name)
            if target is None or target.generation != request.generation:
                return False
            target.recovering = False
            target.recovery_claimed = False
            if succeeded:
                target.consecutive_failures = 0
                target.last_detail = ""
                target.next_recovery_at = 0.0
            return True

    def drain_recovery_requests(self) -> list[RecoveryRequest]:
        """Return every pending request without blocking the caller."""

        requests: list[RecoveryRequest] = []
        while True:
            try:
                requests.append(self._events.get_nowait())
            except queue.Empty:
                return requests

    def status(self, name: str) -> WatchdogStatus:
        """Return a diagnostic snapshot; an untracked name is represented too."""

        with self._lock:
            target = self._targets.get(str(name))
            if target is None:
                return WatchdogStatus(
                    name=str(name),
                    generation=0,
                    tracked=False,
                    recovering=False,
                    consecutive_failures=0,
                    last_detail="",
                    next_recovery_at=0.0,
                )
            return WatchdogStatus(
                name=target.name,
                generation=target.generation,
                tracked=True,
                recovering=target.recovering,
                consecutive_failures=target.consecutive_failures,
                last_detail=target.last_detail,
                next_recovery_at=target.next_recovery_at,
            )

    def run_once(self) -> None:
        """Probe every target once.  Public mainly for deterministic tests."""

        with self._lock:
            snapshots = [
                (
                    target.name,
                    target.generation,
                    target.process,
                    target.base_url,
                    target.evaluator,
                )
                for target in self._targets.values()
            ]

        for name, generation, process, base_url, evaluator in snapshots:
            exit_code = self._process_exit_code(process)
            if exit_code is not None:
                self._record_failure(
                    name,
                    generation,
                    reason="process_exited",
                    detail=f"tracked process exited with code {exit_code}",
                    exit_code=exit_code,
                    immediate=True,
                )
                continue

            try:
                result = self._health_probe(
                    base_url,
                    timeout_seconds=self.health_timeout_seconds,
                    evaluator=evaluator,
                )
            except Exception as exc:
                # Custom probes are allowed, but an exception must behave like
                # a failed probe rather than silently stopping supervision.
                result = HealthProbeResult(False, f"health probe failed: {exc}")
            if result.healthy:
                self._record_success(name, generation)
            else:
                self._record_failure(
                    name,
                    generation,
                    reason="health_failed",
                    detail=result.detail or "health check failed",
                    exit_code=None,
                    immediate=False,
                )

    def start(self) -> None:
        """Start the daemon probe thread once."""

        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="MekiCopyCompanionWatchdog",
                daemon=True,
            )
            self._thread.start()

    def stop(self, *, join_timeout_seconds: float = 2.0) -> None:
        """Request background shutdown; safe to call repeatedly during UI exit."""

        self._stop_event.set()
        with self._lock:
            worker = self._thread
        if worker and worker is not threading.current_thread():
            worker.join(timeout=max(0.0, float(join_timeout_seconds)))

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self.run_once()
            self._stop_event.wait(self.interval_seconds)

    @staticmethod
    def _process_exit_code(process: ProcessHandle) -> int | None:
        try:
            return process.poll()
        except Exception:
            # A Popen wrapper error is not proof the OS child exited.  Let the
            # bounded health request decide rather than force-killing anything.
            return None

    def _record_success(self, name: str, generation: int) -> None:
        with self._lock:
            target = self._targets.get(name)
            if target is None or target.generation != generation:
                return
            if target.recovering:
                # The UI has not started recovery yet, so a fresh healthy
                # probe is stronger evidence than the older failure streak.
                # Leave a claimed request alone: its owner may already be
                # terminating the process outside this lock.
                if target.recovery_claimed:
                    return
                target.recovering = False
                target.recovery_claimed = False
                target.next_recovery_at = 0.0
            target.consecutive_failures = 0
            target.last_detail = ""

    def _record_failure(
        self,
        name: str,
        generation: int,
        *,
        reason: str,
        detail: str,
        exit_code: int | None,
        immediate: bool,
    ) -> None:
        now = self._clock()
        with self._lock:
            target = self._targets.get(name)
            if target is None or target.generation != generation or target.recovering:
                return
            # Frozen desktop applications can need several seconds to load
            # native DLLs before their loopback server exists.  Do not turn a
            # normal launch into a kill/restart loop, while still treating an
            # actual process exit as immediately recoverable.
            if not immediate and now < target.startup_grace_until:
                target.last_detail = detail
                return
            target.consecutive_failures += 1
            target.last_detail = detail
            if not immediate and target.consecutive_failures < self.failures_before_recovery:
                return
            if now < target.next_recovery_at:
                return
            target.recovering = True
            target.next_recovery_at = now + self.cooldown_seconds
            request = RecoveryRequest(
                name=name,
                generation=generation,
                reason=reason,
                detail=detail,
                consecutive_failures=target.consecutive_failures,
                observed_at=now,
                exit_code=exit_code,
            )
        self._events.put(request)
