import SwiftUI

struct ParallelWorkSettings: Codable, Sendable {
    var limit: Int?
    let effectiveLimit: Int
    let automaticLimit: Int
}

struct ParallelWorkSettingsView: View {
    @Environment(AppStore.self) private var store
    var botId: String? = nil
    @State private var saving = false
    @State private var customLimit = 4

    private var bot: Bot? { store.bots.first { $0.id == botId } }
    private var limit: Int? { botId == nil ? store.parallelWork?.limit : bot?.workerLimit }
    private var effectiveLimit: Int { limit ?? store.parallelWork?.effectiveLimit ?? 1 }

    var body: some View {
        List {
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
                            LabeledContent("Workers", value: "\(customLimit)")
                        }
                        .accessibilityIdentifier("worker-limit")
                        Button("Save limit") { save(customLimit) }
                            .disabled(customLimit == limit)
                    } else {
                        LabeledContent("Workers", value: "\(effectiveLimit)")
                    }
                } footer: {
                    Text(botId == nil
                         ? "Automatic allows up to \(settings.automaticLimit) workers per session, based on your Mac’s processors and memory."
                         : "This bot uses the default of \(settings.effectiveLimit) workers unless you choose a limit.")
                }
                Section {} footer: {
                    Text("Hermes can delegate independent steps to workers. The limit applies to each session. More workers use more memory and model allowance.\n\nBots read the default on their next turn. Running workers continue.")
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
        .onAppear { customLimit = effectiveLimit }
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
