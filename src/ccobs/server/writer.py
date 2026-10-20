"""The writer: one thread that parses, reduces and persists hooks (ADRs 0024, 0026, 0027).

The hook route only enqueues raw bodies (``ccobs.server.ingest``). This thread takes them
off the ``HookQueue`` in micro-batches, parses each one (``parse_hook``), folds it into
its session's state in memory (``ccobs.domain.sessions.apply``) and persists the batch
in one transaction (``Repository.apply_batch``). The new states replace the old ones in
memory only once the batch has committed, so memory never runs ahead of the database.

Every other write goes through the same thread, so writes stay serialized (ADR 0026,
one writer): the stale sweep (``submit_tick``), the telemetry join
(``submit_telemetry_close``) and retention (``submit_retention``). They wait on a
control channel of their own, so they never take room in the hook queue's bounds.

Failures never reach Claude Code and never stop the thread. A body that is not a hook
payload is counted ``invalid``; a reducer error or a write that keeps failing is counted
``internal``. A failed batch is retried item by item, so one bad item costs only itself,
and when nothing can be written (a full disk, say) the thread backs off instead of
spinning. Logs hold counts and exception type names only: never bodies, paths, session
IDs or exception messages (SQLAlchemy's hide bound values but still quote SQL).

After each commit the writer names the sessions it changed to ``on_committed`` (the live
updates hub, ADR 0032), and ``ALL`` after retention deletes sessions: only once the
batch is in the database, so a page that refetches on that news reads it, and never for
a batch that failed. Like the block observer, the callback must be quick and must not
block (the hub only schedules work on the event loop); its errors are logged and never
reach the writer.

The alert rules (ADR 0041) run here too, over the sessions this thread holds. Every hook
event a commit stores is recorded in the failure windows (``record_failure_event``), in
the order it was applied. Every ``AlertTick`` (``submit_alert_tick``, every 5 s from
``ccobs.server.sweeps.AlertTicker``) runs the rules in a fixed order, ``waiting_user``,
``repeated_failures``, ``session_cost`` and ``daily_cost``, the cost rules on the last
``CostSample`` submitted (``submit_cost_sample``), and writes their effects in one
transaction (``Repository.apply_alert_effects``); then ``on_committed`` gets
``ALERTS``. A tick without effects writes nothing. The evaluators' state is rebuilt from
the database at the first tick (ADR 0042: the open and cooling keys, and the failure
windows, dropped while ``repeated_failures`` is off), with the saved settings applied
over the environment's (``apply_saved_settings``); ``submit_alert_settings_changed``
reloads them. A failed tick changes nothing in memory, so the next one tries again.

Hooks still queued when ``stop`` runs out of time are counted ``internal``: ADR 0027
already accepts that a stop loses what is still in memory and counts losses there, and
``internal`` is its reason for an app-side loss, so the drop reasons stay as recorded.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Final

from ccobs.domain.alerts import (
    AlertConfig,
    AlertEffect,
    AlertFired,
    AlertState,
    CostSample,
    apply_saved_settings,
    evaluate_daily_cost,
    evaluate_repeated_failures,
    evaluate_session_cost,
    evaluate_waiting_user,
    forget_sessions,
    record_failure_event,
)
from ccobs.domain.app_metrics import event_label
from ccobs.domain.denials import OpenBlock
from ccobs.domain.hooks import HookEvent, parse_hook
from ccobs.domain.sessions import (
    BlockClosed,
    BlockKind,
    SessionChanged,
    SessionState,
    apply,
    close_block_denied,
    mark_stale,
)
from ccobs.server.changes import ALERTS, ALL, HubEvent
from ccobs.server.db.repository import Applied, Repository, hook_event_row
from ccobs.server.ingest import HookQueue, RawHook

__all__ = [
    "AlertSettingsChanged",
    "AlertTick",
    "BlockClosedCallback",
    "CommittedCallback",
    "Control",
    "Retention",
    "TelemetryClose",
    "Tick",
    "Writer",
    "WriterCounts",
    "WriterObservers",
]

_log = logging.getLogger(__name__)

# Statuses that retention may delete (Repository.delete_older_than) and that hold no
# live work for the counts.
_CLOSED: Final = frozenset({"ended", "stale"})
_ENDED: Final = "ended"
_PERMISSION: Final = "permission"
_MS_PER_S: Final = 1000

# Receives each closed block's kind and how long it was open, in seconds.
type BlockClosedCallback = Callable[[BlockKind, float], None]
# Receives the sessions each commit changed, or ALL (ADR 0032), or ALERTS after a tick
# that wrote alert effects (ADR 0041).
type CommittedCallback = Callable[[HubEvent], None]


@dataclass(frozen=True, kw_only=True)
class WriterObservers:
    """The callbacks the app gives the writer it builds: the block-duration histogram
    (ADR 0028) and the live updates hub (ADR 0032)."""

    on_block_closed: BlockClosedCallback
    on_committed: CommittedCallback


@dataclass(frozen=True, slots=True)
class Tick:
    """Run the stale sweep (``mark_stale``) over every session in memory at ``now_ms``."""

    now_ms: int


@dataclass(frozen=True, slots=True)
class TelemetryClose:
    """Close the blocks of ``tool_use_id`` as denied, from telemetry (``close_block_denied``)."""

    session_id: str
    tool_use_id: str
    at_ms: int


@dataclass(frozen=True, slots=True)
class Retention:
    """Delete the ended and stale sessions whose last event is before ``cutoff_ms``."""

    cutoff_ms: int


@dataclass(frozen=True, slots=True)
class AlertTick:
    """Run the alert rules at ``now_ms`` and write their effects (ADR 0041)."""

    now_ms: int


@dataclass(frozen=True, slots=True)
class AlertSettingsChanged:
    """Reload the saved alert settings: the settings screen saved or reset one."""


# A CostSample, the cost reader's latest answer, is stored for the next AlertTick.
type Control = Tick | TelemetryClose | Retention | AlertTick | AlertSettingsChanged | CostSample


@dataclass(frozen=True, slots=True)
class _HookOp:
    """A parsed hook, ready for the reducer."""

    event: HookEvent
    received_at_ms: int
    seq: int


type _Op = _HookOp | Tick | TelemetryClose


@dataclass(frozen=True, kw_only=True)
class WriterCounts:
    """A snapshot of the writer's live state, for the metrics (stage 10 of milestone 2).

    ``sessions_by_status`` covers the sessions the writer holds: every one that has not
    ended (ended sessions leave memory; the database still has them).
    ``running_agents`` counts running agents that are not internal, in sessions that are
    neither ended nor stale. ``events_processed`` counts the hook events persisted since
    the writer started, and ``sessions_ended`` the sessions that moved to ``ended`` from
    another status (a new session whose first event is SessionEnd included); both are
    cumulative, for counters. ``events_received`` counts the hooks parsed since the
    writer started, persisted or not, by event label (``event_label``: the registered
    events and ``other``, so its keys are a closed set); cumulative too.
    """

    sessions_by_status: dict[str, int]
    open_blocks_by_kind: dict[str, int]
    running_agents: int
    events_processed: int
    sessions_ended: int
    events_received: dict[str, int] = field(default_factory=dict)
    alerts_fired: dict[str, int] = field(default_factory=dict)


class _LoadError(Exception):
    """Reading a session's state from the database failed; holds the cause's type name."""


class _View:
    """The states one write sees: the committed ones, plus its own changes on top."""

    def __init__(self, committed: dict[str, SessionState], repository: Repository) -> None:
        self._committed = committed
        self._repository = repository
        self.changed: dict[str, SessionState] = {}

    def get(self, session_id: str) -> SessionState | None:
        if session_id in self.changed:
            return self.changed[session_id]
        state = self._committed.get(session_id)
        if state is None:
            # Not in memory: an ended session that may reopen, or a new one (None).
            try:
                state = self._repository.load_state(session_id)
            except Exception as error:
                raise _LoadError(type(error).__name__) from None
            if state is not None:
                self.changed[session_id] = state
        return state

    def put(self, session_id: str, state: SessionState) -> None:
        self.changed[session_id] = state

    def states(self) -> dict[str, SessionState]:
        return {**self._committed, **self.changed}


class Writer:
    """The single writer thread. ``start`` runs it; ``run_once`` drives it in tests.

    ``stale_after_ms`` is the sweep's threshold (``CCOBS_SESSION_STALE_AFTER``).
    ``batch_max`` and ``batch_wait_s`` bound a micro-batch: up to ``batch_max`` hooks,
    collected for at most ``batch_wait_s`` after the first. ``poll_s`` is how long the
    idle thread waits for a hook before it checks the control channel and the stop flag.
    A write that nothing in a batch survives makes the thread wait ``backoff_initial_s``,
    doubling up to ``backoff_max_s``, until a write succeeds. ``monotonic`` and ``sleep``
    are injectable for tests; the default sleep returns early when ``stop`` is called.
    ``on_block_closed`` and ``on_committed`` are the observers (``WriterObservers``).
    ``alert_config`` is the alert rules the environment gives (``Settings.alert_config``,
    the defaults when ``None``), which saved settings override; ``discord_configured``
    whether a fired alert waits to be sent to Discord (``pending``) or never is
    (``none``).
    """

    def __init__(
        self,
        hook_queue: HookQueue,
        repository: Repository,
        host: str,
        *,
        stale_after_ms: int,
        batch_max: int = 256,
        batch_wait_s: float = 0.05,
        poll_s: float = 0.2,
        backoff_initial_s: float = 0.1,
        backoff_max_s: float = 30.0,
        on_block_closed: BlockClosedCallback | None = None,
        on_committed: CommittedCallback | None = None,
        alert_config: AlertConfig | None = None,
        discord_configured: bool = False,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], object] | None = None,
    ) -> None:
        self.host: Final = host
        self.stale_after_ms: Final = stale_after_ms
        self.discord_configured: Final = discord_configured
        self._queue = hook_queue
        self._repository = repository
        self._batch_max = batch_max
        self._batch_wait_s = batch_wait_s
        self._poll_s = poll_s
        self._backoff_initial_s = backoff_initial_s
        self._backoff_max_s = backoff_max_s
        self._backoff_s = backoff_initial_s
        self._on_block_closed = on_block_closed
        self._on_committed = on_committed
        # The alert rules: the environment's, the effective ones (saved settings on top,
        # loaded at the first tick), and the evaluators' state, None until the first
        # tick rebuilds it from the database.
        self._env_alerts = alert_config or AlertConfig()
        self._alert_config = self._env_alerts
        self._alerts: AlertState | None = None
        self._cost_sample: CostSample | None = None
        self._monotonic = monotonic
        self._stopping = threading.Event()
        self._sleep: Callable[[float], object] = sleep or self._stopping.wait
        self._controls: queue.SimpleQueue[Control] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._deadline = 0.0
        # Guards _states and the counters for readers on other threads; only this
        # writer changes them.
        self._lock = threading.Lock()
        self._states = repository.load_open_states()
        self._events = 0
        self._ended = 0
        self._received: Counter[str] = Counter()
        self._fired: Counter[str] = Counter()
        # Per run_once: whether to back off, and the seqs already persisted or counted.
        self._failing = False
        self._settled: set[int] = set()
        _log.info("writer loaded %d open sessions", len(self._states))

    # Thread control.

    def start(self) -> None:
        """Run the writer on a daemon thread of its own."""
        if self._thread is not None:
            raise RuntimeError("the writer is already started")
        self._thread = threading.Thread(target=self._run, name="ccobs-writer", daemon=True)
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self, timeout_s: float) -> None:
        """Drain the queue for up to ``timeout_s``, then stop. Hooks still queued after
        that are counted ``internal``. Without a thread, drains on the calling one."""
        self._deadline = self._monotonic() + timeout_s
        self._stopping.set()
        if self._thread is None:
            self._drain()
        else:
            self._thread.join(timeout_s)
            if self._thread.is_alive():
                _log.warning("writer did not stop within %.1f s", timeout_s)
        self._drop_leftovers()

    def _run(self) -> None:
        while not self._stopping.is_set():
            self.run_once(self._poll_s)
        self._drain()

    def _drain(self) -> None:
        while self._monotonic() < self._deadline and self.run_once(0.0):
            pass

    def _drop_leftovers(self) -> None:
        hooks = 0
        while (item := self._queue.get(0.0)) is not None:
            self._queue.count_drop("internal")
            self._queue.task_done(item)
            hooks += 1
        controls = len(self._take_controls(limit=None))
        if hooks or controls:
            _log.warning(
                "writer stopped with %d hooks and %d scheduled tasks left; hooks counted"
                " as dropped (internal)",
                hooks,
                controls,
            )

    # Scheduling (stages 11 and 12 of milestone 2).

    def submit_tick(self, now_ms: int) -> None:
        self._controls.put(Tick(now_ms))

    def submit_telemetry_close(self, session_id: str, tool_use_id: str, at_ms: int) -> None:
        self._controls.put(TelemetryClose(session_id, tool_use_id, at_ms))

    def submit_retention(self, cutoff_ms: int) -> None:
        self._controls.put(Retention(cutoff_ms))

    def submit_alert_tick(self, now_ms: int) -> None:
        self._controls.put(AlertTick(now_ms))

    def submit_cost_sample(self, sample: CostSample) -> None:
        self._controls.put(sample)

    def submit_alert_settings_changed(self) -> None:
        self._controls.put(AlertSettingsChanged())

    # Reads, from any thread.

    def counts(self) -> WriterCounts:
        with self._lock:
            states = list(self._states.values())
            events = self._events
            ended = self._ended
            received = dict(self._received)
            fired = dict(self._fired)
        return WriterCounts(
            sessions_by_status=dict(Counter(state.session.status for state in states)),
            open_blocks_by_kind=dict(
                Counter(block.kind for state in states for block in state.open_blocks)
            ),
            running_agents=sum(
                1
                for state in states
                if state.session.status not in _CLOSED
                for agent in state.agents
                if agent.status == "running" and not agent.internal
            ),
            events_processed=events,
            sessions_ended=ended,
            events_received=received,
            alerts_fired=fired,
        )

    def alert_config(self) -> AlertConfig:
        """The alert rules in force: the environment's, with the saved settings on top
        once the first tick has loaded them."""
        return self._alert_config

    def open_permission_blocks(self) -> list[OpenBlock]:
        """The committed open permission blocks that have a ``tool_use_id``, which the
        reconciler can match with telemetry (ADR 0029); blocks without one cannot be."""
        with self._lock:
            states = list(self._states.values())
        return [
            OpenBlock(state.session.session_id, block.tool_use_id, block.opened_at_ms)
            for state in states
            for block in state.open_blocks
            if block.kind == _PERMISSION and block.tool_use_id is not None
        ]

    def state(self, session_id: str) -> SessionState | None:
        """The committed state of a session in memory; ``None`` for ended or unknown ones."""
        with self._lock:
            return self._states.get(session_id)

    # One step.

    def run_once(self, timeout_s: float = 0.0) -> int:
        """Handle one micro-batch: the scheduled tasks waiting, and the hooks that arrive
        within ``timeout_s`` (plus ``batch_wait_s``). Returns how many items it took."""
        hooks: list[RawHook] = []
        controls: list[Control] = []
        self._failing = False
        self._settled = set()
        try:
            controls = self._take_controls(limit=self._batch_max)
            hooks = self._collect(0.0 if controls else timeout_s)
            self._process(hooks, controls)
        except Exception as error:
            # Never expected; the type only, as everywhere here.
            _log.error("writer error (%s)", type(error).__name__)
            for item in hooks:
                if item.seq not in self._settled:
                    self._queue.count_drop("internal")
            self._failing = True
        finally:
            for item in hooks:
                self._queue.task_done(item)
        if self._failing:
            self._sleep(self._backoff_s)
            self._backoff_s = min(self._backoff_s * 2, self._backoff_max_s)
        return len(hooks) + len(controls)

    def _take_controls(self, limit: int | None) -> list[Control]:
        items: list[Control] = []
        while limit is None or len(items) < limit:
            try:
                items.append(self._controls.get(block=False))
            except queue.Empty:
                break
        return items

    def _collect(self, timeout_s: float) -> list[RawHook]:
        first = self._queue.get(timeout_s)
        if first is None:
            return []
        items = [first]
        deadline = self._monotonic() + self._batch_wait_s
        while len(items) < self._batch_max:
            item = self._queue.get(max(0.0, deadline - self._monotonic()))
            if item is None:
                break
            items.append(item)
        return items

    def _process(self, hooks: Sequence[RawHook], controls: Sequence[Control]) -> None:
        parsed = [op for item in hooks if (op := self._parse(item)) is not None]
        if parsed:
            received = Counter(event_label(op.event.hook_event_name) for op in parsed)
            with self._lock:
                self._received.update(received)
        ops: list[_Op] = list(parsed)
        for control in controls:
            if isinstance(control, Tick | TelemetryClose):
                ops.append(control)
                continue
            # After the writes queued before it, so it sees their effects.
            self._write(ops)
            ops = []
            if isinstance(control, Retention):
                self._retain(control.cutoff_ms)
            elif isinstance(control, AlertTick):
                self._evaluate_alerts(control.now_ms)
            elif isinstance(control, AlertSettingsChanged):
                self._reload_alert_settings()
            else:
                self._cost_sample = control
        self._write(ops)

    def _parse(self, item: RawHook) -> _HookOp | None:
        try:
            event = parse_hook(json.loads(item.body))
        except (ValueError, RecursionError):
            # ValueError covers bad JSON, bad UTF-8 and HookPayloadError; RecursionError a
            # body nested too deep for the parser.
            self._queue.count_drop("invalid")
            self._settled.add(item.seq)
            _log.debug("hook dropped: invalid")
            return None
        except Exception as error:
            # A bug in the parser: it costs this hook only.
            self._queue.count_drop("internal")
            self._settled.add(item.seq)
            _log.error("hook dropped: could not parse (%s)", type(error).__name__)
            return None
        return _HookOp(event, item.received_at_ms, item.seq)

    # Writes.

    def _write(self, ops: Sequence[_Op]) -> None:
        """Persist ``ops`` in one transaction, or item by item if that fails."""
        view = _View(self._states, self._repository)
        planned: list[_Op] = []
        applied: list[Applied] = []
        for op in ops:
            result = self._plan(op, view)
            if result:
                planned.append(op)
                applied.extend(result)
        if not applied:
            return
        try:
            self._repository.apply_batch(self.host, applied)
        except Exception as error:
            # The view is dropped: memory keeps the states of the last commit.
            _log.error(
                "writer: a batch of %d writes failed (%s); retrying one by one",
                len(applied),
                type(error).__name__,
            )
            self._retry(planned)
            return
        self._commit(view, applied, planned)

    def _retry(self, ops: Sequence[_Op]) -> None:
        attempted = persisted = 0
        for op in ops:
            view = _View(self._states, self._repository)
            applied = self._plan(op, view)
            if not applied:
                continue
            attempted += 1
            try:
                self._repository.apply_batch(self.host, applied)
            except Exception as error:
                self._drop(op, "write failed", type(error).__name__)
                continue
            self._commit(view, applied, [op])
            persisted += 1
        if attempted and not persisted:
            # Nothing could be written: likely the database itself (a full disk, a lock).
            self._failing = True

    def _plan(self, op: _Op, view: _View) -> list[Applied] | None:
        """What ``op`` writes, against ``view``; None (counted) if it cannot be applied."""
        try:
            applied = self._compute(op, view)
        except _LoadError as error:
            # A storage failure, like a failed write: back off.
            self._drop(op, "could not load a session", str(error))
            self._failing = True
            return None
        except Exception as error:
            # ValueError: an event applied to another session's state (a bug).
            self._drop(op, "could not apply", type(error).__name__)
            return None
        if not applied and isinstance(op, _HookOp):
            self._settled.add(op.seq)  # already applied: nothing to write
        return applied

    def _compute(self, op: _Op, view: _View) -> list[Applied]:
        if isinstance(op, _HookOp):
            event = op.event
            state, effects = apply(view.get(event.session_id), event, op.received_at_ms, op.seq)
            view.put(event.session_id, state)
            if not effects:
                return []
            row = hook_event_row(event, seq=op.seq, received_at_ms=op.received_at_ms)
            return [Applied(event_row=row, effects=effects, state=state)]
        if isinstance(op, Tick):
            applied = []
            for session_id, state in view.states().items():
                swept, effects = mark_stale(state, op.now_ms, self.stale_after_ms)
                if effects:
                    view.put(session_id, swept)
                    applied.append(Applied(event_row=None, effects=effects, state=swept))
            return applied
        found = view.get(op.session_id)
        if found is None:
            return []
        closed, effects = close_block_denied(found, tool_use_id=op.tool_use_id, at_ms=op.at_ms)
        if not effects:
            return []
        view.put(op.session_id, closed)
        return [Applied(event_row=None, effects=effects, state=closed)]

    def _drop(self, op: _Op, what: str, error_type: str) -> None:
        """Count (a hook) or log (a scheduled task) an op that is given up on."""
        if isinstance(op, _HookOp):
            self._queue.count_drop("internal")
            self._settled.add(op.seq)
            _log.error("hook dropped: %s (%s)", what, error_type)
        else:
            _log.error("%s skipped: %s (%s)", type(op).__name__, what, error_type)

    def _commit(self, view: _View, applied: Sequence[Applied], ops: Sequence[_Op]) -> None:
        """The batch is in the database: make its states the committed ones, and record
        its hook events in the failure windows, in the order they were applied."""
        ended = sum(
            1
            for item in applied
            for effect in item.effects
            if isinstance(effect, SessionChanged)
            and effect.session.status == _ENDED
            and effect.previous_status != _ENDED
        )
        with self._lock:
            for session_id, state in view.changed.items():
                if state.session.status == _ENDED:
                    self._states.pop(session_id, None)
                else:
                    self._states[session_id] = state
            self._events += sum(item.event_row is not None for item in applied)
            self._ended += ended
        self._settled.update(item.event_row.seq for item in applied if item.event_row)
        self._record_failures(ops)
        self._backoff_s = self._backoff_initial_s
        callback = self._on_block_closed
        if callback is not None:
            for item in applied:
                for effect in item.effects:
                    if isinstance(effect, BlockClosed):
                        _observe(callback, effect)
        self._notify(frozenset(item.state.session.session_id for item in applied))

    def _retain(self, cutoff_ms: int) -> None:
        try:
            deleted = self._repository.delete_older_than(cutoff_ms)
        except Exception as error:
            _log.error("retention failed (%s)", type(error).__name__)
            return
        # Memory forgets exactly the sessions that went, alert state included: a
        # failure window left behind would fire an alert for a session no longer
        # stored, whose insert fails the whole tick.
        with self._lock:
            for session_id in deleted:
                self._states.pop(session_id, None)
        if self._alerts is not None:
            self._alerts = forget_sessions(self._alerts, deleted)
        _log.info("retention deleted %d sessions", len(deleted))
        if deleted:
            self._notify(ALL)

    # The alert rules (ADR 0041).

    def _record_failures(self, ops: Sequence[_Op]) -> None:
        """Feed committed hook events to ``record_failure_event``. Before the first tick
        there is no state yet: it is rebuilt from the database, which holds them."""
        state = self._alerts
        if state is None:
            return
        for op in ops:
            if isinstance(op, _HookOp):
                state = record_failure_event(
                    state, op.event, op.received_at_ms, config=self._alert_config
                )
        self._alerts = state

    def _evaluate_alerts(self, now_ms: int) -> None:
        """Run the rules at ``now_ms`` and write their effects in one transaction. On
        any error nothing changes in memory: the next tick tries again."""
        try:
            state = self._alerts if self._alerts is not None else self._rebuild_alerts(now_ms)
            state, effects = self._run_rules(state, now_ms)
            if effects:
                self._repository.apply_alert_effects(
                    self.host, effects, discord_configured=self.discord_configured
                )
        except Exception as error:
            _log.error("alert evaluation failed (%s)", type(error).__name__)
            return
        self._alerts = state
        if not effects:
            return
        fired = Counter(effect.rule for effect in effects if isinstance(effect, AlertFired))
        with self._lock:
            self._fired.update(fired)
        self._notify(ALERTS)

    def _run_rules(self, state: AlertState, now_ms: int) -> tuple[AlertState, list[AlertEffect]]:
        """Every rule, in a fixed order, over the committed sessions (O(sessions held))."""
        config = self._alert_config
        sample = self._cost_sample
        sessions = list(self._states.values())
        state, waiting = evaluate_waiting_user(state, sessions, config=config, now_ms=now_ms)
        state, failures = evaluate_repeated_failures(state, config=config, now_ms=now_ms)
        state, session_cost = evaluate_session_cost(
            state, sample, sessions, config=config, now_ms=now_ms
        )
        state, daily_cost = evaluate_daily_cost(state, sample, config=config, now_ms=now_ms)
        return state, [*waiting, *failures, *session_cost, *daily_cost]

    def _rebuild_alerts(self, now_ms: int) -> AlertState:
        """The evaluators' state after a start (ADR 0042), with the saved settings
        loaded: the keys still firing or cooling down, and the failure windows, dropped
        while ``repeated_failures`` is off."""
        config = self._load_alert_config()
        keys = self._repository.alert_keys(
            self.host, cooldown_since_ms=now_ms - config.cooldown_s * _MS_PER_S
        )
        rule = config.repeated_failures
        failures = (
            self._repository.failure_windows(
                self.host, since_ms=now_ms - rule.window_s * _MS_PER_S, count=rule.count
            )
            if rule.enabled
            else {}
        )
        self._alert_config = config
        _log.info("alerts rebuilt: %d keys, %d failure windows", len(keys), len(failures))
        return AlertState(keys=keys, failures=failures)

    def _load_alert_config(self) -> AlertConfig:
        """The environment's rules with the saved settings on top; a saved value out of
        bounds or of the wrong type is ignored, and logged by its name only."""
        saved = self._repository.alert_settings(self.host)
        config, ignored = apply_saved_settings(self._env_alerts, saved)
        if ignored:
            _log.warning(
                "ignored saved alert settings out of bounds or of the wrong type: %s",
                ", ".join(ignored),
            )
        return config

    def _reload_alert_settings(self) -> None:
        if self._alerts is None:
            return  # the first tick loads them
        try:
            self._alert_config = self._load_alert_config()
        except Exception as error:
            _log.error("alert settings not reloaded (%s)", type(error).__name__)

    def _notify(self, change: HubEvent) -> None:
        """Tell ``on_committed`` what a commit changed; an error is logged, not raised."""
        callback = self._on_committed
        if callback is None:
            return
        try:
            callback(change)
        except Exception as error:
            _log.error("commit observer failed (%s)", type(error).__name__)


def _observe(callback: BlockClosedCallback, effect: BlockClosed) -> None:
    """Report a closed block's kind and duration; an observer error is logged, not raised."""
    block = effect.block
    # A BlockClosed always carries closed_at_ms, clamped to the opening (ADR 0024).
    closed_at = block.closed_at_ms or block.opened_at_ms
    try:
        callback(block.kind, (closed_at - block.opened_at_ms) / _MS_PER_S)
    except Exception as error:
        _log.error("block observer failed (%s)", type(error).__name__)
