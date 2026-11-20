#!/usr/bin/env bash
# Give this Linux machine the standard sender address on the HaLowLink 2.
#   sudo ./setup-network-linux.sh usb            HaLowLink on USB-C (its USB network interface)
#   sudo ./setup-network-linux.sh eth <ifname>   HaLowLink LAN port -> this Ethernet port
#   ./setup-network-linux.sh show                what is configured now
#   sudo ./setup-network-linux.sh remove         delete the profile
# Uses NetworkManager. Static 192.168.12.50/24, NO default route (internet stays on your
# normal interface), multicast 239.0.0.0/8 routed to the HaLowLink.
# Optional: HALOW_ADDR (default 192.168.12.50/24), HALOW_ROUTER (default 192.168.12.1),
# HALOW_FORCE=1 to use an interface that carries this machine's default route.
set -euo pipefail
ADDR=${HALOW_ADDR:-192.168.12.50/24}
[[ $ADDR == */* ]] || ADDR=$ADDR/24      # NetworkManager reads a bare address as /32
ROUTER=${HALOW_ROUTER:-192.168.12.1}
CON=halow
die() { echo "setup-network: $*" >&2; exit 1; }

case "${1:-}" in
  usb|eth|show|remove) ;;
  *) sed -n '2,10p' "$0"; exit 1 ;;
esac
command -v nmcli >/dev/null || die "NetworkManager (nmcli) not found; see docs/NETWORK.md, 'Linux without NetworkManager'"

find_usb() {   # sets IF to the only USB-attached, non-wireless network interface
  local d n p
  local -a found=()
  for d in /sys/class/net/*; do
    n=${d##*/}
    [[ $n == lo || -e $d/wireless || -e $d/phy80211 ]] && continue
    p=$(readlink -f "$d/device" 2>/dev/null) || continue
    [[ $p == */usb* ]] || continue
    found+=("$n")
  done
  case ${#found[@]} in
    0) die "no USB network interface found; is the HaLowLink plugged in by USB-C and booted (~60 s)?" ;;
    1) IF=${found[0]} ;;
    *) die "several USB network interfaces: ${found[*]}; run: $0 eth <the HaLowLink's interface>  (see: ip -br link)" ;;
  esac
}

case "$1" in
  usb|eth)
    IF=""
    if [[ $1 == usb ]]; then
      find_usb
    else
      IF=${2:-}; [[ -n $IF && -e /sys/class/net/$IF ]] || die "usage: $0 eth <interface>  (see: ip -br link)"
    fi
    # Refuse the machine's uplink: a default route through anything but the HaLowLink.
    gw=$(ip -4 route show default dev "$IF" 2>/dev/null | awk '{print $3; exit}' || true)
    if [[ -n $gw && $gw != "$ROUTER" && ${HALOW_FORCE:-} != 1 ]]; then
      die "$IF carries this machine's default route via $gw; it looks like your network uplink, not the HaLowLink. Use: $0 eth <interface>  (HALOW_FORCE=1 overrides)"
    fi
    echo "HaLowLink interface: $IF (current connection: $(nmcli -g GENERAL.CONNECTION device show "$IF" 2>/dev/null || true))"
    if nmcli connection show "$CON" >/dev/null 2>&1; then nmcli connection delete "$CON" >/dev/null; fi
    nmcli connection add type ethernet ifname "$IF" con-name "$CON" autoconnect yes \
      ipv4.method manual ipv4.addresses "$ADDR" ipv4.never-default yes \
      ipv4.routes "239.0.0.0/8" ipv6.method disabled \
      connection.autoconnect-priority 50 >/dev/null
    nmcli connection up "$CON" >/dev/null
    ip -4 -br addr show "$IF"
    ip route show 239.0.0.0/8
    if ping -c 2 -W 2 "$ROUTER" >/dev/null; then
      echo "HaLowLink $ROUTER answers"
    else
      echo "WARN: $ROUTER does not answer ping yet"
    fi
    ;;
  show)
    nmcli -f NAME,DEVICE,STATE connection show | grep -E "^(NAME|$CON) " || true
    ip -4 -br addr | grep -F "${ROUTER%.*}." || echo "no ${ROUTER%.*}.x address"
    ip route show 239.0.0.0/8
    ;;
  remove)
    if nmcli connection show "$CON" >/dev/null 2>&1; then
      nmcli connection delete "$CON"
    else
      echo "no '$CON' profile; nothing to remove"
    fi
    ;;
esac
