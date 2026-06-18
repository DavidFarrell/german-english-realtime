"""AudioGuardian - the single owner that keeps BT earbuds in A2DP stereo (PLAN: notes/AUTOFIX_PLAN.md).

ONE asyncio task owns all CoreAudio default mutation and the blocking recovery recipe. Everything
else (the 5 Hz armed poll, wizard/live transitions, the manual Fix button, future CoreAudio
listeners) only ENQUEUES events; the owner re-enumerates and decides. This makes the guard the sole
mutator of system defaults and the sole launcher of the `asyncio.to_thread` recipe, so re-entrancy
is impossible (guarded by the `recovering_*` states).

Locked UX (David, 18 Jun 2026):
  - The AUTOMATIC layer is PREVENTION ONLY: while armed/live the guard may silently re-point the
    system default INPUT back to the real mic (`correct_default_input`). That stops the large
    majority of collapses before they happen.
  - The DISRUPTIVE recovery - the `park` recipe (laptop-speaker blip) and the `blueutil` reconnect -
    is EXPLICIT-TAP ONLY. The guard enters `recovering_park`/`recovering_reconnect` ONLY in response
    to a user `manual_fix` event, never autonomously.
  - Prevention re-points the default input but CANNOT un-collapse a bud already stuck in HFP mono
    (David's locked finding). So when a bud reads mono, the guard surfaces `needs_user_recovery`
    directly - PERSISTENT, non-self-clearing actionable status (not a toast that vanishes) - and the
    user taps to escalate. A confirmed external holder of the earbud mic -> `blocked`.

Headless-testable: all CoreAudio access goes through an injected `AudioOps` (see DefaultAudioOps in
this module and FakeAudioOps in tests). No real hardware/CoreAudio in unit tests.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Optional, Protocol


def _now_ms() -> int:
    return time.monotonic_ns() // 1_000_000


# -- states / events ------------------------------------------------------------------------------

class State(str, Enum):
    DISABLED = "disabled"                       # user toggle off -> never touch audio
    PASSIVE = "passive"                          # idle/observe only -> never touch system defaults
    ARMED = "armed"                              # wizard on output/channel steps or toggle on -> lease
    LIVE = "live"                                # a translation session is running -> lease
    RECOVERING_PARK = "recovering_park"          # running the park recipe (user-tapped)
    RECOVERING_RECONNECT = "recovering_reconnect"  # running the blueutil reconnect (user-tapped)
    NEEDS_USER_RECOVERY = "needs_user_recovery"  # prevention couldn't restore stereo -> persistent UI
    BLOCKED = "blocked"                          # external holder / oscillation -> sticky, surfaced


class Event(str, Enum):
    TICK = "tick"                     # 5 Hz while armed/live (housekeeping emits it)
    ARM = "arm"                       # wizard reached an output/channel step, or protect toggle on
    DISARM = "disarm"                 # left those steps / backgrounded with no session
    LIVE_START = "live_start"
    LIVE_STOP = "live_stop"
    OUTPUT_SELECTED = "output_selected"  # user picked the earbud output device -> reconcile now
    SESSION_START = "session_start"      # immediate reconcile on session start
    MANUAL_FIX = "manual_fix"         # user tapped Fix/Reconnect -> escalate (park, then reconnect)
    SET_ENABLED = "set_enabled"       # protect toggle changed (payload: bool)
    DIRTY = "dirty"                   # a CoreAudio listener fired (Slice 3) -> re-enumerate


@dataclass
class GuardEvent:
    kind: Event
    payload: object = None


# -- the snapshot the owner reconciles against ----------------------------------------------------

@dataclass(frozen=True)
class AudioSnapshot:
    """One debounced read of the reconciled earbud graph (all by UID)."""
    present: bool                 # earbud A2DP node present at all
    output_uid: str
    input_uid: Optional[str]
    out_ch: int                   # earbud A2DP output channels (2 = stereo, 1 = collapsed mono)
    default_input_uid: Optional[str]
    dji_input_uid: Optional[str]  # the real mic we steer the default back to
    holders: frozenset            # process names holding the EARBUD input specifically (excl. us)
    group_generation: int         # earbud-group-specific generation (NOT global device churn)

    @property
    def mono(self) -> bool:
        return self.present and self.out_ch < 2

    def episode_signature(self) -> tuple:
        # PLAN: one recovery per distinct signature. Group generation + default-input UID + earbud
        # channel count + earbud-input holder set. (Global device-list churn must NOT reset budgets.)
        return (self.group_generation, self.default_input_uid, self.out_ch, self.holders)


class AudioOps(Protocol):
    """The narrow CoreAudio surface the guardian needs. DefaultAudioOps wraps macaudio; tests fake it."""
    def snapshot(self) -> AudioSnapshot: ...
    def correct_default_input(self) -> bool: ...        # set default input -> DJI by UID (light)
    def run_park_recipe(self) -> dict: ...              # blocking park (no reconnect)
    def run_reconnect_recipe(self) -> dict: ...         # blocking blueutil reconnect


# -- status surfaced to the renderer --------------------------------------------------------------

@dataclass
class GuardianStatus:
    """Slim guardian status (generalises the old `fixingOutput`). Mirrored in ViewState.snapshot()."""
    enabled: bool = True
    state: str = State.PASSIVE.value
    # `phase`: what the user sees - idle | preventing | fixing | needs_recovery | blocked
    phase: str = "idle"
    message: str = ""
    holder: Optional[str] = None
    actionable: bool = False      # show a Fix/Reconnect button
    action: str = ""              # "fix" (park) | "reconnect" | ""
    # Persistent (non-self-clearing) flag: needs_user_recovery / blocked must NOT auto-expire.
    persistent: bool = False

    def snapshot(self) -> dict:
        return {
            "enabled": self.enabled, "state": self.state, "phase": self.phase,
            "message": self.message, "holder": self.holder, "actionable": self.actionable,
            "action": self.action, "persistent": self.persistent,
        }


# -- tunables -------------------------------------------------------------------------------------

POLL_HZ = 5
OSC_WINDOW_MS = 10_000      # oscillation window
OSC_MAX_CORRECTIONS = 3     # >3 corrections in the window -> blocked_oscillation
DEBOUNCE_MS = 250           # holder/channel snapshots flicker; require two agreeing reads


class DefaultAudioOps:
    """The real AudioOps: reads the reconciled earbud graph via macaudio + the engine's resolver.

    Tracks an earbud-group-SPECIFIC generation (PLAN): it ticks when the resolved group's output or
    input UID changes - NOT on every HDMI/USB device-list churn - so recovery budgets aren't reset by
    unrelated hotplugs. The engine supplies `resolve_group()` (cached UID identity) and the DJI input
    name; everything else is read fresh each snapshot.
    """

    def __init__(self, resolve_group: Callable[[], object],
                 output_substr: Callable[[], str], dji_input_substr: Callable[[], str],
                 park_output_substr: str = "MacBook Pro Speakers") -> None:
        from . import macaudio
        self._mac = macaudio
        self._resolve_group = resolve_group
        # GPT-5 cp4 #4: read the CURRENT selected output/input names each time (the user can change
        # devices after boot), so reconnect/correct never use a stale substring.
        self._output_substr = output_substr
        self._dji_substr = dji_input_substr
        self._park_output_substr = park_output_substr
        self._generation = 0
        self._last_group_key: tuple | None = None

    def _group(self):
        return self._resolve_group()

    def _bump_generation(self, group) -> int:
        key = (getattr(group, "output_uid", None), getattr(group, "input_uid", None)) \
            if group is not None else None
        if key != self._last_group_key:
            self._generation += 1
            self._last_group_key = key
        return self._generation

    def _dji_uid(self) -> str | None:
        return self._mac.input_uid_for_name(self._dji_substr())

    def snapshot(self) -> AudioSnapshot:
        group = self._group()
        gen = self._bump_generation(group)
        dji_uid = self._dji_uid()
        if group is None:
            return AudioSnapshot(
                present=False, output_uid="", input_uid=None, out_ch=0,
                default_input_uid=self._mac.default_input_uid(), dji_input_uid=dji_uid,
                holders=frozenset(), group_generation=gen)
        out_ch = self._mac.output_channels_for_uid(group.output_uid)
        holders = frozenset(self._mac.earbud_input_holders(group.input_uid)) \
            if group.input_uid else frozenset()
        return AudioSnapshot(
            present=out_ch > 0, output_uid=group.output_uid, input_uid=group.input_uid,
            out_ch=out_ch, default_input_uid=self._mac.default_input_uid(),
            dji_input_uid=dji_uid, holders=holders, group_generation=gen)

    def correct_default_input(self) -> bool:
        dji_uid = self._dji_uid()
        if not dji_uid:
            return False
        return self._mac.set_default_uid("input", dji_uid)

    def run_park_recipe(self) -> dict:
        group = self._group()
        if group is None:
            return {"ok": False, "channels": 0, "steps": ["no earbud group"]}
        return self._mac.park_recipe(group.output_uid, self._dji_uid(), self._park_output_substr)

    def run_reconnect_recipe(self) -> dict:
        group = self._group()
        if group is None:
            return {"ok": False, "channels": 0, "steps": ["no earbud group"]}
        return self._mac.reconnect_recipe(group.output_uid, self._output_substr(), self._dji_uid())


@dataclass
class _Lease:
    """What grants the guard permission to mutate global audio: armed (wizard/toggle) or live."""
    armed: bool = False
    live: bool = False

    @property
    def leased(self) -> bool:
        return self.armed or self.live


class AudioGuardian:
    """The single owner task. Build it with an AudioOps + a GuardianStatus to mutate, then `run()`.

    Concurrency: external callers use `post(event)` which routes through the loop
    (`call_soon_threadsafe`) so future CoreAudio listener callbacks on foreign threads are safe.
    The owner loop is the ONLY place that reads `ops.snapshot()`, calls `correct_default_input`, or
    launches the park/reconnect recipe (via `asyncio.to_thread`). `recovering_*` states make the
    recipe non-re-entrant; events queued during a recovery are dropped on completion.

    Hooks: `on_change` fires whenever the status surface changes (mirror it to the view);
    `before_recovery` runs on the owner loop just before the disruptive recipe (the engine frees its
    audio streams there so the park can re-route); `after_recovery` runs just after (refresh devices).
    """

    def __init__(self, ops: AudioOps, status: GuardianStatus,
                 on_change: Callable[[], None] | None = None,
                 before_recovery: Callable[[], Awaitable[None]] | None = None,
                 after_recovery: Callable[[], Awaitable[None]] | None = None,
                 enabled: bool = True) -> None:
        self._ops = ops
        self.status = status
        self._on_change = on_change or (lambda: None)
        self._before_recovery = before_recovery
        self._after_recovery = after_recovery
        self._q: asyncio.Queue[GuardEvent] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lease = _Lease()
        self.state = State.PASSIVE if enabled else State.DISABLED
        self.status.enabled = enabled
        self.status.state = self.state.value
        # PREVENTION bookkeeping (grabbed-but-stereo): repeatable, gated only by the oscillation guard.
        self._correction_times: list[int] = []           # ms timestamps of recent corrections
        # the signature behind the current sticky surface (needs_user_recovery / blocked); the surface
        # is kept until this signature changes (holder gone, channel back, reconnect, generation bump).
        self._surfaced_signature: tuple | None = None
        # cross-tick debounce of volatile fields (holders / out_ch / default-input)
        self._debounce_key: tuple | None = None
        self._debounce_since_ms: int = 0
        self._owner_task: asyncio.Task | None = None
        self._poll_task: asyncio.Task | None = None
        self._last_snapshot: AudioSnapshot | None = None

    # -- public API (thread-safe enqueue) ----------------------------------------------------
    def post(self, kind: Event, payload: object = None) -> None:
        """Enqueue an event. Thread-safe: if a loop is captured (run() was called) and we're off the
        loop thread, route via call_soon_threadsafe; otherwise enqueue directly."""
        ev = GuardEvent(kind, payload)
        loop = self._loop
        if loop is None:
            self._q.put_nowait(ev)
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._q.put_nowait(ev)
        else:
            loop.call_soon_threadsafe(self._q.put_nowait, ev)

    def run(self) -> asyncio.Task:
        """Start the owner loop + the armed poll (idempotent). Returns the owner task."""
        if self._owner_task is not None:
            return self._owner_task
        self._loop = asyncio.get_event_loop()
        self._owner_task = asyncio.create_task(self._owner_loop(), name="guardian-owner")
        self._poll_task = asyncio.create_task(self._poll_loop(), name="guardian-poll")
        return self._owner_task

    async def stop(self) -> None:
        for t in (self._poll_task, self._owner_task):
            if t is not None:
                t.cancel()
        for t in (self._poll_task, self._owner_task):
            if t is not None:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

    # -- the 5 Hz poll: only emits TICK while leased (armed/live), never mid-recovery ----------
    async def _poll_loop(self) -> None:
        period = 1.0 / POLL_HZ
        try:
            while True:
                await asyncio.sleep(period)
                if self._lease.leased and self.state not in (
                        State.RECOVERING_PARK, State.RECOVERING_RECONNECT):
                    self.post(Event.TICK)
        except asyncio.CancelledError:
            raise

    # -- the owner loop: sole decision point -------------------------------------------------
    async def _owner_loop(self) -> None:
        try:
            while True:
                ev = await self._q.get()
                await self._handle(ev)
        except asyncio.CancelledError:
            raise

    def _set_state(self, state: State) -> None:
        if state != self.state:
            self.state = state
            self.status.state = state.value

    def _emit(self) -> None:
        self._on_change()

    async def _handle(self, ev: GuardEvent) -> None:
        kind = ev.kind
        if kind == Event.SET_ENABLED:
            self._apply_enabled(bool(ev.payload))
            self._emit()
            return
        if self.state == State.DISABLED:
            # Track lease intent while disabled so re-enabling resumes correctly; mutate nothing.
            self._track_lease(kind)
            return
        if kind in (Event.ARM, Event.DISARM, Event.LIVE_START, Event.LIVE_STOP):
            self._track_lease(kind)
            self._recompute_lease_state()
            if self._lease.leased:
                await self._reconcile()
            self._emit()
            return
        if kind in (Event.OUTPUT_SELECTED, Event.SESSION_START, Event.TICK, Event.DIRTY):
            if self._lease.leased:
                await self._reconcile()
            return
        if kind == Event.MANUAL_FIX:
            await self._user_escalate()
            self._emit()
            return

    # -- lease tracking ----------------------------------------------------------------------
    def _track_lease(self, kind: Event) -> None:
        if kind == Event.ARM:
            self._lease.armed = True
        elif kind == Event.DISARM:
            self._lease.armed = False
        elif kind == Event.LIVE_START:
            self._lease.live = True
        elif kind == Event.LIVE_STOP:
            self._lease.live = False

    def _resting_state(self) -> State:
        """The non-sticky state the lease implies right now (live > armed > passive)."""
        if not self.status.enabled:
            return State.DISABLED
        if self._lease.live:
            return State.LIVE
        if self._lease.armed:
            return State.ARMED
        return State.PASSIVE

    def _go_to_resting(self) -> None:
        """FORCE the state back to what the lease implies, leaving any sticky/recovering state.
        (GPT-5 cp3 #1/#2: _recompute_lease_state no-ops on sticky states, so success/clear paths
        must use THIS to actually exit needs_user_recovery / blocked / recovering_*.)"""
        rest = self._resting_state()
        self._set_state(rest)
        self._surfaced_signature = None
        self._set_idle()

    def _recompute_lease_state(self) -> None:
        # A plain lease toggle must NOT yank a sticky surfaced/recovering state (those clear only when
        # the episode changes or the user acts). For non-sticky states, follow the lease.
        if self.state in (State.NEEDS_USER_RECOVERY, State.BLOCKED,
                          State.RECOVERING_PARK, State.RECOVERING_RECONNECT):
            return
        rest = self._resting_state()
        self._set_state(rest)
        if rest in (State.PASSIVE, State.DISABLED):
            self._set_idle()

    def _apply_enabled(self, enabled: bool) -> None:
        self.status.enabled = enabled
        if not enabled:
            self._set_state(State.DISABLED)
            self._set_idle()
            self._surfaced_signature = None
        else:
            self._go_to_resting()

    # -- the reconcile: PREVENTION ONLY (correct_default_input) + surface --------------------
    async def _reconcile(self) -> None:
        """Read the world; re-point a grabbed earbud default-input (silent prevention). If the bud is
        already collapsed and prevention can't bring it back, surface (needs_user_recovery). A
        confirmed external holder -> blocked. The ONLY automatic mutation is correct_default_input;
        park/reconnect are user-tapped (see _user_escalate)."""
        if not self._lease.leased:
            return
        snap = self._debounced_snapshot()
        if snap is None:
            return
        self._last_snapshot = snap
        sig = snap.episode_signature()

        # Leave a sticky surface the moment the episode signature changes (holder gone, channel back,
        # reconnect, generation bump). Uses the forced resting transition so it actually exits.
        if self.state in (State.NEEDS_USER_RECOVERY, State.BLOCKED):
            if sig != self._surfaced_signature:
                self._go_to_resting()
                self._emit()   # GPT-5 cp4 #2: the renderer mirrors status only on change - emit it
            else:
                return  # still the same bad situation; keep it surfaced, don't act

        if not snap.present:
            if self.status.phase != "idle":   # GPT-5 cp4 #2: clear a stale surface when buds vanish
                self._set_idle()
                self._emit()
            return  # buds gone -> nothing to do; lease stays, idle surface

        # 1. Confirmed external holder of the earbud mic -> light/park will lose; surface blocked.
        if snap.holders:
            self._enter_blocked(holder=", ".join(sorted(snap.holders)), sig=sig)
            return

        # 2. Already collapsed to mono: prevention can't un-collapse a stuck HFP bud (David's locked
        #    finding) -> surface (needs_user_recovery); the user taps to escalate (park).
        if snap.mono:
            self._enter_needs_user_recovery(sig)
            return

        # 3. PREVENTION: macOS grabbed the buds as default input but they're still stereo. Re-point the
        #    default input back to the DJI (silent, structural - success is immediate). Repeatable
        #    (NOT one-per-signature); the oscillation guard is the backstop against repeated re-grabs.
        grabbed = (snap.input_uid is not None and snap.default_input_uid == snap.input_uid)
        if grabbed:
            if self._would_oscillate():
                self._enter_blocked(holder=None, sig=sig, oscillation=True)
                return
            self._ops.correct_default_input()
            self._record_correction()
            self.status.phase = "preventing"
            self.status.message = "Keeping the earbuds in stereo…"
            self.status.persistent = False
            self.status.actionable = False
            self._emit()
            return

        # 4. All good. Clear any lingering "preventing" surface (GPT-5 cp4 #2: emit on change).
        if self.state in (State.ARMED, State.LIVE) and self.status.phase != "idle":
            self._set_idle()
            self._emit()

    def _set_idle(self) -> None:
        self.status.phase = "idle"
        self.status.message = ""
        self.status.holder = None
        self.status.actionable = False
        self.status.action = ""
        self.status.persistent = False

    # -- surfaced (persistent, non-self-clearing) states -------------------------------------
    def _enter_needs_user_recovery(self, sig: tuple) -> None:
        self._surfaced_signature = sig
        self._set_state(State.NEEDS_USER_RECOVERY)
        self.status.phase = "needs_recovery"
        self.status.message = "The earbuds are in mono. Tap Fix to switch them back to stereo."
        self.status.holder = None
        self.status.actionable = True
        self.status.action = "fix"
        self.status.persistent = True
        self._emit()

    def _enter_blocked(self, holder: str | None, sig: tuple, oscillation: bool = False) -> None:
        self._surfaced_signature = sig
        self._set_state(State.BLOCKED)
        self.status.phase = "blocked"
        if oscillation:
            self.status.message = "The earbuds keep dropping to mono. Tap Reconnect to reset them."
            self.status.holder = None
        else:
            self.status.message = f"{holder} is using the earbud mic - quit it, or tap Reconnect."
            self.status.holder = holder
        self.status.action = "reconnect"
        self.status.actionable = True
        self.status.persistent = True
        self._emit()

    # -- oscillation guard (>OSC_MAX corrections / window -> block) ---------------------------
    def _record_correction(self) -> None:
        self._correction_times.append(_now_ms())

    def _would_oscillate(self) -> bool:
        """True if recording ANOTHER correction now would exceed the budget (so we block INSTEAD of
        doing the over-budget correction). >OSC_MAX_CORRECTIONS in the window -> blocked."""
        cutoff = _now_ms() - OSC_WINDOW_MS
        self._correction_times = [t for t in self._correction_times if t >= cutoff]
        return len(self._correction_times) >= OSC_MAX_CORRECTIONS

    # -- user-tapped escalation: park first, then opt-in reconnect ---------------------------
    async def _user_escalate(self) -> None:
        """The ONLY entry to the disruptive recipe. Park (blocking, off-loop); if still mono, reconnect.
        Gated by an active lease (passive/disabled never touch defaults) and non-re-entrant via the
        recovering_* states; events queued during the recipe are dropped on completion."""
        if self.state in (State.RECOVERING_PARK, State.RECOVERING_RECONNECT):
            return
        if not self._lease.leased:
            return  # GPT-5 cp3 #3: no lease -> never mutate, even from a stale sticky tap
        self._set_state(State.RECOVERING_PARK)
        self.status.phase = "fixing"
        self.status.message = "Fixing the earbuds - this takes a few seconds…"
        self.status.actionable = False
        self.status.persistent = True
        self._emit()
        if self._before_recovery is not None:
            await self._before_recovery()   # engine frees its streams so park can re-route devices
        result = await asyncio.to_thread(self._ops.run_park_recipe)
        # GPT-5 cp3 #5: honour a disable/disarm that arrived during park BEFORE the reconnect step.
        self._consume_pending()
        if not result.get("ok"):
            if self._lease.leased:
                self._set_state(State.RECOVERING_RECONNECT)
                self.status.message = "Reconnecting the earbuds…"
                self._emit()
                result = await asyncio.to_thread(self._ops.run_reconnect_recipe)
        self._correction_times.clear()
        if self._after_recovery is not None:
            await self._after_recovery()    # engine refreshes devices / re-resolves the earbud group
        # GPT-5 cp4b: re-consume any lease/enable events that queued DURING reconnect/_after_recovery
        # (and drop duplicate taps / stale ticks) so the final decision sees the CURRENT lease, not a
        # stale one - otherwise a Stop/Disarm/Disable mid-reconnect could leave a dead actionable surface.
        self._consume_pending()
        snap = self._debounced_snapshot() or self._ops.snapshot()
        # Only surface (and leave an actionable button) if we're still leased + enabled (cp4 #3).
        still_leased = self.status.enabled and self._lease.leased
        if still_leased and snap is not None and snap.present and snap.mono:
            self._enter_needs_user_recovery(snap.episode_signature())
        else:
            self._go_to_resting()
            self._emit()

    def _consume_pending(self) -> None:
        """Drain the queue: APPLY queued lease/enable events (ARM/DISARM/LIVE_*/SET_ENABLED) so the
        guardian's lease reflects what the user did during the blocking recipe, and DROP the stale
        action/tick events (MANUAL_FIX/TICK/DIRTY/OUTPUT_SELECTED/SESSION_START) - we re-snapshot and
        reconcile right after, so a duplicate Fix tap can't launch a second recipe."""
        while not self._q.empty():
            ev = self._q.get_nowait()
            if ev.kind == Event.SET_ENABLED:
                self.status.enabled = bool(ev.payload)
                if not self.status.enabled:
                    self._lease.armed = self._lease.live = False
            elif ev.kind in (Event.ARM, Event.DISARM, Event.LIVE_START, Event.LIVE_STOP):
                self._track_lease(ev.kind)
            # else: drop (stale TICK/DIRTY/OUTPUT_SELECTED/SESSION_START/duplicate MANUAL_FIX)

    # -- cross-tick debounce -----------------------------------------------------------------
    def _debounced_snapshot(self) -> AudioSnapshot | None:
        """Return a snapshot only once its volatile fields (out_ch, holders, default-input UID) have
        been STABLE for DEBOUNCE_MS across reconcile calls - so 100-250 ms process/device flicker
        doesn't trigger a spurious correction or surface (GPT-5 cp3 #10)."""
        snap = self._ops.snapshot()
        if DEBOUNCE_MS <= 0:
            return snap  # debounce disabled (tests / opt-out): act on every read
        key = (snap.out_ch, snap.holders, snap.default_input_uid, snap.present)
        now = _now_ms()
        if key != self._debounce_key:
            self._debounce_key = key
            self._debounce_since_ms = now
            return None  # changed this round; wait for it to settle
        if now - self._debounce_since_ms < DEBOUNCE_MS:
            return None  # not stable long enough yet
        return snap
