import Foundation
import CryptoKit
import Security

enum ServiceError: LocalizedError {
    case configuration(String)
    case response(Int, String)
    case invalidResponse
    case keychain(OSStatus)

    var errorDescription: String? {
        switch self {
        case .configuration(let message): return message
        case .response(_, let message): return message
        case .invalidResponse: return "The Mac returned an unreadable response."
        case .keychain: return "The iPhone could not save its pairing token securely."
        }
    }
}

final class APIClient: @unchecked Sendable {
    private enum Transport {
        // The end-to-end encrypted relay tunnel of a version 2 pairing.
        case tunnel(TunnelConnection)
        #if DEBUG && targetEnvironment(simulator)
        // The test servers and scripts/desktop.py: plain HTTP on the Mac's loopback, which the simulator shares.
        case loopback(baseURL: URL, session: URLSession, foregroundSession: URLSession)
        #endif
    }

    private let transport: Transport
    private let token: String?

    #if DEBUG && targetEnvironment(simulator)
    init(loopback serverURL: String, token: String?) throws {
        guard let components = URLComponents(string: serverURL.trimmingCharacters(in: .whitespacesAndNewlines)),
              components.scheme == "http", let host = components.host, ["localhost", "127.0.0.1", "::1", "[::1]"].contains(host),
              components.user == nil, components.password == nil, components.query == nil, components.fragment == nil,
              components.path.isEmpty || components.path == "/", let url = components.url else {
            throw ServiceError.configuration("Use an http:// address on this Mac's loopback.")
        }
        self.token = token
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 35
        config.timeoutIntervalForResource = 45
        config.urlCache = nil
        config.httpCookieStorage = nil
        config.httpShouldSetCookies = false
        config.waitsForConnectivity = true
        let session = URLSession(configuration: config)
        config.timeoutIntervalForRequest = 4
        config.timeoutIntervalForResource = 6
        config.waitsForConnectivity = false
        transport = .loopback(baseURL: url, session: session, foregroundSession: URLSession(configuration: config))
    }
    #endif

    // The clients of one pairing share its connection, so the app keeps one socket.
    init(tunnel: TunnelConnection, token: String?) {
        transport = .tunnel(tunnel)
        self.token = token
    }

    #if DEBUG && targetEnvironment(simulator)
    deinit {
        if case .loopback(_, let session, let foregroundSession) = transport {
            session.invalidateAndCancel()
            foregroundSession.invalidateAndCancel()
        }
    }
    #endif

    func request<Response: Decodable & Sendable>(_ path: String, method: String = "GET", body: Data? = nil,
                                      idempotencyKey: String? = nil, retryTransient: Bool = true,
                                      fullState: Bool = false) async throws -> Response {
        let data = try await transfer(path, method: method, body: body, idempotencyKey: idempotencyKey,
                                      retryTransient: retryTransient, fullState: fullState)
        do { return try APIJSON.decoder().decode(Response.self, from: data) }
        catch { throw ServiceError.invalidResponse }
    }

    func artifactContent(id: String) async throws -> Data {
        guard UUID(uuidString: id) != nil else { throw ServiceError.configuration("This artifact identifier is invalid.") }
        return try await transfer("artifacts/\(id)/content")
    }

    // Short foreground reads fail fast. Writes, long polls, and full downloads get the longer budget.
    static func isShortRead(_ path: String, method: String, fullState: Bool) -> Bool {
        let wait = URLComponents(string: path)?.queryItems?.first(where: { $0.name == "wait" })?.value.flatMap(Double.init) ?? 0
        return method == "GET" && ((path == "state" && !fullState) || path.hasPrefix("cards/")
            || path.hasPrefix("history/") || path.hasPrefix("search?")
            || (path.hasPrefix("updates?") && wait <= 0))
    }

    private func transfer(_ path: String, method: String = "GET", body: Data? = nil,
                          idempotencyKey: String? = nil, retryTransient: Bool = true,
                          fullState: Bool = false) async throws -> Data {
        let shortRead = Self.isShortRead(path, method: method, fullState: fullState)
        switch transport {
        case .tunnel(let tunnel):
            let request = try makeTunnelRequest(path, method: method, body: body, idempotencyKey: idempotencyKey,
                                                fullState: fullState)
            for attempt in 0...1 {
                do {
                    let response = try await tunnel.send(request, timeout: shortRead ? TunnelConnection.shortTimeout : TunnelConnection.longTimeout)
                    let data = try response.decodedBody()
                    // The Mac no longer knows this device: only a new pairing link helps.
                    if response.status == 401 && token != nil { throw TunnelError.needsRepair }
                    return try Self.checked(data, status: response.status)
                } catch let error as TunnelError where retryTransient && attempt == 0 && error.isRetryable {
                    try await Task.sleep(for: .milliseconds(400))
                }
            }
        #if DEBUG && targetEnvironment(simulator)
        case .loopback(let baseURL, let session, let foregroundSession):
            let request = try makeLoopbackRequest(baseURL, path, method: method, body: body, idempotencyKey: idempotencyKey,
                                                  fullState: fullState)
            for attempt in 0...1 {
                do {
                    let (data, response) = try await (shortRead ? foregroundSession : session).data(for: request)
                    guard let response = response as? HTTPURLResponse else { throw ServiceError.invalidResponse }
                    return try Self.checked(data, status: response.statusCode)
                } catch let error as URLError where retryTransient && attempt == 0 && Self.canRetry(error) {
                    try await Task.sleep(for: .milliseconds(400))
                }
            }
        #endif
        }
        throw ServiceError.invalidResponse
    }

    private static func checked(_ data: Data, status: Int) throws -> Data {
        guard (200..<300).contains(status) else {
            let error = try? JSONDecoder().decode(ErrorBody.self, from: data)
            throw ServiceError.response(status, error?.error ?? "The Mac could not complete this request (\(status)).")
        }
        return data
    }

    #if DEBUG && targetEnvironment(simulator)
    private func makeLoopbackRequest(_ baseURL: URL, _ path: String, method: String, body: Data?,
                                     idempotencyKey: String?, fullState: Bool) throws -> URLRequest {
        guard let url = URL(string: "/v1/" + path, relativeTo: baseURL)?.absoluteURL,
              url.host == baseURL.host, url.scheme == baseURL.scheme, url.port == baseURL.port else {
            throw ServiceError.configuration("The service route is invalid.")
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.httpBody = body
        for (name, value) in headers(path, method: method, body: body, idempotencyKey: idempotencyKey, fullState: fullState) {
            request.setValue(value, forHTTPHeaderField: name)
        }
        return request
    }

    private static func canRetry(_ error: URLError) -> Bool {
        [.networkConnectionLost, .timedOut, .cannotConnectToHost, .cannotFindHost, .dnsLookupFailed, .notConnectedToInternet].contains(error.code)
    }
    #endif

    func makeTunnelRequest(_ path: String, method: String = "GET", body: Data? = nil,
                           idempotencyKey: String? = nil, fullState: Bool = false) throws -> TunnelRequest {
        // The path goes into the Mac's HTTP request line as it is, so it must be visible ASCII and stay on the Mac.
        let origin = URL(string: "http://mac.invalid")!
        guard path.utf8.allSatisfy({ (33...126).contains($0) }),
              let url = URL(string: "/v1/" + path, relativeTo: origin)?.absoluteURL, url.host == origin.host,
              url.scheme == origin.scheme, url.port == nil else {
            throw ServiceError.configuration("The service route is invalid.")
        }
        var headers = headers(path, method: method, body: body, idempotencyKey: idempotencyKey, fullState: fullState)
        headers["Accept-Encoding"] = "gzip"
        return TunnelRequest(method: method, path: "/v1/" + path, headers: headers, body: body ?? Data())
    }

    private func headers(_ path: String, method: String, body: Data?, idempotencyKey: String?, fullState: Bool) -> [String: String] {
        var headers = ["Accept": "application/json"]
        // Task details loads older finished Work on request, so recent content can leave it out.
        if path == "state" && !fullState {
            headers["X-Coconut-State"] = "recent"
            headers["X-Coconut-Work"] = "recent"
        }
        if path.hasPrefix("updates?") {
            headers["X-Coconut-Updates"] = "4"
            headers["X-Coconut-Work"] = "recent"
        }
        if body != nil { headers["Content-Type"] = "application/json" }
        if let token { headers["Authorization"] = "Bearer " + token }
        if method != "GET" {
            // The same request and key survive the network retry.
            headers["Idempotency-Key"] = idempotencyKey ?? UUID().uuidString
        }
        return headers
    }

    private struct ErrorBody: Decodable { let error: String }
}

// One generic password in the Keychain, readable only on this device while it is unlocked.
struct KeychainItem {
    let service: String
    var account = "paired-mac"

    func read() -> Data? {
        var query = baseQuery
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess else { return nil }
        return result as? Data
    }

    func save(_ data: Data) throws {
        let attributes: [String: Any] = [kSecValueData as String: data,
                                         kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly]
        let update = SecItemUpdate(baseQuery as CFDictionary, attributes as CFDictionary)
        logStatus("update", status: update)
        if update == errSecSuccess { return }
        guard update == errSecItemNotFound else { throw ServiceError.keychain(update) }
        var query = baseQuery
        attributes.forEach { query[$0.key] = $0.value }
        let status = SecItemAdd(query as CFDictionary, nil)
        logStatus("add", status: status)
        guard status == errSecSuccess else { throw ServiceError.keychain(status) }
    }

    func delete() { SecItemDelete(baseQuery as CFDictionary) }

    private func logStatus(_ operation: String, status: OSStatus) {
        #if DEBUG
        NSLog("Coconut Keychain %@ status: %d", operation, status)
        #endif
    }

    private var baseQuery: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: account]
    }
}

enum PairingToken {
    private static let item = KeychainItem(service: "local.coconut.device-token")

    static func read() -> String? { item.read().flatMap { String(data: $0, encoding: .utf8) } }
    static func save(_ token: String) throws { try item.save(Data(token.utf8)) }
    static func delete() { item.delete() }
}

// The phone's static Noise key and the relay client token of a version 2 pairing. Each pairing makes a new key.
struct TunnelCredentials: Codable, Equatable {
    private static let item = KeychainItem(service: "local.coconut.tunnel")

    let privateKey: Data
    let clientToken: String

    init(phoneKey: Curve25519.KeyAgreement.PrivateKey, clientToken: String) {
        privateKey = phoneKey.rawRepresentation
        self.clientToken = clientToken
    }

    var phoneKey: Curve25519.KeyAgreement.PrivateKey? { try? .init(rawRepresentation: privateKey) }

    static func read() -> Self? { item.read().flatMap { try? JSONDecoder().decode(Self.self, from: $0) } }
    func save() throws { try Self.item.save(try JSONEncoder().encode(self)) }
    static func delete() { item.delete() }
}
