import SwiftUI

struct ApprovalSettingsView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.windowShowsError) private var windowShowsError
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
                                    VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                                        Text(mode.label).foregroundStyle(.primary)
                                        Text(mode.description).font(.subheadline).foregroundStyle(.secondary)
                                    }
                                    Spacer()
                                    if effective == mode.id { Image(systemName: "checkmark") }
                                }.padding(.vertical, BoardStyle.Space.xs)
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
            if !windowShowsError, let error = store.error { InlineError(error) }
        }
        .disabled(saving)
        .settingsBackground()
        .navigationTitle("Access").inlineNavigationTitle()
    }

    private func save(_ value: String?) {
        saving = true
        Task { await store.setApproval(value, botId: botId); saving = false }
    }
}
