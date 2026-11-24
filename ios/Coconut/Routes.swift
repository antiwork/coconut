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

/// The Team chat of a team, or a direct chat with one person of a team.
struct TeamChatRoute: Hashable {
    let chat: TeamChatKey
}

/// The Team screen of one team. The title of its chat opens it.
struct TeamScreenRoute: Hashable {
    let roomId: String
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
    /// Starts or leaves the demo: preview data with no Mac. Only the iPhone app sets it.
    @Entry var setDemo: ((Bool) -> Void)? = nil
    /// Shows a chat of a team: Home on the iPhone, the main window on the Mac. The Team screen and the name
    /// above a message open a direct chat with it.
    @Entry var showTeamChat: ((TeamChatKey) -> Void)? = nil
    /// Shows the Team screen of a team by its room ID. Only the Mac window sets it: the title of a Team chat opens
    /// the screen with it. An iPhone pushes the screen onto the stack of the chat.
    @Entry var showTeamScreen: ((String) -> Void)? = nil
}

enum HomeDestination: Hashable {
    /// The cards that wait for an answer. A card row of Home and a notification of a card open it.
    case inbox
}

/// The notice board of a team, by its room ID.
struct NoticeBoardRoute: Hashable {
    let roomId: String
}

/// The list of all agents, with the pages, the live work and the spend. The project menu of Home opens it.
struct AgentListRoute: Hashable {}
