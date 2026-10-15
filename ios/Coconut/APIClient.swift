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

enum ConnectionFailure {
    static func message(for error: URLError, serverURL: String) -> String {
        let host = URLComponents(string: serverURL)?.host?.lowercased() ?? ""
        let access = host.hasSuffix(".ts.net")
            ? "Check that Tailscale is connected on this iPhone and your Mac."
            : "Check network access to your Mac."
        let message: String
        let category: String
        if error.networkUnavailableReason == .cellular || error.code == .dataNotAllowed {
            message = "iOS did not allow cellular data for this request. Check Coconut’s cellular access in Settings."
            category = "Cellular"
        } else {
            switch error.code {
            case .cannotFindHost, .dnsLookupFailed:
                message = "The phone cannot resolve the Mac address. " + access
                category = "DNS"
            case .cannotConnectToHost:
                message = "The phone cannot connect to the Mac at this address and port. " + access
                category = "Connection"
            case .notConnectedToInternet:
                message = "The phone has no usable network connection for this request. " + access
                category = "Network"
            case .timedOut:
                message = "The Mac did not respond in time. " + access
                category = "Timeout"
            case .networkConnectionLost:
                message = "The connection ended during the request. Reconnect to check the current state."
                category = "Interrupted"
            case .internationalRoamingOff:
                message = "iOS did not allow this request while roaming. Check cellular settings or connect to Wi-Fi."
                category = "Roaming"
            case .appTransportSecurityRequiresSecureConnection:
                message = "iOS rejected the connection under its transport security rules. Check the Mac’s HTTPS setup."
                category = "Security"
            case .serverCertificateHasBadDate, .serverCertificateNotYetValid:
                message = "The Mac certificate is outside its valid dates. Check both device clocks and the certificate expiry."
                category = "Certificate"
            case .serverCertificateUntrusted, .serverCertificateHasUnknownRoot,
                    .secureConnectionFailed, .clientCertificateRejected, .clientCertificateRequired:
                message = "The secure connection failed. Check the Mac address and certificate fingerprint."
                category = "TLS"
            case .userCancelledAuthentication:
                message = "Authentication was cancelled. Reconnect to try again."
                category = "Authentication"
            case .cancelled:
                // URLSession also returns this code when the certificate pin check rejects the peer.
                message = "The connection was cancelled. Check the Mac certificate fingerprint, then reconnect."
                category = "Cancelled"
            default:
                message = "The request to your Mac failed. Reconnect to check the current state."
                category = "Request"
            }
        }
        return message + " (\(category) \(error.code.rawValue))"
    }

    static func diagnostic(for error: URLError) -> String {
        var result = "URL error \(error.code.rawValue)"
        if let reason = error.networkUnavailableReason { result += "; network reason \(reason.rawValue)" }
        if let underlying = error.userInfo[NSUnderlyingErrorKey] as? NSError {
            let domains = [NSURLErrorDomain, NSPOSIXErrorDomain, NSOSStatusErrorDomain, "kCFErrorDomainCFNetwork"]
            let domain = domains.contains(underlying.domain) ? underlying.domain : "other"
            result += "; underlying \(domain) \(underlying.code)"
        }
        return result
    }
}

final class APIClient: @unchecked Sendable {
    private let baseURL: URL
    private let token: String?
    private let session: URLSession
    private let foregroundSession: URLSession

    init(serverURL: String, fingerprint: String, token: String?) throws {
        guard let components = URLComponents(string: serverURL.trimmingCharacters(in: .whitespacesAndNewlines)),
              let host = components.host, !host.isEmpty,
              components.user == nil, components.password == nil,
              components.query == nil, components.fragment == nil,
              components.path.isEmpty || components.path == "/",
              let url = components.url else {
            throw ServiceError.configuration("Enter the Mac address, including https:// and its port.")
        }
        let pin = Self.normalizeFingerprint(fingerprint)
        var allowLoopbackHTTP = false
        #if targetEnvironment(simulator) && DEBUG
        allowLoopbackHTTP = components.scheme == "http" && ["localhost", "127.0.0.1", "::1", "[::1]"].contains(host)
        #endif
        guard components.scheme == "https" || allowLoopbackHTTP else {
            throw ServiceError.configuration("Pairing requires HTTPS. Use the secure address shown on your Mac.")
        }
        if !allowLoopbackHTTP {
            guard pin.count == 64, pin.allSatisfy({ $0.isHexDigit && $0.isASCII }) else {
                throw ServiceError.configuration("Enter the full SHA-256 certificate fingerprint shown on your Mac.")
            }
        }
        baseURL = url
        self.token = token
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 35
        config.timeoutIntervalForResource = 45
        config.urlCache = nil
        config.httpCookieStorage = nil
        config.httpShouldSetCookies = false
        config.waitsForConnectivity = true
        session = URLSession(configuration: config, delegate: PinDelegate(host: host, fingerprint: pin), delegateQueue: nil)
        config.timeoutIntervalForRequest = 4
        config.timeoutIntervalForResource = 6
        config.waitsForConnectivity = false
        foregroundSession = URLSession(configuration: config, delegate: PinDelegate(host: host, fingerprint: pin), delegateQueue: nil)
    }

    deinit {
        session.invalidateAndCancel()
        foregroundSession.invalidateAndCancel()
    }

    static func normalizeFingerprint(_ value: String) -> String {
        value.lowercased().filter { !$0.isWhitespace && $0 != ":" }
    }

    func request<Response: Decodable & Sendable>(_ path: String, method: String = "GET", body: Data? = nil,
                                      idempotencyKey: String? = nil, retryTransient: Bool = true,
                                      fullState: Bool = false) async throws -> Response {
        let (data, _) = try await transfer(path, method: method, body: body, idempotencyKey: idempotencyKey,
                                           retryTransient: retryTransient, fullState: fullState)
        do { return try APIJSON.decoder().decode(Response.self, from: data) }
        catch { throw ServiceError.invalidResponse }
    }

    func artifactContent(id: String) async throws -> Data {
        guard UUID(uuidString: id) != nil else { throw ServiceError.configuration("This artifact identifier is invalid.") }
        return try await transfer("artifacts/\(id)/content").0
    }

    private func transfer(_ path: String, method: String = "GET", body: Data? = nil,
                          idempotencyKey: String? = nil, retryTransient: Bool = true,
                          fullState: Bool = false) async throws -> (Data, HTTPURLResponse) {
        let request = try makeRequest(path, method: method, body: body, idempotencyKey: idempotencyKey,
                                      fullState: fullState)
        guard let url = request.url else { throw ServiceError.configuration("The service route is invalid.") }
        let wait = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?
            .first(where: { $0.name == "wait" })?.value.flatMap(Double.init) ?? 0
        let shortRead = method == "GET" && ((path == "state" && !fullState) || path.hasPrefix("cards/")
            || path.hasPrefix("history/") || path.hasPrefix("search?")
            || (path.hasPrefix("updates?") && wait <= 0))
        let transport = shortRead ? foregroundSession : session
        for attempt in 0...1 {
            do {
                let (data, response) = try await transport.data(for: request)
                guard let response = response as? HTTPURLResponse else { throw ServiceError.invalidResponse }
                guard (200..<300).contains(response.statusCode) else {
                    let error = try? JSONDecoder().decode(ErrorBody.self, from: data)
                    throw ServiceError.response(response.statusCode, error?.error ?? "The Mac could not complete this request (\(response.statusCode)).")
                }
                return (data, response)
            } catch let error as URLError where retryTransient && attempt == 0 && Self.canRetry(error) {
                try await Task.sleep(for: .milliseconds(400))
            }
        }
        throw ServiceError.invalidResponse
    }

    func makeRequest(_ path: String, method: String = "GET", body: Data? = nil,
                     idempotencyKey: String? = nil, fullState: Bool = false) throws -> URLRequest {
        guard let url = URL(string: "/v1/" + path, relativeTo: baseURL)?.absoluteURL,
              url.host == baseURL.host, url.scheme == baseURL.scheme, url.port == baseURL.port else {
            throw ServiceError.configuration("The service route is invalid.")
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        // Task details loads older finished Work on request, so recent content can leave it out.
        if path == "state" && !fullState {
            request.setValue("recent", forHTTPHeaderField: "X-Coconut-State")
            request.setValue("recent", forHTTPHeaderField: "X-Coconut-Work")
        }
        if path.hasPrefix("updates?") {
            request.setValue("4", forHTTPHeaderField: "X-Coconut-Updates")
            request.setValue("recent", forHTTPHeaderField: "X-Coconut-Work")
        }
        request.httpBody = body
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if body != nil { request.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        if let token { request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization") }
        if method != "GET" {
            // The same request and key survive the network retry.
            request.setValue(idempotencyKey ?? UUID().uuidString, forHTTPHeaderField: "Idempotency-Key")
        }
        return request
    }

    private static func canRetry(_ error: URLError) -> Bool {
        [.networkConnectionLost, .timedOut, .cannotConnectToHost, .cannotFindHost, .dnsLookupFailed, .notConnectedToInternet].contains(error.code)
    }

    private struct ErrorBody: Decodable { let error: String }
}

private final class PinDelegate: NSObject, URLSessionDelegate, URLSessionTaskDelegate, @unchecked Sendable {
    let host: String
    let fingerprint: String

    init(host: String, fingerprint: String) {
        self.host = host.lowercased()
        self.fingerprint = fingerprint
    }

    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              challenge.protectionSpace.host.lowercased() == host,
              let trust = challenge.protectionSpace.serverTrust,
              let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate],
              let certificate = chain.first else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        let digest = SHA256.hash(data: SecCertificateCopyData(certificate) as Data)
            .map { String(format: "%02x", $0) }.joined()
        guard digest == fingerprint else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        // Pinning permits a private certificate while retaining host and expiry checks.
        SecTrustSetPolicies(trust, SecPolicyCreateSSL(true, host as CFString))
        SecTrustSetAnchorCertificates(trust, [certificate] as CFArray)
        SecTrustSetAnchorCertificatesOnly(trust, true)
        guard SecTrustEvaluateWithError(trust, nil) else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        completionHandler(.useCredential, URLCredential(trust: trust))
    }

    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

enum PairingToken {
    private static let service = "local.coconut.device-token"
    private static let account = "paired-mac"

    static func read() -> String? {
        var query = baseQuery
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func save(_ token: String) throws {
        let data = Data(token.utf8)
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

    static func delete() { SecItemDelete(baseQuery as CFDictionary) }

    private static func logStatus(_ operation: String, status: OSStatus) {
        #if DEBUG
        NSLog("Coconut Keychain %@ status: %d", operation, status)
        #endif
    }

    private static var baseQuery: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: account]
    }
}
