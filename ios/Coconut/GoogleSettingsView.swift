import SwiftUI
import AuthenticationServices

struct GoogleConnectionStatus: Decodable, Sendable {
    let connected: Bool
    let email: String?
    let needsReconnect: Bool
    /// "support" for the support mailbox. A Mac without the support mailbox leaves it out.
    var mailbox: String? = nil
}

struct GoogleConnectionRequest: Encodable {
    var state: String? = nil
    var code: String? = nil
    /// nil for the Google connection of the agents, GoogleMailbox.support for the support mailbox.
    var mailbox: String? = nil
}

/// The two Google connections of a Mac. The agents read Gmail and Calendar through the first one. The support mailbox is
/// Coconut's own: the Support page answers and closes conversations with it, and no agent gets it
/// (backend/coconut/google_connection.py).
enum GoogleMailbox {
    static let support = "support"
}

extension AppStore {
    /// Signs in to Google in the sign-in sheet of the system and saves the connection on the Mac. A sign-in that the
    /// person cancels throws CancellationError.
    func connectGoogle(_ signIn: GoogleSignIn, mailbox: String? = nil) async throws -> GoogleConnectionStatus {
        do {
            let authorization = try await googleRequest("start", request: GoogleConnectionRequest(mailbox: mailbox), as: GoogleAuthorization.self)
            try Task.checkCancellation()
            let code = try await signIn.open(authorization)
            try Task.checkCancellation()
            return try await googleRequest("finish", request: GoogleConnectionRequest(state: authorization.state, code: code, mailbox: mailbox),
                                           as: GoogleConnectionStatus.self)
        } catch let authError as ASWebAuthenticationSessionError where authError.code == .canceledLogin {
            throw CancellationError()
        }
    }
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

// The Google account shows inline on the Connections page, and the support mailbox under it.
struct GoogleConnectionSection: View {
    @Environment(AppStore.self) private var store
    /// nil for the connection of the agents, GoogleMailbox.support for the support mailbox.
    var mailbox: String? = nil
    @State private var status: GoogleConnectionStatus?
    @State private var busy = false
    @State private var loading = false
    @State private var error: String?
    @State private var signIn = GoogleSignIn()
    @State private var confirmingDisconnect = false
    @State private var connectionTask: Task<Void, Never>?

    private var canConnect: Bool { store.isDemo || store.connection == .connected }
    private var support: Bool { mailbox == GoogleMailbox.support }
    /// A Mac before the support mailbox answers with its one connection, so the section of the mailbox goes away once
    /// the answer shows that. It shows while it loads, because its task loads the answer.
    private var shown: Bool { !support || status == nil || status?.mailbox == GoogleMailbox.support || store.isDemo }

    var body: some View {
        if shown { section }
    }

    private var section: some View {
        Section {
            if let status, status.connected {
                LabeledContent("Account", value: status.email ?? "Google")
                    .accessibilityIdentifier("google-account")
                if status.needsReconnect {
                    Button(busy ? "Connecting…" : "Reconnect Google") { connect() }
                        .disabled(busy || !canConnect).accessibilityIdentifier("google-reconnect")
                }
                Button(support ? "Disconnect support mailbox" : "Disconnect Google", role: .destructive) { confirmingDisconnect = true }
                    .disabled(busy || !canConnect).accessibilityIdentifier(support ? "support-mailbox-disconnect" : "google-disconnect")
                    .confirmationDialog(support ? "Disconnect the support mailbox?" : "Disconnect Google from all agents?",
                                        isPresented: $confirmingDisconnect, titleVisibility: .visible) {
                        Button("Disconnect", role: .destructive) { disconnect() }
                    }
            } else {
                Button(busy ? "Connecting…" : support ? "Connect support mailbox" : "Connect Google") { connect() }
                    .disabled(busy || loading || !canConnect)
                    .accessibilityIdentifier(support ? "support-mailbox-connect" : "google-connect")
            }
            if let error { InlineError(error) }
        } header: {
            Text(support ? "Support mailbox" : "Google")
        } footer: {
            Text(support ? "Coconut answers and closes support conversations from this account. Agents cannot use it."
                 : canConnect ? "All agents can read Gmail and Calendar. This connection cannot send email or change events."
                 : "Connect to your Mac first. All agents can read Gmail and Calendar. This connection cannot send email or change events.")
        }
        .task(id: canConnect) { if canConnect { await refresh() } }
        .onDisappear { connectionTask?.cancel(); signIn.cancel() }
        .onOpenURL { signIn.receive($0) }
    }

    private func refresh() async {
        status = nil; error = nil; loading = true
        defer { loading = false }
        do {
            let result = try await store.googleStatus(mailbox: mailbox)
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
                status = try await store.connectGoogle(signIn, mailbox: mailbox)
            } catch is CancellationError { }
            catch { self.error = error.localizedDescription }
        }
    }

    private func disconnect() {
        busy = true; error = nil
        Task {
            defer { busy = false }
            do {
                let result = try await store.googleRequest("disconnect", request: GoogleConnectionRequest(mailbox: mailbox), as: GoogleConnectionStatus.self)
                status = result
            } catch { self.error = error.localizedDescription }
        }
    }
}
