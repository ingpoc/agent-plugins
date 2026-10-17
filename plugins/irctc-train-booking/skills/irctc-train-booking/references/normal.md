# Normal (GENERAL quota) booking

1. Get from the passenger: from/to station codes, date, class, train (or pick from the train list with them), passengers.
2. `python3 book.py FROM TO DD/MM/YYYY GENERAL "AC Chair car (CC)" TRAIN_NO PREFIX` (verified 2026-10-10, ~65 s; ~43 s of it is IRCTC's passenger Continue loader).
3. Screenshot the payment page, tell the passenger the train, class, availability and total, and ask them to pay (BHIM/UPI).
4. Closeout after they pay or abort.

Class labels as IRCTC shows them: `AC 3 Tier (3A)`, `AC 2 Tier (2A)`, `AC Chair car (CC)`, `Sleeper (SL)`, `Second Sitting (2S)`.
