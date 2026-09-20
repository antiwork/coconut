import XCTest
import Network
@testable import Coconut

final class DecisionDeliveryTests: XCTestCase {
    @MainActor
    func testChoiceSavesBeforeNetworkResponseAndRestoresAfterRestart() async throws {
        try await exercise(retry: false, reject: false, restart: true)
    }

    @MainActor
    func testTransientFailureRetriesTheSameSavedChoiceAfterSync() async throws {
        try await exercise(retry: true, reject: false)
    }

    @MainActor
    func testStaleRevisionRestoresCardAndReplyText() async throws {
        try await exercise(retry: false, reject: true)
    }

    @MainActor
    func testAnotherConversationSendsWhileFirstAnswerWaits() async throws {
        try await exercise(retry: false, reject: false, parallel: true)
    }

    @MainActor
    private func exercise(retry: Bool, reject: Bool, parallel: Bool = false, restart: Bool = false) async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This fixture requires a debug simulator.")
        #else
        var snapshot = try PreviewStateFixture.load()
        var decision = try XCTUnwrap(snapshot.decisions.first(where: \.isPending))
        decision.options = []
        snapshot.decisions = [decision]
        if parallel {
            var second = decision
            second.id = "other-decision"
            second.botId = "other-bot"
            snapshot.decisions.append(second)
        }
        let server = try DecisionDeliveryServer(snapshot: snapshot, failFirst: retry, reject: reject)
        let address = try await server.start()
        defer { server.stop() }
        let savedToken = PairingToken.read()
        let folder = URL.applicationSupportDirectory.appendingPathComponent("Coconut")
        let paths = ["pairing.json", "snapshot.json", "outgoing.json"].map { folder.appendingPathComponent($0) }
        let saved = paths.map { try? Data(contentsOf: $0) }
        for path in paths { try? FileManager.default.removeItem(at: path) }
        let variables = ["COCONUT_SERVER_URL": address, "COCONUT_CERT_SHA256": "", "COCONUT_DEVICE_TOKEN": "fixture-only"]
        let previous = ProcessInfo.processInfo.environment
        for (key, value) in variables { setenv(key, value, 1) }
        let store = AppStore(demo: false)
        var restartedStore: AppStore?
        defer {
            restartedStore?.setForeground(false)
            restartedStore?.disconnect()
            store.setForeground(false)
            store.disconnect()
            for key in variables.keys {
                if let value = previous[key] { setenv(key, value, 1) } else { unsetenv(key) }
            }
            if let savedToken { try? PairingToken.save(savedToken) } else { PairingToken.delete() }
            for (path, data) in zip(paths, saved) {
                if let data { try? data.write(to: path, options: .atomic) }
                else { try? FileManager.default.removeItem(at: path) }
            }
        }
        store.bots = snapshot.bots
        store.decisions = snapshot.decisions
        var queue = DecisionQueue()
        queue.reconcile([decision], sort: .recommended)
        let start = ContinuousClock.now
        let accepted = await store.answer(decision, choice: "reply", correction: "Please explain the effect first.")
        let elapsed = start.duration(to: .now)
        print("COCONUT_CARD_LOCAL_ACCEPTANCE=\(elapsed)")
        XCTAssertTrue(accepted)
        XCTAssertLessThan(elapsed, .milliseconds(200), "Saving a choice must not wait for the network.")
        XCTAssertTrue(store.answeringDecisionIds.contains(decision.id))
        XCTAssertEqual(store.inboxBadgeCount, parallel ? 1 : 0)
        queue.reconcile(store.decisions.filter { $0.isPending && !store.answeringDecisionIds.contains($0.id) }, sort: .recommended)
        XCTAssertFalse(queue.ids.contains(decision.id))
        let restored = AppStore(demo: false)
        XCTAssertTrue(restored.answeringDecisionIds.contains(decision.id), "The saved response must survive app termination.")
        let reply = try XCTUnwrap(restored.outgoingAnswers.first?.message)
        XCTAssertEqual(reply.decisionId, decision.id)
        XCTAssertEqual(reply.replyTo?.text, decision.title)
        XCTAssertEqual(reply.replyTo?.messageId, decision.id)
        let duplicate = await store.answer(decision, choice: "reply", correction: "Different reply")
        XCTAssertFalse(duplicate)
        if parallel {
            let acceptedSecond = await store.answer(snapshot.decisions[1], choice: "reply", correction: "Another conversation")
            XCTAssertTrue(acceptedSecond)
            try await Task.sleep(for: .milliseconds(200))
            XCTAssertEqual(server.answers, 2, "An unrelated conversation must send before the first request returns.")
        }
        if retry {
            try await Task.sleep(for: .milliseconds(200))
            store.setForeground(true)
        }
        var activeStore = store
        if restart {
            for _ in 0..<40 {
                if server.answers == 1 { break }
                try await Task.sleep(for: .milliseconds(25))
            }
            XCTAssertEqual(server.answers, 1)
            let pendingFiles = paths.map { try? Data(contentsOf: $0) }
            let pendingToken = PairingToken.read()
            store.disconnect()
            // Simulate termination without the intentional unpairing performed by disconnect.
            for (path, data) in zip(paths, pendingFiles) {
                if let data { try data.write(to: path, options: .atomic) }
            }
            if let pendingToken { try PairingToken.save(pendingToken) }
            let resumed = AppStore(demo: false)
            restartedStore = resumed
            activeStore = resumed
            resumed.setForeground(true)
        }
        for _ in 0..<100 {
            if !activeStore.answeringDecisionIds.contains(decision.id) { break }
            try await Task.sleep(for: .milliseconds(50))
        }
        XCTAssertFalse(activeStore.answeringDecisionIds.contains(decision.id))
        if reject {
            XCTAssertEqual(store.inboxBadgeCount, 1)
            queue.reconcile(store.decisions.filter { $0.isPending && !store.answeringDecisionIds.contains($0.id) }, sort: .recommended)
            XCTAssertEqual(queue.ids, [decision.id], "A rejected choice must restore the visible card, not only the badge.")
            XCTAssertEqual(store.decisionReplyDrafts[decision.id], "Please explain the effect first.")
        }
        XCTAssertEqual(server.answers, retry ? 3 : parallel || restart ? 2 : 1)
        XCTAssertEqual(Set(server.keys).count, parallel ? 2 : 1, "A retry must keep the original idempotency key.")
        #endif
    }
}

private final class DecisionDeliveryServer: @unchecked Sendable {
    private let queue = DispatchQueue(label: "coconut.decision-delivery-test")
    private let lock = NSLock()
    private let listener: NWListener
    private let snapshot: StateSnapshot
    private var count = 0
    private var requestKeys: [String] = []
    private let failFirst: Bool
    private let reject: Bool
    var keys: [String] { lock.withLock { requestKeys } }
    var answers: Int { lock.withLock { count } }

    init(snapshot: StateSnapshot, failFirst: Bool, reject: Bool) throws {
        self.reject = reject
        self.failFirst = failFirst
        self.snapshot = snapshot
        listener = try NWListener(using: .tcp, on: .any)
    }

    func start() async throws -> String {
        listener.newConnectionHandler = { [self] connection in
            connection.start(queue: queue)
            connection.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [self] data, _, _, _ in
                guard let data else { connection.cancel(); return }
                let answer = String(decoding: data, as: UTF8.self).hasPrefix("POST /v1/decisions/")
                let body: Data
                let fail = answer && failFirst && answers < 2
                if answer {
                    let key = String(decoding: data, as: UTF8.self).components(separatedBy: "\r\n")
                        .first { $0.lowercased().hasPrefix("idempotency-key:") } ?? "missing"
                    lock.withLock { count += 1; requestKeys.append(key) }
                    let request = String(decoding: data, as: UTF8.self)
                    var receipt = snapshot.decisions.first { request.hasPrefix("POST /v1/decisions/\($0.id)/answer ") } ?? snapshot.decisions[0]
                    receipt.status = "replied"
                    body = fail || reject ? Data("{\"error\":\"Temporarily unavailable\"}".utf8) : try! APIJSON.encoder().encode(receipt)
                } else if String(decoding: data, as: UTF8.self).hasPrefix("GET /v1/events") {
                    body = Data("{\"events\":[],\"cursor\":0,\"server_time\":\"2026-09-18T00:00:00Z\"}".utf8)
                } else { body = try! APIJSON.encoder().encode(snapshot) }
                var response = Data("HTTP/1.1 \(fail ? "503 Unavailable" : reject && answer ? "409 Conflict" : "200 OK")\r\nContent-Type: application/json\r\nContent-Length: \(body.count)\r\nConnection: close\r\n\r\n".utf8)
                response.append(body)
                let reply = response
                queue.asyncAfter(deadline: .now() + (fail ? 0.05 : answer ? 2 : 0.05)) {
                    connection.send(content: reply, completion: .contentProcessed { _ in connection.cancel() })
                }
            }
        }
        return try await withCheckedThrowingContinuation { continuation in
            listener.stateUpdateHandler = { [self] state in
                switch state {
                case .ready:
                    listener.stateUpdateHandler = nil
                    continuation.resume(returning: "http://127.0.0.1:\(listener.port!.rawValue)")
                case .failed(let error):
                    listener.stateUpdateHandler = nil
                    continuation.resume(throwing: error)
                default: break
                }
            }
            listener.start(queue: queue)
        }
    }
    func stop() { listener.cancel() }
}
