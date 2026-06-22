import json
import logging
import os
import socket
import threading
import time

import requests
from dotenv import load_dotenv

load_dotenv(override=True)

HOSTNAME = os.getenv("HOSTNAME")
USERNAME = os.getenv("USERNAME")
PASSWORD = os.getenv("PASSWORD")
DEVICE_API_TOKEN = os.getenv("DEVICE_API_TOKEN")
jwt_token = None  # Initializing the jwt_token variable

logging.basicConfig(level=logging.INFO)

# --- Hardening knobs (all overridable via .env, no code change needed) ---
# Per-request (connect, read) timeouts: NO single HTTP call may hang forever.
# A timeout-less request once wedged this process for days, which left the
# systemd unit "active (running)" so its 5-minute timer never fired again and
# customers.json went stale (expired members kept getting the door opened).
CONNECT_TIMEOUT = float(os.getenv("DOWNLOAD_CONNECT_TIMEOUT", "10"))
READ_TIMEOUT = float(os.getenv("DOWNLOAD_READ_TIMEOUT", "30"))
REQUEST_TIMEOUT = (CONNECT_TIMEOUT, READ_TIMEOUT)
# Bounded retry budget. The timer fires every 5 min (OnCalendar=*:00/5) -> the
# whole run MUST finish well under 300s so the next tick always starts fresh.
MAX_RETRIES = int(os.getenv("DOWNLOAD_MAX_RETRIES", "8"))
RETRY_SLEEP = float(os.getenv("DOWNLOAD_RETRY_SLEEP", "5"))
# Hard wall-clock deadline for the entire run. The watchdog below force-exits at
# this point; retries also stop gracefully once it is reached. 180s << 300s.
RUN_DEADLINE = float(os.getenv("DOWNLOAD_RUN_DEADLINE", "180"))

_deadline_at = None  # monotonic timestamp after which we stop retrying


def start_watchdog(deadline=RUN_DEADLINE):
    """Backstop guarantee that this process always terminates well before the next
    timer tick. Per-request timeouts cover ordinary stalls, but a wedged DNS
    resolution (getaddrinfo) is interruptible by neither requests' timeout nor a
    Python signal. getaddrinfo releases the GIL, so this daemon thread keeps
    running and can hard-exit the process regardless of what the main thread is
    blocked on."""

    def _kill():
        time.sleep(deadline)
        logging.error("Run exceeded %.0fs deadline; force-exiting so the 5-min timer can rerun.", deadline)
        os._exit(2)

    t = threading.Thread(target=_kill, name="run-deadline-watchdog", daemon=True)
    t.start()
    return t


def login():
    global jwt_token
    if DEVICE_API_TOKEN:
        return DEVICE_API_TOKEN
    if jwt_token:
        return jwt_token

    url = f"{HOSTNAME}/api/token/"
    payload = json.dumps({"email": USERNAME, "password": PASSWORD})
    headers = {"Content-Type": "application/json"}

    response = make_request("POST", url, headers, payload)  # Specify the method as "POST"
    if response is None or response.status_code != 200:
        log_unsuccessful_request(response)
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


def get_customers():
    global jwt_token

    authorization = get_auth_header()
    if authorization is None:
        logging.error("Could not get authentication token.")
        return None

    url = f"{HOSTNAME}/customers/"
    headers = {
        "Content-Type": "application/json",
        "Authorization": authorization,
    }

    response = make_request("GET", url, headers=headers)
    if response is None or response.status_code != 200:
        log_unsuccessful_request(response)
        return None

    customers = response.json()
    customer_uuid_dict = {customer["customer_uuid"]: customer for customer in customers}
    card_number_dict = {
        customer["card_number"]: customer for customer in customers if customer.get("card_number")
    }
    second_card_number_dict = {
        customer["second_card_number"]: customer for customer in customers if customer.get("second_card_number")
    }
    # merge the two dictionaries
    return {**customer_uuid_dict, **card_number_dict, **second_card_number_dict}


def make_request(method, url, headers=None, payload=None, retries=MAX_RETRIES, sleep_duration=RETRY_SLEEP):
    for i in range(retries):
        if _deadline_at is not None and time.monotonic() >= _deadline_at:
            logging.error("Run deadline reached; aborting further retries.")
            return None
        try:
            if method == "GET":
                response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            elif method == "POST":
                response = requests.post(url, headers=headers, data=payload, timeout=REQUEST_TIMEOUT)
            else:
                logging.error(f"Unsupported HTTP method: {method}.")
                return None

            return response
        except requests.exceptions.RequestException as e:
            logging.warning(f"Internet connection error: {e}. Retrying ({i + 1}/{retries})...")
            time.sleep(sleep_duration)  # back off before retrying

    logging.error("Exhausted all retries. Check your internet connection.")
    return None


def log_unsuccessful_request(response):
    if response is None:
        logging.info("Unsuccessful request: no response (connection failed or timed out).")
        return
    endpoint = response.url  # Get the URL from the response object
    log_message = "\n".join(response.text.split("\n")[-4:])
    logging.info(f"Unsuccessful request to endpoint {endpoint}. Response: {log_message}")


def write_customers_atomically(customers, output_path):
    """Write via a temp file + os.replace so customers.json is never left half-written
    (a partial file would break qr.py, and the watchdog may hard-exit mid-run)."""
    tmp_path = f"{output_path}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(customers, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, output_path)  # atomic on POSIX


if __name__ == "__main__":
    # Floor under any socket op that bypasses requests; getaddrinfo ignores it, hence the watchdog.
    socket.setdefaulttimeout(READ_TIMEOUT)
    _deadline_at = time.monotonic() + RUN_DEADLINE
    start_watchdog(RUN_DEADLINE)

    customers = get_customers()
    if customers is not None:
        # get the directory of the current script
        dir_path = os.path.dirname(os.path.realpath(__file__))
        # construct the full path for the output file
        output_path = os.path.join(dir_path, "customers.json")

        write_customers_atomically(customers, output_path)
        logging.info(f"Successfully written customers to {output_path}")
    else:
        logging.error("Failed to retrieve customers")
