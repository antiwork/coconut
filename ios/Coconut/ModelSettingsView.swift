import SwiftUI

/// The model of every chat. One Hermes profile runs all chats, so they share it.
struct ModelSettingsView: View {
    @Environment(AppStore.self) private var store
    @ShownError private var shownError
    @State private var selectionKey = ""
    @State private var effort: String?
    @State private var saves = ModelSettingsSaves()
    @State private var loaded = false
    @State private var refreshed = false
    @State private var edited = false
    private var saving: Bool { saves.inFlight != nil }

    private var catalog: ModelCatalog? { store.modelCatalog }
    private var current: ModelOverride? { catalog?.defaults.selection }
    private var selectedModel: ModelCatalogEntry? { catalog?.models.first { $0.selectionKey == selectionKey } }
    private var selection: ModelOverride? {
        selectedModel.map { ModelOverride(model: $0.id, provider: $0.provider, reasoningEffort: effort) }
    }
    private var canEdit: Bool { store.connection == .connected || store.isDemo }
    private var valid: Bool { selection.map { catalog?.contains($0) == true } ?? false }

    var body: some View {
        Form {
            if let catalog {
                if catalog.models.isEmpty || current.map({ catalog.entry(for: $0) == nil }) == true {
                    Section {
                        if let current {
                            LabeledContent("Model", value: current.model.isEmpty ? "Not configured" : catalog.name(for: current))
                                .accessibilityIdentifier("current-model")
                            LabeledContent("Provider", value: current.provider)
                                .accessibilityIdentifier("current-model-provider")
                            LabeledContent("Effort", value: ModelPresentation.effortName(catalog.resolved(current).reasoningEffort))
                        }
                        if catalog.loading == true {
                            ProgressView("Loading models")
                        } else if catalog.models.isEmpty {
                            Text("No model choices are available from Hermes.").foregroundStyle(.secondary)
                        }
                    } header: {
                        Text("Hermes default")
                    }
                }
                if !catalog.models.isEmpty {
                    Section {
                        Picker("Model", selection: Binding(get: { selectionKey }, set: { chooseModel($0) })) {
                            if selectedModel == nil { Text("Choose a model").tag("") }
                            ModelChoices(models: catalog.models)
                        }
                        .pagePickerStyle()
                        .accessibilityIdentifier("model-picker")
                        if let selectedModel, !selectedModel.reasoningEfforts.isEmpty {
                            Picker("Effort", selection: Binding(get: { effort }, set: { effort = $0; edited = true; save() })) {
                                Text("Model default · " + ModelPresentation.effortName(selectedModel.defaultReasoningEffort))
                                    .tag(String?.none)
                                ForEach(selectedModel.reasoningEfforts, id: \.self) { value in
                                    Text(ModelPresentation.effortName(value)).tag(Optional(value))
                                }
                            }
                            .pagePickerStyle()
                            .accessibilityIdentifier("model-effort-picker")
                        } else {
                            LabeledContent("Effort", value: ModelPresentation.effortName(selectedModel?.defaultReasoningEffort))
                        }
                    } header: {
                        Text("Model")
                    } footer: {
                        Text("Every chat uses this model. Changes save when you make them.")
                    }
                    // The picker stays usable while a choice saves, so it does not grey out and back at each change.
                    .disabled(!canEdit)
                }
            } else {
                Section {
                    if store.connection == .connecting {
                        ProgressView("Loading models")
                    } else {
                        Text(store.isDemo ? "This preview has no model catalog." : canEdit ? "The Mac has not provided a model catalog." : "Connect to your Mac to load models.")
                            .foregroundStyle(.secondary)
                    }
                }
            }
            FallbackSection()
            if !canEdit {
                Section {} footer: { Text("Connect to your Mac first.") }
            }
            if let issue = catalog?.defaults.unavailableReason {
                Section { InlineError(issue) }
                    .accessibilityIdentifier("current-model-error")
            }
            if let error = shownError {
                Section { InlineError(error) }
            }
        }
        .settingsBackground()
        .navigationTitle("Model").inlineNavigationTitle()
        .toolbar {
            ToolbarItem(placement: .confirmationAction) {
                if saving { ProgressView().accessibilityLabel("Saving model settings") }
            }
        }
        .onAppear { if !loaded { load() } }
        .task {
            guard !refreshed else { return }
            refreshed = true
            await store.refreshSettings()
            if !loaded || !edited { load() }
        }
        .onChange(of: store.modelCatalog) { _, _ in
            if !loaded || (!edited && !saving) { load() }
        }
    }

    private func chooseModel(_ key: String) {
        guard key != selectionKey else { return }
        selectionKey = key
        effort = nil
        edited = true
        save()
    }

    private func load() {
        guard let catalog else { return }
        saves = ModelSettingsSaves(saved: ModelSettingsChoice(selection: current))
        selectionKey = current.flatMap { catalog.entry(for: $0)?.selectionKey } ?? ""
        effort = current?.reasoningEffort
        loaded = true
        edited = false
    }

    // Each change saves when it is valid. A model without a choice waits until the person picks one. A change during a
    // save waits for it, so the last choice wins.
    private func save() {
        guard loaded, canEdit, valid else { return }
        guard let submitted = saves.begin(ModelSettingsChoice(selection: selection)) else {
            if !saving { edited = false }
            return
        }
        edited = true
        store.error = nil
        Task {
            var accepted = false
            if let selection = submitted.selection { accepted = await store.setModel(selection) }
            if saves.finish(accepted: accepted) {
                save()
            } else if accepted {
                edited = false
            } else {
                // A failed save returns the page to the saved choice, so choosing the same model again retries it.
                load()
            }
        }
    }
}

struct ModelSettingsChoice: Equatable {
    let selection: ModelOverride?
}

/// Serializes saves while the picker stays usable. A return to the saved choice during a write still queues a save:
/// the write in flight can replace that choice before it finishes.
struct ModelSettingsSaves {
    var saved: ModelSettingsChoice?
    private(set) var inFlight: ModelSettingsChoice?
    private var pending = false

    init(saved: ModelSettingsChoice? = nil) { self.saved = saved }

    mutating func begin(_ choice: ModelSettingsChoice) -> ModelSettingsChoice? {
        if inFlight != nil { pending = true; return nil }
        guard choice != saved else { return nil }
        inFlight = choice
        return choice
    }

    /// A later choice gets its turn even if the earlier save failed.
    mutating func finish(accepted: Bool) -> Bool {
        if accepted { saved = inFlight }
        inFlight = nil
        defer { pending = false }
        return pending
    }
}

enum ModelPresentation {
    static func effortName(_ value: String?) -> String {
        guard let value else { return "Auto" }
        return value == "none" ? "Off" : value.capitalized
    }
}

/// The models of a picker. A Mac menu shows one line for each model under a header for its provider; a menu item
/// of two lines made the menu and its closed button tall and uneven. The iPhone list shows the provider under the name.
private struct ModelChoices: View {
    let models: [ModelCatalogEntry]

    var body: some View {
        #if os(macOS)
        let providers = models.reduce(into: [String]()) { if !$0.contains($1.provider) { $0.append($1.provider) } }
        ForEach(providers, id: \.self) { provider in
            Section(provider) {
                ForEach(models.filter { $0.provider == provider }, id: \.selectionKey) { model in
                    Text(model.name).tag(model.selectionKey)
                }
            }
        }
        #else
        ForEach(models, id: \.selectionKey) { model in
            ModelRow(name: model.name, provider: model.provider).tag(model.selectionKey)
        }
        #endif
    }
}

/// A model name with its provider under it, as a picker row and as a value.
private struct ModelRow: View {
    let name: String
    let provider: String

    var body: some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
            Text(name)
            Text(provider).font(.subheadline).foregroundStyle(.secondary)
        }
    }
}

/// The fallback chain of Hermes. The picker sets the first fallback model. Hermes keeps the models after it.
private struct FallbackSection: View {
    @Environment(AppStore.self) private var store
    @State private var saving = false

    private var canEdit: Bool { store.connection == .connected || store.isDemo }

    var body: some View {
        let catalog = store.modelCatalog.flatMap { $0.models.isEmpty ? nil : $0 }
        if let chain = store.fallback?.models, catalog != nil || !chain.isEmpty {
            Section {
                // Without a model list the picker still offers None, so a fallback that fails can be removed.
                Picker(selection: Binding(get: { chain.first?.selectionKey ?? "" }, set: { choose($0, chain: chain, catalog: catalog) })) {
                    Text("None").tag("")
                    if let first = chain.first, catalog?.entry(for: first.selection) == nil {
                        ModelRow(name: first.model, provider: first.provider).tag(first.selectionKey)
                    }
                    ModelChoices(models: choices(catalog, first: chain.first))
                } label: {
                    HStack {
                        Text("Fallback model")
                        if saving { ProgressView().controlSize(.small) }
                    }
                }
                .pagePickerStyle()
                .disabled(saving || !canEdit)
                .accessibilityIdentifier("fallback-model-picker")
                ForEach(Array(chain.enumerated().dropFirst()), id: \.offset) { index, model in
                    LabeledContent("Then") { row(model) }
                        .accessibilityIdentifier("fallback-then-\(index)")
                }
            } header: {
                Text("Fallback")
            } footer: {
                footer(chain)
            }
        }
    }

    // A model that the catalog does not have shows its model ID.
    private func row(_ model: FallbackModel) -> ModelRow {
        ModelRow(name: store.modelCatalog?.name(for: model.selection) ?? model.model, provider: model.provider)
    }

    private func footer(_ chain: [FallbackModel]) -> Text {
        if chain.isEmpty {
            return Text("Hermes uses the fallback model when the default model fails or reaches its limit.")
        }
        let text = "Hermes uses this model when the default model fails or reaches its limit. Its provider bills the usage. Changes apply to new turns."
        return Text(chain.count > 1 ? text + " Hermes has more fallback models after this one. Coconut changes only the first." : text)
    }

    // The default model is no fallback for itself. It stays in the list only when Hermes already has it first.
    private func choices(_ catalog: ModelCatalog?, first: FallbackModel?) -> [ModelCatalogEntry] {
        guard let catalog else { return [] }
        let primary = catalog.defaults.selection
        return catalog.models.filter { model in
            !(model.id == primary.model && model.provider == primary.provider)
                || (model.id == first?.model && model.provider == first?.provider)
        }
    }

    // None and the catalog models are the choices. The saved first model is already the current value.
    private func choose(_ key: String, chain: [FallbackModel], catalog: ModelCatalog?) {
        guard !saving, canEdit, key != (chain.first?.selectionKey ?? "") else { return }
        let choice = catalog?.models.first { $0.selectionKey == key }.map { FallbackModel(model: $0.id, provider: $0.provider) }
        guard choice != nil || key.isEmpty else { return }
        let models = FallbackSettings(models: chain).replacingFirst(with: choice)
        saving = true
        store.error = nil
        Task {
            await store.setFallback(models)
            saving = false
        }
    }
}
