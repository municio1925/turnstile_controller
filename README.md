## Install git on Raspberry Pi
```bash
sudo apt update
sudo apt install git
```

## Install pip3 on Raspberry Pi
```bash
sudo apt update
sudo apt install python3-pip
```

## Clone the git repository
````bash
git clone https://github.com/plysytsya/turnstile_controller.git
````

## installar virtualenv
```
sudo apt install python3-venv
```
```
python3 -m venv /home/manager/turnstile_controller/venv
```

## activar virtualenv
```
source /home/manager/turnstile_controller/venv/bin/activate
```

### Install dependencies
```bash
pip3 install -r requirements.txt
```

### Paste .env file
```bash
nano .env
```

## Enable I2C Interface

1. Run `sudo raspi-config`.
2. Navigate to `Interface Options` or `Interfacing Options`.
3. Select `I2C` and enable it.
4. Exit and reboot your Raspberry Pi. `sudo reboot`

## Install I2C Tools

If you haven't already, you should install the I2C tools:

```bash
sudo apt-get update
sudo apt-get install -y i2c-tools libi2c-dev
```

Install our services (QR reader and cronjob to periodically download the customer database)

```bash
make install-qr
```

```bash
make install-cronjob
```

### Optional Wiegand reader

USB readers remain the default. To test a GPIO Wiegand reader on one dedicated
ODROID, configure its `.env` through the device advanced settings:

```dotenv
READER_TYPE=wiegand
WIEGAND_D0_PIN=<BCM GPIO line>
WIEGAND_D1_PIN=<BCM GPIO line>
WIEGAND_OUTPUT_MODE=raw
```

The controller logs the complete frame, parity-stripped data, and the standard
26-bit facility/card representation. Keep `READER_TYPE=usb` on existing
devices until the GPIO wiring and identifier mapping are verified.

## QR reader direction mapping

By default, the controller keeps the legacy two-reader mapping:

- direction `A` prefers a serial-mode QR reader, then falls back to keyboard/HID mode
- direction `B` prefers a keyboard/HID QR reader, then falls back to serial mode

This means a single active `A` door works with either one serial reader or one keyboard reader. With
`USE_2_QR_READERS=true` or `QR_ACTIVE_DIRECTIONS=A,B` and one reader in each mode, serial is assigned to `A` and
keyboard/HID is assigned to `B`.

Optional `.env` overrides:

```bash
QR_ACTIVE_DIRECTIONS=A,B
QR_READER_MODE_A=serial
QR_READER_MODE_B=keyboard
```

`QR_READER_MODE_A` and `QR_READER_MODE_B` accept `auto`, `serial`, `keyboard`, or `disabled`. To flip the default
two-reader assignment, use:

```bash
QR_READER_MODE_A=keyboard
QR_READER_MODE_B=serial
```

For fixed device paths, use `QR_USB_DEVICE_PATH_A` / `QR_USB_DEVICE_PATH_B` with the matching reader mode.
