import CryptoKit
import Foundation

// Noise IK and IKpsk2 with 25519, ChaChaPoly, and SHA-256 (https://noiseprotocol.org/noise.html).
// The iPhone app and the Mac tunnel sidecar compile this same file. The iPhone is always the initiator:
// it knows the Mac's static key from the pairing link, so only the paired Mac can finish a handshake.
enum NoiseError: Error, Equatable {
    static let maxMessage = 65_535

    case invalidKey
    case invalidMessage
    case decryptionFailed
    case nonceExhausted
    case wrongState
}

enum NoisePattern: Sendable {
    // Normal sessions after pairing.
    case ik
    // Pairing: the one-time secret from the link becomes the pre-shared key.
    case ikpsk2

    var protocolName: String {
        switch self {
        case .ik: "Noise_IK_25519_ChaChaPoly_SHA256"
        case .ikpsk2: "Noise_IKpsk2_25519_ChaChaPoly_SHA256"
        }
    }
}

// A cipher holds live key and nonce state. Never copy one that is in use: two copies would reuse
// nonces. TunnelSession, a class, owns the ciphers of an open connection.
struct NoiseCipher: Sendable {
    static let maxNonce = UInt64.max
    private var key: SymmetricKey?
    private(set) var nonce: UInt64 = 0

    init(key: SymmetricKey? = nil) { self.key = key }

    var hasKey: Bool { key != nil }

    private static func nonceBytes(_ value: UInt64) -> ChaChaPoly.Nonce {
        var bytes = Data(repeating: 0, count: 4)
        withUnsafeBytes(of: value.littleEndian) { bytes.append(contentsOf: $0) }
        return try! ChaChaPoly.Nonce(data: bytes)
    }

    mutating func encrypt(_ plaintext: Data, ad: Data) throws -> Data {
        guard let key else { return plaintext }
        // The last nonce value is reserved for rekey.
        guard nonce < Self.maxNonce else { throw NoiseError.nonceExhausted }
        let box = try ChaChaPoly.seal(plaintext, using: key, nonce: Self.nonceBytes(nonce), authenticating: ad)
        nonce += 1
        return box.ciphertext + box.tag
    }

    mutating func decrypt(_ ciphertext: Data, ad: Data) throws -> Data {
        guard let key else { return ciphertext }
        guard nonce < Self.maxNonce else { throw NoiseError.nonceExhausted }
        guard ciphertext.count >= 16 else { throw NoiseError.decryptionFailed }
        let body = ciphertext.prefix(ciphertext.count - 16), tag = ciphertext.suffix(16)
        do {
            let box = try ChaChaPoly.SealedBox(nonce: Self.nonceBytes(nonce), ciphertext: body, tag: tag)
            let plaintext = try ChaChaPoly.open(box, using: key, authenticating: ad)
            nonce += 1
            return plaintext
        } catch {
            throw NoiseError.decryptionFailed
        }
    }

    // Noise section 11.3. The nonce n is unchanged.
    mutating func rekey() {
        guard let key else { return }
        let zeros = Data(repeating: 0, count: 32)
        let box = try! ChaChaPoly.seal(zeros, using: key, nonce: Self.nonceBytes(Self.maxNonce), authenticating: Data())
        self.key = SymmetricKey(data: box.ciphertext.prefix(32))
    }
}

private struct SymmetricState {
    var cipher = NoiseCipher()
    var chainingKey: Data
    var hash: Data

    init(protocolName: String) {
        let name = Data(protocolName.utf8)
        hash = name.count <= 32 ? name + Data(repeating: 0, count: 32 - name.count) : Data(SHA256.hash(data: name))
        chainingKey = hash
    }

    static func hmac(_ key: Data, _ data: Data) -> Data {
        Data(HMAC<SHA256>.authenticationCode(for: data, using: SymmetricKey(data: key)))
    }

    // The Noise HKDF, built from HMAC-SHA256 as the specification defines it.
    static func hkdf(_ chainingKey: Data, _ input: Data, outputs: Int) -> [Data] {
        let temporary = hmac(chainingKey, input)
        var results: [Data] = []
        var previous = Data()
        for index in 1...outputs {
            previous = hmac(temporary, previous + Data([UInt8(index)]))
            results.append(previous)
        }
        return results
    }

    mutating func mixHash(_ data: Data) { hash = Data(SHA256.hash(data: hash + data)) }

    mutating func mixKey(_ input: Data) {
        let output = Self.hkdf(chainingKey, input, outputs: 2)
        chainingKey = output[0]
        cipher = NoiseCipher(key: SymmetricKey(data: output[1]))
    }

    mutating func mixKeyAndHash(_ input: Data) {
        let output = Self.hkdf(chainingKey, input, outputs: 3)
        chainingKey = output[0]
        mixHash(output[1])
        cipher = NoiseCipher(key: SymmetricKey(data: output[2]))
    }

    mutating func encryptAndHash(_ plaintext: Data) throws -> Data {
        let ciphertext = try cipher.encrypt(plaintext, ad: hash)
        mixHash(ciphertext)
        return ciphertext
    }

    mutating func decryptAndHash(_ ciphertext: Data) throws -> Data {
        let plaintext = try cipher.decrypt(ciphertext, ad: hash)
        mixHash(ciphertext)
        return plaintext
    }

    func split() -> (NoiseCipher, NoiseCipher) {
        let output = Self.hkdf(chainingKey, Data(), outputs: 2)
        return (NoiseCipher(key: SymmetricKey(data: output[0])), NoiseCipher(key: SymmetricKey(data: output[1])))
    }
}

// The transport keys after a completed handshake.
struct NoiseTransport: Sendable {
    var send: NoiseCipher
    var receive: NoiseCipher
    // Both sides bind application data to this value, for example in a pairing proof.
    let handshakeHash: Data
    let remoteStatic: Data
}

struct NoiseHandshake {
    typealias PrivateKey = Curve25519.KeyAgreement.PrivateKey

    let pattern: NoisePattern
    let initiator: Bool
    private var state: SymmetricState
    private let localStatic: PrivateKey
    private var localEphemeral: PrivateKey?
    private(set) var remoteStatic: Data?
    private var remoteEphemeral: Data?
    private let psk: Data?
    private var step = 0
    private var failed = false
    private var split = false

    init(pattern: NoisePattern, initiator: Bool, localStatic: PrivateKey, remoteStatic: Data? = nil,
         psk: Data? = nil, prologue: Data = Data()) throws {
        try self.init(pattern: pattern, initiator: initiator, localStatic: localStatic, remoteStatic: remoteStatic,
                      psk: psk, prologue: prologue, fixedEphemeral: nil)
    }

    #if DEBUG
    // Only the test vectors set the ephemeral key. A reused ephemeral key breaks forward secrecy.
    init(pattern: NoisePattern, initiator: Bool, localStatic: PrivateKey, remoteStatic: Data? = nil,
         psk: Data? = nil, prologue: Data = Data(), ephemeral: PrivateKey) throws {
        try self.init(pattern: pattern, initiator: initiator, localStatic: localStatic, remoteStatic: remoteStatic,
                      psk: psk, prologue: prologue, fixedEphemeral: ephemeral)
    }
    #endif

    private init(pattern: NoisePattern, initiator: Bool, localStatic: PrivateKey, remoteStatic: Data?,
                 psk: Data?, prologue: Data, fixedEphemeral ephemeral: PrivateKey?) throws {
        guard (pattern == .ikpsk2) == (psk?.count == 32) else { throw NoiseError.invalidKey }
        guard !initiator || remoteStatic?.count == 32 else { throw NoiseError.invalidKey }
        self.pattern = pattern
        self.initiator = initiator
        self.localStatic = localStatic
        self.remoteStatic = remoteStatic
        self.psk = psk
        localEphemeral = ephemeral
        state = SymmetricState(protocolName: pattern.protocolName)
        state.mixHash(prologue)
        // IK pre-message: <- s
        state.mixHash(initiator ? remoteStatic! : localStatic.publicKey.rawRepresentation)
    }

    private static func dh(_ privateKey: PrivateKey, _ publicKey: Data) throws -> Data {
        guard let key = try? Curve25519.KeyAgreement.PublicKey(rawRepresentation: publicKey),
              let secret = try? privateKey.sharedSecretFromKeyAgreement(with: key) else { throw NoiseError.invalidKey }
        let output = secret.withUnsafeBytes { Data($0) }
        // A low-order public key gives an all-zero secret. Reject it on every platform.
        guard output.contains(where: { $0 != 0 }) else { throw NoiseError.invalidKey }
        return output
    }

    private mutating func writeEphemeral(into message: inout Data) {
        let ephemeral = localEphemeral ?? PrivateKey()
        localEphemeral = ephemeral
        let publicKey = ephemeral.publicKey.rawRepresentation
        message += publicKey
        state.mixHash(publicKey)
        if psk != nil { state.mixKey(publicKey) }
    }

    private mutating func readEphemeral(from message: Data, at offset: inout Int) throws {
        guard message.count >= offset + 32 else { throw NoiseError.invalidMessage }
        let publicKey = Data(message[message.startIndex + offset ..< message.startIndex + offset + 32])
        offset += 32
        remoteEphemeral = publicKey
        state.mixHash(publicKey)
        if psk != nil { state.mixKey(publicKey) }
    }

    // Message 1 (-> e, es, s, ss) from the iPhone, or message 2 (<- e, ee, se[, psk]) from the Mac.
    mutating func writeMessage(payload: Data = Data()) throws -> Data {
        guard !failed else { throw NoiseError.wrongState }
        do { return try write(payload: payload) } catch { failed = true; throw error }
    }

    private mutating func write(payload: Data) throws -> Data {
        var message = Data()
        if initiator && step == 0 {
            writeEphemeral(into: &message)
            state.mixKey(try Self.dh(localEphemeral!, remoteStatic!))
            message += try state.encryptAndHash(localStatic.publicKey.rawRepresentation)
            state.mixKey(try Self.dh(localStatic, remoteStatic!))
        } else if !initiator && step == 1 {
            writeEphemeral(into: &message)
            state.mixKey(try Self.dh(localEphemeral!, remoteEphemeral!))
            state.mixKey(try Self.dh(localEphemeral!, remoteStatic!))
            if let psk { state.mixKeyAndHash(psk) }
        } else {
            throw NoiseError.wrongState
        }
        message += try state.encryptAndHash(payload)
        guard message.count <= NoiseError.maxMessage else { throw NoiseError.invalidMessage }
        step += 1
        return message
    }

    // The Mac must check `remoteStatic` against its paired devices after it reads message 1, and before it
    // writes message 2. The handshake accepts any initiator key, and message 1 can be replayed.
    mutating func readMessage(_ message: Data) throws -> Data {
        guard !failed else { throw NoiseError.wrongState }
        do { return try read(message) } catch { failed = true; throw error }
    }

    private mutating func read(_ message: Data) throws -> Data {
        guard message.count <= NoiseError.maxMessage else { throw NoiseError.invalidMessage }
        var offset = 0
        if !initiator && step == 0 {
            try readEphemeral(from: message, at: &offset)
            state.mixKey(try Self.dh(localStatic, remoteEphemeral!))
            guard message.count >= offset + 48 else { throw NoiseError.invalidMessage }
            let sealed = Data(message[message.startIndex + offset ..< message.startIndex + offset + 48])
            offset += 48
            let staticKey = try state.decryptAndHash(sealed)
            remoteStatic = staticKey
            state.mixKey(try Self.dh(localStatic, staticKey))
        } else if initiator && step == 1 {
            try readEphemeral(from: message, at: &offset)
            state.mixKey(try Self.dh(localEphemeral!, remoteEphemeral!))
            state.mixKey(try Self.dh(localStatic, remoteEphemeral!))
            if let psk { state.mixKeyAndHash(psk) }
        } else {
            throw NoiseError.wrongState
        }
        let payload = try state.decryptAndHash(Data(message[(message.startIndex + offset)...]))
        step += 1
        return payload
    }

    var isComplete: Bool { step == 2 }

    // The handshake gives its keys out once. A second call would return the same keys at nonce 0.
    mutating func transport() throws -> NoiseTransport {
        guard isComplete, !failed, !split, let remoteStatic else { throw NoiseError.wrongState }
        split = true
        let (first, second) = state.split()
        return NoiseTransport(send: initiator ? first : second, receive: initiator ? second : first,
                              handshakeHash: state.hash, remoteStatic: remoteStatic)
    }
}
