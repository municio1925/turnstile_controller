import importlib.util
import logging
import os
import sys
import types
from pathlib import Path


class FakeJournalHandler(logging.Handler):
    def emit(self, record):
        return None


fake_journal_module = types.ModuleType("systemd.journal")
fake_journal_module.JournalHandler = FakeJournalHandler
sys.modules.setdefault("systemd", types.ModuleType("systemd"))
sys.modules["systemd.journal"] = fake_journal_module


MODULE_PATH = Path(__file__).resolve().parents[1] / "device_configurator_service.py"
SPEC = importlib.util.spec_from_file_location("device_configurator_service_module", MODULE_PATH)
device_configurator_service = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(device_configurator_service)


def test_reconcile_camera_services_enables_expected_services(monkeypatch):
    enable_calls = []
    restart_calls = []
    mosquitto_calls = []

    monkeypatch.setenv("DEVICE_TYPE", "camera")
    monkeypatch.delenv("HAS_CAMERA", raising=False)
    monkeypatch.delenv("CAMERA_TRIGGER_MODE", raising=False)
    monkeypatch.setattr(device_configurator_service, "camera_services_enabled", lambda: True)
    monkeypatch.setattr(device_configurator_service, "has_upload_configuration", lambda: True)
    monkeypatch.setattr(
        device_configurator_service,
        "ensure_camera_mosquitto_listener",
        lambda enabled: mosquitto_calls.append(enabled)
        or {"service": "mosquitto-lan-listener", "managed": "configure", "ok": True},
    )

    def fake_set_service_enabled(service_name, enabled):
        enable_calls.append((service_name, enabled))
        return {
            "service": service_name,
            "managed": "enable" if enabled else "disable",
            "ok": True,
            "active": enabled,
        }

    def fake_restart_managed_service(service_name):
        restart_calls.append(service_name)
        return {
            "service": service_name,
            "managed": "restart",
            "ok": True,
            "active": "active",
        }

    monkeypatch.setattr(device_configurator_service, "set_service_enabled", fake_set_service_enabled)
    monkeypatch.setattr(device_configurator_service, "restart_managed_service", fake_restart_managed_service)

    result = device_configurator_service.reconcile_camera_services()

    assert result["status"] == "succeeded"
    assert result["error_message"] == ""
    assert mosquitto_calls == [True]
    assert enable_calls == [
        ("mqtt-sender", False),
        ("videorecorder", True),
        ("mqtt-receiver", True),
        ("upload", True),
    ]
    assert restart_calls == ["videorecorder", "mqtt-receiver", "upload"]


def test_reconcile_camera_services_disables_all_when_camera_toggle_is_off(monkeypatch):
    calls = []
    restart_calls = []
    mosquitto_calls = []

    monkeypatch.setattr(device_configurator_service, "camera_services_enabled", lambda: False)
    monkeypatch.setattr(
        device_configurator_service,
        "ensure_camera_mosquitto_listener",
        lambda enabled: mosquitto_calls.append(enabled)
        or {"service": "mosquitto-lan-listener", "managed": "remove", "ok": True},
    )

    def fake_set_service_enabled(service_name, enabled):
        calls.append((service_name, enabled))
        return {
            "service": service_name,
            "managed": "enable" if enabled else "disable",
            "ok": True,
            "active": enabled,
        }

    def fake_restart_managed_service(service_name):
        restart_calls.append(service_name)
        return {"service": service_name, "managed": "restart", "ok": True}

    monkeypatch.setattr(device_configurator_service, "set_service_enabled", fake_set_service_enabled)
    monkeypatch.setattr(device_configurator_service, "restart_managed_service", fake_restart_managed_service)

    result = device_configurator_service.reconcile_camera_services()

    assert result["status"] == "succeeded"
    assert result["error_message"] == ""
    assert mosquitto_calls == [False]
    assert calls == [
        ("mqtt-sender", False),
        ("videorecorder", False),
        ("mqtt-receiver", False),
        ("upload", False),
    ]
    assert restart_calls == []


def test_reconcile_camera_services_respects_runtime_override(monkeypatch):
    enable_calls = []
    restart_calls = []
    mosquitto_calls = []

    monkeypatch.setenv("DEVICE_TYPE", "camera")
    monkeypatch.delenv("HAS_CAMERA", raising=False)
    monkeypatch.delenv("CAMERA_TRIGGER_MODE", raising=False)
    monkeypatch.setattr(device_configurator_service, "camera_services_enabled", lambda: False)
    monkeypatch.setattr(device_configurator_service, "has_upload_configuration", lambda: True)
    monkeypatch.setattr(
        device_configurator_service,
        "ensure_camera_mosquitto_listener",
        lambda enabled: mosquitto_calls.append(enabled)
        or {"service": "mosquitto-lan-listener", "managed": "configure", "ok": True},
    )

    def fake_set_service_enabled(service_name, enabled):
        enable_calls.append((service_name, enabled))
        return {
            "service": service_name,
            "managed": "enable" if enabled else "disable",
            "ok": True,
            "active": enabled,
        }

    def fake_restart_managed_service(service_name):
        restart_calls.append(service_name)
        return {"service": service_name, "managed": "restart", "ok": True, "active": "active"}

    monkeypatch.setattr(device_configurator_service, "set_service_enabled", fake_set_service_enabled)
    monkeypatch.setattr(device_configurator_service, "restart_managed_service", fake_restart_managed_service)

    result = device_configurator_service.reconcile_camera_services(enabled_override=True)

    assert result["status"] == "succeeded"
    assert mosquitto_calls == [True]
    assert enable_calls == [
        ("mqtt-sender", False),
        ("videorecorder", True),
        ("mqtt-receiver", True),
        ("upload", True),
    ]
    assert restart_calls == ["videorecorder", "mqtt-receiver", "upload"]


def test_reconcile_camera_services_uses_filesystem_mode_without_mqtt(monkeypatch):
    enable_calls = []
    restart_calls = []
    mosquitto_calls = []

    monkeypatch.setenv("DEVICE_TYPE", "odroid")
    monkeypatch.setenv("HAS_CAMERA", "True")
    monkeypatch.setenv("CAMERA_TRIGGER_MODE", "filesystem")
    monkeypatch.setattr(device_configurator_service, "camera_services_enabled", lambda: False)
    monkeypatch.setattr(device_configurator_service, "has_upload_configuration", lambda: False)
    monkeypatch.setattr(
        device_configurator_service,
        "ensure_camera_mosquitto_listener",
        lambda enabled: mosquitto_calls.append(enabled)
        or {"service": "mosquitto-lan-listener", "managed": "remove", "ok": True},
    )

    def fake_set_service_enabled(service_name, enabled):
        enable_calls.append((service_name, enabled))
        return {"service": service_name, "managed": "enable" if enabled else "disable", "ok": True}

    def fake_restart_managed_service(service_name):
        restart_calls.append(service_name)
        return {"service": service_name, "managed": "restart", "ok": True}

    monkeypatch.setattr(device_configurator_service, "set_service_enabled", fake_set_service_enabled)
    monkeypatch.setattr(device_configurator_service, "restart_managed_service", fake_restart_managed_service)

    result = device_configurator_service.reconcile_camera_services(enabled_override=True)

    assert result["status"] == "succeeded"
    assert mosquitto_calls == [False]
    assert enable_calls == [
        ("mqtt-sender", False),
        ("videorecorder", True),
        ("mqtt-receiver", False),
        ("upload", False),
    ]
    assert restart_calls == ["videorecorder"]


def test_reconcile_camera_services_skips_restart_when_enable_fails(monkeypatch):
    enable_calls = []
    restart_calls = []
    mosquitto_calls = []

    monkeypatch.setenv("DEVICE_TYPE", "camera")
    monkeypatch.delenv("HAS_CAMERA", raising=False)
    monkeypatch.delenv("CAMERA_TRIGGER_MODE", raising=False)
    monkeypatch.setattr(device_configurator_service, "camera_services_enabled", lambda: True)
    monkeypatch.setattr(device_configurator_service, "has_upload_configuration", lambda: False)
    monkeypatch.setattr(
        device_configurator_service,
        "ensure_camera_mosquitto_listener",
        lambda enabled: mosquitto_calls.append(enabled)
        or {"service": "mosquitto-lan-listener", "managed": "configure", "ok": True},
    )

    def fake_set_service_enabled(service_name, enabled):
        enable_calls.append((service_name, enabled))
        return {
            "service": service_name,
            "managed": "enable" if enabled else "disable",
            "ok": service_name != "videorecorder",
            "active": enabled,
        }

    def fake_restart_managed_service(service_name):
        restart_calls.append(service_name)
        return {"service": service_name, "managed": "restart", "ok": True, "active": "active"}

    monkeypatch.setattr(device_configurator_service, "set_service_enabled", fake_set_service_enabled)
    monkeypatch.setattr(device_configurator_service, "restart_managed_service", fake_restart_managed_service)

    result = device_configurator_service.reconcile_camera_services()

    assert result["status"] == "failed"
    assert mosquitto_calls == [True]
    assert enable_calls == [
        ("mqtt-sender", False),
        ("videorecorder", True),
        ("mqtt-receiver", True),
        ("upload", False),
    ]
    assert restart_calls == ["mqtt-receiver"]


def test_reconcile_camera_services_fails_when_mosquitto_listener_cannot_be_prepared(monkeypatch):
    monkeypatch.setattr(device_configurator_service, "camera_services_enabled", lambda: True)
    monkeypatch.setattr(
        device_configurator_service,
        "ensure_camera_mosquitto_listener",
        lambda enabled: {
            "service": "mosquitto-lan-listener",
            "managed": "configure",
            "ok": False,
            "stderr": "permission denied",
        },
    )

    result = device_configurator_service.reconcile_camera_services()

    assert result["status"] == "failed"
    assert result["error_message"] == "No se pudo preparar el broker MQTT de la camara."


def test_persist_env_values_does_not_store_camera_enabled(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text('HOSTNAME="https://example.com"\nCAMERA_ENABLED="False"\n')

    monkeypatch.setattr(device_configurator_service, "ENV_PATH", env_path)
    monkeypatch.setenv("CAMERA_ENABLED", "False")

    device_configurator_service.persist_env_values({"CAMERA_ENABLED": True, "HOSTNAME": "https://new.example.com"})

    rendered = env_path.read_text()
    assert 'HOSTNAME="https://new.example.com"' in rendered
    assert 'CAMERA_ENABLED="False"' in rendered
    assert os.environ["CAMERA_ENABLED"] == "False"


def test_current_device_settings_reports_camera_enabled_from_service_state(monkeypatch):
    monkeypatch.setenv("HOSTNAME", "https://example.com")
    monkeypatch.setattr(
        device_configurator_service,
        "current_camera_services",
        lambda: [
            {"service": "videorecorder", "active": "active", "enabled": "enabled"},
            {"service": "mqtt-receiver", "active": "active", "enabled": "enabled"},
            {"service": "upload", "active": "inactive", "enabled": "disabled"},
        ],
    )

    settings = device_configurator_service.current_device_settings()

    assert settings["CAMERA_ENABLED"] is True


def test_current_device_settings_reports_camera_source_and_rtsp_url(monkeypatch):
    monkeypatch.setenv("CAMERA_SOURCE", "ip")
    monkeypatch.setenv("CAMERA_RTSP_URL", "rtsp://camera.example/media/video1")
    monkeypatch.setattr(device_configurator_service, "current_camera_services", lambda: [])

    settings = device_configurator_service.current_device_settings()

    assert settings["CAMERA_SOURCE"] == "ip"
    assert settings["CAMERA_RTSP_URL"] == "rtsp://camera.example/media/video1"


def test_reconcile_qr_services_enables_only_configured_direction(monkeypatch):
    calls = []
    restart_calls = []

    monkeypatch.setenv("DEVICE_TYPE", "odroid")
    monkeypatch.setenv("QR_ACTIVE_DIRECTIONS", "A")

    def fake_set_service_enabled(service_name, enabled):
        calls.append((service_name, enabled))
        return {"service": service_name, "managed": "enable" if enabled else "disable", "ok": True}

    def fake_restart_managed_service(service_name):
        restart_calls.append(service_name)
        return {"service": service_name, "managed": "restart", "ok": True}

    monkeypatch.setattr(device_configurator_service, "set_service_enabled", fake_set_service_enabled)
    monkeypatch.setattr(device_configurator_service, "restart_managed_service", fake_restart_managed_service)

    result = device_configurator_service.reconcile_qr_services()

    assert result["status"] == "succeeded"
    assert calls == [("qr_script_a", True), ("qr_script_b", False)]
    assert restart_calls == ["qr_script_a"]


def test_reconcile_qr_services_disables_all_for_camera(monkeypatch):
    calls = []
    restart_calls = []

    monkeypatch.setenv("DEVICE_TYPE", "camera")
    monkeypatch.setenv("QR_ACTIVE_DIRECTIONS", "A,B")

    def fake_set_service_enabled(service_name, enabled):
        calls.append((service_name, enabled))
        return {"service": service_name, "managed": "enable" if enabled else "disable", "ok": True}

    monkeypatch.setattr(device_configurator_service, "set_service_enabled", fake_set_service_enabled)
    monkeypatch.setattr(
        device_configurator_service,
        "restart_managed_service",
        lambda service_name: restart_calls.append(service_name) or {"service": service_name, "managed": "restart", "ok": True},
    )

    result = device_configurator_service.reconcile_qr_services()

    assert result["status"] == "succeeded"
    assert calls == [("qr_script_a", False), ("qr_script_b", False)]
    assert restart_calls == []


def test_configured_qr_directions_falls_back_to_entrances(monkeypatch):
    monkeypatch.setenv("DEVICE_TYPE", "odroid")
    monkeypatch.delenv("QR_ACTIVE_DIRECTIONS", raising=False)
    monkeypatch.delenv("USE_2_QR_READERS", raising=False)
    monkeypatch.setattr(
        device_configurator_service,
        "configured_entrances",
        lambda: [{"slot": "A", "uuid": "entrada"}, {"slot": "B", "uuid": "salida"}],
    )

    assert device_configurator_service.configured_qr_directions() == ["A", "B"]


def test_update_env_schedules_reboot_after_success(monkeypatch):
    monkeypatch.setattr(device_configurator_service, "persist_env_values", lambda payload: None)
    monkeypatch.setattr(
        device_configurator_service,
        "reconcile_camera_services",
        lambda enabled_override=None: {"status": "succeeded", "error_message": "", "service_results": []},
    )
    monkeypatch.setattr(
        device_configurator_service,
        "reconcile_qr_services",
        lambda: {"status": "succeeded", "error_message": "", "service_results": []},
    )
    monkeypatch.setattr(device_configurator_service, "configured_entrances", lambda: [])
    monkeypatch.setattr(device_configurator_service, "current_wifi_ssid", lambda: "DIGIFIBRA")
    monkeypatch.setattr(device_configurator_service, "parse_wifi_scan", lambda: [{"ssid": "DIGIFIBRA", "signal": "78"}])
    monkeypatch.setattr(device_configurator_service, "current_ssh_tunnel", lambda: {"ssh_port": 6007})
    monkeypatch.setattr(device_configurator_service, "current_device_settings", lambda: {"HOSTNAME": "https://admin.example.com"})
    monkeypatch.setattr(device_configurator_service, "current_camera_services", lambda: [])
    monkeypatch.setattr(device_configurator_service, "current_qr_services", lambda: [])

    result = device_configurator_service.update_env({"HOSTNAME": "https://admin.example.com"})

    assert result["status"] == "succeeded"
    assert result["result"]["service_restarts"] == []
    assert result["_post_result_action"] == {"type": "reboot", "delay_seconds": 3}


def test_ensure_service_unit_installed_skips_copy_when_unit_matches(tmp_path, monkeypatch):
    source_path = tmp_path / "videorecorder.service"
    target_dir = tmp_path / "etc/systemd/system"
    target_dir.mkdir(parents=True)
    target_path = target_dir / "videorecorder.service"
    rendered = "[Unit]\nDescription=Video Recorder\n"
    source_path.write_text(rendered)
    target_path.write_text(rendered)

    monkeypatch.setattr(device_configurator_service, "CURRENT_DIR", tmp_path)
    monkeypatch.setattr(device_configurator_service, "SYSTEMD_UNIT_DIR", target_dir)

    result = device_configurator_service.ensure_service_unit_installed("videorecorder")

    assert result is None
def test_get_device_identifier_prefers_configured_identifier(monkeypatch):
    monkeypatch.setattr(device_configurator_service, "get_hardware_mac_address", lambda: "e0:e1:a9:3d:41:43")
    monkeypatch.setenv("DEVICE_IDENTIFIER", "from-env")
    monkeypatch.setattr(device_configurator_service.socket, "gethostname", lambda: "odroid")

    assert device_configurator_service.get_device_identifier() == "from-env"


def test_get_device_identifier_falls_back_to_mac_then_hostname(monkeypatch):
    monkeypatch.setattr(device_configurator_service, "get_hardware_mac_address", lambda: "")
    monkeypatch.setenv("DEVICE_IDENTIFIER", "from-env")
    monkeypatch.setattr(device_configurator_service.socket, "gethostname", lambda: "odroid")

    assert device_configurator_service.get_device_identifier() == "from-env"

    monkeypatch.setenv("DEVICE_IDENTIFIER", " ")
    monkeypatch.setattr(device_configurator_service, "get_hardware_mac_address", lambda: "e0:e1:a9:3d:41:43")
    assert device_configurator_service.get_device_identifier() == "e0:e1:a9:3d:41:43"

    monkeypatch.setenv("DEVICE_IDENTIFIER", " ")
    monkeypatch.setattr(device_configurator_service, "get_hardware_mac_address", lambda: "")
    assert device_configurator_service.get_device_identifier() == "odroid"


def test_device_payload_includes_usb_snapshot_and_pending_events(monkeypatch):
    monkeypatch.setattr(device_configurator_service, "get_device_identifier", lambda: "odroid-1")
    monkeypatch.setattr(device_configurator_service, "get_device_name", lambda: "Front door")
    monkeypatch.setattr(device_configurator_service.socket, "gethostname", lambda: "odroid-host")
    monkeypatch.setattr(device_configurator_service, "get_device_type", lambda: "odroid")
    monkeypatch.setattr(device_configurator_service, "get_hardware_model", lambda: "odroid-m1")
    monkeypatch.setattr(device_configurator_service, "get_hardware_mac_address", lambda: "00:11")
    monkeypatch.setattr(device_configurator_service, "get_software_version", lambda: "1.2.3")
    monkeypatch.setattr(device_configurator_service, "get_ip_address", lambda: "10.0.0.9")
    monkeypatch.setattr(device_configurator_service, "current_wifi_ssid", lambda: "DIGI")
    monkeypatch.setattr(device_configurator_service, "ethernet_connected", lambda: True)
    monkeypatch.setattr(device_configurator_service, "parse_wifi_scan", lambda: [{"ssid": "DIGI"}])
    monkeypatch.setattr(device_configurator_service, "configured_entrances", lambda: [{"slot": "A", "uuid": "door-a"}])
    monkeypatch.setattr(device_configurator_service, "current_ssh_tunnel", lambda: {"ssh_port": 6015})
    monkeypatch.setattr(device_configurator_service, "current_device_settings", lambda: {"CAMERA_ENABLED": True})
    monkeypatch.setattr(device_configurator_service, "current_camera_services", lambda: [{"service": "videorecorder"}])
    monkeypatch.setattr(device_configurator_service, "get_usb_status_snapshot", lambda: {"components": {"qr_a": {"connected": True}}})
    monkeypatch.setattr(device_configurator_service, "get_pending_events", lambda: [{"event_uuid": "123", "component": "qr_a"}])

    payload = device_configurator_service.device_payload()

    assert payload["usb_status"] == {"components": {"qr_a": {"connected": True}}}
    assert payload["usb_events"] == [{"event_uuid": "123", "component": "qr_a"}]
