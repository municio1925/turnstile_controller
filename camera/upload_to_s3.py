import asyncio
import aiohttp
import logging
import os
import aioboto3
from botocore.exceptions import ClientError
from systemd.journal import JournalHandler
from tenacity import retry, stop_after_attempt, wait_exponential

from utils import login

logger = logging.getLogger("VideoUploader")
logger.setLevel(logging.INFO)
journal_handler = JournalHandler()
logger.addHandler(journal_handler)
logger.propagate = False


class VideoUploader:
    def __init__(self, settings):
        self.settings = settings
        self.jwt_token = login(settings.HOSTNAME, settings.USERNAME, settings.PASSWORD, logger)


    async def ensure_bucket_exists(self, s3_client):
        """Ensure the S3 bucket exists. Create it if not."""
        bucket_name = self.settings.GYM_UUID
        try:
            await s3_client.head_bucket(Bucket=bucket_name)
            logger.info(f"Bucket {bucket_name} exists.")
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                logger.info(f"Bucket {bucket_name} does not exist. Creating it...")
                try:
                    await s3_client.create_bucket(Bucket=bucket_name)
                    logger.info(f"Bucket {bucket_name} created successfully.")
                except ClientError as create_error:
                    logger.error(f"Failed to create bucket {bucket_name}: {create_error}")
                    raise
            else:
                logger.error(f"Error checking bucket {bucket_name}: {e}")
                raise

    async def upload_file_to_s3(self, s3_client, file_path):
        """Upload a file to S3 with the gym_uuid prepended to the filename."""
        file_name = os.path.basename(file_path)
        bucket_name = self.settings.GYM_UUID
        s3_key = file_name

        try:
            logger.info(f"Uploading {file_name} as {s3_key} to bucket {bucket_name}...")
            await s3_client.upload_file(file_path, bucket_name, s3_key)
            logger.info(f"Successfully uploaded {file_name} to {bucket_name}/{s3_key}.")

            # Delete the file after successful upload
            os.remove(file_path)
            logger.info(f"Deleted local file: {file_path}")

            entrance_log_uuid = os.path.splitext(file_name)[0]
            await asyncio.sleep(5)
            await self.set_has_video_to_true_in_db(entrance_log_uuid)
        except ClientError as e:
            logger.error(f"Failed to upload {file_name}: {e}")
        except OSError as e:
            logger.error(f"Failed to delete file {file_path}: {e}")

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=2), reraise=True)
    async def set_has_video_to_true_in_db(self, entrance_log_uuid):
        url = f"{self.settings.HOSTNAME}/verify_customer/"
        payload = {"has_video": True, "uuid": str(entrance_log_uuid)}
        headers = {
            "Authorization": f"Bearer {self.jwt_token}",
            "Content-Type": "application/json",
        }

        async with aiohttp.ClientSession() as session:
            async with session.patch(url, headers=headers, json=payload) as response:
                response_text = await response.text()

                # Log the response text for debugging
                logger.debug(f"Response text: {response_text}")

                # Check status code and handle accordingly
                if response.status == 200:
                    logger.info(
                        f"Successfully updated has_video for UUID {entrance_log_uuid}. Response: {response_text}"
                    )
                elif response.status in (401, 403):
                    logger.error(
                        f"Unauthorized to update has_video for UUID {entrance_log_uuid}. "
                        f"Status: {response.status}, Response: {response_text}. Refreshing JWT token..."
                    )
                    # Refresh the JWT token
                    self.jwt_token = login(self.settings.HOSTNAME, self.settings.USERNAME, self.settings.PASSWORD)
                    raise aiohttp.ClientResponseError(
                        request_info=response.request_info,
                        history=response.history,
                        status=response.status,
                        message=f"Unauthorized: {response_text}"
                    )
                else:
                    logger.error(
                        f"Failed to update has_video for UUID {entrance_log_uuid}. "
                        f"Status: {response.status}, Response: {response_text}"
                    )
                    # Raise an exception for non-successful responses
                    raise aiohttp.ClientResponseError(
                        request_info=response.request_info,
                        history=response.history,
                        status=response.status,
                        message=f"Unexpected error: {response_text}"
                    )

    async def upload_loop(self):
        """Main loop to check the directory and upload files."""
        try:
            bucket_name = self.settings.GYM_UUID
            session = aioboto3.Session()
            async with session.client(
                "s3",
                aws_access_key_id=self.settings.S3_ACCESS_KEY,
                aws_secret_access_key=self.settings.S3_SECRET_ACCESS_KEY,
                endpoint_url=self.settings.S3_ENDPOINT_URL,
            ) as s3_client:
                # Ensure the bucket exists on startup
                await self.ensure_bucket_exists(s3_client)

                # Start the upload loop
                while True:
                    # List all files in the recording directory
                    files = [
                        os.path.join(self.settings.RECORDING_DIR, f)
                        for f in os.listdir(self.settings.RECORDING_DIR)
                        if os.path.isfile(os.path.join(self.settings.RECORDING_DIR, f))
                    ]
                    video_files = [f for f in files if f.endswith(".mp4") and not "temp" in f]

                    if video_files:
                        logger.info(f"Found {len(video_files)} video files to upload...")
                        tasks = [self.upload_file_to_s3(s3_client, file) for file in video_files]
                        await asyncio.gather(*tasks)
                    else:
                        logger.debug("No files found to upload.")

                    # Sleep for n seconds
                    await asyncio.sleep(1)
        except asyncio.CancelledError:
            logger.info("Upload loop cancelled.")
            raise
        except Exception as e:
            logger.exception(f"Exception in upload_loop {e}")
        finally:
            logger.info("Upload loop exiting.")


# Example Usage
if __name__ == "__main__":
    import asyncio

    class Settings:
        HOSTNAME = "https://example.com"
        USERNAME = "user@example.com"
        PASSWORD = "securepassword"
        GYM_UUID = "my-gym"
        S3_ACCESS_KEY = "your-s3-access-key"
        S3_SECRET_ACCESS_KEY = "your-s3-secret-key"
        S3_ENDPOINT_URL = "https://s3.amazonaws.com"
        RECORDING_DIR = "/path/to/recordings"

    settings = Settings()
    uploader = VideoUploader(settings)

    try:
        asyncio.run(uploader.upload_loop())
    except KeyboardInterrupt:
        uploader.logger.info("Program terminated by user.")
