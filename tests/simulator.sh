#!/bin/bash
# Try a knob with no SDR: tests/fake_flowgraph.py stands in for the fosphor
# flowgraph and run.py drives it with the knob. Ctrl-C stops both.
#
#   tests/simulator.sh [--like SDR] [--freq HZ] [--gain DB] [/dev/input/eventN]
#
# --like makes the fake radio report the limits of that SDR (uhd, bladerf2,
# rtlsdr, hackrf), so the knob stops at its edges, skips the presets it can't
# reach and steps through its real gains. The default, fake, is a made-up SDR
# whose edges are 16 clicks above the 2424 MHz preset and 10 below 140 MHz,
# that skips 5160 MHz and whose gains mix 1 dB steps with uneven jumps; none
# reports no limits, like an older flowgraph. --freq and --gain set where the
# fake radio starts; a device picks the knob when more than one is plugged in
# (see tests/knobs.py).

usage() {
  printf 'usage: %s [--like SDR] [--freq HZ] [--gain DB] [/dev/input/eventN]\n' "${0}" >&2
  exit 2
}

repo="$(cd "$(dirname "${0}")/.." && pwd)"
flowgraph_args=()
like=fake
device=""
while [ $# -gt 0 ]; do
  case "${1}" in
    --freq|--gain)
      [ $# -ge 2 ] || usage
      flowgraph_args+=("${1}" "${2}")
      shift 2
      ;;
    --like)
      [ $# -ge 2 ] || usage
      like="${2}"
      shift 2
      ;;
    -*) usage ;;
    *)
      [ -z "${device}" ] || usage
      device="${1}"
      shift
      ;;
  esac
done

port_in_use() {
  (exec 3<>/dev/tcp/127.0.0.1/8080) 2>/dev/null
}

if port_in_use; then
  printf 'Port 8080 is taken: stop fosphor_knob.sh, run.py or another simulator first.\n' >&2
  exit 1
fi
if [ -z "${device}" ] && ! "${repo}/run.py" --check; then
  exit 1
fi

pids=()
cleanup() {
  trap - INT TERM EXIT
  if [ ${#pids[@]} -gt 0 ]; then
    kill "${pids[@]}" 2>/dev/null
    wait "${pids[@]}" 2>/dev/null
  fi
  printf '\nSimulator stopped.\n'
  exit "${1:-0}"
}
trap 'cleanup 0' INT TERM
trap 'cleanup $?' EXIT

# Background jobs of a script ignore SIGINT, so Ctrl-C reaches only this
# shell and cleanup() stops them with SIGTERM.
python3 -u "${repo}/tests/fake_flowgraph.py" --like "${like}" "${flowgraph_args[@]}" &
pids+=($!)
for _ in $(seq 50); do
  port_in_use && break
  kill -0 "${pids[0]}" 2>/dev/null || cleanup 1
  sleep 0.1
done
port_in_use || { printf 'The fake flowgraph did not start.\n' >&2; cleanup 1; }

python3 -u "${repo}/run.py" ${device:+"${device}"} &
pids+=($!)

printf 'Turn to tune, press for the next preset, hold and turn for gain. Ctrl-C stops.\n'
wait -n "${pids[@]}"
printf 'run.py or the fake flowgraph exited.\n' >&2
cleanup 1
