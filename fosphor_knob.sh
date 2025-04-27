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

if [ "$(basename ${0})" = "fosphor_knob_sponsors" ]; then
  /usr/share/fosphor_knob/fosphor_knob_sponsors.py &
  pids=$!
elif [ "$(basename ${0})" = "fosphor_knob_hackrf_sweep" ]; then
  mkfifo /dev/shm/hackrf_sweep
  hackrf_sweep -I -a0 -f0:7250 -l 40 -g 56 -r /dev/shm/hackrf_sweep &
  pids=$!
  /usr/share/fosphor_knob/fosphor_knob_hackrf_sweep.py &
  pids="${pids} $!"
elif [ "$(basename ${0})" = "fosphor_knob_hackrf_sweep_sponsors" ]; then
  mkfifo /dev/shm/hackrf_sweep
  hackrf_sweep -I -a0 -f0:7250 -l 40 -g 56 -r /dev/shm/hackrf_sweep &
  pids=$!
  /usr/share/fosphor_knob/fosphor_knob_hackrf_sweep_sponsors.py &
  pids="${pids} $!"
else
  /usr/share/fosphor_knob/fosphor_knob.py &
  pids=$!
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
wait ${pids}
exit 0
