import CryptoKit
import Foundation
import XCTest
@testable import Coconut

final class TunnelConnectionTests: XCTestCase {
    private let fast = TunnelConnection.Timing(connect: .milliseconds(300), ping: .seconds(20), silence: .seconds(45))

    private func connection(_ mac: FakeMac, credential: TunnelCredential = .token(String(repeating: "c", count: 64)),
                            phoneKey: Curve25519.KeyAgreement.PrivateKey = .init(),
                            timing: TunnelConnection.Timing? = nil,
                            onUpdateHint: (@Sendable (UInt64) -> Void)? = nil) -> TunnelConnection {
        TunnelConnection(endpoint: mac.endpoint, phoneKey: phoneKey, credential: credential, timing: timing ?? fast,
                         makeSocket: mac.factory, onUpdateHint: onUpdateHint)
    }

    // MARK: Streams

    func testConcurrentRequestsShareOneSocketOnSeparateStreams() async throws {
        let mac = FakeMac()
        // The first answer waits until the second request arrives, so both streams are open at the same time.
        mac.hold = { $0.path == "/v1/first" }
        mac.reply = { FakeMac.Reply(body: Data($0.path.utf8)) }
        let tunnel = connection(mac)
        async let first = tunnel.send(getRequest("/v1/first"), timeout: .seconds(5))
        try await waitFor { mac.requests.count == 1 }
        let second = try await tunnel.send(getRequest("/v1/second"), timeout: .seconds(5))
        XCTAssertEqual(String(decoding: second.body, as: UTF8.self), "/v1/second")
        mac.release(stream: 1)
        let firstResponse = try await first
        XCTAssertEqual(String(decoding: firstResponse.body, as: UTF8.self), "/v1/first")
        XCTAssertEqual(Set(mac.requests.keys), [1, 3])
        XCTAssertEqual(mac.socketCount, 1)
        let attempts = await tunnel.connectAttempts
        XCTAssertEqual(attempts, 1)
        let state = await tunnel.state
        XCTAssertEqual(state, .connected)
    }

    func testRequestHeadCarriesMethodPathHeadersAndBody() async throws {
        let mac = FakeMac()
        mac.reply = { request in FakeMac.Reply(status: 201, headers: ["Content-Type": "application/json"], body: request.body) }
        let tunnel = connection(mac)
        let body = Data(#"{"name":"Bot"}"#.utf8)
        let response = try await tunnel.send(TunnelRequest(method: "POST", path: "/v1/bots", headers: ["Idempotency-Key": "k"],
                                                           body: body), timeout: .seconds(5))
        XCTAssertEqual(response.status, 201)
        XCTAssertEqual(response.header("content-type"), "application/json")
        XCTAssertEqual(response.body, body)
        let request = try XCTUnwrap(mac.requests[1])
        XCTAssertEqual(request.method, "POST")
        XCTAssertEqual(request.headers["Idempotency-Key"], "k")
    }

    func testLargeResponseFollowsTheWindowThatThePhoneExtends() async throws {
        let mac = FakeMac()
        let content = Data((0..<3_200_000).map { UInt8(truncatingIfNeeded: $0 &* 31) })
        mac.reply = { _ in FakeMac.Reply(body: content) }
        let tunnel = connection(mac)
        let response = try await tunnel.send(getRequest("/v1/state"), timeout: .seconds(10))
        XCTAssertEqual(response.body, content)
        // The Mac never sends past the window, so the phone must have extended it by at least 2.2 MB.
        XCTAssertGreaterThanOrEqual(mac.windowGranted(stream: 1), content.count - Int(TunnelFrame.initialWindow))
        XCTAssertTrue(mac.frames(.window).allSatisfy { $0.stream == 1 })
    }

    func testRequestBodyStopsAtTheWindowUntilTheMacExtendsIt() async throws {
        let mac = FakeMac()
        mac.grantRequestWindow = false
        mac.reply = { FakeMac.Reply(body: Data("\($0.body.count)".utf8)) }
        let tunnel = connection(mac)
        let body = Data(count: 2_500_000)
        async let response = tunnel.send(TunnelRequest(method: "POST", path: "/v1/attachments", headers: [:], body: body),
                                         timeout: .seconds(10))
        try await waitFor { mac.requests[1]?.body.count == Int(TunnelFrame.initialWindow) }
        try await Task.sleep(for: .milliseconds(200))
        XCTAssertEqual(mac.requests[1]?.body.count, Int(TunnelFrame.initialWindow), "The phone sent past the 1 MB window.")
        mac.grantWindow(stream: 1, bytes: 2_000_000)
        let result = try await response
        XCTAssertEqual(String(decoding: result.body, as: UTF8.self), "2500000")
    }

    func testEarlyAnswerEndsAnUploadWithoutWaitingForTheWindow() async throws {
        let mac = FakeMac()
        mac.grantRequestWindow = false
        mac.answersAfterHead = true
        mac.reply = { _ in FakeMac.Reply(status: 413) }
        let tunnel = connection(mac)
        let started = ContinuousClock.now
        let response = try await tunnel.send(TunnelRequest(method: "POST", path: "/v1/attachments", headers: [:],
                                                           body: Data(count: 3_000_000)), timeout: .seconds(5))
        XCTAssertEqual(response.status, 413)
        XCTAssertLessThan(started.duration(to: .now), .seconds(2))
        XCTAssertLessThanOrEqual(mac.requests[1]?.body.count ?? 0, Int(TunnelFrame.initialWindow))
    }

    func testBodyFramesNeverExceedTheMaximumPayload() async throws {
        let mac = FakeMac()
        let tunnel = connection(mac)
        _ = try await tunnel.send(TunnelRequest(method: "POST", path: "/v1/x", headers: [:], body: Data(count: 200_000)), timeout: .seconds(5))
        let bodies = mac.frames(.body)
        XCTAssertEqual(bodies.map(\.payload.count).reduce(0, +), 200_000)
        XCTAssertTrue(bodies.allSatisfy { $0.payload.count <= TunnelFrame.maxPayload })
    }

    // MARK: Reconnect

    func testNextRequestReconnectsAfterTheSocketDrops() async throws {
        let mac = FakeMac()
        let tunnel = connection(mac)
        _ = try await tunnel.send(getRequest("/v1/state"), timeout: .seconds(5))
        mac.dropSocket()
        // A socket that ends after it opened is no fault to show: the next request connects again.
        try await waitFor { await tunnel.state == .connecting }
        let response = try await tunnel.send(getRequest("/v1/state"), timeout: .seconds(5))
        XCTAssertEqual(response.status, 200)
        XCTAssertEqual(mac.socketCount, 2)
        let state = await tunnel.state
        XCTAssertEqual(state, .connected)
        // The same phone key opens each session.
        XCTAssertEqual(Set(mac.phoneKeys).count, 1)
    }

    func testDroppedSocketFailsTheOpenStreams() async throws {
        let mac = FakeMac()
        mac.hold = { _ in true }
        let tunnel = connection(mac)
        async let response = tunnel.send(getRequest("/v1/updates?after=0&wait=20"), timeout: .seconds(5))
        try await waitFor { mac.requests.count == 1 }
        mac.dropSocket()
        do {
            _ = try await response
            XCTFail("A dropped socket must fail its streams.")
        } catch let error as TunnelError {
            XCTAssertEqual(error, .connectionLost)
            XCTAssertTrue(error.isRetryable)
        }
    }

    func testSilentSocketSendsAPingAndThenEnds() async throws {
        let mac = FakeMac()
        mac.answersPings = false
        let tunnel = connection(mac, timing: .init(connect: .seconds(1), ping: .milliseconds(100), silence: .milliseconds(400)))
        _ = try await tunnel.send(getRequest("/v1/state"), timeout: .seconds(5))
        try await waitFor { mac.textPings > 0 }
        try await waitFor { await tunnel.state == .connecting }
        XCTAssertTrue(mac.socketClosed)
    }

    func testAHostResetDuringASessionIsARetryableLossNotAnOfflineMac() async throws {
        let mac = FakeMac()
        mac.hold = { _ in true }
        let tunnel = connection(mac)
        async let poll = tunnel.send(getRequest("/v1/updates?after=0&wait=20"), timeout: .seconds(5))
        try await waitFor { mac.requests.count == 1 }
        // Cloudflare resets the Mac's relay socket. The relay sends mac_offline to the open session and closes it.
        mac.resetHost()
        do {
            _ = try await poll
            XCTFail("A host reset must fail the open streams.")
        } catch let error as TunnelError {
            XCTAssertEqual(error, .connectionLost)
            XCTAssertTrue(error.isRetryable)
        }
        let state = await tunnel.state
        XCTAssertEqual(state, .connecting)
        XCTAssertEqual(ConnectionState(TunnelError.connectionLost), .connecting)
        XCTAssertNil(ConnectionState.connecting.notice)
        // The Mac is back. The next request opens a new session.
        mac.hold = { _ in false }
        let response = try await tunnel.send(getRequest("/v1/state"), timeout: .seconds(5))
        XCTAssertEqual(response.status, 200)
        XCTAssertEqual(mac.socketCount, 2)
    }

    // MARK: States

    func testRelayMacOfflineMessageGivesTheMacOfflineState() async throws {
        let mac = FakeMac()
        mac.handshake = .relayError("mac_offline")
        let tunnel = connection(mac)
        await assertFails(tunnel, with: .macOffline)
        let state = await tunnel.state
        XCTAssertEqual(state, .macOffline)
        XCTAssertEqual(ConnectionState(TunnelError.macOffline), .macOffline)
    }

    func testMacThatRefusesThePhoneKeyNeedsARepair() async throws {
        let mac = FakeMac()
        mac.handshake = .relayError("closed")
        let tunnel = connection(mac)
        await assertFails(tunnel, with: .needsRepair)
        let state = await tunnel.state
        XCTAssertEqual(state, .needsRepair)
    }

    func testAnotherMacKeyNeedsARepair() async throws {
        let mac = FakeMac()
        let other = FakeMac()
        let tunnel = TunnelConnection(endpoint: other.endpoint, phoneKey: .init(), credential: .token("t"), timing: fast,
                                      makeSocket: mac.factory)
        await assertFails(tunnel, with: .needsRepair)
    }

    func testUnansweredHandshakeOnAnOpenSocketMeansTheMacSleeps() async throws {
        let mac = FakeMac()
        mac.handshake = .silent
        let tunnel = connection(mac)
        await assertFails(tunnel, with: .macOffline)
        mac.socketsOpen = false
        await assertFails(tunnel, with: .relayUnreachable)
        let state = await tunnel.state
        XCTAssertEqual(state, .relayUnreachable)
    }

    func testBackendDownErrorFrameFailsOnlyThatStream() async throws {
        let mac = FakeMac()
        mac.reply = { $0.path == "/v1/down" ? FakeMac.Reply(error: "backend_down") : FakeMac.Reply() }
        let tunnel = connection(mac)
        await assertFails(tunnel, path: "/v1/down", with: .backendDown)
        let response = try await tunnel.send(getRequest("/v1/up"), timeout: .seconds(5))
        XCTAssertEqual(response.status, 200)
        XCTAssertEqual(mac.socketCount, 1)
        XCTAssertEqual(ConnectionState(TunnelError.backendDown), .macOffline)
    }

    func testTimeoutCancelsTheStreamAndKeepsTheSocket() async throws {
        let mac = FakeMac()
        mac.hold = { $0.path == "/v1/slow" }
        let tunnel = connection(mac)
        do {
            _ = try await tunnel.send(getRequest("/v1/slow"), timeout: .milliseconds(300))
            XCTFail("A held answer must time out.")
        } catch let error as TunnelError {
            XCTAssertEqual(error, .timedOut)
        }
        try await waitFor { mac.cancelled.contains(1) }
        let response = try await tunnel.send(getRequest("/v1/fast"), timeout: .seconds(5))
        XCTAssertEqual(response.status, 200)
        XCTAssertEqual(mac.socketCount, 1)
    }

    func testTimeoutWithoutAnyTrafficEndsTheSocket() async throws {
        let mac = FakeMac()
        let tunnel = connection(mac)
        _ = try await tunnel.send(getRequest("/v1/state"), timeout: .seconds(5))
        // The network changed: the old socket delivers nothing any more.
        mac.goSilent()
        do {
            _ = try await tunnel.send(getRequest("/v1/state"), timeout: .milliseconds(300))
            XCTFail("A silent socket must time out.")
        } catch let error as TunnelError {
            XCTAssertEqual(error, .timedOut)
        }
        mac.goSilent(false)
        let response = try await tunnel.send(getRequest("/v1/state"), timeout: .seconds(5))
        XCTAssertEqual(response.status, 200)
        XCTAssertEqual(mac.socketCount, 2)
    }

    func testConnectionStatesMapToTheAppStates() {
        XCTAssertEqual(ConnectionState(TunnelError.needsRepair), .needsRepair)
        for error in [TunnelError.relayUnreachable, .timedOut, .protocolViolation] {
            XCTAssertEqual(ConnectionState(error), .relayUnreachable, "\(error)")
        }
        XCTAssertEqual(ConnectionState(TunnelError.connectionLost), .connecting)
        XCTAssertEqual(TunnelError.relay(code: "mac_offline", duringHandshake: true), .macOffline)
        XCTAssertEqual(TunnelError.relay(code: "mac_offline", duringHandshake: false), .connectionLost)
        XCTAssertEqual(TunnelError.relay(code: "revoked", duringHandshake: false), .needsRepair)
        XCTAssertEqual(TunnelError.relay(code: "closed", duringHandshake: false), .connectionLost)
        XCTAssertEqual(TunnelError.relay(code: "host_reconnected", duringHandshake: true), .connectionLost)
        XCTAssertNotNil(ConnectionState.macOffline.bannerMessage)
        XCTAssertNil(ConnectionState.connected.bannerMessage)
    }

    // MARK: Background

    func testCloseWhenIdleWaitsForWritesAndStopsReads() async throws {
        let mac = FakeMac()
        mac.hold = { _ in true }
        let tunnel = connection(mac)
        async let poll = tunnel.send(getRequest("/v1/updates?after=0&wait=20"), timeout: .seconds(5))
        async let write = tunnel.send(TunnelRequest(method: "POST", path: "/v1/messages", headers: [:], body: Data("{}".utf8)),
                                      timeout: .seconds(5))
        // The two requests can start in either order, so find each stream by its method.
        try await waitFor { mac.requests.values.filter(\.ended).count == 2 }
        let pollStream = try XCTUnwrap(mac.requests.values.first { $0.method == "GET" }?.stream)
        let writeStream = try XCTUnwrap(mac.requests.values.first { $0.method == "POST" }?.stream)
        let closing = Task { await tunnel.closeWhenIdle() }
        do {
            _ = try await poll
            XCTFail("Going to the background must stop the long poll.")
        } catch is CancellationError {}
        try await waitFor { mac.cancelled.contains(pollStream) }
        XCTAssertFalse(mac.socketClosed, "The socket must stay open for the write in flight.")
        mac.release(stream: writeStream)
        let response = try await write
        XCTAssertEqual(response.status, 200)
        await closing.value
        XCTAssertTrue(mac.socketClosed)
        let state = await tunnel.state
        XCTAssertEqual(state, .idle)
    }

    func testBackgroundCloseStopsReadsThatWaitForTheHandshake() async throws {
        let mac = FakeMac()
        mac.holdsHandshake = true
        let tunnel = connection(mac, timing: .init(connect: .seconds(5), ping: .seconds(20), silence: .seconds(45)))
        async let poll = tunnel.send(getRequest("/v1/updates?after=0&wait=20"), timeout: .seconds(5))
        async let write = tunnel.send(TunnelRequest(method: "POST", path: "/v1/messages", headers: [:]), timeout: .seconds(5))
        try await waitFor { mac.heldHandshakes == 1 }
        try await Task.sleep(for: .milliseconds(50))
        let closing = Task { await tunnel.closeWhenIdle() }
        do {
            _ = try await poll
            XCTFail("A read that waits for the handshake must stop when the app goes to the background.")
        } catch is CancellationError {}
        mac.releaseHandshake()
        let response = try await write
        XCTAssertEqual(response.status, 200)
        await closing.value
        XCTAssertEqual(mac.allRequests.map(\.method), ["POST"], "The stopped read must not reach the Mac.")
        XCTAssertEqual(mac.socketCount, 1, "The background close must not open another socket.")
        XCTAssertTrue(mac.socketClosed)
    }

    // The close can come while the read waits for the handshake, between the handshake and its stream, or after
    // the stream opened. In each case the read stops, and the phone opens no other socket.
    func testBackgroundCloseAsTheHandshakeEndsStopsTheReadWithoutASecondSocket() async throws {
        for attempt in 0..<25 {
            let mac = FakeMac()
            mac.holdsHandshake = true
            mac.hold = { _ in true }
            let tunnel = connection(mac, timing: .init(connect: .seconds(5), ping: .seconds(20), silence: .seconds(45)))
            let read = Task { try await tunnel.send(getRequest("/v1/updates?after=0&wait=20"), timeout: .seconds(5)) }
            try await waitFor { mac.heldHandshakes == 1 }
            mac.releaseHandshake()
            // A few turns of the executor move the read through different steps in different attempts.
            for _ in 0..<(attempt % 5) { await Task.yield() }
            await tunnel.closeWhenIdle()
            do {
                _ = try await read.value
                XCTFail("Attempt \(attempt): the read must stop.")
            } catch is CancellationError {
            } catch {
                XCTFail("Attempt \(attempt): the read failed with \(error), which APIClient would retry.")
            }
            try await Task.sleep(for: .milliseconds(20))
            XCTAssertEqual(mac.socketCount, 1, "Attempt \(attempt): the close opened another socket.")
        }
    }

    func testRequestDuringBackgroundCloseKeepsTheSocket() async throws {
        let mac = FakeMac()
        mac.hold = { $0.method == "POST" }
        let tunnel = connection(mac)
        async let write = tunnel.send(TunnelRequest(method: "POST", path: "/v1/messages", headers: [:]), timeout: .seconds(5))
        try await waitFor { mac.requests[1]?.ended == true }
        let closing = Task { await tunnel.closeWhenIdle() }
        try await Task.sleep(for: .milliseconds(50))
        _ = try await tunnel.send(getRequest("/v1/state"), timeout: .seconds(5))
        mac.release(stream: 1)
        _ = try await write
        await closing.value
        XCTAssertFalse(mac.socketClosed)
    }

    // MARK: Pairing

    func testTicketSessionUsesThePreSharedKeyAndDerivesTheClientToken() async throws {
        let secret = Data((0..<32).map { UInt8($0 * 3) })
        let mac = FakeMac()
        mac.psk = TunnelKeys.derive(secret, "coconut pairing psk")
        let tunnel = connection(mac, credential: .ticket(secret: secret))
        _ = try await tunnel.send(TunnelRequest(method: "POST", path: "/v1/pair", headers: [:]), timeout: .seconds(5))
        let ticket = TunnelKeys.derive(secret, "coconut pairing ticket").hexString
        XCTAssertEqual(mac.authorizations, ["Ticket " + ticket])
        let phoneHash = await tunnel.handshakeHash
        XCTAssertEqual(phoneHash, mac.handshakeHashes.first)
        XCTAssertEqual(TunnelKeys.clientToken(handshakeHash: try XCTUnwrap(phoneHash)).count, 64)
    }

    func testTicketSessionWithAWrongSecretGetsNoSession() async throws {
        let mac = FakeMac()
        // The pre-shared key enters at message 2, so the Mac answers, and the phone cannot read the answer.
        mac.psk = TunnelKeys.derive(Data(count: 32), "coconut pairing psk")
        let tunnel = connection(mac, credential: .ticket(secret: Data(repeating: 1, count: 32)))
        await assertFails(tunnel, with: .needsRepair)
    }

    func testTokenSessionSendsTheBearerToken() async throws {
        let mac = FakeMac()
        _ = try await connection(mac, credential: .token("abc")).send(getRequest("/health"), timeout: .seconds(5))
        XCTAssertEqual(mac.authorizations, ["Bearer abc"])
    }

    // MARK: Update hints

    func testAnUpdateHintReachesTheAppAndKeepsTheSession() async throws {
        let mac = FakeMac()
        let hints = HintLog()
        let tunnel = connection(mac) { hints.add($0) }
        _ = try await tunnel.send(getRequest("/v1/updates?after=1"), timeout: .seconds(5))
        mac.sendHint(cursor: 42)
        mac.sendHint(cursor: 43)
        try await waitFor { hints.values == [42, 43] }
        let response = try await tunnel.send(getRequest("/health"), timeout: .seconds(5))
        XCTAssertEqual(response.status, 200)
        XCTAssertEqual(mac.socketCount, 1, "A hint keeps the socket.")
    }

    func testAPIClientAsksForHintsOnlyWithUpdateReads() async throws {
        let mac = FakeMac()
        mac.reply = { _ in FakeMac.Reply(body: Data(#"{"ok":true}"#.utf8)) }
        let client = APIClient(tunnel: connection(mac), token: "t")
        let _: [String: Bool] = try await client.request("updates?after=3")
        let _: [String: Bool] = try await client.request("state")
        XCTAssertEqual(mac.allRequests.map { $0.headers["X-Coconut-Hints"] }, ["1", nil])
    }

    // MARK: APIClient

    func testAPIClientDecodesGzipAndSendsItsHeaders() async throws {
        let mac = FakeMac()
        let json = Data(#"{"ok":true}"#.utf8)
        mac.reply = { _ in FakeMac.Reply(headers: ["Content-Encoding": "gzip"], body: try! Self.gzip(json)) }
        let client = APIClient(tunnel: connection(mac), token: "device-token")
        let result: [String: Bool] = try await client.request("state")
        XCTAssertEqual(result, ["ok": true])
        let request = try XCTUnwrap(mac.requests[1])
        XCTAssertEqual(request.path, "/v1/state")
        XCTAssertEqual(request.headers["Authorization"], "Bearer device-token")
        XCTAssertEqual(request.headers["Accept-Encoding"], "gzip")
        XCTAssertEqual(request.headers["X-Coconut-State"], "recent")
    }

    func testAPIClientTurnsARevokedDeviceIntoNeedsRepair() async throws {
        let mac = FakeMac()
        mac.reply = { _ in FakeMac.Reply(status: 401, body: Data(#"{"error":"Pair again"}"#.utf8)) }
        let client = APIClient(tunnel: connection(mac), token: "old")
        do {
            let _: [String: Bool] = try await client.request("state")
            XCTFail("A 401 must ask for a new pairing.")
        } catch let error as TunnelError {
            XCTAssertEqual(error, .needsRepair)
        }
    }

    func testAPIClientRetriesAWriteWithTheSameIdempotencyKey() async throws {
        let mac = FakeMac()
        var first = true
        mac.reply = { _ in
            defer { first = false }
            return first ? FakeMac.Reply(drop: true) : FakeMac.Reply(body: Data(#"{"ok":true}"#.utf8))
        }
        let client = APIClient(tunnel: connection(mac), token: "t")
        let result: [String: Bool] = try await client.request("messages", method: "POST", body: Data("{}".utf8))
        XCTAssertEqual(result, ["ok": true])
        XCTAssertEqual(mac.allRequests.count, 2)
        XCTAssertEqual(mac.allRequests[0].headers["Idempotency-Key"], mac.allRequests[1].headers["Idempotency-Key"])
    }

    func testTunnelPathsStayOnTheMac() throws {
        let client = APIClient(tunnel: connection(FakeMac()), token: nil)
        XCTAssertEqual(try client.makeTunnelRequest("search?q=a%20b").path, "/v1/search?q=a%20b")
        for path in ["state\r\nX: y", "search?q=a b"] {
            XCTAssertThrowsError(try client.makeTunnelRequest(path), path)
        }
        XCTAssertTrue(APIClient.isShortRead("updates?after=3", method: "GET", fullState: false))
        XCTAssertFalse(APIClient.isShortRead("updates?after=3&wait=20", method: "GET", fullState: false))
        XCTAssertFalse(APIClient.isShortRead("state", method: "GET", fullState: true))
    }

    func testGzipRejectsDamagedData() throws {
        let good = try Self.gzip(Data("hello".utf8))
        XCTAssertEqual(Gzip.decompress(good), Data("hello".utf8))
        var damaged = good
        damaged[damaged.count - 1] ^= 1
        XCTAssertNil(Gzip.decompress(damaged))
        XCTAssertNil(Gzip.decompress(Data("plain".utf8)))
    }

    // MARK: Helpers

    // A minimal gzip member: header, raw DEFLATE, CRC-32 (unchecked by the app), and the size.
    static func gzip(_ data: Data) throws -> Data {
        let deflated = try (data as NSData).compressed(using: .zlib) as Data
        var output = Data([0x1f, 0x8b, 8, 0, 0, 0, 0, 0, 0, 0xff])
        output.append(deflated)
        output.append(contentsOf: [0, 0, 0, 0])
        withUnsafeBytes(of: UInt32(data.count).littleEndian) { output.append(contentsOf: $0) }
        return output
    }

    private func assertFails(_ tunnel: TunnelConnection, path: String = "/v1/state", with expected: TunnelError,
                             file: StaticString = #filePath, line: UInt = #line) async {
        do {
            _ = try await tunnel.send(getRequest(path), timeout: .seconds(5))
            XCTFail("The request must fail with \(expected).", file: file, line: line)
        } catch {
            XCTAssertEqual(error as? TunnelError, expected, "\(error)", file: file, line: line)
        }
    }

    private func waitFor(_ condition: @escaping () async -> Bool, timeout: Duration = .seconds(5)) async throws {
        let deadline = ContinuousClock.now + timeout
        while !(await condition()) {
            guard ContinuousClock.now < deadline else { return XCTFail("The condition did not hold in time.") }
            try await Task.sleep(for: .milliseconds(10))
        }
    }
}

private func getRequest(_ path: String) -> TunnelRequest {
    TunnelRequest(method: "GET", path: path, headers: ["Accept": "application/json"])
}

// MARK: - In-memory relay socket

final class MemorySocket: TunnelSocket, @unchecked Sendable {
    private let lock = NSLock()
    private var inbox: [TunnelSocketMessage] = []
    private var waiters: [CheckedContinuation<TunnelSocketMessage, Error>] = []
    private var closed = false
    private let opened: Bool
    private var _peer: (@Sendable (TunnelSocketMessage) -> Void)?

    init(opened: Bool) { self.opened = opened }

    var peer: (@Sendable (TunnelSocketMessage) -> Void)? {
        get { lock.withLock { _peer } }
        set { lock.withLock { _peer = newValue } }
    }

    var isOpen: Bool { opened }
    var isClosed: Bool { lock.withLock { closed } }

    func send(_ message: TunnelSocketMessage) async throws {
        guard !isClosed else { throw TunnelError.connectionLost }
        peer?(message)
    }

    func receive() async throws -> TunnelSocketMessage {
        try await withCheckedThrowingContinuation { continuation in
            lock.lock()
            if !inbox.isEmpty {
                let message = inbox.removeFirst()
                lock.unlock()
                continuation.resume(returning: message)
            } else if closed {
                lock.unlock()
                continuation.resume(throwing: TunnelError.connectionLost)
            } else {
                waiters.append(continuation)
                lock.unlock()
            }
        }
    }

    // A message from the relay.
    func deliver(_ message: TunnelSocketMessage) {
        lock.lock()
        guard !closed else { return lock.unlock() }
        if waiters.isEmpty {
            inbox.append(message)
            lock.unlock()
        } else {
            let waiter = waiters.removeFirst()
            lock.unlock()
            waiter.resume(returning: message)
        }
    }

    func close() {
        lock.lock()
        closed = true
        let pending = waiters
        waiters = []
        lock.unlock()
        pending.forEach { $0.resume(throwing: TunnelError.connectionLost) }
    }
}

// MARK: - A Mac sidecar in memory

final class FakeMac: @unchecked Sendable {
    struct Request {
        var stream: UInt32
        var method: String
        var path: String
        var headers: [String: String]
        var body = Data()
        var ended = false
        var windowSent = 0
    }

    struct Reply {
        var status = 200
        var headers: [String: String] = [:]
        var body = Data()
        var error: String?
        // Closes the socket instead of an answer.
        var drop = false
    }

    enum Handshake { case accept, silent, relayError(String) }

    private let lock = NSRecursiveLock()
    private let key = Curve25519.KeyAgreement.PrivateKey()
    var handshake = Handshake.accept
    var psk: Data?
    var grantRequestWindow = true
    var answersPings = true
    // Keeps message 1 until releaseHandshake(), as a slow relay.
    var holdsHandshake = false
    private var heldHandshake: (message: Data, socket: MemorySocket)?
    // Answers when the request head arrives, before the body, as a backend that refuses a large upload.
    var answersAfterHead = false
    var socketsOpen = true
    var hold: (Request) -> Bool = { _ in false }
    var reply: (Request) -> Reply = { _ in Reply() }

    private var sockets: [MemorySocket] = []
    private var silent = false
    private var session: TunnelSession?
    private var _requests: [UInt32: Request] = [:]
    private var _allRequests: [Request] = []
    private var _frames: [TunnelFrame] = []
    private var _cancelled: Set<UInt32> = []
    private var _authorizations: [String] = []
    private var _phoneKeys: [Data] = []
    private var _hashes: [Data] = []
    private var _pings = 0
    private var granted: [UInt32: Int] = [:]
    private var outgoing: [UInt32: (body: Data, offset: Int, window: Int)] = [:]

    var endpoint: TunnelEndpoint {
        TunnelEndpoint(relay: URL(string: "https://relay.test")!, route: String(repeating: "0", count: 64),
                       macKey: key.publicKey.rawRepresentation)
    }

    var factory: TunnelSocketFactory {
        { [self] request in connect(request) }
    }

    private func locked<T>(_ body: () -> T) -> T { lock.lock(); defer { lock.unlock() }; return body() }

    var requests: [UInt32: Request] { locked { _requests } }
    var allRequests: [Request] { locked { _allRequests } }
    var cancelled: Set<UInt32> { locked { _cancelled } }
    var authorizations: [String] { locked { _authorizations } }
    var phoneKeys: [Data] { locked { _phoneKeys } }
    var handshakeHashes: [Data] { locked { _hashes } }
    var textPings: Int { locked { _pings } }
    var socketCount: Int { locked { sockets.count } }
    var socketClosed: Bool { locked { sockets.last?.isClosed ?? false } }
    func frames(_ type: TunnelFrameType) -> [TunnelFrame] { locked { _frames.filter { $0.type == type } } }
    func windowGranted(stream: UInt32) -> Int { locked { granted[stream] ?? 0 } }

    private func connect(_ request: URLRequest) -> any TunnelSocket {
        locked {
            _authorizations.append(request.value(forHTTPHeaderField: "Authorization") ?? "")
            session = nil
            _requests = [:]
            outgoing = [:]
            let socket = MemorySocket(opened: socketsOpen)
            socket.peer = { [weak self, unowned socket] message in self?.receive(message, from: socket) }
            sockets.append(socket)
            return socket
        }
    }

    func dropSocket() { locked { sockets.last?.close() } }

    // The relay lost the Mac's socket: it sends mac_offline to the open session and closes it.
    func resetHost() {
        locked {
            sockets.last?.deliver(.text(#"{"type":"error","code":"mac_offline"}"#))
            sockets.last?.close()
        }
    }

    var heldHandshakes: Int { locked { heldHandshake == nil ? 0 : 1 } }

    func releaseHandshake() {
        locked {
            holdsHandshake = false
            if let held = heldHandshake { handshake(held.message, held.socket) }
            heldHandshake = nil
        }
    }

    // The Mac reads and answers nothing, as after a network change.
    func goSilent(_ silent: Bool = true) { locked { self.silent = silent } }

    func release(stream: UInt32) { locked { answer(stream) } }

    func grantWindow(stream: UInt32, bytes: Int) {
        locked { send(.window(UInt32(bytes), stream: stream)) }
    }

    func sendHint(cursor: UInt64) { locked { send(.updateHint(cursor: cursor)) } }

    private var current: MemorySocket? { sockets.last }

    private func receive(_ message: TunnelSocketMessage, from socket: MemorySocket) {
        locked {
            guard socket === current, !silent else { return }
            switch message {
            case .text(let text):
                guard text == "ping" else { return }
                _pings += 1
                if answersPings { socket.deliver(.text("pong")) }
            case .data(let data):
                if let session {
                    guard let frame = try? session.open(data) else { return socket.close() }
                    handle(frame)
                } else if holdsHandshake {
                    heldHandshake = (data, socket)
                } else {
                    handshake(data, socket)
                }
            }
        }
    }

    private func handshake(_ message: Data, _ socket: MemorySocket) {
        switch handshake {
        case .silent: return
        case .relayError(let code):
            socket.deliver(.text(#"{"type":"error","code":"\#(code)"}"#))
            socket.close()
            return
        case .accept:
            do {
                var responder = try NoiseHandshake(pattern: psk == nil ? .ik : .ikpsk2, initiator: false, localStatic: key, psk: psk)
                _ = try responder.readMessage(message)
                _phoneKeys.append(responder.remoteStatic!)
                let reply = try responder.writeMessage()
                let transport = try responder.transport()
                _hashes.append(transport.handshakeHash)
                session = TunnelSession(transport: transport)
                socket.deliver(.data(reply))
            } catch {
                // Message 1 for another Mac key does not decrypt. The sidecar closes the channel.
                socket.deliver(.text(#"{"type":"error","code":"closed"}"#))
                socket.close()
            }
        }
    }

    private func handle(_ frame: TunnelFrame) {
        _frames.append(frame)
        let stream = frame.stream
        switch frame.type {
        case .requestHead:
            let head = try! JSONSerialization.jsonObject(with: frame.payload) as! [String: Any]
            _requests[stream] = Request(stream: stream, method: head["method"] as! String, path: head["path"] as! String,
                                        headers: head["headers"] as? [String: String] ?? [:])
            if answersAfterHead { answer(stream) }
        case .body:
            _requests[stream]?.body.append(frame.payload)
            if grantRequestWindow, let request = _requests[stream], request.body.count - request.windowSent >= 256 * 1024 {
                let bytes = request.body.count - request.windowSent
                _requests[stream]?.windowSent = request.body.count
                send(.window(UInt32(bytes), stream: stream))
            }
        case .end:
            _requests[stream]?.ended = true
            if !answersAfterHead, let request = _requests[stream] {
                _allRequests.append(request)
                if !hold(request) { answer(stream) }
            }
        case .window:
            let bytes = frame.payload.reduce(0) { $0 << 8 | Int($1) }
            granted[stream, default: 0] += bytes
            outgoing[stream]?.window += bytes
            pump(stream)
        case .cancel:
            _cancelled.insert(stream)
            outgoing[stream] = nil
        case .ping:
            send(TunnelFrame(type: .pong, stream: 0))
        default:
            break
        }
    }

    private func answer(_ stream: UInt32) {
        guard let request = _requests[stream], !_cancelled.contains(stream) else { return }
        let result = reply(request)
        if result.drop {
            current?.close()
            return
        }
        if let code = result.error {
            return send(TunnelFrame(type: .error, stream: stream, payload: Data(#"{"code":"\#(code)"}"#.utf8)))
        }
        let head = try! JSONSerialization.data(withJSONObject: ["status": result.status, "headers": result.headers])
        send(TunnelFrame(type: .responseHead, stream: stream, payload: head))
        outgoing[stream] = (result.body, 0, Int(TunnelFrame.initialWindow))
        pump(stream)
    }

    // The Mac sends a response body only inside the window that the phone grants.
    private func pump(_ stream: UInt32) {
        guard var state = outgoing[stream] else { return }
        while state.offset < state.body.count, state.window > 0 {
            let count = min(TunnelFrame.maxPayload, state.window, state.body.count - state.offset)
            send(TunnelFrame(type: .body, stream: stream, payload: state.body.subdata(in: state.offset..<state.offset + count)))
            state.offset += count
            state.window -= count
        }
        if state.offset == state.body.count {
            send(TunnelFrame(type: .end, stream: stream))
            outgoing[stream] = nil
        } else {
            outgoing[stream] = state
        }
    }

    private func send(_ frame: TunnelFrame) {
        guard let session, let socket = current else { return }
        for ciphertext in try! session.seal(frame) { socket.deliver(.data(ciphertext)) }
    }
}

final class HintLog: @unchecked Sendable {
    private let lock = NSLock()
    private var _values: [UInt64] = []
    var values: [UInt64] { lock.withLock { _values } }
    func add(_ value: UInt64) { lock.withLock { _values.append(value) } }
}
