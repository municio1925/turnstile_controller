import base64
import configparser
import json
import logging
import os
import socket
import subprocess
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from systemd.journal import JournalHandler
from usb_diagnostics import get_pending_events, get_usb_status_snapshot, initialize_database, mark_events_sent, prune_events


load_dotenv()

LOGGER = logging.getLogger("device_configurator_service")
LOGGER.setLevel(logging.INFO)
LOGGER.addHandler(JournalHandler())

CURRENT_DIR = Path(__file__).resolve().parent
ENV_PATH = CURRENT_DIR / ".env"
FRPC_CONFIG_PATH = Path("/etc/frpc.ini")
SYSTEMD_UNIT_DIR = Path("/etc/systemd/system")
MOSQUITTO_LAN_LISTENER_SOURCE_PATH = CURRENT_DIR / "mosquitto-lan-listener.conf"
MOSQUITTO_LAN_LISTENER_TARGET_PATH = Path("/etc/mosquitto/conf.d/lan-listener.conf")
WIFI_SCAN_SETTLE_SECONDS = float(os.getenv("DEVICE_WIFI_SCAN_SETTLE_SECONDS", "2"))
INVALID_ENV_VALUES = {"", "none", "null", "undefined"}
DEFAULT_LOCAL_MQTT_BROKER = "127.0.0.1"
DEFAULT_SSH_USERNAME = os.getenv("FRP_SSH_USER", "manager")
ACTIVE_CONTROL_API_BASE_URL = str(os.getenv("CONTROL_API_BASE_URL", "")).rstrip("/")
ACTIVE_DEVICE_BOOTSTRAP_TOKEN = str(os.getenv("DEVICE_BOOTSTRAP_TOKEN", "")).strip()
ACTIVE_POLL_INTERVAL_SECONDS = float(os.getenv("DEVICE_POLL_INTERVAL_SECONDS", "10"))
ACTIVE_VIDEO_POLL_INTERVAL_SECONDS = float(os.getenv("VIDEO_POLL_INTERVAL_SECONDS", "2"))
PREVIEW_FAST_POLL_UNTIL = 0.0

DEVICE_SETTINGS_SCHEMA = [
    ("AS_HEX", "bool", False),
    ("CAMERA_ENABLED", "bool", False),
    ("CAMERA_TRIGGER_DIRECTIONS", "str", ""),
    ("CAMERA_TRIGGER_MODE", "str", ""),
    ("CONTROL_API_BASE_URL", "str", ""),
    ("DARK_MODE", "bool", False),
    ("DEVICE_BOOTSTRAP_TOKEN", "str", ""),
    ("DEVICE_HARDWARE_MODEL", "str", "odroid"),
    ("DEVICE_POLL_INTERVAL_SECONDS", "float", 10.0),
    ("VIDEO_POLL_INTERVAL_SECONDS", "float", 2.0),
    ("DEVICE_SOFTWARE_VERSION", "str", "dev"),
    ("DEVICE_TYPE", "str", "odroid"),
    ("ENTRANCE_UUID_A", "str", ""),
    ("ENTRANCE_UUID_B", "str", ""),
    ("FLIP_VIDEO", "bool", False),
    ("FPS", "int", 20),
    ("FRAME_HEIGHT", "int", 360),
    ("FRAME_WIDTH", "int", 480),
    ("FRP_SSH_USER", "str", "manager"),
    ("GYM_UUID", "str", ""),
    ("HAS_CAMERA", "bool", False),
    ("HOSTNAME", "str", ""),
    ("I2C_ADDRESS", "str", "0x27"),
    ("MQTT_BROKER", "str", ""),
    ("PASSWORD", "str", ""),
    ("QR_ACTIVE_DIRECTIONS", "str", ""),
    ("QR_READER_MODE_A", "str", ""),
    ("QR_READER_MODE_B", "str", ""),
    ("RECORDING_DIR", "str", ""),
    ("RELAY_PIN_A", "int", 62),
    ("RELAY_PIN_B", "int", 65),
    ("RELAY_PIN_DISPLAY", "int", 69),
    ("RELAY_PIN_DISPLAY_A", "int", 69),
    ("RELAY_TOGGLE_DURATION", "str", "1"),
    ("RELAY_TRIGGER", "str", "LOW"),
    ("RETRIES_ON_OS_ERROR", "int", 500),
    ("S3_ACCESS_KEY", "str", ""),
    ("S3_ENDPOINT_URL", "str", ""),
    ("S3_SECRET_ACCESS_KEY", "str", ""),
    ("SENTRY_DSN", "str", ""),
    ("USERNAME", "str", ""),
    ("USE_2_QR_READERS", "bool", False),
    ("USE_LCD", "int", 1),
]

QR_RUNTIME_SERVICES = ("qr_script_a", "qr_script_b")

BASE_RUNTIME_SERVICES_TO_RESTART = (*QR_RUNTIME_SERVICES, "mqtt-sender")

CAMERA_MANDATORY_SERVICES = (
    "videorecorder",
    "mqtt-receiver",
)

CAMERA_TRIGGER_SERVICES = ("mqtt-sender",)
CAMERA_OPTIONAL_SERVICES = ("upload",)

SERVICE_UNIT_FILES = {
    "qr_script_a": "qr_script_a.service",
    "qr_script_b": "qr_script_b.service",
    "mqtt-sender": "mqtt-sender.service",
    "mqtt-receiver": "mqtt-receiver.service",
    "videorecorder": "videorecorder.service",
    "upload": "upload.service",
}


def get_control_api_base_url():
    return ACTIVE_CONTROL_API_BASE_URL


def get_device_bootstrap_token():
    return ACTIVE_DEVICE_BOOTSTRAP_TOKEN


def get_video_poll_interval_seconds():
    return ACTIVE_VIDEO_POLL_INTERVAL_SECONDS


def get_poll_interval_seconds():
    if PREVIEW_FAST_POLL_UNTIL > time.time():
        return max(0.5, get_video_poll_interval_seconds())
    return ACTIVE_POLL_INTERVAL_SECONDS


def get_device_name():
    return os.getenv("DEVICE_NAME", "")


def get_device_type():
    return os.getenv("DEVICE_TYPE", "odroid")


def get_hardware_model():
    return os.getenv("DEVICE_HARDWARE_MODEL", "odroid")


def get_software_version():
    return os.getenv("DEVICE_SOFTWARE_VERSION", "dev")


def _read_interface_mac(interface_name):
    address_path = Path("/sys/class/net") / interface_name / "address"
    try:
        mac_address = address_path.read_text().strip().lower()
    except OSError:
        return ""

    if mac_address in {"", "00:00:00:00:00:00"}:
        return ""

    return mac_address


def get_hardware_mac_address():
    preferred_interfaces = (
        "eth0",
        "enp0s3",
        "enp1s0",
        "end0",
        "wlan0",
        "wlp1s0",
    )
    seen_interfaces = set()

    for interface_name in preferred_interfaces:
        seen_interfaces.add(interface_name)
        mac_address = _read_interface_mac(interface_name)
        if mac_address:
            return mac_address

    net_class_dir = Path("/sys/class/net")
    try:
        interface_names = sorted(path.name for path in net_class_dir.iterdir())
    except OSError:
        return ""

    for interface_name in interface_names:
        if interface_name in seen_interfaces or interface_name == "lo":
            continue

        mac_address = _read_interface_mac(interface_name)
        if mac_address:
            return mac_address

    return ""


def get_device_identifier():
    configured_identifier = str(os.getenv("DEVICE_IDENTIFIER", "")).strip()
    if configured_identifier and configured_identifier.lower() not in INVALID_ENV_VALUES:
        return configured_identifier

    mac_address = get_hardware_mac_address()
    if mac_address:
        return mac_address

    return socket.gethostname()


def auth_headers():
    return {
        "Authorization": f"DeviceBootstrap {get_device_bootstrap_token()}",
        "Content-Type": "application/json",
    }


def run_command(command, input_text=None):
    command_env = os.environ.copy()
    command_env.update(
        {
            "LANG": "C",
            "LC_ALL": "C",
            "NO_COLOR": "1",
            "PAGER": "cat",
            "TERM": "dumb",
        }
    )
    return subprocess.run(command, capture_output=True, text=True, check=False, env=command_env, input=input_text)


def run_nmcli(args, require_sudo=False, fallback_to_unprivileged=True):
    if require_sudo:
        result = run_command(["sudo", "-n", "nmcli", *args])
        if result.returncode == 0 or not fallback_to_unprivileged:
            return result

        LOGGER.warning("Privileged nmcli failed for '%s': %s", " ".join(args), result.stderr.strip())

    return run_command(["nmcli", *args])


def parse_wifi_scan():
    rescan_result = run_nmcli(["device", "wifi", "rescan"], require_sudo=True, fallback_to_unprivileged=False)
    if rescan_result.returncode == 0:
        time.sleep(WIFI_SCAN_SETTLE_SECONDS)
    else:
        LOGGER.warning("Wi-Fi rescan failed: %s", rescan_result.stderr.strip())

    result = run_nmcli(
        [
            "-t",
            "-f",
            "SSID,SIGNAL,SECURITY,IN-USE",
            "device",
            "wifi",
            "list",
        ],
        require_sudo=True,
        fallback_to_unprivileged=False,
    )
    if result.returncode != 0:
        LOGGER.warning("Wi-Fi scan failed: %s", result.stderr.strip())
        return []

    networks = []
    for raw_line in result.stdout.splitlines():
        parts = raw_line.split(":", 3)
        if len(parts) < 4:
            continue

        ssid = parts[0]
        signal = parts[1].strip()
        security = parts[2].strip()
        in_use = parts[3].strip() == "*"
        if not ssid.strip():
            continue

        networks.append(
            {
                "ssid": ssid,
                "signal": signal,
                "security": security,
                "in_use": in_use,
            }
        )

    return networks


def get_ip_address():
    result = run_command(["hostname", "-I"])
    if result.returncode != 0:
        return None

    addresses = [address.strip() for address in result.stdout.split() if address.strip()]
    return addresses[0] if addresses else None


def current_wifi_ssid():
    # Read the SSID from the active Wi-Fi connection instead of the scan list.
    # `nmcli dev wifi` reads the scan cache and triggers an implicit rescan once the
    # cache is stale (>30s), which pulls the radio off-channel. Association state is
    # known without any scan, so the heartbeat can report the current network cheaply.
    connection_name = current_wifi_connection_name()
    if not connection_name:
        return ""

    result = run_nmcli(
        ["-g", "802-11-wireless.ssid", "connection", "show", connection_name],
        require_sudo=True,
        fallback_to_unprivileged=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()

    # Fall back to the connection name (equals the SSID under NetworkManager's
    # default naming) if the profile's SSID field could not be read.
    return connection_name


def current_wifi_connection_name():
    result = run_nmcli(
        ["-t", "-f", "NAME,TYPE", "connection", "show", "--active"],
        require_sudo=True,
        fallback_to_unprivileged=False,
    )
    if result.returncode != 0:
        return ""

    for line in result.stdout.splitlines():
        if line.endswith(":802-11-wireless"):
            return line.rsplit(":", 1)[0].strip()
    return ""


def parse_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def camera_services_enabled():
    mandatory_service_names = required_camera_services(True)
    service_states_by_name = {service.get("service"): service for service in current_camera_services()}
    mandatory_states = [service_states_by_name.get(service_name, {}) for service_name in mandatory_service_names]
    return bool(mandatory_states) and all(service.get("active") == "active" for service in mandatory_states)


def camera_trigger_mode():
    return str(os.getenv("CAMERA_TRIGGER_MODE", "") or "").strip().lower()


def local_filesystem_camera_trigger_enabled():
    return parse_bool(os.getenv("HAS_CAMERA"), False) and camera_trigger_mode() == "filesystem"


def mqtt_camera_trigger_enabled():
    return parse_bool(os.getenv("HAS_CAMERA"), False) and not local_filesystem_camera_trigger_enabled()


def camera_mqtt_receiver_enabled(camera_enabled):
    if not camera_enabled:
        return False
    if local_filesystem_camera_trigger_enabled():
        return False
    return str(os.getenv("DEVICE_TYPE", "")).strip().lower() == "camera"


def required_camera_services(camera_enabled):
    if not camera_enabled:
        return []
    services = ["videorecorder"]
    if camera_mqtt_receiver_enabled(camera_enabled):
        services.append("mqtt-receiver")
    return services


def reload_runtime_control_settings():
    global ACTIVE_CONTROL_API_BASE_URL, ACTIVE_DEVICE_BOOTSTRAP_TOKEN, ACTIVE_POLL_INTERVAL_SECONDS, ACTIVE_VIDEO_POLL_INTERVAL_SECONDS

    ACTIVE_CONTROL_API_BASE_URL = str(os.getenv("CONTROL_API_BASE_URL", "")).rstrip("/")
    ACTIVE_DEVICE_BOOTSTRAP_TOKEN = str(os.getenv("DEVICE_BOOTSTRAP_TOKEN", "")).strip()
    ACTIVE_POLL_INTERVAL_SECONDS = parse_float(os.getenv("DEVICE_POLL_INTERVAL_SECONDS", "10"), 10.0) or 10.0
    ACTIVE_VIDEO_POLL_INTERVAL_SECONDS = parse_float(os.getenv("VIDEO_POLL_INTERVAL_SECONDS", "2"), 2.0) or 2.0


def current_device_settings():
    settings_payload = {}
    for key, value_type, default in DEVICE_SETTINGS_SCHEMA:
        if key == "CAMERA_ENABLED":
            continue
        raw_value = os.getenv(key)
        if value_type == "bool":
            settings_payload[key] = parse_bool(raw_value, default)
        elif value_type == "int":
            settings_payload[key] = parse_int(raw_value, default)
        elif value_type == "float":
            settings_payload[key] = parse_float(raw_value, default)
        else:
            settings_payload[key] = raw_value if raw_value is not None else default
    settings_payload["CAMERA_ENABLED"] = camera_services_enabled()
    return settings_payload


def build_ssh_command(ssh_username, ssh_hostname, ssh_port):
    ssh_username = str(ssh_username or "").strip()
    ssh_hostname = str(ssh_hostname or "").strip()
    if not ssh_username or not ssh_hostname:
        return ""

    if ssh_port:
        return f"ssh -p {ssh_port} {ssh_username}@{ssh_hostname}"

    return f"ssh {ssh_username}@{ssh_hostname}"


def current_ssh_tunnel():
    if not FRPC_CONFIG_PATH.exists():
        return {}

    parser = configparser.ConfigParser()
    try:
        parser.read(FRPC_CONFIG_PATH)
    except (configparser.Error, OSError) as exc:
        LOGGER.warning("Could not parse %s: %s", FRPC_CONFIG_PATH, exc)
        return {}

    if not parser.has_section("common"):
        return {}

    server_addr = str(parser.get("common", "server_addr", fallback="")).strip()
    if not server_addr:
        return {}

    server_port = parse_int(parser.get("common", "server_port", fallback="7000"), 7000)
    proxy_sections = [section for section in parser.sections() if section != "common"]
    preferred_section = None

    for section in proxy_sections:
        if parser.get(section, "type", fallback="").strip() != "tcp":
            continue
        if parse_int(parser.get(section, "local_port", fallback="22"), 22) == 22:
            preferred_section = section
            break

    if preferred_section is None and proxy_sections:
        preferred_section = proxy_sections[0]

    if preferred_section is None:
        return {}

    ssh_username = str(os.getenv("FRP_SSH_USER", DEFAULT_SSH_USERNAME) or DEFAULT_SSH_USERNAME).strip() or "manager"
    frp_local_ip = str(parser.get(preferred_section, "local_ip", fallback="127.0.0.1")).strip() or "127.0.0.1"
    frp_local_port = parse_int(parser.get(preferred_section, "local_port", fallback="22"), 22)
    ssh_port = parse_int(parser.get(preferred_section, "remote_port", fallback=""), None)
    if not ssh_port:
        return {}

    return {
        "ssh_username": ssh_username,
        "ssh_hostname": server_addr,
        "ssh_port": ssh_port,
        "frp_server_port": server_port,
        "frp_local_ip": frp_local_ip,
        "frp_local_port": frp_local_port,
        "ssh_command": build_ssh_command(ssh_username, server_addr, ssh_port),
    }


def ethernet_connected():
    result = run_nmcli(
        ["-t", "-f", "DEVICE,TYPE,STATE", "device", "status"], require_sudo=True, fallback_to_unprivileged=False
    )
    if result.returncode != 0:
        return False

    return any(":ethernet:connected" in line for line in result.stdout.splitlines())


def configured_entrances():
    entrances = []
    for slot in ("A", "B"):
        entrance_uuid = os.getenv(f"ENTRANCE_UUID_{slot}", "").strip()
        if entrance_uuid.lower() in INVALID_ENV_VALUES:
            continue

        entrances.append(
            {
                "slot": slot,
                "uuid": entrance_uuid,
                "direction": os.getenv(f"ENTRANCE_DIRECTION_{slot}", ""),
            }
        )

    if not entrances:
        single_entrance_uuid = os.getenv("ENTRANCE_UUID", "").strip()
        if single_entrance_uuid.lower() not in INVALID_ENV_VALUES:
            entrances.append(
                {
                    "slot": os.getenv("ENTRANCE_DIRECTION", "") or "A",
                    "uuid": single_entrance_uuid,
                    "direction": os.getenv("ENTRANCE_DIRECTION", ""),
                }
            )

    return entrances


def read_env_values():
    env_values = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                env_values[key] = value
    return env_values


def write_env_values(env_values):
    rendered = "\n".join(f"{key}={value}" for key, value in sorted(env_values.items())) + "\n"
    ENV_PATH.write_text(rendered)


def persist_env_values(payload):
    env_values = read_env_values()

    for key, value in payload.items():
        if key.startswith("_"):
            continue
        if key == "CAMERA_ENABLED":
            continue

        if value is None:
            env_values.pop(key, None)
            os.environ.pop(key, None)
            continue

        if key == "USE_LCD":
            normalized_value = "1" if parse_bool(value, False) else "0"
            env_values[key] = normalized_value
            os.environ[key] = normalized_value
            continue

        rendered_value = json.dumps(value) if isinstance(value, (dict, list)) else f'"{value}"'
        env_values[key] = rendered_value
        os.environ[key] = str(value)

    write_env_values(env_values)


def device_payload():
    return {
        "device_identifier": get_device_identifier(),
        "name": get_device_name(),
        "hostname": socket.gethostname(),
        "device_type": get_device_type(),
        "hardware_model": get_hardware_model(),
        "hardware_mac_address": get_hardware_mac_address(),
        "software_version": get_software_version(),
        "ip_address": get_ip_address(),
        "wifi_ssid": current_wifi_ssid(),
        "ethernet_connected": ethernet_connected(),
        # Intentionally omit "wifi_networks" here: emitting it every heartbeat forced a
        # `nmcli device wifi rescan` (~every 10s), knocking the radio off-channel and
        # degrading connectivity. Omitting the key entirely (rather than sending []/None)
        # lets the backend preserve the last cached scan. The neighbour list is refreshed
        # on demand via the `wifi_scan` command (fired when the frontend Wi-Fi dialog opens
        # and by "Actualizar redes").
        "configured_entrances": configured_entrances(),
        "ssh_tunnel": current_ssh_tunnel(),
        "device_settings": current_device_settings(),
        "camera_services": current_camera_services(),
        "usb_status": get_usb_status_snapshot(),
        "usb_events": get_pending_events(),
    }


def post_json(path, payload):
    control_api_base_url = get_control_api_base_url()
    if not control_api_base_url or not get_device_bootstrap_token():
        raise RuntimeError("CONTROL_API_BASE_URL and DEVICE_BOOTSTRAP_TOKEN must be configured.")

    response = requests.post(
        f"{control_api_base_url}{path}",
        headers=auth_headers(),
        data=json.dumps(payload),
        timeout=20,
    )
    response.raise_for_status()
    return response


def get_json(path, params):
    control_api_base_url = get_control_api_base_url()
    response = requests.get(
        f"{control_api_base_url}{path}",
        headers=auth_headers(),
        params=params,
        timeout=20,
    )
    if response.status_code == 204:
        return None
    response.raise_for_status()
    return response.json()


def validate_wifi_password(password):
    if not password:
        return ""

    if len(password) < 8:
        return "La contrasena Wi-Fi debe tener al menos 8 caracteres."

    if len(password) > 64:
        return "La contrasena Wi-Fi no puede superar los 64 caracteres."

    if len(password) == 64:
        try:
            int(password, 16)
        except ValueError:
            return "Una contrasena Wi-Fi de 64 caracteres debe contener solo digitos hexadecimales."

    return ""


def restore_previous_wifi_connection(connection_name):
    if not connection_name:
        return None

    result = run_nmcli(["connection", "up", connection_name], require_sudo=True, fallback_to_unprivileged=False)
    if result.returncode != 0:
        LOGGER.warning("Failed to restore Wi-Fi connection %s: %s", connection_name, result.stderr.strip())
        return result

    time.sleep(WIFI_SCAN_SETTLE_SECONDS)
    return result


def restart_base_runtime_services():
    restart_results = []
    for service_name in BASE_RUNTIME_SERVICES_TO_RESTART:
        install_result = ensure_service_unit_installed(service_name)
        if install_result is not None:
            restart_results.append(install_result)
            if not install_result["ok"]:
                continue
        result = run_command(["sudo", "-n", "systemctl", "restart", service_name])
        restart_results.append(
            {
                "service": service_name,
                "managed": "restart",
                "ok": result.returncode == 0,
                "stderr": result.stderr.strip(),
            }
        )
        if result.returncode != 0:
            LOGGER.warning("Could not restart %s: %s", service_name, result.stderr.strip())
    return restart_results


def service_state(service_name):
    active_result = run_command(["systemctl", "is-active", service_name])
    enabled_result = run_command(["systemctl", "is-enabled", service_name])
    return {
        "service": service_name,
        "active": active_result.stdout.strip() or active_result.stderr.strip() or "unknown",
        "enabled": enabled_result.stdout.strip() or enabled_result.stderr.strip() or "unknown",
    }


def has_upload_configuration():
    required_keys = ("GYM_UUID", "S3_ACCESS_KEY", "S3_SECRET_ACCESS_KEY", "S3_ENDPOINT_URL")
    return all(str(os.getenv(key) or "").strip() for key in required_keys)


def ensure_service_unit_installed(service_name):
    unit_filename = SERVICE_UNIT_FILES.get(service_name)
    if not unit_filename:
        return None

    source_path = CURRENT_DIR / unit_filename
    if not source_path.exists():
        return {
            "service": service_name,
            "managed": "install",
            "ok": False,
            "stderr": f"No existe {source_path}.",
        }

    target_path = SYSTEMD_UNIT_DIR / unit_filename
    try:
        if target_path.exists() and target_path.read_bytes() == source_path.read_bytes():
            return None
    except OSError as exc:
        LOGGER.warning("Could not compare %s and %s: %s", source_path, target_path, exc)

    copy_result = run_command(["sudo", "-n", "cp", str(source_path), str(target_path)])
    if copy_result.returncode != 0:
        return {
            "service": service_name,
            "managed": "install",
            "ok": False,
            "stderr": copy_result.stderr.strip() or f"No se pudo copiar {unit_filename}.",
        }

    daemon_reload_result = run_command(["sudo", "-n", "systemctl", "daemon-reload"])
    return {
        "service": service_name,
        "managed": "install",
        "ok": daemon_reload_result.returncode == 0,
        "stderr": daemon_reload_result.stderr.strip(),
    }


def set_service_enabled(service_name, enabled):
    install_result = ensure_service_unit_installed(service_name)
    if install_result is not None and not install_result["ok"]:
        return install_result

    action = "enable" if enabled else "disable"
    result = run_command(["sudo", "-n", "systemctl", action, "--now", service_name])
    state = service_state(service_name)
    return {
        "service": service_name,
        "managed": action,
        "ok": result.returncode == 0,
        "stderr": result.stderr.strip(),
        **state,
    }


def restart_managed_service(service_name):
    install_result = ensure_service_unit_installed(service_name)
    if install_result is not None and not install_result["ok"]:
        return install_result

    result = run_command(["sudo", "-n", "systemctl", "restart", service_name])
    state = service_state(service_name)
    return {
        "service": service_name,
        "managed": "restart",
        "ok": result.returncode == 0,
        "stderr": result.stderr.strip(),
        **state,
    }


def ensure_camera_mosquitto_listener(enabled):
    listener_changed = False

    if enabled:
        if not MOSQUITTO_LAN_LISTENER_SOURCE_PATH.exists():
            return {
                "service": "mosquitto-lan-listener",
                "managed": "configure",
                "ok": False,
                "stderr": f"No existe {MOSQUITTO_LAN_LISTENER_SOURCE_PATH}.",
            }

        try:
            target_matches = (
                MOSQUITTO_LAN_LISTENER_TARGET_PATH.exists()
                and MOSQUITTO_LAN_LISTENER_TARGET_PATH.read_bytes() == MOSQUITTO_LAN_LISTENER_SOURCE_PATH.read_bytes()
            )
        except OSError:
            target_matches = False

        if not target_matches:
            copy_result = run_command(
                [
                    "sudo",
                    "-n",
                    "cp",
                    str(MOSQUITTO_LAN_LISTENER_SOURCE_PATH),
                    str(MOSQUITTO_LAN_LISTENER_TARGET_PATH),
                ]
            )
            if copy_result.returncode != 0:
                return {
                    "service": "mosquitto-lan-listener",
                    "managed": "configure",
                    "ok": False,
                    "stderr": copy_result.stderr.strip() or "No se pudo configurar el listener LAN de Mosquitto.",
                }
            listener_changed = True
    else:
        if MOSQUITTO_LAN_LISTENER_TARGET_PATH.exists():
            remove_result = run_command(["sudo", "-n", "rm", "-f", str(MOSQUITTO_LAN_LISTENER_TARGET_PATH)])
            if remove_result.returncode != 0:
                return {
                    "service": "mosquitto-lan-listener",
                    "managed": "remove",
                    "ok": False,
                    "stderr": remove_result.stderr.strip() or "No se pudo eliminar el listener LAN de Mosquitto.",
                }
            listener_changed = True

    if listener_changed:
        restart_result = run_command(["sudo", "-n", "systemctl", "restart", "mosquitto"])
        if restart_result.returncode != 0:
            return {
                "service": "mosquitto-lan-listener",
                "managed": "restart",
                "ok": False,
                "stderr": restart_result.stderr.strip() or "No se pudo reiniciar Mosquitto.",
            }

    return {
        "service": "mosquitto-lan-listener",
        "managed": "configure" if enabled else "remove",
        "ok": True,
        "stderr": "",
    }


def reconcile_camera_services(enabled_override=None):
    service_results = []
    enabled = camera_services_enabled() if enabled_override is None else bool(enabled_override)
    required_services = tuple(required_camera_services(enabled))
    mqtt_receiver_required = "mqtt-receiver" in required_services
    mosquitto_result = ensure_camera_mosquitto_listener(mqtt_receiver_required)
    service_results.append(mosquitto_result)
    if not mosquitto_result.get("ok"):
        return {
            "status": "failed",
            "error_message": "No se pudo preparar el broker MQTT de la camara.",
            "service_results": service_results,
        }

    mqtt_sender_enabled = mqtt_camera_trigger_enabled()
    for service_name in CAMERA_TRIGGER_SERVICES:
        enable_result = set_service_enabled(service_name, mqtt_sender_enabled)
        service_results.append(enable_result)
        if mqtt_sender_enabled and enable_result.get("ok"):
            service_results.append(restart_managed_service(service_name))

    if not enabled:
        for service_name in CAMERA_MANDATORY_SERVICES + CAMERA_OPTIONAL_SERVICES:
            service_results.append(set_service_enabled(service_name, False))
        return {"status": "succeeded", "error_message": "", "service_results": service_results}

    for service_name in CAMERA_MANDATORY_SERVICES:
        service_enabled = service_name in required_services
        enable_result = set_service_enabled(service_name, service_enabled)
        service_results.append(enable_result)
        if service_enabled and enable_result.get("ok"):
            service_results.append(restart_managed_service(service_name))

    upload_enabled = has_upload_configuration()
    for service_name in CAMERA_OPTIONAL_SERVICES:
        enable_result = set_service_enabled(service_name, upload_enabled)
        service_results.append(enable_result)
        if upload_enabled and enable_result.get("ok"):
            service_results.append(restart_managed_service(service_name))

    failed_services = [result["service"] for result in service_results if not result["ok"] and result["service"] in required_services]
    if failed_services:
        return {
            "status": "failed",
            "error_message": f"No se pudieron activar los servicios de camara: {', '.join(failed_services)}.",
            "service_results": service_results,
        }

    return {"status": "succeeded", "error_message": "", "service_results": service_results}


def current_camera_services():
    return [service_state(service_name) for service_name in CAMERA_MANDATORY_SERVICES + CAMERA_TRIGGER_SERVICES + CAMERA_OPTIONAL_SERVICES]


def current_qr_services():
    return [service_state(service_name) for service_name in QR_RUNTIME_SERVICES]


def configured_qr_directions():
    if str(os.getenv("DEVICE_TYPE", "")).strip().lower() == "camera":
        return []

    active_directions = str(os.getenv("QR_ACTIVE_DIRECTIONS", "") or "").strip()
    if active_directions:
        directions = []
        for raw_direction in active_directions.split(","):
            direction = raw_direction.strip().upper()
            if direction in {"A", "B"} and direction not in directions:
                directions.append(direction)
        return directions

    directions = [entry["slot"] for entry in configured_entrances() if entry.get("slot") in {"A", "B"}]
    if directions:
        return directions

    return ["A", "B"] if parse_bool(os.getenv("USE_2_QR_READERS"), False) else []


def reconcile_qr_services():
    service_results = []
    active_directions = set(configured_qr_directions())
    service_by_direction = {
        "A": "qr_script_a",
        "B": "qr_script_b",
    }

    for direction, service_name in service_by_direction.items():
        enabled = direction in active_directions
        enable_result = set_service_enabled(service_name, enabled)
        service_results.append(enable_result)
        if enabled and enable_result.get("ok"):
            service_results.append(restart_managed_service(service_name))

    failed_services = [result["service"] for result in service_results if not result.get("ok")]
    if failed_services:
        return {
            "status": "failed",
            "error_message": f"No se pudieron reconciliar los servicios QR: {', '.join(failed_services)}.",
            "service_results": service_results,
        }

    return {"status": "succeeded", "error_message": "", "service_results": service_results}


def resolve_requested_wifi_ssid(requested_ssid, available_networks):
    requested_ssid = str(requested_ssid or "")
    if not requested_ssid:
        return requested_ssid, False

    exact_matches = [network.get("ssid", "") for network in available_networks if network.get("ssid", "") == requested_ssid]
    if exact_matches:
        return exact_matches[0], False

    trimmed_requested_ssid = requested_ssid.strip()
    if not trimmed_requested_ssid:
        return requested_ssid, False

    trimmed_matches = []
    for network in available_networks:
        candidate = network.get("ssid", "")
        if str(candidate).strip() != trimmed_requested_ssid or not candidate:
            continue
        if candidate not in trimmed_matches:
            trimmed_matches.append(candidate)
    if len(trimmed_matches) == 1:
        return trimmed_matches[0], trimmed_matches[0] != requested_ssid

    return requested_ssid, False


def connect_wifi(ssid, password):
    previous_connection_name = current_wifi_connection_name()
    previous_ssid = current_wifi_ssid()
    password_error = validate_wifi_password(password)
    available_networks = parse_wifi_scan()
    resolved_ssid, ssid_was_resolved = resolve_requested_wifi_ssid(ssid, available_networks)
    if password_error:
        return {
            "status": "failed",
            "result": {
                "wifi_ssid": previous_ssid,
                "wifi_networks": available_networks,
                "requested_ssid": ssid,
                "resolved_ssid": resolved_ssid,
                "ssid_was_resolved": ssid_was_resolved,
            },
            "error_message": password_error,
        }

    command = ["dev", "wifi", "connect", resolved_ssid]
    if password:
        command.extend(["password", password])

    result = run_nmcli(command, require_sudo=True, fallback_to_unprivileged=False)
    if result.returncode != 0:
        restore_result = restore_previous_wifi_connection(previous_connection_name)
        restored_ssid = current_wifi_ssid()
        return {
            "status": "failed",
            "result": {
                "stdout": result.stdout.strip(),
                "wifi_ssid": restored_ssid or previous_ssid,
                "wifi_networks": parse_wifi_scan(),
                "restored_previous_connection": bool(restore_result and restore_result.returncode == 0),
                "requested_ssid": ssid,
                "resolved_ssid": resolved_ssid,
                "ssid_was_resolved": ssid_was_resolved,
            },
            "error_message": result.stderr.strip() or "nmcli connection failed",
        }

    return {
        "status": "succeeded",
        "result": {
            "stdout": result.stdout.strip(),
            "wifi_ssid": current_wifi_ssid(),
            "wifi_networks": parse_wifi_scan(),
            "requested_ssid": ssid,
            "resolved_ssid": resolved_ssid,
            "ssid_was_resolved": ssid_was_resolved,
        },
    }


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


def open_camera_capture(cv2, frame_width, frame_height):
    attempted_candidates = []
    for candidate in iter_camera_candidates():
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


def capture_camera_snapshot():
    try:
        import sys

        if "/usr/lib/python3/dist-packages" not in sys.path:
            sys.path.append("/usr/lib/python3/dist-packages")

        import cv2
    except Exception as exc:
        return {"status": "failed", "error_message": f"No se pudo cargar la camara: {exc}"}

    frame_width = parse_int(os.getenv("FRAME_WIDTH", "480"), 480) or 480
    frame_height = parse_int(os.getenv("FRAME_HEIGHT", "360"), 360) or 360
    jpeg_quality = 70
    video, selected_candidate, attempted_candidates = open_camera_capture(cv2, frame_width, frame_height)

    try:
        if video is None:
            attempted_text = ", ".join(str(candidate) for candidate in attempted_candidates) or "sin candidatos"
            return {
                "status": "failed",
                "error_message": f"No se pudo abrir la camara. Candidatos probados: {attempted_text}",
            }

        time.sleep(0.2)
        ret, frame = video.read()
        if not ret:
            return {"status": "failed", "error_message": "No se pudo capturar una imagen de la camara."}

        if parse_bool(os.getenv("FLIP_VIDEO"), False):
            frame = cv2.flip(frame, 0)
            frame = cv2.flip(frame, 1)

        success, encoded_image = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality])
        if not success:
            return {"status": "failed", "error_message": "No se pudo codificar la imagen de la camara."}

        return {
            "status": "succeeded",
            "result": {
                "content_type": "image/jpeg",
                "image_base64": base64.b64encode(encoded_image.tobytes()).decode('ascii'),
                "width": int(frame.shape[1]),
                "height": int(frame.shape[0]),
                "captured_at": int(time.time()),
                "camera_device": str(selected_candidate),
            },
        }
    finally:
        if video is not None:
            video.release()


def activate_preview_fast_poll_window():
    global PREVIEW_FAST_POLL_UNTIL

    preview_interval = max(0.5, get_video_poll_interval_seconds())
    PREVIEW_FAST_POLL_UNTIL = time.time() + max(preview_interval * 3, 6)


def update_env(payload):
    camera_enabled_override = payload.get("CAMERA_ENABLED") if "CAMERA_ENABLED" in payload else None
    persist_env_values(payload)
    camera_service_reconciliation = reconcile_camera_services(enabled_override=camera_enabled_override)
    qr_service_reconciliation = reconcile_qr_services()
    result_payload = {
        "updated_keys": sorted(payload.keys()),
        "configured_entrances": configured_entrances(),
        "wifi_ssid": current_wifi_ssid(),
        "wifi_networks": parse_wifi_scan(),
        "ssh_tunnel": current_ssh_tunnel(),
        "device_settings": current_device_settings(),
        "service_restarts": [],
        "camera_services": current_camera_services(),
        "qr_services": current_qr_services(),
        "camera_service_reconciliation": camera_service_reconciliation["service_results"],
        "qr_service_reconciliation": qr_service_reconciliation["service_results"],
    }
    if camera_service_reconciliation["status"] != "succeeded":
        return {
            "status": "failed",
            "result": result_payload,
            "error_message": camera_service_reconciliation["error_message"],
        }

    if qr_service_reconciliation["status"] != "succeeded":
        return {
            "status": "failed",
            "result": result_payload,
            "error_message": qr_service_reconciliation["error_message"],
        }

    return {
        "status": "succeeded",
        "result": result_payload,
        "_post_result_action": {
            "type": "reboot",
            "delay_seconds": 3,
        },
    }


def configure_remote_ssh(payload):
    ssh_username = str(payload.get("ssh_username") or "").strip()
    ssh_hostname = str(payload.get("ssh_hostname") or "").strip()
    ssh_port = parse_int(payload.get("ssh_port"), None)
    frp_server_port = parse_int(payload.get("frp_server_port"), 7000)
    frp_local_ip = str(payload.get("frp_local_ip") or "127.0.0.1").strip() or "127.0.0.1"
    frp_local_port = parse_int(payload.get("frp_local_port"), 22)

    if not ssh_username or not ssh_hostname or not ssh_port:
        return {"status": "failed", "error_message": "La configuracion SSH remota esta incompleta."}

    rendered_config = "\n".join(
        [
            "[common]",
            f"server_addr = {ssh_hostname}",
            f"server_port = {frp_server_port}",
            "",
            f"[ssh_{ssh_port}]",
            "type = tcp",
            f"local_ip = {frp_local_ip}",
            f"local_port = {frp_local_port}",
            f"remote_port = {ssh_port}",
            "",
        ]
    )
    write_result = run_command(["sudo", "-n", "tee", str(FRPC_CONFIG_PATH)], input_text=rendered_config)
    if write_result.returncode != 0:
        return {
            "status": "failed",
            "result": {"stdout": write_result.stdout.strip(), "ssh_tunnel": current_ssh_tunnel()},
            "error_message": write_result.stderr.strip() or "No se pudo escribir /etc/frpc.ini.",
        }

    persist_env_values({"FRP_SSH_USER": ssh_username})
    restart_result = run_command(["sudo", "-n", "systemctl", "restart", "frpc"])
    if restart_result.returncode != 0:
        return {
            "status": "failed",
            "result": {"stdout": restart_result.stdout.strip(), "ssh_tunnel": current_ssh_tunnel()},
            "error_message": restart_result.stderr.strip() or "No se pudo reiniciar frpc.",
        }

    ssh_tunnel = current_ssh_tunnel()
    return {
        "status": "succeeded",
        "result": {
            "ssh_tunnel": ssh_tunnel,
            "ssh_command": ssh_tunnel.get("ssh_command", ""),
            "wifi_ssid": current_wifi_ssid(),
            "wifi_networks": parse_wifi_scan(),
        },
    }


def clear_remote_ssh():
    remove_result = run_command(["sudo", "-n", "rm", "-f", str(FRPC_CONFIG_PATH)])
    if remove_result.returncode != 0:
        return {
            "status": "failed",
            "result": {"stdout": remove_result.stdout.strip(), "ssh_tunnel": current_ssh_tunnel()},
            "error_message": remove_result.stderr.strip() or "No se pudo eliminar /etc/frpc.ini.",
        }

    persist_env_values({"FRP_SSH_USER": None})
    stop_result = run_command(["sudo", "-n", "systemctl", "stop", "frpc"])
    if stop_result.returncode != 0:
        return {
            "status": "failed",
            "result": {"stdout": stop_result.stdout.strip(), "ssh_tunnel": current_ssh_tunnel()},
            "error_message": stop_result.stderr.strip() or "No se pudo detener frpc.",
        }

    return {
        "status": "succeeded",
        "result": {
            "ssh_tunnel": {},
            "wifi_ssid": current_wifi_ssid(),
            "wifi_networks": parse_wifi_scan(),
        },
    }


def schedule_reboot(delay_seconds):
    command = f"sleep {max(delay_seconds, 1)} && sudo -n systemctl reboot"
    subprocess.Popen(
        ["sh", "-c", command],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def reboot_device():
    return {
        "status": "succeeded",
        "result": {
            "message": "Reinicio programado.",
        },
        "_post_result_action": {
            "type": "reboot",
            "delay_seconds": 3,
        },
    }


def execute_command(command):
    command_type = command.get("command_type")
    payload = command.get("payload") or {}

    if command_type == "wifi_connect":
        return connect_wifi(payload.get("ssid", ""), payload.get("password", ""))
    if command_type == "env_update":
        return update_env(payload)
    if command_type == "wifi_scan":
        return {"status": "succeeded", "result": {"wifi_networks": parse_wifi_scan()}}
    if command_type == "camera_snapshot":
        return capture_camera_snapshot()
    if command_type == "frp_config":
        if payload.get("_clear_remote_ssh"):
            return clear_remote_ssh()
        return configure_remote_ssh(payload)
    if command_type == "reboot":
        return reboot_device()

    return {"status": "failed", "error_message": f"Unsupported command type: {command_type}"}


def ensure_registered():
    payload = device_payload()
    response = post_json("/device/bootstrap/register/", payload)
    mark_events_sent([event["event_uuid"] for event in payload.get("usb_events") or []])
    prune_events()
    LOGGER.info("Device registered: %s", response.status_code)


def heartbeat():
    payload = device_payload()
    response = post_json("/device/bootstrap/heartbeat/", payload)
    mark_events_sent([event["event_uuid"] for event in payload.get("usb_events") or []])
    prune_events()
    try:
        return response.json()
    except ValueError:
        return {}


def reconcile_mqtt_broker(heartbeat_response):
    """Keep MQTT_BROKER pointed at the linked camera's current LAN IP.

    The backend resolves the paired camera's latest heartbeat IP and returns it
    as ``mqtt_broker`` in the heartbeat response. When the camera's DHCP lease
    changes, that value changes with it; we rewrite ``.env`` (an unprivileged
    file write) and mqtt-sender re-reads it on its next reconnect. Deliberately
    lightweight: no reboot (unlike ``env_update``) and no privileged service
    restart — this service runs unprivileged (only ``nmcli`` is passwordless),
    and mqtt-sender self-heals from the ``.env`` change on its own.
    """
    if not isinstance(heartbeat_response, dict):
        return
    if not mqtt_camera_trigger_enabled():
        return
    new_broker = str(heartbeat_response.get("mqtt_broker") or "").strip()
    # An empty value or the local fallback means the backend does not yet know
    # the camera's IP (camera offline / just booted). Keep the last known-good
    # broker rather than pointing the sender at localhost.
    if not new_broker or new_broker == DEFAULT_LOCAL_MQTT_BROKER:
        return
    current_broker = str(os.getenv("MQTT_BROKER") or "").strip()
    if new_broker == current_broker:
        return
    persist_env_values({"MQTT_BROKER": new_broker})
    LOGGER.info(
        "Camera broker moved %s -> %s; mqtt-sender will reconnect to it",
        current_broker or "<unset>",
        new_broker,
    )


def fetch_next_command():
    return get_json("/device/bootstrap/commands/next/", {"device_identifier": get_device_identifier()})


def send_command_result(command_uuid, result_payload):
    post_json(f"/device/bootstrap/commands/{command_uuid}/result/", result_payload)


def main():
    LOGGER.info("Starting device configurator service for %s", get_device_identifier())
    initialize_database()
    ensure_registered()

    while True:
        try:
            heartbeat_response = heartbeat()
            reconcile_mqtt_broker(heartbeat_response)
            command = fetch_next_command()
            if command:
                LOGGER.info("Executing command %s", command["uuid"])
                result_payload = execute_command(command)
                post_result_action = result_payload.pop("_post_result_action", None)
                send_command_result(command["uuid"], result_payload)
                if command.get("command_type") == "camera_snapshot":
                    activate_preview_fast_poll_window()
                if post_result_action and post_result_action.get("type") == "reboot":
                    schedule_reboot(int(post_result_action.get("delay_seconds", 3)))
                reload_runtime_control_settings()
        except Exception as exc:
            LOGGER.exception("Configurator loop failed: %s", exc)

        reload_runtime_control_settings()
        time.sleep(get_poll_interval_seconds())


if __name__ == "__main__":
    main()
