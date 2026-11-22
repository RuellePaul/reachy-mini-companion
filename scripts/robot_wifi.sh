#!/usr/bin/env bash
# The robot's Wi-Fi held steady for a body that streams its pose. NetworkManager
# has it scan every channel in the background whenever its signal is weak, and roam between the radios of a box that
# gives both bands one name: each scan and each roam that fails is seconds without the Mac. `lock` (the default) ties
# its connection to the access point it is on (NetworkManager then asks for no background scan and never roams) and
# turns the Wi-Fi's power save off; `unlock` puts both back as they were; `status` says where it stands.
# Locked, the robot joins that access point alone under this network's name: `unlock` before changing the box.
# A lock that loses the gateway takes itself back. Usage: scripts/robot_wifi.sh [lock|unlock|status] [user@robot]
set -euo pipefail

ACTION="${1:-lock}"
ROBOT="${2:-${ROBOT:-pollen@reachy-mini.local}}"
SSH=(ssh -o ConnectTimeout=5 -o BatchMode=yes)
TMP="${ROBOT_WIFI_TMP:-/tmp}" # on the robot
LOG=$TMP/robot_wifi.log

case "$ACTION" in lock | unlock | status) ;; *)
    echo "usage: $0 [lock|unlock|status] [user@robot]" >&2
    exit 2
    ;;
esac

# What runs on the robot. Read-only for `status`; else detached, so that the Wi-Fi going down under this SSH session
# for the few seconds of a reconnection does not stop it half-way.
read -r -d '' REMOTE <<'EOF' || true
set -u
IW=$(command -v iw || echo /usr/sbin/iw)
con=$(nmcli -g GENERAL.CONNECTION device show wlan0)
status() {
    echo "connection: $con"
    echo "locked to:  $(nmcli -g 802-11-wireless.bssid connection show "$con" | tr -d '\\' | sed 's/^$/(none: it scans and roams)/')"
    echo "on:         $("$IW" dev wlan0 link | awk '/Connected to/ {print $3} /signal:/ {print $2, $3} /freq:/ {print $2, "MHz"}' | paste -sd ' ' -)"
    echo "power save: $("$IW" dev wlan0 get power_save | awk '{print $3}')"
    echo "bgscan:     $(sudo -n wpa_cli -i wlan0 get_network 0 bgscan 2>/dev/null | sed 's/^FAIL$/(none)/')"
}
reconnect() { sudo -n nmcli connection up "$con" >/dev/null 2>&1; }
answers() {
    local gateway n
    for n in $(seq 1 25); do
        gateway=$(ip route show default dev wlan0 | awk '{print $3; exit}')
        [ -n "$gateway" ] && ping -c 1 -W 1 "$gateway" >/dev/null 2>&1 && return 0
        sleep 1
    done
    return 1
}
unlock() { sudo -n nmcli connection modify "$con" 802-11-wireless.bssid '' 802-11-wireless.powersave 0 && reconnect; }
case "$1" in
status) status ;;
unlock) unlock && answers && echo 'unlocked: it scans and roams as before' && status ;;
lock)
    bssid=$("$IW" dev wlan0 link | awk '/Connected to/ {print toupper($3)}')
    [ -n "$bssid" ] || { echo 'not connected: nothing changed'; exit 1; }
    sudo -n nmcli connection modify "$con" 802-11-wireless.bssid "$bssid" 802-11-wireless.powersave 2 && reconnect
    if answers; then echo "locked to $bssid" && status; else
        echo 'the gateway no longer answers: taken back'
        unlock
        answers
        status
        exit 1
    fi
    ;;
esac
EOF

if [ "$ACTION" = status ]; then
    "${SSH[@]}" "$ROBOT" "bash -s status" <<<"$REMOTE"
    exit
fi
"${SSH[@]}" "$ROBOT" "cat > $TMP/robot_wifi.sh && { (nohup bash $TMP/robot_wifi.sh $ACTION > $LOG 2>&1; echo \"exit \$?\" >> $LOG) > /dev/null 2>&1 & }" <<<"$REMOTE"
echo "the robot's Wi-Fi reconnects (a few seconds)…"
for _ in $(seq 1 30); do
    sleep 2
    out=$("${SSH[@]}" "$ROBOT" "cat $LOG" 2>/dev/null) || continue
    if grep -q '^exit ' <<<"$out"; then
        sed '/^exit /d' <<<"$out"
        [ "$(grep '^exit ' <<<"$out")" = 'exit 0' ] && exit 0
        exit 1
    fi
done
echo 'the robot does not answer any more: its Wi-Fi took the change back by itself if the gateway was lost' >&2
exit 1
