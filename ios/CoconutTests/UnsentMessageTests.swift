import XCTest
@testable import Coconut

final class UnsentMessageTests: XCTestCase {
    private func outgoing(_ id: String, _ status: OutgoingMessage.Status, sequence: Int, bot: String = "bot") -> OutgoingMessage {
        OutgoingMessage(id: id, botId: bot, executorIds: [bot],
                        request: MessageRequest(text: id, mode: "queue", taskId: nil, scheduledAt: nil, attachmentIds: [],
                                                recipientBotIds: []),
                        createdAt: "2026-11-02T00:00:00Z", status: status, sequence: sequence)
    }

    @MainActor
    func testOnlyAMessageThatDidNotSendCanBeDeleted() {
        let store = AppStore(demo: true)
        store.outgoingMessages = [outgoing("failed", .failed, sequence: 1), outgoing("rejected", .rejected, sequence: 2),
                                  outgoing("sending", .sending, sequence: 3), outgoing("accepted", .accepted, sequence: 4)]
        // A message that is on its way, or that the Mac took, stays.
        XCTAssertFalse(store.deleteUnsent("sending"))
        XCTAssertFalse(store.deleteUnsent("accepted"))
        XCTAssertTrue(store.deleteUnsent("failed"))
        XCTAssertTrue(store.deleteUnsent("rejected"))
        XCTAssertEqual(store.outgoingMessages.map(\.id), ["sending", "accepted"])
        XCTAssertFalse(store.deleteUnsent("failed"), "Nothing is left to delete.")
    }

    @MainActor
    func testADeletionIsSavedAndLetsTheNextMessageGo() async throws {
        let fixture = try await fixture()
        defer { fixture.cleanup() }
        let store = fixture.store
        let bot = fixture.bot
        store.outgoingMessages = [outgoing("held", .failed, sequence: 1, bot: bot)]
        XCTAssertTrue(store.deleteUnsent("held"))
        // A new launch reads the saved queue, so the message does not come back.
        XCTAssertFalse(AppStore(demo: false).outgoingMessages.contains { $0.id == "held" })

        // A save that fails keeps the message, so the queue on the phone and on disk agree.
        store.outgoingMessages = [outgoing("kept", .failed, sequence: 2, bot: bot)]
        store.writeOutgoing = { _ in throw CocoaError(.fileWriteNoPermission) }
        XCTAssertFalse(store.deleteUnsent("kept"))
        XCTAssertEqual(store.outgoingMessages.map(\.id), ["kept"])
        XCTAssertNotNil(store.error)

        // Without the failed message, the message after it goes to the Mac.
        store.writeOutgoing = { try ProtectedCache.write($0, name: "outgoing.json") }
        store.outgoingMessages = [outgoing("blocking", .failed, sequence: 3, bot: bot), outgoing("next", .sending, sequence: 4, bot: bot)]
        XCTAssertTrue(store.deleteUnsent("blocking"))
        store.setForeground(true)
        try await waitUntil { fixture.server.requests.contains { $0.contains("/messages") && $0.contains("next") } }
        XCTAssertFalse(fixture.server.requests.contains { $0.contains("blocking") })
    }

    private struct Fixture {
        let store: AppStore
        let server: DecisionDeliveryServer
        let bot: String
        let cleanup: () -> Void
    }

    @MainActor
    private func fixture() async throws -> Fixture {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This fixture requires a debug simulator.")
        #else
        let snapshot = try PreviewStateFixture.load()
        let server = try DecisionDeliveryServer(snapshot: snapshot, failFirst: false, reject: false)
        let address = try await server.start()
        let savedToken = PairingToken.read()
        let folder = URL.applicationSupportDirectory.appendingPathComponent(TestIsolation.name("Coconut"))
        let paths = ["pairing.json", "snapshot.json", "outgoing.json"].map { folder.appendingPathComponent($0) }
        let saved = paths.map { try? Data(contentsOf: $0) }
        for path in paths { try? FileManager.default.removeItem(at: path) }
        let variables = ["COCONUT_SERVER_URL": address, "COCONUT_DEVICE_TOKEN": "fixture-only"]
        let previous = ProcessInfo.processInfo.environment
        for (key, value) in variables { setenv(key, value, 1) }
        let store = AppStore(demo: false)
        store.bots = snapshot.bots
        return Fixture(store: store, server: server, bot: try XCTUnwrap(snapshot.bots.first).id) {
            store.setForeground(false)
            store.disconnect()
            server.stop()
            for key in variables.keys {
                if let value = previous[key] { setenv(key, value, 1) } else { unsetenv(key) }
            }
            if let savedToken { try? PairingToken.save(savedToken) } else { PairingToken.delete() }
            for (path, data) in zip(paths, saved) {
                if let data { try? data.write(to: path, options: .atomic) } else { try? FileManager.default.removeItem(at: path) }
            }
        }
        #endif
    }
}
