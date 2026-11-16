import Foundation

enum PreviewStateFixture {
    private enum Failure: Error {
        case missingResource
        case invalidReferenceTime
        case invalidTimestamp(String)
    }

    private static let timestampFields: Set<String> = [
        "server_time", "created_at", "updated_at", "scheduled_at", "decided_at",
        "checked_at", "observed_at", "next_run_at", "last_run_at", "read_at", "resolved_at"
    ]

    static func load(bundle: Bundle = .main, now: Date = Date(), resource: String = "PreviewState") throws -> StateSnapshot {
        guard let url = bundle.url(forResource: resource, withExtension: "json") else {
            throw Failure.missingResource
        }
        return try decode(Data(contentsOf: url), now: now)
    }

    static func decode(_ data: Data, now: Date) throws -> StateSnapshot {
        let reference = try APIJSON.decoder().decode(StateSnapshot.self, from: data)
        guard let referenceTime = reference.serverTime.apiDate else {
            throw Failure.invalidReferenceTime
        }
        let offset = now.timeIntervalSince(referenceTime)
        let formatter = ISO8601DateFormatter()

        func shiftTimestamps(_ value: Any) throws -> Any {
            if let items = value as? [Any] {
                return try items.map(shiftTimestamps)
            }
            guard var object = value as? [String: Any] else { return value }
            for (key, child) in object {
                if timestampFields.contains(key), let text = child as? String {
                    guard let date = text.apiDate else { throw Failure.invalidTimestamp(key) }
                    object[key] = formatter.string(from: date.addingTimeInterval(offset))
                } else {
                    object[key] = try shiftTimestamps(child)
                }
            }
            return object
        }

        let document = try JSONSerialization.jsonObject(with: data)
        let shifted = try JSONSerialization.data(withJSONObject: shiftTimestamps(document))
        return try APIJSON.decoder().decode(StateSnapshot.self, from: shifted)
    }
}
