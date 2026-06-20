import logging
import os
import asyncio
import json
import time
from uuid import UUID

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

from utils import SentryLogger
from inotify_watch import DirectoryWatcher
import sentry_sdk

try:
    from systemd.journal import JournalHandler
except ImportError:  # pragma: no cover - only used on non-systemd dev machines
    class JournalHandler(logging.NullHandler):
        pass

# Load environment variables
load_dotenv()

sentry_sdk.init(
    dsn=os.getenv("SENTRY_DSN"),
    environment=os.getenv("SENTRY_ENV"),
    traces_sample_rate=1.0,
)

# === Constants ===
SCAN_INTERVAL_MS = 250
# Fallback rescan cadence when the inotify watch is armed: triggers normally
# wake the loop within ~1 ms, so this only catches the rare event that slipped
# past inotify (e.g. files queued while the broker was unreachable). Far less
# frequent than the old fixed poll, with none of the per-trigger latency.
TRIGGER_FALLBACK_SCAN_SECONDS = float(os.getenv("TRIGGER_FALLBACK_SCAN_SECONDS", 2))
MQTT_PUBLISH_RETRY_SECONDS = float(os.getenv("MQTT_PUBLISH_RETRY_SECONDS", 120))
MQTT_INITIAL_RETRY_DELAY_SECONDS = float(os.getenv("MQTT_INITIAL_RETRY_DELAY_SECONDS", 1))
MQTT_MAX_RETRY_DELAY_SECONDS = float(os.getenv("MQTT_MAX_RETRY_DELAY_SECONDS", 15))
MQTT_PUBLISH_TIMEOUT_SECONDS = float(os.getenv("MQTT_PUBLISH_TIMEOUT_SECONDS", 10))

# Set up our special logger
logging.setLoggerClass(SentryLogger)
logger = logging.getLogger("mqtt_sender")
logger.setLevel(logging.INFO)
journal_handler = JournalHandler()
logger.addHandler(journal_handler)

# Global MQTT client and lock
client = None
client_lock = asyncio.Lock()

def _mqtt_target():
    mqtt_broker = os.getenv("MQTT_BROKER")
    if not mqtt_broker:
        raise RuntimeError("MQTT_BROKER environment variable is not set.")
    return mqtt_broker, int(os.getenv("MQTT_PORT", 1883))


def _create_mqtt_client():
    mqtt_client = mqtt.Client()
    username = os.getenv("MQTT_USERNAME")
    password = os.getenv("MQTT_PASSWORD")
    if username and password:
        mqtt_client.username_pw_set(username, password)
    return mqtt_client


async def _disconnect_client():
    global client
    if client is None:
        return
    old_client = client
    client = None
    try:
        await asyncio.to_thread(old_client.loop_stop)
    except Exception as loop_stop_err:
        logger.warning(f"Error stopping MQTT loop: {loop_stop_err}")
    try:
        await asyncio.to_thread(old_client.disconnect)
    except Exception as disconnect_err:
        logger.warning(f"Error disconnecting MQTT client: {disconnect_err}")


async def _connect_client():
    global client
    mqtt_broker, port = _mqtt_target()
    mqtt_client = _create_mqtt_client()
    await asyncio.to_thread(mqtt_client.connect, mqtt_broker, port, 60)
    mqtt_client.loop_start()
    client = mqtt_client
    logger.info(f"Connected to MQTT broker at {mqtt_broker}:{port}")
    return client


async def _ensure_connected():
    global client
    if client is not None and client.is_connected():
        return client
    await _disconnect_client()
    return await _connect_client()


async def _publish_once(topic: str, payload: str):
    mqtt_client = await _ensure_connected()
    info = await asyncio.to_thread(mqtt_client.publish, topic, payload, qos=1)
    await asyncio.wait_for(
        asyncio.to_thread(info.wait_for_publish),
        timeout=MQTT_PUBLISH_TIMEOUT_SECONDS,
    )
    if info.rc != mqtt.MQTT_ERR_SUCCESS:
        raise RuntimeError(f"Publish returned error code: {info.rc}")


async def send_with_reconnect(topic: str, payload: str):
    """
    Attempts to publish the payload to the specified MQTT topic.
    Returns False if the retry window expires so the trigger can be kept for a later scan.
    """
    deadline = time.monotonic() + MQTT_PUBLISH_RETRY_SECONDS
    retry_delay = MQTT_INITIAL_RETRY_DELAY_SECONDS

    async with client_lock:
        while True:
            try:
                await _publish_once(topic, payload)
                logger.info(f"Sent payload: {payload} to topic: {topic}")
                return True
            except Exception as send_err:
                await _disconnect_client()
                if time.monotonic() >= deadline:
                    logger.error(
                        "MQTT publish still failing after %.1f seconds: %s. Keeping trigger for retry.",
                        MQTT_PUBLISH_RETRY_SECONDS,
                        send_err,
                    )
                    return False
                logger.warning(
                    "MQTT publish failed: %s. Reconnecting in %.1f seconds...",
                    send_err,
                    retry_delay,
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, MQTT_MAX_RETRY_DELAY_SECONDS)

async def scan_and_send(recording_dir: str):
    """
    Scans the specified directory for files matching the pattern and sends the JSON payload
    over MQTT using the send_with_reconnect function. If transmission takes too long, an exception
    is raised to force a restart.
    """
    # Use MQTT topic from environment variable; default to "home/raspberry"
    mqtt_topic = os.getenv("MQTT_TOPIC", "home/raspberry")
    # Event-driven hand-off: block on an inotify watch of the trigger directory
    # so a freshly written trigger file is sent within ~1 ms instead of waiting
    # for the next poll tick. qr.py is unchanged — it still just drops a file —
    # so the door can never be slowed or broken by anything on the MQTT side.
    try:
        watcher = DirectoryWatcher(recording_dir)
        logger.info("Watching %s for triggers via inotify (instant).", recording_dir)
    except OSError as watch_err:
        logger.warning(
            "inotify unavailable (%s); falling back to %dms polling.",
            watch_err,
            SCAN_INTERVAL_MS,
        )
        watcher = None
    try:
        while True:
            await scan_and_send_once(recording_dir, mqtt_topic=mqtt_topic)
            if watcher is None:
                await asyncio.sleep(SCAN_INTERVAL_MS / 1000)
            else:
                await watcher.wait(timeout=TRIGGER_FALLBACK_SCAN_SECONDS)
    finally:
        if watcher is not None:
            watcher.close()


def restore_incomplete_sends(recording_dir: str):
    for filename in os.listdir(recording_dir):
        if not filename.endswith(".txt.sending"):
            continue
        entrance_log_uuid = filename[:-12]
        try:
            UUID(entrance_log_uuid)
        except ValueError:
            continue
        sending_file_path = os.path.join(recording_dir, filename)
        file_path = os.path.join(recording_dir, f"{entrance_log_uuid}.txt")
        if os.path.exists(file_path):
            continue
        logger.warning(f"Restoring incomplete MQTT send for trigger file: {filename}")
        os.replace(sending_file_path, file_path)


async def scan_and_send_once(recording_dir: str, mqtt_topic: str | None = None):
    mqtt_topic = mqtt_topic or os.getenv("MQTT_TOPIC", "home/raspberry")
    restore_incomplete_sends(recording_dir)
    for filename in os.listdir(recording_dir):
        if filename.endswith('.txt') and filename != "record.txt":
            file_path = os.path.join(recording_dir, filename)
            entrance_log_uuid = filename[:-4]
            try:
                UUID(entrance_log_uuid)
            except ValueError:
                continue
            logger.info(f"Found trigger file: {filename}")
            try:
                file_contents = open(file_path, "r", encoding="utf-8").read().strip()
            except OSError as exc:
                logger.warning(f"Could not read file {filename}: {exc}")
                continue
            if file_contents:
                logger.info(f"Skipping non-empty trigger file: {filename}")
                continue
            sending_file_path = f"{file_path}.sending"
            file_mtime = int(os.path.getmtime(file_path))

            payload = [entrance_log_uuid, file_mtime]
            json_payload = json.dumps(payload)

            try:
                os.replace(file_path, sending_file_path)
                sent = await send_with_reconnect(mqtt_topic, json_payload)
                if sent is False:
                    logger.warning(f"Keeping trigger file {filename} for the next MQTT retry.")
                    if os.path.exists(sending_file_path):
                        os.replace(sending_file_path, file_path)
                    continue
            except Exception as exc:
                logger.exception(f"Unexpected MQTT sender error for {filename}: {exc}. Keeping trigger for retry.")
                if os.path.exists(sending_file_path):
                    os.replace(sending_file_path, file_path)
            else:
                if os.path.exists(sending_file_path):
                    os.remove(sending_file_path)

async def main():
    global client
    recording_dir = os.getenv("RECORDING_DIR")
    if not recording_dir:
        logger.error("Error: RECORDING_DIR environment variable is not set.")
        return

    try:
        _mqtt_target()
    except RuntimeError as config_err:
        logger.error(f"Error: {config_err}")
        return

    try:
        await _connect_client()
    except Exception as conn_err:
        logger.warning(f"Initial MQTT connection failed: {conn_err}. Will retry when a trigger is queued.")

    try:
        # Since MQTT manages keep-alives internally, we no longer need to send pings manually.
        await scan_and_send(recording_dir)
    except asyncio.CancelledError as e:
        logger.error(f"Task cancelled: {e}")
    finally:
        await _disconnect_client()
        logger.info("MQTT client disconnected.")

if __name__ == "__main__":
    asyncio.run(main())
