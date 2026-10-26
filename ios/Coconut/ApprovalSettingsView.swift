import SwiftUI

struct ApprovalSettingsView: View {
    @Environment(AppStore.self) private var store
    var botId: String? = nil
    @State private var saving = false

    private var bot: Bot? { store.bots.first { $0.id == botId } }
    private var effective: String? { bot?.approvalOverride ?? store.approvals?.defaults }

    var body: some View {
        List {
            if let settings = store.approvals {
                if botId != nil {
                    Section {
                        Toggle("Use default", isOn: Binding(get: { bot?.approvalOverride == nil }, set: { value in
                            save(value ? nil : settings.defaults)
                        })).tint(BoardStyle.green)
                    } footer: { Text("Default: \(settings.label(for: settings.defaults))") }
                }
                if botId == nil || bot?.approvalOverride != nil {
                    Section {
                        ForEach(settings.modes) { mode in
                            Button { save(mode.id) } label: {
                                HStack {
                                    VStack(alignment: .leading, spacing: 4) {
                                        Text(mode.label).foregroundStyle(.primary)
                                        Text(mode.description).font(.footnote).foregroundStyle(.secondary)
                                    }
                                    Spacer()
                                    if effective == mode.id { Image(systemName: "checkmark") }
                                }.padding(.vertical, 3)
                            }
                            .accessibilityAddTraits(effective == mode.id ? .isSelected : [])
                        }
                    }
                }
                Section {} footer: {
                    Text("Changes apply to the next action, including queued work. An action already executing continues until it finishes or you stop it.")
                }
            } else {
                Text("Connect to your Mac to read Hermes settings.").foregroundStyle(.secondary)
            }
            if let error = store.error { InlineError(error) }
        }
        .disabled(saving)
        .settingsBackground()
        .navigationTitle("Access").navigationBarTitleDisplayMode(.inline)
    }

    private func save(_ value: String?) {
        saving = true
        Task { await store.setApproval(value, botId: botId); saving = false }
    }
}
