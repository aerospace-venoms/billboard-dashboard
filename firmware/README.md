# ESP32-S3 firmware

C firmware for the 960×672 black/white/red Billboard Badge, tested with ESP-IDF 5.4,
8 MB quad flash and 8 MB octal PSRAM. Start with the root setup wizard; it generates
`main/private_config.h` and `main/dashboard_ca.h`. Neither file belongs in Git.
Check your board revision and preserve your original firmware before flashing.

After installing ESP-IDF 5.4 and activating its environment:

```sh
# From the repository root, with ESP-IDF's export.sh already sourced:
idf.py -C firmware build
# Connect the badge to this computer in BOOT/BOOTSEL mode. Choose its actual port:
idf.py -C firmware -p /dev/ttyACM0 flash
```

Release BOOT and tap RESET if the application does not start. Native USB can
disappear during reset/deep sleep. `idf.py -C firmware -p /dev/ttyACM0 monitor`
can capture logs while awake. Do not publish binaries, logs, build directories or
screenshots containing credentials or personal data. Wi-Fi and device credentials
are compiled into the binary. `.gitignore` is a guard, not a security boundary.

## Behavior

- Samples the calibrated GPIO14 regulator-input divider before Wi-Fi; ADC2 is
  released before connecting. Invalid readings are reported as unknown.
- Joins WPA2-or-better Wi-Fi with bounded retries and a 25-second wait.
- On a cold boot, synchronizes time via `NTP_SERVER` (default `pool.ntp.org`) with
  a 10-second wait. Failed time setup preserves the screen and sleeps. RTC time
  normally survives deep sleep.
- Uses HTTPS with the provisioned CA, hostname/IP verification and certificate
  validity checks. No insecure TLS fallback or HTTP redirect is permitted.
- Sends its bearer credential and input voltage with the manifest request.
  If telemetry receives 429, retries the manifest once without telemetry.
- Validates dimensions, expected length, frame path, SHA-256 and disjoint color
  planes before displaying. The frame is fetched into PSRAM. A SHA-256 check is
  an integrity check; TLS provides server authentication.
- Shuts Wi-Fi off before the full panel refresh. Unchanged frames are skipped;
  failures keep the existing screen. Panel shutdown, LEDs off, GPIO holds and
  deep sleep follow the refresh. Normal refresh interval is one hour.
- GPIO16 (B) and GPIO15 (upstream Menu+) can wake it. Held buttons are omitted from
  wake sources to prevent loops. With one page these act as manual update checks.

Panel pins: CS 13/3, BUSY 12/8, DC 10, RESET 11, SCK 21 and MOSI 47. Status LED is
active-low GPIO1; eight addressable LEDs use GPIO38. GPIO14 measures half the
regulator input, so USB voltage must not be treated as battery state of charge.

`TEST_SLEEP_SECONDS` must be zero for normal operation. Do not infer battery
runtime from the ESP32 chip's deep-sleep specification alone; board peripherals
also consume power. There is no hardware-equivalent battery disconnect here.

Secure boot and flash encryption are disabled; physical flash access can expose
credentials. Enabling these involves irreversible provisioning decisions and is
outside this project's setup workflow. NTP is not authenticated; a time attack
can interrupt service or influence expiry checks, but does not supply a valid
server certificate under the project's private CA.

## Additional devices

Generate a fresh high-entropy bearer token for each device and add its SHA-256
hex digest under a distinct key in the server's `secrets/auth.json` `devices`
object. Store the plaintext token only in that device's private configuration and
secure provisioning backup. Copy the public CA header, not the server or CA private
keys. Recreate the server after changing its credential file. Add the new device
IP to the generated nginx allowlist and router firewall as appropriate.

The current server has one shared rendered dashboard and battery sample, so a
second token provides independent revocation, not yet a personalized dashboard.
