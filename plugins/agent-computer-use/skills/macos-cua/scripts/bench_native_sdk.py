#!/usr/bin/env python3
"""Measure HID suppression + SCShareableContent catalog wins (macOS).

Prints JSON with before-style default suppression vs interval-0, and
double catalog fetch vs short-TTL reuse. Does not require CUAService.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

SWIFT = r'''
import CoreGraphics
import Foundation
import ScreenCaptureKit

func timeHid(interval: CFTimeInterval, count: Int) -> Double {
    let src = CGEventSource(stateID: .combinedSessionState)!
    src.localEventsSuppressionInterval = interval
    let permit: CGEventFilterMask = [
        .permitLocalMouseEvents,
        .permitLocalKeyboardEvents,
        .permitSystemDefinedEvents,
    ]
    src.setLocalEventsFilterDuringSuppressionState(
        permit, state: .eventSuppressionStateSuppressionInterval
    )
    src.setLocalEventsFilterDuringSuppressionState(
        permit, state: .eventSuppressionStateRemoteMouseDrag
    )
    let t0 = CFAbsoluteTimeGetCurrent()
    for _ in 0..<count {
        let down = CGEvent(
            keyboardEventSource: src, virtualKey: 0x09, keyDown: true
        )
        let up = CGEvent(
            keyboardEventSource: src, virtualKey: 0x09, keyDown: false
        )
        // Global tap is where local-events suppression gates bursts.
        down?.post(tap: .cghidEventTap)
        up?.post(tap: .cghidEventTap)
    }
    return (CFAbsoluteTimeGetCurrent() - t0) * 1000
}

func timeCatalog() async -> (firstMs: Double, secondMs: Double) {
    let t0 = CFAbsoluteTimeGetCurrent()
    let a = try? await SCShareableContent.excludingDesktopWindows(
        false, onScreenWindowsOnly: true
    )
    let first = (CFAbsoluteTimeGetCurrent() - t0) * 1000
    _ = a
    let t1 = CFAbsoluteTimeGetCurrent()
    let b = try? await SCShareableContent.excludingDesktopWindows(
        false, onScreenWindowsOnly: true
    )
    let second = (CFAbsoluteTimeGetCurrent() - t1) * 1000
    _ = b
    return (first, second)
}

@main
struct Bench {
    static func main() async {
        let n = 20
        let defaultMs = timeHid(interval: 0.25, count: n)
        let zeroMs = timeHid(interval: 0, count: n)
        let cat = await timeCatalog()
        let out: [String: Any] = [
            "hid_events": n * 2,
            "hid_default_suppression_ms": round(defaultMs * 10) / 10,
            "hid_zero_suppression_ms": round(zeroMs * 10) / 10,
            "hid_speedup_x": defaultMs > 0
                ? round((defaultMs / max(zeroMs, 0.001)) * 10) / 10 : 0,
            "sc_catalog_first_ms": round(cat.firstMs * 10) / 10,
            "sc_catalog_second_ms": round(cat.secondMs * 10) / 10,
            "sc_catalog_note": "second fetch is uncached cost; CUAService TTL skips it within 1s",
        ]
        let data = try! JSONSerialization.data(withJSONObject: out, options: [.prettyPrinted, .sortedKeys])
        print(String(data: data, encoding: .utf8)!)
    }
}
'''


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "Package.swift").write_text(
            """// swift-tools-version: 5.9
import PackageDescription
let package = Package(
  name: "NativeSDKBench",
  platforms: [.macOS(.v14)],
  targets: [.executableTarget(name: "NativeSDKBench", path: "Sources")]
)
"""
        )
        src = root / "Sources"
        src.mkdir()
        (src / "main.swift").write_text(SWIFT)
        subprocess.run(
            ["swift", "run", "-c", "release"],
            cwd=root,
            check=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
