import CryptoKit
import Foundation

// The iPhone side of the relay tunnel, for scripts/test_tunnel_loopback.py only.
// Usage: tunnel-test-client <setup link>
// It pairs and runs real requests through the app's own TunnelConnection (ios/Coconut/TunnelConnection.swift).
// The checks for refused sessions use the small Connection class below. It prints one JSON line for each result,
// and exits with 1 on the first failure.

struct Failure: Error, CustomStringConvertible { let description: String }

func check(_ condition: Bool, _ message: String) throws { if !condition { throw Failure(description: message) } }

// One request through the app's transport, with the app's timeouts and gzip handling.
func send(_ tunnel: TunnelConnection, _ method: String, _ path: String, headers: [String: String] = [:],
          body: Data = Data()) async throws -> Response {
    let timeout = method == "GET" ? TunnelConnection.shortTimeout : TunnelConnection.longTimeout
    let response = try await tunnel.send(TunnelRequest(method: method, path: path,
                                                       headers: headers.merging(["Accept-Encoding": "gzip"]) { $1 }, body: body),
                                         timeout: timeout)
    return Response(status: response.status, headers: response.headers, body: try response.decodedBody())
}

struct Response {
    var status: Int
    var headers: [String: String]
    var body: Data
}

final class Connection {
    let socket: URLSessionWebSocketTask
    var session: TunnelSession!
    var nextStream: UInt32 = 1

    init(url: URL, authorization: String) {
        var request = URLRequest(url: url)
        request.setValue(authorization, forHTTPHeaderField: "Authorization")
        socket = URLSession.shared.webSocketTask(with: request)
        socket.maximumMessageSize = 128 * 1024
        socket.resume()
    }

    func receiveBinary() async throws -> Data {
        switch try await socket.receive() {
        case .data(let data): return data
        case .string(let text): throw Failure(description: "relay: \(text)")
        @unknown default: throw Failure(description: "unknown message")
        }
    }

    func handshake(pattern: NoisePattern, key: Curve25519.KeyAgreement.PrivateKey, macKey: Data, psk: Data?) async throws {
        var handshake = try NoiseHandshake(pattern: pattern, initiator: true, localStatic: key, remoteStatic: macKey, psk: psk)
        try await socket.send(.data(try handshake.writeMessage()))
        _ = try handshake.readMessage(try await receiveBinary())
        session = TunnelSession(transport: try handshake.transport())
    }

    func send(_ frame: TunnelFrame) async throws {
        for ciphertext in try session.seal(frame) { try await socket.send(.data(ciphertext)) }
    }

    func receiveFrame() async throws -> TunnelFrame {
        while true {
            if let frame = try session.open(try await receiveBinary()) { return frame }
        }
    }

    // One request on a new stream. The body follows the stream's window, as the app will do.
    func request(_ method: String, _ path: String, headers: [String: String] = [:], body: Data = Data()) async throws -> Response {
        let stream = nextStream
        nextStream += 2
        let head = try JSONSerialization.data(withJSONObject: ["method": method, "path": path, "headers": headers])
        try await send(TunnelFrame(type: .requestHead, stream: stream, payload: head))
        var window = Int(TunnelFrame.initialWindow)
        var response = Response(status: 0, headers: [:], body: Data())
        var received = 0
        var offset = 0
        var sentEnd = false
        while true {
            while !sentEnd, window > 0 {
                let count = min(body.count - offset, window, TunnelFrame.maxPayload)
                if count > 0 {
                    try await send(TunnelFrame(type: .body, stream: stream, payload: body.subdata(in: offset..<offset + count)))
                    offset += count
                    window -= count
                }
                if offset == body.count {
                    try await send(TunnelFrame(type: .end, stream: stream))
                    sentEnd = true
                }
            }
            let frame = try await receiveFrame()
            try check(frame.stream == stream || frame.stream == 0, "frame on stream \(frame.stream)")
            switch frame.type {
            case .window:
                window += frame.payload.reduce(0) { $0 << 8 | Int($1) }
            case .responseHead:
                let value = try JSONSerialization.jsonObject(with: frame.payload) as! [String: Any]
                response.status = value["status"] as! Int
                response.headers = value["headers"] as? [String: String] ?? [:]
            case .body:
                response.body.append(frame.payload)
                received += frame.payload.count
                if received >= 256 * 1024 {
                    try await send(.window(UInt32(received), stream: stream))
                    received = 0
                }
            case .end:
                return response
            case .error:
                throw Failure(description: "tunnel error \(String(decoding: frame.payload, as: UTF8.self))")
            default:
                throw Failure(description: "unexpected frame \(frame.type)")
            }
        }
    }

    func close() { socket.cancel(with: .normalClosure, reason: nil) }
}

func json(_ data: Data) throws -> [String: Any] {
    guard let value = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { throw Failure(description: "not JSON") }
    return value
}

func report(_ value: [String: Any]) {
    print(String(decoding: try! JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]), as: UTF8.self))
    fflush(stdout)
}

@main
struct TunnelTestClient {
    static func main() async {
        do {
            try await run(link: CommandLine.arguments[1])
        } catch {
            report(["failure": "\(error)"])
            exit(1)
        }
    }

    static func run(link: String) async throws {
        let encoded = URLComponents(string: link)!.queryItems!.first { $0.name == "setup" }!.value!
        let setup = try json(Data(base64URL: encoded)!)
        try check(setup["version"] as? Int == 2, "not a version 2 link")
        let secret = Data(base64URL: setup["secret"] as! String)!
        let macKey = Data(base64URL: setup["mac_key"] as! String)!
        let relay = (setup["relay"] as! String).replacingOccurrences(of: "http://", with: "ws://")
            .replacingOccurrences(of: "https://", with: "wss://")
        let url = URL(string: "\(relay)/v1/tunnels/\(setup["route"] as! String)/client")!
        let phone = Curve25519.KeyAgreement.PrivateKey()

        // 1. Pair: the ticket opens the relay, and the pre-shared key proves the link to the Mac.
        let endpoint = TunnelEndpoint(relay: URL(string: setup["relay"] as! String)!, route: setup["route"] as! String, macKey: macKey)
        let pairing = TunnelConnection(endpoint: endpoint, phoneKey: phone, credential: .ticket(secret: secret))
        let early = try await send(pairing, "GET", "/v1/state", headers: ["Authorization": "Bearer nothing"])
        try check(early.status == 401, "a ticket session served /v1/state before pairing: \(early.status)")
        let body = try JSONSerialization.data(withJSONObject: ["code": setup["secret"]!, "device_name": "Loopback test"])
        let paired = try await send(pairing, "POST", "/v1/pair", headers: ["Content-Type": "application/json"], body: body)
        try check(paired.status == 200, "pairing answered \(paired.status): \(String(decoding: paired.body, as: UTF8.self))")
        let token = try json(paired.body)["token"] as! String
        let clientToken = TunnelKeys.clientToken(handshakeHash: await pairing.handshakeHash!)
        await pairing.close()
        report(["step": "pair", "status": paired.status])

        // 2. Reconnect with the client token and the same static key, as the app does after pairing.
        let connection = TunnelConnection(endpoint: endpoint, phoneKey: phone, credential: .token(clientToken))
        let auth = ["Authorization": "Bearer \(token)"]
        var started = Date()
        let state = try await send(connection, "GET", "/v1/state", headers: auth)
        try check(state.status == 200, "GET /v1/state answered \(state.status)")
        try check(json(state.body)["bots"] is [Any], "the state has no bots list")
        report(["step": "state", "status": state.status, "bytes": state.body.count, "seconds": Date().timeIntervalSince(started)])
        let write = auth.merging(["Content-Type": "application/json"]) { $1 }
        let bot = try await send(connection, "POST", "/v1/bots", headers: write.merging(["Idempotency-Key": UUID().uuidString]) { $1 },
                                 body: try JSONSerialization.data(withJSONObject: ["name": "Loopback"]))
        try check(bot.status == 200, "POST /v1/bots answered \(bot.status)")

        // 3. Upload an attachment of about 5 MB of JSON, and read the 3.75 MB of content back.
        let content = Data((0..<(3_900_000)).map { _ in UInt8.random(in: 0...255) })
        let upload = try JSONSerialization.data(withJSONObject: [
            "bot_id": try json(bot.body)["id"] as! String, "filename": "loopback.bin", "content_base64": content.base64EncodedString(),
        ])
        started = Date()
        let created = try await send(connection, "POST", "/v1/attachments",
                                     headers: write.merging(["Idempotency-Key": UUID().uuidString]) { $1 }, body: upload)
        try check(created.status == 200, "upload answered \(created.status): \(String(decoding: created.body.prefix(300), as: UTF8.self))")
        report(["step": "upload", "status": created.status, "bytes": upload.count, "seconds": Date().timeIntervalSince(started)])
        started = Date()
        let id = try json(created.body)["id"] as! String
        // A read of this size gets the long budget in the app.
        let response = try await connection.send(TunnelRequest(method: "GET", path: "/v1/artifacts/\(id)/content", headers: auth),
                                                 timeout: TunnelConnection.longTimeout)
        let download = Response(status: response.status, headers: response.headers, body: try response.decodedBody())
        try check(download.status == 200 && download.body == content, "the downloaded content differs")
        report(["step": "download", "status": download.status, "bytes": download.body.count, "seconds": Date().timeIntervalSince(started)])
        let attempts = await connection.connectAttempts
        try check(attempts == 1, "the app transport opened \(attempts) sockets for one session")
        await connection.close()

        // 4. A header value with CR LF could add a header or a request. The Mac ends the session instead.
        let injected = Connection(url: url, authorization: "Bearer \(clientToken)")
        try await injected.handshake(pattern: .ik, key: phone, macKey: macKey, psk: nil)
        do {
            _ = try await injected.request("GET", "/v1/state", headers: auth.merging(["X-Test": "a\r\nX-Coconut-Peer: \(String(repeating: "0", count: 64))"]) { $1 })
            throw Failure(description: "the Mac forwarded a header with CR LF")
        } catch let error as Failure where error.description.hasPrefix("relay:") {
            report(["step": "header injection", "result": error.description])
        }

        // 5. The client token belongs to one phone key. Another key gets no answer from the Mac.
        let stranger = Connection(url: url, authorization: "Bearer \(clientToken)")
        do {
            try await stranger.handshake(pattern: .ik, key: Curve25519.KeyAgreement.PrivateKey(), macKey: macKey, psk: nil)
            throw Failure(description: "the Mac answered another phone key")
        } catch let error as Failure where error.description.hasPrefix("relay:") {
            report(["step": "other key", "result": error.description])
        }

        // 6. A spent ticket does not open the relay again.
        let spent = Connection(url: url, authorization: "Ticket \(TunnelKeys.derive(secret, "coconut pairing ticket").hexString)")
        do {
            _ = try await spent.receiveBinary()
            throw Failure(description: "a spent ticket opened the relay")
        } catch let error as Failure where error.description == "a spent ticket opened the relay" {
            throw error
        } catch {
            report(["step": "spent ticket", "result": "refused"])
        }
    }
}
