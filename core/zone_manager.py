from __future__ import annotations
import time
import cv2
import numpy as np
from models.config_schema import MonitorConfig, Zone, Line, Event
from core.tracker import TrackedObject


# ======================================
# -------- ROBUSTNESS KNOBS --------
# ======================================
# Centroid EMA — smooth tracker-reported box centers before zone-/line-
# checks so detector-side jitter (especially RF-DETR's transformer queries,
# which wobble more than YOLO's anchor grid) doesn't fire false line-cross
# or zone-enter events. ``alpha`` = weight given to the new raw centroid;
# 1.0 = no smoothing (legacy behavior), 0.3-0.5 is typical for ~25 fps.
CENTROID_EMA_ALPHA = 0.4

# Line-cross debounce — only count a crossing once the object has been on
# the new side for this many consecutive frames. Filters jitter-induced
# false sign-flips on detectors with noisy box edges. 1 = legacy
# behavior (count every sign flip). At 25 fps, 3 = 120 ms of confirmation.
LINE_CROSS_MIN_FRAMES = 3


class ZoneLineManager:
    """Evaluates zone containment and line crossing events per frame.

    Zones use a 4-state debounce machine per (object_id, zone_id):
        outside → pending_enter → inside → pending_exit → outside
    where the pending phases require continuous presence/absence for
    ``zone.min_inside_seconds`` before promoting to the stable phase. With
    threshold 0 this collapses to instantaneous transitions (current
    behavior). Each state entry is ``{"phase": str, "since": float_ts}``.

    Lines use a side-confirmation state machine per (object_id, line_id):
    each crossing must persist for ``LINE_CROSS_MIN_FRAMES`` consecutive
    frames on the new side before being counted, which absorbs
    sub-pixel-scale centroid jitter that would otherwise fire
    false back-and-forth events.

    Centroids are EMA-smoothed before either check (see CENTROID_EMA_ALPHA).
    """

    def __init__(self):
        self._config: MonitorConfig | None = None
        # (object_id, zone_id) -> {"phase": str, "since": float}
        self._zone_state: dict[tuple[int, str], dict] = {}
        # (object_id, line_id) -> {"stable_side": int (-1 or +1),
        #                          "pending_side": int (0/-1/+1),
        #                          "pending_count": int,
        #                          "pre_change_centroid": (cx, cy)}
        self._line_state: dict[tuple[int, str], dict] = {}
        # object_id -> (cx_smoothed, cy_smoothed) — running EMA of centroid.
        self._centroid_ema: dict[int, tuple[float, float]] = {}

    def set_config(self, config: MonitorConfig):
        self._config = config
        self._zone_state.clear()
        self._line_state.clear()
        self._centroid_ema.clear()

    def _smoothed_centroid(self, obj_id: int,
                           raw: tuple[int, int]) -> tuple[int, int]:
        """Return an EMA-smoothed centroid for ``obj_id``, updating the
        rolling state. First observation passes through unchanged; later
        ones blend with the previous smoothed value at ``CENTROID_EMA_ALPHA``.
        Returns ints to match the existing ``TrackedObject.centroid`` type."""
        prev = self._centroid_ema.get(obj_id)
        rx, ry = float(raw[0]), float(raw[1])
        if prev is None:
            self._centroid_ema[obj_id] = (rx, ry)
            return raw
        sx = CENTROID_EMA_ALPHA * rx + (1.0 - CENTROID_EMA_ALPHA) * prev[0]
        sy = CENTROID_EMA_ALPHA * ry + (1.0 - CENTROID_EMA_ALPHA) * prev[1]
        self._centroid_ema[obj_id] = (sx, sy)
        return (int(round(sx)), int(round(sy)))

    def update(self, tracked_objects: dict[int, TrackedObject],
               now: float | None = None,
               expired_ids: set[int] | None = None) -> list[Event]:
        """Check all zones and lines against tracked objects. Returns new events.

        ``now`` is the per-frame timestamp from the runner (content time for
        file sources, wall clock for live). Used as the event timestamp and
        as the debounce timer so dwell stats in the report reflect what
        happened in the video, not how fast we processed it.

        ``expired_ids``: obj_ids that have been missing long enough to be
        considered truly gone (drives state cleanup). When None — the
        legacy behavior — any obj_id not in ``tracked_objects`` is purged,
        which causes dwell timers to reset on a single missed-detection
        frame. The runner provides the grace-aware set."""
        if self._config is None:
            return []
        if now is None:
            now = time.time()

        events = []
        events.extend(self._check_zones(tracked_objects, now))
        events.extend(self._check_lines(tracked_objects, now))
        self._cleanup(tracked_objects, expired_ids)
        return events

    def _check_zones(self, tracked: dict[int, TrackedObject],
                     now: float) -> list[Event]:
        events = []

        for obj_id, obj in tracked.items():
            for zone in self._config.zones:
                if not zone.enabled:
                    continue
                if zone.target_classes and obj.class_name not in zone.target_classes:
                    continue

                polygon = np.array(zone.points, dtype=np.float32)
                centroid = self._smoothed_centroid(obj_id, obj.centroid)
                inside = cv2.pointPolygonTest(polygon, centroid, False) >= 0
                threshold = max(0.0, float(getattr(zone, "min_inside_seconds", 0.0)))

                key = (obj_id, zone.id)
                state = self._zone_state.get(key)
                phase = state["phase"] if state else "outside"
                since = state["since"] if state else now

                if phase == "outside":
                    if inside:
                        if threshold <= 0.0:
                            # No debounce — fire immediately like before.
                            events.append(self._make_zone_event(
                                "zone_enter", zone, obj_id, obj, now))
                            self._zone_state[key] = {"phase": "inside",
                                                     "since": now}
                        else:
                            self._zone_state[key] = {"phase": "pending_enter",
                                                     "since": now}
                elif phase == "pending_enter":
                    if not inside:
                        # Brief incursion — discard as jitter.
                        self._zone_state[key] = {"phase": "outside",
                                                 "since": now}
                    elif now - since >= threshold:
                        events.append(self._make_zone_event(
                            "zone_enter", zone, obj_id, obj, now))
                        self._zone_state[key] = {"phase": "inside",
                                                 "since": now}
                    # else: still inside but under threshold — keep waiting.
                elif phase == "inside":
                    if not inside:
                        if threshold <= 0.0:
                            events.append(self._make_zone_event(
                                "zone_exit", zone, obj_id, obj, now))
                            self._zone_state[key] = {"phase": "outside",
                                                     "since": now}
                        else:
                            self._zone_state[key] = {"phase": "pending_exit",
                                                     "since": now}
                elif phase == "pending_exit":
                    if inside:
                        # Came back — cancel pending exit.
                        self._zone_state[key] = {"phase": "inside",
                                                 "since": now}
                    elif now - since >= threshold:
                        events.append(self._make_zone_event(
                            "zone_exit", zone, obj_id, obj, now))
                        self._zone_state[key] = {"phase": "outside",
                                                 "since": now}
        return events

    def _make_zone_event(self, event_type: str, zone, obj_id: int,
                         obj: TrackedObject, ts: float) -> Event:
        return Event(
            timestamp=ts,
            event_type=event_type,
            region_id=zone.id,
            region_name=zone.name,
            object_id=obj_id,
            class_name=obj.class_name,
        )

    def is_in_zone_for_overstay(self, obj_id: int, zone_id: str) -> bool:
        """Debounced "inside" view, exposed for the runner's overstay tracker
        so a centroid wobble (which is absorbed by the state machine and does
        NOT fire zone_exit) doesn't reset the dwell timer either.

        Returns True for ``inside`` and ``pending_exit`` phases — i.e. the
        zone considers this object present, even if a recent frame showed it
        briefly outside. ``pending_enter`` returns False because the object
        hasn't been confirmed inside yet (zone_enter hasn't fired)."""
        state = self._zone_state.get((obj_id, zone_id))
        return bool(state and state["phase"] in ("inside", "pending_exit"))

    def is_object_in_any_zone_polygon(self, obj_id: int) -> bool:
        """True if the object's centroid is currently inside any zone's
        polygon — including the pending_enter window before zone_enter has
        actually fired. Used by the area-overstay tracker so the debounce
        delay doesn't leak into "loitering time" calculations: as soon as
        the centroid touches a zone, the area should consider it "in zone".
        """
        for (oid, _), state in self._zone_state.items():
            if oid == obj_id and state["phase"] != "outside":
                return True
        return False

    def _check_lines(self, tracked: dict[int, TrackedObject],
                     now: float) -> list[Event]:
        """Side-confirmation state machine. For each (obj_id, line):
          * On first observation, record which side the object is on.
          * If subsequent frame shows a different side, start a pending
            transition. Increment a counter as long as the object stays on
            that candidate side; reset if it bounces back.
          * When the counter reaches ``LINE_CROSS_MIN_FRAMES``, fire the
            crossing event (validated against the line segment) and commit
            the new side as the stable one.

        Smoothed centroids (via ``_smoothed_centroid``) feed both the
        side-check and the within-segment check, so detector-side jitter
        is dampened before the state machine sees it.
        """
        events = []

        for obj_id, obj in tracked.items():
            centroid = self._smoothed_centroid(obj_id, obj.centroid)

            for line in self._config.lines:
                if not line.enabled:
                    continue
                if line.target_classes and obj.class_name not in line.target_classes:
                    continue

                lx = line.end[0] - line.start[0]
                ly = line.end[1] - line.start[1]
                cross = (lx * (centroid[1] - line.start[1])
                         - ly * (centroid[0] - line.start[0]))
                if cross == 0:
                    # Exactly on the line — treat as no-change to avoid
                    # arbitrary zero-sign behavior.
                    continue
                curr_side = 1 if cross > 0 else -1

                key = (obj_id, line.id)
                state = self._line_state.get(key)
                if state is None:
                    # First observation near this line — just record side.
                    self._line_state[key] = {
                        "stable_side": curr_side,
                        "pending_side": 0,
                        "pending_count": 0,
                        "pre_change_centroid": centroid,
                    }
                    continue

                if curr_side == state["stable_side"]:
                    # Still on the stable side — refresh the pre-change
                    # anchor and clear any in-progress pending crossing.
                    state["pre_change_centroid"] = centroid
                    state["pending_side"] = 0
                    state["pending_count"] = 0
                    continue

                # On the opposite side from stable. Accumulate / restart
                # the pending counter as appropriate.
                if state["pending_side"] != curr_side:
                    state["pending_side"] = curr_side
                    state["pending_count"] = 1
                else:
                    state["pending_count"] += 1

                if state["pending_count"] < LINE_CROSS_MIN_FRAMES:
                    continue  # not yet confirmed

                # Confirmed crossing — validate it happened within the
                # line segment using the pre-change anchor (where the
                # object was while still on the stable side) and the
                # current confirmed-side centroid.
                if not self._crossing_within_segment(
                        state["pre_change_centroid"], centroid, line):
                    # Trajectory bypassed the segment — commit the side
                    # change so we don't keep re-firing, but no event.
                    state["stable_side"] = curr_side
                    state["pre_change_centroid"] = centroid
                    state["pending_side"] = 0
                    state["pending_count"] = 0
                    continue

                is_a_to_b = state["stable_side"] < 0  # was on A (- side) → now on B
                if line.invert:
                    is_a_to_b = not is_a_to_b
                event_type = "line_in" if is_a_to_b else "line_out"
                events.append(Event(
                    timestamp=now,
                    event_type=event_type,
                    region_id=line.id,
                    region_name=line.name,
                    object_id=obj_id,
                    class_name=obj.class_name,
                ))
                # Commit the new stable side.
                state["stable_side"] = curr_side
                state["pre_change_centroid"] = centroid
                state["pending_side"] = 0
                state["pending_count"] = 0

        return events

    def _crossing_within_segment(self, p1: tuple, p2: tuple, line: Line) -> bool:
        """Check that the trajectory p1->p2 crosses within the line segment bounds."""
        # Line segment: A -> B
        ax, ay = line.start
        bx, by = line.end
        # Trajectory: p1 -> p2
        cx, cy = p1
        dx, dy = p2

        denom = (bx - ax) * (dy - cy) - (by - ay) * (dx - cx)
        if abs(denom) < 1e-10:
            return False

        t = ((cx - ax) * (dy - cy) - (cy - ay) * (dx - cx)) / denom
        return 0.0 <= t <= 1.0

    def _cleanup(self, tracked: dict[int, TrackedObject],
                 expired_ids: set[int] | None = None):
        """Remove state entries for objects no longer tracked.

        Prefers the runner-supplied ``expired_ids`` (grace-aware) so a
        brief detection miss does not drop the zone/line state. Falls back
        to the legacy "anything not in current frame" behavior for callers
        that still pass two args (tests, headless harnesses)."""
        if expired_ids is not None:
            self._zone_state = {k: v for k, v in self._zone_state.items()
                                if k[0] not in expired_ids}
            self._line_state = {k: v for k, v in self._line_state.items()
                                if k[0] not in expired_ids}
            for oid in expired_ids:
                self._centroid_ema.pop(oid, None)
            return
        active_ids = set(tracked.keys())
        self._zone_state = {k: v for k, v in self._zone_state.items() if k[0] in active_ids}
        self._line_state = {k: v for k, v in self._line_state.items() if k[0] in active_ids}
        self._centroid_ema = {oid: c for oid, c in self._centroid_ema.items()
                              if oid in active_ids}

    def reset(self):
        self._zone_state.clear()
        self._line_state.clear()
        self._centroid_ema.clear()
