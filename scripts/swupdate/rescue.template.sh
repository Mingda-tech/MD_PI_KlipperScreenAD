#!/bin/sh
set -eu

# Only packages declaring touchesScreen=true and bootRescue=true use this
# SWUpdate stable,rescue selection. The signed installer MUST implement rescue
# as an idempotent restoration of a previously captured SCREEN baseline only.
# It must not flash MCU firmware, start a new install, clear the supervisor's
# maintenance marker or overwrite the firmware/host pair under recovery.
# MINGDA_BOOT_RECOVERY=1 means the boot oneshot may run BEFORE the screen only.
# Restore files ONLY; do not start/stop printer services or synchronously restart
# the screen. Klipper, Linux MCU and Moonraker startup do not depend on this unit.
: "${TMPDIR:?}"
: "${MINGDA_JOB_DIR:?}"
MINGDA_BOOT_RECOVERY=${MINGDA_BOOT_RECOVERY:-0}
case "$MINGDA_BOOT_RECOVERY" in 0|1) ;; *) exit 2 ;; esac
export MINGDA_BOOT_RECOVERY
case "${1:-}" in
    preinst) exit 0 ;;
    postinst) exec /usr/bin/python3 -I "$TMPDIR/installer.py" rescue ;;
    postfailure) exit 1 ;;
    *) exit 2 ;;
esac
