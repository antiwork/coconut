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

    private var pending: [Decision] {
        store.pendingDecisions.sorted { ($0.createdAt.apiDate ?? .distantPast) > ($1.createdAt.apiDate ?? .distantPast) }
    }
    private var chosen: Decision? { pending.first { $0.id == chosenId } ?? pending.first }

    var body: some View {
        HStack(spacing: 0) {
            List(pending, selection: $chosenId) { decision in
                MacInboxRow(decision: decision).tag(decision.id)
            }
            .listStyle(.inset)
            .frame(width: 300)
            .overlay {
                if pending.isEmpty {
                    QuietEmptyState(symbol: "tray", title: "Nothing waits for you",
                                    message: "Cards from your bots show here.")
                }
            }
            Divider()
            Group {
                if let chosen {
                    // The card links to its chat and to an earlier answer. Those open in this column.
                    NavigationStack(path: $path) {
                        ScrollView {
                            DecisionCard(decision: chosen, showsBot: true)
                                .id(chosen.id)
                                .frame(maxWidth: 560)
                                .padding(24)
                                .frame(maxWidth: .infinity)
                        }
                        .navigationDestination(for: ConversationRoute.self) { route in
                            ConversationView(botId: route.botId, focusDecisionId: route.decisionId,
                                             focusMessageId: route.messageId, attachedWork: route.workContext)
                        }
                        .navigationDestination(for: BotExchangeRoute.self) { BotExchangeView(route: $0) }
                        .navigationDestination(for: BotSettingsRoute.self) { BotSettingsView(botId: $0.botId) }
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
            .frame(minWidth: 360, maxWidth: .infinity, maxHeight: .infinity)
            .background(BoardStyle.paper)
        }
        .navigationTitle("Inbox")
        .onChange(of: chosenId) { _, _ in path = NavigationPath() }
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
