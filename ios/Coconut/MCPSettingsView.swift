import SwiftUI

struct MCPSettingsView: View {
    @Environment(AppStore.self) private var store
    @State private var adding = false

    var body: some View {
        List {
            Section {
                NavigationLink { GoogleSettingsView() } label: {
                    Text("Google")
                }.accessibilityIdentifier("google-settings")
            }
            Section {
                ForEach(store.mcpServers) { server in
                    NavigationLink { MCPConnectionView(server: server) } label: {
                        VStack(alignment: .leading, spacing: 4) {
                            Text(server.id)
                            Text(server.url.isEmpty ? "Configured in Hermes" : server.url)
                                .font(.footnote).foregroundStyle(.secondary).lineLimit(1)
                        }.padding(.vertical, 3)
                    }
                }
                Button { adding = true } label: {
                    Label("Add connection", systemImage: "plus")
                }.accessibilityIdentifier("add-mcp")
            } header: {
                Text("MCP servers")
            } footer: {
                Text("MCP connections are available to all bots. Hermes stores their settings on your Mac.")
            }
        }
        .navigationTitle("Connections").navigationBarTitleDisplayMode(.inline)
        .scrollContentBackground(.hidden).background(BoardStyle.paper)
        .sheet(isPresented: $adding) { NavigationStack { MCPConnectionView() } }
    }
}

struct MCPConnectionView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    var server: MCPServer? = nil
    @State private var name = ""
    @State private var address = ""
    @State private var token = ""
    @State private var removeAuthentication = false
    @State private var busy = false
    @State private var test: MCPResponse?

    var body: some View {
        Form {
            Section {
                TextField("Name", text: $name)
                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                    .disabled(server != nil).accessibilityIdentifier("mcp-name")
                TextField("https://example.com/mcp", text: $address)
                    .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                    .disabled(server?.editable == false).accessibilityIdentifier("mcp-url")
                if server?.editable != false {
                    SecureField(server?.hasToken == true ? "Token saved · enter to replace" : "Bearer token (optional)", text: $token)
                        .textInputAutocapitalization(.never).autocorrectionDisabled().privacySensitive()
                        .accessibilityIdentifier("mcp-token")
                        .disabled(removeAuthentication)
                    if server?.hasToken == true {
                        Toggle("No authentication", isOn: $removeAuthentication)
                    }
                }
            } footer: {
                Text(server?.editable == false ? "Manage this connection through Hermes on your Mac." : "Use an HTTP MCP server. The token stays on your Mac and is never returned to this app.")
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
                        } else { Text(test.error ?? "Connection failed.").font(.footnote) }
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
            if let error = store.error { Section { Text(error).font(.footnote) } }
        }
        .navigationTitle(server?.id ?? "Add connection").navigationBarTitleDisplayMode(.inline)
        .scrollContentBackground(.hidden).background(BoardStyle.paper)
        .toolbar {
            if server == nil {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
            }
        }
        .onAppear { if let server { name = server.id; address = server.url } }
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
