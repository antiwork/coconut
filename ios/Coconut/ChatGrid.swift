import SwiftUI
#if os(macOS)
import AppKit
#endif

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
/// chat that stops keeps its card, so no card moves while the person reads it. On the Mac, View > Show working agents
/// (Option-Command-\) shows it over the main window, and Escape or the same command closes it.
///
/// On the iPhone, two fingers on two cards pinch them together, and letting go merges the chats ("from the grid view
/// would be cool to pinch and merge two sessions together", 2026-11-26). The cards follow the fingers. Once they are
/// close enough, the card of the chat that stays takes a ring and the bar names it, and letting go slides the other
/// card under it. Only chats of one project merge (AppStore.mergeChats). VoiceOver merges with an action of a card.
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
    /// The two cards that a pinch draws together, while the fingers stay on the screen.
    @State private var pinch: Pinch?
    #if os(macOS)
    @State private var monitor: Any?
    #endif

    /// `hidden` is the grid see-through and a little larger, before it opens and after it closes. `opening` is a card
    /// zoomed in until it fills the screen, on its way to the chat.
    private enum Phase { case hidden, grid, opening }

    private struct Zoom: Equatable {
        let id: String
        let frame: CGRect
    }

    /// Two cards that a pinch draws together: `into` is the chat that stays (survivor), `from` the chat that goes into
    /// it. The fingers began at `start` and are at `center` now, and `scale` is how far they have closed, 1 at the
    /// start. `armed` is a pinch close enough that letting go merges the chats, and `merging` one that was let go.
    private struct Pinch: Equatable {
        let into: String
        let from: String
        /// Where the two cards stood when the pinch began.
        let intoFrame: CGRect
        let fromFrame: CGRect
        /// False for chats of two projects. Their cards follow the fingers, and the bar says why they do not merge.
        let merges: Bool
        let start: CGPoint
        var center: CGPoint
        var scale: CGFloat = 1
        var armed = false
        var merging = false
    }

    /// The scale of a pinch that arms it, and the scale over which it lets go of it again, so a hand that shakes at the
    /// edge does not flip it.
    private static let arms: CGFloat = 0.55
    private static let disarms: CGFloat = 0.62

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
                // The grid covers the notice of the app, so it shows a refusal of the Mac itself, as of a merge.
                .overlay(alignment: .top) {
                    if let error = store.error {
                        ErrorBanner(message: error) { store.error = nil }
                            .padding(.horizontal, ChatGridLayout.margin)
                            .transition(.opacity)
                    }
                }
                .animation(.easeOut(duration: 0.2), value: store.error)
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
        #if DEBUG && targetEnvironment(simulator)
        // A task of its own, which no redraw of the cover cancels in the middle of the hold.
        .onAppear { Task { await holdPreviewPinch() } }
        #endif
        #if os(macOS)
        .onDisappear { removeKeys() }
        // A second Show working agents closes the grid, as the deck closes on a second Show all agents.
        .onReceive(NotificationCenter.default.publisher(for: .showChatGrid)) { _ in close() }
        #endif
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
                ForEach(cards) { bot in card(bot, in: cards, layout: layout) }
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
        .scrollClipDisabled()
        .scrollDisabled(phase != .grid || pinch != nil)
        #if os(iOS)
        .gesture(ChatGridPinch(began: beginPinch, changed: movePinch, ended: endPinch))
        // A light tap when the cards are close enough to merge.
        .sensoryFeedback(.impact(weight: .light), trigger: pinch?.armed ?? false) { old, new in new && !old }
        // The grid bounces also when its cards fit, so a pull down past the top can close it. The Mac leaves this
        // out: a swipe of a trackpad past the top would close the grid by mistake, and Escape closes it there.
        .scrollBounceBehavior(.always)
        .onScrollGeometryChange(for: Bool.self) { geometry in
            geometry.contentOffset.y + geometry.contentInsets.top < -90
        } action: { _, pulled in
            if pulled { close() }
        }
        #endif
    }

    private func card(_ bot: Bot, in cards: [Bot], layout: ChatGridLayout) -> some View {
        let moved = moved(bot.id)
        return ChatGridCard(bot: bot, layout: layout)
            .overlay {
                RoundedRectangle(cornerRadius: ChatGridLayout.radius, style: .continuous)
                    .strokeBorder(BoardStyle.control, lineWidth: 2)
                    .opacity(pinch?.into == bot.id && pinch?.armed == true ? 1 : 0)
            }
            .onGeometryChange(for: CGRect.self) { $0.frame(in: .global) } action: { frames.value[bot.id] = $0 }
            .scaleEffect(moved.scale)
            .offset(moved.offset)
            .opacity(moved.opacity)
            .zIndex(moved.z)
            // Once a card zooms, the others stay away until the grid has gone.
            .opacity(zoom != nil && zoom?.id != bot.id ? 0 : 1)
            .onTapGesture { open(bot.id) }
            .accessibilityElement(children: .combine)
            .accessibilityAddTraits(.isButton)
            .accessibilityAction { open(bot.id) }
            .accessibilityActions {
                ForEach(cards.filter { $0.id != bot.id && $0.agentId == bot.agentId }) { other in
                    Button("Merge with “\(Self.name(other))”") { merge(bot, other) }
                }
            }
            .accessibilityIdentifier("chat-grid-card-" + bot.id)
    }

    /// Where a pinch puts a card. Its two cards follow the fingers as if the fingers held the grid, so each card stays
    /// under its finger, and they shrink a little as they close in. The chat that stays is on top. Letting go slides
    /// the other card under it, where it fades.
    private func moved(_ id: String) -> (offset: CGSize, scale: CGFloat, opacity: Double, z: Double) {
        guard let pinch, id == pinch.into || id == pinch.from else { return (.zero, 1, 1, 0) }
        let stays = id == pinch.into
        let frame = stays ? pinch.intoFrame : pinch.fromFrame
        if pinch.merging {
            if stays { return (.zero, 1, 1, 2) }
            return (CGSize(width: pinch.intoFrame.midX - frame.midX, height: pinch.intoFrame.midY - frame.midY), 0.2, 0, 1)
        }
        let s = pinch.scale
        let offset = CGSize(width: pinch.center.x - pinch.start.x + (s - 1) * (frame.midX - pinch.start.x),
                            height: pinch.center.y - pinch.start.y + (s - 1) * (frame.midY - pinch.start.y))
        let closed = min(max((1 - s) / (1 - Self.arms), 0), 1)
        return (offset, !stays && pinch.armed ? 0.9 : 1 - 0.06 * closed, 1, stays ? 2 : 1)
    }

    /// The count of the agents that work, in the middle, and Done at the side, as the tab overview of Safari has it.
    /// During a pinch, Done steps aside for what letting go does.
    private func bar(_ cards: [Bot]) -> some View {
        HStack {
            Spacer(minLength: 0)
            Button("Done") { close() }
                .buttonStyle(CoconutButtonStyle())
                .opacity(pinch == nil ? 1 : 0)
                .accessibilityIdentifier("chat-grid-done")
        }
        .overlay {
            if !cards.isEmpty {
                Text(barText(cards))
                    .font(.footnote.monospacedDigit())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .padding(.horizontal, BoardStyle.Space.m).padding(.vertical, BoardStyle.Space.xs)
                    .background(.ultraThinMaterial, in: Capsule())
                    .accessibilityIdentifier("chat-grid-count")
            }
        }
        .padding(.horizontal, ChatGridLayout.margin)
        .frame(height: ChatGridLayout.bar)
        .opacity(phase == .grid ? 1 : 0)
    }

    /// What the bar says: the chat that letting go of a pinch merges into, why two cards do not merge, or the count.
    private func barText(_ cards: [Bot]) -> String {
        if let pinch {
            if pinch.armed, let into = cards.first(where: { $0.id == pinch.into }) { return "Merge into “\(Self.name(into))”" }
            if !pinch.merges, pinch.scale <= Self.arms { return "Only chats of the same project can merge" }
        }
        return Self.summary(working: cards.filter { store.botPhase($0) != nil }.count,
                            waiting: cards.filter { store.botPhase($0) == nil && store.botNeedsYou($0) }.count)
    }

    /// The name of a card in what the grid says: the title of a further chat, else the name of the agent.
    static func name(_ bot: Bot) -> String { bot.isChat ? bot.chatLabel : bot.name }

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
        #if os(macOS)
        installKeys()
        #endif
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
        guard phase == .grid, pinch == nil else { return }
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

    // MARK: Merge

    /// A pinch begins with a finger on each of two cards. Any other pinch is not the grid's: it returns false.
    private func beginPinch(_ first: CGPoint, _ second: CGPoint) -> Bool {
        guard phase == .grid, pinch == nil, zoom == nil else { return false }
        let cards = self.cards
        func card(at point: CGPoint) -> Bot? { cards.first { frames.value[$0.id]?.contains(point) == true } }
        guard let a = card(at: first), let b = card(at: second), a.id != b.id else { return false }
        let (into, from) = Self.survivor(a, b)
        guard let intoFrame = frames.value[into.id], let fromFrame = frames.value[from.id] else { return false }
        let start = CGPoint(x: (first.x + second.x) / 2, y: (first.y + second.y) / 2)
        pinch = Pinch(into: into.id, from: from.id, intoFrame: intoFrame, fromFrame: fromFrame,
                      merges: a.agentId == b.agentId, start: start, center: start)
        return true
    }

    private func movePinch(_ center: CGPoint, _ scale: CGFloat) {
        guard var moved = pinch, !moved.merging else { return }
        moved.center = center
        moved.scale = min(max(scale, 0.15), 1.5)
        let armed = moved.merges && moved.scale <= (moved.armed ? Self.disarms : Self.arms)
        guard armed != moved.armed else {
            pinch = moved
            return
        }
        moved.armed = armed
        withAnimation(.snappy(duration: 0.2)) { pinch = moved }
    }

    /// Letting go of an armed pinch merges the chats. Any other end puts the cards back.
    private func endPinch(_ released: Bool) {
        guard let ended = pinch, !ended.merging else { return }
        guard released, ended.armed, let into = store.bots.first(where: { $0.id == ended.into }),
              let from = store.bots.first(where: { $0.id == ended.from }) else {
            withAnimation(.spring(response: 0.35, dampingFraction: 0.82)) { pinch = nil }
            return
        }
        withAnimation(.easeIn(duration: 0.22)) { pinch?.merging = true }
        Task {
            try? await Task.sleep(for: .milliseconds(240))
            withAnimation(.snappy) {
                store.mergeChats(into: into, from: from)
                pinch = nil
            }
        }
    }

    /// Merges the chats of two cards at once, for VoiceOver, which has no pinch.
    private func merge(_ a: Bot, _ b: Bot) {
        let (into, from) = Self.survivor(a, b)
        withAnimation(.snappy) { store.mergeChats(into: into, from: from) }
    }

    /// The chat that stays when two cards merge, and the chat that goes into it. A project stays, as the Mac keeps it.
    /// Of two chats, the older one stays and the newer one joins it.
    static func survivor(_ a: Bot, _ b: Bot) -> (into: Bot, from: Bot) {
        if !a.isChat { return (a, b) }
        if !b.isChat { return (b, a) }
        let first = a.createdAt.apiDate ?? .distantPast
        let second = b.createdAt.apiDate ?? .distantPast
        return second < first ? (b, a) : (a, b)
    }

    #if DEBUG && targetEnvironment(simulator)
    /// `--demo --open grid-merge` holds a pinch of the first two cards of one project, then lets go of it. A simulator
    /// takes no pinch from a terminal, so the hand check of the merge starts the app on it (ChatDeckPresenter).
    private func holdPreviewPinch() async {
        let arguments = ProcessInfo.processInfo.arguments
        guard store.isDemo, let index = arguments.firstIndex(of: "--open"), arguments.indices.contains(index + 1),
              arguments[index + 1] == "grid-merge" else { return }
        try? await Task.sleep(for: .seconds(0.8))
        let cards = self.cards
        let pairs = cards.indices.flatMap { i in cards[(i + 1)...].map { (cards[i], $0) } }
        guard let (a, b) = pairs.first(where: { $0.0.agentId == $0.1.agentId }),
              let first = frames.value[a.id], let second = frames.value[b.id],
              beginPinch(CGPoint(x: first.midX, y: first.midY), CGPoint(x: second.midX, y: second.midY)),
              let start = pinch?.start else { return }
        movePinch(start, 0.5)
        try? await Task.sleep(for: .seconds(3))
        endPinch(true)
    }
    #endif

    // MARK: Keyboard

    #if os(macOS)
    /// Escape closes the grid. The message box of the chat under the grid keeps the focus otherwise, so a monitor takes
    /// the key first, as in the deck. The message box gives up the focus, so no typing goes into a field that the
    /// person cannot see.
    private func installKeys() {
        guard monitor == nil else { return }
        _ = NSApp.keyWindow?.makeFirstResponder(nil)
        monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
            // A key with Command, Option or Control is a shortcut of the window, not a key of the grid.
            let plain = event.modifierFlags.intersection(.deviceIndependentFlagsMask).isEmpty
            guard plain, event.keyCode == 53 else { return event }
            MainActor.assumeIsolated { close() }
            return nil
        }
    }

    private func removeKeys() {
        if let monitor { NSEvent.removeMonitor(monitor) }
        monitor = nil
    }
    #endif
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

/// The filter at the top right of Chats, one menu of what Chats shows, as the filter of the sidebar of Slack: All
/// chats, Unreads or Working, one at a time. Working is the person's request of 2026-11-24 ("Ability to show working
/// only from chats top right"): Chats lists only the chats that work or whose card waits for the person
/// (AppStore.botWorks), in one list without the rows of the sections, and its title says Working. Unreads is the
/// person's request of 2026-11-27 ("Starred and only show unread option. Mirror slack almost exactly"), the "Unreads
/// only" of Slack along with everything the person starred: Chats shows Starred with all its chats and Unread, and
/// no other row, and its title says Unreads. The icon fills while a filter is on. The phone keeps the choice, each
/// filter in its key, and Working wins while both are on, because the Working link of a widget sets its key alone.
struct ChatsFilterMenu: View {
    enum Choice: Hashable { case all, unreads, working }
    static let workingKey = "chats-working-only"
    static let unreadsKey = "chats-unreads-only"
    @Binding var working: Bool
    @Binding var unreads: Bool

    var body: some View {
        let choice = Binding<Choice> {
            working ? .working : unreads ? .unreads : .all
        } set: { choice in
            withAnimation(.snappy) {
                working = choice == .working
                unreads = choice == .unreads
            }
        }
        Menu {
            Picker("Show", selection: choice) {
                Text("All chats").tag(Choice.all)
                Text("Unreads").tag(Choice.unreads)
                Text("Working").tag(Choice.working)
            }
        } label: {
            Label("Filter chats", systemImage: choice.wrappedValue == .all
                  ? "line.3.horizontal.decrease.circle" : "line.3.horizontal.decrease.circle.fill")
        }
        .accessibilityIdentifier("chats-filter")
    }
}
