import CryptoKit
import Foundation

// The iPhone end of the relay tunnel (docs/relay-transport.md). One WebSocket carries all requests as streams of
// TunnelFrames inside a Noise session. This file uses Foundation and CryptoKit only, so the loopback test client
// (scripts/tunnel-test-client.swift) compiles it with ios/Tunnel on the Mac.

struct TunnelEndpoint: Equatable, Sendable {
    let relay: URL
    let route: String
    let macKey: Data

    // The relay serves the client socket on the same host as its HTTP routes.
    var clientURL: URL {
        var parts = URLComponents(url: relay, resolvingAgainstBaseURL: false)!
        parts.scheme = parts.scheme == "http" ? "ws" : "wss"
        parts.path = "/v1/tunnels/\(route)/client"
        return parts.url!
    }
}

enum TunnelCredential: Sendable {
    // The relay client token that the pairing session derived. Normal sessions use Noise IK.
    case token(String)
    // The one-time secret from a pairing link. The pairing session uses Noise IKpsk2.
    case ticket(secret: Data)

    var authorization: String {
        switch self {
        case .token(let token): "Bearer " + token
        case .ticket(let secret): "Ticket " + TunnelKeys.derive(secret, "coconut pairing ticket").hexString
        }
    }

    var pattern: NoisePattern {
        if case .ticket = self { .ikpsk2 } else { .ik }
    }

    var psk: Data? {
        if case .ticket(let secret) = self { TunnelKeys.derive(secret, "coconut pairing psk") } else { nil }
    }
}

enum TunnelKeys {
    // HKDF-SHA256 with an empty salt and a 32-byte output, as the Mac derives it.
    static func derive(_ input: Data, _ label: String) -> Data {
        HKDF<SHA256>.deriveKey(inputKeyMaterial: SymmetricKey(data: input), info: Data(label.utf8), outputByteCount: 32)
            .withUnsafeBytes { Data($0) }
    }

    static func clientToken(handshakeHash: Data) -> String {
        derive(handshakeHash, "coconut relay client").hexString
    }
}

enum TunnelError: Error, Equatable, Sendable {
    // The relay has no socket from the Mac, or the Mac does not answer the handshake.
    case macOffline
    // The Mac answered, but the Coconut service on it did not.
    case backendDown
    // The phone cannot open a socket to the relay, or the relay limits it.
    case relayUnreachable
    // The relay or the Mac refuses this phone's credentials. Only a new pairing link helps.
    case needsRepair
    case timedOut
    case connectionLost
    case tooLarge
    case protocolViolation

    // These failures can pass on a second attempt with a new socket.
    var isRetryable: Bool { [.relayUnreachable, .connectionLost, .timedOut].contains(self) }

    // The relay sends `{"type":"error","code":c}` as a text message before it closes the socket.
    static func relay(code: String, duringHandshake: Bool) -> TunnelError {
        switch code {
        // During a handshake, the relay says this after it waits 5 seconds for a Mac. On an open session, it means that
        // the Mac's relay socket ended. Cloudflare resets that socket from time to time. The Mac connects again at once,
        // and the relay holds the next handshake until then, so a second attempt usually works.
        case "mac_offline": duringHandshake ? .macOffline : .connectionLost
        case "revoked": .needsRepair
        // The Mac closes the channel when it does not know the phone key or the pairing ticket.
        case "closed": duringHandshake ? .needsRepair : .connectionLost
        default: .connectionLost
        }
    }

    var message: String {
        switch self {
        case .macOffline: "Your Mac is offline. Coconut shows the last known state and connects again when the Mac is back."
        case .backendDown: "Coconut is not running on your Mac. It connects again when the service is back."
        case .relayUnreachable: "Coconut cannot reach the relay. Check the internet connection of this iPhone."
        case .needsRepair: "This iPhone needs a new pairing. Ask your Hermes for a new pairing link."
        case .timedOut: "Your Mac did not answer in time. Coconut tries again."
        case .connectionLost: "The connection to your Mac ended. Coconut connects again."
        case .tooLarge: "This request is too large for the connection to your Mac."
        case .protocolViolation: "The connection to your Mac sent unexpected data. Coconut connects again."
        }
    }
}

struct TunnelRequest: Sendable {
    var method: String
    var path: String
    var headers: [String: String]
    var body = Data()
}

struct TunnelResponse: Sendable {
    var status: Int
    var headers: [String: String]
    var body: Data

    func header(_ name: String) -> String? {
        headers.first { $0.key.caseInsensitiveCompare(name) == .orderedSame }?.value
    }

    // URLSession is not in the path, so the phone removes the gzip encoding itself.
    func decodedBody() throws -> Data {
        guard header("Content-Encoding")?.lowercased() == "gzip" else { return body }
        guard let data = Gzip.decompress(body) else { throw TunnelError.protocolViolation }
        return data
    }
}

enum Gzip {
    // RFC 1952: a header, raw DEFLATE data, then CRC-32 and the size. NSData decompresses the raw DEFLATE data.
    static func decompress(_ data: Data, limit: Int = TunnelConnection.maxResponse) -> Data? {
        let base = data.startIndex, count = data.count
        func byte(_ index: Int) -> UInt8 { data[base + index] }
        guard count >= 18, byte(0) == 0x1f, byte(1) == 0x8b, byte(2) == 8 else { return nil }
        let flags = byte(3)
        var offset = 10
        if flags & 0x04 != 0 {
            guard count >= offset + 2 else { return nil }
            offset += 2 + Int(byte(offset)) + Int(byte(offset + 1)) << 8
        }
        for flag in [UInt8(0x08), 0x10] where flags & flag != 0 {
            guard offset < count, let end = data[(base + offset)...].firstIndex(of: 0) else { return nil }
            offset = end - base + 1
        }
        if flags & 0x02 != 0 { offset += 2 }
        guard offset <= count - 8 else { return nil }
        let size = (1...4).reduce(0) { $0 << 8 | Int(byte(count - $1)) }
        // The size field comes before the work, so a small body cannot inflate past the limit.
        guard size <= limit,
              let output = try? (data[(base + offset)..<(base + count - 8)] as NSData).decompressed(using: .zlib) as Data,
              output.count == size else { return nil }
        return output
    }
}

enum TunnelSocketMessage: Equatable, Sendable {
    case data(Data)
    case text(String)
}

protocol TunnelSocket: AnyObject, Sendable {
    // True after the relay accepted the WebSocket upgrade.
    var isOpen: Bool { get }
    func send(_ message: TunnelSocketMessage) async throws
    func receive() async throws -> TunnelSocketMessage
    // Ends the socket. A pending receive throws.
    func close()
}

typealias TunnelSocketFactory = @Sendable (URLRequest) -> any TunnelSocket

final class URLSessionTunnelSocket: NSObject, TunnelSocket, URLSessionWebSocketDelegate, @unchecked Sendable {
    private let lock = NSLock()
    private var opened = false
    private var session: URLSession!
    private var task: URLSessionWebSocketTask!

    static let factory: TunnelSocketFactory = { URLSessionTunnelSocket(request: $0) }

    init(request: URLRequest) {
        super.init()
        let configuration = URLSessionConfiguration.ephemeral
        configuration.urlCache = nil
        configuration.httpCookieStorage = nil
        configuration.httpShouldSetCookies = false
        configuration.waitsForConnectivity = false
        // TunnelConnection pings after 20 silent seconds, so a live socket is never idle this long.
        configuration.timeoutIntervalForRequest = 60
        session = URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
        task = session.webSocketTask(with: request)
        // One Noise message is at most 65,535 bytes.
        task.maximumMessageSize = 128 * 1024
        task.resume()
    }

    var isOpen: Bool { lock.withLock { opened } }

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask, didOpenWithProtocol protocol: String?) {
        lock.withLock { opened = true }
    }

    func send(_ message: TunnelSocketMessage) async throws {
        do {
            switch message {
            case .data(let data): try await task.send(.data(data))
            case .text(let text): try await task.send(.string(text))
            }
        } catch { throw failure() }
    }

    func receive() async throws -> TunnelSocketMessage {
        do {
            switch try await task.receive() {
            case .data(let data): return .data(data)
            case .string(let text): return .text(text)
            @unknown default: throw TunnelError.protocolViolation
            }
        } catch let error as TunnelError { throw error } catch { throw failure() }
    }

    // The relay answers an unknown route (404) or an unknown token or ticket (401) before the upgrade.
    private func failure() -> TunnelError {
        if let response = task.response as? HTTPURLResponse, response.statusCode != 101 {
            return [401, 404].contains(response.statusCode) ? .needsRepair : .relayUnreachable
        }
        return isOpen ? .connectionLost : .relayUnreachable
    }

    func close() {
        task.cancel(with: .normalClosure, reason: nil)
        session.invalidateAndCancel()
    }
}

// The one socket of a paired phone. Each request is a stream with its own 1 MB windows in both directions.
// A failed socket fails its streams; the next request opens a new socket with a new handshake.
actor TunnelConnection {
    enum State: Equatable, Sendable {
        case idle, connecting, connected, macOffline, relayUnreachable, needsRepair

        init(_ error: TunnelError) {
            switch error {
            case .macOffline, .backendDown: self = .macOffline
            case .needsRepair: self = .needsRepair
            // An open socket ended. The next request connects again, so the app does not show a fault.
            case .connectionLost: self = .connecting
            default: self = .relayUnreachable
            }
        }
    }

    struct Timing: Sendable {
        var connect: Duration = .seconds(6)
        // The phone sends a text `ping` after this much silence. The relay answers it without the Mac.
        var ping: Duration = .seconds(20)
        // A socket that stays silent this long has failed.
        var silence: Duration = .seconds(45)
    }

    static let shortTimeout: Duration = .seconds(6)
    static let longTimeout: Duration = .seconds(45)
    // The phone extends the Mac's response window after it reads this many bytes.
    static let windowStep = 256 * 1024
    static let maxResponse = 32 * 1024 * 1024

    nonisolated let endpoint: TunnelEndpoint
    private let phoneKey: Curve25519.KeyAgreement.PrivateKey
    private let credential: TunnelCredential
    private let makeSocket: TunnelSocketFactory
    private let timing: Timing
    // Gets the cursor of each UPDATE_HINT frame from the Mac. See docs/relay-transport.md.
    private let onUpdateHint: (@Sendable (UInt64) -> Void)?

    private(set) var state: State = .idle
    private(set) var handshakeHash: Data?
    // The number of handshakes that this connection started. Tests read it.
    private(set) var connectAttempts = 0
    private var link: Link?
    private var generation = 0
    private var connecting = false
    private var connectWaiters: [UInt32: CheckedContinuation<Void, Error>] = [:]
    // Every open request, from its start to its end, and whether it writes. A request is here also while it
    // waits for the handshake, or between the handshake and its stream, so the background close sees it.
    private var openRequests: [UInt32: Bool] = [:]
    // Open requests that abandon() stopped before they had a stream.
    private var abandoned: Set<UInt32> = []
    private var streams: [UInt32: Stream] = [:]
    private var nextStream: UInt32 = 1
    private var closeRequest: UUID?
    private var idleWaiters: [CheckedContinuation<Void, Never>] = []

    init(endpoint: TunnelEndpoint, phoneKey: Curve25519.KeyAgreement.PrivateKey, credential: TunnelCredential,
         timing: Timing = Timing(), makeSocket: @escaping TunnelSocketFactory = URLSessionTunnelSocket.factory,
         onUpdateHint: (@Sendable (UInt64) -> Void)? = nil) {
        self.endpoint = endpoint
        self.phoneKey = phoneKey
        self.credential = credential
        self.timing = timing
        self.makeSocket = makeSocket
        self.onUpdateHint = onUpdateHint
    }

    private final class Link {
        let generation: Int
        let socket: any TunnelSocket
        let session: TunnelSession
        let outbox: AsyncStream<TunnelSocketMessage>.Continuation
        var tasks: [Task<Void, Never>] = []
        var lastReceived = ContinuousClock.now
        var pendingWrites = 0

        init(generation: Int, socket: any TunnelSocket, session: TunnelSession,
             outbox: AsyncStream<TunnelSocketMessage>.Continuation) {
            self.generation = generation
            self.socket = socket
            self.session = session
            self.outbox = outbox
        }
    }

    private final class Stream {
        let id: UInt32
        let generation: Int
        let isWrite: Bool
        var sendWindow = Int(TunnelFrame.initialWindow)
        var headSent = false
        var status: Int?
        var headers: [String: String] = [:]
        var body = Data()
        var unacknowledged = 0
        var finished = false
        var failure: Error?
        var waiter: CheckedContinuation<Void, Never>?

        init(id: UInt32, generation: Int, isWrite: Bool) {
            self.id = id
            self.generation = generation
            self.isWrite = isWrite
        }

        func wake() {
            waiter?.resume()
            waiter = nil
        }
    }

    // MARK: Requests

    func send(_ request: TunnelRequest, timeout: Duration) async throws -> TunnelResponse {
        // A new request keeps the socket open, also when the app was about to close it.
        closeRequest = nil
        let id = nextStream
        nextStream = nextStream >= UInt32.max - 2 ? 1 : nextStream + 2
        let started = ContinuousClock.now
        do {
            return try await Self.deadline(timeout) { [self] in try await perform(id, request) }
        } catch TunnelError.timedOut {
            // Nothing arrived during the whole request: after a network change the socket is gone, so the next
            // request opens a new one instead of waiting for the silence limit.
            if let link, link.lastReceived < started { fail(link.generation, .connectionLost) }
            throw TunnelError.timedOut
        }
    }

    private static func deadline<T: Sendable>(_ timeout: Duration, _ operation: @escaping @Sendable () async throws -> T) async throws -> T {
        try await withThrowingTaskGroup(of: T?.self) { group in
            group.addTask { try await operation() }
            group.addTask { try await Task.sleep(for: timeout); return nil }
            defer { group.cancelAll() }
            guard let first = try await group.next(), let value = first else { throw TunnelError.timedOut }
            return value
        }
    }

    private func perform(_ id: UInt32, _ request: TunnelRequest) async throws -> TunnelResponse {
        try await withTaskCancellationHandler {
            try await exchange(id, request)
        } onCancel: {
            Task { await self.abandon(id) }
        }
    }

    private func exchange(_ id: UInt32, _ request: TunnelRequest) async throws -> TunnelResponse {
        openRequests[id] = request.method != "GET"
        defer {
            openRequests[id] = nil
            abandoned.remove(id)
            wakeIdleWaiters()
        }
        do {
            return try await streamExchange(id, request)
        } catch where abandoned.contains(id) {
            // A stopped request can also see the closed socket. It must end as cancelled, never as connectionLost,
            // which APIClient would retry on a new socket.
            throw CancellationError()
        }
    }

    private func streamExchange(_ id: UInt32, _ request: TunnelRequest) async throws -> TunnelResponse {
        let generation = try await connectedGeneration(for: id)
        try Task.checkCancellation()
        if abandoned.contains(id) { throw CancellationError() }
        let stream = Stream(id: id, generation: generation, isWrite: request.method != "GET")
        streams[id] = stream
        defer { streams[id] = nil }
        let head = try JSONSerialization.data(withJSONObject: ["method": request.method, "path": request.path,
                                                               "headers": request.headers])
        try send(TunnelFrame(type: .requestHead, stream: id, payload: head), on: generation)
        stream.headSent = true
        var offset = 0
        // The Mac can answer before it reads the whole body, for example with 413. Then the rest stays here.
        while offset < request.body.count, !stream.finished {
            while stream.sendWindow <= 0, !stream.finished { try await wait(stream) }
            guard !stream.finished else { break }
            let count = min(request.body.count - offset, stream.sendWindow, TunnelFrame.maxPayload)
            let start = request.body.startIndex + offset
            try send(TunnelFrame(type: .body, stream: id, payload: request.body[start..<start + count]), on: generation)
            offset += count
            stream.sendWindow -= count
            if let failure = stream.failure { throw failure }
        }
        if !stream.finished { try send(TunnelFrame(type: .end, stream: id), on: generation) }
        while !stream.finished { try await wait(stream) }
        guard let status = stream.status else { throw TunnelError.protocolViolation }
        return TunnelResponse(status: status, headers: stream.headers, body: stream.body)
    }

    private func wait(_ stream: Stream) async throws {
        if let failure = stream.failure { throw failure }
        await withCheckedContinuation { stream.waiter = $0 }
        if let failure = stream.failure { throw failure }
    }

    // A cancelled or expired request stops its stream. The Mac gets CANCEL, so it stops the response.
    private func abandon(_ id: UInt32) {
        if openRequests[id] != nil { abandoned.insert(id) }
        if let waiter = connectWaiters.removeValue(forKey: id) { waiter.resume(throwing: CancellationError()) }
        guard let stream = streams[id], !stream.finished, stream.failure == nil else { return }
        stream.failure = CancellationError()
        if stream.headSent { try? send(TunnelFrame(type: .cancel, stream: id), on: stream.generation) }
        stream.wake()
    }

    private func send(_ frame: TunnelFrame, on generation: Int) throws {
        guard let link, link.generation == generation else { throw TunnelError.connectionLost }
        let ciphertexts: [Data]
        do { ciphertexts = try link.session.seal(frame) } catch {
            fail(generation, .protocolViolation)
            throw TunnelError.protocolViolation
        }
        // One writer task sends in order, so the Mac reads the nonces in the order that the session used them.
        for ciphertext in ciphertexts {
            link.pendingWrites += 1
            link.outbox.yield(.data(ciphertext))
        }
    }

    // MARK: Connection

    private func connectedGeneration(for id: UInt32) async throws -> Int {
        if let link { return link.generation }
        if !connecting {
            connecting = true
            Task { await openLink() }
        }
        try await withCheckedThrowingContinuation { connectWaiters[id] = $0 }
        guard let link else { throw TunnelError.connectionLost }
        return link.generation
    }

    private func openLink() async {
        connectAttempts += 1
        generation += 1
        let generation = generation
        state = .connecting
        var request = URLRequest(url: endpoint.clientURL)
        request.setValue(credential.authorization, forHTTPHeaderField: "Authorization")
        let socket = makeSocket(request)
        var result: Result<Void, Error> = .success(())
        do {
            var handshake = try NoiseHandshake(pattern: credential.pattern, initiator: true, localStatic: phoneKey,
                                               remoteStatic: endpoint.macKey, psk: credential.psk)
            let reply = try await handshakeReply(socket, first: try handshake.writeMessage())
            do { _ = try handshake.readMessage(reply) } catch {
                // Another Mac key: the Mac was set up again, so the pairing no longer matches.
                throw TunnelError.needsRepair
            }
            let transport = try handshake.transport()
            handshakeHash = transport.handshakeHash
            let session = TunnelSession(transport: transport)
            // close() during the handshake: the app no longer wants this socket.
            guard self.generation == generation else { throw CancellationError() }
            install(Link(generation: generation, socket: socket, session: session, outbox: outbox(for: socket, generation)))
        } catch is CancellationError {
            socket.close()
            result = .failure(TunnelError.connectionLost)
        } catch {
            socket.close()
            let failure = (error as? TunnelError) ?? .protocolViolation
            state = State(failure)
            result = .failure(failure)
        }
        connecting = false
        let waiters = connectWaiters
        connectWaiters = [:]
        for waiter in waiters.values { waiter.resume(with: result) }
        wakeIdleWaiters()
    }

    // The connect budget covers the upgrade, message 1, and the answer.
    private func handshakeReply(_ socket: any TunnelSocket, first message: Data) async throws -> Data {
        let expired = Flag()
        let timer = Task { [timing] in
            try await Task.sleep(for: timing.connect)
            expired.set()
            socket.close()
        }
        defer { timer.cancel() }
        do {
            try await socket.send(.data(message))
            switch try await socket.receive() {
            case .data(let data): return data
            case .text(let text): throw TunnelError.relay(code: Self.relayCode(text), duringHandshake: true)
            }
        } catch {
            // An open socket without an answer means that the relay still has a socket from a Mac that sleeps.
            if expired.value { throw socket.isOpen ? TunnelError.macOffline : TunnelError.relayUnreachable }
            throw error
        }
    }

    private func outbox(for socket: any TunnelSocket, _ generation: Int) -> AsyncStream<TunnelSocketMessage>.Continuation {
        let (messages, continuation) = AsyncStream<TunnelSocketMessage>.makeStream()
        Task {
            for await message in messages {
                do { try await socket.send(message) } catch {
                    fail(generation, (error as? TunnelError) ?? .connectionLost)
                    return
                }
                if case .data = message { wrote(generation) }
            }
        }
        return continuation
    }

    private func install(_ link: Link) {
        self.link = link
        state = .connected
        let generation = link.generation
        let socket = link.socket
        link.tasks.append(Task {
            while true {
                do { handle(try await socket.receive(), generation) } catch {
                    fail(generation, (error as? TunnelError) ?? .connectionLost)
                    return
                }
                guard self.link?.generation == generation else { return }
            }
        })
        link.tasks.append(Task { [timing] in
            while !Task.isCancelled {
                try? await Task.sleep(for: timing.ping / 2)
                checkSilence(generation)
            }
        })
    }

    private func checkSilence(_ generation: Int) {
        guard let link, link.generation == generation else { return }
        let silent = link.lastReceived.duration(to: .now)
        if silent >= timing.silence { fail(generation, .connectionLost) }
        else if silent >= timing.ping { link.outbox.yield(.text("ping")) }
    }

    private func wrote(_ generation: Int) {
        guard let link, link.generation == generation else { return }
        link.pendingWrites -= 1
        if link.pendingWrites == 0 { wakeIdleWaiters() }
    }

    // The `code` of a relay text error or of an ERROR frame.
    private static func errorCode(_ json: Data) -> String? {
        (try? JSONSerialization.jsonObject(with: json) as? [String: Any])?["code"] as? String
    }

    private static func relayCode(_ text: String) -> String { errorCode(Data(text.utf8)) ?? "unknown" }

    // MARK: Frames from the Mac

    private func handle(_ message: TunnelSocketMessage, _ generation: Int) {
        guard let link, link.generation == generation else { return }
        link.lastReceived = .now
        switch message {
        case .text(let text):
            if text == "pong" { return }
            fail(generation, .relay(code: Self.relayCode(text), duringHandshake: false))
        case .data(let data):
            let frame: TunnelFrame?
            do { frame = try link.session.open(data) } catch {
                fail(generation, .protocolViolation)
                return
            }
            guard let frame else { return }
            switch frame.type {
            case .ping: try? send(TunnelFrame(type: .pong, stream: 0), on: generation)
            case .pong, .rekey: break
            case .updateHint:
                guard let cursor = frame.hintCursor else { return fail(generation, .protocolViolation) }
                onUpdateHint?(cursor)
            // The Mac never opens a stream.
            case .requestHead: fail(generation, .protocolViolation)
            default:
                guard let stream = streams[frame.stream], stream.generation == generation, !stream.finished else { return }
                receive(frame, on: stream)
            }
        }
    }

    private func receive(_ frame: TunnelFrame, on stream: Stream) {
        switch frame.type {
        case .responseHead:
            guard let head = try? JSONSerialization.jsonObject(with: frame.payload) as? [String: Any],
                  let status = head["status"] as? Int else {
                stream.failure = TunnelError.protocolViolation
                break
            }
            stream.status = status
            stream.headers = head["headers"] as? [String: String] ?? [:]
        case .body:
            stream.body.append(frame.payload)
            stream.unacknowledged += frame.payload.count
            if stream.body.count > Self.maxResponse {
                stream.failure = TunnelError.tooLarge
                try? send(TunnelFrame(type: .cancel, stream: stream.id), on: stream.generation)
            } else if stream.unacknowledged >= Self.windowStep {
                try? send(.window(UInt32(stream.unacknowledged), stream: stream.id), on: stream.generation)
                stream.unacknowledged = 0
            }
        case .end:
            if stream.status == nil { stream.failure = TunnelError.protocolViolation } else { stream.finished = true }
        case .window:
            guard frame.payload.count == 4 else {
                stream.failure = TunnelError.protocolViolation
                break
            }
            stream.sendWindow += frame.payload.reduce(0) { $0 << 8 | Int($1) }
        case .error:
            stream.failure = switch Self.errorCode(frame.payload).flatMap(TunnelErrorCode.init(rawValue:)) {
            case .backendDown: TunnelError.backendDown
            case .macOffline: TunnelError.macOffline
            case .tooLarge: TunnelError.tooLarge
            case nil: TunnelError.protocolViolation
            }
        case .cancel:
            stream.failure = TunnelError.connectionLost
        default:
            return
        }
        stream.wake()
    }

    // MARK: Closing

    private func fail(_ generation: Int, _ error: TunnelError) {
        guard let link, link.generation == generation else { return }
        tearDown(link, error: error)
        state = State(error)
    }

    private func tearDown(_ link: Link, error: Error) {
        self.link = nil
        link.outbox.finish()
        link.tasks.forEach { $0.cancel() }
        link.socket.close()
        for stream in streams.values where stream.generation == link.generation && stream.failure == nil {
            stream.failure = error
            stream.wake()
        }
        wakeIdleWaiters()
    }

    private func wakeIdleWaiters() {
        let waiters = idleWaiters
        idleWaiters = []
        waiters.forEach { $0.resume() }
    }

    // The app went to the background. Reads stop now; the socket closes after the writes in flight finish.
    // A request that starts before then keeps the socket.
    func closeWhenIdle() async {
        let request = UUID()
        closeRequest = request
        // Every open read stops at once: with a stream, while it waits for the handshake, or between the two.
        // Otherwise a read would fail with connectionLost after the close, and APIClient would open another
        // socket in the background.
        for (id, isWrite) in openRequests where !isWrite { abandon(id) }
        while closeRequest == request, openRequests.values.contains(true) || (link?.pendingWrites ?? 0) > 0 {
            await withCheckedContinuation { idleWaiters.append($0) }
        }
        guard closeRequest == request else { return }
        close()
    }

    // A request that is still open fails with connectionLost, so APIClient tries it again on a new socket.
    func close() {
        closeRequest = nil
        // A handshake in progress sees the new generation and drops its socket.
        generation += 1
        if let link { tearDown(link, error: TunnelError.connectionLost) }
        state = .idle
    }
}

private final class Flag: @unchecked Sendable {
    private let lock = NSLock()
    private var raised = false
    var value: Bool { lock.withLock { raised } }
    func set() { lock.withLock { raised = true } }
}

extension Data {
    var hexString: String { map { String(format: "%02x", $0) }.joined() }

    // Unpadded base64url, as in the pairing link.
    init?(base64URL text: String) {
        guard !text.isEmpty, text.utf8.allSatisfy({ $0 == 45 || $0 == 95 || (48...57).contains($0) || (65...90).contains($0) || (97...122).contains($0) }) else {
            return nil
        }
        var value = text.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
        value += String(repeating: "=", count: (4 - value.count % 4) % 4)
        self.init(base64Encoded: value)
    }
}
