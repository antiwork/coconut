import SwiftUI
import QuickLook

struct DecisionCard: View {
    @Environment(AppStore.self) private var store
    let decision: Decision
    var showsBot = false
    var showsUndo = true
    var replyFocus: FocusState<Bool>.Binding? = nil
    var confirmingOptionId: String? = nil
    var onRespond: () -> Void = {}
    var onPreviewFrameChange: (CGRect) -> Void = { _ in }
    var onViewPrevious: ((String) -> Void)? = nil
    var onChoose: ((String, String?) -> Void)? = nil
    @State private var dismissing = false
    @State private var dismissRequest: (revision: Int, id: String)?
    @State private var submitting = false
    @State private var submittingOptionId: String?
    @State private var responseError: String?
    @State private var loadedPreviewIds: Set<String> = []
    @State private var editingDraft: Decision?
    private var canRespond: Bool { store.canSubmitDecision }
    private var showsSelection: Bool { !decision.isPending || dismissing }
    private var isDismissed: Bool { dismissing || decision.status == "dismissed" }
    /// A card in the chat has its own X. The iPhone Inbox dismisses with a swipe, but a Mac has no swipe, so its Inbox
    /// card has the X too.
    private var showsDismiss: Bool {
        #if os(macOS)
        true
        #else
        !showsBot
        #endif
    }
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
                    ScrollView {
                        cardContent.padding(16)
                    }
                    .scrollDismissesKeyboard(.interactively)
                    .accessibilityIdentifier("inbox-card-scroll")
                    .simultaneousGesture(TapGesture().onEnded { replyFocus?.wrappedValue = false })
                    if let replyFocus {
                        DecisionReplyComposer(decision: decision, focused: replyFocus, onSend: onRespond)
                            .padding(12)
                    }
                }
            } else { cardContent.padding(16) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.large))
        .clipShape(RoundedRectangle(cornerRadius: BoardStyle.Radius.large))
        .overlay {
            if showsBot {
                RoundedRectangle(cornerRadius: BoardStyle.Radius.large).strokeBorder(BoardStyle.line, lineWidth: 1)
                    .allowsHitTesting(false).accessibilityHidden(true)
            }
        }
        .sheet(item: $editingDraft) { original in MessageDraftEditor(decision: original) }
    }

    private var cardContent: some View {
        VStack(alignment: .leading, spacing: 16) {
            if let previousId = decision.previousDecisionId,
               store.decisions.first(where: { $0.id == previousId })?.status != "replied" {
                let previous = store.decisions.first { $0.id == previousId }
                let label = previous?.selectedOptionLabel.map { "Earlier choice: \($0)" } ?? "View earlier answer"
                Group {
                    if let onViewPrevious {
                        Button { onViewPrevious(previousId) } label: {
                            Label(label, systemImage: "arrow.turn.up.left")
                        }
                    } else {
                        NavigationLink(value: previous.map { store.decisionChatRoute(for: $0) }
                            ?? ConversationRoute(botId: store.conversationBotId(for: decision), decisionId: previousId)) {
                            Label(label, systemImage: "arrow.turn.up.left")
                        }
                    }
                }
                .font(.caption).foregroundStyle(.secondary).buttonStyle(.plain)
                .frame(minHeight: 44, alignment: .leading)
                .accessibilityIdentifier("previous-decision-\(decision.id)")
            }
            HStack(alignment: .top, spacing: 8) {
                DecisionIntroduction(decision: decision, showsContext: !showsSelection)
                    .frame(maxWidth: .infinity, alignment: .leading)
                if showsDismiss && !showsSelection {
                    Button(action: dismiss) {
                        // The icon sits at the top of its tap area, level with the first line of the title.
                        Image(systemName: "xmark").font(.body).foregroundStyle(.secondary)
                            .padding(.top, 2)
                            .frame(width: 44, height: 44, alignment: .topTrailing).contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .disabled(!canRespond || submitting || store.answeringDecisionIds.contains(decision.id))
                    .accessibilityLabel("Dismiss card")
                    .accessibilityIdentifier("dismiss-card-\(decision.id)")
                }
            }
            // Inbox has its own Undo and moves on at once, so a row here would only shift the card while it leaves.
            if showsUndo, let waiting = store.waitingAnswer(for: decision.id) {
                AnswerUndoRow(answer: waiting) { store.undoAnswer(decisionId: decision.id) }
            }
            if let previews = decision.previews, !previews.isEmpty {
                DecisionPreviewStrip(previews: previews, loadedIds: $loadedPreviewIds, onFrameChange: onPreviewFrameChange)
                    .id(decision.previewsHash)
            }
            if let draft = decision.draft { MessageDraftContent(draft: draft) }
            if !showsSelection {
                if decision.draft != nil { draftActions }
                else if !decision.cardOptions.isEmpty { choices }
                if !canRespond {
                    Text("Pair with your Mac to respond.").font(.caption).foregroundStyle(.secondary)
                }
                if let responseError {
                    Text(responseError).font(.caption).foregroundStyle(BoardStyle.attention)
                        .textSelection(.enabled)
                }
            } else if decision.isRefreshing {
                Text("Updating this decision…").font(.subheadline).foregroundStyle(.secondary)
                    .accessibilityIdentifier("decision-refreshing-\(decision.id)")
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

    private var draftActions: some View {
        MessageDraftActions(selected: confirmingOptionId == "send", send: { answer("approve", optionId: "send") },
            edit: { replyFocus?.wrappedValue = false; onRespond(); editingDraft = decision },
            discard: { answer("reject", optionId: "discard") })
        .disabled(!canRespond || submitting || store.answeringDecisionIds.contains(decision.id))
    }

    private var choices: some View {
        VStack(spacing: 8) {
            ForEach(decision.cardOptions, id: \.id) { option in
                choiceButton(option)
            }
        }
    }

    private func choiceButton(_ option: DecisionOption) -> some View {
        Button { answer(option.answer, optionId: decision.options.isEmpty ? nil : option.id) } label: {
            DecisionChoiceContent(option: option, isRecommended: decision.recommendedOptionId == option.id, isSending: submittingOptionId == option.id, isSelected: confirmingOptionId == option.id, isCompact: showsBot, showsAssessment: decision.assessmentStale != true)
        }
        .buttonStyle(HomePressStyle())
        .disabled(!canRespond || submitting || store.answeringDecisionIds.contains(decision.id)
            || (option.answer == "approve" && (decision.previews ?? []).contains { !loadedPreviewIds.contains($0.id) }))
        .accessibilityIdentifier("decision-option-\(option.id)")
        .accessibilityLabel(option.label + (decision.recommendedOptionId == option.id ? ", Recommended" : ""))
        .accessibilityHint(choiceHint(option))
        .accessibilityValue(submittingOptionId == option.id ? "Sending choice" : confirmingOptionId == option.id ? "Selected" : "")
        .accessibilityAddTraits(confirmingOptionId == option.id ? .isSelected : [])
    }

    private func choiceHint(_ option: DecisionOption) -> String {
        var hint = "Submits this choice. " + option.explanation
        if decision.assessmentStale != true, let reason = option.risk, !reason.isEmpty {
            hint += " \(DecisionRisk.label(option.riskLevel)). \(reason)"
        }
        return hint
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
                    .foregroundStyle(isDismissed ? .secondary : decision.status == "failed" ? BoardStyle.attention :
                                        decision.isPending ? .secondary : BoardStyle.positive)
                    .accessibilityLabel(isDismissed ? "Dismissed" : decision.isPending ? "Sending choice" :
                                            decision.status == "failed" ? "Action failed" : "Choice recorded")
            }
            .padding(.horizontal, 12).padding(.vertical, 12)
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .background(BoardStyle.paper, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.small))
            .overlay(RoundedRectangle(cornerRadius: BoardStyle.Radius.small).stroke(BoardStyle.line))
            if decision.status == "failed", let outcome = decision.outcome, !outcome.isEmpty {
                Text(outcome).font(.caption).foregroundStyle(BoardStyle.attention)
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
        if choice == "approve", (decision.previews ?? []).contains(where: { !loadedPreviewIds.contains($0.id) }) {
            responseError = "Wait for the previews to load before choosing this option."
            return
        }
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

struct DecisionIntroduction: View {
    let decision: Decision
    var showsContext = true

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            VStack(alignment: .leading, spacing: 8) {
                Text(decision.title).font(.body.weight(.semibold))
                    .fixedSize(horizontal: false, vertical: true)
                if !decision.cardSources.isEmpty {
                    DecisionSources(sources: decision.cardSources)
                }
                if decision.mergePrUrl != nil, let diff = decision.mergePrDiff {
                    Text(diff.summary).font(.footnote.monospacedDigit()).foregroundStyle(.secondary)
                        .accessibilityLabel(diff.accessibilityLabel)
                        .accessibilityIdentifier("decision-merge-diff")
                }
            }
            if showsContext, let context = decision.cardContext {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(Array(context.components(separatedBy: "\n\n").enumerated()), id: \.offset) { _, paragraph in
                        Text(paragraph).font(.subheadline).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            if showsContext { DecisionAssessment(decision: decision) }
        }
    }
}

struct DecisionRiskDescription: View {
    let level: String?
    let reason: String?

    var body: some View {
        if let reason, !reason.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                Text(DecisionRisk.label(level)).fontWeight(.medium)
                Text(reason).foregroundStyle(.secondary)
            }
            .font(.subheadline)
            .fixedSize(horizontal: false, vertical: true)
            .accessibilityElement(children: .combine)
        }
    }
}

struct DecisionAssessment: View {
    let decision: Decision

    var body: some View {
        if decision.assessmentStale == true {
            Text("Assessment needs updating").font(.subheadline).foregroundStyle(.secondary)
                .accessibilityIdentifier("decision-assessment-stale")
        } else {
            DecisionRiskDescription(level: decision.riskLevel, reason: decision.risk)
        }
    }
}

struct DecisionSources: View {
    let sources: [DecisionSource]

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(sources, id: \.self) { source in
                Text(reference(source))
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .font(.footnote)
        .foregroundStyle(.secondary)
        .tint(.secondary)
    }

    private func reference(_ source: DecisionSource) -> AttributedString {
        var text = AttributedString("\(source.label) · \(source.title)" + (source.link == nil ? "" : " ↗"))
        if let url = source.link {
            text.link = url
        }
        return text
    }
}


struct DecisionChoiceContent: View {
    let option: DecisionOption
    var isRecommended = false
    var isSending = false
    var isSelected = false
    var isCompact = false
    var showsAssessment = true
    @ScaledMetric(relativeTo: .body) private var checkSize = 20

    var body: some View {
        HStack(alignment: .top, spacing: isCompact ? 8 : 12) {
            VStack(alignment: .leading, spacing: isCompact ? 3 : 5) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    (Text(option.label) + Text(isRecommended ? "  Recommended" : "").font(.caption).foregroundColor(.secondary))
                        .font(.body)
                        .foregroundStyle(.primary)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                    Image(systemName: "checkmark")
                        .font(.body.weight(.semibold))
                        .foregroundStyle(BoardStyle.positive)
                        .frame(width: checkSize, height: checkSize)
                        .opacity(isSelected || isSending ? 1 : 0)
                        .accessibilityHidden(true)
                }
                if !option.explanation.isEmpty {
                    Text(option.explanation).font(.subheadline).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if showsAssessment { DecisionRiskDescription(level: option.riskLevel, reason: option.risk) }
            }
        }
        .padding(.horizontal, 12).padding(.vertical, isCompact ? 8 : 12)
        .frame(maxWidth: .infinity, minHeight: 44)
        .background(isSelected ? BoardStyle.positive.opacity(0.14) : BoardStyle.paper,
                    in: RoundedRectangle(cornerRadius: BoardStyle.Radius.small))
        .overlay(RoundedRectangle(cornerRadius: BoardStyle.Radius.small)
            .stroke(isSelected ? BoardStyle.positive.opacity(0.7) : BoardStyle.line, lineWidth: 1))
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
    @ScaledMetric(relativeTo: .title3) private var iconWidth = 30
    @ScaledMetric(relativeTo: .title3) private var iconHeight = 36
    private var shortResult: Bool {
        artifact.preview.count <= 160 && artifact.preview.components(separatedBy: "\n").count <= 3
    }

    private var externalURL: URL? {
        guard let raw = artifact.url, let url = URL(string: raw), let scheme = url.scheme,
              ["https", "http"].contains(scheme) else { return nil }
        return url
    }

    var body: some View {
        TimelineView(.periodic(from: .now, by: 30)) { context in
            VStack(alignment: .leading, spacing: 10) {
                HStack(alignment: .top, spacing: 12) {
                    Image(systemName: artifact.kind == "pr" ? "arrow.triangle.pull" : artifact.kind == "task" ? "checkmark.circle" : "doc.text")
                        .font(.title3).foregroundStyle(.secondary)
                        .frame(width: iconWidth, height: iconHeight)
                    VStack(alignment: .leading, spacing: 6) {
                        Text(artifact.title).font(.subheadline.weight(.semibold))
                        StatusLabel(status: artifact.cardStatus(at: context.date))
                    }
                    Spacer(minLength: 0)
                }
                if !artifact.cardPreview(at: context.date).isEmpty {
                    Text(.init(artifact.cardPreview(at: context.date))).font(.subheadline).foregroundStyle(.secondary)
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
                        ShareLink(item: externalURL) { Image(systemName: "square.and.arrow.up").frame(minWidth: 44, minHeight: 44) }
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
            .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.large))
            .quickLookPreview($previewURL)
        }
    }
}

/// An answer that the app holds for its Undo window: "Sending in 8 s" and Undo.
struct AnswerUndoRow: View {
    let answer: OutgoingAnswer
    let undo: () -> Void

    // The answer shows as sent at once. While the app holds it, Undo takes it back, and nothing reaches the Mac.
    var body: some View {
        HStack(spacing: 12) {
            Label(answer.text, systemImage: "checkmark.circle.fill")
                .font(.subheadline.weight(.semibold)).lineLimit(2)
                .frame(maxWidth: .infinity, alignment: .leading)
            Button("Undo", action: undo)
                .buttonStyle(.bordered)
                .frame(minHeight: 44)
                .accessibilityIdentifier("undo-answer-\(answer.decisionId)")
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Answered: \(answer.text)")
        .accessibilityAction(named: "Undo", undo)
    }
}
