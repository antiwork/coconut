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
    static let shortLabels = ["<15m", "15m", "30m", "1h", "2h", "4h", "8h", ">24h"]
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
        let report: BusinessReport = try await businessRequest("business/reports/" + key)
        businessReports[key] = report
        return report
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
    /// The shapes follow the published snapshots, so the demo shows each tile of each dashboard.
    static func preview(_ key: String, now: Date = Date()) throws -> BusinessReport {
        let stamp = now.formatted(.iso8601)
        let data: [String: Any] = switch key {
        case "support": previewSupport(now)
        case "risk": previewRisk(now)
        case "engineering": previewEngineering(now)
        default: previewHome(now)
        }
        // The pull request snapshot has no timestamp of its own. The demo gives it one, so it reads as fresh.
        let payload: [String: Any] = ["key": key, "data": data, "source": ["repository": "example/reports", "path": "demo.json", "sha": "demo", "url": "https://github.com"], "generated_at": stamp, "ledger_url": "https://github.com/example/accounting"]
        return try APIJSON.decoder().decode(BusinessReport.self, from: JSONSerialization.data(withJSONObject: payload))
    }

    private static let previewCalendar: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "UTC")!
        return calendar
    }()
    private static func previewDay(_ now: Date, back: Int) -> String {
        let date = previewCalendar.date(byAdding: .day, value: -back, to: now) ?? now
        let parts = previewCalendar.dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d-%02d-%02d", parts.year ?? 0, parts.month ?? 0, parts.day ?? 0)
    }
    private static func previewMonth(_ now: Date, back: Int) -> String {
        let date = previewCalendar.date(byAdding: .month, value: -back, to: now) ?? now
        let parts = previewCalendar.dateComponents([.year, .month], from: date)
        return String(format: "%04d-%02d", parts.year ?? 0, parts.month ?? 0)
    }

    private static func previewHome(_ now: Date) -> [String: Any] {
        // Twelve closed months that end with the last month, and thirty settled days that end with yesterday.
        let months: [String] = (1...12).reversed().map { previewMonth(now, back: $0) }
        let rows: [[String: Any]] = months.enumerated().map { i, month -> [String: Any] in
            let x = Double(i)
            let revenue: Double = 210000 + x * 14000 + sin(x * 1.3) * 9000
            let ebitda: Double = 41000 + x * 6500 + cos(x) * 7000
            let net: Double = 28000 + x * 4600 + cos(x * 1.7) * 6000
            return ["month": month, "revenue": revenue.rounded(), "ebitda": ebitda.rounded(), "net_income": net.rounded(), "impairment": 0]
        }
        var monthly: [String: Any] = [:]
        for (month, row) in zip(months, rows) { monthly[month] = row }
        func series(_ offset: Int, base: Double) -> [[Any]] {
            (0..<30).map { i -> [Any] in
                let x = Double(i)
                let value: Double = base + x * 2100 + sin(x * 0.9) * 17000 + cos(x * 0.37) * 9000
                return [previewDay(now, back: 30 - i + offset), value.rounded()]
            }
        }
        let day = previewDay(now, back: 1)
        let closed: String = months.last ?? ""
        let current = series(0, base: 180000)
        // The last day of the series is the published last day, so each screen reads the same number.
        let gmv: [String: Any] = ["last_day_gmv": current.last?[1] ?? 0, "last_day": day, "trailing_avg_gmv": 230000, "annualized_gmv": 83950000,
                                  "gmv_30d": 6900000, "gmv_prev_30d": 6100000, "series": current, "series_prev": series(30, base: 158000),
                                  "change_pct": 13.1, "change_label": "vs prior 30 days", "last_day_vs_avg_pct": 3.5,
                                  "same_weekday_avg": 226000, "same_weekday_pct": 5.3]
        let ledger: [String: Any] = ["ltm_revenue": 3600000, "ltm_ebitda": 870000, "ltm_net_income": 610000, "closed_through": closed, "months": months, "per_month": monthly]
        let money: [String: Any] = ["ltm_revenue": 3600000, "ltm_ebitda": 870000, "ltm_net_income": 610000, "ebitda_margin_pct": 24.2, "net_margin_pct": 16.9,
                                    "impairment_total": 0, "closed_through": closed, "revenue_3m_pct": 8.4, "months": rows]
        var sections: [String: Any] = [:]
        for name in ["support", "issues", "risk", "prs", "gmv", "ledger"] { sections[name] = ["ok": true, "as_of": name == "ledger" ? closed : day] as [String: Any] }
        let top: [String: Any] = ["subject": "Payout timing question", "hours": 29, "queue": "Gumroad"]
        let support: [String: Any] = ["gumroad": "12", "flexile": "2", "gumroad_past_sla": "3", "flexile_past_sla": "0", "top": [top]]
        let issues: [String: Any] = ["total": "18", "past_sla": "2", "oldest_hours": 61]
        let titles: [String: Any] = ["4712": "Checkout fails for cards that need 3D Secure", "4721": "Payout export misses refunds"]
        let high: [String: Any] = ["ok": true, "count": 2, "oldest_hours": 20, "numbers": [4712, 4721], "titles": titles]
        let risk: [String: Any] = ["total": "24", "past_sla": "4", "cb_30d": "61"]
        func count(_ total: Int) -> [String: Any] { ["pullRequests": ["totalCount": total] as [String: Any]] }
        let prs: [String: Any] = ["data": ["gumroad": count(6), "flexile": count(3)] as [String: Any]]
        return ["generated_at": now.formatted(.iso8601), "gmv": gmv, "ledger": ledger, "money": money, "sections": sections,
                "support": support, "issues": issues, "high_priority": high, "risk": risk, "prs": prs]
    }

    private static func previewSupport(_ now: Date) -> [String: Any] {
        func ago(_ seconds: Double) -> String { now.addingTimeInterval(-seconds).formatted(.iso8601) }
        let daily: [[String: Any]] = (0..<30).map { i in ["date": previewDay(now, back: 29 - i), "count": 12 + (i * 7) % 11 + (i % 7 == 5 ? 6 : 0)] }
        let subjects = ["Payout timing question", "Help with a product download", "Update a purchase email", "Refund for a double charge",
                        "VAT invoice for a purchase", "License key did not arrive"]
        let closed: [[String: Any]] = (0..<36).map { i in
            let wait = Double([8, 12, 22, 35, 50, 75, 110, 170, 260, 420, 700, 1500][i % 12])
            return ["slug": "closed-\(i)", "subject": subjects[i % subjects.count], "queue": "gumroad", "status": "closed",
                    "createdAt": ago(Double(i) * 1800 + wait * 60 + 7200), "lastUserEmailCreatedAt": ago(Double(i) * 1800 + wait * 60), "closedAt": ago(Double(i) * 1800)]
        }
        let open: [[String: Any]] = subjects.prefix(4).enumerated().map { i, title in
            ["slug": "sample-\(i)", "subject": title, "queue": "gumroad", "status": "open", "createdAt": ago(Double(i + 1) * 3600), "lastUserEmailCreatedAt": ago(Double(i + 1) * 3000)]
        }
        let issues: [[String: Any]] = ["Checkout fails for cards that need 3D Secure", "Payout export misses refunds", "Discover search drops accented names"].enumerated().map { i, title in
            ["slug": "\(4712 + i * 9)", "subject": title, "createdAt": ago(Double(i + 1) * 86400), "url": "https://github.com/example/gumroad/issues/\(4712 + i * 9)", "labels": ["bug"]]
        }
        return ["generatedAt": now.formatted(.iso8601),
                "support": ["counts": ["all": 14, "mine": 0, "unassigned": 4, "assigned": 10], "open": open, "closed": closed,
                            "closedCount": ["total": 184, "byAgent": [["displayName": "Sam", "count": 100], ["displayName": "Alex", "count": 84]]], "dailyVolume": daily],
                "issues": ["counts": ["all": 18, "mine": 0, "unassigned": 5, "assigned": 13], "open": issues, "closed": [],
                           "closedCount": ["total": 48, "unknown": 2, "byAgent": [["displayName": "Lin", "count": 21], ["displayName": "Grace", "count": 16], ["displayName": "Ada", "count": 9]]],
                           "dailyVolume": daily.map { ["date": $0["date"] ?? "", "count": (($0["count"] as? Int) ?? 0) / 4] }]]
    }

    private static func previewRisk(_ now: Date) -> [String: Any] {
        let names = ["Mira Chen", "Jonas Weber", "Priya Raman", "Tomás Silva", "Hana Sato", "Noor Haddad"]
        func users(_ state: String, _ count: Int) -> [[String: Any]] {
            (0..<count).map { i -> [String: Any] in
                let name = names[(i + state.count) % names.count]
                let handle = name.lowercased().replacingOccurrences(of: " ", with: "")
                let email: String = handle + "@example.com"
                let balance: Int = 48200 + i * 173100
                let user: [String: Any] = ["id": 1000 + i, "external_id": 5000 + i, "name": name, "username": handle, "email": email,
                                           "user_risk_state": state, "balance_cents": balance]
                return user
            }
        }
        let watching: [[String: Any]] = (0..<3).map { i -> [String: Any] in
            let revenue: Int = 1840000 + i * 620000
            let unpaid: Int = 212000 + i * 40000
            let watched: String = now.addingTimeInterval(-Double(i + 2) * 86400).formatted(.iso8601)
            let user: [String: Any] = ["id": 2000 + i, "external_id": 6000 + i, "name": NSNull(), "username": NSNull(), "email": "creator\(i + 1)@example.com",
                                       "user_risk_state": "compliant", "revenue_cents": revenue, "unpaid_balance_cents": unpaid,
                                       "revenue_threshold_cents": 2500000, "notes": "Sales grew fast this month.", "watched_at": watched]
            return user
        }
        let daily: [[String: Any]] = (0..<28).map { i in ["day": previewDay(now, back: 28 - i), "count": 14 + (i * 5) % 9 + (i % 7 == 2 ? 7 : 0), "amount_cents": 140000 + i * 3100] }
        return ["generatedAt": now.formatted(.iso8601),
                "notReviewed": ["count": 24, "users": users("not_reviewed", 6)], "flagged": ["count": 9, "users": users("flagged_for_fraud", 4)],
                "probated": ["count": 5, "users": users("on_probation", 3)], "softSuspended": ["count": 31, "users": users("suspended_for_fraud", 5)],
                "watching": ["count": 3, "users": watching],
                "stats": ["queueSize": 24, "suspendedFraud": 1840, "suspendedTos": 2615],
                "chargebacks": ["last28d": ["count": 512, "amount_cents": 4120000, "mor_count": 489, "mor_amount_cents": 3980000, "connect_count": 23, "connect_amount_cents": 140000],
                                "rate": ["disputes": 512, "transactions": 284000],
                                "outcomes90d": ["won": 142, "lost": 318, "won_cents": 861000, "lost_cents": 3420000], "daily": daily],
                "suspensionAllowance": ["users": 19, "total_negative_cents": -482000, "avg_daily_negative_cents": -17200]]
    }

    private static func previewEngineering(_ now: Date) -> [String: Any] {
        func pull(_ repo: String, _ number: Int, _ title: String, _ author: String, _ adds: Int, _ dels: Int, hours: Double,
                  draft: Bool = false, review: Any = NSNull(), checks: String = "SUCCESS") -> [String: Any] {
            let url: String = "https://github.com/example/\(repo)/pull/\(number)"
            let created: String = now.addingTimeInterval(-hours * 3600).formatted(.iso8601)
            let updated: String = now.addingTimeInterval(-hours * 900).formatted(.iso8601)
            let files: Int = max(1, (adds + dels) / 40)
            let rollup: [String: Any] = ["nodes": [["commit": ["statusCheckRollup": ["state": checks]]]]]
            let login: [String: Any] = ["login": author]
            let pull: [String: Any] = ["number": number, "title": title, "url": url, "author": login, "createdAt": created, "updatedAt": updated,
                                       "additions": adds, "deletions": dels, "changedFiles": files, "isDraft": draft, "reviewDecision": review,
                                       "commits": rollup]
            return pull
        }
        func repo(_ name: String, _ pulls: [[String: Any]], total: Int? = nil) -> [String: Any] {
            ["nameWithOwner": "example/" + name, "name": name, "pullRequests": ["totalCount": total ?? pulls.count, "nodes": pulls]]
        }
        return ["data": [
            "gumroad": repo("gumroad", [
                pull("gumroad", 4731, "Retry failed Ping webhooks with backoff", "lin", 214, 38, hours: 5, review: "APPROVED"),
                pull("gumroad", 4728, "Upgrade the Stripe SDK", "grace", 96, 71, hours: 26, review: "CHANGES_REQUESTED", checks: "FAILURE"),
                pull("gumroad", 4722, "Show refunds in the payout export", "ada", 148, 12, hours: 49, checks: "PENDING"),
                pull("gumroad", 4719, "Faster Discover search for accented names", "sam", 61, 9, hours: 70, draft: true)], total: 6),
            "flexile": repo("flexile", [
                pull("flexile", 912, "Invoice approval reminders", "alex", 187, 44, hours: 11, review: "REVIEW_REQUIRED"),
                pull("flexile", 907, "Fix rounding of equity grants", "lin", 23, 19, hours: 31, review: "APPROVED")], total: 3),
            "helper": repo("helper", []), "iffy": repo("iffy", [])]]
    }
}
