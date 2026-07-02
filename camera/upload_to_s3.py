import asyncio
import logging
import os
import shutil
import subprocess
import sys

import boto3
from botocore.exceptions import ClientError
from systemd.journal import JournalHandler
from botocore.config import Config
import sentry_sdk

# Add the global Python library path to sys.path to use cv2 just like in the videorecorder
sys.path.append("/usr/lib/python3/dist-packages")
import cv2


logger = logging.getLogger("VideoUploader")
logger.setLevel(logging.INFO)
journal_handler = JournalHandler()
logger.addHandler(journal_handler)
logger.propagate = False


class VideoUploader:
    def __init__(self, settings):
        self.settings = settings

    def _build_s3_config(self):
        # Storj rejects botocore's newer checksum/trailer upload path unless we
        # force the more conservative request mode and path-style addressing.
        return Config(
            retries={"max_attempts": 1, "mode": "standard"},
            connect_timeout=30,
            read_timeout=300,
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            s3={"addressing_style": "path"},
        )

    def _build_sync_s3_client(self):
        return boto3.client(
            "s3",
            aws_access_key_id=self.settings.S3_ACCESS_KEY,
            aws_secret_access_key=self.settings.S3_SECRET_ACCESS_KEY,
            endpoint_url=self.settings.S3_ENDPOINT_URL,
            config=self._build_s3_config(),
        )

    async def ensure_bucket_exists(self, s3_client):
        """Ensure the S3 bucket exists. Create it if not."""
        bucket_name = self.settings.GYM_UUID
        def ensure_bucket_exists_sync():
            try:
                s3_client.head_bucket(Bucket=bucket_name)
            except ClientError as e:
                if e.response["Error"]["Code"] == "404":
                    logger.info(f"Bucket {bucket_name} does not exist. Creating it...")
                    try:
                        s3_client.create_bucket(Bucket=bucket_name)
                        logger.info(f"Bucket {bucket_name} created successfully.")
                    except ClientError as create_error:
                        logger.error(f"Failed to create bucket {bucket_name}: {create_error}")
                        raise
                else:
                    logger.error(f"Error checking bucket {bucket_name}: {e}")
                    raise

        await asyncio.to_thread(ensure_bucket_exists_sync)

    async def flip_video(self, input_file_path, output_file_path):
        """Flip the video upside down and mirror it left to right."""
        cap = cv2.VideoCapture(input_file_path)
        if not cap.isOpened():
            logger.error(f"Could not open video file: {input_file_path}")
            return False

        # Read video properties
        fps = cap.get(cv2.CAP_PROP_FPS)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        codec_candidates = []
        configured_codec = str(getattr(self.settings, "VIDEO_CODEC", "") or "").strip()
        if configured_codec:
            codec_candidates.append(configured_codec)
        codec_candidates.extend(["mp4v", "avc1", "MJPG"])

        out = None
        chosen_codec = None
        for codec_name in codec_candidates:
            fourcc = cv2.VideoWriter_fourcc(*codec_name)
            candidate = cv2.VideoWriter(output_file_path, fourcc, fps, (width, height))
            if candidate.isOpened():
                out = candidate
                chosen_codec = codec_name
                break
            candidate.release()

        if out is None:
            logger.error(f"Could not open flipped video writer for: {output_file_path}")
            cap.release()
            return False
        logger.info(f"Flipped video writer using codec {chosen_codec}.")

        while True:
            ret, frame = cap.read()
            if not ret:
                break
            # First flip upside down (vertical flip)
            flipped_frame = cv2.flip(frame, 0)
            # Then mirror left to right (horizontal flip)
            flipped_frame = cv2.flip(flipped_frame, 1)
            out.write(flipped_frame)

        cap.release()
        out.release()
        return os.path.exists(output_file_path) and os.path.getsize(output_file_path) > 0

    def _build_ffmpeg_command(self, input_file_path, output_file_path):
        command = [
            "ffmpeg",
            "-y",
            "-i",
            input_file_path,
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
        ]
        if getattr(self.settings, "FLIP_VIDEO", False):
            command.extend(["-vf", "vflip,hflip"])
        command.append(output_file_path)
        return command

    async def prepare_video_for_upload(self, input_file_path):
        """
        Produce a browser-friendly mp4 for upload.

        Prefer ffmpeg so the final file is H.264/yuv420p with a front-loaded
        moov atom. Fall back to the original file if ffmpeg is unavailable and
        no flip is needed, or to the OpenCV flipper if a flip is required.
        """
        ffmpeg_path = shutil.which("ffmpeg")
        if ffmpeg_path:
            processed_file_path = f"{input_file_path}.upload.mp4"
            command = self._build_ffmpeg_command(input_file_path, processed_file_path)
            logger.info("Preparing %s for upload via ffmpeg.", os.path.basename(input_file_path))
            completed = await asyncio.to_thread(
                subprocess.run,
                command,
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode == 0 and os.path.exists(processed_file_path) and os.path.getsize(processed_file_path) > 0:
                return processed_file_path
            logger.error("ffmpeg preprocessing failed: %s", completed.stderr.strip())
            if os.path.exists(processed_file_path):
                os.remove(processed_file_path)

        if getattr(self.settings, "FLIP_VIDEO", False):
            flipped_file_path = input_file_path + ".flipped.mp4"
            logger.info(f"Flipping and mirroring video {os.path.basename(input_file_path)} before upload...")
            success = await self.flip_video(input_file_path, flipped_file_path)
            if success:
                return flipped_file_path
            logger.error("Flipping video failed, uploading original.")

        return input_file_path

    async def upload_file_to_s3(self, s3_client, file_path):
        """Upload a file to S3, optionally flipping and mirroring it first if configured."""
        file_name = os.path.basename(file_path)
        bucket_name = self.settings.GYM_UUID
        s3_key = file_name

        file_path_to_upload = await self.prepare_video_for_upload(file_path)

        logger.info(f"Uploading {os.path.basename(file_path_to_upload)} as {s3_key} to bucket {bucket_name}...")

        def upload_sync():
            content_length = os.path.getsize(file_path_to_upload)
            with open(file_path_to_upload, "rb") as handle:
                body = handle.read()
            s3_client.put_object(
                Bucket=bucket_name,
                Key=s3_key,
                Body=body,
                ContentType="video/mp4",
                ContentLength=content_length,
            )

        try:
            await asyncio.to_thread(upload_sync)
        except Exception:
            # Upload failed: drop the pre-processed artifact so it is never
            # re-ingested as a fresh recording on a later pass. That re-ingestion
            # (an ffmpeg output that itself ends in ".mp4") is what produced the
            # <name>.upload.mp4.upload.mp4... filename mangling. Keep the original
            # recording so it can be retried cleanly.
            if file_path_to_upload != file_path and os.path.exists(file_path_to_upload):
                os.remove(file_path_to_upload)
            raise
        logger.info(f"Successfully uploaded {file_name} to {bucket_name}/{s3_key}.")

        # Delete local files
        if os.path.exists(file_path):
            os.remove(file_path)
        if file_path_to_upload != file_path and os.path.exists(file_path_to_upload):
            os.remove(file_path_to_upload)
        logger.info(f"Deleted local file(s) related to {file_name}")

    async def upload(self, video_files):
        """Main loop to check the directory and upload files."""
        s3_client = self._build_sync_s3_client()
        await self.ensure_bucket_exists(s3_client)

        logger.info(f"Found {len(video_files)} video files to upload...")
        for file in video_files:
            logger.info(f"Uploading {file}...")
            await self.upload_file_to_s3(s3_client, file)
            break  # better to not keep uploading because there might be a newer one with higher prio
    @staticmethod
    def _is_uploadable_recording(path):
        """True only for a raw recording (<uuid>.mp4), never our own
        pre-processing artifacts. Excluding the ffmpeg/flip outputs is what
        stops them being re-ingested and mangled into
        <name>.upload.mp4.upload.mp4..."""
        name = os.path.basename(path)
        if not name.endswith(".mp4") or "temp" in name:
            return False
        if name.endswith(".upload.mp4") or name.endswith(".flipped.mp4"):
            return False
        return True

    async def upload_loop(self):
        """Run the upload loop continuously."""
        while True:
            try:
                # List all files in the recording directory and sort them by modification time (newest first)
                files = sorted(
                    [
                        os.path.join(self.settings.RECORDING_DIR, f)
                        for f in os.listdir(self.settings.RECORDING_DIR)
                        if os.path.isfile(os.path.join(self.settings.RECORDING_DIR, f))
                    ],
                    key=lambda x: os.path.getmtime(x),
                    reverse=True
                )
                video_files = [f for f in files if self._is_uploadable_recording(f)]
                if video_files:
                    await self.upload(video_files)
            except Exception as exc:
                # Never let a transient failure (e.g. an S3 outage) crash the
                # process: a crash makes systemd restart us, and each restart
                # re-runs ffmpeg on the leftover artifact, appending another
                # ".upload.mp4" — that is the filename mangling + object bloat.
                logger.error("Upload cycle failed, will retry: %s", exc)
            await asyncio.sleep(5)


# Example Usage
if __name__ == "__main__":
    import dotenv

    # the path of the .env file which is in the directory that is one level up from the current directory
    path_to_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.env")

    dotenv.load_dotenv(path_to_env)

    sentry_sdk.init(
        dsn=os.getenv("SENTRY_DSN"),
        environment=os.getenv("SENTRY_ENV"),
        traces_sample_rate=1.0,
    )

    class Settings:
        """Configuration settings for camera video uploads and S3 integration."""

        S3_BUCKET = os.getenv("S3_BUCKET")
        S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY")
        S3_SECRET_ACCESS_KEY = os.getenv("S3_SECRET_ACCESS_KEY")
        S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL")
        GYM_UUID = os.getenv("GYM_UUID")
        RECORDING_DIR = os.getenv("RECORDING_DIR")
        HOSTNAME = os.getenv("HOSTNAME")
        USERNAME = os.getenv("USERNAME")
        PASSWORD = os.getenv("PASSWORD")
        FLIP_VIDEO = os.getenv("FLIP_VIDEO", "False").lower() == "true"
        VIDEO_CODEC = os.getenv("VIDEO_CODEC", "")

    settings = Settings()
    uploader = VideoUploader(settings)

    try:
        asyncio.run(uploader.upload_loop())
    except KeyboardInterrupt:
        logger.info("Program terminated by user.")
