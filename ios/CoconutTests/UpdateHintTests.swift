import Foundation
import XCTest
@testable import Coconut

final class UpdateHintTests: XCTestCase {
    @MainActor
    func testAHintEndsAPauseAfterTheMinimum() async {
        let wake = UpdateHintWake()
        let started = ContinuousClock.now
        let pause = Task { await wake.pause(for: .seconds(30)) }
        try? await Task.sleep(for: .milliseconds(50))
        wake.hint()
        await pause.value
        let elapsed = started.duration(to: .now)
        XCTAssertGreaterThanOrEqual(elapsed, UpdateHintWake.minimumPause)
        XCTAssertLessThan(elapsed, .seconds(5))
        XCTAssertEqual(wake.wakes, 1)
    }

    @MainActor
    func testAHintDuringTheFailedRequestEndsTheNextPause() async {
        let wake = UpdateHintWake()
        wake.hint()
        let started = ContinuousClock.now
        await wake.pause(for: .seconds(30))
        XCTAssertLessThan(started.duration(to: .now), .seconds(5))
        XCTAssertEqual(wake.wakes, 1)
    }

    @MainActor
    func testAClearedHintAndNoHintKeepThePause() async {
        let wake = UpdateHintWake()
        wake.hint()
        wake.clear()
        let started = ContinuousClock.now
        await wake.pause(for: .milliseconds(400))
        XCTAssertGreaterThanOrEqual(started.duration(to: .now), .milliseconds(400))
        XCTAssertEqual(wake.wakes, 0)
    }

    @MainActor
    func testACancelledPauseEndsAtOnce() async {
        let wake = UpdateHintWake()
        let started = ContinuousClock.now
        let pause = Task { await wake.pause(for: .seconds(30)) }
        try? await Task.sleep(for: .milliseconds(50))
        pause.cancel()
        await pause.value
        XCTAssertLessThan(started.duration(to: .now), .seconds(5))
        XCTAssertEqual(wake.wakes, 0)
    }

    // The poll loop pauses after each failed read. A hint for a newer cursor gives one read at once. A hint for a cursor
    // that the phone has gives none.
    @MainActor
    func testAHintForANewerCursorGivesOneImmediateFetch() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This fixture changes the Keychain, so it runs only in a debug simulator build.")
        #else
        let savedToken = PairingToken.read()
        let savedCredentials = TunnelCredentials.read()
        let folder = URL.applicationSupportDirectory.appendingPathComponent("Coconut")
        let paths = ["pairing.json", "snapshot.json", "outgoing.json"].map { folder.appendingPathComponent($0) }
        let savedFiles = paths.map { try? Data(contentsOf: $0) }
        for path in paths { try? FileManager.default.removeItem(at: path) }
        let store = AppStore(demo: false)
        defer {
            store.setForeground(false)
            store.disconnect()
            if let savedToken { try? PairingToken.save(savedToken) } else { PairingToken.delete() }
            if let savedCredentials { try? savedCredentials.save() } else { TunnelCredentials.delete() }
            for (path, data) in zip(paths, savedFiles) {
                if let data { try? data.write(to: path, options: .atomic) } else { try? FileManager.default.removeItem(at: path) }
            }
        }

        let secret = Data((0..<32).map { UInt8($0) })
        let mac = FakeMac()
        mac.psk = TunnelKeys.derive(secret, "coconut pairing psk")
        let reads = ReadLog()
        mac.reply = { [unowned mac] request in
            if request.path == "/v1/pair" {
                // The phone connects again with its client token and Noise IK.
                mac.psk = nil
                return FakeMac.Reply(body: Data(#"{"token":"device-token","device_id":"phone"}"#.utf8))
            }
            // Every read fails, so the poll loop pauses longer after each one: 0.5, 1, 2, 4, then 8 seconds.
            if request.path == "/v1/state" || request.path.hasPrefix("/v1/updates") || request.path.hasPrefix("/v1/events") {
                reads.add()
            }
            return FakeMac.Reply(status: 503, body: Data(#"{"error":"Try again."}"#.utf8))
        }
        store.tunnelSocketFactory = mac.factory
        let setup = PairingSetup(relay: mac.endpoint.relay, route: mac.endpoint.route, macKey: mac.endpoint.macKey,
                                 secret: secret, code: "code", expires: .distantFuture, name: "Test Mac")
        let paired = await store.pair(setup: setup)
        XCTAssertTrue(paired, store.error ?? "No error")
        store.setForeground(true)

        // The first read comes from the pairing, before the poll loop. The loop's fourth failed read starts a 4 second
        // pause.
        try await waitUntil { reads.count >= 5 }
        let before = reads.count
        // A hint for the phone's own cursor (0) does not end the pause.
        store.updateHint(cursor: 0)
        try await Task.sleep(for: .milliseconds(500))
        XCTAssertEqual(reads.count, before, "A hint without a newer cursor started a read.")

        let hinted = ContinuousClock.now
        store.updateHint(cursor: 7)
        try await waitUntil(timeout: .seconds(3)) { reads.count > before }
        XCTAssertEqual(reads.count, before + 1)
        XCTAssertLessThan(hinted.duration(to: .now), .milliseconds(2500), "The hint did not end the 4 second pause.")
        // The next pause is 8 seconds, so the one hint gave exactly one read.
        try await Task.sleep(for: .seconds(1))
        XCTAssertEqual(reads.count, before + 1)
        #endif
    }
}

private final class ReadLog: @unchecked Sendable {
    private let lock = NSLock()
    private var reads = 0
    var count: Int { lock.withLock { reads } }
    func add() { lock.withLock { reads += 1 } }
}
