import CryptoKit
import Foundation

// The helper of the Mac backend for the team log (docs/team-log.md). The backend uses the Python standard library
// only, so this helper does each step that needs CryptoKit. It compiles with ios/Tunnel/TeamLog.swift, which holds
// the format. backend/coconut/team_helper.py calls it. scripts/team.py compiles it for a source install, and
// scripts/mac_app.py puts it in Coconut.app.
//
// The helper reads one JSON object from standard input and writes one JSON object to standard output. It takes no
// argument, and it reads and writes no file. So a key, a token or a secret is never in a command line or in a log.
//
// - A room ID, an entry ID, a member ID and a token are lowercase hex.
// - A key, a secret, a body, a welcome, a proof and a payload are standard base64, as the relay takes them in JSON.
// - A failure is `{"error": "<code>"}`. The codes are `malformed`, `unknown_version`, `unknown_epoch`, `seal` and
//   `signature`. A code names no value of the input.
//
// Operations (`operation` in the input), with their other input fields and their output:
// - `member`: makes the keys and the token of a member. Output: `member`, `member_key`, `agreement` (the public
//   agreement key as unpadded base64url, as a payload carries it), `agreement_key`, `token`.
// - `proof` (`room`, `token`, `member_key`): `proof` and `hash`, the hash of the token.
// - `seal` (`room`, `entry`, `epoch`, `key`, `member_key`, `payload`): `body`.
// - `open` (`room`, `keys`: epoch to room key, `entries`: a list of `id`, `member`, `body`): `entries`, one result
//   for each entry in the same order: `epoch` and `payload`, or `error`. A result with `unknown_epoch` has `epoch`.
// - `seal_direct` (`room`, `entry`, `agreement_key`, `member`, `other`, `agreement`, `plaintext`): `box`, the box of a
//   direct message as unpadded base64url, as a payload carries it. `member` is this Mac and the writer, `other` the
//   member that the message is for, and `agreement` the public agreement key of `other` as unpadded base64url.
// - `open_direct` (`room`, `agreement_key`, `member`, `boxes`: a list of `entry`, `writer`, `other`, `agreement`,
//   `box`): `boxes`, one result for each box in the same order: `plaintext`, or `error`. The pair key of a box is the
//   one of this Mac and `other`, with the agreement key `agreement` of `other`.
// - `ticket` (`secret`): `ticket` and `hash`, the hash of the ticket.
// - `seal_welcome` (`room`, `secret`, `epoch`, `key`, `admin`, `name`): `welcome`.
// - `open_welcome` (`room`, `secret`, `welcome`): `epoch`, `key`, `admin`, `name`.
// - `link` (`room`, `secret`, `expires`, `relay`, `name`): `link`.
// - `invite` (`link`): `room`, `secret`, `expires`, `relay`, `name`.
//
// The watch mode (`watch`) does not end. It holds the socket of each room (see Watch below), and it ends when its
// standard input closes.
@main
struct TeamHelper {
    struct Input: Decodable {
        struct Entry: Decodable {
            let id: String
            let member: String
            let body: Data
        }

        /// The box of a direct message in a call of `open_direct`. The IDs are lowercase hex, and the agreement key
        /// and the box are unpadded base64url.
        struct Box: Decodable {
            let entry: String
            let writer: String
            let other: String
            let agreement: String
            let box: String
        }

        let operation: String
        var room: String?
        var entry: String?
        var epoch: UInt32?
        var key: Data?
        var keys: [String: Data]?
        var memberKey: Data?
        var payload: Data?
        var entries: [Entry]?
        var secret: Data?
        var welcome: Data?
        var admin: String?
        var name: String?
        var relay: String?
        var expires: UInt32?
        var link: String?
        var token: String?
        var agreementKey: Data?
        var member: String?
        var other: String?
        var agreement: String?
        var plaintext: Data?
        var boxes: [Box]?

        enum CodingKeys: String, CodingKey {
            case operation, room, entry, epoch, key, keys, payload, entries, secret, welcome, admin, name, relay
            case expires, link, token, member, other, agreement, plaintext, boxes
            case memberKey = "member_key"
            case agreementKey = "agreement_key"
        }
    }

    static func main() {
        let input = readInput()
        var output: [String: Any]
        do {
            // JSONDecoder reads each Data field as standard base64.
            output = try run(JSONDecoder().decode(Input.self, from: input))
        } catch let failure as TeamLog.Failure {
            output = ["error": code(failure)]
        } catch {
            // Input that is not the JSON of an operation, or a key that CryptoKit refuses.
            output = ["error": "malformed"]
        }
        let data = (try? JSONSerialization.data(withJSONObject: output)) ?? Data(#"{"error":"malformed"}"#.utf8)
        FileHandle.standardOutput.write(data)
    }

    /// The input of a call that ends. A call of the watch mode does not come back from here.
    ///
    /// A call that ends sends one JSON object without a line break and closes the input. The watch mode sends one
    /// object on each line and keeps the input open. So the helper reads until the first line break or the end of the
    /// input, whichever comes first, and only then knows which of the two it has.
    static func readInput() -> Data {
        let standardInput = FileHandle.standardInput
        var data = Data()
        while true {
            let chunk = standardInput.availableData
            if chunk.isEmpty { return data }
            data.append(chunk)
            if let end = data.firstIndex(of: 0x0A) {
                let line = Data(data[data.startIndex..<end])
                if (try? JSONDecoder().decode(Watch.Request.self, from: line))?.operation == "watch" {
                    Watch.run(first: line, rest: Data(data[(end + 1)...]))
                }
                return data + standardInput.readDataToEndOfFile()
            }
        }
    }

    static func run(_ input: Input) throws -> [String: Any] {
        switch input.operation {
        case "member":
            let member = Curve25519.Signing.PrivateKey(), agreement = Curve25519.KeyAgreement.PrivateKey()
            // CryptoKit fills a new symmetric key with random bytes.
            let token = SymmetricKey(size: .bits256).withUnsafeBytes { Data($0) }
            return ["member": TeamLog.hex(member.publicKey.rawRepresentation),
                    "member_key": member.rawRepresentation.base64EncodedString(),
                    "agreement": TeamLog.base64URL(agreement.publicKey.rawRepresentation),
                    "agreement_key": agreement.rawRepresentation.base64EncodedString(),
                    "token": TeamLog.hex(token)]
        case "proof":
            let token = try need(input.token)
            let proof = try TeamLog.proof(room: bytes(input.room), token: token, member: signer(input.memberKey))
            return ["proof": proof.base64EncodedString(), "hash": TeamLog.hash(token)]
        case "seal":
            let body = try TeamLog.seal(need(input.payload), room: bytes(input.room), entry: bytes(input.entry),
                                        epoch: need(input.epoch), key: SymmetricKey(data: roomKey(input.key)),
                                        member: signer(input.memberKey))
            return ["body": body.base64EncodedString()]
        case "open":
            let room = try bytes(input.room)
            var keys: [UInt32: SymmetricKey] = [:]
            for (epoch, key) in try need(input.keys) {
                guard let epoch = UInt32(epoch) else { throw TeamLog.Failure.malformed }
                keys[epoch] = try SymmetricKey(data: roomKey(key))
            }
            return ["entries": try need(input.entries).map { open($0, room: room, keys: keys) }]
        case "seal_direct":
            let room = try bytes(input.room), member = try bytes(input.member)
            let key = try TeamLog.pairKey(agreement: agreement(input.agreementKey), other: base64URL(input.agreement),
                                          room: room, member: member, otherMember: bytes(input.other))
            let box = try TeamLog.sealDirect(need(input.plaintext), key: key, room: room, entry: bytes(input.entry),
                                             writer: member)
            return ["box": TeamLog.base64URL(box)]
        case "open_direct":
            let room = try bytes(input.room), member = try bytes(input.member)
            let own = try agreement(input.agreementKey)
            return ["boxes": try need(input.boxes).map { openDirect($0, room: room, member: member, agreement: own) }]
        case "ticket":
            let ticket = try TeamLog.ticket(secret: need(input.secret))
            return ["ticket": ticket, "hash": TeamLog.hash(ticket)]
        case "seal_welcome":
            let welcome = try TeamLog.Welcome(epoch: need(input.epoch), key: roomKey(input.key), admin: bytes(input.admin),
                                              name: need(input.name))
            let sealed = try TeamLog.seal(welcome, room: bytes(input.room), secret: need(input.secret))
            return ["welcome": sealed.base64EncodedString()]
        case "open_welcome":
            let welcome = try TeamLog.open(welcome: need(input.welcome), room: bytes(input.room), secret: need(input.secret))
            return ["epoch": Int(welcome.epoch), "key": welcome.key.base64EncodedString(), "admin": TeamLog.hex(welcome.admin),
                    "name": welcome.name]
        case "link":
            let invite = try TeamLog.Invite(room: bytes(input.room), secret: need(input.secret), expires: need(input.expires),
                                            relay: need(input.relay), name: need(input.name))
            return ["link": try TeamLog.link(invite)]
        case "invite":
            let invite = try TeamLog.invite(link: need(input.link))
            return ["room": TeamLog.hex(invite.room), "secret": invite.secret.base64EncodedString(),
                    "expires": Int(invite.expires), "relay": invite.relay, "name": invite.name]
        default:
            throw TeamLog.Failure.malformed
        }
    }

    /// Opens one entry of a batch. A failure of one entry does not stop the batch.
    static func open(_ entry: Input.Entry, room: Data, keys: [UInt32: SymmetricKey]) -> [String: Any] {
        // TeamLog asks for the room key of the epoch that the body names.
        var epoch: UInt32?
        do {
            let opened = try TeamLog.open(entry.body, room: room, entry: bytes(entry.id), member: bytes(entry.member)) {
                epoch = $0
                return keys[$0]
            }
            return ["epoch": Int(opened.epoch), "payload": opened.payload.base64EncodedString()]
        } catch let failure as TeamLog.Failure {
            var result: [String: Any] = ["error": code(failure)]
            if failure == .unknownEpoch, let epoch { result["epoch"] = Int(epoch) }
            return result
        } catch {
            return ["error": "malformed"]
        }
    }

    /// Opens one box of a batch. A failure of one box does not stop the batch.
    static func openDirect(_ box: Input.Box, room: Data, member: Data,
                           agreement: Curve25519.KeyAgreement.PrivateKey) -> [String: Any] {
        do {
            let key = try TeamLog.pairKey(agreement: agreement, other: base64URL(box.agreement), room: room,
                                          member: member, otherMember: bytes(box.other))
            let plaintext = try TeamLog.openDirect(base64URL(box.box), key: key, room: room, entry: bytes(box.entry),
                                                   writer: bytes(box.writer))
            return ["plaintext": plaintext.base64EncodedString()]
        } catch let failure as TeamLog.Failure {
            return ["error": code(failure)]
        } catch {
            return ["error": "malformed"]
        }
    }

    static func code(_ failure: TeamLog.Failure) -> String {
        switch failure {
        case .malformed: "malformed"
        case .unknownVersion: "unknown_version"
        case .unknownEpoch: "unknown_epoch"
        case .seal: "seal"
        case .signature: "signature"
        }
    }

    static func need<Value>(_ value: Value?) throws -> Value {
        guard let value else { throw TeamLog.Failure.malformed }
        return value
    }

    /// The bytes of an ID in lowercase hex. TeamLog checks the length of each ID.
    static func bytes(_ hex: String?) throws -> Data {
        guard let data = TeamLog.bytes(hex: try need(hex)) else { throw TeamLog.Failure.malformed }
        return data
    }

    static func roomKey(_ key: Data?) throws -> Data {
        let key = try need(key)
        guard key.count == TeamLog.longLength else { throw TeamLog.Failure.malformed }
        return key
    }

    static func signer(_ key: Data?) throws -> Curve25519.Signing.PrivateKey {
        try Curve25519.Signing.PrivateKey(rawRepresentation: need(key))
    }

    static func agreement(_ key: Data?) throws -> Curve25519.KeyAgreement.PrivateKey {
        try Curve25519.KeyAgreement.PrivateKey(rawRepresentation: need(key))
    }

    /// The bytes of an unpadded base64url text, as a payload carries a key or a box.
    static func base64URL(_ text: String?) throws -> Data {
        guard let data = TeamLog.bytes(base64URL: try need(text)) else { throw TeamLog.Failure.malformed }
        return data
    }
}

// The watch mode: one socket for each room of the Mac (docs/team-log.md, "Socket"). The socket only says that a room
// has new entries, so the backend then reads them with its own requests.
//
// Input: one JSON object on each line, `{"operation": "watch", "rooms": [{"room": id, "relay": origin, "token": t}]}`.
// A new line replaces the whole set. A room that stays with the same relay and token keeps its socket.
//
// Output: one JSON object on each line. No line has a token or an address.
// - `{"event": "ready", "version": 1}` after the first line. The backend knows from it that this helper has the
//   watch mode. An older helper reads to the end of its input, so it writes nothing while the input is open.
// - `{"event": "open", "room": id}` when a socket opens.
// - `{"event": "seq", "room": id, "seq": n}` for each `{"seq": n}` of the relay.
// - `{"event": "closed", "room": id, "code": c, "status": s, "retry": r}` when a socket closes or does not open.
//   `code` is the close code of the relay and `status` the HTTP status of a refused upgrade, each only when there is
//   one. With `retry` false, the helper does not connect again until the next line: the relay answered 401 or 404,
//   or it closed with 1000 (the room is gone) or 1008 (the member is removed).
// - `{"event": "error", "error": "malformed"}` for a line that is not a valid set. The helper keeps the earlier set.
final class Watch: NSObject, URLSessionWebSocketDelegate, @unchecked Sendable {
    struct Request: Decodable {
        let operation: String
        var rooms: [Room]?
    }

    struct Room: Decodable, Equatable {
        let room: String
        let relay: String
        let token: String
    }

    /// One room and its socket. Only the queue of the watch touches it.
    final class Socket {
        let room: Room
        let url: URL
        var task: URLSessionWebSocketTask?
        var opened: Date?
        /// True while a `ping` has no answer.
        var pinged = false
        /// The closes in a row without a socket that stayed open for `steadySeconds`. They make the wait grow.
        var failures = 0
        var retry: DispatchWorkItem?

        init(room: Room, url: URL) {
            self.room = room
            self.url = url
        }
    }

    /// The helper sends `ping` this often on each open socket. A socket without an answer by the next ping is dead.
    static let pingSeconds = 30.0
    /// The wait before the next connection starts at 1 second and doubles after each close, up to this limit.
    static let maximumWait = 60.0
    /// A socket that stayed open this long resets the wait.
    static let steadySeconds = 30.0

    let queue = DispatchQueue(label: "coconut.team-helper.watch")
    var session: URLSession!
    var sockets: [String: Socket] = [:]
    var timer: DispatchSourceTimer?

    static func run(first: Data, rest: Data) -> Never {
        let watch = Watch()
        let operations = OperationQueue()
        operations.underlyingQueue = watch.queue
        operations.maxConcurrentOperationCount = 1
        // An ephemeral session keeps no cookie, no cache and no credential on the disk.
        watch.session = URLSession(configuration: .ephemeral, delegate: watch, delegateQueue: operations)
        watch.queue.sync {
            watch.emit(["event": "ready", "version": 1])
            watch.update(first)
        }
        let timer = DispatchSource.makeTimerSource(queue: watch.queue)
        timer.schedule(deadline: .now() + pingSeconds, repeating: pingSeconds)
        timer.setEventHandler { watch.ping() }
        timer.resume()
        watch.timer = timer
        let reader = Thread {
            var buffer = rest
            while true {
                while let end = buffer.firstIndex(of: 0x0A) {
                    let line = Data(buffer[buffer.startIndex..<end])
                    buffer = Data(buffer[(end + 1)...])
                    watch.queue.async { watch.update(line) }
                }
                let chunk = FileHandle.standardInput.availableData
                // The backend closed the input: it stopped, or it ends the watch.
                if chunk.isEmpty { exit(0) }
                buffer.append(chunk)
            }
        }
        reader.start()
        dispatchMain()
    }

    /// Writes one line of output. Only the queue of the watch calls it, so two lines never mix.
    func emit(_ object: [String: Any]) {
        guard var data = try? JSONSerialization.data(withJSONObject: object) else { return }
        data.append(0x0A)
        FileHandle.standardOutput.write(data)
    }

    func update(_ line: Data) {
        guard let request = try? JSONDecoder().decode(Request.self, from: line), request.operation == "watch",
              let rooms = request.rooms, let wanted = Self.valid(rooms)
        else {
            emit(["event": "error", "error": "malformed"])
            return
        }
        for (id, socket) in sockets where wanted[id]?.room != socket.room {
            stop(socket)
            sockets[id] = nil
        }
        for (id, entry) in wanted {
            if let socket = sockets[id] {
                // A new line lets a room that the relay refused try again.
                if socket.task == nil && socket.retry == nil { connect(socket) }
            } else {
                let socket = Socket(room: entry.room, url: entry.url)
                sockets[id] = socket
                connect(socket)
            }
        }
    }

    /// The rooms of a line, or nil when one of them does not follow the rules: a room ID and a token of 64 lowercase
    /// hex digits, and a relay that an invite link can name. A release build so takes only an HTTPS relay.
    static func valid(_ rooms: [Room]) -> [String: (room: Room, url: URL)]? {
        var result: [String: (room: Room, url: URL)] = [:]
        for room in rooms {
            guard isHex64(room.room), isHex64(room.token), result[room.room] == nil, TeamLog.validRelay(room.relay),
                  var parts = URLComponents(string: room.relay)
            else { return nil }
            parts.scheme = parts.scheme == "https" ? "wss" : "ws"
            parts.path = "/v1/rooms/\(room.room)/socket"
            guard let url = parts.url else { return nil }
            result[room.room] = (room, url)
        }
        return result
    }

    static func isHex64(_ text: String) -> Bool {
        text.utf8.count == 64 && text.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }
    }

    func connect(_ socket: Socket) {
        socket.retry = nil
        var request = URLRequest(url: socket.url)
        request.setValue("Bearer " + socket.room.token, forHTTPHeaderField: "Authorization")
        let task = session.webSocketTask(with: request)
        socket.task = task
        socket.opened = nil
        socket.pinged = false
        task.resume()
        receive(socket, task)
    }

    func receive(_ socket: Socket, _ task: URLSessionWebSocketTask) {
        task.receive { [weak self] result in
            guard let self else { return }
            self.queue.async {
                guard socket.task === task else { return }
                switch result {
                case .success(let message):
                    socket.pinged = false
                    if case .string(let text) = message, text != "pong",
                       let value = try? JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any],
                       let seq = value["seq"] as? Int, seq >= 0 {
                        self.emit(["event": "seq", "room": socket.room.room, "seq": seq])
                    }
                    self.receive(socket, task)
                case .failure:
                    self.finish(socket, task)
                }
            }
        }
    }

    /// Sends `ping` on each open socket. A socket that did not answer the last ping is dead: the helper closes it
    /// and connects again.
    func ping() {
        for socket in sockets.values {
            guard let task = socket.task, socket.opened != nil else { continue }
            if socket.pinged {
                task.cancel(with: .goingAway, reason: nil)
                finish(socket, task)
                continue
            }
            socket.pinged = true
            task.send(.string("ping")) { _ in }
        }
    }

    func stop(_ socket: Socket) {
        socket.retry?.cancel()
        socket.retry = nil
        let task = socket.task
        socket.task = nil
        task?.cancel(with: .normalClosure, reason: nil)
    }

    /// Ends the life of one task: the relay closed it, it did not open, or the connection broke. Each task ends once.
    func finish(_ socket: Socket, _ task: URLSessionWebSocketTask) {
        guard socket.task === task, sockets[socket.room.room] === socket else { return }
        socket.task = nil
        var event: [String: Any] = ["event": "closed", "room": socket.room.room]
        let code = task.closeCode.rawValue
        if code != 0 { event["code"] = code }
        var status: Int?
        if let response = task.response as? HTTPURLResponse, response.statusCode != 101 {
            status = response.statusCode
            event["status"] = response.statusCode
        }
        // 401: the token is no longer a member's. 404: the room is gone. 1000: the relay deleted the room. 1008: the
        // admin removed this member. Another try gets the same answer, so the helper waits for the next line.
        let final = status == 401 || status == 404 || code == 1000 || code == 1008
        event["retry"] = !final
        emit(event)
        if final { return }
        if let opened = socket.opened, Date().timeIntervalSince(opened) >= Self.steadySeconds { socket.failures = 0 }
        let wait = min(pow(2, Double(socket.failures)), Self.maximumWait) * Double.random(in: 0.8...1.2)
        socket.failures += 1
        let retry = DispatchWorkItem { [weak self, weak socket] in
            guard let self, let socket, self.sockets[socket.room.room] === socket, socket.task == nil else { return }
            self.connect(socket)
        }
        socket.retry = retry
        queue.asyncAfter(deadline: .now() + wait, execute: retry)
    }

    // MARK: URLSessionWebSocketDelegate. The delegate queue runs on the queue of the watch.

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask, didOpenWithProtocol protocol: String?) {
        guard let socket = sockets.values.first(where: { $0.task === webSocketTask }) else { return }
        socket.opened = Date()
        emit(["event": "open", "room": socket.room.room])
    }

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {
        guard let socket = sockets.values.first(where: { $0.task === webSocketTask }) else { return }
        finish(socket, webSocketTask)
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard let task = task as? URLSessionWebSocketTask,
              let socket = sockets.values.first(where: { $0.task === task }) else { return }
        finish(socket, task)
    }

    /// The token goes only to the relay of the room, so the helper follows no redirect.
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}
