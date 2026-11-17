import SwiftUI

/// The access of every chat: how much Hermes may do without asking. One Hermes profile runs all chats.
struct ApprovalSettingsView: View {
    @Environment(AppStore.self) private var store
    @ShownError private var shownError
    @State private var saving = false

    var body: some View {
        List {
            if let settings = store.approvals {
                Section {
                    ForEach(settings.modes) { mode in
                        Button { save(mode.id) } label: {
                            HStack {
                                VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                                    Text(mode.label).foregroundStyle(.primary)
                                    Text(mode.description).font(.subheadline).foregroundStyle(.secondary)
                                }
                                Spacer()
                                if settings.defaults == mode.id { Image(systemName: "checkmark") }
                            }.padding(.vertical, BoardStyle.Space.xs)
                        }
                        .accessibilityAddTraits(settings.defaults == mode.id ? .isSelected : [])
                    }
                }
                Section {} footer: {
                    Text("Every chat uses this access. Changes apply to the next action, including queued work. An action already executing continues until it finishes or you stop it.")
                }
            } else {
                Text("Connect to your Mac to read Hermes settings.").foregroundStyle(.secondary)
            }
            if let error = shownError { InlineError(error) }
        }
        .disabled(saving)
        .settingsBackground()
        .navigationTitle("Access").inlineNavigationTitle()
    }

    private func save(_ value: String) {
        saving = true
        Task { await store.setApproval(value); saving = false }
    }
}
