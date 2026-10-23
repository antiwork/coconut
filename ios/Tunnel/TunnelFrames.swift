import Foundation

// Frames inside the encrypted tunnel. The relay sees only `channel | ciphertext` (RelayFrame);
// one ciphertext holds one TunnelFrame: `type u8 | stream u32 | flags u8 | payload`.
enum TunnelFrameType: UInt8, Sendable {
    case requestHead = 1   // JSON {method, path, headers}
    case body = 2          // At most `TunnelFrame.maxPayload` bytes of a request or response body
    case end = 3           // The body of this stream is complete
    case responseHead = 4  // JSON {status, headers}
    case cancel = 5        // The sender no longer wants this stream
    case ping = 6
    case pong = 7
    case rekey = 8         // The sender rekeys its send key after this frame
    case window = 9        // payload: u32 bytes that the receiver may now send on this stream
    case error = 10        // JSON {code}; see TunnelErrorCode
}

enum TunnelErrorCode: String, Codable, Sendable {
    case macOffline = "mac_offline"
    case backendDown = "backend_down"
    case tooLarge = "too_large"
}

enum TunnelFrameError: Error, Equatable {
    case invalidRekey
    case truncated
    case unknownType(UInt8)
    case tooLarge
}

struct TunnelFrame: Equatable, Sendable {
    static let headerSize = 6
    // A Noise message is at most 65,535 bytes, including the 16-byte tag.
    static let maxPayload = 60 * 1024
    // The initial window for each stream. WINDOW frames extend it.
    static let initialWindow: UInt32 = 1024 * 1024

    var type: TunnelFrameType
    var stream: UInt32
    var flags: UInt8 = 0
    var payload = Data()

    func encoded() throws -> Data {
        guard payload.count <= Self.maxPayload else { throw TunnelFrameError.tooLarge }
        var data = Data([type.rawValue])
        withUnsafeBytes(of: stream.bigEndian) { data.append(contentsOf: $0) }
        data.append(flags)
        data.append(payload)
        return data
    }

    static func decode(_ data: Data) throws -> TunnelFrame {
        guard data.count >= headerSize else { throw TunnelFrameError.truncated }
        guard data.count - headerSize <= maxPayload else { throw TunnelFrameError.tooLarge }
        let bytes = [UInt8](data.prefix(headerSize))
        guard let type = TunnelFrameType(rawValue: bytes[0]) else { throw TunnelFrameError.unknownType(bytes[0]) }
        let stream = bytes[1...4].reduce(UInt32(0)) { $0 << 8 | UInt32($1) }
        return TunnelFrame(type: type, stream: stream, flags: bytes[5], payload: Data(data.dropFirst(headerSize)))
    }

    // A body larger than one frame becomes several BODY frames on the same stream. An empty body gives
    // no BODY frames: END always closes the body.
    static func bodyFrames(_ body: Data, stream: UInt32) -> [TunnelFrame] {
        stride(from: 0, to: body.count, by: maxPayload).map { start in
            let slice = body[body.startIndex + start ..< body.startIndex + min(start + maxPayload, body.count)]
            return TunnelFrame(type: .body, stream: stream, payload: Data(slice))
        }
    }

    static func window(_ bytes: UInt32, stream: UInt32) -> TunnelFrame {
        var payload = Data()
        withUnsafeBytes(of: bytes.bigEndian) { payload.append(contentsOf: $0) }
        return TunnelFrame(type: .window, stream: stream, payload: payload)
    }
}

// What the relay forwards. The relay adds the channel toward the Mac and removes it toward the iPhone.
struct RelayFrame: Equatable, Sendable {
    // Channel 0 carries relay control messages from the Mac, never tunnel data.
    static let controlChannel: UInt32 = 0
    // The channel and one Noise message of at most 65,535 bytes.
    static let maxSize = 4 + 65_535

    var channel: UInt32
    var ciphertext: Data

    func encoded() -> Data {
        var data = Data()
        withUnsafeBytes(of: channel.bigEndian) { data.append(contentsOf: $0) }
        return data + ciphertext
    }

    static func decode(_ data: Data) throws -> RelayFrame {
        guard data.count >= 4 else { throw TunnelFrameError.truncated }
        guard data.count <= maxSize else { throw TunnelFrameError.tooLarge }
        let channel = data.prefix(4).reduce(UInt32(0)) { $0 << 8 | UInt32($1) }
        return RelayFrame(channel: channel, ciphertext: Data(data.dropFirst(4)))
    }
}

// Seals and opens frames for one connection. Any failure ends the session; the iPhone then reconnects
// with a new handshake, so a dropped, repeated, or changed frame never reaches the app.
// This is a class, so the live ciphers have exactly one owner and are never copied.
final class TunnelSession {
    static let rekeyAfterMessages: UInt64 = 1 << 16
    static let rekeyAfterSeconds: TimeInterval = 30 * 60

    private var transport: NoiseTransport
    private var sentSinceRekey: UInt64 = 0
    private var lastRekey: Date
    private(set) var closed = false

    var handshakeHash: Data { transport.handshakeHash }
    var remoteStatic: Data { transport.remoteStatic }

    init(transport: consuming NoiseTransport, now: Date = Date()) {
        self.transport = transport
        lastRekey = now
    }

    // Returns one ciphertext, or two when a REKEY frame must go first.
    func seal(_ frame: TunnelFrame, now: Date = Date()) throws -> [Data] {
        guard !closed else { throw NoiseError.wrongState }
        // A REKEY frame always changes the send key, so both sides keep the same key.
        if frame.type == .rekey { return [try sealRekey(now: now)] }
        var output: [Data] = []
        if sentSinceRekey >= Self.rekeyAfterMessages || now.timeIntervalSince(lastRekey) >= Self.rekeyAfterSeconds {
            output.append(try sealRekey(now: now))
        }
        output.append(try sealOne(frame))
        return output
    }

    private func sealRekey(now: Date) throws -> Data {
        let ciphertext = try sealOne(TunnelFrame(type: .rekey, stream: 0))
        transport.send.rekey()
        sentSinceRekey = 0
        lastRekey = now
        return ciphertext
    }

    private func sealOne(_ frame: TunnelFrame) throws -> Data {
        do {
            let ciphertext = try transport.send.encrypt(try frame.encoded(), ad: Data())
            sentSinceRekey += 1
            return ciphertext
        } catch {
            closed = true
            throw error
        }
    }

    // Returns nil for a REKEY frame, which only changes the receive key.
    func open(_ ciphertext: Data) throws -> TunnelFrame? {
        guard !closed else { throw NoiseError.wrongState }
        do {
            let frame = try TunnelFrame.decode(try transport.receive.decrypt(ciphertext, ad: Data()))
            if frame.type == .rekey {
                guard frame.stream == 0, frame.flags == 0, frame.payload.isEmpty else { throw TunnelFrameError.invalidRekey }
                transport.receive.rekey()
                return nil
            }
            return frame
        } catch {
            closed = true
            throw error
        }
    }
}
