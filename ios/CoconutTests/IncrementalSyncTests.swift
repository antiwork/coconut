import XCTest
import Network
@testable import Coconut

final class IncrementalSyncTests: XCTestCase {
    @MainActor
    func testContentCardsAndUpdatesDoNotWaitForNativeSettings() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        var state = try PreviewStateFixture.load()
        state.cursor = 10; state.syncVersion = 1; state.contentOnly = true
        state.historyBefore = [state.bots[0].id: 60]
        state.confirmedRequestIds = ["lost-receipt"]
        var decision = state.decisions[0]; decision.revision += 1
        decision.title = "Current card"
        var task = state.tasks[0]; task.status = "completed"
        var message = state.messages[0]; message.id = "incremental-message"; message.text = "Incremental output"
        var old = message; old.id = "old-history"; old.text = "Earlier output"
        var oldDecision = decision; oldDecision.id = "old-card"; oldDecision.status = "resolved"
        let routine = Routine(id: "confirmed-routine", botId: state.bots[0].id, name: "Daily check", prompt: "Check the queue.",
            cron: "daily", timezone: "UTC", enabled: true, nextRunAt: nil, lastRunAt: nil,
            lastTaskId: nil, createdAt: state.serverTime, updatedAt: state.serverTime)
        let device = UUID().uuidString
        func json(_ value: Any) throws -> Data { try JSONSerialization.data(withJSONObject: value) }
        func object<T: Encodable>(_ value: T) throws -> Any { try JSONSerialization.jsonObject(with: APIJSON.encoder().encode(value)) }
        let server = try IncrementalServer(responses: [
            "POST /v1/bots/\(state.bots[0].id)/routines": try APIJSON.encoder().encode(routine),
            "POST /v1/settings/mcp": try json(["ok": true, "servers": [], "tools": []]),
            "POST /v1/pair": try json(["device_id": device, "token": "fixture-only"]),
            "GET /v1/state": try APIJSON.encoder().encode(state),
            "GET /v1/cards/\(decision.id)": try json(["decision": object(decision), "task": object(task),
                "bot": object(state.bots[0]), "owner": object(state.bots[0]), "cursor": 11]),
            "GET /v1/history/\(state.bots[0].id)": try json(["messages": [object(old)], "tasks": [], "decisions": [object(oldDecision), object(state.decisions[0])], "before": 50]),
            "GET /v1/history/\(state.bots[0].id)?before=50": try json(["messages": [object(old)], "tasks": [], "before": NSNull()])
        ], update: try json(["after": 10, "cursor": 11, "server_time": state.serverTime,
                            "tasks": [object(task)], "messages": [object(message)], "decisions": [object(decision)]]),
            initialUpdate: try json(["after": 10, "cursor": 11, "server_time": state.serverTime,
                "tasks": [object(task)], "messages": [object(message)], "decisions": [object(state.decisions[0])]]))
        let address = try await server.start()
        defer { server.stop() }
        let savedToken = PairingToken.read()
        let folder = URL.applicationSupportDirectory.appendingPathComponent("Coconut")
        let paths = ["pairing.json", "snapshot.json", "outgoing.json"].map { folder.appendingPathComponent($0) }
        let saved = paths.map { try? Data(contentsOf: $0) }
        let store = AppStore(demo: false)
        store.disconnect()
        defer {
            store.setForeground(false); store.disconnect()
            if let savedToken { try? PairingToken.save(savedToken) }
            for (path, data) in zip(paths, saved) {
                if let data { try? data.write(to: path, options: .atomic) }
                else { try? FileManager.default.removeItem(at: path) }
            }
        }
        store.serverURL = address; store.pairCode = "fixture"
        let start = ContinuousClock.now
        await store.pair()
        XCTAssertEqual(store.connection, .connected, store.error ?? "No error")
        XCTAssertLessThan(start.duration(to: .now), .seconds(2), "Native settings wait five seconds in this test.")
        let target = try XCTUnwrap(NotificationTarget(userInfo: ["coconut": ["version": 1, "device_id": device,
            "bot_id": state.bots[0].id, "task_id": task.id, "decision_id": decision.id]]))
        store.outgoingMessages = [OutgoingMessage(id: "lost-receipt", botId: state.bots[0].id, executorIds: [state.bots[0].id],
            request: MessageRequest(text: "Accepted outside recent history", mode: "queue", taskId: nil, scheduledAt: nil, attachmentIds: [], recipientBotIds: []),
            createdAt: state.serverTime, status: .accepted)]
        store.setForeground(true)
        for _ in 0..<40 {
            if server.updateRequests > 0 { break }
            try await Task.sleep(for: .milliseconds(10))
        }
        XCTAssertGreaterThan(server.updateRequests, 0)
        let cardStart = ContinuousClock.now
        let destination = await store.notificationDestination(target)
        XCTAssertNotNil(destination)
        XCTAssertEqual(store.decisions.first { $0.id == decision.id }?.title, "Current card")
        XCTAssertLessThan(cardStart.duration(to: .now), .seconds(1))
        store.setForeground(true)
        for _ in 0..<40 {
            if store.messages.contains(where: { $0.id == message.id }) { break }
            try await Task.sleep(for: .milliseconds(50))
        }
        XCTAssertEqual(store.messages.filter { $0.id == message.id }.count, 1)
        XCTAssertEqual(store.decisions.first { $0.id == decision.id }?.title, "Current card")
        XCTAssertEqual(store.tasks.first { $0.id == task.id }?.status, "completed")
        XCTAssertTrue(store.outgoingMessages.isEmpty)
        let firstPage = await store.loadOlderMessages(state.bots[0].id)
        XCTAssertTrue(firstPage)
        XCTAssertTrue(store.messages.contains { $0.text == "Earlier output" })
        XCTAssertEqual(store.decisions.first { $0.id == oldDecision.id }, oldDecision)
        XCTAssertEqual(store.decisions.first { $0.id == decision.id }?.title, "Current card")
        let count = store.messages.count
        let duplicatePage = await store.loadOlderMessages(state.bots[0].id)
        XCTAssertTrue(duplicatePage, "A page can advance its cursor without adding another message.")
        XCTAssertEqual(store.messages.count, count)
        XCTAssertFalse(store.hasOlderMessages(state.bots[0].id))
        let saveStart = ContinuousClock.now
        await store.createRoutine(bot: state.bots[0], name: routine.name, prompt: routine.prompt, cron: routine.cron, timezone: routine.timezone)
        XCTAssertLessThan(saveStart.duration(to: .now), .seconds(2), "Confirmed saves must not wait for native discovery.")
        XCTAssertEqual(store.routines.filter { $0.id == routine.id }.count, 1)
        XCTAssertNil(store.error, "A failed settings read must not turn a confirmed creation into a failed save.")
        XCTAssertEqual(server.stateRequests, 1, "Updates and card reads must not download another full state.")
        let savedWrite = Task { await store.updateMCP(MCPRequest(action: "remove", name: "fixture")) }
        try await Task.sleep(for: .milliseconds(100))
        store.setForeground(false)
        let receipt = await savedWrite.value
        XCTAssertEqual(receipt?.ok, true, "A transport stop must not discard a confirmed mutation.")
        store.setForeground(true)
        let uncertainWrite = Task { await store.updateInbox(decision, action: "dismiss", idempotencyKey: UUID().uuidString) }
        try await Task.sleep(for: .milliseconds(100))
        store.setForeground(false)
        let uncertain = await uncertainWrite.value
        XCTAssertTrue(uncertain.isUncertain, "An incomplete receipt must retain recovery across a transport stop.")
        store.model = "previous-mac-model"
        store.mcpServers = [MCPServer(id: "previous-mac-server", url: "https://example.com/mcp", transport: "http", hasToken: false, editable: true)]
        store.serverURL = address; store.pairCode = "fixture"
        await store.pair()
        XCTAssertEqual(store.model, state.model)
        XCTAssertTrue(store.mcpServers.isEmpty)
        XCTAssertTrue(store.routines.isEmpty)
        let lateWrite = Task { await store.createRoutine(bot: state.bots[0], name: routine.name, prompt: routine.prompt, cron: routine.cron, timezone: routine.timezone) }
        try await Task.sleep(for: .milliseconds(10))
        store.disconnect()
        await lateWrite.value
        XCTAssertTrue(store.routines.isEmpty, "A late write must not restore records after unpairing.")
        XCTAssertNil(store.error)
        #endif
    }
}

private final class IncrementalServer: @unchecked Sendable {
    private let queue = DispatchQueue(label: "coconut.incremental-test")
    private let lock = NSLock()
    private let listener: NWListener
    private let responses: [String: Data]
    private let update: Data
    private let initialUpdate: Data?
    private var count = 0
    private var updates = 0
    var updateRequests: Int { lock.withLock { updates } }
    var stateRequests: Int { lock.withLock { count } }
    init(responses: [String: Data], update: Data, initialUpdate: Data? = nil) throws {
        self.responses = responses; self.update = update; self.initialUpdate = initialUpdate
        listener = try NWListener(using: .tcp, on: .any)
    }
    func start() async throws -> String {
        listener.newConnectionHandler = { [self] connection in
            connection.start(queue: queue)
            connection.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [self] data, _, _, _ in
                guard let data, let first = String(data: data, encoding: .utf8)?.components(separatedBy: "\r\n").first else { connection.cancel(); return }
                let words = first.split(separator: " ")
                guard words.count >= 2 else { connection.cancel(); return }
                let path = String(words[1])
                let route = String(words[0]) + " " + path.components(separatedBy: "?")[0]
                if route == "GET /v1/state" { lock.withLock { count += 1 } }
                let body: Data
                if path.hasPrefix("/v1/updates") {
                    let firstUpdate = lock.withLock { updates += 1; return updates == 1 }
                    body = path.contains("after=10") ? (firstUpdate ? initialUpdate ?? update : update) : Data("{\"after\":11,\"cursor\":11,\"server_time\":\"2026-09-15T00:00:00Z\"}".utf8)
                } else { body = responses[String(words[0]) + " " + path] ?? responses[route] ?? Data("{}".utf8) }
                var response = Data("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: \(body.count)\r\nConnection: close\r\n\r\n".utf8)
                response.append(body)
                let reply = response
                let delay = path == "/v1/state/settings" ? 5.0 : ((path.hasPrefix("/v1/updates") || path.hasSuffix("/inbox")) ? 0.25 : 0.05)
                queue.asyncAfter(deadline: .now() + delay) {
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
