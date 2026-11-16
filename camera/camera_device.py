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
        is_rtsp = isinstance(candidate, str) and candidate.lower().startswith(("rtsp://", "rtsps://"))
        if is_rtsp:
            # OpenCV/FFmpeg otherwise lets the RTSP queue grow while the
            # recorder is idle (health checks read only one frame every few
            # seconds). Keep the live frame instead of replaying stale video
            # when a QR trigger arrives.
            os.environ.setdefault(
                "OPENCV_FFMPEG_CAPTURE_OPTIONS",
                "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|max_delay;0",
            )
            ffmpeg_backend = getattr(cv2, "CAP_FFMPEG", None)
            try:
                video = cv2.VideoCapture(candidate, ffmpeg_backend) if ffmpeg_backend is not None else cv2.VideoCapture(candidate)
            except TypeError:
                video = cv2.VideoCapture(candidate)
            buffer_property = getattr(cv2, "CAP_PROP_BUFFERSIZE", None)
            if buffer_property is not None:
                video.set(buffer_property, 1)
        else:
            video = cv2.VideoCapture(candidate)
        video.set(cv2.CAP_PROP_FRAME_WIDTH, frame_width)
        video.set(cv2.CAP_PROP_FRAME_HEIGHT, frame_height)
        if video.isOpened():
            return video, candidate, attempted_candidates
        video.release()
    return None, None, attempted_candidates
