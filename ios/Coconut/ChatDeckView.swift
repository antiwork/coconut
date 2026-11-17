import SwiftUI
#if os(macOS)
import AppKit
#endif

/// The deck: one card for each agent chat, in a row that scrolls sideways one card at a time. The card in the middle
/// is large and the cards at its sides are smaller and dim. A tap on a card zooms into its chat. A tap on the
/// background, or a pull down, closes the deck. The deck never archives or deletes by a gesture: the menu of a card
/// holds those actions, and Delete asks first.
struct ChatDeckView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let request: ChatDeckRequest
    /// Takes the deck off the screen. It runs when the deck has zoomed away.
    let dismiss: () -> Void

    /// The card in the middle.
    @State private var selected: String?
    /// The chats in the order that they had when the deck opened. A chat that stops working does not move away
    /// while the person looks at the cards.
    @State private var order: [String]?
    /// The chat that the deck keeps although it is archived: an archived chat that the person opened.
    @State private var kept: String?
    @State private var phase = Phase.hidden
    /// How far the person pulled the deck down.
    @State private var pull: CGFloat = 0
    @State private var deleting: Bot?
    #if os(macOS)
    @State private var monitor: Any?
    #endif

    /// `hidden` is the deck zoomed in to the screen and see-through, before it opens and after it closes. `opening` is
    /// the card in the middle zoomed in until it fills the screen, on its way to the chat.
    private enum Phase { case hidden, deck, opening }

    init(request: ChatDeckRequest, dismiss: @escaping () -> Void) {
        self.request = request
        self.dismiss = dismiss
        _selected = State(initialValue: request.start)
    }

    /// The cards: the chats in their fixed order, less the chats that left the list since.
    private var cards: [Bot] {
        guard let order else { return store.chatDeckCards(keeping: request.start) }
        let live = Dictionary(store.bots.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        return order.compactMap { id in
            guard let bot = live[id], !bot.archived || id == kept else { return nil }
            return bot
        }
    }

    private var openAnimation: Animation {
        reduceMotion ? .easeOut(duration: 0.15) : .spring(response: 0.42, dampingFraction: 0.82)
    }

    var body: some View {
        let cards = self.cards
        GeometryReader { proxy in
            let layout = ChatDeckLayout(container: proxy.size)
            ZStack {
                backdrop
                pager(cards, layout: layout)
                counter(cards, layout: layout)
            }
            .frame(width: proxy.size.width, height: proxy.size.height)
        }
        .ignoresSafeArea()
        #if os(iOS)
        .presentationBackground(.clear)
        #endif
        .modifier(DeleteBotDialog(bot: $deleting))
        .accessibilityAddTraits(.isModal)
        .accessibilityAction(.escape) { close() }
        .sensoryFeedback(.selection, trigger: selected)
        .onAppear { begin() }
        .onChange(of: cards.map(\.id)) { old, new in cardsChanged(from: old, to: new) }
        #if os(macOS)
        .onDisappear { removeKeys() }
        // The command Show all chats shows the deck, and it closes the deck while it shows, as Show Tab Overview does.
        .onReceive(NotificationCenter.default.publisher(for: .showChatDeck)) { _ in close() }
        #endif
    }

    // MARK: Layers

    /// The blurred screen under the cards. A tap on it closes the deck, and so does a pull down.
    private var backdrop: some View {
        ZStack {
            Rectangle().fill(.ultraThinMaterial)
            Color.black.opacity(0.3)
            // While a card zooms into its chat, the screen turns to the page, so the chat that opens under the deck
            // does not show through.
            BoardStyle.paper.opacity(phase == .opening ? 1 : 0)
        }
        .opacity(phase == .hidden ? 0 : 1)
        .contentShape(Rectangle())
        .onTapGesture { close() }
        .simultaneousGesture(pullDown)
        .accessibilityHidden(true)
    }

    private var pullDown: some Gesture {
        DragGesture(minimumDistance: 18)
            .onChanged { value in
                guard phase == .deck else { return }
                let down = value.translation.height
                pull = down > abs(value.translation.width) ? max(0, down) : 0
            }
            .onEnded { value in
                let down = value.translation.height
                let mostlyDown = down > abs(value.translation.width)
                let far = down > 90 || value.predictedEndTranslation.height > 260
                if mostlyDown && far { close() } else { withAnimation(.snappy) { pull = 0 } }
            }
    }

    /// The row of cards. It pages: a swipe ends with one card in the middle.
    private func pager(_ cards: [Bot], layout: ChatDeckLayout) -> some View {
        let calm = reduceMotion
        return ScrollViewReader { proxy in
            ScrollView(.horizontal) {
                LazyHStack(spacing: layout.spacing) {
                    ForEach(cards) { bot in
                        card(bot, layout: layout, calm: calm)
                    }
                }
                .scrollTargetLayout()
                .padding(.vertical, 40)
            }
            .scrollIndicators(.hidden)
            .scrollTargetBehavior(.viewAligned(limitBehavior: .alwaysByOne))
            .scrollPosition(id: $selected)
            .contentMargins(.horizontal, layout.sideInset, for: .scrollContent)
            .scrollClipDisabled()
            .frame(height: layout.card.height + 80)
            // A tap beside the cards closes the deck. A card takes its own tap.
            .onTapGesture { close() }
            .onAppear { centerSelected(proxy) }
            .task {
                await Task.yield()
                centerSelected(proxy)
            }
        }
        .scaleEffect(deckScale(layout))
        .offset(y: pull * 0.5)
        .opacity(phase == .hidden ? 0 : 1)
    }

    private func card(_ bot: Bot, layout: ChatDeckLayout, calm: Bool) -> some View {
        ChatDeckCard(bot: bot, layout: layout, tap: { tapped(bot.id) }, pullDown: { close() },
                     openChat: { zoom(into: $0) }, delete: { deleting = $0 })
            .containerRelativeFrame(.horizontal) { _, _ in layout.card.width }
            .frame(height: layout.card.height)
            .opacity(phase == .opening && bot.id != selected ? 0 : 1)
            .scrollTransition(axis: .horizontal) { content, transition in
                content
                    .scaleEffect(calm ? 1 : ChatDeckLayout.shrink(transition.value))
                    .opacity(ChatDeckLayout.light(transition.value))
            }
            .id(bot.id)
    }

    /// Where the person is in the deck, under the middle card.
    @ViewBuilder
    private func counter(_ cards: [Bot], layout: ChatDeckLayout) -> some View {
        if cards.count > 1, let selected, let index = cards.firstIndex(where: { $0.id == selected }) {
            Text("\(index + 1) of \(cards.count)")
                .font(.footnote.monospacedDigit())
                .foregroundStyle(.secondary)
                .padding(.horizontal, BoardStyle.Space.m).padding(.vertical, BoardStyle.Space.xs)
                .background(.ultraThinMaterial, in: Capsule())
                .offset(y: layout.card.height / 2 + 44)
                .opacity(phase == .deck ? 1 : 0)
                .allowsHitTesting(false)
                .accessibilityLabel("Chat \(index + 1) of \(cards.count)")
        }
    }

    /// The deck is zoomed in to fill the screen before it opens and after it closes, as the chat that it came from.
    private func deckScale(_ layout: ChatDeckLayout) -> CGFloat {
        if reduceMotion { return 1 }
        if phase == .deck { return 1 - min(pull / 1200, 0.08) }
        return layout.zoom
    }

    // MARK: Actions

    private func begin() {
        let ids = store.chatDeckCards(keeping: request.start).map(\.id)
        order = ids
        if let start = request.start, store.bots.first(where: { $0.id == start })?.archived == true { kept = start }
        // The pressed chat stays in the middle when it has a card. Else the first card comes.
        let start = request.start.flatMap { ids.contains($0) ? $0 : nil }
        selected = start ?? ids.first
        #if os(macOS)
        installKeys()
        #endif
        withAnimation(openAnimation) { phase = .deck }
    }

    private func centerSelected(_ proxy: ScrollViewProxy) {
        guard let selected else { return }
        proxy.scrollTo(selected, anchor: .center)
    }

    /// A card that left the list, for example an archived chat, leaves the deck. The card next to it moves in.
    private func cardsChanged(from old: [String], to new: [String]) {
        guard phase != .hidden else { return }
        if new.isEmpty {
            close()
            return
        }
        guard let selected, !new.contains(selected) else { return }
        let index = old.firstIndex(of: selected) ?? 0
        withAnimation(.snappy) { self.selected = new[min(index, new.count - 1)] }
    }

    /// A tap on a card. A card at the side comes to the middle first, then zooms in.
    private func tapped(_ id: String) {
        guard phase == .deck else { return }
        guard id == selected else {
            withAnimation(.snappy) { selected = id }
            Task {
                try? await Task.sleep(for: .milliseconds(300))
                if selected == id { zoom(into: id) }
            }
            return
        }
        zoom(into: id)
    }

    /// The card in the middle fills the screen, then the chat shows without a push, and the deck fades away.
    private func zoom(into id: String) {
        guard phase == .deck else { return }
        if request.opened == id {
            close()
            return
        }
        withAnimation(openAnimation) { phase = .opening }
        Task {
            try? await Task.sleep(for: .milliseconds(320))
            var transaction = Transaction()
            transaction.disablesAnimations = true
            withTransaction(transaction) { request.open(id) }
            withAnimation(.easeOut(duration: 0.2)) { phase = .hidden }
            try? await Task.sleep(for: .milliseconds(230))
            dismiss()
        }
    }

    private func close() {
        guard phase == .deck else { return }
        withAnimation(openAnimation) {
            phase = .hidden
            pull = 0
        }
        Task {
            try? await Task.sleep(for: .milliseconds(280))
            dismiss()
        }
    }

    // MARK: Keyboard

    #if os(macOS)
    /// The arrow keys move between the cards, Return opens the chat in the middle, and Escape closes the deck. The
    /// composer of the chat under the deck keeps the focus otherwise, so a monitor takes these keys first. The
    /// composer gives up the focus, so no typing goes into a field that the person cannot see.
    private func installKeys() {
        guard monitor == nil else { return }
        _ = NSApp.keyWindow?.makeFirstResponder(nil)
        monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
            let code = event.keyCode
            // A key with Command, Option or Control is a shortcut of the window, not a key of the deck.
            let plain = event.modifierFlags.intersection(.deviceIndependentFlagsMask).isEmpty
            var handled = false
            if plain { handled = MainActor.assumeIsolated { handle(key: code) } }
            return handled ? nil : event
        }
    }

    private func removeKeys() {
        if let monitor { NSEvent.removeMonitor(monitor) }
        monitor = nil
    }

    private func handle(key: UInt16) -> Bool {
        switch key {
        case 123: move(by: -1)
        case 124: move(by: 1)
        case 36, 76: if let selected { zoom(into: selected) }
        case 53: close()
        default: return false
        }
        return true
    }

    private func move(by step: Int) {
        let ids = cards.map(\.id)
        guard phase == .deck, let current = selected, let index = ids.firstIndex(of: current) else { return }
        let target = min(max(index + step, 0), ids.count - 1)
        guard target != index else { return }
        withAnimation(.snappy) { selected = ids[target] }
    }
    #endif
}

/// One card: a smaller copy of a chat, read only. The face of the agent, its name and the title of the chat, what it
/// does now, the conversation, and what the chat cost.
struct ChatDeckCard: View {
    @Environment(AppStore.self) private var store
    let bot: Bot
    let layout: ChatDeckLayout
    let tap: () -> Void
    let pullDown: () -> Void
    /// Shows another chat, for example the new chat that the menu started.
    let openChat: (String) -> Void
    let delete: (Bot) -> Void

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: ChatDeckLayout.radius, style: .continuous)
        face
            .frame(width: layout.content.width, height: layout.content.height, alignment: .top)
            .background(BoardStyle.paper)
            .scaleEffect(layout.scale, anchor: .topLeading)
            .frame(width: layout.card.width, height: layout.card.height, alignment: .topLeading)
            .clipShape(shape)
            .overlay { shape.strokeBorder(BoardStyle.line) }
            .shadow(color: .black.opacity(0.3), radius: 24, y: 12)
            .contentShape(shape)
            .onTapGesture(perform: tap)
            .accessibilityElement(children: .contain)
            .accessibilityLabel(bot.isChat ? "\(bot.name), \(bot.chatLabel)" : bot.name)
            .accessibilityAction(named: "Open chat") { tap() }
            .accessibilityIdentifier("chat-deck-card-\(bot.id)")
    }

    /// The chat at full size. The card scales it down.
    private var face: some View {
        VStack(spacing: 0) {
            header
            Rectangle().fill(BoardStyle.line).frame(height: 1)
            status
            ChatDeckTranscript(botId: bot.id)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            spend
        }
        .task(id: bot.id) { await store.loadChatUsage(bot.id) }
        // The cost of a chat changes when a turn ends.
        .onChange(of: store.conversationActivity(bot.id) == nil) { _, idle in
            if idle { Task { await store.loadChatUsage(bot.id) } }
        }
    }

    private var header: some View {
        HStack(spacing: BoardStyle.Space.m) {
            LiveBotAvatar(bot: bot, size: 40)
            VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                Text(bot.name).font(.headline).lineLimit(1)
                Text(subtitle).font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer(minLength: BoardStyle.Space.s)
            if bot.unreadCount > 0 { UnreadBadge(count: bot.unreadCount) }
            menu
        }
        .padding(.leading, BoardStyle.Space.l).padding(.trailing, BoardStyle.Space.xs)
        .padding(.top, BoardStyle.Space.l).padding(.bottom, BoardStyle.Space.s)
        .contentShape(Rectangle())
        // A pull down on the header closes the deck. The conversation under it scrolls instead.
        .simultaneousGesture(
            DragGesture(minimumDistance: 18).onEnded { value in
                if value.translation.height > 80 && value.translation.height > abs(value.translation.width) { pullDown() }
            }
        )
    }

    /// The title of a further chat, or what the agent does for its first chat.
    private var subtitle: String {
        if bot.isChat { return bot.chatLabel }
        let role = bot.role.trimmingCharacters(in: .whitespacesAndNewlines)
        return role.isEmpty ? "Chat" : role
    }

    /// What a row of the Chats list offered on a long press, before the long press showed the deck.
    private var menu: some View {
        Menu {
            NewChatButton(bot: bot) { chat in openChat(chat.id) }
            if !bot.isChat && !bot.coconut {
                item(bot.pinned ? "Unstar agent" : "Star agent", bot.pinned ? "star.slash" : "star") {
                    Task { await store.pinBot(bot, pinned: !bot.pinned) }
                }
            }
            if !bot.coconut {
                item(bot.isChat ? "Archive chat" : "Archive agent", "archivebox") {
                    Task { await store.archiveBot(bot, archived: true) }
                }
                item(bot.isChat ? "Delete chat" : "Delete agent", "trash", role: .destructive) { delete(bot) }
            }
        } label: {
            Image(systemName: "ellipsis")
                .font(.body.weight(.semibold))
                .foregroundStyle(.secondary)
                .frame(width: 44, height: 44)
                .contentShape(Rectangle())
        }
        .menuIndicator(.hidden)
        #if os(macOS)
        .menuStyle(.borderlessButton)
        #endif
        .accessibilityLabel("Actions of \(bot.name)")
        .accessibilityIdentifier("chat-deck-menu-\(bot.id)")
    }

    /// A menu item. A Mac menu has titles only, as the other menus of the Mac client. An iPhone menu shows the symbol too.
    @ViewBuilder
    private func item(_ title: String, _ symbol: String, role: ButtonRole? = nil, run: @escaping () -> Void) -> some View {
        #if os(macOS)
        Button(title, role: role, action: run)
        #else
        Button(title, systemImage: symbol, role: role, action: run)
        #endif
    }

    /// The working line of the chat: what the agent does now, with the seconds and the model.
    @ViewBuilder
    private var status: some View {
        if let activity = store.conversationActivity(bot.id) {
            BotStatusRow(bot: bot, activity: activity)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.vertical, BoardStyle.Space.xs)
                .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.small, style: .continuous))
                .padding(.horizontal, BoardStyle.Space.l).padding(.vertical, BoardStyle.Space.s)
        }
    }

    /// What the chat cost so far, as the last row of the chat shows it.
    @ViewBuilder
    private var spend: some View {
        if let usage = store.chatUsage[bot.id], usage.total.tokens > 0 {
            ChatSpendFooter(usage: usage)
                .allowsHitTesting(false)
                .padding(.bottom, BoardStyle.Space.s)
        }
    }
}

/// The conversation of a card as real message rows, newest at the bottom. It scrolls, so a look back through the day
/// is a drag up. A tap on it goes to the card. A chat shows its last rows. The rows before them come with a button, and
/// a card that holds few rows loads a page of older messages when it first shows.
struct ChatDeckTranscript: View {
    @Environment(AppStore.self) private var store
    let botId: String
    @State private var cache = DeckTranscriptCache()
    @State private var shown = ChatDeckTranscript.firstRows
    @State private var nearBottom = true

    static let firstRows = 30
    static let moreRows = 30
    private static let bottom = "deck-bottom"

    var body: some View {
        let entries = cache.current(in: store, botId: botId)
        let visible = entries.suffix(shown)
        ScrollViewReader { proxy in
            ScrollView {
                VStack(spacing: 0) {
                    if entries.count > shown || store.hasOlderMessages(botId) {
                        earlierButton(entries, proxy: proxy)
                    }
                    if entries.isEmpty {
                        Text("No messages yet.")
                            .font(.subheadline).foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity).padding(.vertical, 45)
                    }
                    ForEach(visible) { entry in row(entry) }
                    Color.clear.frame(height: 1).id(Self.bottom)
                }
                .padding(.horizontal, BoardStyle.Space.l)
                .padding(.bottom, BoardStyle.Space.s)
            }
            .defaultScrollAnchor(.bottom)
            .scrollBounceBehavior(.basedOnSize)
            .onScrollGeometryChange(for: Bool.self) { geometry in
                geometry.contentOffset.y + geometry.containerSize.height >= geometry.contentSize.height - 80
            } action: { _, near in
                nearBottom = near
            }
            // New rows follow the bottom while the person reads the bottom. A person who looks back stays where they are.
            .onChange(of: entries.last?.id) { _, _ in follow(proxy) }
            .onChange(of: entries.last?.message.text) { _, _ in follow(proxy) }
            .task(id: botId) { await fill() }
        }
    }

    private func row(_ entry: TranscriptEntry) -> some View {
        VStack(spacing: 0) {
            if entry.startsDay, let date = entry.message.createdAt.apiDate {
                DayPill(date: date, now: .now)
            }
            if entry.isExchange {
                BotExchangeRow(messages: entry.messages, botId: botId)
            } else {
                TranscriptMessageRow(message: entry.message, photos: entry.photos, photoMessages: entry.messages)
            }
        }
        .padding(.vertical, BoardStyle.Space.xs)
        .fixedSize(horizontal: false, vertical: true)
        // The card is read only: no row takes a tap, a hold or a swipe. The card takes the tap.
        .allowsHitTesting(false)
        .id(entry.id)
    }

    private func earlierButton(_ entries: [TranscriptEntry], proxy: ScrollViewProxy) -> some View {
        let loading = store.historyLoading.contains(botId)
        return Button {
            Task { await loadEarlier(entries, proxy: proxy) }
        } label: {
            Group {
                if loading { ProgressView() } else { Text("Load earlier messages").font(.footnote) }
            }
            .frame(maxWidth: .infinity, minHeight: 44)
        }
        .buttonStyle(.borderless)
        .disabled(loading)
    }

    private func follow(_ proxy: ScrollViewProxy) {
        guard nearBottom else { return }
        proxy.scrollTo(Self.bottom, anchor: .bottom)
    }

    /// Shows more rows. When the card shows every row that the app holds, a page of older messages comes from the Mac
    /// first. The row that was at the top stays there.
    private func loadEarlier(_ entries: [TranscriptEntry], proxy: ScrollViewProxy) async {
        let anchor = entries.suffix(shown).first?.id
        if entries.count > shown {
            shown += Self.moreRows
        } else if await store.loadOlderMessages(botId) {
            shown += Self.moreRows
        }
        guard let anchor else { return }
        await Task.yield()
        proxy.scrollTo(anchor, anchor: .top)
    }

    /// A card that comes into view with few rows loads older messages, so there is a day to look back through.
    private func fill() async {
        var pages = 0
        while pages < 3, !Task.isCancelled, cache.current(in: store, botId: botId).count < 24, store.hasOlderMessages(botId) {
            guard await store.loadOlderMessages(botId) else { return }
            pages += 1
        }
    }
}

/// The rows of a chat as the chat itself builds them. A redraw reuses them until the store changes what they read.
@MainActor
private final class DeckTranscriptCache {
    private struct Inputs: Equatable {
        let botId: String
        let messages: [Message]
        let outgoingMessages: [OutgoingMessage]
        let outgoingAnswers: [OutgoingAnswer]
        let decisions: [Decision]
        let artifacts: [Artifact]
        let tasks: [AgentTask]
        let calendar: Calendar
    }

    private var inputs: Inputs?
    private var entries: [TranscriptEntry] = []

    func current(in store: AppStore, botId: String) -> [TranscriptEntry] {
        let latest = Inputs(botId: botId, messages: store.messages, outgoingMessages: store.outgoingMessages,
                            outgoingAnswers: store.outgoingAnswers, decisions: store.decisions,
                            artifacts: store.transcriptArtifacts, tasks: store.tasks, calendar: .current)
        if latest != inputs {
            inputs = latest
            let messages = store.conversationTranscript(for: botId)
            entries = TranscriptEntry.grouped(messages, botId: botId, calendar: .current)
        }
        return entries
    }
}
