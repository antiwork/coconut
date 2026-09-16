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
    @State private var saved = false
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
                        Toggle("Use default model", isOn: Binding(get: { useDefault }, set: { useDefault = $0; edited = true }))
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
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(model.name)
                                    Text(model.provider).font(.caption).foregroundStyle(.secondary)
                                }.tag(model.selectionKey)
                            }
                        }
                        .pickerStyle(.navigationLink)
                        .accessibilityIdentifier("model-picker")
                        if let selectedModel, !selectedModel.reasoningEfforts.isEmpty {
                            Picker("Effort", selection: Binding(get: { effort }, set: { effort = $0; edited = true })) {
                                Text("Model default · " + ModelPresentation.effortName(selectedModel.defaultReasoningEffort))
                                    .tag(String?.none)
                                ForEach(selectedModel.reasoningEfforts, id: \.self) { value in
                                    Text(ModelPresentation.effortName(value)).tag(Optional(value))
                                }
                            }
                            .pickerStyle(.navigationLink)
                            .accessibilityIdentifier("model-effort-picker")
                        } else {
                            LabeledContent("Effort", value: ModelPresentation.effortName(selectedModel?.defaultReasoningEffort))
                        }
                    } header: {
                        Text(isBot ? "Custom model" : "Default model")
                    } footer: {
                        if !isBot { Text("Hermes default. Shared by bots using the default model.") }
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
            if !canEdit {
                Section { Text("Connect to your Mac to save changes.").font(.footnote).foregroundStyle(.secondary) }
            }
            if let currentIssue {
                Section { Text(currentIssue).font(.footnote).foregroundStyle(BoardStyle.accent).textSelection(.enabled) }
                    .accessibilityIdentifier("current-model-error")
            }
            if let error = store.error {
                Section { Text(error).font(.footnote).foregroundStyle(BoardStyle.accent).textSelection(.enabled) }
            }
        }
        .scrollContentBackground(.hidden).background(BoardStyle.paper)
        .navigationTitle("Model").navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .confirmationAction) {
                Button { save() } label: {
                    if saving {
                        ProgressView().accessibilityLabel("Saving model settings")
                    } else if saved && !changed {
                        Image(systemName: "checkmark").accessibilityLabel(store.isDemo ? "Preview updated" : "Saved")
                    } else {
                        Text("Save")
                    }
                }
                    .disabled(saving || !canEdit || !loaded || !valid || !changed || (isBot && bot == nil))
                    .accessibilityIdentifier("save-model-settings")
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

    private func save() {
        guard !saving, valid else { return }
        let submitted = isBot && useDefault ? nil : selection
        saving = true
        saved = false
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
            if accepted && store.error == nil {
                load()
                saved = true
            }
        }
    }

}

enum ModelPresentation {
    static func effortName(_ value: String?) -> String {
        guard let value else { return "Auto" }
        return value == "none" ? "Off" : value.capitalized
    }
}
