import SwiftUI
import AuthenticationServices

struct GoogleConnectionStatus: Decodable, Sendable {
    let connected: Bool
    let email: String?
    let needsReconnect: Bool
}

struct GoogleConnectionRequest: Encodable {
    var state: String? = nil
    var code: String? = nil
}

struct GoogleAuthorization: Decodable, Sendable {
    let authorizationUrl: String
    let callbackScheme: String
    let state: String

    func callbackCode(_ url: URL) throws -> String {
        guard url.scheme == callbackScheme, url.path == "/oauthredirect",
              let components = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            throw ServiceError.configuration("Google returned an invalid sign-in response.")
        }
        let items = components.queryItems ?? []
        guard items.filter({ $0.name == "state" }).count == 1,
              items.first(where: { $0.name == "state" })?.value == state else {
            throw ServiceError.configuration("This Google sign-in no longer matches. Try again.")
        }
        if items.contains(where: { $0.name == "error" }) {
            throw ServiceError.configuration("Google access was not granted.")
        }
        guard items.filter({ $0.name == "code" }).count == 1,
              let code = items.first(where: { $0.name == "code" })?.value, !code.isEmpty else {
            throw ServiceError.configuration("Google did not complete sign-in. Try again.")
        }
        return code
    }
}

@MainActor
final class GoogleSignIn: NSObject, ASWebAuthenticationPresentationContextProviding {
    private var session: ASWebAuthenticationSession?
    private var authorization: GoogleAuthorization?
    private var continuation: CheckedContinuation<String, Error>?

    func open(_ authorization: GoogleAuthorization) async throws -> String {
        guard let url = URL(string: authorization.authorizationUrl), url.scheme == "https",
              url.host == "accounts.google.com", authorization.callbackScheme.hasPrefix("com.googleusercontent.apps.") else {
            throw ServiceError.configuration("The Mac returned an invalid Google sign-in link.")
        }
        cancel()
        return try await withCheckedThrowingContinuation { continuation in
            self.authorization = authorization
            self.continuation = continuation
            let state = authorization.state
            let session = ASWebAuthenticationSession(url: url, callbackURLScheme: authorization.callbackScheme) { url, error in
                Task { @MainActor in
                    guard self.authorization?.state == state else { return }
                    if let error { self.complete(.failure(error)); return }
                    guard let url else { self.complete(.failure(ServiceError.invalidResponse)); return }
                    self.receive(url)
                }
            }
            session.presentationContextProvider = self
            self.session = session
            if !session.start() {
                complete(.failure(ServiceError.configuration("Google sign-in could not open. Try again.")))
            }
        }
    }

    func receive(_ url: URL) {
        guard let authorization, url.scheme == authorization.callbackScheme else { return }
        do { complete(.success(try authorization.callbackCode(url))) }
        catch { complete(.failure(error)) }
    }

    private func complete(_ result: Result<String, Error>) {
        guard let continuation else { return }
        self.continuation = nil
        authorization = nil
        let previous = session
        session = nil
        previous?.cancel()
        continuation.resume(with: result)
    }

    func cancel() { complete(.failure(CancellationError())) }

    func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
        #if os(iOS)
        UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
            .filter { $0.activationState == .foregroundActive }
            .flatMap(\.windows).first(where: \.isKeyWindow) ?? ASPresentationAnchor()
        #else
        NSApplication.shared.keyWindow ?? ASPresentationAnchor()
        #endif
    }
}

// The Google account shows inline on the Connections page.
struct GoogleConnectionSection: View {
    @Environment(AppStore.self) private var store
    @State private var status: GoogleConnectionStatus?
    @State private var busy = false
    @State private var loading = false
    @State private var error: String?
    @State private var signIn = GoogleSignIn()
    @State private var confirmingDisconnect = false
    @State private var connectionTask: Task<Void, Never>?

    private var canConnect: Bool { store.isDemo || store.connection == .connected }

    var body: some View {
        Section {
            if let status, status.connected {
                LabeledContent("Account", value: status.email ?? "Google")
                    .accessibilityIdentifier("google-account")
                if status.needsReconnect {
                    Button(busy ? "Connecting…" : "Reconnect Google") { connect() }
                        .disabled(busy || !canConnect).accessibilityIdentifier("google-reconnect")
                }
                Button("Disconnect Google", role: .destructive) { confirmingDisconnect = true }
                    .disabled(busy || !canConnect).accessibilityIdentifier("google-disconnect")
                    .confirmationDialog("Disconnect Google from all bots?", isPresented: $confirmingDisconnect, titleVisibility: .visible) {
                        Button("Disconnect", role: .destructive) { disconnect() }
                    }
            } else {
                Button(busy ? "Connecting…" : "Connect Google") { connect() }
                    .disabled(busy || loading || !canConnect)
                    .accessibilityIdentifier("google-connect")
            }
            if let error { InlineError(error) }
        } header: {
            Text("Google")
        } footer: {
            Text(canConnect ? "All bots can read Gmail and Calendar. This connection cannot send email or change events."
                 : "Connect to your Mac first. All bots can read Gmail and Calendar. This connection cannot send email or change events.")
        }
        .task(id: canConnect) { if canConnect { await refresh() } }
        .onDisappear { connectionTask?.cancel(); signIn.cancel() }
        .onOpenURL { signIn.receive($0) }
    }

    private func refresh() async {
        status = nil; error = nil; loading = true
        defer { loading = false }
        do {
            let result = try await store.googleStatus()
            guard !Task.isCancelled else { return }
            status = result
        } catch is CancellationError { }
        catch { self.error = error.localizedDescription }
    }

    private func connect() {
        busy = true; error = nil
        connectionTask = Task {
            defer { busy = false }
            do {
                let authorization = try await store.googleRequest("start", request: GoogleConnectionRequest(), as: GoogleAuthorization.self)
                try Task.checkCancellation()
                let code = try await signIn.open(authorization)
                try Task.checkCancellation()
                let result = try await store.googleRequest("finish", request: GoogleConnectionRequest(state: authorization.state, code: code), as: GoogleConnectionStatus.self)
                status = result
            } catch let authError as ASWebAuthenticationSessionError where authError.code == .canceledLogin { }
            catch is CancellationError { }
            catch { self.error = error.localizedDescription }
        }
    }

    private func disconnect() {
        busy = true; error = nil
        Task {
            defer { busy = false }
            do {
                let result = try await store.googleRequest("disconnect", request: GoogleConnectionRequest(), as: GoogleConnectionStatus.self)
                status = result
            } catch { self.error = error.localizedDescription }
        }
    }
}
