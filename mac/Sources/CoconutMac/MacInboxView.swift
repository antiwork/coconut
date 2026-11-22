import SwiftUI

/// The cards that wait, on a Mac: the cards in a list, and the chosen card next to it. A card row of Home opens it. The iPhone shows one card at a
/// time and swipes it away; a Mac window has room for the whole queue.
struct MacInboxView: View {
    @Environment(AppStore.self) private var store
    @Binding var selection: MacSelection?
    @State private var chosenId: String?

    /// `first` is the card that shows first: the card of the row of Home that opened the list.
    init(selection: Binding<MacSelection?>, first: String? = nil) {
        _selection = selection
        _chosenId = State(initialValue: first)
    }
    // The cards whose rows are on screen. A rebuilt list scrolls back to the top one (see the list below).
    @State private var shownIds: Set<String> = []
    // What the card opened: its chat or an earlier answer. A new card from the Mac keeps it, so an open chat keeps its
    // unsent message. A card that the person picks goes back to the card.
    @State private var path = NavigationPath()
    // The last card that the X dismissed. Undo restores it while the Mac keeps its restore window open.
    @State private var dismissed: Dismissal?
    // The time at which the view checks whether an undo waits. A task moves it to each deadline (see undoDeadline).
    @State private var undoClock = Date()

    private struct Dismissal {
        let receipt: Decision
        let restoreKey = UUID().uuidString
        let at = Date()
    }

    private enum UndoAction {
        case dismissal(Dismissal)
        case answer(OutgoingAnswer)
    }

    /// What Undo takes back: the newest action, a dismissal or an answer that the app still holds. Nil when no undo
    /// waits.
    private var undoAction: UndoAction? {
        let waiting = store.latestWaitingAnswer(at: undoClock)
        if let dismissed, let deadline = dismissed.receipt.inboxUndoUntil?.apiDate, deadline > undoClock,
           dismissed.at > (waiting?.createdAt.apiDate ?? .distantPast) {
            return .dismissal(dismissed)
        }
        return waiting.map(UndoAction.answer)
    }

    /// The next time when Undo can change: the end of the restore window of the dismissal, or the time when the app
    /// sends the waiting answer.
    private var undoDeadline: Date? {
        [dismissed?.receipt.inboxUndoUntil?.apiDate, store.latestWaitingAnswer(at: undoClock)?.sendAt]
            .compactMap { $0 }.filter { $0 > undoClock }.min()
    }

    // An answered card leaves at once. The app holds the answer for a few seconds, and Undo in the toolbar takes it back.
    private var pending: [Decision] {
        store.pendingDecisions.filter { !store.answeringDecisionIds.contains($0.id) }.sorted { ($0.createdAt.apiDate ?? .distantPast) > ($1.createdAt.apiDate ?? .distantPast) }
    }
    private var chosen: Decision? { pending.first { $0.id == chosenId } ?? pending.first }

    var body: some View {
        HStack(spacing: 0) {
            ScrollViewReader { proxy in
                List(pending, selection: $chosenId) { decision in
                    MacInboxRow(decision: decision).tag(decision.id)
                        .onAppear { shownIds.insert(decision.id) }
                        .onDisappear { shownIds.remove(decision.id) }
                }
                .listStyle(.inset)
                // The Mac list gave a card that arrived while it showed the height of a one-line row and never measured
                // it again, so its title was cut off with the bot name under it. A new set of cards builds the list
                // again, and the list measures every row. The chosen card stays, because chosenId lives outside the list.
                .id(pending.map(\.id))
                .onChange(of: pending.map(\.id)) { before, after in
                    // A new list starts at its top. Scroll back to the top row on screen that stays. A person at the top
                    // stays at the top, where a new card shows.
                    guard let place = before.first(where: { shownIds.contains($0) && after.contains($0) }),
                          place != before.first else { return }
                    DispatchQueue.main.async { proxy.scrollTo(place, anchor: .top) }
                }
            }
            .frame(minWidth: 220, idealWidth: 280, maxWidth: 320)
            .overlay {
                if pending.isEmpty {
                    QuietEmptyState(symbol: "tray", title: "Nothing waits for you",
                                    message: "Cards from your agents show here.")
                }
            }
            Divider()
            // The card links to its chat and to an earlier answer. Those open in this column. The stack stays when the
            // list empties, so a chat that a card opened keeps its unsent message; only its root changes.
            NavigationStack(path: $path) {
                Group {
                    if let chosen {
                        ScrollView {
                            DecisionCard(decision: chosen, showsBot: true, showsUndo: false,
                                         onDismissed: { dismissed = Dismissal(receipt: $0) })
                                .id(chosen.id)
                                .frame(maxWidth: 560)
                                // The content margin of each pane of the window.
                                .padding(BoardStyle.Space.l)
                                .frame(maxWidth: .infinity)
                        }
                        .toolbar {
                            ToolbarItem(placement: .primaryAction) {
                                Button { selection = .bot(chosen.botId) } label: {
                                    Label("Open agent", systemImage: "bubble.left.and.bubble.right")
                                }
                                .keyboardShortcut("o", modifiers: .command)
                                .help("Open the agent of this card")
                            }
                        }
                    } else {
                        Color.clear
                    }
                }
                .navigationDestination(for: ConversationRoute.self) { route in
                    ConversationView(botId: route.botId, focusDecisionId: route.decisionId,
                                     focusMessageId: route.messageId, attachedWork: route.workContext)
                }
                .navigationDestination(for: BotExchangeRoute.self) { BotExchangeView(route: $0) }
                .navigationDestination(for: BotSettingsRoute.self) { BotSettingsView(botId: $0.botId) }
                .modifier(InAppDestinations())
            }
            .stackRoutes { route in path.append(route) }
            .frame(minWidth: 300, maxWidth: .infinity, maxHeight: .infinity)
            .background(BoardStyle.paper)
        }
        .navigationTitle("Cards")
        .toolbar {
            // The cards are a page of Home. The sidebar keeps Home marked, so this button leads back.
            ToolbarItem(placement: .navigation) {
                Button { selection = .home } label: { Label("Home", systemImage: "chevron.left") }
                    .help("Back to Home")
                    .accessibilityIdentifier("cards-back")
            }
            // The toolbar has the Undo item only while an undo waits. An item without content still took a slot in the
            // glass capsule of "Open chat", so that capsule was two slots wide with its symbol in the right half.
            if let undoAction {
                ToolbarItem(placement: .primaryAction) {
                    switch undoAction {
                    case .dismissal(let dismissed):
                        Button { restore(dismissed) } label: { Label("Undo", systemImage: "arrow.uturn.backward") }
                            .keyboardShortcut("z", modifiers: .command)
                            .help("Bring back the dismissed card \(dismissed.receipt.title)")
                    case .answer(let waiting):
                        Button {
                            store.undoAnswer(decisionId: waiting.decisionId)
                            chosenId = waiting.decisionId
                        } label: { Label("Undo", systemImage: "arrow.uturn.backward") }
                            .keyboardShortcut("z", modifiers: .command)
                            .help("Undo the answer \(waiting.text)")
                    }
                }
            }
        }
        // The task sleeps until the next deadline and then moves the clock, so Undo leaves on time. A new dismissal or
        // a new answer gives a new deadline, and the task starts again.
        .task(id: undoDeadline) {
            undoClock = Date()
            guard let deadline = undoDeadline else { return }
            do { try await Task.sleep(for: .seconds(max(0, deadline.timeIntervalSinceNow))) } catch { return }
            undoClock = Date()
        }
        .onChange(of: chosenId) { _, _ in path = NavigationPath() }
    }

    /// Restores a dismissed card, as Undo after a swipe does on the iPhone. A retry uses the same key.
    private func restore(_ dismissed: Dismissal) {
        Task {
            let result = await store.updateInbox(dismissed.receipt, action: "restore", idempotencyKey: dismissed.restoreKey)
            // A newer dismissal can arrive while this one restores. It keeps its own Undo.
            let current = self.dismissed?.restoreKey == dismissed.restoreKey
            if result.decision != nil {
                if current { self.dismissed = nil }
                chosenId = dismissed.receipt.id
            } else if result.isUncertain {
                store.error = "Undo is not confirmed. Reconnect and try Undo again."
            } else if current {
                self.dismissed = nil
            }
        }
    }
}

private struct MacInboxRow: View {
    @Environment(AppStore.self) private var store
    let decision: Decision
    private var bot: Bot? { store.bots.first { $0.id == decision.botId } }

    var body: some View {
        HStack(alignment: .top, spacing: BoardStyle.Space.m) {
            VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                Text(decision.title).font(.body).lineLimit(2)
                HStack(spacing: BoardStyle.Space.xs) {
                    Text(bot?.name ?? "Agent")
                    if let date = decision.createdAt.apiDate {
                        Text("·")
                        CompactAgeLabel(date: date)
                    }
                }
                .font(.subheadline.monospacedDigit()).foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, BoardStyle.Space.xs)
    }
}
