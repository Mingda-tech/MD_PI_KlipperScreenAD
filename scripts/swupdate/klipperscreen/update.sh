#!/bin/sh
set -eu
: "${TMPDIR:?SWUpdate private workspace is required}"
: "${MINGDA_JOB_DIR:?Persistent task directory is required}"
case "${1:-}" in
    preinst) exec /usr/bin/python3 -I "$TMPDIR/installer.py" check ;;
    postinst)
        case "${MINGDA_UPDATE_ACTION:-install}" in
            install|start) action=install ;;
            recover) action=recover ;;
            *) exit 2 ;;
        esac
        /usr/bin/python3 -I "$TMPDIR/installer.py" "$action"
        exec /usr/bin/python3 -I "$TMPDIR/installer.py" verify
        ;;
    postfailure) exec /usr/bin/python3 -I "$TMPDIR/installer.py" failure ;;
    *) exit 2 ;;
esac
