#!/bin/sh

control_c() {
  if [ -n "${pids}" ]; then
    kill ${pids}
    sleep 1
    kill -9 ${pids} 2>/dev/null
  fi
  if [ -f "/dev/shm/hackrf_sweep" ]; then
    rm -f "/dev/shm/hackrf_sweep"
  fi
  exit
}

trap control_c INT
trap control_c TERM

hackrf_common() {
  mkfifo /dev/shm/hackrf_sweep
  hackrf_sweep -I -a0 -f0:7250 -l 40 -g 56 -r /dev/shm/hackrf_sweep &
  pids=$!
}

hackrf_sweep() {
  hackrf_common
  /usr/share/fosphor_knob/fosphor_knob_hackrf_sweep.py &
  pids="${pids} $!"
}

hackrf_sweep_sponsors() {
  hackrf_common
  /usr/share/fosphor_knob/fosphor_knob_hackrf_sweep_sponsors.py &
  pids="${pids} $!"
}

uhd() {
  /usr/share/fosphor_knob/fosphor_knob_uhd.py &
  pids=$!
}

uhd_sponsors() {
  /usr/share/fosphor_knob/fosphor_knob_uhd_sponsors.py &
  pids=$!
}

bladerf2() {
  /usr/share/fosphor_knob/fosphor_knob_bladerf2.py &
  pids=$!
}

bladerf2_sponsors() {
  /usr/share/fosphor_knob/fosphor_knob_bladerf2_sponsors.py &
  pids=$!
}

pids=""

if [ "$(basename ${0})" = "fosphor_knob_uhd" ]; then
  uhd
elif [ "$(basename ${0})" = "fosphor_knob_uhd_sponsors" ]; then
  uhd_sponsors
elif [ "$(basename ${0})" = "fosphor_knob_hackrf_sweep" ]; then
  hackrf_sweep
elif [ "$(basename ${0})" = "fosphor_knob_hackrf_sweep_sponsors" ]; then
  hackrf_sweep_sponsors
elif [ "$(basename ${0})" = "fosphor_knob_sponsors" ]; then
  if [ -n "$(lsusb -d 2500:0022)" ]; then
    uhd_sponsors
  elif [ -n "$(lsusb -d 2cf0:5250)" ]; then
    bladerf2_sponsors
  elif [ -d "$(lsusb -d 1d50:6089)" ]; then
    hackrf_sweep_sponsors
  fi
else
  if [ -n "$(lsusb -d 2500:0022)" ]; then
    uhd
  elif [ -n "$(lsusb -d 2cf0:5250)" ]; then
    bladerf2
  elif [ -d "$(lsusb -d 1d50:6089)" ]; then
    hackrf_sweep
  fi
fi

/usr/share/fosphor_knob/run.py &
pids="${pids} $!"
sleep 20
wmctrl -F -r "fosphor" -e 0,0,0,0,0
wmctrl -F -a "fosphor" -b add,maximized_vert,maximized_horz
wmctrl -F -a "fosphor"

if [ "$(basename ${0})" = "fosphor_knob_sponsors" ]; then
  wmctrl -F -r "Fosphor Knob Sponsors" -e 0,0,0,160,0
  wmctrl -F -a "Fosphor Knob Sponsors"
elif [ "$(basename ${0})" = "fosphor_knob_hackrf_sweep" ]; then
  #wmctrl -F -r "Fosphor HackRF Sweep" -e 0,0,0,160,0
  #wmctrl -F -a "Fosphor HackRF Sweep"
  true
else
  wmctrl -F -r "Fosphor Knob" -e 0,0,0,160,0
  wmctrl -F -a "Fosphor Knob"
fi
if [ -x '/usr/bin/xtrlock' ]; then
  xtrlock
fi
wait ${pids}
exit 0
