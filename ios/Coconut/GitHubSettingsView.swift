import SwiftUI

struct GitHubDeviceCode: Decodable, Equatable, Sendable {
    let userCode: String
    let verificationUri: String
}

struct GitHubConnectionStatus: Decodable, Sendable {
    let connected: Bool
    let login: String?
    let needsReconnect: Bool
    let pending: GitHubDeviceCode?
    var error: String? = nil
}

struct GitHubSettingsView: View {
    @Environment(AppStore.self) private var store
    @State private var status: GitHubConnectionStatus?
    @State private var busy = false
    @State private var loading = false
    @State private var error: String?
    @State private var copied = false
    @State private var confirmingDisconnect = false

    var body: some View {
        Form {
            if let pending = status?.pending, status?.connected != true || status?.needsReconnect == true {
                Section {
                    Text(pending.userCode)
                        .font(.system(.title, design: .monospaced).weight(.semibold))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity)
                        .listRowSeparator(.hidden)
                        .accessibilityIdentifier("github-code")
                    Button(copied ? "Code copied. Open GitHub" : "Copy code and open GitHub") { open(pending) }
                        .accessibilityIdentifier("github-open")
                } footer: {
                    Text("Paste the code on GitHub and approve Coconut. This page updates when GitHub confirms.")
                }
            }
            Section {
                if let status, status.connected {
                    LabeledContent("Account", value: "@" + (status.login ?? "github"))
                        .accessibilityIdentifier("github-account")
                    if status.needsReconnect && status.pending == nil {
                        Button(busy ? "Connecting…" : "Reconnect GitHub") { connect() }
                            .disabled(busy).accessibilityIdentifier("github-reconnect")
                    }
                    Button("Disconnect GitHub", role: .destructive) { confirmingDisconnect = true }
                        .disabled(busy).accessibilityIdentifier("github-disconnect")
                } else if status?.pending == nil {
                    Button(busy ? "Connecting…" : "Connect GitHub") { connect() }
                        .disabled(busy || loading || (!store.isDemo && store.connection != .connected))
                        .accessibilityIdentifier("github-connect")
                }
            } footer: {
                Text("All agents use this GitHub account. Other GitHub logins on your Mac stay unchanged.")
            }
            if let message = error ?? (status?.pending == nil ? status?.error : nil) {
                Section { Text(message).font(.footnote) }
            }
        }
        .navigationTitle("GitHub").inlineNavigationTitle()
        .settingsBackground()
        .task { await refresh() }
        .task(id: status?.pending) { await followSignIn() }
        .confirmationDialog("Disconnect GitHub from all agents?", isPresented: $confirmingDisconnect, titleVisibility: .visible) {
            Button("Disconnect", role: .destructive) { disconnect() }
        }
    }

    private func refresh() async {
        error = nil; loading = true
        defer { loading = false }
        do {
            let result = try await store.githubStatus()
            guard !Task.isCancelled else { return }
            if let previous = status, previous.login != result.login || previous.connected != result.connected {
                store.resetBusiness()
            }
            status = result
        } catch is CancellationError { }
        catch { self.error = error.localizedDescription }
    }

    /// The Mac finishes the sign-in when GitHub approves the code. Check while the code is on screen.
    private func followSignIn() async {
        guard status?.pending != nil else { return }
        while !Task.isCancelled, status?.pending != nil {
            try? await Task.sleep(for: .seconds(3))
            guard !Task.isCancelled else { return }
            await refresh()
        }
    }

    private func open(_ pending: GitHubDeviceCode) {
        guard let url = URL(string: pending.verificationUri), url.scheme == "https", url.host == "github.com" else {
            error = "The Mac returned an invalid GitHub link."
            return
        }
        Pasteboard.copy(pending.userCode)
        copied = true
        // The person signs in to GitHub in their own browser, where they are signed in already.
        SystemBrowser.open(url)
    }

    private func connect() {
        busy = true; error = nil; copied = false
        Task {
            defer { busy = false }
            do { status = try await store.githubRequest("start") }
            catch is CancellationError { }
            catch { self.error = error.localizedDescription }
        }
    }

    private func disconnect() {
        busy = true; error = nil
        Task {
            defer { busy = false }
            do {
                status = try await store.githubRequest("disconnect")
                store.resetBusiness()
            }
            catch { self.error = error.localizedDescription }
        }
    }
}
