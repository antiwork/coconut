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
        let client = try APIClient(serverURL: address, fingerprint: "", token: "fixture-only")
        async let poll: [String: Int] = client.request("updates?after=0&wait=20", retryTransient: false)
        let started = ContinuousClock.now
        do {
            let _: [String: Int] = try await client.request("state", retryTransient: false)
            XCTFail("A stalled foreground read must time out before the delayed response.")
        } catch let error as URLError {
            XCTAssertEqual(error.code, .timedOut)
            XCTAssertLessThan(started.duration(to: .now), .seconds(7))
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
        for _ in 0..<40 {
            if server.requests > 0 { break }
            try await Task.sleep(for: .milliseconds(50))
        }
        XCTAssertEqual(server.requests, 1)
        for _ in 0..<4 {
            store.networkChanged(available: true)
            try await Task.sleep(for: .milliseconds(50))
        }
        for _ in 0..<40 {
            if store.connection == .connected { break }
            try await Task.sleep(for: .milliseconds(50))
        }
        XCTAssertEqual(store.connection, .connected, store.syncIssue ?? "No sync error")
        XCTAssertEqual(server.requests, 1, "An unchanged route must preserve the current download.")
        #endif
    }
}

private final class ConnectionServer: @unchecked Sendable {
    private let queue = DispatchQueue(label: "coconut.connection-test")
    private let lock = NSLock()
    private let listener: NWListener
    private let snapshot: Data
    private var count = 0
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
                queue.asyncAfter(deadline: .now() + .milliseconds(500)) {
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

private final class DelayedConnectionServer: @unchecked Sendable {
    private let queue = DispatchQueue(label: "coconut.timeout-test")
    private let listener: NWListener

    init() throws { listener = try NWListener(using: .tcp, on: .any) }

    func start() async throws -> String {
        listener.newConnectionHandler = { [self] connection in
            connection.start(queue: queue)
            connection.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [self] data, _, _, _ in
                guard data != nil else { connection.cancel(); return }
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
