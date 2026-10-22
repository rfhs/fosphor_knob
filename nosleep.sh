#!/bin/sh
# Move the mouse to a random spot every 58 s so the screen never blanks.
# POSIX sh has no $RANDOM; awk's rand() is POSIX.
while true; do
  awk 'BEGIN { srand(); print int(1 + rand() * 1920), int(1 + rand() * 1080) }' |
    { read -r x y; DISPLAY=:0 xdotool mousemove "$x" "$y"; }
  sleep 58
done
