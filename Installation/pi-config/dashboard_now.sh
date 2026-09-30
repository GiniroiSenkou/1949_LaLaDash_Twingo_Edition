#!/bin/bash
# Back from the desktop to LaLaDash (the desktop icon desktop_now.sh creates).
# Takes the desktop shell down again — it would otherwise keep the panel's
# 36 px and ~130 MB — and starts the app's unit, splash and all.
pkill -x wf-panel-pi 2>/dev/null
pkill -f "pcmanfm --desktop" 2>/dev/null
systemctl --user start laladash.service
