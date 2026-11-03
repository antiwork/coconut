import XCTest
@testable import Coconut

/// The app holds each answer for its Undo window, then sends the same request as before the window.
final class AnswerUndoTests: XCTestCase {
    private struct Fixture {
        let store: AppStore
        let server: DecisionDeliveryServer
        let decision: Decision
        let paths: [URL]
        let cleanup: () -> Void
    }

    @MainActor
    private func fixture() async throws -> Fixture {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This fixture requires a debug simulator.")
        #else
        var snapshot = try PreviewStateFixture.load()
        var decision = try XCTUnwrap(snapshot.decisions.first(where: \.isPending))
        decision.options = []
        snapshot.decisions = [decision]
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
        store.decisions = snapshot.decisions
        return Fixture(store: store, server: server, decision: decision, paths: paths) {
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

    @MainActor
    func testTheAnswerWaitsForItsWindowAndThenSendsTheSameRequestAsBefore() async throws {
        let fixture = try await fixture()
        defer { fixture.cleanup() }
        let store = fixture.store
        let accepted = await store.answer(fixture.decision, choice: "reply", correction: "Explain the effect first.", undoSeconds: 1)
        XCTAssertTrue(accepted)
        let waiting = try XCTUnwrap(store.waitingAnswer(for: fixture.decision.id))
        XCTAssertEqual(store.latestWaitingAnswer()?.id, waiting.id)
        XCTAssertTrue(store.answeringDecisionIds.contains(fixture.decision.id))
        try await Task.sleep(for: .milliseconds(500))
        XCTAssertEqual(fixture.server.answers, 0, "Nothing reaches the Mac in the Undo window.")
        try await waitUntil { fixture.server.answers == 1 }
        // The same body and idempotency key as an answer that sends at once.
        let expected = AnswerRequest(choice: "reply", revision: fixture.decision.revision, correction: "Explain the effect first.",
                                     optionId: nil, draftHash: fixture.decision.draftHash, previewsHash: fixture.decision.previewsHash)
        let sent = try JSONSerialization.jsonObject(with: XCTUnwrap(fixture.server.bodies.first)) as? NSDictionary
        XCTAssertEqual(sent, try JSONSerialization.jsonObject(with: APIJSON.encode(expected)) as? NSDictionary)
        XCTAssertEqual(fixture.server.keys.first?.lowercased(), "idempotency-key: " + waiting.id.lowercased())
        try await waitUntil { !store.answeringDecisionIds.contains(fixture.decision.id) }
        XCTAssertNil(store.waitingAnswer(for: fixture.decision.id))
    }

    @MainActor
    func testUndoInTheWindowSendsNothingAndReturnsTheCard() async throws {
        let fixture = try await fixture()
        defer { fixture.cleanup() }
        let store = fixture.store
        _ = await store.answer(fixture.decision, choice: "reply", correction: "Wait for the review.", undoSeconds: 1)
        XCTAssertTrue(store.undoAnswer(decisionId: fixture.decision.id))
        XCTAssertFalse(store.answeringDecisionIds.contains(fixture.decision.id))
        XCTAssertEqual(store.restoredDecisionId, fixture.decision.id)
        XCTAssertEqual(store.decisionReplyDrafts[fixture.decision.id], "Wait for the review.")
        XCTAssertTrue(store.outgoingAnswers.isEmpty)
        XCTAssertEqual(AppStore(demo: false).outgoingAnswers, [], "Undo also removes the saved answer.")
        try await Task.sleep(for: .milliseconds(1500))
        XCTAssertEqual(fixture.server.answers, 0)
        XCTAssertFalse(store.undoAnswer(decisionId: fixture.decision.id), "Nothing is left to undo.")
    }

    @MainActor
    func testAnUndoThatCannotBeSavedKeepsTheAnswer() async throws {
        let fixture = try await fixture()
        defer { fixture.cleanup() }
        let store = fixture.store
        _ = await store.answer(fixture.decision, choice: "reject", undoSeconds: 1)
        let answer = try XCTUnwrap(store.outgoingAnswers.first)
        store.writeOutgoing = { _ in throw CocoaError(.fileWriteNoPermission) }
        XCTAssertFalse(store.undoAnswer(decisionId: fixture.decision.id))
        XCTAssertEqual(store.outgoingAnswers, [answer])
        XCTAssertTrue(store.answeringDecisionIds.contains(fixture.decision.id))
        XCTAssertNil(store.restoredDecisionId)
        XCTAssertNotNil(store.error)
        // The answer stays saved and sends, because the person did not get Undo.
        store.writeOutgoing = { try ProtectedCache.write($0, name: "outgoing.json") }
        try await waitUntil { fixture.server.answers == 1 }
        XCTAssertEqual(fixture.server.answers, 1)
    }

    @MainActor
    func testAnAnswerSendsInTheBackground() async throws {
        let fixture = try await fixture()
        defer { fixture.cleanup() }
        let store = fixture.store
        store.setForeground(true)
        _ = await store.answer(fixture.decision, choice: "reject", undoSeconds: 1)
        store.setForeground(false)
        try await waitUntil { fixture.server.answers == 1 }
        XCTAssertEqual(fixture.server.answers, 1)
    }

    @MainActor
    func testAnAppKilledInTheWindowSendsTheAnswerAtTheNextLaunch() async throws {
        let fixture = try await fixture()
        defer { fixture.cleanup() }
        let store = fixture.store
        _ = await store.answer(fixture.decision, choice: "reject", undoSeconds: 0.5)
        let id = try XCTUnwrap(store.outgoingAnswers.first?.id)
        // Simulate termination: keep the saved files and the token, and stop this store without its delivery.
        let files = fixture.paths.map { try? Data(contentsOf: $0) }
        let token = PairingToken.read()
        store.disconnect()
        for (path, data) in zip(fixture.paths, files) { if let data { try data.write(to: path, options: .atomic) } }
        if let token { try PairingToken.save(token) }
        try await Task.sleep(for: .milliseconds(700))
        XCTAssertEqual(fixture.server.answers, 0)
        let relaunched = AppStore(demo: false)
        defer { relaunched.setForeground(false); relaunched.disconnect() }
        XCTAssertEqual(relaunched.outgoingAnswers.first?.id, id)
        relaunched.setForeground(true)
        try await waitUntil { fixture.server.answers == 1 }
        XCTAssertEqual(fixture.server.keys.first?.lowercased(), "idempotency-key: " + id.lowercased())
    }

    @MainActor
    func testAChangedCardDropsTheAnswerAndTellsThePerson() async throws {
        let fixture = try await fixture()
        defer { fixture.cleanup() }
        let store = fixture.store
        _ = await store.answer(fixture.decision, choice: "reply", correction: "Merge after the review.", undoSeconds: 0.5)
        store.decisions[0].revision += 1
        try await waitUntil { !store.answeringDecisionIds.contains(fixture.decision.id) }
        XCTAssertEqual(fixture.server.answers, 0)
        XCTAssertTrue(store.outgoingAnswers.isEmpty)
        XCTAssertEqual(store.error, AppStore.answerNotSent(store.decisions[0]))
        XCTAssertEqual(store.decisionReplyDrafts[fixture.decision.id], "Merge after the review.")
    }

    func testAnAnswerSavedBeforeTheWindowSendsAtOnce() throws {
        let saved = #"{"id":"a","decisionId":"d","botId":"b","createdAt":"2026-10-30T12:00:00Z","sequence":1,"text":"Reject","#
            + #""request":{"choice":"reject","revision":2}}"#
        let answer = try JSONDecoder().decode(OutgoingAnswer.self, from: Data(saved.utf8))
        XCTAssertNil(answer.sendAt)
        XCTAssertFalse(answer.isWaiting())
        var waiting = answer
        waiting.sendAt = Date().addingTimeInterval(5)
        XCTAssertTrue(waiting.isWaiting())
        XCTAssertFalse(waiting.isWaiting(at: Date().addingTimeInterval(6)))
        let restored = try JSONDecoder().decode(OutgoingAnswer.self, from: JSONEncoder().encode(waiting))
        XCTAssertEqual(restored.sendAt?.timeIntervalSince1970 ?? 0, waiting.sendAt!.timeIntervalSince1970, accuracy: 0.001)
    }
}
