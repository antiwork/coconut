import SwiftUI
import QuickLook
import MarkdownUI

struct DecisionCard: View {
    @Environment(AppStore.self) private var store
    let decision: Decision
    var showsBot = false
    var replyFocus: FocusState<Bool>.Binding? = nil
    var confirmingOptionId: String? = nil
    var onRespond: () -> Void = {}
    var onChoose: ((String, String?) -> Void)? = nil
    @State private var dismissing = false
    @State private var dismissRequest: (revision: Int, id: String)?
    @State private var submitting = false
    @State private var submittingOptionId: String?
    @State private var responseError: String?
    @State private var showsEarlierReplies = false
    @State private var editingDraft: Decision?
    private var canRespond: Bool { store.connection == .connected || store.isDemo }
    private var showsSelection: Bool { !decision.isPending || dismissing }
    private var isDismissed: Bool { dismissing || decision.status == "dismissed" }
    private var selectionLabel: String {
        if isDismissed { return "Dismissed" }
        if decision.draft != nil {
            return decision.status == "rejected" ? "Discarded" : decision.status == "failed" ? "Could not finish" : "Approved for sending"
        }
        if let id = submittingOptionId, let option = decision.options.first(where: { $0.id == id }) {
            return option.label
        }
        return decision.selectedOptionLabel ?? decision.status.capitalized
    }
    var body: some View {
        Group {
            if showsBot {
                VStack(spacing: 0) {
                    botHeader.padding(.horizontal, 16)
                    Divider()
                    ScrollViewReader { proxy in
                        ScrollView {
                            cardContent.padding(16)
                            Color.clear.frame(height: 1).id("reply-bottom")
                        }
                        .scrollDismissesKeyboard(.interactively)
                        .accessibilityIdentifier("inbox-card-scroll")
                        .simultaneousGesture(TapGesture().onEnded { replyFocus?.wrappedValue = false })
                        .onChange(of: replies.last?.id) { _, _ in
                            proxy.scrollTo("reply-bottom", anchor: .bottom)
                        }
                    }
                    if let replyFocus {
                        DecisionReplyComposer(decision: decision, focused: replyFocus, onSend: onRespond)
                            .padding(12)
                    }
                }
            } else { cardContent.padding(16) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: 24))
        .clipShape(RoundedRectangle(cornerRadius: 24))
        .sheet(item: $editingDraft) { original in MessageDraftEditor(decision: original) }
    }

    private var cardContent: some View {
        VStack(alignment: .leading, spacing: showsBot ? 10 : 14) {
            HStack(alignment: .top, spacing: 8) {
                VStack(alignment: .leading, spacing: 8) {
                    Text(decision.title).font(.body.weight(.semibold))
                        .fixedSize(horizontal: false, vertical: true)
                    if !showsSelection, let summary = decision.cardContext {
                        Text(summary).font(.subheadline).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                if !showsBot && !showsSelection {
                    Button(action: dismiss) {
                        Image(systemName: "xmark").font(.body).foregroundStyle(.secondary)
                            .frame(width: 44, height: 44).contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .disabled(!canRespond || submitting || store.answeringDecisionIds.contains(decision.id))
                    .accessibilityLabel("Dismiss card")
                    .accessibilityIdentifier("dismiss-card-\(decision.id)")
                }
            }
            if let draft = decision.draft { MessageDraftContent(draft: draft) }
            if !showsSelection {
                if decision.draft != nil { draftActions }
                else if !decision.cardOptions.isEmpty { choices }
                if showsBot { inlineReplies }
                if !canRespond {
                    Text("Reconnect to respond.").font(.caption).foregroundStyle(.secondary)
                }
                if let responseError {
                    Text(responseError).font(.caption).foregroundStyle(BoardStyle.accent)
                        .textSelection(.enabled)
                }
            } else {
                decisionReceipt
            }
        }
    }

    private var botHeader: some View {
        NavigationLink(value: store.decisionChatRoute(for: decision)) {
            HStack(spacing: 8) {
                if let bot = store.bots.first(where: { $0.id == decision.botId }) {
                    BotAvatar(bot: bot, size: 24)
                    Text(bot.name).font(.subheadline.weight(.medium)).lineLimit(1)
                }
                Spacer(minLength: 12)
                Text("Open chat").font(.subheadline)
                Image(systemName: "chevron.right").font(.caption2)
            }
            .frame(maxWidth: .infinity, minHeight: 44).contentShape(Rectangle())
        }.buttonStyle(.plain)
            .accessibilityIdentifier("inbox-context-\(decision.id)")
            .accessibilityLabel("Open this card in chat")
            .simultaneousGesture(TapGesture().onEnded { replyFocus?.wrappedValue = false; onRespond() })
    }

    private var replies: [Message] {
        store.messages.filter {
            $0.decisionId == decision.id && $0.kind == "text" && !$0.text.isEmpty
        }
    }

    private var inlineReplies: some View {
        VStack(alignment: .leading, spacing: 14) {
            if replies.count > 2 {
                Button(showsEarlierReplies ? "Hide earlier replies" : "Earlier replies") {
                    showsEarlierReplies.toggle()
                }.font(.subheadline).foregroundStyle(.secondary).frame(minHeight: 44)
                    .accessibilityIdentifier("decision-earlier-replies")
            }
            ForEach(showsEarlierReplies ? replies : Array(replies.suffix(2))) { message in
                HStack {
                    if message.role == "user" { Spacer(minLength: 24) }
                    Markdown(message.text).markdownTextStyle { FontSize(15) }
                        .textSelection(.enabled)
                        .padding(message.role == "user" ? 12 : 0)
                        .background(message.role == "user" ? BoardStyle.paper : Color.clear,
                                    in: RoundedRectangle(cornerRadius: 16))
                    if message.role != "user" { Spacer(minLength: 0) }
                }
            }
        }
    }

    private var draftActions: some View {
        MessageDraftActions(selected: confirmingOptionId == "send", send: { answer("approve", optionId: "send") },
            edit: { replyFocus?.wrappedValue = false; onRespond(); editingDraft = decision },
            discard: { answer("reject", optionId: "discard") })
        .disabled(!canRespond || submitting || store.answeringDecisionIds.contains(decision.id))
    }

    private var choices: some View {
        VStack(spacing: 0) {
            ForEach(Array(decision.cardOptions.enumerated()), id: \.element.id) { index, option in
                Button { answer(option.answer, optionId: decision.options.isEmpty ? nil : option.id) } label: {
                    DecisionChoiceContent(option: option, index: index, isRecommended: decision.recommendedOptionId == option.id, isSending: submittingOptionId == option.id, isSelected: confirmingOptionId == option.id, isCompact: showsBot)
                }
                .buttonStyle(HomePressStyle())
                .disabled(!canRespond || submitting || store.answeringDecisionIds.contains(decision.id))
                .accessibilityIdentifier("decision-option-\(option.id)")
                .accessibilityLabel(option.label + (decision.recommendedOptionId == option.id ? ", Recommended" : ""))
                .accessibilityHint("Submits this choice. " + option.explanation)
                .accessibilityAddTraits(confirmingOptionId == option.id ? .isSelected : [])
                if option.id != decision.cardOptions.last?.id { Divider().padding(.horizontal, 12) }
            }
        }
        .background(BoardStyle.paper, in: RoundedRectangle(cornerRadius: 12))
        .clipShape(RoundedRectangle(cornerRadius: 12))
    }

    private var decisionReceipt: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                Text(selectionLabel).font(.subheadline)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityIdentifier("decision-selection-\(decision.id)")
                Image(systemName: isDismissed ? "xmark" : decision.status == "failed" ? "exclamationmark.circle" : decision.isPending ? "arrow.up" : "checkmark")
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(isDismissed ? .secondary : decision.status == "failed" ? BoardStyle.accent :
                                        decision.isPending ? .secondary : BoardStyle.green)
                    .accessibilityLabel(isDismissed ? "Dismissed" : decision.isPending ? "Sending choice" :
                                            decision.status == "failed" ? "Action failed" : "Choice recorded")
            }
            .padding(.horizontal, 12).padding(.vertical, 12)
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .background(BoardStyle.paper, in: RoundedRectangle(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(BoardStyle.line))
            if decision.status == "approved" && decision.draft == nil {
                Text("Choice received").font(.caption).foregroundStyle(.secondary)
            }
            if decision.status == "failed", let outcome = decision.outcome, !outcome.isEmpty {
                Text(outcome).font(.caption).foregroundStyle(BoardStyle.accent)
            }
            if let replacement = decision.replacedByDecisionId {
                NavigationLink(value: ConversationRoute(botId: store.conversationBotId(for: decision), decisionId: replacement)) {
                    Label("View revised proposal", systemImage: "arrow.turn.down.right")
                        .font(.caption.weight(.medium)).frame(minHeight: 44)
                }
            }
        }
    }

    private func dismiss() {
        guard decision.isPending, canRespond, !submitting, !dismissing,
              !store.answeringDecisionIds.contains(decision.id) else { return }
        let requestId = dismissRequest?.revision == decision.revision ? dismissRequest!.id : UUID().uuidString
        dismissRequest = (decision.revision, requestId)
        dismissing = true
        responseError = nil
        store.error = nil
        onRespond()
        Task {
            let result = await store.updateInbox(decision, action: "dismiss", idempotencyKey: requestId)
            dismissing = false
            if result.decision != nil {
                dismissRequest = nil
            } else {
                if !result.isUncertain { dismissRequest = nil }
                responseError = "Dismissal not confirmed. Try again."
            }
        }
    }

    private func answer(_ choice: String, optionId: String? = nil) {
        guard decision.isPending, canRespond, !submitting else { return }
        if let onChoose { onChoose(choice, optionId); return }
        onRespond()
        submitting = true
        submittingOptionId = optionId
        responseError = nil
        store.error = nil
        Task {
            await store.answer(decision, choice: choice, optionId: optionId)
            responseError = store.error
            submitting = false
            submittingOptionId = nil
        }
    }


}


struct DecisionChoiceContent: View {
    let option: DecisionOption
    let index: Int
    var isRecommended = false
    var isSending = false
    var isSelected = false
    var isCompact = false

    var body: some View {
        HStack(alignment: .top, spacing: isCompact ? 8 : 12) {
            ZStack {
                Text(String(UnicodeScalar(65 + index)!)).opacity(isSelected ? 0 : 1)
                Image(systemName: "checkmark").fontWeight(.semibold).opacity(isSelected ? 1 : 0)
            }
                .font(.subheadline)
                .foregroundStyle(isSelected ? .primary : .secondary).frame(width: isCompact ? 24 : 28, height: isCompact ? 24 : 28)
                .background(isSelected ? Color.white.opacity(0.14) : BoardStyle.surface, in: RoundedRectangle(cornerRadius: 7))
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: isCompact ? 3 : 5) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    (Text(option.label) + Text(isRecommended ? "  Recommended" : "").font(.caption).foregroundColor(.secondary))
                        .font(.body)
                        .foregroundStyle(option.answer == "reject" ? Color.red : .primary)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                    if isSending {
                        Image(systemName: "checkmark").foregroundStyle(.secondary)
                            .accessibilityLabel("Sending choice")
                    }
                }
                if !option.explanation.isEmpty {
                    Text(option.explanation).font(.subheadline).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(.horizontal, 12).padding(.vertical, isCompact ? 8 : 12)
        .frame(maxWidth: .infinity, minHeight: 44)
        .background(isSelected ? Color.white.opacity(0.12) : .clear)
        .animation(.easeOut(duration: 0.1), value: isSelected)
        .contentShape(Rectangle())
    }
}

struct ArtifactCard: View {
    @Environment(AppStore.self) private var store
    let artifact: Artifact
    @State private var previewURL: URL?
    @State private var downloading = false
    @State private var expanded = false
    private var shortResult: Bool {
        artifact.preview.count <= 160 && artifact.preview.components(separatedBy: "\n").count <= 3
    }

    private var externalURL: URL? {
        guard let raw = artifact.url, let url = URL(string: raw), let scheme = url.scheme,
              ["https", "http"].contains(scheme) else { return nil }
        return url
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: artifact.kind == "pr" ? "arrow.triangle.pull" : artifact.kind == "task" ? "checkmark.circle" : "doc.text")
                    .font(.title3).foregroundStyle(.secondary)
                    .frame(width: 30, height: 36)
                VStack(alignment: .leading, spacing: 6) {
                    Text(artifact.title).font(.subheadline.weight(.semibold))
                    StatusLabel(status: artifact.status)
                }
                Spacer(minLength: 0)
            }
            if !artifact.preview.isEmpty {
                Text(.init(artifact.preview)).font(.subheadline).foregroundStyle(.secondary)
                    .lineLimit(expanded || (artifact.kind == "task" && shortResult) ? nil : 3).textSelection(.enabled)
            }
            if artifact.kind == "task" && !shortResult {
                Button(expanded ? "Show less" : "Show full result") { expanded.toggle() }
                    .font(.subheadline).frame(minHeight: 44)
            }
            if artifact.kind == "pr", let error = artifact.refreshError, !error.isEmpty {
                Label("Status could not refresh", systemImage: "exclamationmark.circle")
                    .font(.caption2).foregroundStyle(.secondary)
            }
            HStack {
                if let externalURL {
                    Link(destination: externalURL) {
                        Label(artifact.kind == "pr" ? "Open pull request" : "Open result", systemImage: "arrow.up.right")
                            .font(.subheadline.weight(.medium)).frame(minHeight: 44)
                    }
                    Spacer()
                    ShareLink(item: externalURL) { Image(systemName: "square.and.arrow.up").frame(width: 44, height: 44) }
                } else if artifact.kind != "task" {
                    Button {
                        downloading = true
                        Task {
                            previewURL = await store.downloadArtifact(artifact)
                            downloading = false
                        }
                    } label: {
                        Label(downloading ? "Opening…" : "Preview file", systemImage: "doc.text.magnifyingglass")
                            .font(.subheadline.weight(.medium)).frame(minHeight: 44)
                    }
                    .disabled(downloading)
                }
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: 20))
        .quickLookPreview($previewURL)
    }
}
