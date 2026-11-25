import SwiftUI

/// The deck of agent chats, as the multitasking cards of webOS and of the iPhone show apps: one card for each chat, in
/// a row that scrolls sideways. A long press on a chat row, or on the title of an open chat, shows it on the iPhone. A
/// pinch on a chat, the menu of a chat and Command-Shift-\ show it on the Mac.
enum ChatDeck {
    /// The chats of the deck, in the order of the cards: the chats that work first, then the newest activity first. An
    /// archived chat has no card, except the chat in `keeping`, and a chat that nobody wrote in has none.
    static func cards(_ bots: [Bot], latest: [String: Date], working: Set<String>, keeping: String? = nil) -> [Bot] {
        func activity(_ bot: Bot) -> Date { latest[bot.id] ?? bot.createdAt.apiDate ?? .distantPast }
        return bots.filter { AgentChats.isListed($0, latest: latest) || $0.id == keeping }.sorted { first, second in
            let firstWorks = working.contains(first.id)
            let secondWorks = working.contains(second.id)
            if firstWorks != secondWorks { return firstWorks }
            let firstDate = activity(first)
            let secondDate = activity(second)
            if firstDate != secondDate { return firstDate > secondDate }
            return first.id < second.id
        }
    }
}

extension AppStore {
    /// The chats of the deck now. The deck keeps this order while it is open.
    func chatDeckCards(keeping: String? = nil) -> [Bot] {
        let working = Set(bots.filter { isBotActive($0) }.map(\.id))
        return ChatDeck.cards(bots, latest: chatSummaries.newestActivity, working: working, keeping: keeping)
    }

    /// A chat works while its row says so: a working line, or a card that waits for the person. The Working filter of
    /// Chats and the grid of the working agents (ChatGridView) read it.
    func botWorks(_ bot: Bot) -> Bool { botPhase(bot) != nil || botNeedsYou(bot) }

    /// A row of a chat list works: an agent chat that works or whose card waits (botWorks), or a direct chat with a bot
    /// of a team while the bot types in it. Every other chat is idle, also a chat with people. A folded section of
    /// Chats on the iPhone and of the Mac sidebar keeps such a row in sight.
    func chatWorks(_ item: ChatListItem, at date: Date) -> Bool {
        switch item {
        case .bot(let bot): botWorks(bot)
        case .botDirect(_, let direct): !(direct.typing?.active(at: date).isEmpty ?? true)
        case .team, .direct: false
        }
    }

    /// The cards of the grid now: the chats of the deck that work, the chats with a working line first.
    func chatGridCards() -> [Bot] { chatDeckCards().filter(botWorks) }
}

/// A request to show the deck.
struct ChatDeckRequest: Identifiable {
    let id = UUID()
    /// The chat in the middle of the deck: the chat that the person pressed. Without one, the first card is.
    var start: String?
    /// The chat that is open under the deck. Its card closes the deck and opens nothing again.
    var opened: String?
    /// Shows the agents that work in a grid, all at once (ChatGridView), in place of the row of every chat.
    var grid = false
    /// Shows a chat that the person chose.
    let open: (String) -> Void
}

extension EnvironmentValues {
    /// Shows the deck of the chats. The tab view of the iPhone and the main window of the Mac set it.
    @Entry var showChatDeck: ((ChatDeckRequest) -> Void)? = nil
}

extension Notification.Name {
    /// The command Show all chats of the Mac menu asks the main window for the deck.
    static let showChatDeck = Notification.Name("coconut.showChatDeck")
}

/// The sizes of the deck. A card lays its chat out at `content` and shows it at `scale`: a smaller copy of the chat,
/// as the multitasking cards of the iPhone show an app.
struct ChatDeckLayout: Equatable {
    /// The size of the screen or of the window that holds the deck.
    let container: CGSize

    /// The corner radius of a card, as it shows.
    static let radius: CGFloat = 34

    /// The width of a card in a container of this width.
    static func cardWidth(in length: CGFloat) -> CGFloat {
        #if os(macOS)
        return min(max(length * 0.36, 380), 460)
        #else
        return min(length * 0.78, 460)
        #endif
    }

    /// The height of a card in a container of this height.
    static func cardHeight(in length: CGFloat) -> CGFloat {
        #if os(macOS)
        return max(320, length - 150)
        #else
        return min(length * 0.78, 760)
        #endif
    }

    /// How far a card scales down while it is away from the middle: 1 in the middle, less for each card beyond.
    static func shrink(_ distance: Double) -> CGFloat { CGFloat(1 - min(abs(distance), 1) * 0.1) }

    /// How much of a card shows while it is away from the middle. The side cards are dim.
    static func light(_ distance: Double) -> Double { 1 - min(abs(distance), 1) * 0.45 }

    /// The size of a card as it shows.
    var card: CGSize {
        CGSize(width: Self.cardWidth(in: container.width), height: Self.cardHeight(in: container.height))
    }

    /// The width at which a card lays its chat out. On the iPhone it is the width of the screen, so a line breaks
    /// where it breaks in the chat.
    var contentWidth: CGFloat {
        #if os(macOS)
        return 560
        #else
        return min(container.width, 520)
        #endif
    }

    /// The scale of the chat in a card.
    var scale: CGFloat { card.width / max(contentWidth, 1) }

    /// The size at which a card lays its chat out, before it scales the chat down.
    var content: CGSize { CGSize(width: contentWidth, height: card.height / max(scale, 0.01)) }

    /// The space between two cards.
    var spacing: CGFloat {
        #if os(macOS)
        return 28
        #else
        return 14
        #endif
    }

    /// The space at each end of the row, so the first and the last card can stand in the middle.
    var sideInset: CGFloat { max(0, (container.width - card.width) / 2) }

    /// How much the deck grows while it zooms into a chat, or out of one: the middle card then fills the container.
    var zoom: CGFloat {
        let fill = max(container.width / max(card.width, 1), container.height / max(card.height, 1))
        #if os(macOS)
        return min(fill, 1.6)
        #else
        return fill
        #endif
    }
}
