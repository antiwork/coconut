import SwiftUI

struct DecisionReplyComposer: View {
    @Environment(AppStore.self) private var store
    let decision: Decision
    @FocusState.Binding var focused: Bool
    var onSend: () -> Void = {}
    @State private var sendingReply = false
    @State private var responseError: String?
    private var canRespond: Bool { store.canSubmitDecision }

    var body: some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
            HStack(alignment: .bottom, spacing: 8) {
                TextField("Reply…", text: Binding(
                    get: { store.decisionReplyDrafts[decision.id] ?? "" },
                    set: { store.decisionReplyDrafts[decision.id] = $0 }
                ), axis: .vertical)
                .lineLimit(1...5).font(.subheadline).focused($focused)
                .padding(.leading, 12).padding(.vertical, 12)
                .accessibilityIdentifier("decision-reply-\(decision.id)")
                Button(action: sendReply) {
                    Image(systemName: "arrow.up.circle.fill").font(.title)
                        .frame(minWidth: 44, minHeight: 44)
                }
                .buttonStyle(.plain).accessibilityLabel("Send reply")
                .accessibilityIdentifier("send-decision-reply-\(decision.id)")
                .disabled(!canRespond || sendingReply ||
                          (store.decisionReplyDrafts[decision.id] ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
            .background(BoardStyle.paper, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.small))
            .overlay(RoundedRectangle(cornerRadius: BoardStyle.Radius.small).stroke(BoardStyle.line))
            if sendingReply {
                Text("Sending…").font(.footnote).foregroundStyle(.secondary)
            }
            if let responseError {
                Text(responseError).font(.footnote).foregroundStyle(.secondary)
            }
        }
    }

    private func sendReply() {
        let text = (store.decisionReplyDrafts[decision.id] ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !sendingReply else { return }
        onSend()
        sendingReply = true
        focused = false
        Task {
            let sent = await store.replyToDecision(decision, text: text)
            if sent && store.decisionReplyDrafts[decision.id]?.trimmingCharacters(in: .whitespacesAndNewlines) == text {
                store.decisionReplyDrafts[decision.id] = ""
            }
            responseError = sent ? nil : store.error
            sendingReply = false
        }
    }

}
