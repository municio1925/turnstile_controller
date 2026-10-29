import sys
import types


sys.modules.setdefault("gpiod", types.SimpleNamespace())

from wiegand_reader import WiegandFrame


def test_26_bit_frame_exposes_standard_values():
    frame = WiegandFrame("0" + format((12 << 16) | 3456, "024b") + "0")

    assert frame.bit_count == 26
    assert frame.facility_and_card == "12:3456"
    assert frame.data_decimal == str((12 << 16) | 3456)


def test_unknown_frame_uses_raw_data():
    frame = WiegandFrame("10101")

    assert frame.raw_decimal == "21"
    assert frame.data_decimal == "21"

