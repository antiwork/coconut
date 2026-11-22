import SwiftUI
#if os(iOS)
import UIKit
#endif

/// Shows the deck over the screen and takes it away. The tab view of the iPhone and the main window of the Mac wear it.
/// A view that wants the deck reads `showChatDeck` from the environment.
struct ChatDeckPresenter: ViewModifier {
    /// The chat that the window shows now, for the command of the Mac menu. Only the Mac window has one.
    var current: String? = nil
    /// Shows a chat that the person chose, for the command of the Mac menu. Only the Mac window has one.
    var open: ((String) -> Void)? = nil
    @State private var request: ChatDeckRequest?
    #if DEBUG && targetEnvironment(simulator)
    @Environment(AppStore.self) private var store
    #endif

    func body(content: Content) -> some View {
        #if os(iOS)
        content
            .environment(\.showChatDeck) { present($0) }
            // A cover with no color of its own, so the screen shows blurred under the deck.
            .fullScreenCover(item: $request) { shown in
                ChatDeckView(request: shown) { dismiss() }
            }
            .sensoryFeedback(trigger: request?.id, feedback)
            #if DEBUG && targetEnvironment(simulator)
            .task { await openForPreview() }
            #endif
        #else
        content
            .environment(\.showChatDeck) { present($0) }
            .overlay {
                if let request {
                    ChatDeckView(request: request) { dismiss() }
                        .id(request.id)
                }
            }
            .onReceive(NotificationCenter.default.publisher(for: .showChatDeck)) { _ in
                guard let open else { return }
                present(ChatDeckRequest(start: current, opened: current, open: open))
            }
        #endif
    }

    /// A light tap when the deck opens. Closing it has none.
    private func feedback(_ old: UUID?, _ new: UUID?) -> SensoryFeedback? {
        new == nil ? nil : .impact(weight: .light)
    }

    /// The deck shows at once, without the slide of a cover. It zooms by itself.
    private func present(_ shown: ChatDeckRequest) {
        guard request == nil else { return }
        #if os(iOS)
        // The keyboard of a message box would cover the cards.
        UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
        #endif
        var transaction = Transaction()
        transaction.disablesAnimations = true
        withTransaction(transaction) { request = shown }
    }

    private func dismiss() {
        var transaction = Transaction()
        transaction.disablesAnimations = true
        withTransaction(transaction) { request = nil }
    }

    #if DEBUG && targetEnvironment(simulator)
    /// `--demo --open deck` shows the deck at launch with the first card in the middle, and `--open deck-2` with the
    /// third. A simulator takes no taps from a terminal, so the hand check of the deck starts the app on it.
    private func openForPreview() async {
        let arguments = ProcessInfo.processInfo.arguments
        guard store.isDemo, let index = arguments.firstIndex(of: "--open"), index + 1 < arguments.count,
              arguments[index + 1].hasPrefix("deck") else { return }
        try? await Task.sleep(for: .seconds(1))
        let ids = store.chatDeckCards().map(\.id)
        let place = Int(arguments[index + 1].dropFirst(5)) ?? 0
        present(ChatDeckRequest(start: ids.indices.contains(place) ? ids[place] : ids.first, opened: nil) { _ in })
    }
    #endif
}

/// A chat row that opens its chat on a tap and shows the deck on a long press. A button cannot tell the two apart: it
/// opens the chat when the finger lifts after the hold. So the row takes both gestures itself, and a hold never opens
/// the chat.
struct ChatDeckRow: ViewModifier {
    let botId: String
    /// Opens the chat on a tap.
    let open: () -> Void
    @Environment(\.showChatDeck) private var showChatDeck
    @Environment(\.openChat) private var openChat
    @Environment(\.pushRoute) private var pushRoute
    @State private var pressed = false

    func body(content: Content) -> some View {
        content
            .opacity(pressed ? 0.55 : 1)
            .contentShape(Rectangle())
            .onTapGesture(perform: open)
            .onLongPressGesture(minimumDuration: 0.4, maximumDistance: 16, perform: { show() },
                                onPressingChanged: { pressing in pressed = pressing })
            .accessibilityAddTraits(.isButton)
            // VoiceOver has no long press, so the deck is an action of the row.
            .accessibilityAction(named: "Show all agents") { show() }
    }

    private func show() {
        showChatDeck?(ChatDeckRequest(start: botId, opened: nil) { id in
            if let openChat { openChat(id) } else { pushRoute?(ConversationRoute(botId: id)) }
        })
    }
}

extension View {
    /// A row that opens a chat. On the iPhone a long press on it shows the deck of all chats (ChatDeckRow). On the Mac
    /// the row stays a button: the Mac shows the deck with a pinch, with the menu of a chat and with a shortcut, and a
    /// click that holds must not open it.
    @ViewBuilder
    func chatRowButton(_ identifier: String, botId: String, action: @escaping () -> Void) -> some View {
        #if os(iOS)
        modifier(ChatDeckRow(botId: botId, open: action)).accessibilityIdentifier(identifier)
        #else
        boardButton(identifier, action: action)
        #endif
    }
}

#if os(macOS)
/// The View menu entry that shows all chats, with the shortcut that Safari has for its tab overview.
struct ChatDeckCommands: Commands {
    var body: some Commands {
        CommandGroup(after: .sidebar) {
            Button("Show all agents") {
                NotificationCenter.default.post(name: .showChatDeck, object: nil)
            }
            .keyboardShortcut("\\", modifiers: [.command, .shift])
        }
    }
}

/// A pinch in on a chat shows the deck, as a pinch in on the page of a browser shows its tabs.
struct ChatDeckPinch: ViewModifier {
    let chat: String
    /// Shows a chat that the person chose.
    let open: (String) -> Void
    @Environment(\.showChatDeck) private var showChatDeck
    @State private var fired = false

    func body(content: Content) -> some View {
        content.simultaneousGesture(
            MagnifyGesture()
                .onChanged { value in
                    guard !fired, value.magnification < 0.8 else { return }
                    fired = true
                    showChatDeck?(ChatDeckRequest(start: chat, opened: chat, open: open))
                }
                .onEnded { _ in fired = false }
        )
    }
}
#endif
