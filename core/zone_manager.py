from __future__ import annotations
import time
import cv2
import numpy as np
from models.config_schema import MonitorConfig, Zone, Line, Event
from core.tracker import TrackedObject


class ZoneLineManager:
    """Evaluates zone containment and line crossing events per frame.

    Zones use a 4-state debounce machine per (object_id, zone_id):
        outside → pending_enter → inside → pending_exit → outside
    where the pending phases require continuous presence/absence for
    ``zone.min_inside_seconds`` before promoting to the stable phase. With
    threshold 0 this collapses to instantaneous transitions (current
    behavior). Each state entry is ``{"phase": str, "since": float_ts}``.
    """

    def __init__(self):
        self._config: MonitorConfig | None = None
        # (object_id, zone_id) -> {"phase": str, "since": float}
        self._zone_state: dict[tuple[int, str], dict] = {}
        # (object_id, line_id) -> last cross product sign
        self._line_state: dict[tuple[int, str], float] = {}

    def set_config(self, config: MonitorConfig):
        self._config = config
        self._zone_state.clear()
        self._line_state.clear()

    def update(self, tracked_objects: dict[int, TrackedObject],
               now: float,
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
                inside = cv2.pointPolygonTest(polygon, obj.centroid, False) >= 0
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
        events = []

        for obj_id, obj in tracked.items():
            if obj.prev_centroid is None:
                continue

            for line in self._config.lines:
                if not line.enabled:
                    continue
                if line.target_classes and obj.class_name not in line.target_classes:
                    continue

                # Line vector
                lx = line.end[0] - line.start[0]
                ly = line.end[1] - line.start[1]

                # Cross product for previous and current centroid
                prev_cross = lx * (obj.prev_centroid[1] - line.start[1]) - \
                             ly * (obj.prev_centroid[0] - line.start[0])
                curr_cross = lx * (obj.centroid[1] - line.start[1]) - \
                             ly * (obj.centroid[0] - line.start[0])

                # Sign change means crossing
                if prev_cross * curr_cross < 0:
                    if not self._crossing_within_segment(obj.prev_centroid, obj.centroid, line):
                        continue

                    # Determine direction: A->B or B->A
                    # prev_cross < 0 means was on A side, now on B side = A->B
                    is_a_to_b = prev_cross < 0
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
            return
        active_ids = set(tracked.keys())
        self._zone_state = {k: v for k, v in self._zone_state.items() if k[0] in active_ids}
        self._line_state = {k: v for k, v in self._line_state.items() if k[0] in active_ids}

    def reset(self):
        self._zone_state.clear()
        self._line_state.clear()
