import AppIntents
import Observation

/// The place that a widget, the Live Activity, a control or a shortcut asked the app to open. CoconutHome opens it and
/// clears it. A link of a widget arrives through `onOpenURL`; an intent sets it in the process of the app.
@MainActor @Observable
final class HomeScreenLinks {
    static let shared = HomeScreenLinks()
    var pending: HomeScreenLink?
}

/// New agent, from Control Center, the Lock Screen, the Action button, Spotlight or Shortcuts: Coconut opens on the
/// message box of Chats with the keyboard up, ready for a typed or a dictated request. It saves the taps to the app,
/// to Chats and into the box. The system runs it in the app, which it opens first, so the app and the widgets both
/// compile it.
struct NewAgentIntent: AppIntent {
    static var title: LocalizedStringResource { "New agent" }
    static var description: IntentDescription {
        IntentDescription("Opens the message box of Coconut with the keyboard up, to type or dictate a request.")
    }
    static var openAppWhenRun: Bool { true }

    @MainActor
    func perform() async throws -> some IntentResult {
        HomeScreenLinks.shared.pending = .newAgent
        return .result()
    }
}
