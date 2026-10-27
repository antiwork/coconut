import Foundation
import XCTest
@testable import Coconut

final class TunnelRepairTests: XCTestCase {
    @MainActor
    func testRefusedPairingStopsAutomaticPollingUntilThePhonePairsAgain() async throws {
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
        mac.reply = { [unowned mac] request in
            guard request.path == "/v1/pair" else { return FakeMac.Reply(status: 500) }
            // The pairing works, then the relay refuses the client token: the Mac removed this phone.
            mac.handshake = .relayError("revoked")
            mac.psk = nil
            return FakeMac.Reply(body: Data(#"{"token":"device-token","device_id":"phone"}"#.utf8))
        }
        store.tunnelSocketFactory = mac.factory
        let setup = PairingSetup(relay: mac.endpoint.relay, route: mac.endpoint.route, macKey: mac.endpoint.macKey,
                                 secret: secret, code: "code", expires: .distantFuture, name: "Test Mac")

        let paired = await store.pair(setup: setup)
        XCTAssertTrue(paired, store.error ?? "No error")
        XCTAssertEqual(store.connection, .needsRepair)
        XCTAssertTrue(store.repairRequired)
        let sockets = mac.socketCount

        // A return to the foreground and a network recovery must not ask the relay again.
        store.setForeground(true)
        store.networkChanged(available: false)
        store.networkChanged(available: true)
        try await Task.sleep(for: .milliseconds(500))
        XCTAssertEqual(mac.socketCount, sockets, "Automatic polling used the rejected pairing.")
        XCTAssertEqual(store.connection, .needsRepair)

        // Reconnect is the person's choice, so it tries once more.
        await store.reconnect()
        XCTAssertEqual(mac.socketCount, sockets + 1)
        XCTAssertTrue(store.repairRequired)
        #endif
    }

    // A closed pairing screen cancels its pairing. A Mac that answers after that must not replace the saved Mac.
    @MainActor
    func testACancelledPairingSavesNothing() async throws {
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
        let pairing = PendingTask()
        mac.reply = { request in
            guard request.path == "/v1/pair" else { return FakeMac.Reply(status: 500) }
            // The screen closes while the Mac answers.
            pairing.task?.cancel()
            return FakeMac.Reply(body: Data(#"{"token":"device-token","device_id":"phone"}"#.utf8))
        }
        store.tunnelSocketFactory = mac.factory
        let setup = PairingSetup(relay: mac.endpoint.relay, route: mac.endpoint.route, macKey: mac.endpoint.macKey,
                                 secret: secret, code: "code", expires: .distantFuture, name: "Test Mac")

        let task = Task { await store.pair(setup: setup) }
        pairing.task = task
        let paired = await task.value
        XCTAssertFalse(paired)
        XCTAssertFalse(store.hasPairing)
        XCTAssertNil(store.error)
        XCTAssertNil(PairingToken.read())
        #endif
    }
}

private final class PendingTask: @unchecked Sendable {
    var task: Task<Bool, Never>?
}

