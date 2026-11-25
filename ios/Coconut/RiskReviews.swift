import SwiftUI

/// The risk reviews past SLA of Finance (backend/coconut/risk_reviews.py). Gumroad pays a seller only after a risk review
/// of the account, and a review is past SLA when the oldest unpaid balance of an unreviewed account that holds over $100
/// is older than 7 days. The dashboards publisher lists those accounts, and Finance shows each one as a row of its list,
/// in place of the one row that only counted them, so the person decides each review in the app.
///
/// A decision goes to Gumclaw, the agent of the Gumroad project, which holds the audited admin access: the Mac files the
/// review as an issue of gumroad-private with the label `finance` and an `@gumclaw` line, and Gumclaw acts, reports in
/// the issue and closes it. The page of a review shows that thread under the choices.
struct RiskReviewList: Codable, Equatable, Sendable {
    /// The publisher lists the accounts. An older publisher has only the count of the overview, which Finance then
    /// shows as one row.
    var available: Bool
    /// owner/name of the issues: antiwork/gumroad-private.
    var repository: String
    var generatedAt: String?
    var slaDays: Int
    /// Oldest unpaid balance first.
    var reviews: [RiskReview]

    /// The file of the last list, so the rows and the count of Finance show at the next start before the Mac answers.
    static let cache = "risk-reviews.json"
    /// A list older than this proves nothing about now, as an old overview proves no breach (AttentionRules).
    static let freshness: TimeInterval = 3 * 3600

    /// The reviews that Finance lists now: nil when it shows the count of the overview instead.
    func current(now: Date = .now) -> [RiskReview]? {
        guard available, !reviews.isEmpty, let date = generatedAt?.apiDate,
              now.timeIntervalSince(date) < Self.freshness else { return nil }
        return reviews
    }
}

struct RiskReview: Codable, Identifiable, Hashable, Sendable {
    /// The issue of the review in the work repository, which Gumclaw answers and closes.
    struct Issue: Codable, Hashable, Sendable {
        var number: Int
        var state: String
        var title: String
        var url: String?
        var updatedAt: String?
        var comments: Int
    }

    /// The external ID of the Gumroad account.
    var user: String
    var name: String
    var username: String
    var email: String
    var balanceCents: Int?
    var unpaidSince: String?
    var days: Int?
    var issue: Issue?

    var id: String { user }
    var title: String { [name, username].first { !$0.isEmpty } ?? "User " + user }
    var held: String { (Double(balanceCents ?? 0) / 100).formatted(.currency(code: "USD")) }
    /// Gumclaw has the decision and has not closed its issue.
    var withGumclaw: Bool { issue?.state == "open" }
    /// Gumclaw closed the issue of the decision. The account leaves the list at the next publish.
    var decided: Bool { issue?.state == "closed" }

    /// "$412.18 held · 11 days", then where the decision is: "With Gumclaw · #5012".
    func detail() -> String {
        var parts = [held + " held"]
        if let days { parts.append(days == 1 ? "1 day" : "\(days) days") }
        if let issue {
            // A number of GitHub is a name, so it has no separator of thousands.
            parts.append((issue.state == "open" ? "With Gumclaw · #" : "Decided · #") + String(issue.number))
        }
        return parts.joined(separator: " · ")
    }

    var symbol: String { withGumclaw ? AgentTodos.symbol("working") : decided ? "checkmark.circle" : "shield.lefthalf.filled" }
    var tint: Color { withGumclaw ? BoardStyle.positive : decided ? .secondary : BoardStyle.destructive }
    var storefront: URL? { username.isEmpty ? nil : URL(string: "https://\(username).gumroad.com") }
    var admin: URL? { URL(string: "https://app.gumroad.com/admin/users/" + user) }
}

/// The page of a review, which a row of Finance opens.
struct RiskReviewRoute: Hashable {
    let user: String
}

/// What the person can decide. Each one is a line to Gumclaw in the issue of the review; Ask sends the person's words.
enum RiskDecision: String, CaseIterable, Identifiable, Sendable {
    case gumclaw, compliant, watch, suspend, ask

    var id: String { rawValue }

    var label: String {
        switch self {
        case .gumclaw: "Let Gumclaw decide"
        case .compliant: "Mark compliant"
        case .watch: "Watch"
        case .suspend: "Suspend for fraud"
        case .ask: "Ask Gumclaw"
        }
    }

    var explanation: String {
        switch self {
        case .gumclaw: "Gumclaw reviews the account and reports why."
        case .compliant: "The held balance pays out on its schedule."
        case .watch: "Gumclaw sets a revenue threshold."
        case .suspend, .ask: ""
        }
    }

    /// The choices of the page, in the order of a poll. Ask is the box under them.
    static let choices: [RiskDecision] = [.gumclaw, .compliant, .watch, .suspend]
}

private struct RiskDecisionRequest: Encodable {
    let user: String
    let decision: String
    let note: String?
}

extension AppStore {
    /// The reviews that Finance lists now (RiskReviewList.current).
    var shownRiskReviews: [RiskReview]? { riskReviews?.current() }

    /// Reads the reviews again. A failure keeps the list that shows.
    func loadRiskReviews(fresh: Bool = false) async {
        if isDemo {
            if riskReviews == nil { riskReviews = RiskReviewDemo.list() }
            return
        }
        guard connection == .connected else { return }
        do {
            let list: RiskReviewList = try await businessRequest("risk/reviews" + (fresh ? "?fresh=1" : ""))
            receiveRiskReviews(list)
        } catch {}
    }

    private func receiveRiskReviews(_ list: RiskReviewList) {
        guard list != riskReviews else { return }
        riskReviews = list
        if !isDemo { try? ProtectedCache.write(list, name: RiskReviewList.cache) }
    }

    /// Sends a decision to Gumclaw through the issue of the review, and returns the review with its issue. One decision
    /// keeps one request key, so a retry after a lost answer writes nothing twice (risk_reviews.py).
    func decideRiskReview(_ review: RiskReview, _ decision: RiskDecision, note: String? = nil, requestId: String) async throws -> RiskReview {
        let decided: RiskReview
        if isDemo {
            decided = RiskReviewDemo.decide(review, decision)
        } else {
            decided = try await businessRequest("risk/reviews", method: "POST",
                body: APIJSON.encode(RiskDecisionRequest(user: review.user, decision: decision.rawValue, note: note)),
                requestId: requestId)
        }
        if var list = riskReviews, let index = list.reviews.firstIndex(where: { $0.user == decided.user }) {
            list.reviews[index].issue = decided.issue
            receiveRiskReviews(list)
        }
        // The issue is a to-do of Finance until Gumclaw closes it.
        if !isDemo { businessChanges += 1 }
        return decided
    }
}

/// A review: the account, the money it holds and since when, the choices as the options of a poll, and the thread of
/// its issue once Gumclaw has it. A choice shows as chosen at once and goes to the Mac after the Undo time of a card
/// (AppStore.answerUndoSeconds), so a wrong tap costs nothing. Suspend asks first. The box at the bottom asks Gumclaw
/// anything about the account in the same issue.
struct RiskReviewView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.pushRoute) private var pushRoute
    @Environment(\.openURL) private var openURL
    let route: RiskReviewRoute
    /// The review as the last decision returned it, for an account that the list no longer has.
    @State private var decided: RiskReview?
    @State private var thread: GitHubItem?
    @State private var chosen: RiskDecision?
    /// The choice that this page sent, which stays marked as a poll's vote does.
    @State private var sent: RiskDecision?
    @State private var sending: RiskDecision?
    @State private var pending: Task<Void, Never>?
    @State private var confirming = false
    @State private var error: String?

    private var review: RiskReview? {
        store.riskReviews?.reviews.first { $0.user == route.user } ?? decided
    }
    private var repository: String { store.riskReviews?.repository ?? "antiwork/gumroad-private" }

    var body: some View {
        BoardPage(spacing: BoardStyle.Space.l) {
            if let review {
                header(review)
                facts(review)
                // Gumclaw's newest words lead once it has the decision: they say what it did.
                if let issue = review.issue { conversation(issue) }
                choices(review)
                if let error {
                    Text(error).font(.subheadline).foregroundStyle(BoardStyle.destructive).fixedSize(horizontal: false, vertical: true)
                        .accessibilityIdentifier("risk-review-error")
                }
            } else if store.riskReviews == nil {
                BoardLoading(title: "Loading review")
            } else {
                Text("This account no longer waits for a review.").font(.subheadline).foregroundStyle(.secondary)
                    .accessibilityIdentifier("risk-review-gone")
            }
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            if review != nil {
                ReplyBox(draftKey: "risk:" + route.user, placeholder: "Ask Gumclaw", identifier: "risk-ask") { text, key in
                    await send(.ask, note: text, key: key)
                }
            }
        }
        .navigationTitle("Risk review")
        .task(id: review?.issue?.number) { await follow() }
        .refreshable {
            await store.loadRiskReviews(fresh: true)
            await loadThread()
        }
        .onDisappear {
            // A choice still in its Undo time goes out when the page closes, as a card's answer does.
            if let chosen, pending != nil { pending?.cancel(); pending = nil; Task { await commit(chosen) } }
        }
    }

    private func header(_ review: RiskReview) -> some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
            Text(review.title).font(.title3.weight(.semibold)).textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true).accessibilityAddTraits(.isHeader)
            let who = [review.name.isEmpty ? "" : review.username, review.email].filter { !$0.isEmpty }
            if !who.isEmpty {
                Text(who.joined(separator: " · ")).font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
                    .textSelection(.enabled)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func facts(_ review: RiskReview) -> some View {
        BoardSection {
            BoardRow(title: "Held", value: review.held, chevron: false)
            BoardRow(title: "Waiting", value: waiting(review), chevron: false)
            if let storefront = review.storefront {
                Button { openURL(storefront) } label: {
                    BoardRow(title: "Storefront", value: storefront.host() ?? "")
                }
                .buttonStyle(.plain).accessibilityIdentifier("risk-review-storefront")
            }
            if let admin = review.admin {
                Button { openURL(admin) } label: { BoardRow(title: "Gumroad admin") }
                    .buttonStyle(.plain).accessibilityIdentifier("risk-review-admin")
            }
        }
    }

    /// "11 days · since Nov 13".
    private func waiting(_ review: RiskReview) -> String {
        let since = review.unpaidSince?.apiDate.map { "since " + $0.formatted(.dateTime.month(.abbreviated).day()) }
        let days = review.days.map { $0 == 1 ? "1 day" : "\($0) days" }
        return [days, since].compactMap { $0 }.joined(separator: " · ")
    }

    @ViewBuilder private func choices(_ review: RiskReview) -> some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
            if let chosen, pending != nil {
                HStack(spacing: BoardStyle.Space.m) {
                    Label(chosen.label, systemImage: "checkmark.circle.fill")
                        .font(.subheadline.weight(.semibold)).lineLimit(2)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Button("Undo") { undo() }.buttonStyle(.bordered).frame(minHeight: 44)
                        .accessibilityIdentifier("risk-review-undo")
                }
                .accessibilityElement(children: .combine)
                .accessibilityAction(named: "Undo") { undo() }
            }
            ForEach(RiskDecision.choices) { decision in
                Button {
                    if decision == .suspend { confirming = true } else { choose(decision) }
                } label: {
                    DecisionChoiceContent(option: DecisionOption(id: decision.rawValue, label: decision.label,
                                                                 explanation: decision.explanation, answer: "approve"),
                                          isSending: sending == decision, isSelected: (chosen ?? sent) == decision)
                }
                .buttonStyle(HomePressStyle())
                .disabled(chosen != nil || sending != nil)
                .accessibilityIdentifier("risk-decision-" + decision.rawValue)
                .accessibilityHint(decision.explanation)
                // Suspend asks first, from its own row, so the question points at the choice.
                .confirmationDialog("Suspend \(review.title) for fraud?",
                                    isPresented: decision == .suspend ? $confirming : .constant(false), titleVisibility: .visible) {
                    Button("Suspend for fraud", role: .destructive) { choose(.suspend) }
                        .accessibilityIdentifier("risk-suspend-confirm")
                } message: {
                    Text("Gumclaw suspends the account and reports in the issue of this review.")
                }
            }
        }
    }

    /// The issue of the review, and its newest comment: Gumclaw's report once it acted.
    @ViewBuilder private func conversation(_ issue: RiskReview.Issue) -> some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
            Button { pushRoute?(GitHubItemRoute(repository: repository, number: issue.number)) } label: {
                HStack(spacing: BoardStyle.Space.s) {
                    Image(systemName: issue.state == "open" ? AgentTodos.symbol("working") : "checkmark.circle")
                        .foregroundStyle(issue.state == "open" ? BoardStyle.positive : GitHubTint.merged)
                    Text(issue.state == "open" ? "With Gumclaw" : "Decided").font(.subheadline.weight(.semibold))
                    Text(verbatim: "#\(issue.number)").font(.subheadline.monospacedDigit()).foregroundStyle(.secondary)
                    Spacer(minLength: 0)
                    Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
                }
                .frame(minHeight: 44).contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("risk-review-issue")
            if let entry = thread?.comments.last {
                GitHubEntryTile(author: entry.author, verb: "wrote", date: entry.createdAt, text: entry.body,
                                own: entry.author?.login == thread?.viewer)
            }
        }
    }

    private func choose(_ decision: RiskDecision) {
        guard chosen == nil, sending == nil else { return }
        chosen = decision
        error = nil
        pending = Task {
            try? await Task.sleep(for: .seconds(AppStore.answerUndoSeconds))
            guard !Task.isCancelled else { return }
            pending = nil
            await commit(decision)
        }
    }

    private func undo() {
        pending?.cancel()
        pending = nil
        chosen = nil
    }

    private func commit(_ decision: RiskDecision) async {
        let failure = await send(decision, note: nil, key: UUID().uuidString)
        chosen = nil
        if let failure { error = failure }
    }

    /// Sends a decision. Nil is success; else the words of the failure.
    private func send(_ decision: RiskDecision, note: String?, key: String) async -> String? {
        guard let review else { return "This account no longer waits for a review." }
        sending = decision
        defer { sending = nil }
        do {
            decided = try await store.decideRiskReview(review, decision, note: note, requestId: key)
            if decision != .ask { sent = decision }
            error = nil
            await loadThread()
            return nil
        } catch is CancellationError {
            return "The decision was not confirmed. Send it again: it goes out once."
        } catch {
            return error.localizedDescription
        }
    }

    private func loadThread() async {
        guard let review, let issue = review.issue else { thread = nil; return }
        if store.isDemo { thread = RiskReviewDemo.thread(review, repository: repository); return }
        if let item = try? await store.githubItem(GitHubItemRoute(repository: repository, number: issue.number)) {
            thread = item
        }
    }

    /// Reads the thread while Gumclaw has the decision, so its report shows here without a refresh.
    private func follow() async {
        if store.riskReviews == nil { await store.loadRiskReviews() }
        await loadThread()
        var reads = 0
        while !Task.isCancelled, review?.issue?.state == "open", reads < 40 {
            try? await Task.sleep(for: .seconds(15))
            guard !Task.isCancelled else { return }
            await loadThread()
            reads += 1
            if thread?.state == "closed" {
                await store.loadRiskReviews(fresh: true)
                return
            }
        }
    }
}

/// Reviews for the demo and the screenshots: Gumroad sellers whose first payout waits past the SLA.
enum RiskReviewDemo {
    static func list(now: Date = .now) -> RiskReviewList {
        func since(_ days: Double) -> String { now.addingTimeInterval(-days * 86_400).formatted(.iso8601) }
        return RiskReviewList(available: true, repository: "antiwork/gumroad-private", generatedAt: now.addingTimeInterval(-6 * 60).formatted(.iso8601),
                              slaDays: 7, reviews: [
            RiskReview(user: "8155930716101", name: "Marisol Duarte", username: "marisolprints", email: "marisol.duarte@example.com",
                       balanceCents: 41_218, unpaidSince: since(11.3), days: 11, issue: nil),
            RiskReview(user: "4321043910336", name: "Tomás Rivera", username: "beatsbytomas", email: "tomas@example.com",
                       balanceCents: 128_450, unpaidSince: since(9.1), days: 9,
                       issue: RiskReview.Issue(number: 4952, state: "open", title: "Risk review: beatsbytomas (4321043910336), $1,284.50 held 9 days",
                                               url: "https://github.com/antiwork/gumroad-private/issues/4952",
                                               updatedAt: now.addingTimeInterval(-4 * 60).formatted(.iso8601), comments: 1)),
            RiskReview(user: "2078417683303", name: "", username: "sortedprintables", email: "hello@example.com",
                       balanceCents: 17_340, unpaidSince: since(8.2), days: 8, issue: nil)])
    }

    /// The last choice of each account in the demo, which the first comment of its thread says.
    @MainActor static var choices: [String: RiskDecision] = [:]

    @MainActor static func decide(_ review: RiskReview, _ decision: RiskDecision) -> RiskReview {
        choices[review.user] = decision
        var decided = review
        decided.issue = review.issue ?? RiskReview.Issue(number: 4958, state: "open", title: "Risk review: \(review.title)",
                                                         url: nil, updatedAt: Date.now.formatted(.iso8601), comments: 0)
        decided.issue?.state = "open"
        return decided
    }

    @MainActor static func thread(_ review: RiskReview, repository: String, now: Date = .now) -> GitHubItem {
        func ago(_ minutes: Double) -> String { now.addingTimeInterval(-minutes * 60).formatted(.iso8601) }
        let gumclaw = GitHubPerson(login: "gumclaw"), person = GitHubPerson(login: "slavingia")
        let number = review.issue?.number ?? 4958
        let account = "`\(review.username)` (user \(review.user))"
        let line: String = switch choices[review.user] ?? .gumclaw {
        case .compliant: "mark \(account) compliant, so its held balance pays out on schedule. Report here"
        case .watch: "add \(account) to the watchlist with a revenue threshold that you choose. Report here"
        case .suspend: "suspend \(account) for fraud. Report here"
        default: "review \(account) and decide whether to mark it compliant, add it to the watchlist or suspend it. Report what you did and why here"
        }
        return GitHubItem(kind: "issue", repository: repository, number: number, title: review.issue?.title ?? review.title,
                          body: "", state: review.issue?.state ?? "open", author: person, labels: [GitHubLabel(name: "finance")],
                          assignees: [], createdAt: ago(9), viewer: "slavingia", comments: [
            .init(id: "c1", kind: "comment", author: person, body: "@gumclaw, \(line), and close this issue when it is done.", createdAt: ago(9)),
            .init(id: "c2", kind: "comment", author: gumclaw, body: "On it. 212 sales from 198 buyers since Oct 30, no disputes or early fraud warnings, Stripe identity verified. Reading the products and the buyers' cards now.", createdAt: ago(4))])
    }
}
