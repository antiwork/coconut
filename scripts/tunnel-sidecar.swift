import CryptoKit
import Foundation
import Network

// The Mac end of the relay tunnel (docs/relay-transport.md). scripts/tunnel.py compiles this file with
// ios/Tunnel/Noise.swift, ios/Tunnel/TunnelFrames.swift, and scripts/tunnel-events.swift, and runs it as the LaunchAgent
// com.gumroad.coconut.tunnel.
//
// The sidecar keeps one WebSocket to the relay. Each iPhone socket arrives as a channel with its own Noise session.
// Each stream in a session is one HTTP request, which the sidecar sends to the backend's plain HTTP listener on
// loopback with X-Coconut-Peer set to the iPhone's static key. The backend checks that key against the device token.
//
// When a paired iPhone asks for update hints (X-Coconut-Hints: 1 on a request), the sidecar long-polls the backend's
// event cursor on loopback, and sends an UPDATE_HINT frame on that session when the cursor moves. The iPhone then
// fetches the updates at once. Older iPhones never ask, so they never get the frame type that they do not know.
//
// Files in <data>/tunnel, all private to this user:
// - identity.key: the raw Curve25519 private key of this Mac (mode 0600).
// - route.json: {relay, route, host_token, mac_key} from the registration with the relay (mode 0600).
// - peers.json: the iPhone keys that paired through the tunnel, each with the hash of its relay client token.
// - tickets/<hash>.json: {psk, expires} for one pairing link. scripts/pairing.py writes it, and the sidecar
//   removes it after use or expiry.
// - status.json: the connection state for scripts and diagnostics, with the last disconnect and its cause.
// - events.log, events.log.1: the connection events as JSON lines, at most about 256 KB each (scripts/tunnel-events.swift).
//   They hold no tokens, keys, routes, or payloads.

let maxRequestBody = 7 * 1024 * 1024
// All request bodies that the sidecar holds at one time, over every channel.
let maxBufferedBodies = 32 * 1024 * 1024
let maxResponseHead = 64 * 1024
let maxStreams = 64
// The relay allows 8 iPhone sockets for each route, and a pairing ticket for at most 600 seconds.
let maxChannels = 8
let maxTickets = 8
let maxTicketSeconds = 600
let heartbeatInterval: TimeInterval = 60
let pingInterval: TimeInterval = 20
let silenceLimit: TimeInterval = 45
let maxBackoff: TimeInterval = 60
// One long poll of the backend's event cursor waits this long. The backend allows at most 25 seconds.
let cursorWait = 25

enum Derive {
    // HKDF-SHA256 with an empty salt. scripts/pairing.py and the iPhone use the same labels.
    static func key(_ input: Data, _ label: String) -> Data {
        HKDF<SHA256>.deriveKey(inputKeyMaterial: SymmetricKey(data: input), info: Data(label.utf8), outputByteCount: 32)
            .withUnsafeBytes { Data($0) }
    }

    // The relay client token of a phone that paired on this session. Both ends know the handshake hash.
    static func clientToken(handshakeHash: Data) -> String { key(handshakeHash, "coconut relay client").hex }

    static func sha256(_ text: String) -> String { Data(SHA256.hash(data: Data(text.utf8))).hex }
}

extension Data {
    var hex: String { map { String(format: "%02x", $0) }.joined() }
    var base64URL: String {
        base64EncodedString().replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    init?(hex: String) {
        guard hex.count % 2 == 0, hex.allSatisfy(\.isHexDigit) else { return nil }
        var bytes: [UInt8] = []
        var index = hex.startIndex
        while index < hex.endIndex {
            let next = hex.index(index, offsetBy: 2)
            bytes.append(UInt8(hex[index..<next], radix: 16)!)
            index = next
        }
        self.init(bytes)
    }
}

let timestamps = ISO8601DateFormatter()

func log(_ message: String) {
    FileHandle.standardOutput.write(Data("\(timestamps.string(from: Date())) \(message)\n".utf8))
}

// RFC 9110 token characters, for header names.
func isToken(_ text: String) -> Bool {
    !text.isEmpty && text.unicodeScalars.allSatisfy { $0.isASCII && ($0.properties.isAlphabetic || ("0"..."9").contains($0)
        || "!#$%&'*+-.^_`|~".unicodeScalars.contains($0)) }
}

// Visible ASCII and spaces only. Swift compares Characters, and "\r\n" is one Character, so check the scalars.
func isFieldValue(_ text: String) -> Bool {
    text.unicodeScalars.allSatisfy { $0 == " " || $0 == "\t" || (0x21...0x7E).contains($0.value) }
}

// Writes a private file atomically, so a reader never sees half of it.
func writePrivate(_ data: Data, to url: URL) throws {
    let temporary = url.deletingLastPathComponent().appendingPathComponent(".\(url.lastPathComponent).\(UUID().uuidString)")
    guard FileManager.default.createFile(atPath: temporary.path, contents: data, attributes: [.posixPermissions: 0o600]) else {
        throw CocoaError(.fileWriteUnknown)
    }
    guard rename(temporary.path, url.path) == 0 else {
        try? FileManager.default.removeItem(at: temporary)
        throw CocoaError(.fileWriteUnknown)
    }
}

func privateDirectory(_ url: URL) throws {
    let values = try? url.resourceValues(forKeys: [.isSymbolicLinkKey])
    if values?.isSymbolicLink == true { throw CocoaError(.fileWriteInvalidFileName) }
    try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
    chmod(url.path, 0o700)
}

struct Route: Codable {
    var relay: String
    var route: String
    var host_token: String
    var mac_key: String
}

struct Peer: Codable {
    var key: String
    var token_hash: String
    var created: Date
}

struct Ticket: Codable {
    var psk: String
    var expires: Double
}

// The files in <data>/tunnel.
struct Store {
    let directory: URL

    var identityURL: URL { directory.appendingPathComponent("identity.key") }
    var routeURL: URL { directory.appendingPathComponent("route.json") }
    var peersURL: URL { directory.appendingPathComponent("peers.json") }
    var statusURL: URL { directory.appendingPathComponent("status.json") }
    var eventsURL: URL { directory.appendingPathComponent("events.log") }
    var ticketsURL: URL { directory.appendingPathComponent("tickets") }

    func identity() throws -> Curve25519.KeyAgreement.PrivateKey {
        if let data = try? Data(contentsOf: identityURL) {
            return try Curve25519.KeyAgreement.PrivateKey(rawRepresentation: data)
        }
        let key = Curve25519.KeyAgreement.PrivateKey()
        try writePrivate(key.rawRepresentation, to: identityURL)
        return key
    }

    func route() -> Route? {
        guard let data = try? Data(contentsOf: routeURL) else { return nil }
        return try? JSONDecoder().decode(Route.self, from: data)
    }

    func peers() -> [Peer] {
        guard let data = try? Data(contentsOf: peersURL) else { return [] }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return (try? decoder.decode([Peer].self, from: data)) ?? []
    }

    func savePeers(_ peers: [Peer]) throws {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        try writePrivate(try encoder.encode(peers), to: peersURL)
    }

    // Returns the valid tickets by hash, and removes expired or invalid ones.
    func tickets(now: Date = Date()) -> [String: Ticket] {
        var result: [String: Ticket] = [:]
        let files = (try? FileManager.default.contentsOfDirectory(at: ticketsURL, includingPropertiesForKeys: nil)) ?? []
        for file in files where file.pathExtension == "json" {
            let hash = file.deletingPathExtension().lastPathComponent
            guard hash.count == 64, Data(hex: hash) != nil, let data = try? Data(contentsOf: file),
                  let ticket = try? JSONDecoder().decode(Ticket.self, from: data),
                  Data(hex: ticket.psk)?.count == 32, ticket.expires > now.timeIntervalSince1970 else {
                try? FileManager.default.removeItem(at: file)
                continue
            }
            result[hash] = ticket
        }
        return result
    }

    func ticket(_ hash: String) -> Ticket? {
        guard hash.count == 64, Data(hex: hash) != nil,
              let data = try? Data(contentsOf: ticketsURL.appendingPathComponent("\(hash).json")),
              let ticket = try? JSONDecoder().decode(Ticket.self, from: data),
              ticket.expires > Date().timeIntervalSince1970 else { return nil }
        return ticket
    }

    func removeTicket(_ hash: String) {
        try? FileManager.default.removeItem(at: ticketsURL.appendingPathComponent("\(hash).json"))
    }
}

// Registers a new route with the relay. The relay answers 409 for a route that exists, so the sidecar picks another.
func register(store: Store, relay: String) throws -> Route {
    let key = try store.identity()
    guard let base = URL(string: relay), ["https", "http"].contains(base.scheme) else { throw CocoaError(.fileReadInvalidFileName) }
    for _ in 0..<3 {
        let route = Data((0..<32).map { _ in UInt8.random(in: 0...255) }).hex
        var request = URLRequest(url: base.appendingPathComponent("v1/tunnels"))
        request.httpMethod = "POST"
        request.httpBody = try JSONSerialization.data(withJSONObject: ["route": route])
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let semaphore = DispatchSemaphore(value: 0)
        var result: (Data?, URLResponse?, Error?)
        URLSession.shared.dataTask(with: request) { result = ($0, $1, $2); semaphore.signal() }.resume()
        semaphore.wait()
        let status = (result.1 as? HTTPURLResponse)?.statusCode ?? 0
        if status == 409 { continue }
        guard status == 200, let data = result.0,
              let token = (try JSONSerialization.jsonObject(with: data) as? [String: Any])?["host_token"] as? String else {
            throw NSError(domain: "Coconut", code: status, userInfo: [NSLocalizedDescriptionKey:
                "The relay did not register the tunnel (HTTP \(status)). \(result.2?.localizedDescription ?? "")"])
        }
        let value = Route(relay: relay, route: route, host_token: token, mac_key: key.publicKey.rawRepresentation.base64URL)
        try writePrivate(try JSONEncoder().encode(value), to: store.routeURL)
        return value
    }
    throw NSError(domain: "Coconut", code: 409, userInfo: [NSLocalizedDescriptionKey: "The relay refused every new route."])
}

// One HTTP request inside a Noise session, forwarded to the backend.
final class Stream {
    let id: UInt32
    var method = ""
    var path = ""
    var headers: [String: String] = [:]
    var body = Data()
    var unacknowledged = 0
    // Bytes that the iPhone allows this sidecar to send on the stream.
    var window = Int(TunnelFrame.initialWindow)
    var backend: NWConnection?
    var response = Data()
    var responseHead = false
    // A refused stream ignores the BODY and END frames that the iPhone already sent.
    var refused = false
    var remaining: Int?
    var receiving = false
    var backendDone = false
    var finished = false

    init(id: UInt32) { self.id = id }
}

final class Channel {
    let id: UInt32
    let ticket: Bool
    let tokenHash: String
    var session: TunnelSession?
    // A ticket channel serves only pairing until the backend accepts the pairing.
    var paired: Bool
    // The pre-shared key of a ticket channel, from the ticket file that the sidecar removes at once.
    var psk: Data?
    var streams: [UInt32: Stream] = [:]
    // The iPhone asked for UPDATE_HINT frames. An older iPhone ends the session on an unknown frame type.
    var hints = false

    init(id: UInt32, ticket: Bool, tokenHash: String) {
        self.id = id
        self.ticket = ticket
        self.tokenHash = tokenHash
        paired = !ticket
    }
}

final class Sidecar {
    let store: Store
    let identity: Curve25519.KeyAgreement.PrivateKey
    let route: Route
    let backendHost: NWEndpoint.Host
    let backendPort: NWEndpoint.Port
    let queue = DispatchQueue(label: "coconut.tunnel")
    var relay: NWConnection?
    var connectedAt: Date?
    var lastReceived = Date()
    var lastTick = Date()
    var backoff: TimeInterval = 1
    var reconnecting = false
    // The waiting retry. An immediate reconnect replaces it.
    var retry: DispatchWorkItem?
    var state = "starting"
    var lastError: String?
    let events: TunnelEventLog
    var connectStarted = Date()
    // Connection attempts since the last connected socket.
    var attempts = 0
    // Connected sockets since the sidecar started.
    var connections = 0
    var reconnects: Int { max(0, connections - 1) }
    // The end of the last connected socket.
    var lastDisconnect: (at: Date, cause: DisconnectCause)?
    // Backend failures can come in bursts. The log gets at most one line a minute for them.
    var lastBackendEvent = Date.distantPast
    var backendFailures = 0
    var channels: [UInt32: Channel] = [:]
    var sentTickets: Set<String> = []
    // The sidecar is the only writer of peers.json, so it reads the file once.
    var peers: [Peer]
    var ticks = 0
    var lastStatus: [String: String] = [:]
    var lastStatusWrite = Date.distantPast
    var timer: DispatchSourceTimer?
    var ticketWatch: DispatchSourceFileSystemObject?
    let pathMonitor = NWPathMonitor()
    var network: String?
    // The backend's event cursor, while a channel wants update hints. nil until the first answer.
    var cursor: UInt64?
    var cursorPoll: NWConnection?
    var cursorFailures = 0
    var hintPacer = UpdateHintPacer()

    init(store: Store, route: Route, backend: String) throws {
        self.store = store
        self.route = route
        identity = try store.identity()
        peers = store.peers()
        events = TunnelEventLog(url: store.eventsURL)
        let parts = backend.split(separator: ":")
        guard parts.count == 2, let port = NWEndpoint.Port(String(parts[1])) else { throw CocoaError(.fileReadInvalidFileName) }
        backendHost = NWEndpoint.Host(String(parts[0]))
        backendPort = port
    }

    func start() {
        queue.async { [self] in
            events.record("start", ["pid": Int(getpid())])
            writeStatus()
            connect()
            let timer = DispatchSource.makeTimerSource(queue: queue)
            timer.schedule(deadline: .now() + 5, repeating: 5)
            timer.setEventHandler { [weak self] in self?.tick() }
            timer.resume()
            self.timer = timer
            watchTickets()
        }
        // A new network path ends the old socket at once. Without this, the socket stays silent until the ping fails.
        // Only a new status or a new primary interface counts. Other path updates, for example DNS, keep the socket.
        pathMonitor.pathUpdateHandler = { [weak self] path in
            guard let self else { return }
            let previous = network
            network = "\(path.status) \(path.availableInterfaces.first?.name ?? "")"
            if previous != nil, previous != network, path.status == .satisfied {
                log("The network changed. Reconnecting.")
                reconnect(cause: DisconnectCause(.networkChanged, reason: network), immediately: true)
            }
        }
        pathMonitor.start(queue: queue)
    }

    // MARK: Relay socket

    func connect() {
        reconnecting = false
        retry = nil
        attempts += 1
        connectStarted = Date()
        events.record("connect_start", ["attempt": attempts])
        let scheme = route.relay.hasPrefix("http://") ? "ws://" : "wss://"
        let base = route.relay.replacingOccurrences(of: #"^https?://"#, with: "", options: .regularExpression)
            .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let url = URL(string: "\(scheme)\(base)/v1/tunnels/\(route.route)/host") else { return fail(DisconnectCause(.invalidURL)) }
        let options = NWProtocolWebSocket.Options()
        options.autoReplyPing = true
        options.maximumMessageSize = 128 * 1024
        options.setAdditionalHeaders([("Authorization", "Bearer \(route.host_token)")])
        let parameters: NWParameters = scheme == "wss://" ? .tls : .tcp
        parameters.defaultProtocolStack.applicationProtocols.insert(options, at: 0)
        let connection = NWConnection(to: .url(url), using: parameters)
        relay = connection
        setState("connecting")
        connection.stateUpdateHandler = { [weak self, weak connection] update in
            guard let self, let connection, connection === relay else { return }
            switch update {
            case .ready:
                log("Connected to the relay.")
                connectedAt = Date()
                connections += 1
                events.record("connected", ["attempt": attempts, "took_ms": milliseconds(since: connectStarted),
                                            "reconnects": reconnects])
                attempts = 0
                lastReceived = Date()
                lastError = nil
                sentTickets = []
                setState("connected")
                sendTickets()
                receive(on: connection)
            case .waiting(let error), .failed(let error):
                fail(.network(error))
            case .cancelled:
                break
            default:
                break
            }
        }
        connection.start(queue: queue)
    }

    func fail(_ cause: DisconnectCause) {
        lastError = cause.summary
        reconnect(cause: cause, immediately: false)
    }

    func milliseconds(since start: Date) -> Int { Int((Date().timeIntervalSince(start) * 1000).rounded()) }

    // Ends the socket and every Noise session on it. The relay closes the iPhone sockets when a new host socket arrives.
    func reconnect(cause: DisconnectCause, immediately: Bool) {
        // A delayed retry waits. A network change or a wake must not wait for it.
        if reconnecting {
            guard immediately, let retry else { return }
            retry.cancel()
            backoff = 1
            schedule(after: ReconnectDelay.settle)
            log("Reconnecting at once (\(cause.summary)).")
            events.record("backoff", cause.fields.merging(["delay_ms": Int(ReconnectDelay.settle * 1000)]) { $1 })
            return
        }
        reconnecting = true
        var fields = cause.fields
        fields["phase"] = connectedAt == nil ? "connecting" : "connected"
        fields["channels"] = channels.count
        if let connectedAt {
            fields["uptime_s"] = Int(Date().timeIntervalSince(connectedAt))
            lastDisconnect = (Date(), cause)
        } else {
            fields["after_ms"] = milliseconds(since: connectStarted)
        }
        events.record("disconnected", fields)
        relay?.cancel()
        relay = nil
        for channel in channels.values { drop(channel) }
        channels = [:]
        // The end of a healthy socket, for example a reset by Cloudflare, gets a new attempt at once.
        let next = ReconnectDelay.next(immediately: immediately, uptime: connectedAt.map { Date().timeIntervalSince($0) },
                                       backoff: backoff, maxBackoff: maxBackoff, jitter: Double.random(in: 0.8...1.2))
        connectedAt = nil
        let delay = next.delay
        backoff = next.backoff
        events.record("backoff", ["delay_ms": Int((delay * 1000).rounded())])
        setState("offline")
        log("The relay socket ended (\(cause.summary)). Retrying in \(String(format: "%.1f", delay)) seconds.")
        schedule(after: delay)
    }

    func schedule(after delay: TimeInterval) {
        let item = DispatchWorkItem { [weak self] in self?.connect() }
        retry = item
        queue.asyncAfter(deadline: .now() + delay, execute: item)
    }

    func receive(on connection: NWConnection) {
        connection.receiveMessage { [weak self, weak connection] data, context, _, error in
            guard let self, let connection, connection === relay else { return }
            if let error { return fail(.network(error)) }
            let metadata = context?.protocolMetadata(definition: NWProtocolWebSocket.definition) as? NWProtocolWebSocket.Metadata
            lastReceived = Date()
            switch metadata?.opcode {
            case .binary: if let data { handleRelay(data) }
            case .close: return fail(.relayClosed(code: metadata?.closeCode, reason: data))
            default: break
            }
            receive(on: connection)
        }
    }

    func send(_ data: Data) {
        guard let relay else { return }
        let metadata = NWProtocolWebSocket.Metadata(opcode: .binary)
        let context = NWConnection.ContentContext(identifier: "frame", metadata: [metadata])
        relay.send(content: data, contentContext: context, isComplete: true, completion: .contentProcessed { _ in })
    }

    func sendText(_ text: String) {
        guard let relay else { return }
        let metadata = NWProtocolWebSocket.Metadata(opcode: .text)
        let context = NWConnection.ContentContext(identifier: "text", metadata: [metadata])
        relay.send(content: Data(text.utf8), contentContext: context, isComplete: true, completion: .contentProcessed { _ in })
    }

    func control(_ value: [String: Any]) {
        guard let json = try? JSONSerialization.data(withJSONObject: value) else { return }
        send(RelayFrame(channel: RelayFrame.controlChannel, ciphertext: json).encoded())
    }

    // Runs every 5 seconds. A long gap between ticks means that the Mac slept, and the socket is gone.
    func tick() {
        let now = Date()
        let previousTick = lastTick
        let slept = now.timeIntervalSince(previousTick) > 30
        lastTick = now
        if slept, relay != nil {
            log("The Mac woke from sleep. Reconnecting.")
            let gap = Int(now.timeIntervalSince(previousTick))
            return reconnect(cause: DisconnectCause(.wake, reason: "no tick for \(gap) seconds"), immediately: true)
        }
        if state == "connected" {
            if now.timeIntervalSince(lastReceived) > silenceLimit { return fail(DisconnectCause(.pingTimeout)) }
            // The relay answers `ping` with `pong` without waking its Durable Object.
            if now.timeIntervalSince(lastReceived) > pingInterval { sendText("ping") }
            // The folder watcher sends new tickets at once. This slow pass also removes expired ones.
            ticks += 1
            if ticks % 12 == 0 { sendTickets() }
        }
        writeStatus()
    }

    // MARK: Pairing tickets

    func watchTickets() {
        try? privateDirectory(store.ticketsURL)
        let descriptor = open(store.ticketsURL.path, O_EVTONLY)
        guard descriptor >= 0 else { return }
        let source = DispatchSource.makeFileSystemObjectSource(fileDescriptor: descriptor, eventMask: .write, queue: queue)
        source.setEventHandler { [weak self] in self?.sendTickets() }
        source.setCancelHandler { Darwin.close(descriptor) }
        source.resume()
        ticketWatch = source
    }

    // Allows each new pairing link on the relay. The relay keeps a ticket until its expiry or its first use, and holds
    // at most 8. A ticket that does not fit waits in the folder until a ticket is used or expires, then goes first.
    func sendTickets() {
        guard state == "connected" else { return }
        let now = Date().timeIntervalSince1970
        let tickets = store.tickets()
        sentTickets.formIntersection(tickets.keys)
        for (hash, ticket) in tickets.sorted(by: { $0.value.expires < $1.value.expires })
        where !sentTickets.contains(hash) && sentTickets.count < maxTickets {
            let ttl = Int(ticket.expires - now)
            guard ttl >= 1 else { continue }
            control(["type": "add_ticket", "hash": hash, "ttl": min(ttl, maxTicketSeconds)])
            sentTickets.insert(hash)
        }
    }

    // MARK: Channels and Noise

    func handleRelay(_ data: Data) {
        guard let frame = try? RelayFrame.decode(data) else { return }
        if frame.channel == RelayFrame.controlChannel { return handleControl(frame.ciphertext) }
        guard let channel = channels[frame.channel] else {
            return control(["type": "close_channel", "id": Int(frame.channel)])
        }
        do {
            if channel.session == nil {
                try handshake(channel, message: frame.ciphertext)
            } else if let tunnelFrame = try channel.session!.open(frame.ciphertext) {
                try handleFrame(tunnelFrame, on: channel)
            }
        } catch {
            close(channel, reason: channel.session == nil ? "handshake_failed" : "session_error", error: error)
        }
    }

    func handleControl(_ data: Data) {
        guard let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = value["type"] as? String else { return }
        switch type {
        case "open":
            guard let id = (value["id"] as? NSNumber)?.uint32Value, id != 0, let auth = value["auth"] as? String,
                  let hash = value["hash"] as? String else { return }
            let channel = Channel(id: id, ticket: auth == "ticket", tokenHash: hash)
            if channel.ticket {
                // The relay removed the ticket at this connection, so the link is spent and its slot is free.
                channel.psk = store.ticket(hash).flatMap { Data(hex: $0.psk) }
                store.removeTicket(hash)
                sentTickets.remove(hash)
                sendTickets()
            }
            if channels.count >= maxChannels {
                events.record("channel_refused", ["channel": Int(id), "reason": "limit", "channels": channels.count])
                return control(["type": "close_channel", "id": Int(id)])
            }
            channels[id] = channel
            events.record("channel_open", ["channel": Int(id), "auth": channel.ticket ? "ticket" : "token",
                                           "channels": channels.count])
            writeStatus()
        case "closed":
            if let id = (value["id"] as? NSNumber)?.uint32Value, let channel = channels.removeValue(forKey: id) {
                drop(channel)
                events.record("channel_close", ["channel": Int(id), "by": "relay", "channels": channels.count])
                writeStatus()
            }
        case "error":
            log("The relay refused a control message: \(value["code"] as? String ?? "unknown").")
        default:
            break
        }
    }

    // Reads message 1 from the iPhone and answers with message 2. The iPhone key must belong to this channel's token.
    // On a ticket channel, the ticket's pre-shared key proves that the iPhone has the pairing link.
    func handshake(_ channel: Channel, message: Data) throws {
        if channel.ticket, channel.psk == nil { throw NoiseError.invalidKey }
        var handshake = try NoiseHandshake(pattern: channel.ticket ? .ikpsk2 : .ik, initiator: false, localStatic: identity,
                                           psk: channel.psk)
        guard try handshake.readMessage(message).isEmpty, let peer = handshake.remoteStatic else { throw NoiseError.invalidMessage }
        if !channel.ticket {
            guard peers.contains(where: { $0.key == peer.hex && $0.token_hash == channel.tokenHash }) else {
                throw NoiseError.invalidKey
            }
        }
        let reply = try handshake.writeMessage()
        channel.session = TunnelSession(transport: try handshake.transport())
        send(RelayFrame(channel: channel.id, ciphertext: reply).encoded())
    }

    func seal(_ frame: TunnelFrame, on channel: Channel) {
        guard let session = channel.session else { return }
        do {
            for ciphertext in try session.seal(frame) {
                send(RelayFrame(channel: channel.id, ciphertext: ciphertext).encoded())
            }
        } catch {
            close(channel, reason: "seal_failed", error: error)
        }
    }

    // A failed rekey shows as the error invalidRekey or decryptionFailed of the session.
    func close(_ channel: Channel, reason: String, error: Error) {
        guard channels.removeValue(forKey: channel.id) != nil else { return }
        drop(channel)
        events.record("channel_close", ["channel": Int(channel.id), "by": "mac", "reason": reason,
                                        "error": sessionErrorName(error), "channels": channels.count])
        control(["type": "close_channel", "id": Int(channel.id)])
        writeStatus()
    }

    func drop(_ channel: Channel) {
        for stream in channel.streams.values { stream.backend?.cancel() }
        channel.streams = [:]
        channel.session = nil
    }

    func handleFrame(_ frame: TunnelFrame, on channel: Channel) throws {
        switch frame.type {
        case .ping:
            seal(TunnelFrame(type: .pong, stream: frame.stream), on: channel)
        case .pong, .error:
            break
        case .requestHead:
            // The iPhone opens odd streams. A repeated stream number is a protocol error.
            guard frame.stream % 2 == 1, channel.streams[frame.stream] == nil, channel.streams.count < maxStreams,
                  let head = try JSONSerialization.jsonObject(with: frame.payload) as? [String: Any],
                  let method = head["method"] as? String, let path = head["path"] as? String, path.hasPrefix("/"),
                  isToken(method), path.unicodeScalars.allSatisfy({ (0x21...0x7E).contains($0.value) }),
                  let headers = (head["headers"] ?? [String: String]()) as? [String: String],
                  // A control character in a header could add headers or a second request. The app never sends one.
                  headers.allSatisfy({ isToken($0.key) && isFieldValue($0.value) })
            else { throw TunnelFrameError.truncated }
            let stream = Stream(id: frame.stream)
            stream.method = method
            stream.path = path
            stream.headers = headers
            channel.streams[frame.stream] = stream
            if channel.paired, !channel.hints, headers.contains(where: { $0.key.lowercased() == "x-coconut-hints" && $0.value == "1" }) {
                channel.hints = true
                watchCursor()
            }
        case .body:
            guard let stream = channel.streams[frame.stream], stream.backend == nil else { throw TunnelFrameError.truncated }
            if stream.refused { return }
            if stream.body.count + frame.payload.count > maxRequestBody
                || bufferedBodies() + frame.payload.count > maxBufferedBodies {
                return refuse(stream, on: channel, code: "too_large")
            }
            stream.body.append(frame.payload)
            // The sidecar holds the body in memory, so it returns the window at once in steps of 256 KB.
            stream.unacknowledged += frame.payload.count
            if stream.unacknowledged >= 256 * 1024 {
                seal(.window(UInt32(stream.unacknowledged), stream: stream.id), on: channel)
                stream.unacknowledged = 0
            }
        case .end:
            guard let stream = channel.streams[frame.stream], stream.backend == nil else { throw TunnelFrameError.truncated }
            if stream.refused { channel.streams[stream.id] = nil } else { forward(stream, on: channel) }
        case .cancel:
            channel.streams.removeValue(forKey: frame.stream)?.backend?.cancel()
        case .window:
            guard frame.payload.count == 4 else { throw TunnelFrameError.truncated }
            let bytes = frame.payload.reduce(0) { $0 << 8 | Int($1) }
            if let stream = channel.streams[frame.stream] {
                stream.window += bytes
                pump(stream, on: channel)
            }
        case .responseHead, .rekey, .updateHint:
            throw TunnelFrameError.truncated
        }
    }

    // Answers ERROR, and keeps the stream until the iPhone's END or CANCEL, so frames in flight do not end the session.
    func refuse(_ stream: Stream, on channel: Channel, code: String) {
        stream.refused = true
        stream.body = Data()
        seal(error(code, stream: stream.id), on: channel)
    }

    func bufferedBodies() -> Int {
        channels.values.reduce(0) { $0 + $1.streams.values.reduce(0) { $0 + $1.body.count } }
    }

    func error(_ code: String, stream: UInt32) -> TunnelFrame {
        TunnelFrame(type: .error, stream: stream, payload: try! JSONSerialization.data(withJSONObject: ["code": code]))
    }

    // MARK: Backend

    static let dropped: Set<String> = ["host", "connection", "content-length", "transfer-encoding", "keep-alive",
                                       "upgrade", "x-coconut-peer", "proxy-connection", "te", "trailer"]

    func forward(_ stream: Stream, on channel: Channel) {
        guard let peer = channel.session?.remoteStatic.hex else { return }
        let pairing = stream.method == "POST" && stream.path == "/v1/pair"
        if !channel.paired && !pairing && stream.path != "/health" {
            // A ticket session exists to pair. Other requests wait for a session with the client token.
            return respond(stream, on: channel, status: 401, json: ["error": "Pair this phone with the Mac to continue."])
        }
        var head = "\(stream.method) \(stream.path) HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        // handleFrame already refused any name or value with a control character.
        for (name, value) in stream.headers where !Self.dropped.contains(name.lowercased()) {
            head += "\(name): \(value)\r\n"
        }
        head += "X-Coconut-Peer: \(peer)\r\nContent-Length: \(stream.body.count)\r\nConnection: close\r\n\r\n"
        let connection = NWConnection(host: backendHost, port: backendPort, using: .tcp)
        stream.backend = connection
        connection.stateUpdateHandler = { [weak self, weak channel, weak stream, weak connection] update in
            guard let self, let channel, let stream, let connection, stream.backend === connection else { return }
            switch update {
            case .ready:
                connection.send(content: Data(head.utf8), completion: .contentProcessed { _ in })
                connection.send(content: stream.body, completion: .contentProcessed { _ in })
                stream.body = Data()
                pump(stream, on: channel)
            case .waiting(let error), .failed(let error):
                backendUnreachable(error)
                fail(stream, on: channel)
            default:
                break
            }
        }
        connection.start(queue: queue)
    }

    func backendUnreachable(_ error: NWError) {
        backendFailures += 1
        guard Date().timeIntervalSince(lastBackendEvent) >= 60 else { return }
        lastBackendEvent = Date()
        var fields = DisconnectCause.network(error).fields
        fields["cause"] = nil
        fields["failures"] = backendFailures
        events.record("backend_unreachable", fields)
        backendFailures = 0
    }

    func respond(_ stream: Stream, on channel: Channel, status: Int, json: [String: Any]) {
        let body = try! JSONSerialization.data(withJSONObject: json)
        let head = ["status": status, "headers": ["Content-Type": "application/json; charset=utf-8",
                                                  "Content-Length": String(body.count)]] as [String: Any]
        seal(TunnelFrame(type: .responseHead, stream: stream.id, payload: try! JSONSerialization.data(withJSONObject: head)), on: channel)
        for frame in TunnelFrame.bodyFrames(body, stream: stream.id) { seal(frame, on: channel) }
        seal(TunnelFrame(type: .end, stream: stream.id), on: channel)
        channel.streams[stream.id] = nil
    }

    // Moves response bytes from the backend to the iPhone within the stream's window, and reads more when the
    // buffer is small. A slow iPhone therefore slows the backend read, not the memory of the sidecar.
    func pump(_ stream: Stream, on channel: Channel) {
        guard channel.streams[stream.id] === stream, !stream.finished else { return }
        if !stream.responseHead {
            guard parseHead(stream, on: channel) else {
                // Only an incomplete head counts against the limit. Body bytes can come in the same read.
                if stream.backendDone || stream.response.count > maxResponseHead { return fail(stream, on: channel) }
                return receive(stream, on: channel)
            }
            if stream.finished { return }
        }
        // One copy of the rest of the buffer per pass, not one per frame.
        var offset = stream.response.startIndex
        while stream.responseHead, offset < stream.response.endIndex, stream.window > 0 {
            let count = min(stream.response.endIndex - offset, stream.window, TunnelFrame.maxPayload)
            seal(TunnelFrame(type: .body, stream: stream.id, payload: stream.response.subdata(in: offset..<offset + count)), on: channel)
            offset += count
            stream.window -= count
        }
        if offset > stream.response.startIndex { stream.response = stream.response.subdata(in: offset..<stream.response.endIndex) }
        if stream.remaining == 0 || (stream.backendDone && stream.remaining == nil) {
            if stream.response.isEmpty { finish(stream, on: channel) }
        } else if stream.backendDone {
            // The backend ended before the whole body. The iPhone must not take a partial body as complete.
            fail(stream, on: channel)
        } else if stream.response.count < 256 * 1024 {
            receive(stream, on: channel)
        }
    }

    func receive(_ stream: Stream, on channel: Channel) {
        guard let connection = stream.backend, !stream.receiving, !stream.backendDone else { return }
        stream.receiving = true
        connection.receive(minimumIncompleteLength: 1, maximumLength: 256 * 1024) { [weak self, weak channel, weak stream] data, _, done, error in
            guard let self, let channel, let stream else { return }
            stream.receiving = false
            if var data, !data.isEmpty {
                if let remaining = stream.remaining, stream.responseHead {
                    data = data.prefix(remaining)
                    stream.remaining = remaining - data.count
                }
                stream.response.append(data)
            }
            if done || error != nil { stream.backendDone = true }
            pump(stream, on: channel)
        }
    }

    // Parses the status line and headers, and sends RESP_HEAD. Returns false while the head is incomplete.
    func parseHead(_ stream: Stream, on channel: Channel) -> Bool {
        guard let end = stream.response.range(of: Data("\r\n\r\n".utf8)) else { return false }
        let text = String(decoding: stream.response[stream.response.startIndex..<end.lowerBound], as: UTF8.self)
        stream.response = Data(stream.response[end.upperBound...])
        var lines = text.components(separatedBy: "\r\n")
        let status = Int(lines.removeFirst().split(separator: " ").dropFirst().first ?? "") ?? 502
        var headers: [String: String] = [:]
        for line in lines {
            guard let colon = line.firstIndex(of: ":") else { continue }
            let name = String(line[..<colon]).trimmingCharacters(in: .whitespaces)
            let value = String(line[line.index(after: colon)...]).trimmingCharacters(in: .whitespaces)
            if ["connection", "keep-alive", "transfer-encoding"].contains(name.lowercased()) { continue }
            headers[name] = headers[name].map { "\($0), \(value)" } ?? value
        }
        if let length = headers.first(where: { $0.key.lowercased() == "content-length" }).flatMap({ Int($0.value) }) {
            stream.remaining = max(0, length - stream.response.count)
            stream.response = stream.response.prefix(length)
        }
        stream.responseHead = true
        // The phone gets its relay client token from this session. The relay must know the token before the phone
        // reads the answer and reconnects with it, so the control message goes first on the same socket.
        if !channel.paired, stream.method == "POST", stream.path == "/v1/pair", status == 200 {
            // Without a saved key, the phone would lose the Mac at the next start of the sidecar. It must pair again.
            guard addClient(on: channel) else {
                fail(stream, on: channel)
                return true
            }
            channel.paired = true
        }
        let head = ["status": status, "headers": headers] as [String: Any]
        seal(TunnelFrame(type: .responseHead, stream: stream.id, payload: (try? JSONSerialization.data(withJSONObject: head)) ?? Data()),
             on: channel)
        return true
    }

    func fail(_ stream: Stream, on channel: Channel) {
        guard !stream.finished else { return }
        stream.finished = true
        stream.backend?.cancel()
        seal(error("backend_down", stream: stream.id), on: channel)
        channel.streams[stream.id] = nil
    }

    func finish(_ stream: Stream, on channel: Channel) {
        guard !stream.finished else { return }
        stream.finished = true
        stream.backend?.cancel()
        seal(TunnelFrame(type: .end, stream: stream.id), on: channel)
        channel.streams[stream.id] = nil
    }

    // Allows the new phone's client token on the relay, and records its key. A phone that pairs again replaces its
    // old token. The relay allows 32 tokens, so the oldest one goes first.
    // Returns false when the key cannot be saved. Then nothing changes on the relay or in memory.
    func addClient(on channel: Channel) -> Bool {
        guard let session = channel.session else { return false }
        let peer = session.remoteStatic.hex
        let hash = Derive.sha256(Derive.clientToken(handshakeHash: session.handshakeHash))
        var kept = peers.filter { $0.key != peer }.sorted { $0.created < $1.created }
        var removed = peers.filter { $0.key == peer }
        while kept.count >= 31 { removed.append(kept.removeFirst()) }
        kept.append(Peer(key: peer, token_hash: hash, created: Date()))
        do {
            try store.savePeers(kept)
        } catch {
            log("The sidecar could not save the paired phone keys. The pairing failed.")
            return false
        }
        peers = kept
        for old in removed { control(["type": "remove_client", "hash": old.token_hash]) }
        control(["type": "add_client", "hash": hash])
        return true
    }

    // MARK: Update hints

    // Long-polls the backend's event cursor while a channel wants hints. The first poll only reads the cursor.
    func watchCursor() {
        guard cursorPoll == nil else { return }
        guard channels.values.contains(where: { $0.hints && $0.session != nil }) else {
            // Without a listener, the cursor gets stale. The next watch reads it again before it sends a hint.
            cursor = nil
            return
        }
        let after = cursor
        var head = "GET /v1/tunnel/cursor?after=\(after ?? 0)&wait=\(after == nil ? 0 : cursorWait) HTTP/1.1\r\n"
        head += "Host: 127.0.0.1\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        let connection = NWConnection(host: backendHost, port: backendPort, using: .tcp)
        cursorPoll = connection
        var response = Data()
        let timeout = DispatchWorkItem { [weak connection] in connection?.cancel() }
        queue.asyncAfter(deadline: .now() + .seconds(cursorWait + 10), execute: timeout)
        func finish(_ value: UInt64?) {
            guard cursorPoll === connection else { return }
            timeout.cancel()
            connection.stateUpdateHandler = nil
            connection.cancel()
            cursorPoll = nil
            cursorAnswered(value, after: after)
        }
        func read() {
            connection.receive(minimumIncompleteLength: 1, maximumLength: 4096) { data, _, done, error in
                if let data { response.append(data) }
                if done || error != nil || response.count > 4096 { return finish(tunnelCursor(fromResponse: response)) }
                read()
            }
        }
        connection.stateUpdateHandler = { update in
            switch update {
            case .ready:
                connection.send(content: Data(head.utf8), completion: .contentProcessed { _ in })
                read()
            case .waiting, .failed, .cancelled:
                finish(nil)
            default:
                break
            }
        }
        connection.start(queue: queue)
    }

    func cursorAnswered(_ value: UInt64?, after: UInt64?) {
        guard let value else {
            // An older backend answers 404, and a stopped one does not answer. Try again later, up to once a minute.
            cursorFailures += 1
            cursor = nil
            let delay = min(60, 1 << min(cursorFailures - 1, 6))
            queue.asyncAfter(deadline: .now() + .seconds(delay)) { [weak self] in self?.watchCursor() }
            return
        }
        cursorFailures = 0
        cursor = value
        if let after, value != after, let delay = hintPacer.changed(at: Date()) {
            queue.asyncAfter(deadline: .now() + delay) { [weak self] in self?.sendHints() }
        }
        watchCursor()
    }

    // One hint carries every change since the last one. The iPhone compares the cursor with its own.
    func sendHints() {
        hintPacer.sent(at: Date())
        guard let cursor else { return }
        for channel in channels.values where channel.hints && channel.session != nil {
            seal(.updateHint(cursor: cursor), on: channel)
        }
    }

    // MARK: Status

    func setState(_ value: String) {
        state = value
        writeStatus()
    }

    // Writes when the state changes, and once a minute as a heartbeat, so an idle Mac does not write every tick.
    func writeStatus() {
        var status = ["state": state, "relay": route.relay, "route": route.route, "channels": String(channels.count)]
        if let connectedAt { status["connected_since"] = timestamps.string(from: connectedAt) }
        if let lastError { status["last_error"] = lastError }
        status["reconnects"] = String(reconnects)
        if let lastDisconnect {
            status["last_disconnect"] = "\(timestamps.string(from: lastDisconnect.at)) \(lastDisconnect.cause.summary)"
        }
        guard status != lastStatus || Date().timeIntervalSince(lastStatusWrite) >= heartbeatInterval else { return }
        lastStatus = status
        lastStatusWrite = Date()
        var output: [String: Any] = status
        output["channels"] = channels.count
        output["reconnects"] = reconnects
        output["last_disconnect"] = nil
        if let lastDisconnect {
            var value = lastDisconnect.cause.fields
            value["at"] = timestamps.string(from: lastDisconnect.at)
            if let detail = lastDisconnect.cause.detail { value["detail"] = detail }
            output["last_disconnect"] = value
        }
        output["updated"] = timestamps.string(from: lastStatusWrite)
        output["pid"] = Int(getpid())
        if let data = try? JSONSerialization.data(withJSONObject: output, options: [.sortedKeys]) {
            try? writePrivate(data, to: store.statusURL)
        }
    }
}

@main
struct TunnelSidecarCommand {
    static func usage() -> Never {
        FileHandle.standardError.write(Data("""
        Usage: tunnel-sidecar setup --data-dir <dir> --relay <url>
               tunnel-sidecar run --data-dir <dir> [--backend 127.0.0.1:8961]

        """.utf8))
        exit(2)
    }

    static func main() {
        let arguments = Array(CommandLine.arguments.dropFirst())
        guard let command = arguments.first else { usage() }
        var options: [String: String] = [:]
        var index = 1
        while index + 1 < arguments.count, arguments[index].hasPrefix("--") {
            options[String(arguments[index].dropFirst(2))] = arguments[index + 1]
            index += 2
        }
        guard index == arguments.count, let data = options["data-dir"] else { usage() }
        umask(0o077)
        let store = Store(directory: URL(fileURLWithPath: data).appendingPathComponent("tunnel"))
        do {
            try privateDirectory(store.directory)
            try privateDirectory(store.ticketsURL)
            switch command {
            case "setup":
                guard let relay = options["relay"] else { usage() }
                let existing = store.route()
                let route = existing?.relay == relay ? existing! : try register(store: store, relay: relay)
                print(try String(decoding: JSONSerialization.data(withJSONObject: ["route": route.route, "mac_key": route.mac_key, "relay": route.relay]), as: UTF8.self))
            case "run":
                guard let route = store.route() else {
                    FileHandle.standardError.write(Data("Run tunnel.py install to register this Mac with the relay.\n".utf8))
                    exit(1)
                }
                let sidecar = try Sidecar(store: store, route: route, backend: options["backend"] ?? "127.0.0.1:8961")
                signal(SIGPIPE, SIG_IGN)
                sidecar.start()
                withExtendedLifetime(sidecar) { dispatchMain() }
            default:
                usage()
            }
        } catch {
            FileHandle.standardError.write(Data("tunnel-sidecar: \(error.localizedDescription)\n".utf8))
            exit(1)
        }
    }
}
