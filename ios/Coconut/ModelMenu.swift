import SwiftUI

/// The models that a menu shows first, and the search of the others.
enum ModelShortlist {
    static let limit = 6

    /// The model in use, the default, the models that the person chose lately, and the models of the other agents,
    /// in this order and without a model twice. A short catalog shows whole.
    static func make(catalog: ModelCatalog, current: ModelOverride?, recent: [String], inUse: [ModelOverride]) -> [ModelCatalogEntry] {
        if catalog.models.count <= limit { return catalog.models }
        var keys: [String] = []
        for key in [current?.selectionKey, catalog.defaults.selection.selectionKey] + recent + inUse.map(\.selectionKey) {
            if let key, !keys.contains(key) { keys.append(key) }
        }
        return Array(keys.compactMap { key in catalog.models.first { $0.selectionKey == key } }.prefix(limit))
    }

    /// The models that have each word of the query in the name, the ID or the provider. A name that starts with the
    /// query comes first; the others keep the order of the catalog.
    static func search(_ models: [ModelCatalogEntry], query: String) -> [ModelCatalogEntry] {
        let words = query.split(whereSeparator: \.isWhitespace).map(String.init)
        guard !words.isEmpty else { return models }
        func has(_ text: String, _ word: String) -> Bool {
            text.range(of: word, options: [.caseInsensitive, .diacriticInsensitive]) != nil
        }
        let found = models.filter { model in
            words.allSatisfy { has(model.name, $0) || has(model.id, $0) || has(model.provider, $0) }
        }
        let prefix = query.trimmingCharacters(in: .whitespaces)
        func starts(_ model: ModelCatalogEntry) -> Bool {
            model.name.range(of: prefix, options: [.caseInsensitive, .diacriticInsensitive, .anchored]) != nil
        }
        return found.filter(starts) + found.filter { !starts($0) }
    }

    /// The providers of these models in the order of their first model.
    static func providers(_ models: [ModelCatalogEntry]) -> [String] {
        models.reduce(into: [String]()) { if !$0.contains($1.provider) { $0.append($1.provider) } }
    }
}

/// The models that the person chose lately on this device, the newest first.
enum RecentModels {
    static let key = "recent-models"
    static let limit = 5

    static func read(_ defaults: UserDefaults = .standard) -> [String] { defaults.stringArray(forKey: key) ?? [] }

    static func record(_ selectionKey: String, in defaults: UserDefaults = .standard) {
        defaults.set(adding(selectionKey, to: read(defaults)), forKey: key)
    }

    static func adding(_ selectionKey: String, to keys: [String]) -> [String] {
        Array(([selectionKey] + keys.filter { $0 != selectionKey }).prefix(limit))
    }
}

/// What a model menu shows and changes: the model of every chat. One Hermes profile runs them all.
@MainActor
struct ModelChoice {
    let store: AppStore

    var catalog: ModelCatalog? { store.modelCatalog }
    var canEdit: Bool { store.connection == .connected || store.isDemo }
    var selection: ModelOverride? { catalog?.defaults.selection }
    var entry: ModelCatalogEntry? { selection.flatMap { catalog?.entry(for: $0) } }
    /// The name of the model in use. Nil when the Mac gave no model.
    var name: String? {
        guard let catalog else { return store.model.isEmpty ? nil : store.model }
        return catalog.defaults.model.isEmpty ? nil : catalog.name(for: catalog.defaults.selection)
    }
    var effort: String? { selection.flatMap { catalog?.resolved($0).reasoningEffort } }

    func shortlist(recent: [String]) -> [ModelCatalogEntry] {
        guard let catalog else { return [] }
        return ModelShortlist.make(catalog: catalog, current: selection, recent: recent,
                                   inUse: [])
    }

    func isCurrent(_ model: ModelCatalogEntry) -> Bool {
        model.selectionKey == selection?.selectionKey
    }

    func choose(_ model: ModelCatalogEntry, effort: String? = nil) {
        guard canEdit else { return }
        let selection = ModelOverride(model: model.id, provider: model.provider, reasoningEffort: effort)
        RecentModels.record(model.selectionKey)
        store.error = nil
        Task { _ = await store.setModel(selection) }
    }
}

/// A menu that changes the model in two steps: open it, choose a model. It shows the model in use, the default, and
/// the models that the person chose lately. "All models" opens the whole catalog with a search.
struct ModelMenu<MenuLabel: View>: View {
    @Environment(AppStore.self) private var store
    /// Opens the page with the effort and the fallback model. Nil where no page can open.
    var settings: (() -> Void)? = nil
    /// The closed menu. It gets the name of the model in use.
    @ViewBuilder let label: (String) -> MenuLabel
    @State private var allOpen = false
    @State private var recent = RecentModels.read()

    var body: some View {
        let choice = ModelChoice(store: store)
        Menu {
            if let catalog = choice.catalog, !catalog.models.isEmpty {
                Section(catalog.models.count > ModelShortlist.limit ? "Recent and suggested" : "Models") {
                    ForEach(choice.shortlist(recent: recent), id: \.selectionKey) { model in
                        Toggle(isOn: Binding(get: { choice.isCurrent(model) }, set: { _ in choose(model, choice) })) {
                            Text(model.name)
                        }
                    }
                }
                if let model = choice.entry, !model.reasoningEfforts.isEmpty {
                    Menu("Reasoning · " + ModelPresentation.effortName(choice.effort)) {
                        ForEach(model.reasoningEfforts, id: \.self) { value in
                            Toggle(isOn: Binding(get: { choice.effort == value }, set: { _ in choice.choose(model, effort: value) })) {
                                Text(ModelPresentation.effortName(value))
                            }
                        }
                    }
                }
                Divider()
                if catalog.models.count > ModelShortlist.limit {
                    Button { allOpen = true } label: { Label("All models…", systemImage: "magnifyingglass") }
                }
            } else {
                Text(choice.canEdit ? "The Mac gave no model choices" : "Connect to your Mac to change the model")
            }
            if let settings {
                Button(action: settings) { Label("Effort and fallback…", systemImage: "slider.horizontal.3") }
            }
        } label: {
            label(choice.name ?? "Model")
        }
        .menuStyle(.button).buttonStyle(.plain).menuIndicator(.hidden)
        .disabled(!choice.canEdit && settings == nil)
        .accessibilityLabel("Model, " + (choice.name ?? "not set"))
        .sheet(isPresented: $allOpen) {
            ModelPickerSheet { model in choose(model, ModelChoice(store: store)) }
                .environment(store)
        }
    }

    private func choose(_ model: ModelCatalogEntry, _ choice: ModelChoice) {
        choice.choose(model)
        recent = RecentModels.read()
    }
}

/// The model and its reasoning effort as a pill in the composer under the text, as in Codex. A tap opens the models and
/// the reasoning efforts. Every chat shares the model: one Hermes profile runs them all.
struct ComposerModelMenu: View {
    /// The effort shows in its own chip (ComposerEffortMenu) where the toolbar has one.
    var showsEffort = true
    @Environment(AppStore.self) private var store

    var body: some View {
        let choice = ModelChoice(store: store)
        ModelMenu { name in
            HStack(spacing: BoardStyle.Space.xs) {
                Text(name).lineLimit(1).truncationMode(.middle)
                if showsEffort, let effort = choice.effort {
                    Text(ModelPresentation.effortName(effort)).foregroundStyle(.secondary).lineLimit(1)
                }
                Image(systemName: "chevron.down").font(.caption2.weight(.semibold)).foregroundStyle(.secondary)
            }
            .font(.subheadline.weight(.medium))
            .foregroundStyle(.primary)
            .frame(maxWidth: 220, alignment: .leading)
            .padding(.horizontal, BoardStyle.Space.s)
            .frame(minHeight: 44)
            .contentShape(Rectangle())
        }
        .accessibilityIdentifier("chat-model-menu")
    }
}

/// The name of the model with the mark of a menu, for a header line.
struct ModelMenuLabel: View {
    let name: String
    var symbol = true

    var body: some View {
        HStack(spacing: BoardStyle.Space.xs) {
            if symbol { Image(systemName: "cpu").imageScale(.small) }
            Text(name).lineLimit(1).truncationMode(.middle)
            Image(systemName: "chevron.down").font(.caption2.weight(.semibold))
        }
        .contentShape(Rectangle())
    }
}

/// The whole catalog with a search: the models that the menu shows first, then each provider with its models.
struct ModelPickerSheet: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let choose: (ModelCatalogEntry) -> Void
    @State private var query = ""
    @FocusState private var searching: Bool

    var body: some View {
        let choice = ModelChoice(store: store)
        let models = ModelShortlist.search(store.modelCatalog?.models ?? [], query: query)
        VStack(spacing: 0) {
            HStack(spacing: BoardStyle.Space.m) {
                VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                    Text("Model").font(.headline)
                    Text("For every chat")
                        .font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer(minLength: 0)
                Button("Done") { dismiss() }
                    .buttonStyle(CoconutButtonStyle(kind: .quiet))
                    .keyboardShortcut(.cancelAction)
                    .accessibilityIdentifier("model-picker-done")
            }
            .padding(BoardStyle.Space.l)
            HStack(spacing: BoardStyle.Space.s) {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                TextField("Search models", text: $query)
                    .textFieldStyle(.plain).autocorrectionDisabled().noAutocapitalization()
                    .focused($searching)
                    .accessibilityIdentifier("model-search")
                if !query.isEmpty {
                    Button { query = "" } label: { Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary) }
                        .buttonStyle(.plain).accessibilityLabel("Clear the search")
                }
            }
            .padding(.horizontal, BoardStyle.Space.m).frame(minHeight: 36)
            .background(BoardStyle.surface, in: Capsule())
            .padding(.horizontal, BoardStyle.Space.l)
            ScrollView {
                VStack(alignment: .leading, spacing: BoardStyle.Space.xl) {
                    if query.trimmingCharacters(in: .whitespaces).isEmpty {
                        BoardSection("Recent and suggested") {
                            ForEach(choice.shortlist(recent: RecentModels.read()), id: \.selectionKey) { row($0, choice, provider: true) }
                        }
                    }
                    if models.isEmpty {
                        Text("No model matches \u{201C}\(query)\u{201D}.").font(.subheadline).foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity).padding(.vertical, BoardStyle.Space.xl)
                    }
                    ForEach(ModelShortlist.providers(models), id: \.self) { provider in
                        let rows = models.filter { $0.provider == provider }
                        BoardSection(provider, count: rows.count) {
                            ForEach(rows, id: \.selectionKey) { row($0, choice, provider: false) }
                        }
                    }
                }
                .padding(BoardStyle.Space.l)
            }
        }
        .background(BoardStyle.paper)
        #if os(macOS)
        .frame(width: 460, height: 560)
        #endif
        .onAppear { searching = true }
    }

    private func row(_ model: ModelCatalogEntry, _ choice: ModelChoice, provider: Bool) -> some View {
        HStack(spacing: BoardStyle.Space.m) {
            VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                Text(model.name).foregroundStyle(.primary).lineLimit(1)
                if provider { Text(model.provider).font(.subheadline).foregroundStyle(.secondary).lineLimit(1) }
            }
            Spacer(minLength: BoardStyle.Space.s)
            if model.selectionKey == choice.selection?.selectionKey {
                Text("In use").font(.subheadline).foregroundStyle(.secondary)
                Image(systemName: "checkmark").font(.footnote.weight(.semibold))
            }
        }
        .padding(.horizontal, BoardStyle.Space.m).padding(.vertical, BoardStyle.Space.s)
        .frame(minHeight: 40)
        .contentShape(Rectangle())
        .boardButton("model-choice-" + model.id) {
            choose(model)
            dismiss()
        }
    }
}
