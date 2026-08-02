#!/bin/sh
while true; do DISPLAY=:0 xdotool mousemove $(( 1 + $RANDOM % 1920 )) $(( 1 + $RANDOM % 1080 )); sleep 58; done
