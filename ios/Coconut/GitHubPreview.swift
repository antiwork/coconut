import Foundation
import Observation

/// The preview of a GitHub address, as the Mac sends it (backend/coconut/github_preview.py). The Mac reads GitHub with
/// the connected account of the person. A kind that this app does not know fails to decode, and the link stays plain.
struct GitHubPreview: Codable, Equatable, Sendable {
    enum Kind: String, Codable, Sendable {
        case pull, issue, commit, repository, file, comment
    }

    var kind: Kind
    var url: String
    var repository: String
    var number: Int? = nil
    var title: String? = nil
    /// A pull request: open, draft, merged or closed. An issue: open, closed or not_planned.
    var state: String? = nil
    var author: GitHubPerson? = nil
    var additions: Int? = nil
    var deletions: Int? = nil
    var changedFiles: Int? = nil
    var comments: Int? = nil
    var checks: GitHubChecks? = nil
    /// approved, changes_requested or review_requested.
    var review: String? = nil
    var updatedAt: String? = nil
    var labels: [GitHubLabel]? = nil
    var moreLabels: Int? = nil
    var assignees: [GitHubPerson]? = nil
    var sha: String? = nil
    var description: String? = nil
    var stars: Int? = nil
    var forks: Int? = nil
    var language: String? = nil
    var `private`: Bool? = nil
    var archived: Bool? = nil
    var path: String? = nil
    var ref: String? = nil
    var startLine: Int? = nil
    var endLine: Int? = nil
    var lines: [String]? = nil
    var moreLines: Int? = nil
    /// The page of a comment: pull, issue or commit.
    var on: String? = nil
    var excerpt: String? = nil

    var link: URL? { URL(string: url).flatMap { GitHubLink.isCard($0) ? $0 : nil } }

    /// The first seven characters of the commit.
    var shortSHA: String? { sha.map { String($0.prefix(7)) } }

    /// The reference after the name of the repository: "#2314" or the short commit.
    var reference: String? {
        if let number { return "#\(number)" }
        return kind == .commit || kind == .comment ? shortSHA : nil
    }

    /// The word of the state, as the card shows it.
    var stateLabel: String? {
        switch (kind, state) {
        case (.pull, "open"), (.issue, "open"): "Open"
        case (.pull, "draft"): "Draft"
        case (.pull, "merged"): "Merged"
        case (.pull, "closed"), (.issue, "closed"): "Closed"
        case (.issue, "not_planned"): "Not planned"
        default: nil
        }
    }

    /// "Lines 12–19", "Line 7", or nil for a file without a range.
    var lineRange: String? {
        guard let startLine, let endLine else { return nil }
        return startLine == endLine ? "Line \(startLine)" : "Lines \(startLine)–\(endLine)"
    }

    /// What VoiceOver says for the card.
    var spoken: String {
        var parts: [String] = []
        switch kind {
        case .pull: parts.append("Pull request \(number.map(String.init) ?? "") in \(repository)")
        case .issue: parts.append("Issue \(number.map(String.init) ?? "") in \(repository)")
        case .commit: parts.append("Commit \(shortSHA ?? "") in \(repository)")
        case .repository: parts.append("Repository \(repository)")
        case .file: parts.append("\(path ?? "File") in \(repository)")
        case .comment: parts.append("Comment by \(author?.login ?? "someone") in \(repository)")
        }
        if let stateLabel { parts.append(stateLabel) }
        for text in [title, description, excerpt, lineRange] {
            if let text, !text.isEmpty { parts.append(text) }
        }
        if kind != .comment, let author { parts.append("by \(author.login)") }
        if let checks { parts.append(checks.label) }
        if let review = GitHubPreview.reviewLabel(review) { parts.append(review) }
        return parts.joined(separator: ", ")
    }

    static func reviewLabel(_ review: String?) -> String? {
        switch review {
        case "approved": "Approved"
        case "changes_requested": "Changes requested"
        case "review_requested": "Review requested"
        default: nil
        }
    }

    /// A count as GitHub writes it: 942, 7.4k, 12k, 1.2M.
    static func compact(_ count: Int) -> String {
        func short(_ value: Double, _ unit: String) -> String {
            let rounded = (value * 10).rounded(.down) / 10
            return (rounded < 10 && rounded != rounded.rounded() ? String(format: "%.1f", rounded) : String(Int(rounded))) + unit
        }
        if count < 1000 { return String(count) }
        return count < 1_000_000 ? short(Double(count) / 1000, "k") : short(Double(count) / 1_000_000, "M")
    }

    /// The age of the last change in a few characters: now, 5m, 2h, 3d, then the day (Nov 3) and the year of an
    /// older one (Nov 3, 2024).
    static func age(of text: String?, now: Date = Date(), calendar: Calendar = .current) -> String? {
        guard let date = text?.apiDate else { return nil }
        let seconds = now.timeIntervalSince(date)
        if seconds < 60 { return "now" }
        if seconds < 3600 { return "\(Int(seconds / 60))m" }
        if seconds < 86_400 { return "\(Int(seconds / 3600))h" }
        if seconds < 7 * 86_400 { return "\(Int(seconds / 86_400))d" }
        let sameYear = calendar.component(.year, from: date) == calendar.component(.year, from: now)
        let formatter = DateFormatter()
        formatter.calendar = calendar
        formatter.timeZone = calendar.timeZone
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = sameYear ? "MMM d" : "MMM d, yyyy"
        return formatter.string(from: date)
    }
}

struct GitHubPerson: Codable, Hashable, Sendable {
    var login: String
    var avatarUrl: String? = nil

    /// The address of a small avatar. Only an avatar of GitHub loads.
    var avatar: URL? {
        guard let avatarUrl, var components = URLComponents(string: avatarUrl), components.scheme == "https",
              components.host?.hasSuffix(".githubusercontent.com") == true else { return nil }
        components.queryItems = (components.queryItems ?? []).filter { $0.name != "s" } + [URLQueryItem(name: "s", value: "64")]
        return components.url
    }
}

struct GitHubLabel: Codable, Hashable, Sendable {
    var name: String
    /// Six hex digits, or nil.
    var color: String? = nil
}

struct GitHubChecks: Codable, Equatable, Sendable {
    /// passing, failing or pending.
    var state: String
    var passed = 0
    var failed = 0
    var pending = 0

    var label: String {
        switch state {
        case "passing": "Checks pass"
        case "failing": failed == 1 ? "1 check fails" : "\(failed) checks fail"
        default: pending == 1 ? "1 check runs" : "\(pending) checks run"
        }
    }
}

/// The lines of a code card: at most `limit` lines with their numbers, without the indentation that all of them share.
struct GitHubSnippet: Equatable {
    struct Row: Equatable, Identifiable {
        let number: Int
        let text: String
        var id: Int { number }
    }

    static let limit = 8
    let rows: [Row]
    /// The lines of the range that the card does not show.
    let more: Int

    init?(_ preview: GitHubPreview, limit: Int = GitHubSnippet.limit) {
        guard let start = preview.startLine, let lines = preview.lines, !lines.isEmpty else { return nil }
        let shown = Array(lines.prefix(limit))
        let indent = shown.filter { !$0.allSatisfy(\.isWhitespace) }.map { $0.prefix { $0 == " " }.count }.min() ?? 0
        rows = shown.enumerated().map { Row(number: start + $0.offset, text: String($0.element.dropFirst(min(indent, $0.element.count)))) }
        more = (lines.count - shown.count) + (preview.moreLines ?? 0)
    }
}

/// Which GitHub addresses get a card. The Mac checks the address again (github_preview.parse).
enum GitHubLink {
    /// The most cards under one message.
    static let limit = 4
    private static let reserved: Set<String> = [
        "about", "apps", "collections", "contact", "customer-stories", "enterprise", "events", "explore", "features",
        "issues", "login", "marketplace", "new", "notifications", "orgs", "organizations", "pricing", "pulls", "search",
        "security", "sessions", "settings", "signup", "site", "sponsors", "topics", "trending", "users",
    ]

    /// Whether the address is a pull request, an issue, a commit, a file or a repository of github.com.
    static func isCard(_ url: URL) -> Bool {
        guard url.scheme?.lowercased() == "https", let host = url.host()?.lowercased(),
              host == "github.com" || host == "www.github.com", url.user == nil, url.port == nil,
              url.absoluteString.utf8.count <= 2000 else { return false }
        let parts = url.path().split(separator: "/").map(String.init)
        guard parts.count >= 2, !reserved.contains(parts[0].lowercased()) else { return false }
        if parts.count == 2 { return true }
        switch parts[2] {
        case "pull", "issues":
            return parts.count >= 4 && Int(parts[3]).map { $0 > 0 } == true && parts[3].allSatisfy(\.isNumber)
        case "commit":
            return parts.count == 4 && (7...40).contains(parts[3].count) && parts[3].allSatisfy(\.isHexDigit)
        case "blob":
            return parts.count >= 5 && !parts.contains("..")
        default:
            return false
        }
    }

    /// The GitHub addresses of a text that get a card, in the order of the text, each once, at most `limit`.
    static func links(in text: String) -> [URL] {
        // Most messages name no GitHub page. The check keeps them cheap.
        guard text.range(of: "github.com/", options: .caseInsensitive) != nil,
              let detector = try? NSDataDetector(types: NSTextCheckingResult.CheckingType.link.rawValue) else { return [] }
        var links: [URL] = []
        var seen: Set<String> = []
        for match in detector.matches(in: text, range: NSRange(text.startIndex..., in: text)) {
            guard let url = match.url, isCard(url), seen.insert(url.absoluteString).inserted else { continue }
            links.append(url)
            if links.count == limit { break }
        }
        return links
    }

    /// The route of the Mac for the preview of an address. Each character that is not a letter or a digit is escaped, so
    /// the address stays one query value.
    static func route(_ url: URL) -> String {
        "github/preview?url=" + (url.absoluteString.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? "")
    }
}

/// The GitHub previews that the app loaded. A view reads the cards of a text, and they show when the Mac answers.
/// A preview is fresh for two minutes. An address without a preview (private, missing, no GitHub connection) is
/// asked again after five minutes.
@MainActor
@Observable
final class GitHubEmbeds {
    static let fresh: TimeInterval = 120
    static let retry: TimeInterval = 300
    static let capacity = 300

    private(set) var previews: [String: GitHubPreview] = [:]
    @ObservationIgnored private var loaded: [String: Date] = [:]
    @ObservationIgnored private var failed: [String: Date] = [:]
    @ObservationIgnored private var loading: Set<String> = []
    @ObservationIgnored private var linkCache: [String: [URL]] = [:]
    @ObservationIgnored private var generation = 0
    /// Reads one preview from the Mac. The store sets it.
    @ObservationIgnored var fetch: (@MainActor (URL) async -> GitHubPreview?)?
    @ObservationIgnored var now: () -> Date = Date.init

    /// The GitHub addresses of a text. The app keeps the result, because each redraw of a message asks.
    func links(in text: String) -> [URL] {
        guard text.range(of: "github.com/", options: .caseInsensitive) != nil else { return [] }
        if let links = linkCache[text] { return links }
        if linkCache.count >= 400 { linkCache.removeAll() }
        let links = GitHubLink.links(in: text)
        linkCache[text] = links
        return links
    }

    func preview(for url: URL) -> GitHubPreview? { previews[url.absoluteString] }

    /// The loaded cards of a text, in the order of the text.
    func cards(in text: String) -> [GitHubPreview] { links(in: text).compactMap(preview) }

    /// Loads the previews of the addresses that have none, or an old one. Two messages with one address share a request.
    func load(_ links: [URL]) async {
        guard let fetch else { return }
        let time = now()
        let wanted = links.filter { url in
            let key = url.absoluteString
            if loading.contains(key) { return false }
            if let date = loaded[key], time.timeIntervalSince(date) < Self.fresh { return false }
            if let date = failed[key], time.timeIntervalSince(date) < Self.retry { return false }
            return true
        }
        guard !wanted.isEmpty else { return }
        let started = generation
        wanted.forEach { loading.insert($0.absoluteString) }
        // One at a time on the main actor: a message has a few addresses, and the Mac caches each answer.
        for url in wanted {
            let preview = await fetch(url)
            loading.remove(url.absoluteString)
            guard started == generation else { continue }
            finish(url.absoluteString, preview)
        }
    }

    func load(text: String) async { await load(links(in: text)) }

    private func finish(_ key: String, _ preview: GitHubPreview?) {
        let time = now()
        guard let preview else {
            // A card that showed stays until its next good answer: a short failure of the Mac does not remove it.
            failed[key] = time
            return
        }
        failed[key] = nil
        loaded[key] = time
        if previews[key] != preview {
            if previews[key] == nil, previews.count >= Self.capacity,
               let oldest = loaded.filter({ $0.key != key }).min(by: { $0.value < $1.value })?.key {
                previews[oldest] = nil
                loaded[oldest] = nil
            }
            previews[key] = preview
        }
        if failed.count > Self.capacity { failed.removeAll() }
    }

    /// Forgets each preview, for another Mac or another GitHub account.
    func reset() {
        generation += 1
        previews = [:]
        loaded = [:]
        failed = [:]
        loading = []
    }
}

extension AppStore {
    /// The preview of a GitHub address from the Mac, or nil when the Mac has none: the link then stays plain.
    func githubPreview(_ url: URL) async -> GitHubPreview? {
        if isDemo { return GitHubPreview.demo()[url.absoluteString] }
        let preview: GitHubPreview? = try? await businessRequest(GitHubLink.route(url))
        return preview
    }
}

extension GitHubPreview {
    /// The previews of the demo: Gumroad work, with times near now.
    static func demo(now: Date = Date()) -> [String: GitHubPreview] {
        let formatter = ISO8601DateFormatter()
        func ago(_ minutes: Double) -> String { formatter.string(from: now.addingTimeInterval(-minutes * 60)) }
        let sahil = GitHubPerson(login: "slavingia")
        let gianfranco = GitHubPerson(login: "gianfrancopiana")
        let base = "https://github.com/antiwork/gumroad"
        let previews = [
            GitHubPreview(kind: .pull, url: "\(base)/pull/2314", repository: "antiwork/gumroad", number: 2314,
                          title: "Retry failed Ping webhooks with exponential backoff", state: "open", author: sahil,
                          additions: 184, deletions: 37, changedFiles: 9, comments: 5,
                          checks: GitHubChecks(state: "passing", passed: 14), review: "approved", updatedAt: ago(42)),
            GitHubPreview(kind: .pull, url: "\(base)/pull/2298", repository: "antiwork/gumroad", number: 2298,
                          title: "Hold a payout while its refund is pending", state: "merged", author: gianfranco,
                          additions: 96, deletions: 12, changedFiles: 4, comments: 8, updatedAt: ago(60 * 26)),
            GitHubPreview(kind: .pull, url: "\(base)/pull/2320", repository: "antiwork/gumroad", number: 2320,
                          title: "Upgrade the Stripe SDK to 13.2", state: "draft", author: sahil,
                          additions: 412, deletions: 388, changedFiles: 31, comments: 0,
                          checks: GitHubChecks(state: "failing", passed: 11, failed: 2), review: "changes_requested",
                          updatedAt: ago(95)),
            GitHubPreview(kind: .issue, url: "\(base)/issues/2291", repository: "antiwork/gumroad", number: 2291,
                          title: "Friday payouts are sent twice for creators who sell in two currencies", state: "open",
                          author: sahil, comments: 14, updatedAt: ago(60 * 5),
                          labels: [GitHubLabel(name: "bug", color: "d73a4a"), GitHubLabel(name: "payouts", color: "0e8a16"),
                                   GitHubLabel(name: "priority: high", color: "b60205")],
                          assignees: [gianfranco, sahil]),
            GitHubPreview(kind: .commit, url: "\(base)/commit/9f2c4e1", repository: "antiwork/gumroad",
                          title: "Lock the balance before a payout reads it", author: gianfranco,
                          additions: 12, deletions: 4, changedFiles: 3, updatedAt: ago(60 * 27),
                          sha: "9f2c4e1b7a6d0c35e8f4a1b2c3d4e5f60718293a"),
            GitHubPreview(kind: .repository, url: base, repository: "antiwork/gumroad", updatedAt: ago(30),
                          description: "Sell stuff and see what sticks", stars: 7421, forks: 1502, language: "Ruby",
                          private: false, archived: false),
            GitHubPreview(kind: .file, url: "\(base)/blob/main/app/services/payouts_processor.rb#L41-L48",
                          repository: "antiwork/gumroad", path: "app/services/payouts_processor.rb", ref: "main",
                          startLine: 41, endLine: 48,
                          lines: ["  def process(payment)",
                                  "    payment.user.with_lock do",
                                  "      return if payment.refund_pending?",
                                  "",
                                  "      balance = payment.user.unpaid_balance_cents",
                                  "      transfer = Stripe::Transfer.create(amount: balance, currency: payment.currency)",
                                  "      payment.mark_processing!(transfer.id)",
                                  "    end"],
                          moreLines: 0),
            GitHubPreview(kind: .comment, url: "\(base)/pull/2314#issuecomment-2471930011", repository: "antiwork/gumroad",
                          number: 2314, title: "Retry failed Ping webhooks with exponential backoff", author: gianfranco,
                          updatedAt: ago(18), on: "pull",
                          excerpt: "The backoff caps at six hours, so a seller's endpoint that is down over a weekend still gets "
                              + "the sale on Monday. Can we log the last status code with each retry?"),
        ]
        return Dictionary(uniqueKeysWithValues: previews.map { ($0.url, $0) })
    }
}
