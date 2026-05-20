import logging
import threading
import time
import gpiod
import smbus
from unidecode import unidecode


class LCD:
    """16x2 LCD display with I2C PCF8574 backpack - Odroid compatible"""
    
    # LCD Commands
    LCD_CLEARDISPLAY = 0x01
    LCD_RETURNHOME = 0x02
    LCD_ENTRYMODESET = 0x04
    LCD_DISPLAYCONTROL = 0x08
    LCD_CURSORSHIFT = 0x10
    LCD_FUNCTIONSET = 0x20
    LCD_SETCGRAMADDR = 0x40
    LCD_SETDDRAMADDR = 0x80
    
    # Entry flags
    LCD_ENTRYRIGHT = 0x00
    LCD_ENTRYLEFT = 0x02
    LCD_ENTRYSHIFTINCREMENT = 0x01
    LCD_ENTRYSHIFTDECREMENT = 0x00
    
    # Display control flags
    LCD_DISPLAYON = 0x04
    LCD_DISPLAYOFF = 0x00
    LCD_CURSORON = 0x02
    LCD_CURSOROFF = 0x00
    LCD_BLINKON = 0x01
    LCD_BLINKOFF = 0x00
    
    # Function set flags
    LCD_8BITMODE = 0x10
    LCD_4BITMODE = 0x00
    LCD_2LINE = 0x08
    LCD_1LINE = 0x00
    LCD_5x10DOTS = 0x04
    LCD_5x8DOTS = 0x00
    
    # PCF8574 pin mapping
    RS = 0x01    # P0
    RW = 0x02    # P1  
    E  = 0x04    # P2
    BACKLIGHT = 0x08  # P3
    
    def __init__(self, bus_num=0, address=0x27):
        """Initialize LCD on specified I2C bus and address"""
        self.bus_num = bus_num
        self.address = address
        self.backlight_state = self.BACKLIGHT
        
        try:
            self.bus = smbus.SMBus(bus_num)
            self._init_lcd()
        except Exception as e:
            logging.error(f"Failed to initialize LCD: {e}")
            raise
    
    def _write_byte(self, data):
        """Write byte to I2C bus"""
        self.bus.write_byte(self.address, data)

    def probe(self):
        """Check that the I2C backpack responds before toggling external light."""
        self.bus.write_byte(self.address, self.backlight_state)
    
    def _write_nibble(self, data):
        """Write 4-bit nibble to LCD"""
        data |= self.backlight_state
        self._write_byte(data)
        self._write_byte(data | self.E)
        time.sleep(0.0005)
        self._write_byte(data & ~self.E)
        time.sleep(0.0001)
    
    def _write_byte_data(self, data, mode=0):
        """Write byte to LCD in 4-bit mode"""
        high_nibble = mode | (data & 0xF0) | self.backlight_state
        low_nibble = mode | ((data << 4) & 0xF0) | self.backlight_state
        
        self._write_nibble(high_nibble)
        self._write_nibble(low_nibble)
    
    def _init_lcd(self):
        """Initialize LCD in 4-bit mode"""
        time.sleep(0.05)
        
        # Initialize in 8-bit mode
        self._write_nibble(0x30 | self.backlight_state)
        time.sleep(0.005)
        self._write_nibble(0x30 | self.backlight_state)
        time.sleep(0.0002)
        self._write_nibble(0x30 | self.backlight_state)
        time.sleep(0.0002)
        
        # Switch to 4-bit mode
        self._write_nibble(0x20 | self.backlight_state)
        time.sleep(0.0002)
        
        # Configure LCD
        self._write_byte_data(self.LCD_FUNCTIONSET | self.LCD_4BITMODE | self.LCD_2LINE | self.LCD_5x8DOTS)
        self._write_byte_data(self.LCD_DISPLAYCONTROL | self.LCD_DISPLAYON | self.LCD_CURSOROFF | self.LCD_BLINKOFF)
        self.clear()
        self._write_byte_data(self.LCD_ENTRYMODESET | self.LCD_ENTRYLEFT | self.LCD_ENTRYSHIFTDECREMENT)
        time.sleep(0.002)
    
    def clear(self):
        """Clear LCD display"""
        self._write_byte_data(self.LCD_CLEARDISPLAY)
        time.sleep(0.002)
    
    def home(self):
        """Return cursor to home position"""
        self._write_byte_data(self.LCD_RETURNHOME)
        time.sleep(0.002)
    
    def set_cursor(self, col, row):
        """Set cursor position"""
        row_offsets = [0x00, 0x40]
        if row >= 2:
            row = 1
        if col >= 16:
            col = 15
        self._write_byte_data(self.LCD_SETDDRAMADDR | (col + row_offsets[row]))
    
    def text(self, message, line=1):
        """Display text on specified line"""
        if line == 1:
            self.set_cursor(0, 0)
        else:
            self.set_cursor(0, 1)
        
        message = str(message)[:16].ljust(16)
        
        for char in message:
            self._write_byte_data(ord(char), self.RS)


class LCDController:
    def __init__(
        self,
        use_lcd,
        max_char_count=16,
        scroll_delay=0.5,
        lcd_address=None,
        dark_mode=False,
        relay_pin=None,
        relay_trigger="LOW",
        i2c_bus=0
    ):
        self.use_lcd = use_lcd
        self.max_char_count = max_char_count
        self.scroll_delay = scroll_delay
        self.i2c_bus = i2c_bus
        self._lock = threading.Lock()
        self.lcd = None
        
        # Parse LCD address
        if lcd_address:
            if isinstance(lcd_address, str):
                self.lcd_address = int(lcd_address, 16) if lcd_address.startswith('0x') else int(lcd_address)
            else:
                self.lcd_address = lcd_address
        else:
            self.lcd_address = 0x27
            
        if use_lcd:
            self._initialize_lcd()
                
        self.dark_mode = dark_mode
        self.relay_chip = None
        self.relay_line = None
        self.relay_on_state = 0 if relay_trigger == "LOW" else 1
        self.relay_off_state = 1 if relay_trigger == "LOW" else 0
        
        # Setup GPIO for display relay using gpiod
        if dark_mode and relay_pin:
            try:
                self.relay_chip = gpiod.Chip("gpiochip0")
                self.relay_line = self.relay_chip.get_line(relay_pin)
                self.relay_line.request(consumer="lcd_backlight", type=gpiod.LINE_REQ_DIR_OUT)
                self._set_display_relay(False)
                logging.info(f"Display relay initialized on GPIO line {relay_pin}")
            except Exception as e:
                logging.error(f"Failed to setup display relay: {e}")

    def _initialize_lcd(self):
        try:
            self.lcd = LCD(bus_num=self.i2c_bus, address=self.lcd_address)
            logging.info(f"LCD initialized on I2C bus {self.i2c_bus}, address 0x{self.lcd_address:02X}")
            return True
        except Exception as e:
            logging.error(f"Failed to initialize LCD: {e}")
            self.lcd = None
            return False

    def _ensure_lcd_available(self):
        if not self.use_lcd:
            return False

        if not self.lcd and not self._initialize_lcd():
            return False

        try:
            self.lcd.probe()
            return True
        except Exception as e:
            logging.error(f"LCD I2C probe failed: {e}")
            self.lcd = None
            return False

    def _set_display_relay(self, enabled):
        if not self.dark_mode or not self.relay_line:
            return

        self.relay_line.set_value(self.relay_on_state if enabled else self.relay_off_state)
    
    def clear(self):
        if self.use_lcd and self.lcd:
            try:
                self.lcd.clear()
            except Exception as e:
                logging.error(f"LCD clear error: {e}")
                self.lcd = None
        else:
            logging.info("Clearing display")

    def scroll_text(self, line: str) -> list[str]:
        line_length = len(line)
        if line_length <= self.max_char_count:
            return [line]

        scroll_positions = line_length - self.max_char_count + 1
        return [line[i : i + self.max_char_count] for i in range(scroll_positions)]

    def display_text_on_lcd(self, line1, line2, timeout=None):
        if not self.use_lcd:
            logging.info(f"Display unavailable: {line1} | {line2}")
            return

        with self._lock:
            if not self._ensure_lcd_available():
                logging.info(f"Display unavailable: {line1} | {line2}")
                return

            relay_enabled = False
            keep_relay_on = False
            try:
                lines_to_scroll1 = self.scroll_text(line1)
                lines_to_scroll2 = self.scroll_text(line2)

                self._set_display_relay(True)
                relay_enabled = True

                for i in range(max(len(lines_to_scroll1), len(lines_to_scroll2))):
                    self.lcd.clear()
                    self.lcd.text(unidecode(lines_to_scroll1[i % len(lines_to_scroll1)]), 1)
                    self.lcd.text(unidecode(lines_to_scroll2[i % len(lines_to_scroll2)]), 2)
                    time.sleep(self.scroll_delay)

                if timeout is None:
                    keep_relay_on = True
                else:
                    time.sleep(max(0, timeout - self.scroll_delay))
                    self.lcd.clear()
            except Exception as e:
                logging.error(f"LCD display error: {e}")
                self.lcd = None
            finally:
                if relay_enabled and not keep_relay_on:
                    self._set_display_relay(False)

    def display(self, line1, line2, timeout=2):
        self.display_text_on_lcd(line1, line2, timeout)

    def display_text_on_lcd_async(self, line1, line2, timeout=3):
        thread = threading.Thread(
            target=self.display_text_on_lcd, 
            args=(line1, line2, timeout), 
            daemon=True
        )
        thread.start()

    def cleanup(self):
        """Clean up GPIO resources"""
        if self.relay_line:
            try:
                self._set_display_relay(False)
                self.relay_line.release()
            except:
                pass
