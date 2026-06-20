import shutil
import time
import asyncio
import os
import signal
import uuid

import sys
import logging
from systemd.journal import JournalHandler
import aiofiles.os
import sentry_sdk

# Add the global Python library path and project paths used by systemd.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.append("/usr/lib/python3/dist-packages")
sys.path.append(SCRIPT_DIR)
sys.path.append(PROJECT_DIR)
import cv2

from camera_device import open_camera_capture
from usb_diagnostics import record_component_state
from inotify_watch import DirectoryWatcher

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("VideoCamera")
journal_handler = JournalHandler()
logger.addHandler(journal_handler)
logger.propagate = False


class VideoCamera:
    """Video camera class that records video upon QR data trigger."""

    def __init__(self, settings):
        self.RECORDING_DIR = settings.RECORDING_DIR
        self.FRAME_WIDTH = settings.FRAME_WIDTH
        self.FRAME_HEIGHT = settings.FRAME_HEIGHT
        self.FPS = settings.FPS

        # Video recording parameters
        self.VIDEO_CODEC = os.getenv("VIDEO_CODEC", "mp4v")  # Codec used for recording video
        self.VIDEO_FORMAT = "mp4"  # Final format of the recorded video files

        self.RECORDING_DURATION = 6  # Duration to record after trigger (in seconds)
        self.QR_DATA_CHECK_INTERVAL = 0.2  # Polling fallback interval if inotify is unavailable (seconds)
        # When the inotify watch is armed the loop wakes the instant record.txt
        # appears; this is only the idle fallback cadence so camera health checks
        # keep running and any missed event is caught quickly.
        self.TRIGGER_IDLE_WAKE_SECONDS = float(os.getenv("CAMERA_TRIGGER_IDLE_WAKE_SECONDS", 2))
        self.CAMERA_HEALTH_CHECK_INTERVAL = float(os.getenv("CAMERA_HEALTH_CHECK_INTERVAL", 5))

        # Recording variables
        self.recording = False
        self.recording_start_time = None
        self.last_camera_health_check = 0
        self.out = None
        self.video = None
        self.camera_device = None
        self.recording_file = None  # Temporary file path for recording
        self.current_qr_data = None  # Store QR data for filename

        # Adjust FPS based on actual camera performance during recording
        self.init_camera()

    def init_camera(self):
        self.cleanup()
        self.video, self.camera_device, attempted_candidates = open_camera_capture(cv2, self.FRAME_WIDTH, self.FRAME_HEIGHT)

        timestamp = int(time.time())
        self.recording_file = f"{self.RECORDING_DIR}/temp_{timestamp}_.{self.VIDEO_FORMAT}"

        # Check if camera is opened successfully
        if self.video is None or not self.video.isOpened():
            record_component_state("camera", False)
            logger.error("Failed to open camera. Candidates tried: %s", attempted_candidates)
            return

        # Read the first frame to get frame dimensions
        ret, frame = self.video.read()

        if not ret:
            record_component_state("camera", False)
            logger.error("Failed to read frame from camera.")
            self.video.release()
            self.video = None
            return

        record_component_state("camera", True)

        frame_size = (
            int(self.video.get(cv2.CAP_PROP_FRAME_WIDTH)),
            int(self.video.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        )
        codec_candidates = [self.VIDEO_CODEC, "mp4v", "avc1", "MJPG"]
        self.out = None
        for codec_name in codec_candidates:
            fourcc = cv2.VideoWriter_fourcc(*codec_name)
            writer = cv2.VideoWriter(self.recording_file, fourcc, self.FPS, frame_size)
            if writer.isOpened():
                self.out = writer
                self.VIDEO_CODEC = codec_name
                logger.info("Using video codec %s on device %s.", codec_name, self.camera_device)
                break
            writer.release()

        if self.out is None:
            record_component_state("camera", False)
            logger.error("Failed to initialize VideoWriter for camera device %s.", self.camera_device)
            self.video.release()
            self.video = None
            self.out = None

    def check_camera_health(self):
        if self.recording:
            return

        now = time.time()
        if now - self.last_camera_health_check < self.CAMERA_HEALTH_CHECK_INTERVAL:
            return
        self.last_camera_health_check = now

        if self.video is None or self.out is None or not self.video.isOpened():
            logger.warning("Camera is not ready. Reinitializing camera capture.")
            self.init_camera()
            return

        try:
            ret, _frame = self.video.read()
        except Exception as e:
            record_component_state("camera", False)
            logger.exception("Failed to read camera health-check frame: %s", e)
            self.cleanup()
            self.video = None
            self.out = None
            return

        if ret:
            record_component_state("camera", True)
            return

        record_component_state("camera", False)
        logger.error("Camera health check failed. Reinitializing camera capture.")
        self.cleanup()
        self.video = None
        self.out = None

    async def find_trigger(self):
        """Check for the existence of the record.txt file to start processing."""
        record_file_path = os.path.join(self.RECORDING_DIR, "record.txt")

        # Try to open the record.txt file asynchronously
        try:
            async with aiofiles.open(record_file_path, mode="r"):
                await aiofiles.os.remove(record_file_path)
                # File exists; continue processing
        except FileNotFoundError:
            # record.txt does not exist, no trigger
            return False

        # record.txt exists; proceed with listing and deleting files
        filenames = os.listdir(self.RECORDING_DIR)  # Synchronous call here
        txt_files = []
        for filename in filenames:
            if not filename.endswith(".txt"):
                continue
            trigger_name = filename[:-4]
            try:
                uuid.UUID(trigger_name)
            except ValueError:
                logger.warning("Ignoring non-UUID camera trigger file: %s", filename)
                continue
            txt_files.append(trigger_name)

        # Remove only valid trigger files asynchronously.
        for trigger_name in txt_files:
            file_path = os.path.join(self.RECORDING_DIR, f"{trigger_name}.txt")
            try:
                await aiofiles.os.remove(file_path)  # Asynchronous delete
            except FileNotFoundError:
                logger.warning(f"File {file_path} already deleted.")

        return txt_files

    def cleanup(self):
        """Release resources properly on exit."""
        if self.video and self.video.isOpened():
            self.video.release()
        if self.out:
            self.out.release()

    async def run(self, global_qr_data=None, lock=None):
        """Asynchronous method to check for QR data and start recording."""
        # Event-driven trigger: wake the instant the MQTT receiver drops
        # record.txt instead of polling every QR_DATA_CHECK_INTERVAL. Falls back
        # to the original polling if inotify can't be armed, so behaviour is
        # never worse than before.
        try:
            watcher = DirectoryWatcher(self.RECORDING_DIR)
            logger.info("Watching %s for triggers via inotify (instant).", self.RECORDING_DIR)
        except OSError as watch_err:
            logger.warning(
                "inotify unavailable (%s); falling back to %.1fs polling.",
                watch_err,
                self.QR_DATA_CHECK_INTERVAL,
            )
            watcher = None
        try:
            while True:
                self.check_camera_health()
                filenames = await self.find_trigger()
                if filenames:
                    data = {"uuid": filenames[0], "additional_uuids": filenames[1:]}
                    await self.start_recording(data)
                if watcher is None:
                    await asyncio.sleep(self.QR_DATA_CHECK_INTERVAL)
                else:
                    await watcher.wait(timeout=self.TRIGGER_IDLE_WAKE_SECONDS)
        except asyncio.CancelledError:
            logger.info("Run loop cancelled.")
            raise
        except Exception as e:
            logger.exception("Exception in VideoCamera.run")
        finally:
            if watcher is not None:
                watcher.close()
            # Clean up resources when done
            self.cleanup()

    async def start_recording(self, qr_data):
        """Start video recording."""
        self.current_qr_data = qr_data  # Store QR data for later use
        if self.video is None or self.out is None:
            self.init_camera()
        if self.video is None or self.out is None:
            logger.error("Camera is not ready for recording.")
            return

        start = time.time()

        self.recording = True
        self.recording_start_time = time.time()
        end_time = self.recording_start_time + self.RECORDING_DURATION

        # Record frames for RECORDING_DURATION seconds
        frame_interval = 1.0 / self.FPS

        logger.info(
            f"Started recording: {qr_data.get('uuid')}.{self.VIDEO_FORMAT}. Took {time.time() - start:.2f} seconds to init."
        )
        while time.time() < end_time:
            frame_start_time = time.time()

            ret, frame = self.video.read()
            if not ret:
                record_component_state("camera", False)
                logger.error("Failed to read frame from camera.")
                break
            self.out.write(frame)
            # Sleep to maintain FPS
            elapsed = time.time() - frame_start_time
            sleep_time = frame_interval - elapsed
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
        await self.stop_recording()
        self.init_camera()

    async def stop_recording(self):
        """Stop video recording."""
        if self.recording:
            if self.out:
                self.out.release()
            if self.video:
                self.video.release()
            self.out = None
            self.video = None
            self.recording = False

            if self.current_qr_data:
                uuid = self.current_qr_data.get("uuid", "unknown")
                new_filename = f"{self.RECORDING_DIR}/{uuid}.{self.VIDEO_FORMAT}"
                os.rename(self.recording_file, new_filename)
                self.recording_file = new_filename
                logger.info(f"Recording saved as {new_filename}")
                if self.current_qr_data.get("additional_uuids"):
                    for additional_uuid in self.current_qr_data.get("additional_uuids"):
                        new_filename = f"{self.RECORDING_DIR}/{additional_uuid}.{self.VIDEO_FORMAT}"
                        shutil.copy(self.recording_file, new_filename)
                        logger.info(f"Recording saved as {new_filename}")
                tmp_mp4_files = [f for f in os.listdir(self.RECORDING_DIR) if f.endswith(".mp4") and "temp" in f]
                for tmp_file in tmp_mp4_files:
                    os.remove(os.path.join(self.RECORDING_DIR, tmp_file))
            else:
                logger.warning("No QR data available to rename the recording file.")

        else:
            logger.info("No recording to stop.")


def read_and_delete_multi_process_qr_data(global_qr_data, lock):
    """
    Reads the value from multi_process_qr_data and deletes it.

    Args:
        global_qr_data (Manager.dict): The shared dictionary.
        lock (Manager.Lock): The lock to ensure thread-safe access.

    Returns:
        dict or None: The data that was read, or None if the dictionary is empty.
    """
    with lock:  # Acquire the lock to ensure thread safety
        if "qr_data" in global_qr_data:
            data = global_qr_data["qr_data"]
            del global_qr_data["qr_data"]  # Delete the data after reading
            return data
        else:
            return None  # No data available


# Initialize and run the video camera
async def main(settings, global_qr_data=None, lock=None):
    setproctitle.setproctitle("videocamera")
    camera = VideoCamera(settings)
    logger.info("Press Ctrl+C to stop.")

    # Start the run() coroutine as a task
    camera_task = asyncio.create_task(camera.run(global_qr_data, lock))

    # Handle signals
    loop = asyncio.get_running_loop()

    def shutdown():
        logger.info("Received shutdown signal.")
        for task in [camera_task]:
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown)

    tasks = [camera_task]

    try:
        # Wait for tasks to complete
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Main tasks cancelled.")
    except Exception as e:
        logger.exception("Exception in main")
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        camera.cleanup()
        logger.info("Exiting...")


def run_camera(settings, global_qr_data=None, lock=None):
    try:
        asyncio.run(main(settings, global_qr_data, lock))
    except Exception as e:
        logger.exception(f"Error running camera: {e}... Continuing")


if __name__ == "__main__":
    import setproctitle

    setproctitle.setproctitle("pyvideorecorder")
    import dotenv

    # the path of the .env file which is in the deirectory that is one level up from the current directory
    path_to_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.env")

    dotenv.load_dotenv(path_to_env)

    sentry_sdk.init(
        dsn=os.getenv("SENTRY_DSN"),
        environment=os.getenv("SENTRY_ENV"),
        traces_sample_rate=1.0,
    )

    # Add stream handler
    logger = logging.getLogger("VideoCameraDebugger")
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.DEBUG)
    logger.addHandler(stream_handler)

    class CameraSettings:
        """Configuration settings for camera video uploads and S3 integration."""

        RECORDING_DIR = os.getenv("RECORDING_DIR")
        FRAME_WIDTH = int(os.getenv("FRAME_WIDTH"))
        FRAME_HEIGHT = int(os.getenv("FRAME_HEIGHT"))
        FPS = int(os.getenv("FPS"))

    run_camera(CameraSettings())
