import Foundation

/// The published JSON stays intact. The native views choose a presentation without recalculating the ledger.
enum BusinessValue: Decodable, Sendable, Equatable {
    case object([String: BusinessValue]), array([BusinessValue]), string(String), number(Double), bool(Bool), null

    init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer()
        if value.decodeNil() { self = .null }
        else if let v = try? value.decode(Bool.self) { self = .bool(v) }
        else if let v = try? value.decode(Double.self) { self = .number(v) }
        else if let v = try? value.decode(String.self) { self = .string(v) }
        else if let v = try? value.decode([String: BusinessValue].self) { self = .object(v) }
        else { self = .array(try value.decode([BusinessValue].self)) }
    }
    subscript(_ key: String) -> BusinessValue { object[key] ?? .null }
    subscript(_ index: Int) -> BusinessValue { array.indices.contains(index) ? array[index] : .null }
    var object: [String: BusinessValue] { if case .object(let v) = self { return v }; return [:] }
    var array: [BusinessValue] { if case .array(let v) = self { return v }; return [] }
    var text: String { if case .string(let v) = self { return v }; return "" }
    var number: Double? {
        if case .number(let v) = self { return v.isFinite ? v : nil }
        if case .string(let v) = self, let n = Double(v), n.isFinite { return n }
        return nil
    }
    var isNull: Bool { self == .null }
    var flag: Bool { if case .bool(let v) = self { return v }; return false }
}

struct SupportResolution {
    let minutes: [Double]
    static let labels = ["<15m", "15–30m", "30–60m", "1–2h", "2–4h", "4–8h", "8–24h", ">24h"]
    var buckets: [Int] {
        var result = Array(repeating: 0, count: 8)
        for value in minutes { result[[15.0, 30, 60, 120, 240, 480, 1440].firstIndex(where: { value < $0 }) ?? 7] += 1 }
        return result
    }
    var median: Double? { minutes.isEmpty ? nil : minutes[minutes.count / 2] }
    var p90: Double? { minutes.isEmpty ? nil : minutes[min(minutes.count - 1, Int(Double(minutes.count) * 0.9))] }
    init(closed: [BusinessValue], reference: Date?) {
        guard let reference else { minutes = []; return }
        minutes = closed.compactMap { ticket -> Double? in
            guard let end = ticket["closedAt"].text.apiDate, end <= reference,
                  reference.timeIntervalSince(end) <= 86400 else { return nil }
            let start = ticket["lastUserEmailCreatedAt"].text.apiDate ?? ticket["createdAt"].text.apiDate
            return start.map { max(0, end.timeIntervalSince($0) / 60) }
        }.sorted()
    }
}

struct BusinessReport: Decodable, Sendable {
    struct Source: Decodable, Sendable { let repository: String; let path: String; let sha: String; let url: String }
    let key: String
    let data: BusinessValue
    let source: Source
    let generatedAt: String?
    let ledgerUrl: String
    var stale: Bool { generatedAt?.apiDate.map { Date().timeIntervalSince($0) > 3600 } ?? true }

    static let titles = ["overview": "Business", "accounting": "Accounting", "support": "Support", "risk": "Risk", "engineering": "Engineering"]
    static let symbols = ["overview": "chart.xyaxis.line", "accounting": "books.vertical", "support": "lifepreserver", "risk": "shield.lefthalf.filled", "engineering": "chevron.left.forwardslash.chevron.right"]
    static let keys = ["overview", "accounting", "support", "risk", "engineering"]
}

struct LedgerIndex: Decodable, Sendable {
    struct File: Decodable, Identifiable, Sendable { let path: String; let sha: String; var id: String { path } }
    let files: [File]
}
struct LedgerFile: Decodable, Sendable { let path: String; let content: String; let url: String; let sha: String }

extension AppStore {
    func businessReport(_ key: String) async throws -> BusinessReport {
        if isDemo { return try BusinessReport.preview(key) }
        return try await businessRequest("business/reports/" + key)
    }
    func businessLedger() async throws -> LedgerIndex { try await businessRequest("business/ledger") }
    func businessLedgerFile(_ path: String) async throws -> LedgerFile {
        var components = URLComponents()
        components.queryItems = [URLQueryItem(name: "path", value: path)]
        return try await businessRequest("business/ledger?" + (components.percentEncodedQuery ?? ""))
    }
}

extension BusinessReport {
    /// Synthetic numbers, used only by previews and tests. Never copied from private business records.
    static func preview(_ key: String) throws -> BusinessReport {
        let months = (1...12).map { String(format: "2026-%02d", $0) }
        let monthly = Dictionary(uniqueKeysWithValues: months.enumerated().map { i, m in
            (m, ["revenue": 210000.0 + Double(i) * 14000, "ebitda": 41000.0 + Double(i) * 6500, "net_income": 28000.0 + Double(i) * 4600])
        })
        let days = (1...30).map { String(format: "2026-11-%02d", $0) }
        let series: [[Any]] = days.enumerated().map { i, d in [d, 180000.0 + Double(i) * 2100 + sin(Double(i)) * 17000] }
        let home: [String: Any] = ["gmv": ["last_day_gmv": 238000, "last_day": "2026-11-30", "gmv_30d": 6900000, "gmv_prev_30d": 6100000, "series": series],
            "ledger": ["ltm_revenue": 3600000, "ltm_ebitda": 870000, "ltm_net_income": 610000, "closed_through": "2026-10", "months": months, "per_month": monthly],
            "support": ["gumroad": "12", "flexile": "2", "gumroad_past_sla": "3", "flexile_past_sla": "0"],
            "issues": ["total": "18", "past_sla": "2"], "risk": ["total": "24", "past_sla": "4"], "high_priority": ["count": 2]]
        let daily: [[String: Any]] = days.enumerated().map { i, d in ["date": d, "count": 12 + i % 9] }
        let closed: [[String: Any]] = (0..<36).map { i -> [String: Any] in
            let start = Date().addingTimeInterval(-Double(1800 + i * 900)).formatted(.iso8601)
            let end = Date().addingTimeInterval(-Double(i * 60)).formatted(.iso8601)
            return ["createdAt": start, "closedAt": end]
        }
        let open: [[String: Any]] = ["Payout timing question", "Help with a product download", "Update a purchase email"].enumerated().map { i, title in
            ["slug": "sample-\(i)", "subject": title, "queue": "Gumroad", "createdAt": Date().addingTimeInterval(-Double(i + 1) * 3600).formatted(.iso8601)]
        }
        let support: [String: Any] = ["support": ["counts": ["all": 14, "unassigned": 4, "assigned": 10], "open": open, "closed": closed, "closedCount": ["total": 184, "byAgent": [["displayName": "Alex", "count": 84], ["displayName": "Sam", "count": 100]]], "dailyVolume": daily], "issues": ["counts": ["all": 18], "open": [], "closed": [], "closedCount": ["total": 48], "dailyVolume": daily]]
        let data = key == "support" ? support : home
        let payload: [String: Any] = ["key": key, "data": data, "source": ["repository": "example/reports", "path": "demo.json", "sha": "demo", "url": "https://github.com"], "generated_at": Date().formatted(.iso8601), "ledger_url": "https://github.com/example/accounting"]
        return try APIJSON.decoder().decode(BusinessReport.self, from: JSONSerialization.data(withJSONObject: payload))
    }
}
