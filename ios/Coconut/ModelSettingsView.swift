import SwiftUI

struct ModelSettingsView: View {
    @Environment(AppStore.self) private var store
    var botId: String? = nil
    @State private var useDefault = true
    @State private var selectionKey = ""
    @State private var effort: String?
    @State private var original: ModelOverride?
    @State private var originalUseDefault = true
    @State private var loaded = false
    @State private var refreshed = false
    @State private var saving = false
    @State private var edited = false

    private var bot: Bot? { store.bots.first { $0.id == botId } }
    private var catalog: ModelCatalog? { store.modelCatalog }
    private var current: ModelOverride? {
        if isBot, bot?.modelError != nil { return bot?.modelOverride }
        return bot?.modelOverride ?? catalog?.defaults.selection
    }
    private var currentIssue: String? {
        if let error = bot?.modelError { return error }
        if !isBot || bot?.modelOverride == nil {
            return catalog?.defaults.unavailableReason
        }
        return nil
    }
    private var selectedModel: ModelCatalogEntry? { catalog?.models.first { $0.selectionKey == selectionKey } }
    private var selection: ModelOverride? {
        selectedModel.map { ModelOverride(model: $0.id, provider: $0.provider, reasoningEffort: effort) }
    }
    private var canEdit: Bool { store.connection == .connected || store.isDemo }
    private var isBot: Bool { botId != nil }
    private var changed: Bool {
        if isBot, useDefault { return !originalUseDefault }
        return (isBot && originalUseDefault) || selection != original
    }
    private var valid: Bool {
        if isBot, useDefault { return catalog?.defaults.supported != false }
        return selection.map { catalog?.contains($0) == true } ?? false
    }

    var body: some View {
        Form {
            if let catalog {
                if isBot {
                    Section {
                        Toggle("Use default model", isOn: Binding(get: { useDefault }, set: { chooseDefault($0) }))
                            .tint(BoardStyle.green)
                            .accessibilityIdentifier("use-default-model")
                        if useDefault {
                            LabeledContent("Model", value: catalog.name(for: catalog.defaults.selection))
                                .accessibilityIdentifier("inherited-model")
                            if catalog.entry(for: catalog.defaults.selection) == nil {
                                LabeledContent("Provider", value: catalog.defaults.provider)
                            }
                            LabeledContent("Effort", value: ModelPresentation.effortName(catalog.defaults.reasoningEffort))
                                .accessibilityIdentifier("inherited-effort")
                        }
                    }
                    .disabled(saving || !canEdit)
                }
                if (!isBot || !useDefault) && (catalog.models.isEmpty || current.map { catalog.entry(for: $0) == nil } == true) {
                    Section {
                        if let current {
                            LabeledContent("Model", value: current.model.isEmpty ? "Not configured" : catalog.name(for: current))
                                .accessibilityIdentifier("current-model")
                            LabeledContent("Provider", value: current.provider)
                                .accessibilityIdentifier("current-model-provider")
                            if bot?.modelError == nil {
                                let effective = bot?.effectiveModel ?? catalog.resolved(current)
                                LabeledContent("Effort", value: ModelPresentation.effortName(effective.reasoningEffort))
                            }
                        }
                        if catalog.loading == true {
                            ProgressView("Loading models")
                        } else if catalog.models.isEmpty {
                            Text("No model choices are available from Hermes.").foregroundStyle(.secondary)
                        }
                    } header: {
                        Text(isBot ? "Current model" : "Hermes default")
                    }
                }
                if (!isBot || !useDefault) && !catalog.models.isEmpty {
                    Section {
                        Picker("Model", selection: Binding(get: { selectionKey }, set: { chooseModel($0) })) {
                            if selectedModel == nil { Text("Choose a model").tag("") }
                            ForEach(catalog.models, id: \.selectionKey) { model in
                                ModelRow(name: model.name, provider: model.provider).tag(model.selectionKey)
                            }
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
                        Text(isBot ? "Custom model" : "Default model")
                    } footer: {
                        if !isBot { Text("Hermes default. Shared by bots using the default model. Changes save when you make them.") }
                    }
                    .disabled(saving || !canEdit)
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
            if !isBot { FallbackSection() }
            if !canEdit {
                Section {} footer: { Text("Connect to your Mac first.") }
            }
            if let currentIssue {
                Section { InlineError(currentIssue) }
                    .accessibilityIdentifier("current-model-error")
            }
            if let error = store.error {
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
        .onChange(of: bot?.modelOverride) { _, _ in
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

    // A custom model starts from the default, so the bot keeps the same model until the person picks another one.
    private func chooseDefault(_ value: Bool) {
        useDefault = value
        edited = true
        if !value, selectedModel == nil, let catalog, let entry = catalog.entry(for: catalog.defaults.selection) {
            selectionKey = entry.selectionKey
            effort = catalog.defaults.selection.reasoningEffort
        }
        save()
    }

    private func load() {
        guard let catalog else { return }
        original = isBot ? bot?.modelOverride : current
        originalUseDefault = isBot && bot?.modelOverride == nil && bot?.modelError == nil
        useDefault = originalUseDefault
        selectionKey = current.flatMap { catalog.entry(for: $0)?.selectionKey } ?? ""
        effort = current?.reasoningEffort
        loaded = true
        edited = false
    }

    // Each change saves when it is valid. A model without a choice waits until the person picks one.
    private func save() {
        guard !saving, loaded, canEdit, valid, changed, !(isBot && bot == nil) else { return }
        let submitted = isBot && useDefault ? nil : selection
        saving = true
        store.error = nil
        Task {
            let accepted: Bool
            if let botId {
                accepted = await store.updateBotModel(botId: botId, selection: submitted)
            } else if let submitted {
                accepted = await store.setModel(submitted)
            } else {
                accepted = false
            }
            saving = false
            // A failed save returns the page to the saved choice, so choosing the same model again retries it.
            load()
        }
    }

}

enum ModelPresentation {
    static func effortName(_ value: String?) -> String {
        guard let value else { return "Auto" }
        return value == "none" ? "Off" : value.capitalized
    }
}

/// A model name with its provider under it, as a picker row and as a value.
private struct ModelRow: View {
    let name: String
    let provider: String

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(name)
            Text(provider).font(.caption).foregroundStyle(.secondary)
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
                if let catalog {
                    Picker(selection: Binding(get: { chain.first?.selectionKey ?? "" }, set: { choose($0, chain: chain, catalog: catalog) })) {
                        Text("None").tag("")
                        if let first = chain.first, catalog.entry(for: first.selection) == nil {
                            ModelRow(name: first.model, provider: first.provider).tag(first.selectionKey)
                        }
                        ForEach(choices(catalog, first: chain.first), id: \.selectionKey) { model in
                            ModelRow(name: model.name, provider: model.provider).tag(model.selectionKey)
                        }
                    } label: {
                        HStack {
                            Text("Fallback model")
                            if saving { ProgressView().controlSize(.small) }
                        }
                    }
                    .pagePickerStyle()
                    .disabled(saving || !canEdit)
                    .accessibilityIdentifier("fallback-model-picker")
                } else if let first = chain.first {
                    LabeledContent("Fallback model") { row(first) }
                }
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
    private func choices(_ catalog: ModelCatalog, first: FallbackModel?) -> [ModelCatalogEntry] {
        let primary = catalog.defaults.selection
        return catalog.models.filter { model in
            !(model.id == primary.model && model.provider == primary.provider)
                || (model.id == first?.model && model.provider == first?.provider)
        }
    }

    // The choice replaces the first model. The models after it stay, in order, without a second copy of the choice.
    private func choose(_ key: String, chain: [FallbackModel], catalog: ModelCatalog) {
        guard !saving, canEdit, key != (chain.first?.selectionKey ?? "") else { return }
        let rest = Array(chain.dropFirst())
        var models = rest
        if let choice = catalog.models.first(where: { $0.selectionKey == key }) {
            let model = FallbackModel(model: choice.id, provider: choice.provider)
            models = [model] + rest.filter {
                $0.model.lowercased() != model.model.lowercased() || $0.provider.lowercased() != model.provider.lowercased()
            }
        } else if !key.isEmpty {
            return
        }
        saving = true
        store.error = nil
        Task {
            await store.setFallback(models)
            saving = false
        }
    }
}
