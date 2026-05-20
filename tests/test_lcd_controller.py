import importlib
import sys
import types


class FakeLine:
    def __init__(self):
        self.values = []
        self.released = False

    def request(self, consumer, type):
        self.consumer = consumer
        self.type = type

    def set_value(self, value):
        self.values.append(value)

    def release(self):
        self.released = True


class FakeChip:
    line = FakeLine()

    def __init__(self, name):
        self.name = name

    def get_line(self, pin):
        self.pin = pin
        return self.line


class FakeLCD:
    should_fail_probe = False

    def __init__(self, *args, **kwargs):
        self.calls = []

    def probe(self):
        if self.should_fail_probe:
            raise OSError("I2C device not available")

    def clear(self):
        self.calls.append(("clear",))

    def text(self, value, line):
        self.calls.append(("text", value, line))


def load_lcd_controller(monkeypatch):
    fake_gpiod = types.SimpleNamespace(Chip=FakeChip, LINE_REQ_DIR_OUT="out")
    fake_smbus = types.SimpleNamespace(SMBus=lambda bus_num: object())
    fake_unidecode = types.SimpleNamespace(unidecode=lambda value: value)
    monkeypatch.setitem(sys.modules, "gpiod", fake_gpiod)
    monkeypatch.setitem(sys.modules, "smbus", fake_smbus)
    monkeypatch.setitem(sys.modules, "unidecode", fake_unidecode)
    sys.modules.pop("lcd_controller", None)
    module = importlib.import_module("lcd_controller")
    monkeypatch.setattr(module, "LCD", FakeLCD)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    FakeChip.line = FakeLine()
    FakeLCD.should_fail_probe = False
    return module


def test_dark_mode_turns_display_relay_on_while_showing_timed_text(monkeypatch):
    module = load_lcd_controller(monkeypatch)
    controller = module.LCDController(
        use_lcd=True,
        dark_mode=True,
        relay_pin=69,
        relay_trigger="LOW",
        scroll_delay=0,
    )

    controller.display_text_on_lcd("Hola", "Fitness", timeout=2)

    assert FakeChip.line.values == [1, 0, 1]


def test_dark_mode_keeps_display_relay_on_for_persistent_text(monkeypatch):
    module = load_lcd_controller(monkeypatch)
    controller = module.LCDController(
        use_lcd=True,
        dark_mode=True,
        relay_pin=69,
        relay_trigger="LOW",
        scroll_delay=0,
    )

    controller.display_text_on_lcd("Escanea", "codigo QR", timeout=None)

    assert FakeChip.line.values == [1, 0]


def test_dark_mode_cleanup_turns_display_relay_off(monkeypatch):
    module = load_lcd_controller(monkeypatch)
    controller = module.LCDController(
        use_lcd=True,
        dark_mode=True,
        relay_pin=69,
        relay_trigger="LOW",
        scroll_delay=0,
    )

    controller.display_text_on_lcd("Escanea", "codigo QR", timeout=None)
    controller.cleanup()

    assert FakeChip.line.values == [1, 0, 1]
    assert FakeChip.line.released is True


def test_dark_mode_does_not_toggle_display_relay_when_lcd_probe_fails(monkeypatch):
    module = load_lcd_controller(monkeypatch)
    FakeLCD.should_fail_probe = True
    controller = module.LCDController(
        use_lcd=True,
        dark_mode=True,
        relay_pin=69,
        relay_trigger="LOW",
        scroll_delay=0,
    )

    controller.display_text_on_lcd("Fallo lector USB", "Reintentando", timeout=2)

    assert FakeChip.line.values == [1]


def test_dark_mode_retries_lcd_after_probe_failure(monkeypatch):
    module = load_lcd_controller(monkeypatch)
    FakeLCD.should_fail_probe = True
    controller = module.LCDController(
        use_lcd=True,
        dark_mode=True,
        relay_pin=69,
        relay_trigger="LOW",
        scroll_delay=0,
    )

    controller.display_text_on_lcd("Fallo lector USB", "Reintentando", timeout=2)
    FakeLCD.should_fail_probe = False
    controller.display_text_on_lcd("Fallo lector USB", "Reintentando", timeout=2)

    assert FakeChip.line.values == [1, 0, 1]
