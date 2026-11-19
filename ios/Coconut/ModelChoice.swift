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
