import SwiftUI
import QuickLook

/// The words of the receipt of a card that is not pending.
enum DecisionSelection {
    /// A draft says "Approved for sending" only when the person approved it. Coconut can close a card without an answer,
    /// for example when its GitHub source closed; then the card shows its outcome. The receipt shows no other outcome
    /// text, except the line under a failed card, which never repeats this label.
    static func label(status: String, hasDraft: Bool, dismissed: Bool, choosingLabel: String?,
                      selectedOptionLabel: String?, outcome: String?, decidedBy: String? = nil) -> String {
        if dismissed { return "Dismissed" }
        // A row shows an outcome only when it is one short line. A check that withdrew a card leaves its whole evidence
        // as the outcome; such a card says "Closed".
        let said = outcome.flatMap { text in
            text.isEmpty ? nil : text.count <= 80 && !text.contains(where: \.isNewline) ? text : "Closed"
        }
        if byService(decidedBy) { return said ?? "Closed" }
        if hasDraft {
            switch status {
            case "pending", "approved", "executed": return "Approved for sending"
            case "rejected": return "Discarded"
            case "failed": return "Could not finish"
            default: return said ?? "Closed"
            }
        }
        if let choosingLabel { return choosingLabel }
        if let selectedOptionLabel { return selectedOptionLabel }
        if status == "resolved", let said { return said }
        return status.capitalized
    }

    /// The statuses that record a choice of a person on the card: an option, Send, a discard or a reply.
    private static let choices: Set<String> = ["approved", "executed", "rejected", "modified", "replied"]

    /// The deciders that are the service, not a person: a task cancellation, an archive and a workflow permission.
    private static let serviceDeciders: Set<String> = ["cancellation", "archive", "workflow_permission"]

    /// True when the service, not a person, decided the card (`decided_by`). A task cancellation rejects its cards so.
    static func byService(_ decidedBy: String?) -> Bool { decidedBy.map(serviceDeciders.contains) ?? false }

    /// A receipt shows the check only for a choice of a person. A card that closed without a choice, for example
    /// `resolved`, `cancelled` or rejected by a task cancellation, shows its words as a state: no check, in the
    /// secondary color. A pending card, a failed card and a dismissed card keep their own marks.
    static func showsCheck(status: String, decidedBy: String? = nil) -> Bool {
        choices.contains(status) && !byService(decidedBy)
    }

    /// True for a card that closed without a choice of a person (see showsCheck). Its words use the secondary color.
    static func closedWithoutChoice(status: String, dismissed: Bool, decidedBy: String? = nil) -> Bool {
        !dismissed && !["pending", "failed"].contains(status) && !showsCheck(status: status, decidedBy: decidedBy)
    }

    /// The words, the mark and the color of the receipt row of one card. The view shows exactly this.
    struct Receipt: Equatable {
        let label: String
        let mark: String?
        let dimmed: Bool
    }

    static func receipt(_ decision: Decision, dismissed: Bool, choosingLabel: String? = nil) -> Receipt {
        Receipt(label: label(status: decision.status, hasDraft: decision.draft != nil, dismissed: dismissed,
                             choosingLabel: choosingLabel, selectedOptionLabel: decision.selectedOptionLabel,
                             outcome: decision.outcome, decidedBy: decision.decidedBy),
                mark: mark(status: decision.status, pending: decision.isPending, dismissed: dismissed,
                           decidedBy: decision.decidedBy),
                dimmed: closedWithoutChoice(status: decision.status, dismissed: dismissed, decidedBy: decision.decidedBy))
    }

    /// The mark at the end of the receipt row, as the name of a symbol. A card that closed without a choice has none.
    static func mark(status: String, pending: Bool, dismissed: Bool, decidedBy: String? = nil) -> String? {
        if dismissed { return "xmark" }
        if status == "failed" { return "exclamationmark.circle" }
        if pending { return "arrow.up" }
        return showsCheck(status: status, decidedBy: decidedBy) ? "checkmark" : nil
    }
}

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
    /// The recorded dismissal of this card, for an Undo outside the card.
    var onDismissed: ((Decision) -> Void)? = nil
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
    private var receipt: DecisionSelection.Receipt {
        DecisionSelection.receipt(decision, dismissed: isDismissed,
                                  choosingLabel: submittingOptionId.flatMap { id in decision.options.first { $0.id == id }?.label })
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
                .font(.footnote).foregroundStyle(.secondary).buttonStyle(.plain)
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
                    Text("Pair with your Mac to respond.").font(.footnote).foregroundStyle(.secondary)
                }
                if let responseError {
                    Text(responseError).font(.footnote).foregroundStyle(BoardStyle.attention)
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
                    Text(bot.name).font(.subheadline.weight(.semibold)).lineLimit(1)
                }
                Spacer(minLength: 12)
                Text("Open chat").font(.subheadline)
                Image(systemName: "chevron.right").font(.subheadline).imageScale(.small)
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
        VStack(alignment: .leading, spacing: BoardStyle.Space.m) {
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                Text(receipt.label).font(.subheadline)
                    .foregroundStyle(receipt.dimmed ? .secondary : .primary)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityIdentifier("decision-selection-\(decision.id)")
                if let mark = receipt.mark {
                    Image(systemName: mark)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(isDismissed ? .secondary : decision.status == "failed" ? BoardStyle.attention :
                                            decision.isPending ? .secondary : BoardStyle.positive)
                        .accessibilityLabel(isDismissed ? "Dismissed" : decision.isPending ? "Sending choice" :
                                                decision.status == "failed" ? "Action failed" : "Choice recorded")
                }
            }
            .padding(.horizontal, 12).padding(.vertical, 12)
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .background(BoardStyle.paper, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.small))
            .overlay(RoundedRectangle(cornerRadius: BoardStyle.Radius.small).stroke(BoardStyle.line))
            if decision.status == "failed", let outcome = decision.outcome, !outcome.isEmpty {
                Text(outcome).font(.footnote).foregroundStyle(BoardStyle.attention)
            }
            if let replacement = decision.replacedByDecisionId {
                NavigationLink(value: ConversationRoute(botId: store.conversationBotId(for: decision), decisionId: replacement)) {
                    Label("View revised proposal", systemImage: "arrow.turn.down.right")
                        .font(.footnote.weight(.semibold)).frame(minHeight: 44)
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
            if let receipt = result.decision {
                dismissRequest = nil
                onDismissed?(receipt)
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
                VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
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
    @Environment(AppStore.self) private var store
    let sources: [DecisionSource]

    var body: some View {
        let links = sources.compactMap(\.link).filter(GitHubLink.isCard)
        VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
            ForEach(sources, id: \.self) { source in
                // A GitHub source shows as its card when the Mac can read it. Else the line with the link stays.
                if let link = source.link, GitHubLink.isCard(link), let preview = store.githubEmbeds.preview(for: link) {
                    GitHubEmbedCard(preview: preview).foregroundStyle(.primary)
                } else {
                    Text(reference(source))
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .font(.footnote)
        .foregroundStyle(.secondary)
        .tint(.secondary)
        .task(id: links) { await store.githubEmbeds.load(Array(links.prefix(GitHubLink.limit))) }
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
                    (Text(option.label) + Text(isRecommended ? "  Recommended" : "").font(.footnote).foregroundColor(.secondary))
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

/// The form of a card in a chat: the inset, the surface and the corner. The agent chat and the team chats share it.
struct ChatCardSurface: ViewModifier {
    func body(content: Content) -> some View {
        content
            // The inset of each card in a chat.
            .padding(BoardStyle.Space.l)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.large))
    }
}

/// The top of a card in a chat: the symbol, the title, and one line under it. A file that is still on its way shows
/// the loading state in the place of the symbol, as a tile of a photo does.
struct ChatCardHead<Detail: View>: View {
    let symbol: String
    let title: String
    var loading = false
    @ViewBuilder let detail: () -> Detail
    @ScaledMetric(relativeTo: .title3) private var iconWidth = 30
    @ScaledMetric(relativeTo: .title3) private var iconHeight = 36

    var body: some View {
        HStack(alignment: .top, spacing: BoardStyle.Space.m) {
            Group {
                if loading { ProgressView() }
                else { Image(systemName: symbol).font(.title3).foregroundStyle(.secondary) }
            }
            .frame(width: iconWidth, height: iconHeight)
            VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
                Text(title).font(.subheadline.weight(.semibold))
                detail()
            }
            Spacer(minLength: 0)
        }
    }
}

/// "Preview file" of a card: it opens the file in Quick Look. "Opening…" while the file loads.
struct PreviewFileButton: View {
    let opening: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Label(opening ? "Opening…" : "Preview file", systemImage: "doc.text.magnifyingglass")
                .font(.subheadline.weight(.semibold)).frame(minHeight: 44)
        }
        .disabled(opening)
    }
}

/// A file of a message of a team chat, in the form of a file card of the agent chat: the name, the kind and the size,
/// and "Preview file". A file that is not on the Mac yet shows the loading state, and its button is off.
struct ChatFileCard: View {
    /// The widest card in a team chat: the width of one tile of a photo, so a card stands on the side of its writer.
    static let width: CGFloat = 280
    let file: TeamFile
    /// Where the bytes are (TeamChatRow.address). Nil keeps the button off.
    let source: URL?
    @State private var previewURL: URL?
    @State private var opening = false

    var body: some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.m) {
            ChatCardHead(symbol: "doc.text", title: file.name, loading: !file.isReady) {
                Text(file.detail).font(.footnote).foregroundStyle(.secondary)
            }
            PreviewFileButton(opening: opening) {
                guard let source else { return }
                opening = true
                Task {
                    previewURL = await QuickLookFile.url(for: source, name: file.name)
                    opening = false
                }
            }
            .disabled(!file.isReady || source == nil)
        }
        .modifier(ChatCardSurface())
        .quickLookPreview($previewURL)
        .accessibilityElement(children: .contain)
        .accessibilityLabel(file.name + ", " + file.detail)
    }
}

/// Copy of a file of a team chat: the image of a photo, else the file.
@MainActor
enum TeamFileCopy {
    static func copy(_ file: TeamFile, source: URL) {
        Task {
            if file.isImage, let image = await PhotoLoader.shared.image(source, size: .full) {
                Pasteboard.copy(image)
            } else if let url = await QuickLookFile.url(for: source, name: file.name) {
                Pasteboard.copyFile(url)
            }
        }
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
        TimelineView(.periodic(from: .now, by: 30)) { context in
            VStack(alignment: .leading, spacing: BoardStyle.Space.m) {
                ChatCardHead(symbol: artifact.kind == "pr" ? "arrow.triangle.pull" : artifact.kind == "task" ? "checkmark.circle" : "doc.text",
                             title: artifact.title) {
                    StatusLabel(status: artifact.cardStatus(at: context.date))
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
                        .font(.footnote).foregroundStyle(.secondary)
                }
                HStack {
                    if let externalURL {
                        Link(destination: externalURL) {
                            Label(artifact.kind == "pr" ? "Open pull request" : "Open result", systemImage: "arrow.up.right")
                                .font(.subheadline.weight(.semibold)).frame(minHeight: 44)
                        }
                        Spacer()
                        ShareLink(item: externalURL) { Image(systemName: "square.and.arrow.up").frame(minWidth: 44, minHeight: 44) }
                    } else if artifact.kind != "task" {
                        PreviewFileButton(opening: downloading) {
                            downloading = true
                            Task {
                                previewURL = await store.downloadArtifact(artifact)
                                downloading = false
                            }
                        }
                    }
                }
            }
            .modifier(ChatCardSurface())
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
