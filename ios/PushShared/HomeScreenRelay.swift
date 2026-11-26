import Foundation

// The relay of the widgets and the Live Activity: after the person leaves the app, the Mac sends the glance of the Home
// Screen to the relay at each change, sealed for this iPhone (backend/coconut/home_screen.py). The widgets read it there,
// and the relay gives it to the Live Activity as its content state (relay/src/device.js). The app saves the route of its
// notifications here, because the widget extension cannot ask the app for it.

/// The route of this iPhone on the relay and its manage token: what the widgets need to read the glance and the app
/// needs to name the push tokens of its Live Activity and its widgets. It lives in the Keychain group of the extensions
/// (NotificationKeys), and goes when notifications go (PushRelayClient.disable).
struct HomeScreenRoute: Codable, Sendable, Equatable {
    let relay: String
    let routeId: String
    let manageToken: String

    static let account = "home-screen-route"

    static func load() -> HomeScreenRoute? { try? NotificationKeys.read(HomeScreenRoute.self, account: account) }

    static func save(_ route: HomeScreenRoute?) {
        guard let route else { NotificationKeys.delete(account: account); return }
        if load() != route { try? NotificationKeys.save(route, account: account) }
    }

    fileprivate func url(_ path: String) -> URL? {
        guard let base = URL(string: relay), base.scheme == "https", base.host != nil, base.user == nil, base.password == nil,
              base.query == nil, base.fragment == nil, routeId.count == 64, routeId.allSatisfy(\.isHexDigit) else { return nil }
        return base.appending(path: "v1/routes/\(routeId)/\(path)")
    }
}

enum HomeScreenRelay {
    private struct Stored: Decodable { let envelope: PushEnvelope }
    private struct Failure: Error {}

    /// The newest glance that the Mac sealed for this iPhone, opened with its key: the JSON of home_screen.py, or nil
    /// when the relay has none, the network fails, or the glance is not for this iPhone.
    static func sealedGlance() async -> Data? {
        guard let route = HomeScreenRoute.load(), let url = route.url("glance") else { return nil }
        var request = URLRequest(url: url, timeoutInterval: 8)
        request.setValue("Bearer \(route.manageToken)", forHTTPHeaderField: "Authorization")
        guard let (data, response) = try? await session.data(for: request),
              (response as? HTTPURLResponse)?.statusCode == 200, data.count < 24000,
              let stored = try? JSONDecoder().decode(Stored.self, from: data) else { return nil }
        return try? NotificationKeys.open(stored.envelope, kind: "glance")
    }

    /// Name the push token of the Live Activity to the relay, or remove it with nil. True when the relay took it.
    @discardableResult
    static func setActivityToken(_ token: Data?) async -> Bool { await put(["activity": token.map(hex)]) }

    /// Name the push token of the widgets (WidgetKit, iOS 26) to the relay, or remove it with nil.
    @discardableResult
    static func setWidgetToken(_ token: Data?) async -> Bool { await put(["widgets": token.map(hex)]) }

    private static func put(_ tokens: [String: String?]) async -> Bool {
        guard let route = HomeScreenRoute.load(), let url = route.url("live"),
              let body = try? JSONSerialization.data(withJSONObject: tokens.mapValues { $0 ?? NSNull() as Any }) else {
            return false
        }
        var request = URLRequest(url: url, timeoutInterval: 15)
        request.httpMethod = "PUT"
        request.httpBody = body
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(route.manageToken)", forHTTPHeaderField: "Authorization")
        guard let (_, response) = try? await session.data(for: request) else { return false }
        return (response as? HTTPURLResponse)?.statusCode == 200
    }

    static func hex(_ token: Data) -> String { token.map { String(format: "%02x", $0) }.joined() }

    /// No cookies, no cache, and no redirect: a redirect would carry the manage token to another host.
    private static let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.httpCookieStorage = nil
        config.urlCache = nil
        config.timeoutIntervalForResource = 20
        return URLSession(configuration: config, delegate: NoRedirects(), delegateQueue: nil)
    }()
}

private final class NoRedirects: NSObject, URLSessionTaskDelegate, Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping @Sendable (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}
