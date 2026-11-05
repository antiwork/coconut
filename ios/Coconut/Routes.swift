import SwiftUI

// The places that a link, a card or a notification opens. The iPhone app and the Mac app share them.
struct ConversationRoute: Hashable {
    let botId: String
    var decisionId: String? = nil
    var messageId: String? = nil
    var workContext: WorkReference? = nil
}

struct BotSettingsRoute: Hashable {
    let botId: String
}

extension EnvironmentValues {
    /// Shows or hides the details of the bot next to its chat. Only the Mac window sets it. Without it, the bot's
    /// title opens its settings.
    @Entry var toggleBotDetails: (() -> Void)? = nil
    /// Pushes a route onto the navigation stack around a chat. A row of the chat list opens a page with it: in a list, a
    /// row with a navigation link gets a chevron of its own and did not open on a tap.
    @Entry var pushRoute: ((any Hashable) -> Void)? = nil
    /// Opens a document in the Library: its tab on the iPhone, its row in the sidebar on the Mac.
    @Entry var openDocument: ((String) -> Void)? = nil
    /// Opens the chat of a bot. Only the Mac window sets it. Without it, a page pushes the chat onto its stack.
    @Entry var openChat: ((String) -> Void)? = nil
}

enum HomeDestination: Hashable {
    case inbox
}
