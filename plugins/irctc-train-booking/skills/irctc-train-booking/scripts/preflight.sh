#!/bin/bash
# Tatkal readiness check (run ~10 min before opening). Prints PASS/FAIL per item; exit 1 if any FAIL.
set -euo pipefail
cd ~/irctc || { echo "FAIL ~/irctc missing; fix: copy the scripts per SKILL.md setup"; exit 1; }
F=0; ok(){ echo "PASS $1"; }; bad(){ echo "FAIL $1"; F=1; }
for f in book.py login.py send.sh otp_watch.py ocr; do [ -e "$f" ] || bad "missing ~/irctc/$f"; done
security find-generic-password -s irctc -a username >/dev/null 2>&1 && security find-generic-password -s irctc -a password >/dev/null 2>&1 && ok "Keychain irctc login present" || bad "Keychain irctc login missing"
nc -z -G 5 www.irctc.co.in 443 2>/dev/null && ok "irctc.co.in reachable (TCP 443)" || bad "irctc.co.in not reachable"
P=$(cd ~/.agents/plugins/comet-control && ./scripts/ensure-broker.sh probe --json 2>/dev/null) || P=""
grep -q '"extension_connected": true' <<<"$P" && ok "Comet control connected" || bad "Comet control not connected"
pgrep -x "iPhone Mirroring" >/dev/null || { open -a "iPhone Mirroring" || bad "cannot open iPhone Mirroring"; sleep 6; }
osascript -e 'tell application "iPhone Mirroring" to activate' >/dev/null 2>&1 || bad "iPhone Mirroring would not activate"; sleep 0.8
B=$(osascript -e 'tell application "System Events" to tell process "iPhone Mirroring" to get {position, size} of window 1' 2>/dev/null) || B=""
[ -n "$B" ] && ok "iPhone Mirroring window open at $B" || bad "iPhone Mirroring window not found"
T=""
if [ -n "$B" ]; then
  read -r X Y W H <<<"$(echo $B | tr -d ',')"
  if screencapture -x -R$X,$((Y+54)),$W,180 /tmp/irctc_pf.png 2>/dev/null; then T=$(./ocr /tmp/irctc_pf.png accurate 2>/dev/null) || T=""; fi
fi
grep -q "Transactions" <<<"$T" && ok "Mirroring shows Messages > Transactions (OCR works)" || bad "Mirroring not on Messages > Transactions (phone locked/disconnected?). OCR saw: $(echo $T | cut -c1-80)"
pgrep -fl "book.py" >/dev/null && echo "NOTE book.py already running"
osascript -e 'tell application "Comet" to activate' >/dev/null 2>&1 || echo "NOTE Comet would not activate"
[ $F = 0 ] && echo "ALL READY (login is proven by book.py --at reaching \"search form filled\")" || echo "NOT READY"; exit $F
