import SwiftUI

struct MCPSettingsView: View {
    @Environment(AppStore.self) private var store
    @State private var adding = false
    @State private var github: GitHubConnectionStatus?

    private var canEdit: Bool { store.isDemo || store.connection == .connected }

    var body: some View {
        // A team member never connects GitHub, Gmail or another service of the team in the app. The Mac uses its own
        // gh login, and Gumclaw holds the credentials of the team (github_access.py, support_threads.py). So GitHub has
        // only a read-only line that says which login the Mac reads with, and Gmail has none.
        Form {
            Section {
                ForEach(store.mcpServers) { server in
                    NavigationLink { MCPConnectionView(server: server) } label: {
                        VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                            Text(server.id)
                            Text(server.url.isEmpty ? "Configured in Hermes" : server.url)
                                .font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
                        }.padding(.vertical, BoardStyle.Space.xs)
                    }
                }
                Button { adding = true } label: {
                    Label("Add connection", systemImage: "plus")
                }
                .disabled(!canEdit)
                .accessibilityIdentifier("add-mcp")
            } header: {
                Text("Tools (MCP)")
            } footer: {
                Text(canEdit ? "All agents can use these tools. Hermes keeps their settings on your Mac."
                     : "Connect to your Mac first. All agents can use these tools. Hermes keeps their settings on your Mac.")
            }
            if let github {
                Section {
                    LabeledContent("GitHub", value: github.connected ? github.login ?? "Connected" : "No login")
                } footer: {
                    Text(github.connected ? "The GitHub login of this Mac. Comments and moves on GitHub use it."
                         : "This Mac has no GitHub login. The to-do lists come from Gumclaw and are read-only, and GitHub notifications are off.")
                }
            }
        }
        .navigationTitle("Connections").inlineNavigationTitle()
        .settingsBackground()
        .task { github = try? await store.githubStatus() }
        .sheet(isPresented: $adding) { NavigationStack { MCPConnectionView() } }
    }
}

struct MCPConnectionView: View {
    @Environment(AppStore.self) private var store
    @ShownError private var shownError
    @Environment(\.dismiss) private var dismiss
    var server: MCPServer? = nil
    @State private var name = ""
    @State private var address = ""
    @State private var loadedAddress = ""
    @State private var token = ""
    @State private var removeAuthentication = false
    @State private var busy = false
    @State private var test: MCPResponse?

    var body: some View {
        Form {
            Section {
                TextField("Name", text: $name)
                    .noAutocapitalization().autocorrectionDisabled()
                    .disabled(server != nil).accessibilityIdentifier("mcp-name")
                TextField("https://example.com/mcp", text: $address)
                    .urlKeyboard().noAutocapitalization().autocorrectionDisabled()
                    .disabled(server?.editable == false).accessibilityIdentifier("mcp-url")
                if server?.editable != false {
                    SecureField(server?.hasToken == true ? "Access token saved · enter to replace" : "Access token (optional)", text: $token)
                        .noAutocapitalization().autocorrectionDisabled().privacySensitive()
                        .accessibilityIdentifier("mcp-token")
                        .disabled(removeAuthentication)
                    if server?.hasToken == true {
                        Toggle("No authentication", isOn: $removeAuthentication)
                    }
                }
            } footer: {
                Text(server?.editable == false ? "Manage this connection through Hermes on your Mac." : "Use an HTTP MCP server. The access token stays on your Mac. Coconut never shows it again.")
            }
            if server?.editable != false {
                Section {
                    Button("Save connection") { save() }
                        .disabled(busy || name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || address.isEmpty)
                        .accessibilityIdentifier("save-mcp")
                }
            }
            if let server {
                Section {
                    Button("Test connection") {
                        busy = true
                        Task {
                            test = await store.updateMCP(MCPRequest(action: "test", name: server.id))
                            busy = false
                        }
                    }.disabled(busy).accessibilityIdentifier("test-mcp")
                    if let test {
                        if test.ok {
                            Label("Connected · \(test.tools.count) tools", systemImage: "checkmark")
                                .foregroundStyle(BoardStyle.green)
                            ForEach(test.tools, id: \.self) { Text($0).font(.footnote) }
                        } else { InlineError(test.error ?? "Connection failed.") }
                    }
                }
                if server.editable {
                    Section {
                        Button("Remove connection", role: .destructive) {
                            busy = true
                            Task {
                                let result = await store.updateMCP(MCPRequest(action: "remove", name: server.id))
                                busy = false
                                if result?.ok == true { dismiss() }
                            }
                        }.disabled(busy)
                    }
                }
            }
            if let error = shownError { Section { InlineError(error) } }
        }
        .navigationTitle(server?.id ?? "Add connection").inlineNavigationTitle()
        .settingsBackground()
        .toolbar {
            if server == nil {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
            }
        }
        .onAppear { if let server { name = server.id; address = server.url; loadedAddress = server.url } }
        .onChange(of: store.mcpServers.first { $0.id == server?.id }) { _, current in
            guard let current else { return }
            if address == loadedAddress { address = current.url }
            loadedAddress = current.url
        }
        .onDisappear { token = "" }
    }

    private func save() {
        busy = true
        let request = MCPRequest(action: "save", name: name.trimmingCharacters(in: .whitespacesAndNewlines),
                                 url: address.trimmingCharacters(in: .whitespacesAndNewlines),
                                 token: removeAuthentication ? "" : (token.isEmpty && server != nil ? nil : token.trimmingCharacters(in: .whitespacesAndNewlines)))
        Task {
            let result = await store.updateMCP(request)
            busy = false
            if result?.ok == true { token = ""; dismiss() }
        }
    }
}
