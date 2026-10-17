# Normal (GENERAL quota) booking

1. Get from the passenger: from/to station codes, date, class, train (or pick from the train list with them), passengers.
2. `python3 book.py FROM TO DD/MM/YYYY GENERAL "AC Chair car (CC)" TRAIN_NO PREFIX` (verified 2026-10-10, ~65 s; ~43 s of it is IRCTC's passenger Continue loader).
3. The script stops on the Pay & Book screen. Hand over to Guru with the train, class, availability and total. It clicks Pay & Book only with `--confirm-pay` after Guru confirms this booking in chat. That click opens the iPay gateway and does not charge. Never complete UPI, Google Pay, or a card. Guru pays himself.
4. Closeout after Guru pays or aborts.

Class labels as IRCTC shows them: `AC 3 Tier (3A)`, `AC 2 Tier (2A)`, `AC Chair car (CC)`, `Sleeper (SL)`, `Second Sitting (2S)`.
