---
name: irctc-train-booking
description: >-
  Use when booking an IRCTC train ticket (normal or Tatkal) via the
  comet-control lease controller: Keychain login, search, saved passenger,
  UPI, stop at the payment page. Requires macOS iPhone Mirroring OCR.
---
# IRCTC train booking

Requires the comet-control plugin's lease controller (`durable_lease_controller.py`). Requires macOS iPhone Mirroring OCR (`scripts/ocr.swift`). Never press `Pay & Book`. Never print, argv, or log Keychain values (`login.py` reads service `irctc` at runtime).

## Setup (once per session)

Copy `scripts/{book.py,login.py,send.sh,otp_watch.py,ocr.swift,preflight.sh}` into `~/irctc/`, `chmod +x send.sh`, `swiftc -O ocr.swift -o ocr` (`/tmp` is refused). iPhone Mirroring open on Messages > Transactions.

## Run

`cd ~/irctc && IRCTC_OTP_FILE=$HOME/irctc/.otp python3 -u book.py FROM TO DD/MM/YYYY QUOTA "CLASS LABEL" TRAIN_NO PASSENGER_PREFIX [--at HH:MM:SS]`

- Normal booking: [references/normal.md](references/normal.md)
- Tatkal booking: [references/tatkal.md](references/tatkal.md)
- A step broke: [references/steps.md](references/steps.md) (selectors, gotchas); fix it there and in `book.py`.

## Closeout

After the passenger pays or aborts: `durable_lease_controller.py closeout --workdir <WORK printed>`, require `verified_absent: true`.
