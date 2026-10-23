import os
from pathlib import Path


def parse_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def iter_camera_candidates():
    preferred_device = str(os.getenv("CAMERA_DEVICE", "")).strip()
    if preferred_device:
        yield preferred_device

    preferred_index = parse_int(os.getenv("CAMERA_DEVICE_INDEX"), None)
    if preferred_index is not None:
        yield preferred_index

    sysfs_root = Path("/sys/class/video4linux")
    preferred_paths = []
    fallback_paths = []
    for device_path in sorted(Path("/dev").glob("video*")):
        sysfs_name_path = sysfs_root / device_path.name / "name"
        device_name = ""
        if sysfs_name_path.exists():
            try:
                device_name = sysfs_name_path.read_text().strip().lower()
            except OSError:
                device_name = ""
        if "webcam" in device_name or "camera" in device_name or "usb" in device_name:
            preferred_paths.append(str(device_path))
        else:
            fallback_paths.append(str(device_path))

    for candidate in preferred_paths + fallback_paths:
        yield candidate

    for candidate in (0, 1, 2):
        yield candidate


def open_camera_capture(cv2, frame_width, frame_height, preferred_source=None):
    candidates = [preferred_source] if preferred_source is not None else iter_camera_candidates()
    attempted_candidates = []
    for candidate in candidates:
        if candidate in attempted_candidates:
            continue
        attempted_candidates.append(candidate)
        video = cv2.VideoCapture(candidate)
        video.set(cv2.CAP_PROP_FRAME_WIDTH, frame_width)
        video.set(cv2.CAP_PROP_FRAME_HEIGHT, frame_height)
        if video.isOpened():
            return video, candidate, attempted_candidates
        video.release()
    return None, None, attempted_candidates
