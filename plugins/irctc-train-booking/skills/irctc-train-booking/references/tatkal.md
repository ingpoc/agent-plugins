# Tatkal booking: rules, prep, day-of steps (official sources, checked 2026-10-10)

## Rules
- Opens 1 day before journey (from train's origin station date): AC 10:00, non-AC (SL/FC/2S) 11:00 IST. [TatkalFaq](https://contents.irctc.co.in/en/TatkalFaq.html), [indianrail](https://www.indianrail.gov.in/tatkal_Scheme.html)
- Only Aadhaar-authenticated IRCTC users can book Tatkal/Premium Tatkal online (since 1 Jul 2025). Aadhaar OTP at booking applies on selected trains (322 as of Dec 2025). [PIB 2135694](https://www.pib.gov.in/PressReleasePage.aspx?PRID=2135694), [PIB 2202197](https://www.pib.gov.in/PressReleasePage.aspx?PRID=2202197), [PIB 2287719](https://www.pib.gov.in/PressReleasePage.aspx?PRID=2287719)
- Agents barred first 30 min (10:00-10:30 AC, 11:00-11:30 non-AC). [PIB 2135694](https://www.pib.gov.in/PressReleasePage.aspx?PRID=2135694)
- Max 4 passengers/PNR; no concessions; one passenger carries original photo ID. [TatkalBooking](https://contents.irctc.co.in/en/TatkalBooking.html)
- Tatkal charge per passenger (min-max): SL 100-200, CC 125-225, 3A 300-400, 2A 400-500, 2S 10-15. [TatkalFaq](https://contents.irctc.co.in/en/TatkalFaq.html)
- Confirmed Tatkal: no refund on cancel. RAC/WL: refund minus Rs60 clerkage if cancelled up to 30 min before departure. [RefundCancellationRules.pdf](https://contents.irctc.co.in/en/RefundCancellationRules.pdf)

## Prep (once)
1. Aadhaar-authenticate the IRCTC account (My Account > Authenticate User). The review page shows "Aadhaar Verified" when that is done.
2. Master List (My Account > My Profile > Add/Modify Master List), max 6; Saved passengers are picked via step 14.
3. Optional IRCTC eWallet (min Rs100, max Rs10,000, Rs10+tax/txn) or iPay Autopay UPI mandate to skip the UPI app hop. [eWallet](https://contents.irctc.co.in/en/About_IRCTC_eWallet.pdf), [Autopay](https://contents.irctc.co.in/en/autopay_english.pdf)
4. Password-manager extensions off in Comet; Google Pay ready on phone.

## Intake (every Tatkal ask)
Ask the passenger: from, to, train, class, travel date. Tatkal day = travel date - 1 (AC 10:00, non-AC 11:00). Set the two Mac Reminders and a one-off routine for 09:47 on Tatkal day (preflight + launch with `--at`, readiness message to the passenger by 09:55).

## Day-of steps
1. Day before: confirm train, class, passenger with the passenger; check prep above. Add two Mac Reminders (osascript `tell application "Reminders" to make new reminder with properties {name:..., remind me date:d}`) at opening-30 min and opening-5 min.
2. 09:47 IST (one-off routine): `~/irctc/preflight.sh` (files, Keychain, IRCTC TCP, Comet, iPhone Mirroring + OCR on Messages); fix or alert the passenger. Then then `python3 book.py FROM TO DD/MM/YYYY TATKAL "AC 3 Tier (3A)" TRAIN_NO PREFIX --at 10:00:00` (use 11:00:00 for non-AC). It logs in and fills the search, then waits.
   Run with `IRCTC_OTP_FILE=$HOME/irctc/.otp` and as a background Shell (block 0) so chat messages can't kill it.
3. 10:00:00 it searches and runs to review (~65 s, ~45 s is IRCTC loaders), ticks Aadhaar consent and clicks Generate OTP, then waits for the OTP file.
4. `otp_watch.py` (started by book.py just before Generate OTP) OCRs the mirrored iPhone and writes the new ADHAAR-S OTP the moment it lands; book.py types it, continues, picks IRCTC iPay New, Pay & Book, stops on the gateway (UPI + Pay Now). Clean test 2026-10-11: 78 s fresh window to gateway, OTP in ~6 s, zero manual steps.
5. Ping the passenger to pay. If WL/REGRET, report and offer the next train. Never close the window.
