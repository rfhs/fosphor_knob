#!/bin/sh

#shellcheck disable=2329
control_c() {
  # shellcheck disable=2317
  if [ -n "${pids}" ]; then
    # shellcheck disable=2086
    kill ${pids}
    sleep 1
    # shellcheck disable=2086
    kill -9 ${pids} 2>/dev/null
  fi
  # shellcheck disable=2317
  if [ -f "/dev/shm/hackrf_sweep" ]; then
    rm -f "/dev/shm/hackrf_sweep"
  fi
  # shellcheck disable=2317
  exit
}

trap control_c INT
trap control_c TERM

share="${FOSPHOR_KNOB_SHARE:-/usr/share/fosphor_knob}"

# Whoever installed fosphor_knob may have left out some SDRs' flowgraphs.
installed() {
  [ -x "${share}/${1}.py" ]
}

require_installed() {
  if ! installed "${2}"; then
    printf 'Support for the %s was disabled when fosphor_knob was installed (%s is missing).\n' "${1}" "${share}/${2}.py"
    exit 1
  fi
}

# Every mode but hackrf_sweep is tuned with the knob, so check for a usable
# one before starting anything. hackrf_sweep sweeps the whole range and can't
# be tuned: it runs without the knob and without run.py, and is only started
# by its own command name, never by autodetection.
require_knob() {
  if ! "${share}/run.py" --check; then
    printf 'This mode is tuned with the knob: fix the problem above and start again.\n'
    exit 1
  fi
  use_knob=1
}

exec_hackrf_common() {
  mkfifo /dev/shm/hackrf_sweep
  hackrf_sweep -I -a0 -f0:7250 -l 40 -g 56 -r /dev/shm/hackrf_sweep &
  pids=$!
}

exec_hackrf_sweep() {
  require_installed HackRF fosphor_knob_hackrf_sweep
  exec_hackrf_common
  "${share}/fosphor_knob_hackrf_sweep.py" &
  pids="${pids} $!"
}

exec_hackrf_sweep_sponsors() {
  require_installed HackRF fosphor_knob_hackrf_sweep_sponsors
  exec_hackrf_common
  "${share}/fosphor_knob_hackrf_sweep_sponsors.py" &
  pids="${pids} $!"
}

exec_hackrf() {
  require_installed HackRF fosphor_knob_hackrf
  require_knob
  "${share}/fosphor_knob_hackrf.py" &
  pids=$!
}

exec_hackrf_sponsors() {
  require_installed HackRF fosphor_knob_hackrf_sponsors
  require_knob
  "${share}/fosphor_knob_hackrf_sponsors.py" &
  pids=$!
}

exec_rtlsdr() {
  require_installed RTL-SDR fosphor_knob_rtlsdr
  require_knob
  "${share}/fosphor_knob_rtlsdr.py" &
  pids=$!
}

exec_rtlsdr_sponsors() {
  require_installed RTL-SDR fosphor_knob_rtlsdr_sponsors
  require_knob
  "${share}/fosphor_knob_rtlsdr_sponsors.py" &
  pids=$!
}

exec_uhd() {
  require_installed USRP fosphor_knob_uhd
  require_knob
  "${share}/fosphor_knob_uhd.py" &
  pids=$!
}

exec_uhd_sponsors() {
  require_installed USRP fosphor_knob_uhd_sponsors
  require_knob
  "${share}/fosphor_knob_uhd_sponsors.py" &
  pids=$!
}

# The wide bladeRF flowgraphs sample 122.88 MHz, which needs oversampling
# and bit packing: libbladeRF 2.6.0 (release 2025.10) or later, and a
# gr-osmosdr that passes them on. gr-osmosdr has no version to ask, and an
# older one silently ignores its bladeRF feature= and sample_format=
# arguments, so its library is searched for them instead.
bladerf_wide_supported() {
  python3 - <<'EOF'
import ctypes
import re
import sys


class Version(ctypes.Structure):
    _fields_ = [("major", ctypes.c_uint16), ("minor", ctypes.c_uint16),
                ("patch", ctypes.c_uint16), ("describe", ctypes.c_char_p)]


try:
    libbladerf = ctypes.CDLL("libbladeRF.so.2")
except OSError:
    sys.exit("libbladeRF not found")
version = Version()
libbladerf.bladerf_version(ctypes.byref(version))
if (version.major, version.minor) < (2, 6):
    sys.exit("libbladeRF %s is older than 2.6.0" % version.describe.decode())
try:
    import osmosdr  # noqa: F401  (loads libgnuradio-osmosdr)
except ImportError:
    sys.exit("gr-osmosdr not found")
with open("/proc/self/maps") as maps:
    libraries = set(re.findall(r"/\S*libgnuradio-osmosdr\S*", maps.read()))
for path in libraries:
    with open(path, "rb") as library:
        code = library.read()
    if b"oversample" in code and b"16bit_packed" in code:
        sys.exit(0)
sys.exit("gr-osmosdr is too old for bladeRF oversampling and bit packing")
EOF
}

# Start the wide bladeRF flowgraph ($1) when it can run here, else the
# normal one ($2).
start_bladerf2() {
  if [ -x "${share}/${1}.py" ] && bladerf_wide_supported; then
    printf 'bladeRF: oversampling and bit packing supported, starting %s\n' "${1}"
    "${share}/${1}.py" &
  else
    printf 'bladeRF: starting %s\n' "${2}"
    "${share}/${2}.py" &
  fi
  pids=$!
}

exec_bladerf2() {
  require_installed "bladeRF 2.0" fosphor_knob_bladerf2
  require_knob
  start_bladerf2 fosphor_knob_bladerf2_wide fosphor_knob_bladerf2
}

exec_bladerf2_sponsors() {
  require_installed "bladeRF 2.0" fosphor_knob_bladerf2_sponsors
  require_knob
  start_bladerf2 fosphor_knob_bladerf2_wide_sponsors fosphor_knob_bladerf2_sponsors
}

pids=""
use_knob=0

# XXX rewrite these lsusb to be correct for busybox too
plugged_in() {
  case "${1}" in
    uhd) [ -n "$(lsusb -d 2500:0022)" ] ;;
    bladerf2) [ -n "$(lsusb -d 2cf0:5250)" ] ;;
    hackrf) [ -n "$(lsusb -d 1d50:6089)" ] ;;
    rtlsdr) [ -n "$(lsusb -d 0bda:2838)" ] || [ -n "$(lsusb -d 0bda:2832)" ] ;;
  esac
}

sdr_name() {
  case "${1}" in
    uhd) printf 'USRP' ;;
    bladerf2) printf 'bladeRF 2.0' ;;
    hackrf) printf 'HackRF' ;;
    rtlsdr) printf 'RTL-SDR' ;;
  esac
}

# Start the first SDR plugged in, in this order, whose support is installed;
# $1 is "" or "_sponsors". The hackrf_sweep modes are never autodetected.
autodetect() {
  disabled=0
  for sdr in uhd bladerf2 hackrf rtlsdr; do
    plugged_in "${sdr}" || continue
    if installed "fosphor_knob_${sdr}${1}"; then
      "exec_${sdr}${1}"
      return
    fi
    printf 'Found a %s, but support for it was disabled when fosphor_knob was installed.\n' "$(sdr_name "${sdr}")"
    disabled=1
  done
  if [ "${disabled}" = 1 ]; then
    exit 1
  fi
  no_hardware
}

# A PortaPack running Mayhem's own firmware shows up as 1d50:6018, not as a
# HackRF (1d50:6089), until it is switched to HackRF mode.
no_hardware() {
  printf 'Failed to autodetect supported hardware\n'
  if [ -n "$(lsusb -d 1d50:6018)" ]; then
    printf 'A PortaPack (Mayhem) was found in standalone mode: switch it to HackRF mode on its menu.\n'
  fi
  exit 1
}

if [ "$(basename "${0}")" = "fosphor_knob_uhd" ]; then
  exec_uhd
elif [ "$(basename "${0}")" = "fosphor_knob_uhd_sponsors" ]; then
  exec_uhd_sponsors
elif [ "$(basename "${0}")" = "fosphor_knob_bladerf" ] || [ "$(basename "${0}")" = "fosphor_knob_bladerf2" ]; then
  exec_bladerf2
elif [ "$(basename "${0}")" = "fosphor_knob_bladerf_sponsors" ] || [ "$(basename "${0}")" = "fosphor_knob_bladerf2_sponsors" ]; then
  exec_bladerf2_sponsors
elif [ "$(basename "${0}")" = "fosphor_knob_hackrf" ]; then
  exec_hackrf
elif [ "$(basename "${0}")" = "fosphor_knob_hackrf_sponsors" ]; then
  exec_hackrf_sponsors
elif [ "$(basename "${0}")" = "fosphor_knob_hackrf_sweep" ]; then
  exec_hackrf_sweep
elif [ "$(basename "${0}")" = "fosphor_knob_hackrf_sweep_sponsors" ]; then
  exec_hackrf_sweep_sponsors
elif [ "$(basename "${0}")" = "fosphor_knob_rtlsdr" ]; then
  exec_rtlsdr
elif [ "$(basename "${0}")" = "fosphor_knob_rtlsdr_sponsors" ]; then
  exec_rtlsdr_sponsors
elif [ "$(basename "${0}")" = "fosphor_knob_sponsors" ]; then
  autodetect _sponsors
else
  autodetect ""
fi

if [ "${use_knob}" = 1 ]; then
  "${share}/run.py" &
  pids="${pids} $!"
fi
sleep 20
wmctrl -F -r "fosphor" -e 0,0,0,0,0
wmctrl -F -a "fosphor" -b add,maximized_vert,maximized_horz
wmctrl -F -a "fosphor"

wmctrl -r "Fosphor Knob" -e 0,0,0,320,0
wmctrl -a "Fosphor Knob"

# We are using xtrlock for security, inhibit the screensaver
"${share}/nosleep.sh" &
pids="${pids} $!"

if [ -x '/usr/bin/xtrlock' ]; then
  xtrlock
fi
# shellcheck disable=2086
wait ${pids}
exit 0
