#!/bin/sh
set -eu

# Native SWUpdate shellscript callbacks. All assets have already been verified
# and staged by SWUpdate. Nothing is imported from the installed screen source.
: "${TMPDIR:?SWUpdate must have a private task temporary directory}"
: "${MINGDA_JOB_DIR:?The screen supervisor must supply a persistent task directory}"

case "${1:-}" in
    preinst)
        exec /usr/bin/python3 -I "$TMPDIR/installer.py" check
        ;;
    postinst)
        case "${MINGDA_UPDATE_ACTION:-install}" in
            install|start) action=install ;;
            recover) action=recover ;;
            *) exit 2 ;;
        esac
        /usr/bin/python3 -I "$TMPDIR/installer.py" "$action"
        exec /usr/bin/python3 -I "$TMPDIR/installer.py" verify
        ;;
    postfailure)
        exec /usr/bin/python3 -I "$TMPDIR/installer.py" failure
        ;;
    *) exit 2 ;;
esac
