#!/bin/sh
# 70mai-settings UI helper v1; DR2830 CN 1.2.42 only.
# Runs once per boot. Does not change native language / voice parameters.
case "${1:-start}" in
    start)
        mkdir /tmp/70mai-settings-ui-once.lock 2>/dev/null || exit 0
        nohup "$0" apply >/tmp/70mai-settings-ui.log 2>&1 &
        exit 0
        ;;
    apply) ;;
    *) exit 2 ;;
esac
attempt=0
while [ "$attempt" -lt 120 ]; do
    attempt=$((attempt + 1))
    camera_pid=$(pidof main_app)
    case "$camera_pid" in
        ''|*[!0-9]*) sleep 1; continue ;;
    esac
    camera_sum=$(md5sum "/proc/$camera_pid/exe" 2>/dev/null)
    camera_sum=${camera_sum%% *}
    if [ "$camera_sum" != 40c91047536e78e80572363fe2ee8a4b ]; then
        echo 'Unsupported firmware; UI unchanged.'
        exit 1
    fi
    pointer=$(dd if="/proc/$camera_pid/mem" bs=4 skip=6814148 count=1 2>/dev/null | od -An -tu4 | tr -d ' \r\n')
    case "$pointer" in
        27293320) ;;
        27264200) echo 'English UI already applied.'; exit 0 ;;
        *) sleep 1; continue ;;
    esac
    # Resource table 1 (CN forced choice) -> built-in English table 2.
    printf '\310\004\240\001' | dd of="/proc/$camera_pid/mem" bs=4 seek=6814148 count=1 conv=notrunc 2>/dev/null
    pointer=$(dd if="/proc/$camera_pid/mem" bs=4 skip=6814148 count=1 2>/dev/null | od -An -tu4 | tr -d ' \r\n')
    [ "$pointer" = 27264200 ] || exit 1
    echo 'English UI applied; native voice settings unchanged.'
    exit 0
done
echo 'main_app did not become ready; UI unchanged.'
exit 1
