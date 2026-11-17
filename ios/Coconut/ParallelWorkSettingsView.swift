import SwiftUI

struct ParallelWorkSettings: Codable, Equatable, Sendable {
    var limit: Int?
    let effectiveLimit: Int
    let automaticLimit: Int
}

/// An agent's queue on the Hermes board of the Mac.
struct AgentQueue: Codable, Equatable, Sendable {
    var running = 0
    var waiting = 0
    var needsYou = 0
    var doneToday = 0

    var isEmpty: Bool { running + waiting + needsYou + doneToday == 0 }

    /// "1 running · 2 waiting · 1 needs you · 3 done today", without the empty lanes.
    var summary: String {
        [(running, "running"), (waiting, "waiting"), (needsYou, "needs you"), (doneToday, "done today")]
            .filter { $0.0 > 0 }.map { "\($0.0) \($0.1)" }.joined(separator: " · ")
    }
}

// Three limits, all kept in Hermes on the Mac: agents working at once, work sessions of each agent, and helpers of each
// task. Automatic lets the Mac or Hermes choose.
struct ParallelWorkSettingsView: View {
    @Environment(AppStore.self) private var store
    @ShownError private var shownError
    @State private var saving = false

    var body: some View {
        Form {
            if let capacity = store.workCapacity {
                limitSection("Agents working at once", capacity, identifier: "work-item-limit",
                             footer: "Automatic lets up to \(capacity.automaticLimit) agents work at once. Your Mac chooses this number from its processors and memory.") { value in
                    await store.setWorkCapacity(value)
                }
                if let agent = store.agentLimit {
                    limitSection("Work sessions per agent", agent, identifier: "agent-work-limit",
                                 footer: "Each agent runs up to this many work sessions at once, so every agent keeps moving. Your messages never wait for a session.") { value in
                        await store.setAgentLimit(value)
                    }
                }
                if let helpers = store.parallelWork {
                    limitSection("Helpers per task", helpers, identifier: "helper-limit",
                                 footer: "A task can hand parts to this many helpers at once. Each helper uses model tokens. Every chat shares this number.") { value in
                        await store.setWorkerLimit(value)
                    }
                }
                Section {} footer: { Text("A lower number does not stop work that runs now.") }
            } else {
                Section {} footer: {
                    Text(store.hasPairing || store.isDemo ? "Update Coconut on your Mac to change this." : "Connect to your Mac first.")
                }
            }
            if let error = shownError { Section { InlineError(error) } }
        }
        .disabled(saving)
        .settingsBackground()
        .navigationTitle("Parallel work")
        .inlineNavigationTitle()
    }

    private func limitSection(_ title: String, _ settings: ParallelWorkSettings, identifier: String, footer: String,
                              save: @escaping @MainActor (Int?) async -> Void) -> some View {
        Section {
            Picker(title, selection: Binding(get: { settings.limit }, set: { value in
                guard value != settings.limit else { return }
                saving = true
                Task {
                    await save(value)
                    saving = false
                }
            })) {
                Text("Automatic (\(settings.automaticLimit))").tag(Int?.none)
                ForEach(Array(1...max(64, settings.effectiveLimit)), id: \.self) { value in
                    Text("\(value)").tag(Optional(value))
                }
            }
            .pagePickerStyle()
            .accessibilityIdentifier(identifier)
        } footer: {
            Text(footer)
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
