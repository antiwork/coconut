import Foundation
import CryptoKit
import DeviceCheck

struct PushRelayConfiguration: Decodable, Sendable {
    let configured: Bool
    let relayUrl: String?
    let senderPublicKey: Data?
}

struct PushRelayGrant: Codable, Sendable {
    let routeId: String
    let sendToken: String
    let publicKey: Data
}

@MainActor
final class PushRelayClient {
    static let shared = PushRelayClient()
    private struct Credential: Codable {
        let routeId: String
        let sendToken: String
        let manageToken: String
    }
    private struct State: Codable {
        var keyId: String
        var signature: String?
        var credential: Credential?
        var pending: Data?
    }
    private struct Challenge: Decodable { let challenge: String; let registered: Bool }
    private struct Revocation: Codable { let routeId: String; let token: String }
    private let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 15
        config.timeoutIntervalForResource = 20
        config.httpCookieStorage = nil
        config.urlCache = nil
        return URLSession(configuration: config, delegate: RelayRedirects(), delegateQueue: nil)
    }()

    private var baseURL: URL? {
        guard let text = Bundle.main.object(forInfoDictionaryKey: "CoconutPushRelayURL") as? String,
              let url = URL(string: text), url.scheme == "https", url.host != nil,
              url.user == nil, url.password == nil, url.query == nil, url.fragment == nil else { return nil }
        return url
    }

    func register(deviceId: String, token: String, environment: String, configuration: PushRelayConfiguration) async throws -> PushRelayGrant {
        guard let baseURL, configuration.configured, configuration.relayUrl == baseURL.absoluteString,
              let senderKey = configuration.senderPublicKey, DCAppAttestService.shared.isSupported else {
            throw CocoaError(.featureUnsupported)
        }
        await flushRevocations()
        let binding = try NotificationKeys.binding(deviceId: deviceId, senderKey: senderKey)
        let signature = try [deviceId, token, environment, binding.keyId, senderKey.base64EncodedString()].joined(separator: ":")
        let account = "relay:\(deviceId):\(environment)"
        var state = try NotificationKeys.read(State.self, account: account)
        if state?.signature == signature, let credential = state?.credential {
            return try PushRelayGrant(routeId: credential.routeId, sendToken: credential.sendToken, publicKey: binding.publicKey)
        }
        if state == nil {
            state = State(keyId: try await DCAppAttestService.shared.generateKey())
            try NotificationKeys.save(state!, account: account)
        }
        var current = state!
        if let pending = current.pending {
            if let result = try? await request("v1/register", body: pending),
               let credential = try? decoder.decode(Credential.self, from: result) {
                current.credential = credential
                current.pending = nil
                try NotificationKeys.save(current, account: account)
                // A token can change while a previous registration waits for its response.
            } else { current.pending = nil }
        }
        let challengeData = try await request("v1/challenge", body: JSONEncoder().encode(["key_id": current.keyId]))
        let challenge = try decoder.decode(Challenge.self, from: challengeData)
        let payload = try JSONSerialization.data(withJSONObject: ["challenge": challenge.challenge,
            "device_id": deviceId, "token": token, "environment": environment,
            "encryption_key": binding.publicKey.base64EncodedString()], options: [.sortedKeys])
        let digest = Data(SHA256.hash(data: payload))
        var input = ["key_id": current.keyId, "payload": payload.base64EncodedString()]
        if challenge.registered {
            input["assertion"] = try await DCAppAttestService.shared.generateAssertion(current.keyId, clientDataHash: digest).base64EncodedString()
        } else {
            do {
                input["attestation"] = try await DCAppAttestService.shared.attestKey(current.keyId, clientDataHash: digest).base64EncodedString()
            } catch {
                // Apple permits one attestation per key. A lost enrollment can require a new key.
                if (error as NSError).code == DCError.invalidKey.rawValue { NotificationKeys.delete(account: account) }
                throw error
            }
        }
        let body = try JSONEncoder().encode(input)
        current.pending = body
        try NotificationKeys.save(current, account: account)
        let result = try await request("v1/register", body: body)
        let credential = try decoder.decode(Credential.self, from: result)
        guard credential.routeId.count == 64, credential.sendToken.count == 64, credential.manageToken.count == 64 else {
            throw CocoaError(.coderInvalidValue)
        }
        current.credential = credential; current.signature = signature; current.pending = nil
        try NotificationKeys.save(current, account: account)
        return try PushRelayGrant(routeId: credential.routeId, sendToken: credential.sendToken, publicKey: binding.publicKey)
    }

    func disable(deviceId: String) {
        var revocations = (try? NotificationKeys.read([Revocation].self, account: "revocations")) ?? []
        for environment in ["development", "production"] {
            let account = "relay:\(deviceId):\(environment)"
            if var state = try? NotificationKeys.read(State.self, account: account), let credential = state.credential {
                revocations.removeAll { $0.routeId == credential.routeId }
                revocations.append(Revocation(routeId: credential.routeId, token: credential.manageToken))
                state.credential = nil; state.signature = nil; state.pending = nil
                try? NotificationKeys.save(state, account: account)
            }
        }
        try? NotificationKeys.save(revocations, account: "revocations")
        Task { await flushRevocations() }
    }

    func flushRevocations() async {
        guard let items = try? NotificationKeys.read([Revocation].self, account: "revocations") else { return }
        for item in items {
            if (try? await request("v1/routes/\(item.routeId)", method: "DELETE", token: item.token)) != nil {
                var pending = (try? NotificationKeys.read([Revocation].self, account: "revocations")) ?? []
                pending.removeAll { $0.routeId == item.routeId && $0.token == item.token }
                try? NotificationKeys.save(pending, account: "revocations")
            }
        }
    }

    private var decoder: JSONDecoder {
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase; return decoder
    }

    private func request(_ path: String, method: String = "POST", body: Data? = nil, token: String? = nil) async throws -> Data {
        guard let baseURL else { throw CocoaError(.featureUnsupported) }
        var request = URLRequest(url: baseURL.appending(path: path))
        request.httpMethod = method; request.httpBody = body
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let token { request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse, response.statusCode == 200, data.count < 24000 else {
            throw URLError(.badServerResponse)
        }
        return data
    }
}

private final class RelayRedirects: NSObject, URLSessionTaskDelegate, Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping @Sendable (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}
