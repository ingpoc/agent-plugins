# irctc-train-booking

Book an IRCTC ticket through the comet-control lease controller. Login reads the macOS Keychain service irctc at runtime. Aadhaar OTP uses macOS iPhone Mirroring OCR (ocr.swift). Default stops on the Pay & Book screen. `--confirm-pay` opens the iPay gateway and does not charge.

Portable [Agent Plugin](https://agent-plugins.org/specification). Install: this folder's `AGENTS.md`. Collection routing: repo-root `AGENTS.md`.

## Benchmarks

Scale 0–10 unless noted. Context efficiency maps to the rating key `token_efficiency`. Source: unmeasured.

| Axis | Score |
| --- | --- |
| Reliability | unmeasured |
| Robustness | unmeasured |
| Context efficiency | unmeasured |
| Speed | unmeasured |
| Efficiency | unmeasured |

### Reliability

Repeatable pass rate. Spread penalty when p95/p50 duration > 1.5.

### Robustness

Fraction of repeats that stay on the owner path (`measured.robust`).

### Context efficiency

Proof bytes / driver RPCs vs floor (`token_efficiency`). Compact query/diff, no chat dumps.

### Speed

Wall time and per-step latency vs floors.

### Efficiency

Round-trips / AX snapshots vs floor. Prefer one asserted run over N granular calls.

Replace `unmeasured` after a real suite. Do not invent scores.
