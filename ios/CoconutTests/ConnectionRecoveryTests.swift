import XCTest
import Network
@testable import Coconut

final class ConnectionRecoveryTests: XCTestCase {
    func testShortReadTimeoutDoesNotCancelLongPoll() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let server = try DelayedConnectionServer()
        let address = try await server.start()
        defer { server.stop() }
        let client = try APIClient(loopback: address, token: "fixture-only")
        async let poll: [String: Int] = client.request("updates?after=0&wait=20", retryTransient: false)
        let started = ContinuousClock.now
        do {
            let _: [String: Int] = try await client.request("state", retryTransient: false)
            XCTFail("A stalled foreground read must time out before the delayed response.")
        } catch let error as URLError {
            XCTAssertEqual(error.code, .timedOut)
            // The foreground budget is 6 s and the long-poll budget is 35 s. A stalled CI simulator can add seconds.
            XCTAssertLessThan(started.duration(to: .now), .seconds(20))
        }
        let result = try await poll
        XCTAssertEqual(result["ok"], 1, "The event request must keep its longer budget.")
        #endif
    }

    @MainActor
    func testRepeatedAvailablePathsDoNotCancelSynchronization() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let server = try ConnectionServer(snapshot: APIJSON.encoder().encode(PreviewStateFixture.load()))
        let address = try await server.start()
        defer { server.stop() }
        let savedToken = PairingToken.read()
        let folder = URL.applicationSupportDirectory.appendingPathComponent("Coconut")
        let paths = ["pairing.json", "snapshot.json", "outgoing.json"].map { folder.appendingPathComponent($0) }
        let savedFiles = paths.map { try? Data(contentsOf: $0) }
        // Start without a saved state, so the first request is always the state download.
        for path in paths { try? FileManager.default.removeItem(at: path) }
        let variables = ["COCONUT_SERVER_URL": address, "COCONUT_DEVICE_TOKEN": "fixture-only"]
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
        try await waitUntil { server.requests > 0 }
        XCTAssertEqual(server.requests, 1)
        // The server holds the state reply, so every repeated path arrives while the download is open.
        for _ in 0..<4 {
            store.networkChanged(available: true)
            try await Task.sleep(for: .milliseconds(50))
        }
        // A restarted download sends its request at once. Check before the release: after a simulator stall, the
        // held request can time out and the app correctly retries it, so the final count can be 2.
        try await Task.sleep(for: .milliseconds(300))
        XCTAssertEqual(server.requests, 1, "An unchanged route must preserve the current download.")
        server.releaseState()
        try await waitUntil { store.connection == .connected }
        XCTAssertEqual(store.connection, .connected, store.syncIssue ?? "No sync error")
        #endif
    }
}

private final class ConnectionServer: @unchecked Sendable {
    private let queue = DispatchQueue(label: "coconut.connection-test")
    private let lock = NSLock()
    private let listener: NWListener
    private let snapshot: Data
    private var count = 0
    private var stateReleased = false
    private var heldReplies: [@Sendable () -> Void] = []
    var requests: Int { lock.withLock { count } }

    init(snapshot: Data) throws {
        self.snapshot = snapshot
        listener = try NWListener(using: .tcp, on: .any)
    }

    func start() async throws -> String {
        listener.newConnectionHandler = { [self] connection in
            connection.start(queue: queue)
            connection.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [self] data, _, _, _ in
                guard let data else { connection.cancel(); return }
                let state = String(decoding: data, as: UTF8.self).hasPrefix("GET /v1/state ")
                if state { lock.withLock { count += 1 } }
                let body = state ? snapshot : Data("{\"events\":[],\"cursor\":0,\"server_time\":\"2026-09-15T00:00:00Z\"}".utf8)
                var response = Data("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: \(body.count)\r\nConnection: close\r\n\r\n".utf8)
                response.append(body)
                let reply = response
                let send: @Sendable () -> Void = {
                    connection.send(content: reply, completion: .contentProcessed { _ in connection.cancel() })
                }
                let held = state && lock.withLock {
                    if stateReleased { return false }
                    heldReplies.append(send)
                    return true
                }
                if !held { send() }
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

    /// Sends the held state replies, and sends later ones at once.
    func releaseState() {
        let replies = lock.withLock {
            stateReleased = true
            defer { heldReplies = [] }
            return heldReplies
        }
        queue.async { replies.forEach { $0() } }
    }

    func stop() { listener.cancel() }
}

private final class DelayedConnectionServer: @unchecked Sendable {
    private let queue = DispatchQueue(label: "coconut.timeout-test")
    private let listener: NWListener

    init() throws { listener = try NWListener(using: .tcp, on: .any) }

    func start() async throws -> String {
        listener.newConnectionHandler = { [self] connection in
            connection.start(queue: queue)
            connection.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [self] data, _, _, _ in
                guard let data else { connection.cancel(); return }
                // The state read never gets a reply, so it can only end with its own timeout.
                if String(decoding: data, as: UTF8.self).hasPrefix("GET /v1/state ") { return }
                let reply = Data("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 8\r\nConnection: close\r\n\r\n{\"ok\":1}".utf8)
                queue.asyncAfter(deadline: .now() + 7) {
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
