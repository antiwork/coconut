import SwiftUI

/// The Inbox on a Mac: the waiting cards in a list, and the chosen card next to it. The iPhone shows one card at a
/// time and swipes it away; a Mac window has room for the whole queue.
struct MacInboxView: View {
    @Environment(AppStore.self) private var store
    @Binding var selection: MacSelection?
    @State private var chosenId: String?
    // What the card opened: its chat or an earlier answer. A new card from the Mac keeps it, so an open chat keeps its
    // unsent message. A card that the person picks goes back to the card.
    @State private var path = NavigationPath()
    // The last card that the X dismissed. Undo restores it while the Mac keeps its restore window open.
    @State private var dismissed: Dismissal?

    private struct Dismissal {
        let receipt: Decision
        let restoreKey = UUID().uuidString
        let at = Date()
    }

    // An answered card leaves at once. The app holds the answer for a few seconds, and Undo in the toolbar takes it back.
    private var pending: [Decision] {
        store.pendingDecisions.filter { !store.answeringDecisionIds.contains($0.id) }.sorted { ($0.createdAt.apiDate ?? .distantPast) > ($1.createdAt.apiDate ?? .distantPast) }
    }
    private var chosen: Decision? { pending.first { $0.id == chosenId } ?? pending.first }

    var body: some View {
        HStack(spacing: 0) {
            List(pending, selection: $chosenId) { decision in
                MacInboxRow(decision: decision).tag(decision.id)
            }
            .listStyle(.inset)
            .frame(minWidth: 220, idealWidth: 280, maxWidth: 320)
            .overlay {
                if pending.isEmpty {
                    QuietEmptyState(symbol: "tray", title: "Nothing waits for you",
                                    message: "Cards from your bots show here.")
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
                                .padding(24)
                                .frame(maxWidth: .infinity)
                        }
                        .toolbar {
                            ToolbarItem(placement: .primaryAction) {
                                Button { selection = .bot(chosen.botId) } label: {
                                    Label("Open chat", systemImage: "bubble.left.and.bubble.right")
                                }
                                .keyboardShortcut("o", modifiers: .command)
                                .help("Open the chat of this card")
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
            }
            .frame(minWidth: 300, maxWidth: .infinity, maxHeight: .infinity)
            .background(BoardStyle.paper)
        }
        .navigationTitle("Inbox")
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                TimelineView(.periodic(from: .now, by: 1)) { context in
                    // Undo takes back the newest action: a dismissal, or an answer that the app still holds.
                    let waiting = store.latestWaitingAnswer(at: context.date)
                    if let dismissed, let deadline = dismissed.receipt.inboxUndoUntil?.apiDate, deadline > context.date,
                       dismissed.at > (waiting?.createdAt.apiDate ?? .distantPast) {
                        Button { restore(dismissed) } label: { Label("Undo", systemImage: "arrow.uturn.backward") }
                            .keyboardShortcut("z", modifiers: .command)
                            .help("Bring back the dismissed card \(dismissed.receipt.title)")
                    } else if let waiting {
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
        HStack(alignment: .top, spacing: 10) {
            if let bot { LiveBotAvatar(bot: bot, size: 22) }
            VStack(alignment: .leading, spacing: 2) {
                Text(decision.title).font(.body.weight(.medium)).lineLimit(2)
                HStack(spacing: 4) {
                    Text(bot?.name ?? "Bot")
                    if let date = decision.createdAt.apiDate {
                        Text("·")
                        CompactAgeLabel(date: date)
                    }
                }
                .font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 4)
    }
}
