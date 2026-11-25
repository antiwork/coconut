import SwiftUI

/// The model of every chat, and its reasoning effort. One Hermes profile runs them all. The person types `/model` or
/// `/effort` in the message box, or asks the Coconut chat (coconut_settings).
@MainActor
struct ModelChoice {
    let store: AppStore

    var catalog: ModelCatalog? { store.modelCatalog }
    var canEdit: Bool { store.connection == .connected || store.isDemo }
    var selection: ModelOverride? { catalog?.defaults.selection }
    var entry: ModelCatalogEntry? { selection.flatMap { catalog?.entry(for: $0) } }

    /// The line under the models of `/model` for an agent whose chats run on mote. The menu sets the Hermes default, which
    /// its other work uses unless its Hermes profile has a model of its own. Nil for an agent on Hermes.
    func chatNote(_ bot: Bot) -> String? {
        guard let chat = bot.chatModel else { return nil }
        return "Chats run on mote with \(catalog?.name(for: chat) ?? chat.model). The models here are for its other work."
    }

    func choose(_ model: ModelCatalogEntry, effort: String? = nil) {
        guard canEdit else { return }
        let selection = ModelOverride(model: model.id, provider: model.provider, reasoningEffort: effort)
        store.error = nil
        Task { _ = await store.setModel(selection) }
    }
}

/// The reasoning effort of a model, as a person reads it.
enum ModelPresentation {
    static func effortName(_ value: String?) -> String {
        guard let value else { return "Auto" }
        return value == "none" ? "Off" : value.capitalized
    }
}
