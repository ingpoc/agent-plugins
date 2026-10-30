import AppKit
import CoreAudio
import XCTest
@testable import CUAService

/// Now Playing (A1) with a fake source only — nothing here talks to MediaRemoteAdapter,
/// starts playback, or touches the output device/volume.
@MainActor
final class FakeNowPlayingSource: NowPlayingSource {
    let name = "fake"
    var onUpdate: ((NowPlayingInfo?) -> Void)?
    private(set) var sent: [NowPlayingCommand] = []
    private(set) var started = false
    func start() { started = true }
    func stop() { started = false }
    func send(_ command: NowPlayingCommand) { sent.append(command) }
    func emit(_ i: NowPlayingInfo?) { onUpdate?(i) }
}

@MainActor
final class NowPlayingTests: XCTestCase {
    private var suite = ""
    private var defaults: UserDefaults!
    private var store: IslandSettingsStore!

    override func setUp() {
        suite = "cua.island.np.test.\(UUID().uuidString)"
        defaults = TestDefaults.make(suite)
        store = IslandSettingsStore(defaults: defaults, tokens: MemoryTokenStore())
    }

    override func tearDown() { defaults.removePersistentDomain(forName: suite) }

    static func track(_ title: String = "Vibrations", playing: Bool = true, bundle: String = NowPlayingRules.musicBundle,
                      elapsed: Double = 30, duration: Double = 200, at: Date = Date()) -> NowPlayingInfo {
        var i = NowPlayingInfo()
        i.bundleID = bundle; i.title = title; i.artist = "Satorio"; i.album = "Tibetan Singing Bowls"
        i.duration = duration; i.elapsed = elapsed; i.timestamp = at; i.playing = playing; i.rate = playing ? 1 : 0
        return i
    }

    // MARK: Parser

    func testAdapterDiffMergeAndNullRemoval() throws {
        var payload: [String: Any] = [:]
        var cache: (hash: Int, image: NSImage?) = (0, nil)
        let full = #"{"type":"data","diff":false,"payload":{"bundleIdentifier":"com.apple.Music","title":"A","artist":"X","playing":true,"duration":100,"elapsedTime":10,"timestamp":"2026-10-20T16:00:00.000Z","shuffleMode":3,"repeatMode":1}}"#
        XCTAssertTrue(NowPlayingParser.merge(line: Data(full.utf8), into: &payload))
        var i = try XCTUnwrap(NowPlayingParser.info(from: payload, artworkCache: &cache))
        XCTAssertEqual(i.title, "A"); XCTAssertTrue(i.playing); XCTAssertTrue(i.shuffleOn); XCTAssertFalse(i.repeatOn)
        XCTAssertEqual(i.timestamp.timeIntervalSince1970, 1_792_512_000, accuracy: 1)
        let diff = #"{"type":"data","diff":true,"payload":{"playing":false,"artist":null}}"#
        XCTAssertTrue(NowPlayingParser.merge(line: Data(diff.utf8), into: &payload))
        i = try XCTUnwrap(NowPlayingParser.info(from: payload, artworkCache: &cache))
        XCTAssertFalse(i.playing); XCTAssertEqual(i.artist, ""); XCTAssertEqual(i.title, "A", "diff keeps other keys")
        XCTAssertFalse(NowPlayingParser.merge(line: Data(#"{"type":"other"}"#.utf8), into: &payload))
        payload["title"] = nil
        XCTAssertNil(NowPlayingParser.info(from: payload, artworkCache: &cache), "no title → nothing playing")
    }

    func testPartialPayloadIsNotAStop() {
        XCTAssertTrue(NowPlayingParser.isCleared([:]))
        XCTAssertTrue(NowPlayingParser.isCleared(["playing": false]))
        XCTAssertFalse(NowPlayingParser.isCleared(["bundleIdentifier": "com.apple.Music", "playing": true]),
                       "app known but title not yet merged: ignore, don't hide")
    }

    func testSkipWhilePausedRestartsIdleWindow() {
        let (c, _) = controller()
        let t0 = Date()
        c.ingest(Self.track(), now: t0)
        c.ingest(Self.track(playing: false), now: t0.addingTimeInterval(1))
        c.ingest(Self.track("Next one", playing: false), now: t0.addingTimeInterval(3))
        c.recompute(now: t0.addingTimeInterval(5))
        XCTAssertTrue(c.visible, "new track while paused shows for a fresh idle window")
        c.recompute(now: t0.addingTimeInterval(6.1))
        XCTAssertFalse(c.visible)
    }

    func testCommandsMapToAdapterSendIDs() {
        XCTAssertEqual(NowPlayingCommand.togglePlayPause.adapterArgs, ["send", "2"])
        XCTAssertEqual(NowPlayingCommand.next.adapterArgs, ["send", "4"])
        XCTAssertEqual(NowPlayingCommand.previous.adapterArgs, ["send", "5"])
        XCTAssertEqual(NowPlayingCommand.toggleShuffle.adapterArgs, ["send", "6"])
        XCTAssertEqual(NowPlayingCommand.toggleRepeat.adapterArgs, ["send", "7"])
        XCTAssertEqual(NowPlayingCommand.seek(12.5).adapterArgs, ["seek", "12500000"])
    }

    // MARK: Rules

    func testTitleExtrasStripper() {
        XCTAssertEqual(NowPlayingRules.stripExtras("Here Comes the Sun (Remastered 2009)"), "Here Comes the Sun")
        XCTAssertEqual(NowPlayingRules.stripExtras("Song [Explicit]"), "Song")
        XCTAssertEqual(NowPlayingRules.stripExtras("Song - 2011 Remaster"), "Song")
        XCTAssertEqual(NowPlayingRules.stripExtras("Song (feat. Someone)"), "Song")
        XCTAssertEqual(NowPlayingRules.stripExtras("Love (Part 2)"), "Love (Part 2)", "meaningful parentheses stay")
        XCTAssertEqual(NowPlayingRules.stripExtras("(Live)"), "(Live)", "never strips to empty")
    }

    func testSourceFilter() {
        let music = Self.track(), spotify = Self.track(bundle: NowPlayingRules.spotifyBundle), safari = Self.track(bundle: "com.apple.Safari")
        XCTAssertTrue([music, spotify, safari].allSatisfy { NowPlayingRules.accepts($0, source: "automatic") })
        XCTAssertTrue(NowPlayingRules.accepts(safari, source: "system"))
        XCTAssertTrue(NowPlayingRules.accepts(music, source: "music")); XCTAssertFalse(NowPlayingRules.accepts(spotify, source: "music"))
        XCTAssertTrue(NowPlayingRules.accepts(spotify, source: "spotify")); XCTAssertFalse(NowPlayingRules.accepts(music, source: "spotify"))
    }

    func testElapsedExtrapolationAndClock() {
        let t0 = Date()
        let i = Self.track(elapsed: 10, duration: 100, at: t0)
        XCTAssertEqual(i.elapsed(at: t0.addingTimeInterval(5)), 15, accuracy: 0.01)
        XCTAssertEqual(i.elapsed(at: t0.addingTimeInterval(500)), 100, "clamped to duration")
        XCTAssertEqual(Self.track(playing: false, elapsed: 10, at: t0).elapsed(at: t0.addingTimeInterval(5)), 10)
        XCTAssertEqual(NowPlayingRules.clock(65), "1:05"); XCTAssertEqual(NowPlayingRules.clock(3725), "1:02:05")
    }

    func testDeviceGlyphs() {
        XCTAssertEqual(OutputDeviceGlyph.symbol(name: "My AirPods Pro", transport: 0), "airpodspro")
        XCTAssertEqual(OutputDeviceGlyph.symbol(name: "AirPods Max", transport: 0), "airpodsmax")
        XCTAssertEqual(OutputDeviceGlyph.symbol(name: "Mac mini Speakers", transport: kAudioDeviceTransportTypeBuiltIn), "hifispeaker.fill")
        XCTAssertEqual(OutputDeviceGlyph.symbol(name: "LG", transport: kAudioDeviceTransportTypeHDMI), "tv")
    }

    // MARK: Controller (fake source)

    private func controller(front: String? = "com.apple.finder") -> (NowPlayingController, FakeNowPlayingSource) {
        let c = NowPlayingController(store: store)
        c.frontmost = { front }
        let f = FakeNowPlayingSource()
        c.use(f, reason: "test")
        return (c, f)
    }

    func testIdleDurationCollapseAfterPause() {
        let (c, _) = controller()
        let t0 = Date()
        c.ingest(Self.track(), now: t0)
        XCTAssertTrue(c.visible)
        c.ingest(Self.track(playing: false), now: t0.addingTimeInterval(10))
        XCTAssertTrue(c.visible, "paused: still shown during the idle duration")
        c.recompute(now: t0.addingTimeInterval(12.9))
        XCTAssertTrue(c.visible)
        c.recompute(now: t0.addingTimeInterval(13.1))
        XCTAssertFalse(c.visible, "default 3 s idle duration")
        c.ingest(Self.track(), now: t0.addingTimeInterval(20))
        XCTAssertTrue(c.visible, "resumes")
        store.set(0.0, for: NowPlayingKeys.idleDuration)
        c.ingest(Self.track(playing: false), now: t0.addingTimeInterval(30))
        XCTAssertFalse(c.visible, "0 s collapses at once")
    }

    func testPausedAtLaunchStaysHidden() {
        let (c, _) = controller()
        c.ingest(Self.track(playing: false))
        XCTAssertFalse(c.visible, "never pops up for a track that was already paused")
    }

    func testHideWhileSourceActiveAndMasterToggle() {
        let (c, _) = controller(front: NowPlayingRules.musicBundle)
        c.ingest(Self.track())
        XCTAssertTrue(c.visible, "default off")
        store.set(true, for: NowPlayingKeys.hideWhileSourceActive)
        c.recompute()
        XCTAssertFalse(c.visible, "Music is frontmost")
        c.frontmost = { "com.apple.finder" }
        c.recompute()
        XCTAssertTrue(c.visible)
        store.set(false, for: NowPlayingKeys.enabled)
        c.applySettings()
        XCTAssertFalse(c.visible)
        XCTAssertNil(c.info)
    }

    func testSettingsFilterAndStripApplied() {
        let (c, _) = controller()
        store.set(false, for: NowPlayingKeys.automaticSource)
        store.set("spotify", for: NowPlayingKeys.app)
        c.ingest(Self.track())
        XCTAssertFalse(c.visible, "Music filtered out when the app is Spotify")
        store.set("music", for: NowPlayingKeys.app)
        store.set(true, for: NowPlayingKeys.hideTitleExtras)
        c.ingest(Self.track("Song (Remastered 2009)"))
        XCTAssertEqual(c.info?.title, "Song")
    }

    func testCommandsOnlyFromPerform() {
        let (c, f) = controller()
        c.ingest(Self.track())
        XCTAssertEqual(f.sent, [], "never sends commands by itself (no auto-play)")
        c.perform(.togglePlayPause)
        XCTAssertEqual(f.sent, [.togglePlayPause])
    }

    func testSettingsDefaultsAndTabLive() {
        XCTAssertTrue(store.bool(NowPlayingKeys.enabled))
        XCTAssertEqual(store.double(NowPlayingKeys.idleDuration), 3)
        XCTAssertEqual(store.string(NowPlayingKeys.color), "mono")
        XCTAssertEqual(store.string(NowPlayingKeys.action), "favorite", "shuffle was cut")
        XCTAssertTrue(store.bool(NowPlayingKeys.automaticSource))
        let keys = Set(IslandSettingsSchema.defs(for: .nowPlaying).map(\.key))
        XCTAssertFalse(keys.contains("island.nowPlaying.badge") || keys.contains("island.nowPlaying.liveWaveform"),
                       "the unbuilt badge and live waveform rows are gone")
    }

    // MARK: Model precedence (Duo-ready)

    func testPrecedenceMusicVsSamanthaAndAgents() {
        let t0 = Date()
        func p(_ k: String, media: Bool = true, approval: IslandAgentApproval? = nil) -> IslandPresentation {
            IslandModel.presentation(kind: k, stepTotal: 0, approval: approval, since: t0, now: t0.addingTimeInterval(100), media: media)
        }
        XCTAssertEqual(p("idle"), .nowPlaying)
        XCTAssertEqual(p("listening"), .nowPlaying)
        XCTAssertEqual(p("done"), .nowPlaying, "settled done yields to music")
        XCTAssertEqual(p("thinking"), .thinking)
        XCTAssertEqual(p("speaking"), .speaking)
        XCTAssertEqual(p("acting"), .acting)
        XCTAssertEqual(p("confirm"), .needsYou, "Samantha needs-you beats music")
        XCTAssertEqual(p("error"), .error)
        XCTAssertEqual(p("idle", approval: IslandAgentApproval(id: "a", title: "Ok?")), .agentApproval, "agent approval beats music")
        XCTAssertEqual(p("idle", media: false), .idle)
        XCTAssertEqual(IslandPresentation.nowPlaying.compactWidth, 74, "music uses the compact activity width")
    }

    func testModelMusicReturnsAfterApprovalAndCardOpens() async throws {
        let c = IslandController(gatewayPort: 1, store: store, hubPort: nil)
        c.start()
        defer { c.shutdown() }
        let fake = FakeNowPlayingSource()
        c.nowPlaying.frontmost = { "com.apple.finder" }
        c.nowPlaying.use(fake)
        fake.emit(Self.track())
        let m = c.debugModel
        XCTAssertEqual(m.presentation, .nowPlaying)
        XCTAssertFalse(m.idleHidden)
        try store.rotateToken(.cursor)
        var r = IslandHub.Request(method: "POST", path: "/api/island/publish",
                                  body: try JSONSerialization.data(withJSONObject: ["source": "cursor", "id": "x", "kind": "approval", "title": "Run?"]))
        r.peerVerified = true
        XCTAssertEqual(c.hub.handle(r, port: 0).status, 200)
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(m.presentation, .agentApproval)
        c.hub.resolveApproval(id: IslandHub.key(.cursor, "x"), approved: true)
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(m.presentation, .nowPlaying, "music returns after the approval")
        m.toggleCard()
        XCTAssertTrue(m.cardOpen, "click opens the media card")
        XCTAssertEqual(m.card.art, .artwork)
        c.hub.ingestSamantha(IslandSnapshot(kind: "idle"))
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertTrue(m.cardOpen, "Samantha idle refresh keeps the open media card")
        m.onMedia?(.next)
        XCTAssertEqual(fake.sent, [.next])
    }
}
