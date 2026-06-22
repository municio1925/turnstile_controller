import json
import asyncio
import logging
import os
import pathlib
import re
import threading
import time
import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

import evdev
from evdev import InputDevice, categorize, KeyEvent
import requests
from dotenv import load_dotenv
import gpiod
import serial

from camera_trigger import queue_camera_trigger
from configurator import apply_config
from find_device import find_qr_devices
try:
    from i2cdetect import detect_i2c_device_not_27
except ImportError as e:
    logging.warning(f"i2cdetect module not available: {e}")
    detect_i2c_device_not_27 = None

from keymap import KEYMAP

try:
    from lcd_controller import LCDController
except ImportError as e:
    logging.warning(f"LCD controller module not available: {e}")
    LCDController = None
from systemd.journal import JournalHandler
import sentry_sdk

from serial_reader import find_serial_devices
from qr_reader_assignment import (
    MODE_SERIAL,
    ReaderAssignmentError,
    env_value_is_set,
    select_reader_for_direction,
)
from usb_diagnostics import record_component_state
from utils import SentryLogger

sentry_sdk.init(
    dsn=os.getenv("SENTRY_DSN"),
    environment=os.getenv("SENTRY_ENV"),
    traces_sample_rate=1.0,
)

load_dotenv()


# Odroid GPIO Compatibility Layer
class OdroidGPIO:
    """GPIO compatibility layer for Odroid using gpiod"""
    HIGH = 1
    LOW = 0
    BCM = "BCM"
    OUT = gpiod.LINE_REQ_DIR_OUT

    def __init__(self):
        self.chip = None
        self.lines = {}

    def setmode(self, mode):
        if not self.chip:
            self.chip = gpiod.Chip("gpiochip0")

    def setup(self, pin, direction):
        self.setmode(self.BCM)
        try:
            line = self.chip.get_line(pin)
            line.request(consumer=f"relay_{pin}", type=direction)
            self.lines[pin] = line
            # Initialize relay to OFF state
            relay_trigger = os.getenv("RELAY_TRIGGER", "HIGH")
            off_state = self.LOW if relay_trigger == "HIGH" else self.HIGH
            line.set_value(off_state)
        except Exception as e:
            logging.error(f"Failed to setup GPIO pin {pin}: {e}")

    def output(self, pin, value):
        if pin in self.lines:
            try:
                self.lines[pin].set_value(value)
            except Exception as e:
                logging.error(f"Failed to set GPIO {pin} to {value}: {e}")

    def cleanup(self):
        for pin, line in self.lines.items():
            try:
                # Turn off relay
                relay_trigger = os.getenv("RELAY_TRIGGER", "HIGH")
                off_state = self.LOW if relay_trigger == "HIGH" else self.HIGH
                line.set_value(off_state)
                line.release()
            except:
                pass
        self.lines.clear()

# Create GPIO instance
GPIO = OdroidGPIO()


class NoDeviceFoundError(Exception):
    pass


class QrDeviceDisconnectedError(Exception):
    pass


def set_env_default(key, value):
    if value is None:
        return
    if not env_value_is_set(os.getenv(key)):
        os.environ[key] = value


def configure_direction_environment(direction, force_reader_refresh=False):
    if direction == "A":
        entrance_uuid = str(os.getenv("ENTRANCE_UUID_A") or "").strip() or None
        if entrance_uuid:
            os.environ["ENTRANCE_UUID"] = entrance_uuid
        # Use Odroid I2C configuration
        set_env_default("LCD_I2C_ADDRESS", os.getenv("I2C_ADDRESS", "0x27"))
        set_env_default("LCD_I2C_BUS", os.getenv("I2C_BUS", "0"))
        # Use Odroid GPIO pins
        os.environ["RELAY_PIN_DOOR"] = os.getenv("RELAY_PIN_A", "62")  # Pin 7 -> GPIO line 62
        set_env_default("RELAY_PIN_DISPLAY", os.getenv("RELAY_PIN_DISPLAY_A", "69"))  # Pin 13 -> GPIO line 69
    elif direction == "B":
        entrance_uuid = str(os.getenv("ENTRANCE_UUID_B") or "").strip() or None
        if entrance_uuid:
            os.environ["ENTRANCE_UUID"] = entrance_uuid
        set_env_default("LCD_I2C_ADDRESS", "0x27")
        os.environ["RELAY_PIN_DOOR"] = os.getenv("RELAY_PIN_B", "65")  # Pin 16 -> GPIO line 65
        set_env_default("RELAY_PIN_DISPLAY", os.getenv("RELAY_PIN_DISPLAY_B", "20"))
    else:
        return

    if (
        not force_reader_refresh
        and env_value_is_set(os.getenv("QR_USB_DEVICE_PATH"))
        and env_value_is_set(os.getenv("IS_SERIAL_DEVICE"))
    ):
        return

    try:
        reader = select_reader_for_direction(
            direction,
            keyboard_devices=find_qr_devices(),
            serial_devices=find_serial_devices(),
            env=os.environ,
        )
    except ReaderAssignmentError as exc:
        raise NoDeviceFoundError(str(exc)) from exc

    os.environ["QR_USB_DEVICE_PATH"] = reader.path
    os.environ["IS_SERIAL_DEVICE"] = str(reader.mode == MODE_SERIAL)


DIRECTION = os.getenv("DIRECTION")
try:
    configure_direction_environment(DIRECTION)
except NoDeviceFoundError as exc:
    logging.warning("Initial QR reader assignment failed. The service will keep retrying: %s", exc)


ENTRANCE_DIRECTION = os.getenv("ENTRANCE_DIRECTION")
ENABLE_STREAM_HANDLER = os.getenv("ENABLE_STREAM_HANDLER", "False").lower() == "true"
DARK_MODE = os.getenv("DARK_MODE", "False").lower() == "true"
MAGIC_TIMESTAMP = 1725628212
SCHEDULE_TIMEZONE = ZoneInfo("Europe/Madrid")
current_dir = pathlib.Path(__file__).parent


class DirectionFilter(logging.Filter):
    def filter(self, record):
        record.msg = f"{DIRECTION} - {record.msg}"
        return True


logging.setLoggerClass(SentryLogger)
logger = logging.getLogger("qr_logger")
logger.setLevel(logging.INFO)
journal_handler = JournalHandler()
journal_handler.addFilter(DirectionFilter())
logger.addHandler(journal_handler)

if ENABLE_STREAM_HANDLER:
    # Stream handler (for stdout)
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)
    logger.addHandler(stream_handler)

# Example log message
logger.info(f"Starting QR script. My direction is {DIRECTION}")

jwt_token = None

ENTRANCE_UUID = os.getenv("ENTRANCE_UUID")
HOSTNAME = os.getenv("HOSTNAME")
USERNAME = os.getenv("USERNAME")
PASSWORD = os.getenv("PASSWORD")
DEVICE_API_TOKEN = os.getenv("DEVICE_API_TOKEN")
USE_LCD = int(os.getenv("USE_LCD", 1))
RELAY_PIN_DOOR = int(os.getenv("RELAY_PIN_DOOR", 10))
RELAY_PIN_DISPLAY = int(os.getenv("RELAY_PIN_DISPLAY")) if os.getenv("RELAY_PIN_DISPLAY") else None
RELAY_TOGGLE_DURATION = float(os.getenv("RELAY_TOGGLE_DURATION", 1))
RELAY_TRIGGER = os.getenv("RELAY_TRIGGER", "HIGH")
RELAY_ON = GPIO.HIGH if RELAY_TRIGGER == "HIGH" else GPIO.LOW
RELAY_OFF = GPIO.LOW if RELAY_TRIGGER == "HIGH" else GPIO.HIGH
OPEN_N_TIMES = int(os.getenv("OPEN_N_TIMES", 1))
IS_SERIAL_DEVICE = os.getenv("IS_SERIAL_DEVICE", "false").lower() == "true"
QR_RECONNECT_SLEEP_SECONDS = float(os.getenv("QR_RECONNECT_SLEEP_SECONDS", 1))
OUTPUT_ENDIAN = os.getenv("OUTPUT_ENDIAN", "big")
AS_HEX = os.getenv("AS_HEX", "false").lower() == "true"
AS_HEX_A = os.getenv("AS_HEX_A", "false").lower() == "true"
AS_HEX_B = os.getenv("AS_HEX_B", "false").lower() == "true"

# Determine the as_hex setting based on direction
if DIRECTION == "A" and AS_HEX_A:
    as_hex_setting = True
elif DIRECTION == "B" and AS_HEX_B:
    as_hex_setting = True
else:
    as_hex_setting = AS_HEX
HAS_CAMERA = os.getenv("HAS_CAMERA", "false").lower() == "true"
# HAS_CAMERA means this QR service emits a trigger for the camera flow.
# CAMERA_ENABLED is handled by the dedicated camera services, not by qr.py directly.
CAMERA_TRIGGER_MODE = os.getenv("CAMERA_TRIGGER_MODE", "mqtt").strip().lower() or "mqtt"


def configured_camera_trigger_directions():
    configured_value = os.getenv("CAMERA_TRIGGER_DIRECTIONS", "")
    directions = []
    for raw_direction in configured_value.split(","):
        direction = raw_direction.strip().upper()
        if direction in {"A", "B"} and direction not in directions:
            directions.append(direction)
    if directions:
        return directions

    legacy_direction = str(ENTRANCE_DIRECTION or "").strip().upper()
    return [legacy_direction] if legacy_direction in {"A", "B"} else []


CAMERA_TRIGGER_DIRECTIONS = configured_camera_trigger_directions()
USE_CAMERA_TRIGGER = HAS_CAMERA and DIRECTION in CAMERA_TRIGGER_DIRECTIONS
if USE_CAMERA_TRIGGER:
    RECORDING_DIR = os.getenv("RECORDING_DIR") or str(current_dir / "camera")
    CAMERA_SLEEP_DURATION = float(os.getenv("CAMERA_SLEEP_DURATION", 0.4))

if not ENTRANCE_UUID:
    logger.warning("No ENTRANCE_UUID configured for direction %s. Door verification will fail until a door is assigned.", DIRECTION)

if USE_LCD:
    try:
        LCD_I2C_ADDRESS = int(os.getenv("LCD_I2C_ADDRESS", 0x27), 16)
    except Exception as e:
        logger.warning(
            f"Error parsing LCD I2C address: {e}. "
            f"Continuing without. The Address is: {os.getenv('LCD_I2C_ADDRESS', 0x27)}")
        USE_LCD = False

QR_USB_DEVICE_PATH = os.getenv("QR_USB_DEVICE_PATH")
USB_COMPONENT = f"qr_{str(DIRECTION or '').strip().lower()}" if DIRECTION in {"A", "B"} else None

_serial_exception = getattr(serial, "SerialException", None)
if isinstance(_serial_exception, type) and issubclass(_serial_exception, BaseException):
    SERIAL_CONNECTION_ERRORS = (FileNotFoundError, OSError, _serial_exception, NoDeviceFoundError)
else:
    SERIAL_CONNECTION_ERRORS = (FileNotFoundError, OSError, NoDeviceFoundError)

logger.info("using relay pin %s for the door. My direction is %s", RELAY_PIN_DOOR, DIRECTION)


def _get_current_local_time():
    return datetime.now(SCHEDULE_TIMEZONE).timetuple()


# Initialize Relay
relay_pin = RELAY_PIN_DOOR
RELAY_PIN_QR_READER = 22  # Hopefully we never again have to use a relay to restart the qr reader
GPIO.setmode(GPIO.BCM)  # Use Broadcom pin numbering
GPIO.setup(relay_pin, GPIO.OUT)  # Set pin as an output pin

if USE_LCD and LCDController:
    # Initialize LCD
    try:
        i2c_bus = int(os.getenv("LCD_I2C_BUS", "0"))
        lcd = LCDController(
            use_lcd=USE_LCD,
            lcd_address=LCD_I2C_ADDRESS,
            dark_mode=DARK_MODE,
            relay_pin=RELAY_PIN_DISPLAY,
            relay_trigger=RELAY_TRIGGER,
            i2c_bus=i2c_bus,
        )
        lcd.clear()
        logger.info("LCD initialized successfully for direction %s.", DIRECTION)
    except Exception as e:
        logger.exception(
            f"Error initializing LCD direction {DIRECTION} on "
            f"address {LCD_I2C_ADDRESS}. Continuing without LCD: {e}"
        )
        USE_LCD = False
elif USE_LCD and not LCDController:
    logger.warning("LCD requested but LCDController not available. Continuing without LCD.")
    USE_LCD = False


DEFAULT_LCD_MESSAGE_TIMEOUT = 3


def display_on_lcd(line1, line2, timeout=DEFAULT_LCD_MESSAGE_TIMEOUT):
    if not USE_LCD:
        logger.info(line1)
        logger.info(line2)
    else:
        lcd.display_text_on_lcd(line1, line2, timeout)


def refresh_runtime_reader_assignment(force_reader_refresh=False):
    global ENTRANCE_UUID, IS_SERIAL_DEVICE, QR_USB_DEVICE_PATH

    configure_direction_environment(DIRECTION, force_reader_refresh=force_reader_refresh)
    ENTRANCE_UUID = os.getenv("ENTRANCE_UUID")
    IS_SERIAL_DEVICE = os.getenv("IS_SERIAL_DEVICE", "false").lower() == "true"
    QR_USB_DEVICE_PATH = os.getenv("QR_USB_DEVICE_PATH")

    if not QR_USB_DEVICE_PATH:
        raise NoDeviceFoundError("No QR reader path configured.")


def init_qr_device():
    global dev
    while True:
        try:
            refresh_runtime_reader_assignment(force_reader_refresh=True)
            dev = (
                serial.Serial(QR_USB_DEVICE_PATH, baudrate=9600, timeout=0.1)
                if IS_SERIAL_DEVICE
                else InputDevice(QR_USB_DEVICE_PATH)
            )
            logger.info("Successfully connected to the QR code scanner.")
            if USB_COMPONENT:
                record_component_state(USB_COMPONENT, True)
            display_on_lcd("Escanea", "codigo QR...")

            if IS_SERIAL_DEVICE:
                # we were just testing the serial connection
                dev.close()
            return dev
        except SERIAL_CONNECTION_ERRORS as exc:
            if USB_COMPONENT:
                record_component_state(USB_COMPONENT, False)
            logger.warning(
                "Failed to connect to the QR code scanner (%s). Retrying in %s seconds...",
                exc,
                QR_RECONNECT_SLEEP_SECONDS,
            )
            display_on_lcd("Fallo lector USB", "Reintentando")
            time.sleep(QR_RECONNECT_SLEEP_SECONDS)


# List to hold decoded QR data
shared_list = []


def log_unsuccessful_request(response):
    endpoint = response.url  # Get the URL from the response object
    log_message = "\n".join(response.text.split("\n")[-4:])
    logger.info(f"Unsuccessful request to endpoint {endpoint}. Response: {log_message}")


def toggle_relay(duration=RELAY_TOGGLE_DURATION, open_n_times=OPEN_N_TIMES):
    logger.info(f"Toggling relay PIN {relay_pin}")
    open_duration = duration / open_n_times
    for _ in range(open_n_times):
        GPIO.output(relay_pin, RELAY_ON)
        time.sleep(open_duration)
    for i in range(10):
        GPIO.output(relay_pin, RELAY_OFF)


def unpack_barcode(barcode_data):
    try:
        login_data = json.loads(barcode_data)
        return login_data["customer_uuid"], login_data["timestamp"]
    except Exception as e:
        display_on_lcd("codigo", "QR invalido", timeout=2)  # Displays "Invalid QR Code" in Spanish
        logger.error(f"Error unpacking barcode: {e}")
        return None, None


def handle_server_response(status_code, first_name=None):
    if status_code == "UserExists":
        return open_door_and_greet(first_name)

    elif status_code == "MembershipInactive":
        display_on_lcd("Membresía", "inactiva", timeout=2)

    elif status_code == "UserDoesNotExist":
        display_on_lcd("Usuario", "no existe", timeout=2)

    else:
        display_on_lcd("Error", "Intenta de nuevo", timeout=2)

    return False


async def queue_camera_trigger_after_success(entrance_log_uuid):
    if not USE_CAMERA_TRIGGER:
        return

    queue_camera_trigger(RECORDING_DIR, entrance_log_uuid, mode=CAMERA_TRIGGER_MODE)
    logger.info(
        "Queued camera trigger %s via %s mode for direction %s.",
        entrance_log_uuid,
        CAMERA_TRIGGER_MODE,
        DIRECTION,
    )
    if CAMERA_SLEEP_DURATION > 0:
        logger.info(f"sleeping for {CAMERA_SLEEP_DURATION} seconds.")
        await asyncio.sleep(CAMERA_SLEEP_DURATION)


def open_door_and_greet(first_name):
    if ENTRANCE_DIRECTION == DIRECTION:
        greet_word = "Hola"
    else:
        greet_word = "Adios"
    logger.info(f"{greet_word}, {first_name}!")
    logger.info(f"Opening door...with pin {RELAY_PIN_DOOR}")

    def display_greeting():
        display_on_lcd(f"{greet_word}", first_name, timeout=3)

    # Start a new thread to toggle the relay
    relay_thread = threading.Thread(target=toggle_relay, daemon=True)
    relay_thread.start()

    # Start a new thread for the display greeting to avoid blocking
    display_thread = threading.Thread(target=display_greeting, daemon=True)
    display_thread.start()

    return True


def load_customers_cache():
    script_path = pathlib.Path(__file__).parent
    cache_file_path = script_path / "customers.json"
    if cache_file_path.exists():
        with cache_file_path.open() as cache_file:
            return json.load(cache_file)
    return {}


def post_request(url, headers, payload, retries=10, sleep_duration=10):
    for i in range(retries):
        try:
            # (connect, read) timeout: the door now leans on this live check for
            # expired-in-cache members, so a wedged server must never hang the turnstile.
            response = requests.post(url, headers=headers, json=payload, timeout=(5, 15))
            return response
        except requests.exceptions.RequestException as e:
            logger.warning(f"Eerror: {e}. Retrying...")
            display_on_lcd("No internet", "Reintentando...")
            time.sleep(sleep_duration)  # sleep for 10 seconds before retrying
    logger.error("Exhausted all retries. Check your internet connection.")
    display_on_lcd("Sin internet", "Verifica conexión", timeout=20)
    return None


def send_entrance_log(url, headers, payload, retries=3, sleep_duration=5):
    for i in range(retries):
        try:
            response = requests.put(url, headers=headers, json=payload)
            if response.status_code == 200:
                logger.info(f"Entrance log sent successfully: {payload}")
            elif response.status_code == 403:
                logger.error(f"Permission denied when sending entrance log: {response.text}. headers sent: {headers}")
                refresh_token()
            else:
                logger.error(f"Failed to send entrance log: {response.text}. headers sent: {headers}")
            return response
        except requests.exceptions.RequestException as e:
            logger.warning(f"Internet connection error when sending entrance-log: {e}. Retrying...")
            time.sleep(sleep_duration)  # sleep for 10 seconds before retrying
    return None


def generate_uuid_from_string(input_string):
    # Use a predefined namespace (e.g., UUID namespace for DNS)
    namespace = uuid.NAMESPACE_DNS

    # Generate a UUID using UUID5, which is based on the SHA-1 hash of a namespace and a name (your string)
    generated_uuid = uuid.uuid5(namespace, input_string)

    return str(generated_uuid)


def login():
    global jwt_token
    if DEVICE_API_TOKEN:
        return DEVICE_API_TOKEN
    if jwt_token:
        return jwt_token

    url = f"{HOSTNAME}/api/token/"
    payload = {"email": USERNAME, "password": PASSWORD}
    headers = {"Content-Type": "application/json"}

    response = post_request(url, headers, payload)

    if response is None or response.status_code != 200:
        log_unsuccessful_request(response)
        display_on_lcd("Login", "Failed", timeout=2)
        return None

    jwt_token = response.json().get("access", None)
    return jwt_token


def get_auth_header():
    if DEVICE_API_TOKEN:
        return f"Token {DEVICE_API_TOKEN}"

    token = login()
    if not token:
        return None
    return f"Bearer {token}"


async def verify_customer(customer_uuid, timestamp):
    global jwt_token

    payload = {
        "customer_uuid": customer_uuid,
        "entrance_uuid": ENTRANCE_UUID,
        "direction": DIRECTION,
        "timestamp": timestamp,
    }

    entrance_log_uuid = generate_uuid_from_string(str(payload))
    payload["uuid"] = entrance_log_uuid

    url = f"{HOSTNAME}/verify_customer/"

    authorization = get_auth_header()
    if not authorization:
        display_on_lcd("Login", "Failed", timeout=2)
        return

    headers = {"Authorization": authorization, "Content-Type": "application/json"}

    if not is_valid_timestamp(timestamp):
        display_on_lcd("Error", "QR vencido", timeout=2)
        payload["response_code"] = "TimestampExpired"
        send_entrance_log(url, headers, payload)
        return

    if timestamp == MAGIC_TIMESTAMP:  # update the magic timestamp after check to create a proper entrance-log
        payload["timestamp"] = int(time.time())

    response = await get_valid_response(url, headers, payload, customer_uuid, entrance_log_uuid)

    if response is None:
        return

    if not DEVICE_API_TOKEN and response.status_code in (401, 403):  # Token expired or invalid
        headers["Authorization"] = refresh_token()
        response = await get_valid_response(url, headers, payload, customer_uuid, entrance_log_uuid)
        if response is None:
            return

    json_response = response.json()
    status_code = json_response.get("status_code")
    first_name = json_response.get("first_name")

    if status_code == "UserExists":
        await queue_camera_trigger_after_success(entrance_log_uuid)

    return handle_server_response(status_code, first_name)


def is_valid_timestamp(timestamp: int):
    """Timestamp can't be older than 10 seconds"""
    timestamp = int(timestamp)
    if timestamp == MAGIC_TIMESTAMP:  # magic timestamp for card users which are an exception
        return True
    current_time = int(time.time())
    if current_time - timestamp > 60:
        return False
    return True


async def get_valid_response(url, headers, payload, customer_uuid, entrance_log_uuid):
    status_code, customer = _find_customer_in_cache(customer_uuid)
    if status_code == "UserExists":
        await queue_camera_trigger_after_success(entrance_log_uuid)
        open_door_and_greet(customer["first_name"])
        payload["response_code"] = status_code
        send_entrance_log(url, headers, payload, retries=15)
        return None
    elif status_code == "OutsideSchedule":
        payload["response_code"] = status_code
        send_entrance_log(url, headers, payload, retries=15)
        display_on_lcd("Fuera del", "horario", timeout=2)
        return None
    else:
        response = post_request(url, headers, payload, retries=5)
        logger.info(f"Response: {response.json()}")

    if response is None or response.status_code not in (200, 401, 403):
        logger.error(f"Invalid response: {response} {response.headers}")
        handle_server_response(None)
        if response:
            log_unsuccessful_request(response)
        return None
    return response


def _membership_currently_valid(customer):
    """Whether the customer's membership covers 'now' in Europe/Madrid.

    Prefers the absolute expiry date ``membership_valid_until`` (sent by the
    server's v2 customers endpoint) so a stale cache fails *closed*: once that
    date has passed, this returns False and the caller defers to the live server
    check instead of trusting a frozen ``active_membership`` flag (the flag is
    computed server-side at download time and silently rots if the cache stops
    refreshing). Falls back to ``active_membership`` for old (v1) caches or
    customers with no membership date (e.g. staff/comped). The odroid runs UTC,
    so we compare against the Madrid date, matching the schedule check."""
    valid_until = customer.get("membership_valid_until")
    if not valid_until:
        return bool(customer.get("active_membership"))
    try:
        valid_until_date = date.fromisoformat(valid_until)
    except (TypeError, ValueError):
        logger.warning(f"Invalid membership_valid_until {valid_until!r}; falling back to active_membership.")
        return bool(customer.get("active_membership"))
    today_madrid = datetime.now(SCHEDULE_TIMEZONE).date()
    return today_madrid <= valid_until_date


def _find_customer_in_cache(customer_uuid):
    customers = load_customers_cache()
    customer = customers.get(customer_uuid, None)
    if customer:
        if _membership_currently_valid(customer) or customer["is_staff"]:
            logger.info(f"Found customer {customer_uuid} in cache.")
            if customer.get("entrance_schedules"):
                if not is_in_schedule(customer):
                    return "OutsideSchedule", None
            return "UserExists", customer
        else:
            return "MembershipInactive", None
    return "UserDoesNotExist", None


def is_in_schedule(customer):
    current_time = _get_current_local_time()
    current_day = current_time.tm_wday
    current_hour = current_time.tm_hour
    current_minute = current_time.tm_min

    for schedule in customer["entrance_schedules"]:
        if current_day in schedule["days_of_week"]:
            start_time, end_time = schedule["start_time"][:5], schedule["end_time"][:5]
            start_hour, start_minute = map(int, start_time.split(":"))
            end_hour, end_minute = map(int, end_time.split(":"))

            if start_hour < current_hour < end_hour:
                return True
            elif start_hour == current_hour and start_minute <= current_minute:
                return True
            elif end_hour == current_hour and current_minute <= end_minute:
                return True

    return False


def refresh_token():
    global jwt_token
    if DEVICE_API_TOKEN:
        return f"Token {DEVICE_API_TOKEN}"
    jwt_token = login()
    return f"Bearer {jwt_token}"


def handle_keyboard_interrupt(vs):
    logger.warning("Keyboard interrupt received. Stopping video stream and exiting...")
    lcd.clear()
    GPIO.cleanup()  # This will reset all GPIO ports you have used in this program back to input mode.
    exit()


def _mapped_keycode(keycode):
    if isinstance(keycode, list):
        for candidate in keycode:
            if candidate in KEYMAP:
                return candidate
        return keycode[0] if keycode else ""
    return keycode


async def keyboard_event_loop(device):
    global shared_list
    output_string = ""

    try:
        async for event in device.async_read_loop():
            if event.type == evdev.ecodes.EV_KEY:
                categorized_event = categorize(event)
                if categorized_event.keystate == KeyEvent.key_up:
                    keycode = _mapped_keycode(categorized_event.keycode)
                    character = KEYMAP.get(keycode, "")

                    if character:
                        output_string += character

                    if keycode == "KEY_ENTER":
                        logger.info(f"Received raw data: {output_string}")

                        try:
                            data = _process_ascii_data(output_string, as_hex_setting)
                        except Exception as e:
                            logger.error(f"Error interpreting ascii data: {e}.. data: {output_string}")
                            output_string = ""
                            continue
                        logger.info(f"Interpreted data: {data}")
                        if "config" in data:
                            display_on_lcd("aplicando", "configuracion", timeout=2)
                            response = apply_config(data)
                            logger.info(f"Config response: {response}")
                            if USE_LCD:
                                display_on_lcd("ajuste", "aplicado", timeout=2)
                            output_string = ""
                            continue

                        try:
                            qr_dict = _load_json_data(data)
                            customer = qr_dict.get("customer-uuid", qr_dict.get("customer_uuid"))
                            await verify_customer(customer, qr_dict["timestamp"])
                        except (json.JSONDecodeError, TypeError, AttributeError, KeyError):
                            await verify_customer(data, int(time.time()))
                        finally:
                            output_string = ""
    except (OSError, FileNotFoundError) as e:
        if USB_COMPONENT:
            record_component_state(USB_COMPONENT, False)
        display_on_lcd("Sin coneccion con", "lector QR")
        logger.error("QR keyboard reader disconnected: %s", e)
        raise QrDeviceDisconnectedError(str(e)) from e


async def serial_device_event_loop():
    global shared_list
    try:
        with serial.Serial(QR_USB_DEVICE_PATH, baudrate=9600, timeout=0.2) as ser:
            if USB_COMPONENT:
                record_component_state(USB_COMPONENT, True)
            while True:
                # Read data from the serial port
                if ser.in_waiting > 0:
                    try:
                        data = _interpret_serial_data(ser, as_hex_setting)
                    except Exception as e:
                        logger.error(f"Error interpreting serial data: {e}.. data: {ser.readline()}")
                        continue
                    logger.info(f"Interpreted data: {data}")
                    if "config" in data:
                        display_on_lcd("aplicando", "configuracion", timeout=2)
                        response = apply_config(data)
                        logger.info(f"Config response: {response}")
                        if USE_LCD:
                            display_on_lcd("ajuste", "aplicado", timeout=2)
                        continue
                    try:
                        qr_dict = _load_json_data(data)
                        customer = qr_dict.get("customer-uuid", qr_dict.get("customer_uuid"))
                        await verify_customer(customer, qr_dict["timestamp"])
                    except (json.JSONDecodeError, TypeError, AttributeError, KeyError):
                        await verify_customer(data, int(time.time()))
                        cleanup_serial_queue(ser)
                await asyncio.sleep(0.2)
    except SERIAL_CONNECTION_ERRORS as e:
        if USB_COMPONENT:
            record_component_state(USB_COMPONENT, False)
        display_on_lcd("Sin coneccion con", "lector QR")
        logger.error("Serial reader disconnected: %s", e)
        raise QrDeviceDisconnectedError(str(e)) from e


def cleanup_serial_queue(ser):
    try:
        ser.reset_input_buffer()
    except AttributeError:
        logger.warning("Serial device does not support reset_input_buffer. Flushing input instead.")
        try:
            ser.flushInput()
        except Exception as e:
            logger.warning(f"Failed to flush input buffer: {e}. Continuing without flushing.")

def _load_json_data(raw_data):
    try:
        return json.loads(raw_data)
    except json.JSONDecodeError:
        # regex pattern to match a valid JSON string
        pattern = r"\{.*?\}"
        match = re.search(pattern, raw_data)
        if match:
            return json.loads(match.group(0))


def _interpret_serial_data(ser, as_hex: bool):
    ascii_data = ser.readline().decode("utf-8").strip()

    if not ascii_data:
        return None

    logger.info(f"Received: {ascii_data}")
    return _process_ascii_data(ascii_data, as_hex)


def _process_ascii_data(ascii_data: str, as_hex: bool):
    is_json = "{" in ascii_data and "}" in ascii_data
    if is_json:
        return ascii_data

    if as_hex:
        hex = _decimal_to_hex(ascii_data)
    else:
        raw_bytes = bytes.fromhex(ascii_data)
        hex = ":".join(f"{b:02x}" for b in raw_bytes)
    return _hash_uuid(hex)


def _decimal_to_hex(decimal_str):
    # Convert the decimal string to an integer
    try:
        decimal_value = int(decimal_str)
    except ValueError:
        cleaned_decimal_str = "".join([char for char in decimal_str if char.isdigit()])
        decimal_value = int(cleaned_decimal_str)
    # Convert the integer to a hexadecimal string
    hex_value = f"{decimal_value:08x}"
    # Reverse the byte order (convert to big-endian)
    reversed_hex = "".join(hex_value[i : i + 2] for i in range(len(hex_value) - 2, -1, -2))
    # Format as hex bytes with colons
    hex_uid = ":".join(reversed_hex[i : i + 2] for i in range(0, len(reversed_hex), 2))
    return hex_uid


def _hash_uuid(input_string) -> str:
    # Use uuid5 with a standard namespace (NAMESPACE_DNS) for consistent hashing
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, input_string))


async def main_loop():
    global shared_list

    if not DEVICE_API_TOKEN:
        login()

    while True:
        if shared_list:
            qr_data = shared_list.pop(0)
            logger.info(f"Received QR data: {qr_data}")
            customer = qr_data.get("customer-uuid", qr_data.get("customer_uuid"))
            await verify_customer(customer, qr_data["timestamp"])
        await asyncio.sleep(0.1)  # 1-second delay to avoid busy-waiting


async def run_qr_service_forever():
    while True:
        dev = init_qr_device()
        try:
            if IS_SERIAL_DEVICE:
                await serial_device_event_loop()
            else:
                main_task = asyncio.create_task(main_loop())
                try:
                    await keyboard_event_loop(dev)
                finally:
                    main_task.cancel()
                    await asyncio.gather(main_task, return_exceptions=True)
        except QrDeviceDisconnectedError:
            logger.warning(
                "QR reader disconnected for direction %s. Waiting %s seconds before rediscovery.",
                DIRECTION,
                QR_RECONNECT_SLEEP_SECONDS,
            )
            await asyncio.sleep(QR_RECONNECT_SLEEP_SECONDS)


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    if DEVICE_API_TOKEN:
        logger.info("Using device API token authentication for controller requests.")
    else:
        refresh_token()
    try:
        loop.run_until_complete(run_qr_service_forever())
    except KeyboardInterrupt:
        logger.warning("Received exit signal.")
