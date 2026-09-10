import Foundation
import CryptoKit

@main
struct PushEncrypt {
    struct Request: Decodable {
        let publicKey: Data
        let id: String
        let expires: Int
        let content: Data
    }

    static func main() {
        umask(0o077)
        do {
            guard CommandLine.arguments.count == 3 else { throw CocoaError(.fileReadInvalidFileName) }
            let keyURL = URL(fileURLWithPath: CommandLine.arguments[1]).appendingPathComponent("sender.key")
            let operation = CommandLine.arguments[2]
            if operation == "setup", !FileManager.default.fileExists(atPath: keyURL.path) {
                let key = Curve25519.KeyAgreement.PrivateKey()
                try key.rawRepresentation.write(to: keyURL, options: .withoutOverwriting)
            }
            let attributes = try FileManager.default.attributesOfItem(atPath: keyURL.path)
            guard attributes[.type] as? FileAttributeType == .typeRegular,
                  (attributes[.posixPermissions] as? Int ?? 0o777) & 0o077 == 0 else { throw CocoaError(.fileReadNoPermission) }
            let sender = try Curve25519.KeyAgreement.PrivateKey(rawRepresentation: Data(contentsOf: keyURL))
            let output: Data
            if operation == "public" || operation == "setup" {
                output = try JSONSerialization.data(withJSONObject: ["sender_public_key": sender.publicKey.rawRepresentation.base64EncodedString()])
            } else if operation == "seal" {
                let data = FileHandle.standardInput.readData(ofLength: 8193)
                guard data.count <= 8192 else { throw CocoaError(.coderInvalidValue) }
                let input = try JSONDecoder().decode(Request.self, from: data)
                let recipient = try Curve25519.KeyAgreement.PublicKey(rawRepresentation: input.publicKey)
                let envelope = try PushEnvelope.seal(input.content, id: input.id, expires: input.expires, recipient: recipient, sender: sender)
                output = try JSONEncoder().encode(envelope)
            } else { throw CocoaError(.coderInvalidValue) }
            FileHandle.standardOutput.write(output)
        } catch {
            FileHandle.standardError.write(Data("Notification encryption failed.\n".utf8))
            exit(1)
        }
    }
}
