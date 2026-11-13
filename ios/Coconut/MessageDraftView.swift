import SwiftUI

struct MessageDraftContent: View {
    let draft: MessageDraft
    @ScaledMetric(relativeTo: .subheadline) private var labelWidth = 58

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 8) {
                field("Via", draft.channel)
                if let sender = draft.sender, !sender.isEmpty { field("From", sender) }
                field("To", draft.recipient)
                if let replyTo = draft.replyTo, !replyTo.isEmpty { field("Reply to", replyTo) }
                if let subject = draft.subject, !subject.isEmpty { field("Subject", subject) }
            }
            .font(.subheadline)
            Divider()
            Text(draft.body).font(.body)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
                .accessibilityIdentifier("draft-body")
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(BoardStyle.paper, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.small))
    }

    private func field(_ label: String, _ value: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: BoardStyle.Space.m) {
            Text(label).foregroundStyle(.secondary).frame(width: labelWidth, alignment: .leading)
            Text(value).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
        }
        .accessibilityElement(children: .combine)
    }
}

struct MessageDraftEditor: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let decision: Decision
    @State private var draft: MessageDraft
    @State private var saving = false
    @State private var saveError: String?

    init(decision: Decision) {
        self.decision = decision
        _draft = State(initialValue: decision.draft!)
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    LabeledContent("Via", value: draft.channel)
                    if draft.sender != nil {
                        TextField("From", text: Binding(get: { draft.sender ?? "" }, set: { draft.sender = $0 }))
                            .noAutocapitalization().autocorrectionDisabled()
                    }
                    TextField("To", text: $draft.recipient)
                        .noAutocapitalization().autocorrectionDisabled()
                        .accessibilityIdentifier("draft-recipient-field")
                    if draft.replyTo != nil {
                        TextField("Reply to", text: Binding(get: { draft.replyTo ?? "" }, set: { draft.replyTo = $0 }))
                            .noAutocapitalization().autocorrectionDisabled()
                    }
                    TextField("Subject", text: Binding(get: { draft.subject ?? "" }, set: { draft.subject = $0 }))
                        .accessibilityIdentifier("draft-subject-field")
                }
                Section {
                    TextEditor(text: $draft.body).frame(minHeight: 220)
                        .accessibilityLabel("Message").accessibilityIdentifier("draft-body-field")
                } footer: {
                    Text("Changes stay in the draft until you select Send.")
                }
                if let saveError { Section { Text(saveError).foregroundStyle(BoardStyle.attention) } }
            }
            .scrollContentBackground(.hidden).background(BoardStyle.paper)
            .navigationTitle("Edit draft").inlineNavigationTitle()
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }.disabled(saving)
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        saving = true
                        saveError = nil
                        Task {
                            if await store.editDraft(decision, draft: draft) { dismiss() }
                            else { saveError = store.error ?? "Could not save the draft. Try again." }
                            saving = false
                        }
                    }
                    .disabled(saving || draft.recipient.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                              || draft.body.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .accessibilityIdentifier("draft-save")
                }
            }
            .interactiveDismissDisabled(saving)
        }
        .presentationDragIndicator(.visible)
    }
}

struct MessageDraftActions: View {
    let selected: Bool
    let send: () -> Void
    let edit: () -> Void
    let discard: () -> Void

    var body: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 8) { buttons }
            VStack(spacing: 8) { buttons }
        }
    }

    @ViewBuilder private var buttons: some View {
        Button(action: send) {
            HStack(spacing: BoardStyle.Space.s) {
                if selected { Image(systemName: "checkmark") }
                Text("Send").fontWeight(.semibold)
            }
            .padding(.horizontal, BoardStyle.Space.l).frame(minHeight: 44)
            .foregroundStyle(BoardStyle.controlLabel)
            .background(BoardStyle.control, in: Capsule())
        }
        .buttonStyle(HomePressStyle())
        .accessibilityIdentifier("draft-send")
        .accessibilityHint("Send this exact message to the shown destination.")
        Button(action: edit) {
            Text("Edit").padding(.horizontal, 12).frame(minHeight: 44).contentShape(Rectangle())
        }.buttonStyle(.plain).accessibilityIdentifier("draft-edit")
        Button(action: discard) {
            Text("Discard").padding(.horizontal, 12).frame(minHeight: 44).contentShape(Rectangle())
        }.buttonStyle(.plain).foregroundStyle(.secondary).accessibilityIdentifier("draft-discard")
    }
}
