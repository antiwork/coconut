import SwiftUI

struct ParallelWorkSettings: Codable, Equatable, Sendable {
    var limit: Int?
    let effectiveLimit: Int
    let automaticLimit: Int
}

// One control: how many bots work at once. Automatic lets the Mac choose from its processors and memory.
struct ParallelWorkSettingsView: View {
    @Environment(AppStore.self) private var store
    @ShownError private var shownError
    @State private var saving = false

    var body: some View {
        Form {
            if let capacity = store.workCapacity {
                Section {
                    Picker("Agents working at once", selection: Binding(get: { capacity.limit }, set: { save($0) })) {
                        Text("Automatic (\(capacity.automaticLimit))").tag(Int?.none)
                        ForEach(Array(1...max(64, capacity.effectiveLimit)), id: \.self) { value in
                            Text("\(value)").tag(Optional(value))
                        }
                    }
                    .pagePickerStyle()
                    .accessibilityIdentifier("work-item-limit")
                } footer: {
                    Text("Automatic lets up to \(capacity.automaticLimit) agents work at once. Your Mac chooses this number from its processors and memory. A lower number does not stop work that runs now.")
                }
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
