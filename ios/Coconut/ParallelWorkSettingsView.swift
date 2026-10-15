import SwiftUI

struct ParallelWorkSettings: Codable, Equatable, Sendable {
    var limit: Int?
    let effectiveLimit: Int
    let automaticLimit: Int
}

struct ParallelWorkSettingsView: View {
    @Environment(AppStore.self) private var store
    var botId: String? = nil
    @State private var saving = false
    @State private var customLimit = 4
    @State private var workLimit = 4

    private var bot: Bot? { store.bots.first { $0.id == botId } }
    private var limit: Int? { botId == nil ? store.parallelWork?.limit : bot?.workerLimit }
    private var effectiveLimit: Int { limit ?? store.parallelWork?.effectiveLimit ?? 1 }

    var body: some View {
        List {
            if botId == nil, let capacity = store.workCapacity {
                Section {
                    Toggle("Automatic", isOn: Binding(
                        get: { capacity.limit == nil },
                        set: { saveCapacity($0 ? nil : capacity.effectiveLimit) }
                    ))
                    .tint(BoardStyle.green)
                    .accessibilityIdentifier("automatic-work-items")
                    if capacity.limit != nil {
                        Stepper("Work items: \(workLimit)", value: $workLimit, in: 1...64)
                            .accessibilityIdentifier("work-item-limit")
                        Button("Save work limit") { saveCapacity(workLimit) }
                            .disabled(workLimit == capacity.limit)
                    } else {
                        LabeledContent("Work items", value: "\(capacity.effectiveLimit)")
                    }
                } header: {
                    Text("Concurrent work")
                } footer: {
                    Text("Runs up to \(capacity.effectiveLimit) work items across all bots. Automatic uses your Mac’s processors and memory. Lowering the limit leaves running work intact.")
                }
            }
            if let settings = store.parallelWork {
                Section {
                    Toggle(botId == nil ? "Automatic" : "Use default", isOn: Binding(
                        get: { limit == nil },
                        set: { automatic in save(automatic ? nil : effectiveLimit) }
                    ))
                    .tint(BoardStyle.green)
                    .accessibilityIdentifier("automatic-workers")
                    if limit != nil {
                        Stepper(value: $customLimit, in: 1...max(64, effectiveLimit)) {
                            LabeledContent("Helpers", value: "\(customLimit)")
                        }
                        .accessibilityIdentifier("worker-limit")
                        Button("Save limit") { save(customLimit) }
                            .disabled(customLimit == limit)
                    } else {
                        LabeledContent("Helpers", value: "\(effectiveLimit)")
                    }
                } header: {
                    Text("Helpers per work item")
                } footer: {
                    Text(botId == nil
                         ? "Automatic allows up to \(settings.automaticLimit) helpers per work item, based on your Mac’s processors and memory."
                         : "This bot uses the default of \(settings.effectiveLimit) helpers unless you choose a limit.")
                }
                Section {} footer: {
                    Text("Hermes can split a work item among helpers. Higher limits use more memory and model allowance. Changes apply to new turns.")
                }
            } else {
                Text("Connect to an updated Mac host to change parallel work.").foregroundStyle(.secondary)
            }
            if let error = store.error { Text(error).foregroundStyle(BoardStyle.accent) }
        }
        .disabled(saving)
        .scrollContentBackground(.hidden)
        .background(BoardStyle.paper)
        .navigationTitle("Parallel work")
        .navigationBarTitleDisplayMode(.inline)
        .onAppear {
            customLimit = effectiveLimit
            workLimit = store.workCapacity?.effectiveLimit ?? 4
        }
        .onChange(of: effectiveLimit) { previous, current in
            if customLimit == previous { customLimit = current }
        }
        .onChange(of: store.workCapacity?.effectiveLimit) { previous, current in
            if workLimit == (previous ?? 4) { workLimit = current ?? 4 }
        }
    }

    private func saveCapacity(_ value: Int?) {
        saving = true
        Task {
            await store.setWorkCapacity(value)
            workLimit = store.workCapacity?.effectiveLimit ?? 4
            saving = false
        }
    }

    private func save(_ value: Int?) {
        saving = true
        Task {
            await store.setWorkerLimit(value, botId: botId)
            customLimit = effectiveLimit
            saving = false
        }
    }
}

struct WorkerLimitRequest: Encodable {
    let limit: Int?
    let bot: Bool
    func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        let key: CodingKeys = bot ? .workerLimit : .limit
        if let limit { try values.encode(limit, forKey: key) }
        else { try values.encodeNil(forKey: key) }
    }
    private enum CodingKeys: String, CodingKey { case limit, workerLimit }
}
