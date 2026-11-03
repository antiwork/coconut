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

enum HomeDestination: Hashable {
    case inbox, archived
}
