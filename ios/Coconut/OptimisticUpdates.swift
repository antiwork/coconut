import Foundation

enum OptimisticRequest {
    @TaskLocal static var id: String?
}

/// Local edits sit over confirmed values. Reads and receipts update the base, then the newest edit is reapplied.
/// Writes of one resource stay in order; a failed earlier write cannot undo a later choice.
@MainActor
final class OptimisticUpdates {
    @MainActor private protocol Field: AnyObject {
        var empty: Bool { get }
        func restore()
        func capture()
        func project()
        func finish(_ id: UUID)
    }
    @MainActor private final class ValueField<Value: Equatable>: Field {
        var base: Value
        var edits: [(UUID, Value)] = []
        let read: () -> Value
        let write: (Value) -> Void
        var empty: Bool { edits.isEmpty }
        init(read: @escaping () -> Value, write: @escaping (Value) -> Void) {
            self.read = read; self.write = write; base = read()
        }
        func restore() { if read() != base { write(base) } }
        func capture() { base = read() }
        func project() { let value = edits.last?.1 ?? base; if read() != value { write(value) } }
        func finish(_ id: UUID) { edits.removeAll { $0.0 == id } }
    }
    private var fields: [String: any Field] = [:]
    private var tails: [String: (UUID, Task<Void, Never>)] = [:]
    private var epoch = UUID()
    private var receiving = false
    private var active: Set<UUID> = []
    private var failed: Set<UUID> = []
    private var failures: [String: Int] = [:]

    func set<Value: Equatable>(_ key: String, to value: Value, in id: UUID,
                               read: @escaping () -> Value, write: @escaping (Value) -> Void) {
        let field: ValueField<Value>
        if let existing = fields[key] as? ValueField<Value> { field = existing }
        else { field = ValueField(read: read, write: write); fields[key] = field }
        field.edits.append((id, value))
        field.project()
    }

    /// Every authoritative update runs here, including background snapshots. Optimistic values never enter the base.
    @discardableResult
    func receive<Result>(_ update: () throws -> Result) rethrows -> Result {
        if receiving { return try update() }
        receiving = true
        fields.keys.sorted().forEach { fields[$0]?.restore() }
        defer {
            fields.keys.sorted().forEach { fields[$0]?.capture() }
            fields.keys.sorted().forEach { fields[$0]?.project() }
            receiving = false
        }
        return try update()
    }

    func enqueue(_ resource: String, change: (UUID) -> Void, save: @escaping @MainActor () async -> Void) {
        let id = UUID(), epoch = epoch
        active.insert(id)
        change(id)
        let previous = tails[resource]?.1
        let task = Task {
            await previous?.value
            guard self.epoch == epoch, !Task.isCancelled else { return }
            await OptimisticRequest.$id.withValue(id.uuidString) { await save() }
            guard self.epoch == epoch else { return }
            self.active.remove(id)
            if self.failed.remove(id) != nil { self.failures[resource, default: 0] += 1 }
            for key in Array(self.fields.keys) {
                guard let field = self.fields[key] else { continue }
                field.finish(id)
                if field.empty { field.restore(); self.fields[key] = nil }
            }
            self.fields.keys.sorted().forEach { self.fields[$0]?.project() }
            if self.tails[resource]?.0 == id { self.tails[resource] = nil }
        }
        tails[resource] = (id, task)
    }

    func wait() async {
        while let task = tails.values.first?.1 { await task.value }
    }

    /// Execution waits for settings that the UI already shows. A failed prerequisite keeps the work unsent.
    func wait(for resource: String) async -> Bool {
        let epoch = epoch
        let before = failures[resource, default: 0]
        while let task = tails[resource]?.1 { await task.value }
        return epoch == self.epoch && before == failures[resource, default: 0]
    }

    func markFailed() {
        if let text = OptimisticRequest.id, let id = UUID(uuidString: text), active.contains(id) { failed.insert(id) }
    }

    func reset() {
        epoch = UUID()
        tails.values.forEach { $0.1.cancel() }
        tails = [:]; fields = [:]; active = []; failed = []; failures = [:]
    }
}

extension AppStore {
    func optimisticBot<Value: Equatable>(_ id: String, _ field: WritableKeyPath<Bot, Value>, _ value: Value, in transaction: UUID) {
        optimisticUpdates.set("bot/\(id)/\(field)", to: Optional(value), in: transaction,
            read: { self.bots.first { $0.id == id }?[keyPath: field] },
            write: { value in
                if let value, let index = self.bots.firstIndex(where: { $0.id == id }) { self.bots[index][keyPath: field] = value }
            })
    }

    func optimisticRoutine<Value: Equatable>(_ id: String, _ field: WritableKeyPath<Routine, Value>, _ value: Value, in transaction: UUID) {
        optimisticUpdates.set("routine/\(id)/\(field)", to: Optional(value), in: transaction,
            read: { self.routines.first { $0.id == id }?[keyPath: field] },
            write: { value in
                if let value, let index = self.routines.firstIndex(where: { $0.id == id }) { self.routines[index][keyPath: field] = value }
            })
    }

    func optimisticValue<Value: Equatable>(_ field: ReferenceWritableKeyPath<AppStore, Value>, _ value: Value, in transaction: UUID) {
        optimisticUpdates.set("store/\(field)", to: value, in: transaction, read: { self[keyPath: field] }, write: { self[keyPath: field] = $0 })
    }

    func optimisticTeam<Value: Equatable>(_ room: String, _ field: WritableKeyPath<TeamRoom, Value>, _ value: Value, in transaction: UUID) {
        optimisticUpdates.set("team/\(room)/\(field)", to: Optional(value), in: transaction,
            read: { self.team?.rooms.first { $0.room == room }?[keyPath: field] },
            write: { value in
                if let value, let index = self.team?.rooms.firstIndex(where: { $0.room == room }) { self.team?.rooms[index][keyPath: field] = value }
            })
    }

    func optimisticDocument<Value: Equatable>(_ id: String, _ field: WritableKeyPath<BotDocument, Value>, _ value: Value, in transaction: UUID) {
        optimisticUpdates.set("document/\(id)/\(field)", to: Optional(value), in: transaction,
            read: { self.documents?.first { $0.id == id }?[keyPath: field] },
            write: { value in
                if let value, let index = self.documents?.firstIndex(where: { $0.id == id }) { self.documents?[index][keyPath: field] = value }
            })
    }

    func optimisticNotice<Value: Equatable>(_ room: String, _ id: String, _ field: WritableKeyPath<WorkspaceNotice, Value>, _ value: Value, in transaction: UUID) {
        optimisticUpdates.set("team/\(room)/notice/\(id)/\(field)", to: Optional(value), in: transaction,
            read: { self.team?.rooms.first { $0.room == room }?.notices?.first { $0.id == id }?[keyPath: field] },
            write: { value in
                if let value, let i = self.team?.rooms.firstIndex(where: { $0.room == room }),
                   let j = self.team?.rooms[i].notices?.firstIndex(where: { $0.id == id }) { self.team?.rooms[i].notices?[j][keyPath: field] = value }
            })
    }
}
