import XCTest
import Network
@testable import Coconut

final class OlderWorkTests: XCTestCase {
    func testRecentStateAndUpdatesAskForRecentWork() throws {
        let client = try APIClient(serverURL: "https://mac.example:443", fingerprint: String(repeating: "a", count: 64),
                                   token: "fixture-token")
        XCTAssertEqual(try client.makeRequest("state").value(forHTTPHeaderField: "X-Coconut-Work"), "recent")
        XCTAssertEqual(try client.makeRequest("updates?after=3&wait=20").value(forHTTPHeaderField: "X-Coconut-Work"), "recent")
        XCTAssertNil(try client.makeRequest("state", fullState: true).value(forHTTPHeaderField: "X-Coconut-Work"))
        XCTAssertNil(try client.makeRequest("cards/card").value(forHTTPHeaderField: "X-Coconut-Work"))
    }

    @MainActor
    func testTaskDetailsLoadsOlderWorkOnceAndKeepsCurrentCards() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        var state = try PreviewStateFixture.load()
        let bot = state.bots[0].id
        state.syncVersion = 1; state.contentOnly = true; state.cursor = 10
        state.olderWorkBotIds = [bot]
        state.historyBefore = [bot: 60]
        let current = Artifact(id: "current", botId: bot, taskId: nil, title: "Current", kind: "work", status: "completed",
                               preview: "", url: nil, createdAt: state.serverTime, nativeTaskId: "native-current", workState: "completed")
        state.artifacts = [current]
        var stale = current
        stale.title = "Stale copy"
        var older = current
        older.id = "older"; older.title = "Older"; older.nativeTaskId = "native-older"; older.taskId = "older-task"
        var task = state.tasks[0]
        task.id = "older-task"; task.status = "completed"
        func object<T: Encodable>(_ value: T) throws -> Any { try JSONSerialization.jsonObject(with: APIJSON.encoder().encode(value)) }
        let page = try JSONSerialization.data(withJSONObject: ["artifacts": [object(stale), object(older)], "tasks": [object(task)], "cursor": 10])
        var card = current
        card.id = "history-card"; card.kind = "pr"; card.title = "Merged fix"; card.url = "https://github.com/acme/app/pull/7"
        var message = state.messages[0]
        message.id = "history-message"; message.kind = "artifact"; message.artifactId = card.id
        let history = try JSONSerialization.data(withJSONObject: ["messages": [object(message)], "tasks": [], "artifacts": [object(card)], "before": NSNull()])
        let server = try OlderWorkServer(responses: ["/v1/state": try APIJSON.encoder().encode(state),
                                                     "/v1/bots/\(bot)/work/older": page, "/v1/history/\(bot)": history])
        let address = try await server.start()
        defer { server.stop() }
        let savedToken = PairingToken.read()
        let folder = URL.applicationSupportDirectory.appendingPathComponent("Coconut")
        let paths = ["pairing.json", "snapshot.json", "outgoing.json"].map { folder.appendingPathComponent($0) }
        let savedFiles = paths.map { try? Data(contentsOf: $0) }
        // Start without a saved state, so the store reads only this fixture.
        for path in paths { try? FileManager.default.removeItem(at: path) }
        let variables = ["COCONUT_SERVER_URL": address,
                         "COCONUT_CERT_SHA256": String(repeating: "0", count: 64), "COCONUT_DEVICE_TOKEN": "fixture-only"]
        let previous = ProcessInfo.processInfo.environment
        for (key, value) in variables { setenv(key, value, 1) }
        let store = AppStore(demo: false)
        defer {
            store.setForeground(false)
            store.disconnect()
            for key in variables.keys {
                if let value = previous[key] { setenv(key, value, 1) } else { unsetenv(key) }
            }
            if let savedToken { try? PairingToken.save(savedToken) } else { PairingToken.delete() }
            for (path, data) in zip(paths, savedFiles) {
                if let data { try? data.write(to: path, options: .atomic) }
                else { try? FileManager.default.removeItem(at: path) }
            }
        }
        store.networkChanged(available: true)
        store.setForeground(true)
        for _ in 0..<60 {
            if store.connection == .connected && !store.olderWorkBotIds.isEmpty { break }
            try await Task.sleep(for: .milliseconds(50))
        }
        XCTAssertEqual(store.olderWorkBotIds, [bot], store.syncIssue ?? "No sync error")
        XCTAssertEqual(store.artifacts.map(\.id), ["current"])

        await store.loadOlderWork(bot)
        XCTAssertEqual(Set(store.artifacts.map(\.id)), ["current", "older"])
        XCTAssertEqual(store.artifacts.first { $0.id == "current" }?.title, "Current", "A card from an update must win over the older copy.")
        XCTAssertTrue(store.tasks.contains { $0.id == "older-task" })
        XCTAssertTrue(store.olderWorkBotIds.isEmpty)
        let items = TrackedWorkItem.nativeTasks(in: store.artifacts, botId: bot, tasks: store.tasks, includeDismissed: true)
        XCTAssertEqual(Set(items.map(\.artifact.id)), ["current", "older"])
        await store.loadOlderWork(bot)
        XCTAssertEqual(server.requests(to: "/v1/bots/\(bot)/work/older"), 1, "Loaded Work stays until the next reset.")
        var saved: SnapshotCache?
        for _ in 0..<40 {
            saved = ProtectedCache.read("snapshot.json")
            if saved?.snapshot.olderWorkBotIds?.isEmpty == true { break }
            try await Task.sleep(for: .milliseconds(50))
        }
        XCTAssertEqual(saved?.snapshot.olderWorkBotIds, [])
        XCTAssertEqual(Set(saved?.snapshot.artifacts.map(\.id) ?? []), ["current", "older"])

        // An older message shows its card, which recent content left out.
        let loaded = await store.loadOlderMessages(bot)
        XCTAssertTrue(loaded)
        XCTAssertEqual(store.artifacts.first { $0.id == "history-card" }?.title, "Merged fix")
        #endif
    }
}

private final class OlderWorkServer: @unchecked Sendable {
    private let queue = DispatchQueue(label: "coconut.older-work-test")
    private let lock = NSLock()
    private let listener: NWListener
    private let responses: [String: Data]
    private var counts: [String: Int] = [:]

    init(responses: [String: Data]) throws {
        self.responses = responses
        listener = try NWListener(using: .tcp, on: .any)
    }

    func requests(to path: String) -> Int { lock.withLock { counts[path] ?? 0 } }

    func start() async throws -> String {
        listener.newConnectionHandler = { [self] connection in
            connection.start(queue: queue)
            connection.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [self] data, _, _, _ in
                guard let data, let first = String(data: data, encoding: .utf8)?.components(separatedBy: "\r\n").first,
                      first.split(separator: " ").count >= 2 else { connection.cancel(); return }
                let path = String(first.split(separator: " ")[1]).components(separatedBy: "?")[0]
                lock.withLock { counts[path, default: 0] += 1 }
                // Updates report no change, so the loaded state stays as the test set it.
                let body = responses[path] ?? Data("{\"after\":10,\"cursor\":10,\"server_time\":\"2026-09-15T00:00:00Z\"}".utf8)
                var response = Data("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: \(body.count)\r\nConnection: close\r\n\r\n".utf8)
                response.append(body)
                let reply = response
                queue.asyncAfter(deadline: .now() + (path == "/v1/updates" ? 0.2 : 0.02)) {
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
