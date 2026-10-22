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

exec_hackrf_common() {
  mkfifo /dev/shm/hackrf_sweep
  hackrf_sweep -I -a0 -f0:7250 -l 40 -g 56 -r /dev/shm/hackrf_sweep &
  pids=$!
}

exec_hackrf_sweep() {
  exec_hackrf_common
  /usr/share/fosphor_knob/fosphor_knob_hackrf_sweep.py &
  pids="${pids} $!"
}

exec_hackrf_sweep_sponsors() {
  exec_hackrf_common
  /usr/share/fosphor_knob/fosphor_knob_hackrf_sweep_sponsors.py &
  pids="${pids} $!"
}

exec_uhd() {
  /usr/share/fosphor_knob/fosphor_knob_uhd.py &
  pids=$!
}

exec_uhd_sponsors() {
  /usr/share/fosphor_knob/fosphor_knob_uhd_sponsors.py &
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
  if [ -x "/usr/share/fosphor_knob/${1}.py" ] && bladerf_wide_supported; then
    printf 'bladeRF: oversampling and bit packing supported, starting %s\n' "${1}"
    "/usr/share/fosphor_knob/${1}.py" &
  else
    printf 'bladeRF: starting %s\n' "${2}"
    "/usr/share/fosphor_knob/${2}.py" &
  fi
  pids=$!
}

exec_bladerf2() {
  start_bladerf2 fosphor_knob_bladerf2_wide fosphor_knob_bladerf2
}

exec_bladerf2_sponsors() {
  start_bladerf2 fosphor_knob_bladerf2_wide_sponsors fosphor_knob_bladerf2_sponsors
}

pids=""

if [ "$(basename "${0}")" = "fosphor_knob_uhd" ]; then
  exec_uhd
elif [ "$(basename "${0}")" = "fosphor_knob_uhd_sponsors" ]; then
  exec_uhd_sponsors
elif [ "$(basename "${0}")" = "fosphor_knob_hackrf_sweep" ]; then
  exec_hackrf_sweep
elif [ "$(basename "${0}")" = "fosphor_knob_hackrf_sweep_sponsors" ]; then
  exec_hackrf_sweep_sponsors
elif [ "$(basename "${0}")" = "fosphor_knob_sponsors" ]; then
  # XXX rewrite these lsusb to be correct for busybox too
  if [ -n "$(lsusb -d 2500:0022)" ]; then
    exec_uhd_sponsors
  elif [ -n "$(lsusb -d 2cf0:5250)" ]; then
    exec_bladerf2_sponsors
  elif [ -n "$(lsusb -d 1d50:6089)" ]; then
    exec_hackrf_sweep_sponsors
  else
    printf 'Failed to autodetect supported hardware\n'
    exit 1
  fi
else
  if [ -n "$(lsusb -d 2500:0022)" ]; then
    exec_uhd
  elif [ -n "$(lsusb -d 2cf0:5250)" ]; then
    exec_bladerf2
  elif [ -n "$(lsusb -d 1d50:6089)" ]; then
    exec_hackrf_sweep
  else
    printf 'Failed to autodetect supported hardware\n'
    exit 1
  fi
fi

/usr/share/fosphor_knob/run.py &
pids="${pids} $!"
sleep 20
wmctrl -F -r "fosphor" -e 0,0,0,0,0
wmctrl -F -a "fosphor" -b add,maximized_vert,maximized_horz
wmctrl -F -a "fosphor"

wmctrl -r "Fosphor Knob" -e 0,0,0,320,0
wmctrl -a "Fosphor Knob"

# We are using xtrlock for security, inhibit the screensaver
/usr/share/fosphor_knob/nosleep.sh &
pids="${pids} $!"

if [ -x '/usr/bin/xtrlock' ]; then
  xtrlock
fi
# shellcheck disable=2086
wait ${pids}
exit 0
