import cv2
import numpy as np

_color_cache: dict[int, tuple[int, int, int]] = {}


def get_class_color(cls_id: int) -> tuple[int, int, int]:
    """Deterministic BGR color for a class ID, cached."""
    if cls_id not in _color_cache:
        hue = (cls_id * 47) % 180
        hsv = np.array([[[hue, 220, 230]]], dtype=np.uint8)
        bgr = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0, 0]
        _color_cache[cls_id] = (int(bgr[0]), int(bgr[1]), int(bgr[2]))
    return _color_cache[cls_id]


def bgr_to_rgb(bgr: tuple[int, int, int]) -> tuple[int, int, int]:
    return (bgr[2], bgr[1], bgr[0])


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
