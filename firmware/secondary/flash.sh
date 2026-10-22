#!/bin/sh
# Back up and flash the Waveshare knob's second chip (ESP32-U4WDH).
#
# That chip is reached through the knob's CH340 serial adapter, which the
# computer only sees with the USB-C plug in the other way round. Run this
# from an ESP-IDF shell (for esptool.py) after 'idf.py build' in this
# directory, then flip the plug when asked.
#
# The factory firmware is saved once per chip (by MAC address) before the
# first flash; restore it with:
#   esptool.py --chip esp32 -p /dev/ttyUSBn write_flash 0x0 <backup>.bin

set -eu

here=$(cd "$(dirname "$0")" && pwd)
backup_dir=${BACKUP_DIR:-${HOME}/esp/waveshare-knob-factory-backup}
esptool=${ESPTOOL:-esptool.py}

if ! command -v "${esptool}" >/dev/null 2>&1; then
  printf '%s not found: run this from an ESP-IDF shell (. export.sh)\n' "${esptool}"
  exit 1
fi
if [ ! -f "${here}/build/flash_args" ]; then
  printf 'No build yet: run idf.py build in %s first\n' "${here}"
  exit 1
fi

# The knob's CH340 identifies as 1a86:7523.
find_port() {
  for tty in /sys/class/tty/ttyUSB*; do
    [ -e "${tty}" ] || continue
    usbdev=$(readlink -f "${tty}/device/../..")
    if [ "$(cat "${usbdev}/idVendor" 2>/dev/null)" = 1a86 ] &&
       [ "$(cat "${usbdev}/idProduct" 2>/dev/null)" = 7523 ]; then
      printf '/dev/%s\n' "$(basename "${tty}")"
      return 0
    fi
  done
  return 1
}

port=$(find_port || true)
if [ -z "${port}" ]; then
  printf 'Flip the USB-C plug on the knob over now (waiting up to 2 minutes)...\n'
  i=0
  while [ -z "${port}" ] && [ "${i}" -lt 240 ]; do
    sleep 0.5
    i=$((i + 1))
    port=$(find_port || true)
  done
  if [ -z "${port}" ]; then
    printf 'No CH340 (1a86:7523) showed up\n'
    exit 1
  fi
  sleep 1
fi
printf 'Second chip on %s\n' "${port}"

mac=$("${esptool}" --chip esp32 -p "${port}" read_mac | sed -n 's/^MAC: *//p' | head -n 1 | tr -d ':')
if [ -z "${mac}" ]; then
  printf "Could not read the chip's MAC address\n"
  exit 1
fi
mkdir -p "${backup_dir}"
backup="${backup_dir}/second_chip_${mac}_factory_4MB.bin"
if [ -e "${backup}" ]; then
  printf 'Factory backup already saved: %s\n' "${backup}"
else
  printf 'Saving the factory firmware to %s\n' "${backup}"
  "${esptool}" --chip esp32 -p "${port}" -b 460800 read_flash 0 0x400000 "${backup}.part"
  mv "${backup}.part" "${backup}"
  sha256sum "${backup}"
fi

printf 'Flashing the sleep firmware\n'
(cd "${here}/build" && "${esptool}" --chip esp32 -p "${port}" -b 460800 write_flash @flash_args)

if command -v bluetoothctl >/dev/null 2>&1; then
  printf 'Checking that the knob no longer advertises Bluetooth (20 s)...\n'
  # forget what BlueZ remembers, so only a live advertisement counts
  for addr in $(bluetoothctl devices 2>/dev/null | awk '/TAIJI_KNOB/ {print $2}'); do
    bluetoothctl remove "${addr}" >/dev/null 2>&1 || true
  done
  if timeout 25 bluetoothctl --timeout 20 scan on 2>/dev/null | grep -q TAIJI_KNOB; then
    printf 'WARNING: something still advertises as TAIJI_KNOB\n'
  else
    printf 'No TAIJI_KNOB advertisement seen\n'
  fi
fi
printf 'Done: flip the USB-C plug back.\n'
