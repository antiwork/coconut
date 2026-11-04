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
}

enum HomeDestination: Hashable {
    case inbox
}
