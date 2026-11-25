import SwiftUI

/// The sizes of the grid of the agents that work (ChatGridView). Each card has the row of its chat, as Chats shows it,
/// over a smaller copy of the chat. The cards fill the screen when they fit, so every agent that works shows at once. A
/// grid with more cards than fit scrolls.
struct ChatGridLayout: Equatable {
    /// The size of the area that holds the cards: the screen or the window, less the bars of the system.
    let container: CGSize
    /// How many cards the grid has.
    let count: Int

    /// The corner radius of a card.
    static let radius: CGFloat = 22
    static let spacing = BoardStyle.Space.m
    static let margin = BoardStyle.Space.l
    /// The height of the row of a card over its copy of the chat (ChatGridLabel), with the line under it.
    static let label: CGFloat = 58
    /// The height of the bar under the cards: the count of the agents that work, and Done.
    static let bar: CGFloat = 52
    #if os(macOS)
    static let narrowest: CGFloat = 260
    #else
    /// The narrowest card. Two cards fit side by side on every iPhone.
    static let narrowest: CGFloat = 150
    #endif
    static let widest: CGFloat = 520
    /// The lowest copy of a chat. A grid whose copies would be lower scrolls.
    static let lowest: CGFloat = 150
    /// A copy of a chat is at most this much higher than wide: about the shape of a phone, as a multitasking card.
    static let tallest: CGFloat = 2
    /// The largest scale of a copy. A wider card lays its chat out wider instead of showing larger text.
    static let largest: CGFloat = 0.7
    /// The narrowest width at which a copy lays its chat out: the narrowest iPhone, so a line breaks about where it
    /// breaks on a phone and the text stays legible at the scale of a card.
    static let narrowestContent: CGFloat = 320

    /// The width and the height that the cards share: the container less its margins and the bar.
    private var area: CGSize {
        CGSize(width: max(0, container.width - 2 * Self.margin),
               height: max(0, container.height - Self.bar - 2 * Self.margin))
    }

    /// As many columns as cards of the narrowest width fit, and never more than there are cards.
    var columns: Int {
        let fit = Int((area.width + Self.spacing) / (Self.narrowest + Self.spacing))
        return max(1, min(fit, count))
    }

    var rows: Int { max(1, (count + columns - 1) / columns) }

    /// The width of a card.
    var width: CGFloat {
        let columns = CGFloat(self.columns)
        return min(Self.widest, max(1, (area.width - (columns - 1) * Self.spacing) / columns))
    }

    /// The height of the copy of a chat: its share of the height of the screen, between the lowest copy and the shape
    /// of a phone.
    var thumbnail: CGFloat {
        let rows = CGFloat(self.rows)
        let share = (area.height - (rows - 1) * Self.spacing) / rows - Self.label
        return min(max(share, Self.lowest), width * Self.tallest)
    }

    /// The width at which a copy lays its chat out.
    var contentWidth: CGFloat { max(Self.narrowestContent, width / Self.largest) }

    /// The scale of the chat in a copy.
    var scale: CGFloat { width / max(contentWidth, 1) }

    /// The height at which a copy lays its chat out, before it scales the chat down.
    var contentHeight: CGFloat { thumbnail / max(scale, 0.01) }

    /// The height between the margins above and under the cards. Cards that fit stand in its middle.
    var visible: CGFloat { area.height }
}

/// The grid of the agents that work: a card for each agent chat that works or whose card waits for the person, all at
/// once, as the tab overview of Safari shows the tabs. The person asked for it at the top right of Chats ("go to the
/// multitasking view which makes it easy in a grid to see all agents working at once", 2026-11-24). Each card has the
/// row of its chat, as Chats shows it, over a smaller copy of the chat that follows its newest messages and its working
/// line live. A tap on a card zooms into its chat, as a card of the deck does. Done, a tap beside the cards, or a pull
/// down closes the grid. The grid keeps its order while it shows: a chat that starts to work joins at the end, and a
/// chat that stops keeps its card, so no card moves while the person reads it.
struct ChatGridView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let request: ChatDeckRequest
    /// Takes the grid off the screen. It runs when the grid has faded away.
    let dismiss: () -> Void

    /// The chats of the cards: their order when the grid opened, then the chats that joined since.
    @State private var order: [String]?
    @State private var phase = Phase.hidden
    /// The card that zooms into its chat, and where it stood.
    @State private var zoom: Zoom?
    /// Where each card stands on the screen. A scroll moves them, so a plain class holds them without a redraw.
    @State private var frames = ChatGridFrames()
    /// The screen, and the area of the cards on it.
    @State private var screen = CGRect.zero
    @State private var area = CGRect.zero

    /// `hidden` is the grid see-through and a little larger, before it opens and after it closes. `opening` is a card
    /// zoomed in until it fills the screen, on its way to the chat.
    private enum Phase { case hidden, grid, opening }

    private struct Zoom: Equatable {
        let id: String
        let frame: CGRect
    }

    private var openAnimation: Animation {
        reduceMotion ? .easeOut(duration: 0.15) : .spring(response: 0.42, dampingFraction: 0.82)
    }

    /// The cards: the chats in their order, less the chats that left the list since, archived or deleted.
    private var cards: [Bot] {
        let ids = order ?? store.chatGridCards().map(\.id)
        let live = Dictionary(store.bots.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        return ids.compactMap { id in live[id].flatMap { $0.archived ? nil : $0 } }
    }

    var body: some View {
        let cards = self.cards
        let transform = self.transform
        ZStack {
            backdrop
            // The area of the cards, measured outside the zoom: a frame inside it would grow with the zoom.
            Color.clear
                .onGeometryChange(for: CGRect.self) { $0.frame(in: .global) } action: { area = $0 }
                .allowsHitTesting(false)
            GeometryReader { proxy in
                let layout = ChatGridLayout(container: proxy.size, count: cards.count)
                ZStack(alignment: .bottom) {
                    if cards.isEmpty {
                        empty.frame(maxWidth: .infinity, maxHeight: .infinity)
                    } else {
                        grid(cards, layout: layout)
                    }
                    bar(cards)
                }
            }
            .scaleEffect(transform.scale, anchor: transform.anchor)
            .offset(transform.offset)
            .opacity(phase == .hidden ? 0 : 1)
        }
        #if os(iOS)
        .presentationBackground(.clear)
        #endif
        .accessibilityAddTraits(.isModal)
        .accessibilityAction(.escape) { close() }
        .onAppear { begin() }
        .onChange(of: store.chatGridCards().map(\.id)) { _, ids in join(ids) }
    }

    // MARK: Layers

    /// The blurred screen under the cards. While a card zooms into its chat, it turns to the page, so the chat that
    /// opens under the grid does not show through. It measures the whole screen: its content extends past the bars of
    /// the system, while the frame of a view that ignores the safe area stays inside them.
    private var backdrop: some View {
        ZStack {
            Rectangle().fill(.ultraThinMaterial)
            Color.black.opacity(0.3)
            BoardStyle.paper.opacity(zoom == nil ? 0 : 1)
        }
        .onGeometryChange(for: CGRect.self) { $0.frame(in: .global) } action: { screen = $0 }
        .opacity(phase == .hidden ? 0 : 1)
        .ignoresSafeArea()
        .contentShape(Rectangle())
        .onTapGesture { close() }
        .accessibilityHidden(true)
    }

    private func grid(_ cards: [Bot], layout: ChatGridLayout) -> some View {
        let columns = Array(repeating: GridItem(.fixed(layout.width), spacing: ChatGridLayout.spacing, alignment: .top),
                            count: layout.columns)
        return ScrollView {
            LazyVGrid(columns: columns, spacing: ChatGridLayout.spacing) {
                ForEach(cards) { bot in card(bot, layout: layout) }
            }
            // Cards that fit stand in the middle, between the top of the screen and the bar.
            .frame(maxWidth: .infinity, minHeight: layout.visible)
            .padding(.vertical, ChatGridLayout.margin)
            .padding(.bottom, ChatGridLayout.bar)
            .contentShape(Rectangle())
            // A tap beside the cards closes the grid. A card takes its own tap.
            .onTapGesture { close() }
        }
        .scrollIndicators(.hidden)
        // The grid bounces also when its cards fit, so a pull down past the top can close it.
        .scrollBounceBehavior(.always)
        .scrollClipDisabled()
        .scrollDisabled(phase != .grid)
        .onScrollGeometryChange(for: Bool.self) { geometry in
            geometry.contentOffset.y + geometry.contentInsets.top < -90
        } action: { _, pulled in
            if pulled { close() }
        }
    }

    private func card(_ bot: Bot, layout: ChatGridLayout) -> some View {
        ChatGridCard(bot: bot, layout: layout)
            .onGeometryChange(for: CGRect.self) { $0.frame(in: .global) } action: { frames.value[bot.id] = $0 }
            // Once a card zooms, the others stay away until the grid has gone.
            .opacity(zoom != nil && zoom?.id != bot.id ? 0 : 1)
            .onTapGesture { open(bot.id) }
            .accessibilityElement(children: .combine)
            .accessibilityAddTraits(.isButton)
            .accessibilityAction { open(bot.id) }
            .accessibilityIdentifier("chat-grid-card-" + bot.id)
    }

    /// The count of the agents that work, in the middle, and Done at the side, as the tab overview of Safari has it.
    private func bar(_ cards: [Bot]) -> some View {
        HStack {
            Spacer(minLength: 0)
            Button("Done") { close() }
                .buttonStyle(CoconutButtonStyle())
                .accessibilityIdentifier("chat-grid-done")
        }
        .overlay {
            if !cards.isEmpty {
                Text(Self.summary(working: cards.filter { store.botPhase($0) != nil }.count,
                                  waiting: cards.filter { store.botPhase($0) == nil && store.botNeedsYou($0) }.count))
                    .font(.footnote.monospacedDigit())
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, BoardStyle.Space.m).padding(.vertical, BoardStyle.Space.xs)
                    .background(.ultraThinMaterial, in: Capsule())
                    .accessibilityIdentifier("chat-grid-count")
            }
        }
        .padding(.horizontal, ChatGridLayout.margin)
        .frame(height: ChatGridLayout.bar)
        .opacity(phase == .grid ? 1 : 0)
    }

    /// "3 working", "3 working · 1 needs you", "2 need you", "None working".
    static func summary(working: Int, waiting: Int) -> String {
        var parts: [String] = []
        if working > 0 { parts.append("\(working) working") }
        if waiting > 0 { parts.append(waiting == 1 ? "1 needs you" : "\(waiting) need you") }
        return parts.isEmpty ? "None working" : parts.joined(separator: " · ")
    }

    private var empty: some View {
        Text("No agent is working.")
            .font(.headline)
            .foregroundStyle(.secondary)
            .padding(.horizontal, BoardStyle.Space.l).padding(.vertical, BoardStyle.Space.m)
            .background(.ultraThinMaterial, in: Capsule())
            .allowsHitTesting(false)
            .accessibilityIdentifier("chat-grid-empty")
    }

    /// The zoom of the grid: none while it shows, a little larger while it fades in or out, and while a card opens, so
    /// large that the card fills the screen.
    private var transform: (scale: CGFloat, anchor: UnitPoint, offset: CGSize) {
        if reduceMotion { return (1, .center, .zero) }
        if let zoom, area.width > 0, area.height > 0, zoom.frame.width > 0, zoom.frame.height > 0 {
            let frame = zoom.frame
            let scale = max(screen.width / frame.width, screen.height / frame.height)
            let anchor = UnitPoint(x: (frame.midX - area.minX) / area.width, y: (frame.midY - area.minY) / area.height)
            return (scale, anchor, CGSize(width: screen.midX - frame.midX, height: screen.midY - frame.midY))
        }
        return (phase == .grid ? 1 : 1.06, .center, .zero)
    }

    // MARK: Actions

    private func begin() {
        order = store.chatGridCards().map(\.id)
        withAnimation(openAnimation) { phase = .grid }
    }

    /// A chat that starts to work while the grid shows joins at the end.
    private func join(_ ids: [String]) {
        guard let order, phase == .grid else { return }
        let joined = ids.filter { !order.contains($0) }
        guard !joined.isEmpty else { return }
        withAnimation(.snappy) { self.order = order + joined }
    }

    /// The card fills the screen, then the chat shows without a push, and the grid fades away.
    private func open(_ id: String) {
        guard phase == .grid else { return }
        if request.opened == id {
            close()
            return
        }
        let frame = frames.value[id] ?? .zero
        withAnimation(openAnimation) {
            zoom = Zoom(id: id, frame: frame)
            phase = .opening
        }
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
        guard phase == .grid else { return }
        withAnimation(openAnimation) { phase = .hidden }
        Task {
            try? await Task.sleep(for: .milliseconds(280))
            dismiss()
        }
    }
}

/// Where the cards of the grid stand on the screen. A plain class, so a scroll that moves them redraws nothing.
@MainActor
final class ChatGridFrames {
    var value: [String: CGRect] = [:]
}

/// One card of the grid: the name of the chat and what its agent does, over a smaller copy of the chat. The card is
/// read only. The grid takes its tap.
struct ChatGridCard: View {
    let bot: Bot
    let layout: ChatGridLayout

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: ChatGridLayout.radius, style: .continuous)
        VStack(spacing: 0) {
            ChatGridLabel(bot: bot)
                .padding(.horizontal, BoardStyle.Space.m)
                .frame(minHeight: ChatGridLayout.label - 1)
            Rectangle().fill(BoardStyle.line).frame(height: 1)
            ChatGridTranscript(bot: bot)
                .frame(width: layout.contentWidth, height: layout.contentHeight, alignment: .bottom)
                .scaleEffect(layout.scale, anchor: .topLeading)
                .frame(width: layout.width, height: layout.thumbnail, alignment: .topLeading)
                .clipped()
                .accessibilityHidden(true)
        }
        .frame(width: layout.width)
        .background(BoardStyle.paper)
        .clipShape(shape)
        .overlay { shape.strokeBorder(BoardStyle.line) }
        .shadow(color: .black.opacity(0.25), radius: 16, y: 8)
        .contentShape(shape)
    }
}

/// The top of a card: what the row of the chat in Chats says, in two lines that fit a narrow card. The name of the chat
/// with the time of the step that its agent does now (else the age of its newest message), and under it the step, a
/// card that waits, or the newest message once the agent stops, across the whole width.
struct ChatGridLabel: View {
    @Environment(AppStore.self) private var store
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let bot: Bot

    var body: some View {
        let subtitle = BotRowSubtitle(needsYou: store.botNeedsYou(bot), phase: store.botPhase(bot), health: bot.health)
        let step: Date? = if case .phase = subtitle { store.botStepStart(bot) } else { nil }
        let newest = store.chatSummaries[bot.id].newestDate
        VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
            HStack(alignment: .firstTextBaseline, spacing: BoardStyle.Space.s) {
                Text(bot.isChat ? bot.listName : bot.name)
                    .font(.subheadline.weight(.semibold))
                    .lineLimit(1)
                Spacer(minLength: 0)
                Group {
                    if let step {
                        StepTimeLabel(start: step)
                    } else if let newest {
                        CompactAgeLabel(date: newest)
                    }
                }
                .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                .fixedSize()
            }
            HStack(spacing: BoardStyle.Space.xs) {
                Group {
                    switch subtitle {
                    case .needsYou:
                        StatusLabel(status: "needs_you")
                    case .phase(let phase):
                        ShimmerText(text: phase.label)
                    case .health(let health):
                        Text(health.label).foregroundStyle(BoardStyle.attention)
                    case .preview:
                        Text(BotContactRow.preview(of: bot, in: store)).foregroundStyle(.secondary)
                    }
                }
                .transition(.opacity)
                Spacer(minLength: 0)
                if !bot.notifications { MutedMark() }
                if BotChatMute.unread(bot) > 0 { UnreadBadge(count: bot.unreadCount) }
            }
            .font(.footnote)
            .lineLimit(1)
            .animation(BoardStyle.fade(reduceMotion: reduceMotion), value: subtitle)
        }
        .padding(.vertical, BoardStyle.Space.s)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(subtitle.accessibilityLabel(bot: bot, preview: BotContactRow.preview(of: bot, in: store),
                                                        queued: store.queuedCount(bot.id)))
    }
}

/// The copy of a chat in a card of the grid: its newest rows, as the chat shows them, and its working line at the
/// bottom. It follows the chat live, and older rows go off the top of the card.
struct ChatGridTranscript: View {
    @Environment(AppStore.self) private var store
    let bot: Bot
    @State private var cache = DeckTranscriptCache()

    /// The rows that a copy lays out: more than the highest card shows.
    static let rows = 12

    var body: some View {
        let entries = cache.current(in: store, botId: bot.id).suffix(Self.rows)
        VStack(spacing: 0) {
            if entries.isEmpty {
                Text("No messages yet.")
                    .font(.subheadline).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity).padding(.vertical, 45)
            }
            ForEach(entries) { entry in row(entry) }
            if let activity = store.conversationActivity(bot.id) {
                BotStatusRow(bot: bot, activity: activity)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(.horizontal, BoardStyle.Space.l)
        .padding(.bottom, BoardStyle.Space.m)
        .fixedSize(horizontal: false, vertical: true)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottom)
        .background(BoardStyle.paper)
        .allowsHitTesting(false)
        .task(id: bot.id) { await fill() }
    }

    private func row(_ entry: TranscriptEntry) -> some View {
        VStack(spacing: 0) {
            if entry.startsDay, let date = entry.message.createdAt.apiDate {
                DayPill(date: date, now: .now)
            }
            if entry.isExchange {
                BotExchangeRow(messages: entry.messages, botId: bot.id)
            } else {
                TranscriptMessageRow(message: entry.message, photos: entry.photos, photoMessages: entry.messages)
            }
        }
        .padding(.vertical, BoardStyle.Space.xs)
        .fixedSize(horizontal: false, vertical: true)
    }

    /// A chat whose rows the app does not hold yet loads one page of older messages, so its card shows the chat.
    private func fill() async {
        guard cache.current(in: store, botId: bot.id).count < 4, store.hasOlderMessages(bot.id) else { return }
        _ = await store.loadOlderMessages(bot.id)
    }
}

/// The button at the top right of Chats that shows the grid of the agents that work (ChatGridView).
struct ChatGridButton: View {
    @Environment(\.showChatDeck) private var showChatDeck
    @Environment(\.openChat) private var openChat
    @Environment(\.pushRoute) private var pushRoute

    var body: some View {
        Button {
            showChatDeck?(ChatDeckRequest(start: nil, opened: nil, grid: true) { id in
                if let openChat { openChat(id) } else { pushRoute?(ConversationRoute(botId: id)) }
            })
        } label: {
            Label("Show working agents", systemImage: "square.grid.2x2")
        }
        .accessibilityIdentifier("chats-grid")
    }
}

/// The filter at the top right of Chats (the person: "Ability to show working only from chats top right",
/// 2026-11-24). On, Chats lists only the chats that work or whose card waits for the person (AppStore.botWorks), in
/// one list without the titles of the sections, and its title says Working. The phone keeps the choice.
struct WorkingFilterButton: View {
    static let key = "chats-working-only"
    @Binding var on: Bool

    var body: some View {
        Button {
            withAnimation(.snappy) { on.toggle() }
        } label: {
            Label(on ? "Show all chats" : "Show working only",
                  systemImage: on ? "line.3.horizontal.decrease.circle.fill" : "line.3.horizontal.decrease.circle")
        }
        .accessibilityIdentifier("chats-working-filter")
    }
}
