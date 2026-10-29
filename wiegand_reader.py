"""Optional Wiegand reader for ODROID/libgpiod (v1 API).

This module is intentionally independent from QR verification and relay
control.  It is only selected when READER_TYPE=wiegand; USB readers never
instantiate it.
"""

import logging
import queue
import threading
import time
from dataclasses import dataclass

import gpiod


logger = logging.getLogger("qr_logger")


@dataclass(frozen=True)
class WiegandFrame:
    bits: str

    @property
    def bit_count(self):
        return len(self.bits)

    @property
    def raw_decimal(self):
        return str(int(self.bits, 2))

    @property
    def data_bits(self):
        if self.bit_count in (26, 34, 37):
            return self.bits[1:-1]
        return self.bits

    @property
    def data_decimal(self):
        return str(int(self.data_bits, 2))

    @property
    def facility_and_card(self):
        if self.bit_count != 26:
            return None
        data = self.data_bits
        return f"{int(data[:8], 2)}:{int(data[8:], 2)}"

    def as_string(self, output_mode="raw"):
        if output_mode == "raw":
            return self.raw_decimal
        if output_mode == "data":
            return self.data_decimal
        if output_mode == "facility_card" and self.facility_and_card is not None:
            return self.facility_and_card
        raise ValueError(f"Unsupported Wiegand output mode or frame: {output_mode}")


class WiegandReader:
    """Collect falling edges from D0 and D1 using libgpiod v1."""

    def __init__(self, d0_pin, d1_pin, frame_timeout=0.05, max_bits=128):
        self.d0_pin = int(d0_pin)
        self.d1_pin = int(d1_pin)
        self.frame_timeout = float(frame_timeout)
        self.max_bits = int(max_bits)
        self._events = queue.Queue()
        self._stop = threading.Event()
        self._threads = []
        self._chip = None
        self._lines = []

    def start(self):
        if self._threads:
            return

        self._chip = gpiod.Chip("gpiochip0")
        for pin, bit in ((self.d0_pin, "0"), (self.d1_pin, "1")):
            line = self._chip.get_line(pin)
            line.request(
                consumer=f"wiegand_d{bit}",
                type=gpiod.LINE_REQ_EV_FALLING_EDGE,
            )
            self._lines.append(line)
            thread = threading.Thread(target=self._read_line, args=(line, bit), daemon=True)
            thread.start()
            self._threads.append(thread)

        logger.info("Wiegand reader started on D0=%s D1=%s", self.d0_pin, self.d1_pin)

    def _read_line(self, line, bit):
        while not self._stop.is_set():
            try:
                if not line.event_wait(sec=0.2):
                    continue
                line.event_read()
                self._events.put((time.monotonic(), bit))
            except (OSError, RuntimeError, ValueError) as error:
                if not self._stop.is_set():
                    logger.error("Wiegand GPIO line failed for bit %s: %s", bit, error)
                return

    def read_frame(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        bits = []
        last_pulse = None

        while True:
            now = time.monotonic()
            if bits and last_pulse is not None and now - last_pulse >= self.frame_timeout:
                return WiegandFrame("".join(bits))

            wait_for = 0.01
            if last_pulse is not None:
                wait_for = min(wait_for, max(self.frame_timeout - (now - last_pulse), 0.001))
            if deadline is not None:
                remaining = deadline - now
                if remaining <= 0:
                    return WiegandFrame("".join(bits)) if bits else None
                wait_for = min(wait_for, remaining)

            try:
                pulse_time, bit = self._events.get(timeout=wait_for)
            except queue.Empty:
                continue

            if len(bits) >= self.max_bits:
                logger.warning("Discarding oversized Wiegand frame (>%s bits)", self.max_bits)
                bits = []
            bits.append(bit)
            last_pulse = pulse_time

    def close(self):
        self._stop.set()
        for line in self._lines:
            try:
                line.release()
            except (OSError, RuntimeError, ValueError):
                pass
        for thread in self._threads:
            thread.join(timeout=0.5)
        self._lines = []
        self._threads = []
        self._chip = None

