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

exec_bladerf2() {
  /usr/share/fosphor_knob/fosphor_knob_bladerf2.py &
  pids=$!
}

exec_bladerf2_sponsors() {
  /usr/share/fosphor_knob/fosphor_knob_bladerf2_sponsors.py &
  pids=$!
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
