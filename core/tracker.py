from __future__ import annotations
from dataclasses import dataclass
import numpy as np
from scipy.optimize import linear_sum_assignment


@dataclass
class TrackedObject:
    object_id: int
    centroid: tuple[int, int]
    prev_centroid: tuple[int, int] | None
    class_id: int
    class_name: str
    disappeared: int = 0


class CentroidTracker:
    """Simple centroid tracker using Hungarian algorithm for frame-to-frame association."""

    def __init__(self, max_disappeared: int = 15, max_distance: float = 150.0):
        self._next_id = 0
        self._objects: dict[int, TrackedObject] = {}
        self._max_disappeared = max_disappeared
        self._max_distance = max_distance

    def update(self, detections: list[tuple[int, int, int, int, int, str]]) -> dict[int, TrackedObject]:
        """
        Update tracker with new detections.
        Each detection: (x1, y1, x2, y2, class_id, class_name)
        Returns dict of object_id -> TrackedObject
        """
        # Compute centroids from bounding boxes
        input_centroids = []
        input_meta = []
        for x1, y1, x2, y2, cls_id, cls_name in detections:
            cx = (x1 + x2) // 2
            cy = (y1 + y2) // 2
            input_centroids.append((cx, cy))
            input_meta.append((cls_id, cls_name))

        # No existing objects — register all
        if len(self._objects) == 0:
            for i, centroid in enumerate(input_centroids):
                self._register(centroid, input_meta[i][0], input_meta[i][1])
            return dict(self._objects)

        # No detections — mark all as disappeared
        if len(input_centroids) == 0:
            to_remove = []
            for obj_id, obj in self._objects.items():
                obj.disappeared += 1
                if obj.disappeared > self._max_disappeared:
                    to_remove.append(obj_id)
            for obj_id in to_remove:
                del self._objects[obj_id]
            return dict(self._objects)

        # Build distance matrix
        obj_ids = list(self._objects.keys())
        obj_centroids = [(self._objects[oid].centroid[0], self._objects[oid].centroid[1]) for oid in obj_ids]

        D = np.zeros((len(obj_centroids), len(input_centroids)), dtype=np.float64)
        for i, (ox, oy) in enumerate(obj_centroids):
            for j, (ix, iy) in enumerate(input_centroids):
                D[i, j] = np.sqrt((ox - ix) ** 2 + (oy - iy) ** 2)

        # Hungarian algorithm
        row_idx, col_idx = linear_sum_assignment(D)

        used_rows = set()
        used_cols = set()

        for r, c in zip(row_idx, col_idx):
            if D[r, c] > self._max_distance:
                continue

            obj_id = obj_ids[r]
            obj = self._objects[obj_id]
            obj.prev_centroid = obj.centroid
            obj.centroid = input_centroids[c]
            obj.class_id = input_meta[c][0]
            obj.class_name = input_meta[c][1]
            obj.disappeared = 0

            used_rows.add(r)
            used_cols.add(c)

        # Handle unmatched existing objects
        for r in range(len(obj_ids)):
            if r not in used_rows:
                obj_id = obj_ids[r]
                self._objects[obj_id].disappeared += 1
                if self._objects[obj_id].disappeared > self._max_disappeared:
                    del self._objects[obj_id]

        # Handle unmatched new detections
        for c in range(len(input_centroids)):
            if c not in used_cols:
                self._register(input_centroids[c], input_meta[c][0], input_meta[c][1])

        return dict(self._objects)

    def reset(self):
        self._objects.clear()
        self._next_id = 0

    def _register(self, centroid: tuple[int, int], cls_id: int, cls_name: str):
        obj = TrackedObject(
            object_id=self._next_id,
            centroid=centroid,
            prev_centroid=None,
            class_id=cls_id,
            class_name=cls_name,
        )
        self._objects[self._next_id] = obj
        self._next_id += 1
