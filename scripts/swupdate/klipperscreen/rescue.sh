#!/bin/sh
set -eu
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
