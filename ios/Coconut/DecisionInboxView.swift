import SwiftUI

enum InboxSort: String, CaseIterable {
    case recommended = "Recommended"
    case oldest = "Oldest first"
    case newest = "Newest first"
}

struct DecisionInboxView: View {
    @Binding var focusDecisionId: String?
    let refreshingDecisionId: String?

    init(focusDecisionId: Binding<String?> = .constant(nil), refreshingDecisionId: String? = nil) {
        self.refreshingDecisionId = refreshingDecisionId
        _focusDecisionId = focusDecisionId
    }
    @Environment(AppStore.self) private var store
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @AppStorage("inboxSort") private var sort = InboxSort.recommended.rawValue
    @State private var queue = DecisionQueue()
    @State private var lastSkipped: InboxUndoOperation?
    /// The time that decides which answer still offers Undo. It moves on when the shown answer sends.
    @State private var undoClock = Date()
    @State private var restoringDecision: Decision?
    @State private var confirmingChoice: (decision: Decision, optionId: String?)?
    @State private var updatingIds: Set<String> = []
    @State private var overviewOpen = false
    @State private var selectedId: String?
    @State private var previewFrame: CGRect = .zero
    @State private var previewDecisionId: String?
    @State private var offset: CGFloat = 0
    @State private var horizontalDrag = false
    @State private var verticalDrag = false
    @State private var skipGeneration = 0
    @GestureState private var dragging = false
    @State private var exiting = false
    @State private var transitionId = UUID()
    @FocusState private var replyFocused: Bool
    @ScaledMetric(relativeTo: .subheadline) private var undoWidth = 54
    @ScaledMetric(relativeTo: .title2) private var swipeLabelWidth = 145
    private var pending: [Decision] { store.decisions.filter { $0.isPending && !store.answeringDecisionIds.contains($0.id) } }
    private var current: Decision? {
        if let confirmingChoice { return confirmingChoice.decision }
        let id = selectedId ?? queue.ids.first
        if let restoringDecision, restoringDecision.id == id { return restoringDecision }
        return pending.first { $0.id == id }
    }
    private var sorting: InboxSort { InboxSort(rawValue: sort) ?? .recommended }
    private var canRespond: Bool { store.canSubmitDecision }

    var body: some View {
        Group {
            if let refreshingDecisionId, !pending.contains(where: { $0.id == refreshingDecisionId }) {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                inboxContent.disabled(refreshingDecisionId != nil)
                    .allowsHitTesting(refreshingDecisionId == nil)
            }
        }
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
        .task(id: store.latestWaitingAnswer(at: undoClock)?.id) {
            undoClock = Date()
            guard let sendAt = store.latestWaitingAnswer(at: undoClock)?.sendAt else { return }
            do { try await Task.sleep(for: .seconds(max(0, sendAt.timeIntervalSinceNow))) }
            catch { return }
            undoClock = Date()
        }
        .onChange(of: store.restoredDecisionId) { _, id in
            guard let id, pending.contains(where: { $0.id == id }) else { return }
            store.restoredDecisionId = nil
            clearUndo()
            queue.reconcile(pending, sort: sorting)
            queue.restoreFirst(id)
            selectedId = id
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
            Button { overviewOpen = true } label: {
                HStack(spacing: 4) {
                    Text("\(queue.remainingCount) left").monospacedDigit()
                    Image(systemName: "chevron.down").imageScale(.small).foregroundStyle(.secondary)
                }
            }
                .font(.subheadline.weight(.semibold)).frame(minHeight: 44)
                .opacity(current == nil ? 0 : 1)
                .disabled(current == nil || restoringDecision != nil || confirmingChoice != nil)
                .accessibilityHidden(current == nil)
                .accessibilityLabel("\(queue.remainingCount) decisions left. Show all")
        }
        if #available(iOS 26.0, macOS 26.0, *) {
            ToolbarItem(placement: .trailingBar) { undoButton }
                .sharedBackgroundVisibility(.hidden)
        } else {
            ToolbarItem(placement: .trailingBar) { undoButton }
        }
    }

    private var undoButton: some View {
        ZStack {
            if lastSkipped != nil {
                Button("Undo", action: undo)
                    .disabled(exiting)
                    .accessibilityIdentifier("undo-inbox-dismiss")
            } else if let waiting = store.latestWaitingAnswer(at: undoClock) {
                // The app holds the latest answer for a few seconds. Undo puts its card back at the top of Inbox.
                Button("Undo") { store.undoAnswer(decisionId: waiting.decisionId) }
                    .disabled(exiting)
                    .accessibilityLabel("Undo the answer \(waiting.text)")
                    .accessibilityIdentifier("undo-inbox-answer")
            } else {
                Color.clear.accessibilityHidden(true)
            }
        }.frame(width: undoWidth, height: 44)
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
                            BotAvatar(symbol: "fruit.coconut", color: "64866A", size: 64,
                                      identity: "inbox-caught-up")
                            Text("All caught up.")
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
            }
        }
        .padding(.horizontal, 16)
    }

    private func cardStack(_ decision: Decision, size: CGSize) -> some View {
        let progress = min(abs(offset) / size.width, 1)
        let cardHeight = max(0, size.height - 24)
        return ZStack(alignment: .top) {
            // The next card is a plain surface. Only the top card shows content.
            if !decisionsAfter(decision).isEmpty {
                RoundedRectangle(cornerRadius: BoardStyle.Radius.large)
                    .fill(BoardStyle.surface)
                    .overlay(RoundedRectangle(cornerRadius: BoardStyle.Radius.large).strokeBorder(BoardStyle.line))
                    .frame(height: cardHeight)
                    .scaleEffect(0.96 + progress * 0.04, anchor: .bottom)
                    .offset(y: 10 * (1 - progress))
                    .allowsHitTesting(false).accessibilityHidden(true)
            }
            DecisionCard(decision: decision, showsBot: true, showsUndo: false, replyFocus: $replyFocused, confirmingOptionId: confirmingChoice?.optionId, onRespond: clearUndo, onPreviewFrameChange: { previewFrame = $0; previewDecisionId = decision.id }, onChoose: { choice, optionId in
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
                // VoiceOver cannot swipe the card, so it gets the swipe's two actions.
                .accessibilityAction(named: "Later") { leaveByAction(decision, action: .later, width: size.width) }
                .accessibilityAction(named: "Dismiss") { leaveByAction(decision, action: .dismiss, width: size.width) }
        }
    }

    private func leaveByAction(_ decision: Decision, action: InboxSwipeAction, width: CGFloat) {
        guard canRespond, !exiting, !horizontalDrag, restoringDecision == nil, confirmingChoice == nil,
              !updatingIds.contains(decision.id) else { return }
        leave(decision, action: action, width: width, direction: action == .dismiss ? 1 : -1)
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
            .navigationTitle("Inbox").inlineNavigationTitle()
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { overviewOpen = false } } }
        }
    }

    private func decisionsAfter(_ decision: Decision) -> [Decision] {
        queue.ids.filter { $0 != decision.id }.compactMap { id in pending.first { $0.id == id } }
    }

    private func swipeLabel(for decision: Decision, width: CGFloat) -> some View {
        let action: InboxSwipeAction = offset > 0 ? .dismiss : .later
        return ZStack(alignment: offset < 0 ? .topTrailing : .topLeading) {
            if offset != 0 {
                let color = action == .dismiss ? BoardStyle.destructive : BoardStyle.ink
                RoundedRectangle(cornerRadius: BoardStyle.Radius.large).fill(color.opacity(min(abs(offset) / (width * 0.4), 0.85)))
                VStack(alignment: offset < 0 ? .trailing : .leading, spacing: 8) {
                    Image(systemName: action == .dismiss ? "xmark.circle" : "clock")
                        .font(.largeTitle)
                    Text(action == .dismiss ? "Dismiss" : "Later")
                        .font(.title2.weight(.bold)).fixedSize(horizontal: false, vertical: true)
                }
                .frame(maxWidth: swipeLabelWidth, alignment: offset < 0 ? .trailing : .leading)
                .foregroundStyle(action == .dismiss ? Color.white : BoardStyle.controlLabel).padding(24).opacity(min(abs(offset) / 60, 1))
            }
        }.allowsHitTesting(false).accessibilityHidden(true)
    }

    private func swipe(_ decision: Decision, width: CGFloat) -> some Gesture {
        DragGesture(minimumDistance: 18, coordinateSpace: .global)
            .updating($dragging) { _, active, _ in active = true }
            .onChanged { value in
                guard !exiting, restoringDecision == nil, canRespond, !replyFocused, !updatingIds.contains(decision.id) else { return }
                guard !(previewDecisionId == decision.id && previewFrame.contains(value.startLocation)), !verticalDrag else { return }
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
        Haptics.selection()
        let write = Task { await store.answer(decision, choice: choice, optionId: optionId) }
        Task {
            try? await Task.sleep(for: .milliseconds(340))
            withAnimation(.easeOut(duration: reduceMotion ? 0.1 : 0.2)) {
                // A saved answer can still be rejected. Keep the queue tied to pending state.
                queue.reconcile(pending, sort: sorting)
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
