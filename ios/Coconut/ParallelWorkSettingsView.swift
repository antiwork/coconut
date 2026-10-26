import SwiftUI

struct ParallelWorkSettings: Codable, Equatable, Sendable {
    var limit: Int?
    let effectiveLimit: Int
    let automaticLimit: Int
}

// One control: how many bots work at once. Automatic lets the Mac choose from its processors and memory.
struct ParallelWorkSettingsView: View {
    @Environment(AppStore.self) private var store
    @State private var saving = false

    var body: some View {
        Form {
            if let capacity = store.workCapacity {
                Section {
                    Picker("Bots working at once", selection: Binding(get: { capacity.limit }, set: { save($0) })) {
                        Text("Automatic (\(capacity.automaticLimit))").tag(Int?.none)
                        ForEach(Array(1...max(64, capacity.effectiveLimit)), id: \.self) { value in
                            Text("\(value)").tag(Optional(value))
                        }
                    }
                    .pickerStyle(.navigationLink)
                    .accessibilityIdentifier("work-item-limit")
                } footer: {
                    Text("Automatic lets up to \(capacity.automaticLimit) bots work at once. Your Mac chooses this number from its processors and memory. A lower number does not stop work that runs now.")
                }
            } else {
                Section {} footer: {
                    Text(store.hasPairing || store.isDemo ? "Update Coconut on your Mac to change this." : "Connect to your Mac first.")
                }
            }
            if let error = store.error { Section { InlineError(error) } }
        }
        .disabled(saving)
        .settingsBackground()
        .navigationTitle("Parallel work")
        .navigationBarTitleDisplayMode(.inline)
    }

    private func save(_ value: Int?) {
        guard value != store.workCapacity?.limit else { return }
        saving = true
        Task {
            await store.setWorkCapacity(value)
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
