import CryptoKit
import Foundation
import Security

// The share extension of the iPhone sends a share to the paired Mac itself, through the relay tunnel of the app
// (docs/relay-transport.md), so the app does not open. It reads the pairing of the app: the route from the list of
// the app (ShareDirectory), and the keys from the Keychain items of the app.

extension ShareFailure {
    static let notPaired = ShareFailure(text: "Open Coconut and pair with your Mac first.")
    static let needsRepair = ShareFailure(text: "This iPhone needs a new pairing. Open Coconut to pair again.")
    static let unreadable = ShareFailure(text: "Your Mac sent an answer that Coconut cannot read. Try again.")

    init(_ error: TunnelError) {
        switch error {
        case .macOffline, .backendDown: self.init(text: "Your Mac is offline. Try again when it is awake.")
        case .relayUnreachable: self.init(text: "Coconut cannot reach the relay. Check the internet connection.")
        case .needsRepair: self = .needsRepair
        case .timedOut, .connectionLost: self.init(text: "Your Mac did not answer in time. Try again.")
        case .tooLarge: self.init(text: "This share is too large for the connection to your Mac.")
        case .protocolViolation: self = .unreadable
        }
    }
}

/// One write to the Mac: a POST under `/v1/` with a JSON body. It gives the body of the answer. A test puts its own
/// transport here.
protocol ShareTransport: Sendable {
    func post(_ path: String, body: Data, key: String) async throws -> Data
}

enum ShareSender {
    /// Sends a share as one message of the chat, as the composer of the app does: each file goes first (an
    /// attachment of the agent chat, or a draft of the team chat), then the message names them. A share to a project
    /// starts a new chat of it first, as the message box of the app does; `newChatKey` stays the same for each try of
    /// one share, so a try whose answer was lost gets the same chat back (Service.mutate keeps each answer under its
    /// key). Each image fits the limit of the Mac first (FileFit), and a file for a team leaves without its GPS
    /// position. No request leaves before each file fits, so a file that cannot go leaves nothing behind. `progress`
    /// gets the number of the request that starts and the count of the requests.
    static func send(_ message: ShareMessage, to destination: ShareDestination, over transport: ShareTransport,
                     newChatKey: String = UUID().uuidString,
                     progress: @Sendable (_ step: Int, _ steps: Int) -> Void = { _, _ in }) async throws {
        guard destination.messagesPath != nil || destination.newChatPath != nil else {
            throw ShareFailure(text: "This chat cannot take a share.")
        }
        guard message.files.isEmpty || destination.takesFiles else { throw ShareFailure(text: ShareProblem.takesNoFiles.text) }
        let forTeam = destination.kind == .team || destination.kind == .direct
        let files = try await Task.detached(priority: .userInitiated) {
            try message.files.map { file -> FittedFile in
                let name = ShareFileName.clean(file.name)
                let outcome = FileFit.outcome(file.data, name: name.isEmpty ? "File" : name, removesPosition: forTeam)
                guard let fitted = outcome.file else {
                    throw ShareFailure(text: "\(file.name): " + (outcome == .tooLarge ? "larger than 4 MB." : FileRules.cannotSendText))
                }
                return fitted
            }
        }.value
        let steps = destination.steps(files: files.count)
        var step = 0
        var target = destination
        if let path = destination.newChatPath {
            step += 1
            progress(step, steps)
            let answer = try await transport.post(path, body: json(["todo": false]), key: newChatKey)
            target = destination.newChat(try id(in: answer))
        }
        guard let messagesPath = target.messagesPath else { throw ShareFailure(text: "This chat cannot take a share.") }
        var ids: [String] = []
        for file in files {
            step += 1
            progress(step, steps)
            let content = file.data.base64EncodedString()
            if target.kind == .agent {
                let answer = try await transport.post("attachments", body: json([
                    "bot_id": target.bot ?? "", "filename": file.name, "content_base64": content,
                ]), key: UUID().uuidString)
                ids.append(try id(in: answer))
            } else {
                guard let path = target.filesPath else { throw ShareFailure(text: ShareProblem.takesNoFiles.text) }
                var body: [String: Any] = ["name": file.name, "content": content]
                if let type = file.type { body["type"] = type }
                if let width = file.width, let height = file.height { body["width"] = width; body["height"] = height }
                let answer = try await transport.post(path, body: json(body), key: UUID().uuidString)
                ids.append(try id(in: answer, under: "file"))
            }
        }
        progress(steps, steps)
        _ = try await transport.post(messagesPath, body: json(body(of: message.text, files: ids, for: target)),
                                     key: UUID().uuidString)
    }

    /// The body of the message request, as the app sends it (MessageRequest and TeamSendRequest in AppStore.swift).
    static func body(of text: String, files: [String], for destination: ShareDestination) -> [String: Any] {
        switch destination.kind {
        case .agent, .project:
            // The Mac needs a text for each message. Files alone go with the marker of the app (AttachmentOnly).
            return ["text": text.isEmpty ? "[attachment]" : text, "mode": "auto", "attachment_ids": files,
                    "recipient_bot_ids": [String]()]
        case .team, .direct:
            var body: [String: Any] = ["text": text]
            if let chat = destination.chat { body["chat"] = chat }
            if !files.isEmpty { body["files"] = files }
            return body
        }
    }

    private static func json(_ body: [String: Any]) throws -> Data {
        try JSONSerialization.data(withJSONObject: body, options: [.sortedKeys])
    }

    private static func id(in answer: Data, under key: String? = nil) throws -> String {
        var object = try? JSONSerialization.jsonObject(with: answer) as? [String: Any]
        if let key { object = object?[key] as? [String: Any] }
        guard let id = object?["id"] as? String, !id.isEmpty else { throw ShareFailure.unreadable }
        return id
    }
}

/// The transport of the extension: one tunnel socket of its own, with the phone key and the tokens of the app.
struct TunnelShareTransport: ShareTransport {
    let tunnel: TunnelConnection
    let token: String

    func post(_ path: String, body: Data, key: String) async throws -> Data {
        let request = TunnelRequest(method: "POST", path: "/v1/" + path, headers: [
            "Accept": "application/json", "Accept-Encoding": "gzip", "Content-Type": "application/json",
            "Authorization": "Bearer " + token, "Idempotency-Key": key,
        ], body: body)
        for attempt in 0...1 {
            do {
                let response = try await tunnel.send(request, timeout: TunnelConnection.longTimeout)
                let data = try response.decodedBody()
                return try Self.checked(data, status: response.status)
            } catch let error as TunnelError {
                // The same request and key go once more on a new socket, as in APIClient.
                guard attempt == 0, error.isRetryable else { throw ShareFailure(error) }
                try await Task.sleep(for: .milliseconds(400))
            }
        }
        throw ShareFailure(.timedOut)
    }

    /// The body of a successful answer. Each other answer is a failure with the text of the Mac.
    static func checked(_ data: Data, status: Int) throws -> Data {
        if (200..<300).contains(status) { return data }
        // The Mac no longer knows this device.
        if status == 401 { throw ShareFailure.needsRepair }
        let reason = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["error"] as? String
        throw ShareFailure(text: reason ?? "Your Mac could not take this share (\(status)).")
    }

    func close() async { await tunnel.close() }
}

/// The pairing of the app, as the extension reads it. The Keychain items are those of APIClient.swift (PairingToken
/// and TunnelCredentials): the extension has the Keychain group of the app in its entitlements, and it only reads.
enum SharePairing {
    private struct Credentials: Decodable {
        let privateKey: Data
        let clientToken: String
    }

    static func transport(for directory: ShareDirectory?) throws -> TunnelShareTransport {
        guard let endpoint = directory?.endpoint, let relay = URL(string: endpoint.relay),
              let macKey = Data(base64Encoded: endpoint.macKey), macKey.count == 32 else { throw ShareFailure.notPaired }
        guard let token = read("local.coconut.device-token").flatMap({ String(data: $0, encoding: .utf8) }),
              let credentials = read("local.coconut.tunnel").flatMap({ try? JSONDecoder().decode(Credentials.self, from: $0) }),
              let phoneKey = try? Curve25519.KeyAgreement.PrivateKey(rawRepresentation: credentials.privateKey)
        else { throw ShareFailure.notPaired }
        let tunnel = TunnelConnection(endpoint: TunnelEndpoint(relay: relay, route: endpoint.route, macKey: macKey),
                                      phoneKey: phoneKey, credential: .token(credentials.clientToken))
        return TunnelShareTransport(tunnel: tunnel, token: token)
    }

    /// One item of the app. A query without a group searches each group of the entitlements of the caller.
    private static func read(_ service: String) -> Data? {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                    kSecAttrService as String: TestIsolation.name(service),
                                    kSecAttrAccount as String: "paired-mac",
                                    kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess else { return nil }
        return result as? Data
    }
}
