import AppKit
import AudioToolbox
import CoreAudio
import Foundation

// A2: volume / brightness HUD that REPLACES the system OSD (evidence/p7/hud.md).
// A session event tap swallows the media keys and applies the change itself, so the system never draws
// its OSD. If the tap can't be created (no Accessibility) the HUD stays off — never a duplicate HUD.

// MARK: - CoreAudio default output

enum SystemAudio {
    static func defaultOutput() -> AudioDeviceID? {
        var id = AudioDeviceID(0)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultOutputDevice,
                                              mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        let st = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &id)
        return st == noErr && id != 0 ? id : nil
    }

    private static func addr(_ sel: AudioObjectPropertySelector, output: Bool = true) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(mSelector: sel, mScope: output ? kAudioDevicePropertyScopeOutput : kAudioObjectPropertyScopeGlobal,
                                   mElement: kAudioObjectPropertyElementMain)
    }

    static let volumeSelector = AudioObjectPropertySelector(kAudioHardwareServiceDeviceProperty_VirtualMainVolume)

    static func volume(_ dev: AudioDeviceID) -> Double? {
        var a = addr(volumeSelector)
        guard AudioObjectHasProperty(dev, &a) else { return nil }
        var v = Float32(0)
        var size = UInt32(MemoryLayout<Float32>.size)
        return AudioObjectGetPropertyData(dev, &a, 0, nil, &size, &v) == noErr ? Double(v) : nil
    }

    static func canSetVolume(_ dev: AudioDeviceID) -> Bool {
        var a = addr(volumeSelector)
        guard AudioObjectHasProperty(dev, &a) else { return false }
        var settable: DarwinBoolean = false
        return AudioObjectIsPropertySettable(dev, &a, &settable) == noErr && settable.boolValue
    }

    @discardableResult
    static func setVolume(_ dev: AudioDeviceID, _ v: Double) -> Bool {
        var a = addr(volumeSelector)
        var f = Float32(max(0, min(1, v)))
        return AudioObjectSetPropertyData(dev, &a, 0, nil, UInt32(MemoryLayout<Float32>.size), &f) == noErr
    }

    static func muted(_ dev: AudioDeviceID) -> Bool {
        var a = addr(kAudioDevicePropertyMute)
        guard AudioObjectHasProperty(dev, &a) else { return false }
        var m = UInt32(0)
        var size = UInt32(MemoryLayout<UInt32>.size)
        return AudioObjectGetPropertyData(dev, &a, 0, nil, &size, &m) == noErr && m != 0
    }

    @discardableResult
    static func setMuted(_ dev: AudioDeviceID, _ on: Bool) -> Bool {
        var a = addr(kAudioDevicePropertyMute)
        guard AudioObjectHasProperty(dev, &a) else { return false }
        var m = UInt32(on ? 1 : 0)
        return AudioObjectSetPropertyData(dev, &a, 0, nil, UInt32(MemoryLayout<UInt32>.size), &m) == noErr
    }

    static func name(_ dev: AudioDeviceID) -> String {
        var a = addr(kAudioObjectPropertyName, output: false)
        var cf: Unmanaged<CFString>?
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        guard AudioObjectGetPropertyData(dev, &a, 0, nil, &size, &cf) == noErr, let s = cf?.takeRetainedValue() else { return "" }
        return s as String
    }

    /// SF Symbol for an output (Bluetooth headphones vs speakers vs displays).
    static func symbol(_ dev: AudioDeviceID) -> String {
        var a = addr(kAudioDevicePropertyTransportType, output: false)
        var t = UInt32(0)
        var size = UInt32(MemoryLayout<UInt32>.size)
        _ = AudioObjectGetPropertyData(dev, &a, 0, nil, &size, &t)
        let n = name(dev).lowercased()
        if n.contains("airpods max") { return "airpodsmax" }
        if n.contains("airpods pro") { return "airpodspro" }
        if n.contains("airpods") { return "airpods" }
        switch t {
        case kAudioDeviceTransportTypeBluetooth, kAudioDeviceTransportTypeBluetoothLE: return "headphones"
        case kAudioDeviceTransportTypeHDMI, kAudioDeviceTransportTypeDisplayPort: return "tv"
        case kAudioDeviceTransportTypeAirPlay: return "airplayaudio"
        default: return "hifispeaker.fill"
        }
    }
}

// MARK: - Built-in display brightness (DisplayServices, private; laptops / iMac only)

enum BuiltInBrightness {
    private typealias GetFn = @convention(c) (UInt32, UnsafeMutablePointer<Float>) -> Int32
    private typealias SetFn = @convention(c) (UInt32, Float) -> Int32
    private static let handle = dlopen("/System/Library/PrivateFrameworks/DisplayServices.framework/DisplayServices", RTLD_LAZY)
    private static let getFn: GetFn? = handle.flatMap { dlsym($0, "DisplayServicesGetBrightness") }.map { unsafeBitCast($0, to: GetFn.self) }
    private static let setFn: SetFn? = handle.flatMap { dlsym($0, "DisplayServicesSetBrightness") }.map { unsafeBitCast($0, to: SetFn.self) }

    /// Built-in display under the pointer's screen, else any built-in display.
    static func display() -> CGDirectDisplayID? {
        var ids = [CGDirectDisplayID](repeating: 0, count: 16)
        var n = UInt32(0)
        guard CGGetOnlineDisplayList(16, &ids, &n) == .success else { return nil }
        return ids.prefix(Int(n)).first { CGDisplayIsBuiltin($0) != 0 }
    }

    static var available: Bool { getFn != nil && setFn != nil && display() != nil }

    static func get() -> Double? {
        guard let d = display(), let getFn else { return nil }
        var v = Float(0)
        return getFn(d, &v) == 0 ? Double(v) : nil
    }

    @discardableResult
    static func set(_ v: Double) -> Bool {
        guard let d = display(), let setFn else { return false }
        return setFn(d, Float(max(0, min(1, v)))) == 0
    }
}

// MARK: - Controller

@MainActor
final class IslandHUDController {
    private weak var model: IslandModel?
    private var tap: CFMachPort?
    private var source: CFRunLoopSource?
    private var device: AudioDeviceID?
    private var listenerBlock: AudioObjectPropertyListenerBlock?
    private var systemListener: AudioObjectPropertyListenerBlock?
    private var lastShown: (Double, Bool)?
    /// Settings (Notifications ▸ Sound / Display).
    var volumeEnabled = true
    var brightnessEnabled = true
    /// Output device change → Quick Peek (Connectivity parity).
    var onOutputChanged: ((String, String) -> Void)?
    private(set) var tapActive = false
    private var suppressObserverUntil = Date.distantPast

    init(model: IslandModel) {
        self.model = model
        model.onHUDScroll = { [weak self] kind, up in self?.step(kind, up: up, fine: false) }
    }

    /// Start the key tap (needs Accessibility; silently stays off without it) and the CoreAudio observers.
    func start() {
        installTap()
        bindDevice()
        observeDefaultDevice()
    }

    func stop() {
        axRetry?.invalidate()
        axRetry = nil
        if let tap { CGEvent.tapEnable(tap: tap, enable: false) }
        if let source { CFRunLoopRemoveSource(CFRunLoopGetMain(), source, .commonModes) }
        tap = nil
        source = nil
        tapActive = false
        unbindDevice()
    }

    // MARK: Tap

    private func installTap() {
        guard tap == nil else { return }
        guard AXIsProcessTrusted() else {
            if !loggedNoAX {
                loggedNoAX = true
                SamanthaActivityLog.startup(phase: "island_hud", status: "fail", detail: "no Accessibility: HUD off (system OSD kept)")
            }
            // Accessibility granted later → the tap comes up without a relaunch (cheap 5 s check).
            axRetry?.invalidate()
            axRetry = Timer.scheduledTimer(withTimeInterval: 5, repeats: false) { [weak self] _ in
                Task { @MainActor in self?.installTap() }
            }
            return
        }
        let mask = CGEventMask(1 << 14)   // NX_SYSDEFINED
        let ref = Unmanaged.passUnretained(self).toOpaque()
        guard let t = CGEvent.tapCreate(tap: .cgSessionEventTap, place: .headInsertEventTap, options: .defaultTap,
                                        eventsOfInterest: mask, callback: islandMediaKeyCallback, userInfo: ref) else {
            SamanthaActivityLog.startup(phase: "island_hud", status: "fail", detail: "event tap refused: HUD off (system OSD kept)")
            return
        }
        tap = t
        source = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, t, 0)
        CFRunLoopAddSource(CFRunLoopGetMain(), source, .commonModes)
        CGEvent.tapEnable(tap: t, enable: true)
        tapActive = true
        SamanthaActivityLog.startup(phase: "island_hud", status: "ok", detail: "media-key tap on")
    }

    fileprivate func reenable() {
        if let tap { CGEvent.tapEnable(tap: tap, enable: true) }
    }

    /// Decide on the tap thread (main run loop): true = consumed (system OSD never draws).
    private var consumedDown: Set<Int> = []
    private var loggedNoAX = false
    private var axRetry: Timer?

    fileprivate func handle(code: Int, down: Bool, flags: NSEvent.ModifierFlags) -> Bool {
        let isVolume = MediaKeyLogic.volumeKeys.contains(code)
        let isBrightness = MediaKeyLogic.brightnessKeys.contains(code)
        guard isVolume || isBrightness else { return false }
        // Option alone opens Sound / Displays settings in macOS — leave that to the system.
        if flags.contains(.option) && !flags.contains(.shift) { return false }
        if flags.contains(.command) || flags.contains(.control) { return false }
        if isVolume {
            guard volumeEnabled, let dev = device ?? SystemAudio.defaultOutput(), SystemAudio.canSetVolume(dev) else { return false }
            // Key-up is consumed only when its key-down was (never a half-swallowed pair).
            guard down else { return consumedDown.remove(code) != nil }
            let ok = code == MediaKeyLogic.mute ? toggleMute(dev)
                : step(.volume, up: code == MediaKeyLogic.soundUp, fine: flags.contains(.shift) && flags.contains(.option))
            if ok { consumedDown.insert(code) }
            return ok      // failed change → the system handles the key (its OSD shows; never a silent swallow)
        }
        guard brightnessEnabled, BuiltInBrightness.available else { return false }   // external displays: pass through untouched
        guard down else { return consumedDown.remove(code) != nil }
        let ok = step(.brightness, up: code == MediaKeyLogic.brightnessUp, fine: flags.contains(.shift) && flags.contains(.option))
        if ok { consumedDown.insert(code) }
        return ok
    }

    /// True when the level actually changed hands to us (and the HUD showed).
    @discardableResult
    func step(_ kind: IslandHUDState.Kind, up: Bool, fine: Bool) -> Bool {
        switch kind {
        case .volume:
            guard let dev = device ?? SystemAudio.defaultOutput(), let v = SystemAudio.volume(dev) else { return false }
            let next = MediaKeyLogic.next(v, up: up, fine: fine)
            suppressObserverUntil = Date().addingTimeInterval(0.25)
            if SystemAudio.muted(dev) && up { SystemAudio.setMuted(dev, false) }
            guard SystemAudio.setVolume(dev, next) else { return false }
            show(.init(kind: .volume, value: next, muted: SystemAudio.muted(dev)))
            return true
        case .brightness:
            guard let v = BuiltInBrightness.get() else { return false }
            let next = MediaKeyLogic.next(v, up: up, fine: fine)
            guard BuiltInBrightness.set(next) else { return false }
            show(.init(kind: .brightness, value: next))
            return true
        }
    }

    private func toggleMute(_ dev: AudioDeviceID) -> Bool {
        let m = !SystemAudio.muted(dev)
        suppressObserverUntil = Date().addingTimeInterval(0.25)
        guard SystemAudio.setMuted(dev, m) else { return false }
        show(.init(kind: .volume, value: SystemAudio.volume(dev) ?? 0, muted: m))
        return true
    }

    private func show(_ s: IslandHUDState) {
        lastShown = (s.value, s.muted)
        model?.showHUD(s)
    }

    // MARK: CoreAudio observers (Control Centre / AppleScript changes show the same HUD)

    private func bindDevice() {
        unbindDevice()
        guard let dev = SystemAudio.defaultOutput() else { return }
        device = dev
        let block: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
            Task { @MainActor in self?.deviceLevelChanged() }
        }
        listenerBlock = block
        for sel in [SystemAudio.volumeSelector, kAudioDevicePropertyMute] {
            var a = AudioObjectPropertyAddress(mSelector: sel, mScope: kAudioDevicePropertyScopeOutput, mElement: kAudioObjectPropertyElementMain)
            if AudioObjectHasProperty(dev, &a) { AudioObjectAddPropertyListenerBlock(dev, &a, DispatchQueue.main, block) }
        }
        if let v = SystemAudio.volume(dev) { lastShown = (v, SystemAudio.muted(dev)) }
    }

    private func unbindDevice() {
        guard let dev = device, let block = listenerBlock else { device = nil; return }
        for sel in [SystemAudio.volumeSelector, kAudioDevicePropertyMute] {
            var a = AudioObjectPropertyAddress(mSelector: sel, mScope: kAudioDevicePropertyScopeOutput, mElement: kAudioObjectPropertyElementMain)
            AudioObjectRemovePropertyListenerBlock(dev, &a, DispatchQueue.main, block)
        }
        device = nil
        listenerBlock = nil
    }

    private func deviceLevelChanged() {
        guard volumeEnabled, Date() > suppressObserverUntil, let dev = device, let v = SystemAudio.volume(dev) else { return }
        let m = SystemAudio.muted(dev)
        if let l = lastShown, abs(l.0 - v) < 0.004, l.1 == m { return }
        show(.init(kind: .volume, value: v, muted: m))
    }

    private func observeDefaultDevice() {
        guard systemListener == nil else { return }
        var a = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultOutputDevice,
                                           mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        let block: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
            Task { @MainActor in
                guard let self else { return }
                let old = self.device
                self.bindDevice()
                if let d = self.device, d != old { self.onOutputChanged?(SystemAudio.name(d), SystemAudio.symbol(d)) }
            }
        }
        systemListener = block
        AudioObjectAddPropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &a, DispatchQueue.main, block)
    }
}

/// C callback for the media-key tap (runs on the main run loop).
private func islandMediaKeyCallback(proxy: CGEventTapProxy, type: CGEventType, event: CGEvent, refcon: UnsafeMutableRawPointer?) -> Unmanaged<CGEvent>? {
    guard let refcon else { return Unmanaged.passUnretained(event) }
    let controller = Unmanaged<IslandHUDController>.fromOpaque(refcon).takeUnretainedValue()
    if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
        MainActor.assumeIsolated { controller.reenable() }
        return Unmanaged.passUnretained(event)
    }
    guard type.rawValue == 14, let ns = NSEvent(cgEvent: event), ns.subtype.rawValue == MediaKeyLogic.auxSubtype else {
        return Unmanaged.passUnretained(event)
    }
    let key = MediaKeyLogic.decode(data1: ns.data1)
    let flags = ns.modifierFlags
    let consumed = MainActor.assumeIsolated { controller.handle(code: key.code, down: key.down, flags: flags) }
    return consumed ? nil : Unmanaged.passUnretained(event)
}
