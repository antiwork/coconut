import SwiftUI

enum InboxSort: String, CaseIterable {
    case recommended = "Recommended"
    case oldest = "Oldest first"
    case newest = "Newest first"
}

struct DecisionInboxView: View {
    @Binding var focusDecisionId: String?

    init(focusDecisionId: Binding<String?> = .constant(nil)) {
        _focusDecisionId = focusDecisionId
    }
    @Environment(AppStore.self) private var store
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @AppStorage("inboxSort") private var sort = InboxSort.recommended.rawValue
    @State private var queue = DecisionQueue()
    @State private var lastSkipped: InboxUndoOperation?
    @State private var restoringDecision: Decision?
    @State private var confirmingChoice: (decision: Decision, optionId: String?)?
    @State private var updatingIds: Set<String> = []
    @State private var overviewOpen = false
    @State private var selectedId: String?
    @State private var offset: CGFloat = 0
    @State private var horizontalDrag = false
    @State private var verticalDrag = false
    @State private var skipGeneration = 0
    @GestureState private var dragging = false
    @State private var exiting = false
    @State private var transitionId = UUID()
    @FocusState private var replyFocused: Bool
    private var pending: [Decision] { store.decisions.filter { $0.isPending && !store.answeringDecisionIds.contains($0.id) } }
    private var current: Decision? {
        if let confirmingChoice { return confirmingChoice.decision }
        let id = selectedId ?? queue.ids.first
        if let restoringDecision, restoringDecision.id == id { return restoringDecision }
        return pending.first { $0.id == id }
    }
    private var sorting: InboxSort { InboxSort(rawValue: sort) ?? .recommended }
    private var canRespond: Bool { store.connection == .connected || store.isDemo }

    var body: some View {
        inboxContent
        .padding(.bottom, 12)
        .background(BoardStyle.paper)
        .toolbar { inboxToolbar }
        .onAppear { queue.reconcile(pending, sort: sorting); focusNotification() }
        .onChange(of: focusDecisionId) { _, _ in focusNotification() }
        .task(id: lastSkipped?.receipt?.inboxUndoUntil) {
            guard let deadline = lastSkipped?.receipt?.inboxUndoUntil?.apiDate else { return }
            do { try await Task.sleep(for: .seconds(max(0, deadline.timeIntervalSinceNow))) }
            catch { return }
            clearUndo()
        }
        .onDisappear { clearUndo() }
        .onChange(of: dragging) { _, active in
            if !active && !exiting {
                horizontalDrag = false; verticalDrag = false
                withAnimation(.spring(response: 0.3, dampingFraction: 0.82)) { offset = 0 }
            }
        }
        .onChange(of: current.map { "\($0.id):\($0.revision)" }) { _, _ in
            transitionId = UUID(); offset = 0; horizontalDrag = false; verticalDrag = false; exiting = false
            replyFocused = false
        }
        .onChange(of: pending.map { "\($0.id):\($0.revision):\($0.inboxDeferredAt ?? "")" }) { _, _ in
            if let selectedId, selectedId != restoringDecision?.id, !pending.contains(where: { $0.id == selectedId }) { self.selectedId = nil }
            queue.reconcile(pending, sort: sorting)
            focusNotification()
        }
        .onChange(of: store.decisions.map { "\($0.id):\($0.revision):\($0.status):\($0.inboxSwipeId ?? "")" }) { _, _ in
            if let receipt = lastSkipped?.receipt,
               !store.decisions.contains(where: { $0.id == receipt.id && $0.revision == receipt.revision && $0.inboxSwipeId == receipt.inboxSwipeId && ($0.isPending || $0.status == "dismissed") }) {
                clearUndo()
            }
            if let restoringDecision,
               store.decisions.contains(where: { $0.id == restoringDecision.id && !["pending", "dismissed"].contains($0.status) }) {
                self.restoringDecision = nil
                selectedId = nil
            }
        }
        .onChange(of: sort) { _, _ in queue.reconcile(pending, sort: sorting, reset: true) }
        .sheet(isPresented: $overviewOpen) { overview }
    }

    private func focusNotification() {
        guard let id = focusDecisionId, pending.contains(where: { $0.id == id }) else { return }
        clearUndo()
        overviewOpen = false
        queue.restoreFirst(id)
        selectedId = id
        focusDecisionId = nil
    }

    @ToolbarContentBuilder
    private var inboxToolbar: some ToolbarContent {
        ToolbarItem(placement: .principal) {
            Button("\(queue.remainingCount) left") { overviewOpen = true }
                .font(.subheadline.weight(.semibold)).monospacedDigit().frame(minHeight: 44)
                .opacity(current == nil ? 0 : 1)
                .disabled(current == nil || restoringDecision != nil || confirmingChoice != nil)
                .accessibilityHidden(current == nil)
                .accessibilityLabel("\(queue.remainingCount) decisions left. Show all")
        }
        if #available(iOS 26.0, *) {
            ToolbarItem(placement: .topBarTrailing) { undoButton }
                .sharedBackgroundVisibility(.hidden)
        } else {
            ToolbarItem(placement: .topBarTrailing) { undoButton }
        }
    }

    private var undoButton: some View {
        ZStack {
            if lastSkipped != nil {
                Button("Undo", action: undo)
                    .disabled(exiting)
                    .accessibilityIdentifier("undo-inbox-dismiss")
            } else {
                Color.clear.accessibilityHidden(true)
            }
        }.frame(width: 54, height: 44)
    }

    private var inboxContent: some View {
        VStack(spacing: 12) {
            GeometryReader { geometry in
                if let decision = current {
                    cardStack(decision, size: geometry.size)
                        .id(decision.id)
                        .transition(.opacity.combined(with: .scale(scale: reduceMotion ? 1 : 0.98)))
                } else {
                    VStack(spacing: 16) {
                        Spacer()
                        VStack(spacing: 8) {
                            BotAvatar(symbol: CoconutShape.round.avatar, color: "64866A", size: 64,
                                      identity: "inbox-caught-up", animated: false)
                            Text("All caught up")
                                .font(.title3.weight(.semibold)).accessibilityIdentifier("inbox-empty")
                        }
                        ZStack {
                            Color.clear.frame(height: 44)
                            if !pending.isEmpty {
                            Button("Review \(pending.count) pending") {
                                clearUndo()
                                queue.reviewAgain(pending, sort: sorting)
                            }
                                .font(.subheadline).frame(minHeight: 44)
                                .accessibilityIdentifier("inbox-review-later")
                            }
                        }.frame(height: 44)
                        Spacer()
                    }.frame(maxWidth: .infinity)
                }
            }.padding(.horizontal, 16)

        }
    }

    private func cardStack(_ decision: Decision, size: CGSize) -> some View {
        let progress = min(abs(offset) / size.width, 1)
        let cardHeight = max(0, size.height - 24)
        return ZStack(alignment: .top) {
            if decisionsAfter(decision).count > 1 {
                RoundedRectangle(cornerRadius: 24)
                    .fill(BoardStyle.surface)
                    .overlay(RoundedRectangle(cornerRadius: 24).stroke(.white.opacity(0.12)))
                    .frame(height: cardHeight)
                    .scaleEffect(0.92 + progress * 0.04, anchor: .bottom)
                    .offset(y: 20 - progress * 10)
                    .accessibilityHidden(true)
            }
            if let next = decisionsAfter(decision).first {
                nextCard(next)
                    .frame(height: cardHeight)
                    .overlay(RoundedRectangle(cornerRadius: 24).stroke(.white.opacity(0.16)))
                    .scaleEffect(0.96 + progress * 0.04, anchor: .bottom)
                    .offset(y: 10 * (1 - progress))
                    .allowsHitTesting(false).accessibilityHidden(true)
            }
            DecisionCard(decision: decision, showsBot: true, replyFocus: $replyFocused, confirmingOptionId: confirmingChoice?.optionId, onRespond: clearUndo, onChoose: { choice, optionId in
                choose(decision, choice: choice, optionId: optionId)
            })
                .id(decision.id)
                .frame(height: cardHeight)
                .disabled(horizontalDrag || exiting || updatingIds.contains(decision.id) || restoringDecision != nil)
                .overlay { swipeLabel(for: decision, width: size.width) }
                .rotationEffect(.degrees(reduceMotion ? 0 : Double(offset / size.width) * -7), anchor: .bottom)
                .offset(x: reduceMotion && exiting ? 0 : offset)
                .opacity(reduceMotion && exiting ? 0 : 1)
                .simultaneousGesture(swipe(decision, width: size.width))
                .accessibilityElement(children: .contain)
                .accessibilityActions {
                    Button("Later") { leave(decision, action: .later, width: size.width) }
                    Button("Dismiss") { leave(decision, action: .dismiss, width: size.width, direction: 1) }
                }
        }
    }

    private var overview: some View {
        NavigationStack {
            List {
                Picker("Sort", selection: $sort) {
                    ForEach(InboxSort.allCases, id: \.rawValue) { Text($0.rawValue).tag($0.rawValue) }
                }
                ForEach(InboxPresentation.entries(decisions: pending, sort: sorting)) { decision in
                    Button {
                        clearUndo()
                        queue.restoreFirst(decision.id)
                        selectedId = decision.id
                        overviewOpen = false
                    } label: {
                        VStack(alignment: .leading, spacing: 6) {
                            Text(store.bots.first { $0.id == decision.botId }?.name ?? "Bot")
                                .font(.caption).foregroundStyle(.secondary)
                            Text(decision.title).font(.body).foregroundStyle(.primary)
                        }.padding(.vertical, 4)
                    }
                }
            }
            .navigationTitle("Inbox").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { overviewOpen = false } } }
        }
    }

    private func decisionsAfter(_ decision: Decision) -> [Decision] {
        queue.ids.filter { $0 != decision.id }.compactMap { id in pending.first { $0.id == id } }
    }

    private func nextCard(_ decision: Decision) -> some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                if let bot = store.bots.first(where: { $0.id == decision.botId }) {
                    BotAvatar(bot: bot, size: 24)
                    Text(bot.name).font(.subheadline.weight(.medium)).lineLimit(1)
                }
                Spacer(minLength: 12)
                Text("Open chat").font(.subheadline)
                Image(systemName: "chevron.right").font(.caption2)
            }.frame(height: 44).padding(.horizontal, 16)
            Divider()
            VStack(alignment: .leading, spacing: 10) {
                VStack(alignment: .leading, spacing: 8) {
                    Text(decision.title).font(.body.weight(.semibold))
                    if let context = decision.cardContext {
                        Text(context).font(.subheadline).foregroundStyle(.secondary)
                    }
                }
                if let draft = decision.draft {
                    MessageDraftContent(draft: draft)
                    MessageDraftActions()
                } else {
                VStack(spacing: 0) {
                    ForEach(Array(decision.cardOptions.enumerated()), id: \.element.id) { index, option in
                        DecisionChoiceContent(option: option, index: index, isRecommended: decision.recommendedOptionId == option.id, isCompact: true)
                        if option.id != decision.cardOptions.last?.id { Divider().padding(.horizontal, 12) }
                    }
                }.background(BoardStyle.paper, in: RoundedRectangle(cornerRadius: 12))
                }
            }.padding(16).frame(maxWidth: .infinity, alignment: .leading)
            Spacer(minLength: 0)
            HStack {
                Text("Reply…").font(.subheadline).foregroundStyle(.tertiary).padding(.leading, 12)
                Spacer()
                Image(systemName: "arrow.up.circle.fill").font(.system(size: 28))
                    .foregroundStyle(.tertiary).frame(width: 44, height: 44)
            }
            .background(BoardStyle.paper, in: RoundedRectangle(cornerRadius: 14))
            .overlay(RoundedRectangle(cornerRadius: 14).stroke(BoardStyle.line))
            .padding(12)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: 24))
        .clipShape(RoundedRectangle(cornerRadius: 24))
    }

    private func swipeLabel(for decision: Decision, width: CGFloat) -> some View {
        let action: InboxSwipeAction = offset > 0 ? .dismiss : .later
        return ZStack(alignment: offset < 0 ? .topTrailing : .topLeading) {
            if offset != 0 {
                let color = action == .dismiss ? Color(red: 0.7, green: 0.25, blue: 0.23) : Color.blue
                RoundedRectangle(cornerRadius: 24).fill(color.opacity(min(abs(offset) / (width * 0.4), 0.85)))
                VStack(alignment: offset < 0 ? .trailing : .leading, spacing: 8) {
                    Image(systemName: action == .dismiss ? "xmark.circle" : "clock")
                        .font(.system(size: 32))
                    Text(action == .dismiss ? "Dismiss" : "Later")
                        .font(.title2.weight(.bold)).fixedSize(horizontal: false, vertical: true)
                }
                .frame(maxWidth: 145, alignment: offset < 0 ? .trailing : .leading)
                .foregroundStyle(.white).padding(24).opacity(min(abs(offset) / 60, 1))
            }
        }.allowsHitTesting(false).accessibilityHidden(true)
    }

    private func swipe(_ decision: Decision, width: CGFloat) -> some Gesture {
        DragGesture(minimumDistance: 18)
            .updating($dragging) { _, active, _ in active = true }
            .onChanged { value in
                guard !exiting, restoringDecision == nil, canRespond, !replyFocused, !updatingIds.contains(decision.id) else { return }
                guard !verticalDrag else { return }
                if !horizontalDrag {
                    guard abs(value.translation.width) > abs(value.translation.height) * 1.5 else {
                        verticalDrag = true
                        return
                    }
                    horizontalDrag = true
                }
                let movement = value.translation.width
                offset = movement
            }
            .onEnded { value in
                guard horizontalDrag, !exiting else { return }
                horizontalDrag = false
                if let action = InboxSwipeAction.resolve(translation: value.translation.width,
                    projected: value.predictedEndTranslation.width, width: width, isProposal: decision.isProposal) {
                    leave(decision, action: action, width: width, direction: value.translation.width > 0 ? 1 : -1)
                } else {
                    withAnimation(.spring(response: 0.3, dampingFraction: 0.82)) { offset = 0 }
                }
            }
    }

    private func choose(_ decision: Decision, choice: String, optionId: String?) {
        guard canRespond, !exiting, confirmingChoice == nil, !updatingIds.contains(decision.id), current?.id == decision.id else { return }
        clearUndo()
        replyFocused = false
        updatingIds.insert(decision.id)
        // Keep the tapped card visible while the write changes the pending queue.
        confirmingChoice = (decision, optionId ?? decision.cardOptions.first { $0.answer == choice }?.id)
        UISelectionFeedbackGenerator().selectionChanged()
        let write = Task { await store.answer(decision, choice: choice, optionId: optionId) }
        Task {
            try? await Task.sleep(for: .milliseconds(340))
            withAnimation(.easeOut(duration: reduceMotion ? 0.1 : 0.2)) {
                queue.remove(decision.id)
                selectedId = nil
                confirmingChoice = nil
            }
            let confirmed = await write.value
            updatingIds.remove(decision.id)
            if !confirmed {
                let visibleId = current?.id
                queue.restoreFirst(decision.id)
                selectedId = visibleId ?? decision.id
            }
        }
    }

    private func leave(_ decision: Decision, action: InboxSwipeAction, width: CGFloat, direction: CGFloat = -1) {
        guard canRespond, !exiting, restoringDecision == nil, !updatingIds.contains(decision.id), current?.id == decision.id else { return }
        replyFocused = false
        exiting = true
        clearUndo()
        let token = UUID(); transitionId = token
        withAnimation(reduceMotion ? .easeOut(duration: 0.12) : .spring(response: 0.3, dampingFraction: 0.95), completionCriteria: .logicallyComplete) {
            offset = direction * width * 1.3
        } completion: {
            guard transitionId == token, current?.id == decision.id else { return }
            queue.remove(decision.id)
            selectedId = nil; offset = 0; exiting = false
            updatingIds.insert(decision.id)
            let key = UUID().uuidString
            let operation = InboxUndoOperation(decision: decision, action: action, idempotencyKey: key,
                write: Task { await store.updateInbox(decision, action: action.rawValue, idempotencyKey: key) })
            lastSkipped = operation
            Task {
                let result = await operation.write.value
                updatingIds.remove(decision.id)
                if let saved = result.decision {
                    if lastSkipped?.id == operation.id {
                        if store.decisions.contains(where: { $0.id == saved.id && $0.revision == saved.revision && $0.inboxSwipeId == saved.inboxSwipeId && ($0.isPending || $0.status == "dismissed") }) {
                            lastSkipped?.receipt = saved
                        } else { clearUndo() }
                    }
                } else if !result.isUncertain {
                    let ownsUndo = lastSkipped?.id == operation.id
                    if ownsUndo { clearUndo() }
                    if pending.contains(where: { $0.id == decision.id }) && restoringDecision?.id != decision.id {
                        let visibleId = current?.id
                        queue.restoreFirst(decision.id)
                        selectedId = ownsUndo ? decision.id : visibleId
                    }
                }
            }
        }
    }

    private func clearUndo() {
        lastSkipped = nil
        if selectedId == restoringDecision?.id { selectedId = nil }
        restoringDecision = nil
        skipGeneration += 1
    }

    private func undo() {
        guard let operation = lastSkipped, !exiting else { return }
        lastSkipped = nil
        let decision = operation.decision
        let previousId = current?.id
        let generation = skipGeneration
        restoringDecision = decision
        queue.restoreFirst(decision.id)
        selectedId = decision.id
        updatingIds.insert(decision.id)
        Task {
            var result = await operation.write.value
            let recoveringSwipe = result.isUncertain
            if recoveringSwipe {
                // Replay the same write to recover its receipt without applying a second swipe.
                result = await store.updateInbox(decision, action: operation.action.rawValue, idempotencyKey: operation.idempotencyKey)
            }
            let receipt = result.decision
            let restored: InboxMutationResult
            if let receipt {
                restored = await store.updateInbox(receipt, action: "restore", idempotencyKey: operation.restoreKey)
            } else {
                restored = result
            }
            updatingIds.remove(decision.id)
            guard generation == skipGeneration else { return }
            restoringDecision = nil
            if restored.decision != nil, pending.contains(where: { $0.id == decision.id }) {
                queue.restoreFirst(decision.id)
                selectedId = decision.id
            } else if receipt == nil, !recoveringSwipe, !result.isUncertain, pending.contains(where: { $0.id == decision.id }) {
                selectedId = decision.id
            } else {
                queue.remove(decision.id)
                selectedId = pending.contains(where: { $0.id == previousId }) ? previousId : nil
                if (recoveringSwipe && receipt == nil) || restored.isUncertain {
                    store.error = "Undo is not confirmed. Reconnect and try Undo again."
                    var retry = operation
                    retry.receipt = receipt
                    lastSkipped = retry
                } else if let receipt, let deadline = receipt.inboxUndoUntil?.apiDate, deadline > Date(),
                   store.decisions.contains(where: { $0.id == receipt.id && $0.inboxSwipeId == receipt.inboxSwipeId && ($0.isPending || $0.status == "dismissed") }) {
                    var retry = operation
                    retry.receipt = receipt
                    lastSkipped = retry
                }
            }
        }
    }
}

private struct InboxUndoOperation {
    let id = UUID()
    let decision: Decision
    let action: InboxSwipeAction
    let idempotencyKey: String
    let restoreKey = UUID().uuidString
    let write: Task<InboxMutationResult, Never>
    var receipt: Decision?
}
