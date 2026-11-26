import SwiftUI

/// An issue or a pull request that Coconut opens itself, in place of github.com: a row of Product or Support, a row of
/// work, a card in a chat, a link in a message. `repository` is owner/name.
struct GitHubItemRoute: Hashable {
    let repository: String
    let number: Int
}

/// A conversation of the support queue that Coconut opens itself, in place of Gmail: a row of Support, or a link to its
/// thread in a message. `id` is the ID of the Gmail thread.
struct SupportThreadRoute: Hashable {
    let id: String
    var subject = ""
}

/// The pages of Coconut that addresses open, so a tap on a link to an issue, a pull request, a support thread or a chat
/// of a team stays in the app. The person answers, closes and merges there; agents do most of that work, and the person
/// does it as the owner. Any other address opens in the browser.
enum InAppLink {
    static func route(_ url: URL) -> (any Hashable)? {
        // The `#` name of a chat in a team chat (TeamChatLinks).
        if let chat = TeamChatLinks.chat(of: url) { return TeamChatRoute(chat: chat) }
        guard url.scheme?.lowercased() == "https", let host = url.host()?.lowercased() else { return nil }
        if host == "github.com" || host == "www.github.com" {
            let parts = url.path().split(separator: "/").map(String.init)
            guard GitHubLink.isCard(url), parts.count >= 4, parts[2] == "issues" || parts[2] == "pull",
                  let number = Int(parts[3]) else { return nil }
            return GitHubItemRoute(repository: parts[0] + "/" + parts[1], number: number)
        }
        // A thread of Gmail: https://mail.google.com/mail/u/0/#all/18c2f0a1b2c3d4e5. A newer ID of Gmail's web pages
        // (FMfcgz…) names no thread of the API, so it opens in the browser.
        if host == "mail.google.com", let thread = url.fragment()?.split(separator: "/").last.map(String.init),
           (8...32).contains(thread.count), thread.allSatisfy(\.isHexDigit) {
            return SupportThreadRoute(id: thread.lowercased())
        }
        return nil
    }
}

extension View {
    /// Pushes routes onto the navigation stack of the view, and opens each link of an issue, a pull request or a support
    /// thread as a page of that stack instead of in the browser (InAppLink). A Link, a link in a message and a card
    /// all open through `openURL`, so each stack of the apps wears this, with InAppDestinations on its root. On an
    /// iPhone, any other web page opens in the browser of the app (iOS 26), so the person stays in Coconut.
    func stackRoutes(_ push: @escaping (any Hashable) -> Void) -> some View {
        environment(\.pushRoute, push)
            .environment(\.openURL, OpenURLAction { url in
                if let route = InAppLink.route(url) {
                    push(route)
                    return .handled
                }
                #if os(iOS)
                if #available(iOS 26.0, *), ["http", "https"].contains(url.scheme?.lowercased() ?? "") {
                    return .systemAction(url, prefersInApp: true)
                }
                #endif
                return .systemAction
            })
    }
}

/// The pages that the links of a stack open (stackRoutes).
struct InAppDestinations: ViewModifier {
    func body(content: Content) -> some View {
        content
            .navigationDestination(for: GitHubItemRoute.self) { GitHubItemView(route: $0).inlineNavigationTitle() }
            .navigationDestination(for: SupportThreadRoute.self) {
                SupportConversationView(id: $0.id, subject: $0.subject).inlineNavigationTitle()
            }
            .navigationDestination(for: RiskReviewRoute.self) { RiskReviewView(route: $0).inlineNavigationTitle() }
            .navigationDestination(for: SupportDeckRoute.self) { SupportDeckView(start: $0.start).inlineNavigationTitle() }
    }
}

/// An issue or a pull request as the Mac reads it (backend/coconut/github_items.py): its text, people and labels, the
/// newest comments, and for a pull request its checks, review, merge state and changes.
struct GitHubItem: Decodable, Equatable, Sendable {
    struct Entry: Decodable, Equatable, Sendable, Identifiable {
        let id: String
        /// comment or review.
        let kind: String
        let author: GitHubPerson?
        let body: String
        let createdAt: String?
        var updatedAt: String? = nil
        /// A review: approved, changes_requested, commented or dismissed.
        var state: String? = nil
    }

    struct Pull: Decodable, Equatable, Sendable {
        let draft: Bool
        let merged: Bool
        var mergedAt: String? = nil
        var mergedBy: String? = nil
        /// GitHub's answer whether the head merges without conflicts. Nil while GitHub computes it.
        var mergeable: Bool? = nil
        /// clean, unstable, blocked, behind, dirty, draft, has_hooks or unknown.
        var mergeableState: String? = nil
        var headSha: String? = nil
        let headRef: String
        let baseRef: String
        var additions: Int? = nil
        var deletions: Int? = nil
        var changedFiles: Int? = nil
        var commits: Int? = nil
        var checks: GitHubChecks? = nil
        var review: String? = nil
        /// How this account merges here: squash, merge or rebase. Nil when it cannot merge.
        var mergeMethod: String? = nil
    }

    /// issue or pull.
    let kind: String
    let repository: String
    let number: Int
    let title: String
    let body: String
    /// open, closed or merged.
    var state: String
    var stateReason: String? = nil
    let author: GitHubPerson?
    let labels: [GitHubLabel]
    let assignees: [GitHubPerson]
    let createdAt: String?
    var updatedAt: String? = nil
    var closedAt: String? = nil
    var url: String? = nil
    /// The login of the connected account: the person.
    var viewer: String? = nil
    var comments: [Entry]
    /// Older comments that the page leaves out.
    var moreComments = 0
    var pull: Pull? = nil

    var isPull: Bool { kind == "pull" }
    var isOpen: Bool { state == "open" }

    /// The word of the state, as GitHub says it.
    var stateLabel: String {
        switch (state, pull?.draft == true) {
        case ("open", true): "Draft"
        case ("open", _): "Open"
        case ("merged", _): "Merged"
        default: stateReason == "not_planned" ? "Not planned" : "Closed"
        }
    }

    var stateSymbol: String {
        switch (isPull, state) {
        case (true, "merged"): "arrow.triangle.merge"
        case (true, _): "arrow.triangle.pull"
        case (false, "open"): "smallcircle.filled.circle"
        default: stateReason == "not_planned" ? "slash.circle" : "checkmark.circle"
        }
    }

    var stateTint: Color {
        switch state {
        case "open": pull?.draft == true ? .secondary : BoardStyle.positive
        case "merged": GitHubTint.merged
        default: isPull || stateReason == "not_planned" ? .secondary : GitHubTint.merged
        }
    }

    /// The words of the merge state of an open pull request, and whether it waits on something.
    var mergeSummary: (text: String, ready: Bool)? {
        guard let pull, isOpen else { return nil }
        if pull.draft { return ("Draft, not ready to merge", false) }
        switch pull.mergeableState {
        case "clean", "has_hooks": return ("Ready to merge", true)
        case "unstable": return ("Checks fail, but GitHub allows the merge", true)
        case "behind": return ("Behind \(pull.baseRef); GitHub may ask to update the branch", true)
        case "blocked": return ("Blocked by a required review or check", false)
        case "dirty": return ("Conflicts with \(pull.baseRef)", false)
        default: return ("GitHub is checking whether it merges", pull.mergeable == true)
        }
    }

    var link: URL? { url.flatMap(URL.init(string:)) }
}

/// A changed file of a pull request with its patch, as the Mac reads it.
struct GitHubChangedFile: Decodable, Equatable, Sendable, Identifiable {
    let path: String
    var previousPath: String? = nil
    /// added, removed, modified, renamed, copied, changed or unchanged.
    let status: String
    let additions: Int
    let deletions: Int
    var patch: String? = nil
    /// The patch is too large to show here.
    var tooLarge = false
    var id: String { path }
}

private struct GitHubFileList: Decodable, Sendable { let files: [GitHubChangedFile] }
private struct GitHubCommentRequest: Encodable { let repository: String; let number: Int; let body: String }
private struct GitHubStateRequest: Encodable { let repository: String; let number: Int; let state: String; let reason: String? }
private struct GitHubMergeRequest: Encodable { let repository: String; let number: Int; let sha: String }

extension AppStore {
    private func githubQuery(_ route: GitHubItemRoute) -> String {
        var components = URLComponents()
        components.queryItems = [URLQueryItem(name: "repository", value: route.repository), URLQueryItem(name: "number", value: String(route.number))]
        return components.percentEncodedQuery ?? ""
    }

    func githubItem(_ route: GitHubItemRoute) async throws -> GitHubItem {
        if isDemo { return GitHubDemo.item(route) }
        return try await businessRequest("github/item?" + githubQuery(route))
    }

    func githubItemFiles(_ route: GitHubItemRoute) async throws -> [GitHubChangedFile] {
        if isDemo { return GitHubDemo.files(route) }
        let list: GitHubFileList = try await businessRequest("github/item/files?" + githubQuery(route))
        return list.files
    }

    /// Posts the person's comment. The key stays with one text, so a Send again after a lost answer posts nothing new.
    func commentOnGitHubItem(_ route: GitHubItemRoute, body: String, requestId: String) async throws -> GitHubItem.Entry {
        if isDemo { return GitHubDemo.comment(route, body: body) }
        let entry: GitHubItem.Entry = try await businessRequest("github/item/comments", method: "POST",
            body: APIJSON.encode(GitHubCommentRequest(repository: route.repository, number: route.number, body: body)), requestId: requestId)
        businessChanges += 1
        return entry
    }

    /// Closes or reopens an issue or a pull request. `reason` is completed or not_planned for an issue.
    func setGitHubItemState(_ route: GitHubItemRoute, state: String, reason: String? = nil) async throws -> GitHubItem {
        if isDemo { return GitHubDemo.setState(route, state: state, reason: reason) }
        let item: GitHubItem = try await businessRequest("github/item", method: "PATCH",
            body: APIJSON.encode(GitHubStateRequest(repository: route.repository, number: route.number, state: state, reason: reason)),
            requestId: UUID().uuidString)
        businessChanges += 1
        return item
    }

    /// Merges the head that the person saw. GitHub refuses when a newer commit arrived since.
    func mergeGitHubPull(_ route: GitHubItemRoute, sha: String) async throws -> GitHubItem {
        if isDemo { return GitHubDemo.merge(route) }
        let item: GitHubItem = try await businessRequest("github/item/merge", method: "POST",
            body: APIJSON.encode(GitHubMergeRequest(repository: route.repository, number: route.number, sha: sha)),
            requestId: UUID().uuidString)
        businessChanges += 1
        return item
    }
}

/// The issues and pull requests of the demo: Gumroad work behind the rows of Product, Support and the chats, which
/// changes in memory when the person comments, closes or merges.
@MainActor
enum GitHubDemo {
    private static var items: [GitHubItemRoute: GitHubItem] = [:]
    static let viewer = GitHubPerson(login: "slavingia")

    static func item(_ route: GitHubItemRoute, now: Date = .now) -> GitHubItem {
        if let item = items[route] { return item }
        let item = make(route, now: now)
        items[route] = item
        return item
    }

    static func comment(_ route: GitHubItemRoute, body: String) -> GitHubItem.Entry {
        var shown = item(route)
        let entry = GitHubItem.Entry(id: UUID().uuidString, kind: "comment", author: viewer, body: body,
                                     createdAt: Date.now.formatted(.iso8601))
        shown.comments.append(entry)
        items[route] = shown
        return entry
    }

    static func setState(_ route: GitHubItemRoute, state: String, reason: String?) -> GitHubItem {
        var shown = item(route)
        shown.state = state
        shown.stateReason = state == "closed" ? (shown.isPull ? nil : reason ?? "completed") : "reopened"
        items[route] = shown
        return shown
    }

    static func merge(_ route: GitHubItemRoute) -> GitHubItem {
        var shown = item(route)
        shown.state = "merged"
        shown.pull?.mergedBy = viewer.login
        shown.pull?.mergedAt = Date.now.formatted(.iso8601)
        items[route] = shown
        return shown
    }

    static func files(_ route: GitHubItemRoute) -> [GitHubChangedFile] {
        [GitHubChangedFile(path: "app/services/ping_webhook_retrier.rb", status: "added", additions: 41, deletions: 0, patch: """
            @@ -0,0 +1,41 @@
            +# frozen_string_literal: true
            +
            +class PingWebhookRetrier
            +  MAX_ATTEMPTS = 8
            +  CAP = 6.hours
            +
            +  def initialize(delivery)
            +    @delivery = delivery
            +  end
            +
            +  def retry_later
            +    return if @delivery.attempts >= MAX_ATTEMPTS
            +
            +    wait = [2**@delivery.attempts * 30.seconds, CAP].min
            +    PingWebhookJob.set(wait:).perform_async(@delivery.id)
            +  end
            +end
            """),
         GitHubChangedFile(path: "app/jobs/ping_webhook_job.rb", status: "modified", additions: 9, deletions: 4, patch: """
            @@ -12,10 +12,15 @@ class PingWebhookJob
               def perform(delivery_id)
                 delivery = PingDelivery.find(delivery_id)
            -    response = HTTParty.post(delivery.url, body: delivery.payload, timeout: 5)
            -    delivery.update!(status: response.code)
            +    response = HTTParty.post(delivery.url, body: delivery.payload, timeout: 10)
            +    delivery.update!(status: response.code, attempts: delivery.attempts + 1)
            +    PingWebhookRetrier.new(delivery).retry_later unless response.success?
            +  rescue Net::OpenTimeout, Net::ReadTimeout
            +    delivery.increment!(:attempts)
            +    PingWebhookRetrier.new(delivery).retry_later
               end
             end
            """),
         GitHubChangedFile(path: "spec/services/ping_webhook_retrier_spec.rb", status: "added", additions: 134, deletions: 0, tooLarge: true)]
    }

    private static func make(_ route: GitHubItemRoute, now: Date) -> GitHubItem {
        func ago(_ minutes: Double) -> String { now.addingTimeInterval(-minutes * 60).formatted(.iso8601) }
        let lin = GitHubPerson(login: "lin"), gianfranco = GitHubPerson(login: "gianfrancopiana"), gumclaw = GitHubPerson(login: "gumclaw")
        let pulls: [Int: (String, String, Int, Int, String, String?)] = [
            4731: ("Retry failed Ping webhooks with backoff", "lin", 214, 38, "clean", "approved"),
            4728: ("Upgrade the Stripe SDK", "grace", 96, 71, "dirty", "changes_requested"),
            4722: ("Show refunds in the payout export", "ada", 148, 12, "blocked", nil),
            4719: ("Faster Discover search for accented names", "sam", 61, 9, "draft", nil),
            912: ("Invoice approval reminders", "alex", 187, 44, "blocked", "review_requested"),
            907: ("Fix rounding of equity grants", "lin", 23, 19, "clean", "approved"),
            2314: ("Retry failed Ping webhooks with exponential backoff", "slavingia", 184, 37, "clean", "approved"),
            2320: ("Upgrade the Stripe SDK to 13.2", "slavingia", 412, 388, "dirty", "changes_requested")]
        let issues: [Int: String] = [
            4712: "Checkout fails for cards that need 3D Secure", 4721: "Payout export misses refunds",
            4730: "Discover search drops accented names",
            2291: "Friday payouts are sent twice for creators who sell in two currencies"]
        if let pull = pulls[route.number] {
            let author = GitHubPerson(login: pull.1)
            let draft = pull.4 == "draft"
            let checks = pull.4 == "dirty" ? GitHubChecks(state: "failing", passed: 11, failed: 2)
                : pull.4 == "blocked" ? GitHubChecks(state: "pending", passed: 9, pending: 3) : GitHubChecks(state: "passing", passed: 14)
            return GitHubItem(
                kind: "pull", repository: route.repository, number: route.number, title: pull.0,
                body: "Ping deliveries that time out now retry with exponential backoff, capped at six hours, so a seller's endpoint that is down over a weekend still gets each sale.\n\n- Retries up to 8 times\n- Logs the status code of each attempt\n- Adds `PingWebhookRetrier` with specs",
                state: "open", author: author, labels: [GitHubLabel(name: "payments", color: "0e8a16")], assignees: [author],
                createdAt: ago(60 * 5), updatedAt: ago(42), viewer: viewer.login,
                comments: [
                    .init(id: "c1", kind: "comment", author: gumclaw, body: "Ran the webhook suite against staging: 312 deliveries, 4 timeouts, each retried and delivered within 9 minutes.", createdAt: ago(60 * 3)),
                    .init(id: "r1", kind: "review", author: gianfranco, body: pull.5 == "changes_requested" ? "The new client drops the idempotency key on refunds. Can you pass it through?" : "Looks good. The cap keeps a dead endpoint from piling up jobs.", createdAt: ago(90), state: pull.5 ?? "commented"),
                    .init(id: "c2", kind: "comment", author: lin, body: "Logged the last status code with each retry, as asked.", createdAt: ago(44))],
                pull: .init(draft: draft, merged: false, mergeable: pull.4 != "dirty", mergeableState: draft ? "draft" : pull.4,
                            headSha: String(repeating: "4", count: 40), headRef: "ping-backoff", baseRef: "main", additions: pull.2,
                            deletions: pull.3, changedFiles: 3, commits: 4, checks: checks, review: pull.5,
                            mergeMethod: "squash"))
        }
        // A row of work of Accounting or Marketing opens its issue with its own words.
        if let work = BusinessWork.previews.first(where: { URL(string: $0.url).flatMap(InAppLink.route) as? GitHubItemRoute == route }) {
            return GitHubItem(kind: "issue", repository: route.repository, number: route.number, title: work.title, body: work.body,
                              state: work.state, author: viewer, labels: work.labels.map { GitHubLabel(name: $0, color: "6f55a5") },
                              assignees: [viewer], createdAt: ago(60 * 30), updatedAt: ago(60 * 3), viewer: viewer.login,
                              comments: [.init(id: "c1", kind: "comment", author: gumclaw, body: "Started on it. The ledger through October balances; November waits on two Stripe payouts.", createdAt: ago(60 * 3))])
        }
        // A to-do of the demo opens with its own words (TodoIssues.preview).
        if let todo = TodoIssues.preview.items.first(where: { $0.number == route.number }) {
            return GitHubItem(
                kind: "issue", repository: route.repository, number: route.number, title: todo.title,
                body: "Gumclaw filed this to-do while it worked the queue. It needs a person before it can go on.\n\nComment here, or close the issue when it is done.",
                state: "open", author: gumclaw, labels: todo.labels, assignees: todo.assignees.map { GitHubPerson(login: $0) },
                createdAt: todo.createdAt, updatedAt: todo.updatedAt, viewer: viewer.login,
                comments: [.init(id: "c1", kind: "comment", author: gumclaw, body: "Checked it again this morning. Nothing changed since yesterday.", createdAt: ago(60 * 3))])
        }
        let title = issues[route.number] ?? "Issue \(route.number)"
        return GitHubItem(
            kind: "issue", repository: route.repository, number: route.number, title: title,
            body: "Sellers report that buyers whose bank asks for 3D Secure see \"Payment failed\" after they approve the charge. Stripe shows the payment as succeeded.\n\n**Steps**\n1. Buy a product with the card 4000 0027 6000 3184\n2. Approve the challenge\n3. The receipt page shows an error\n\nSeen on 14 purchases since Monday.",
            state: "open", author: gumclaw, labels: [GitHubLabel(name: "bug", color: "d73a4a"), GitHubLabel(name: "awaiting-human", color: "d93f0b")],
            assignees: [viewer], createdAt: ago(60 * 26), updatedAt: ago(60 * 2), viewer: viewer.login,
            comments: [
                .init(id: "c1", kind: "comment", author: gumclaw, body: "The confirm call returns before the webhook marks the purchase paid, so the page polls a purchase that is still pending. A fix waits on one choice: show the receipt at once and update it, or wait up to 10 seconds for the webhook.", createdAt: ago(60 * 20)),
                .init(id: "c2", kind: "comment", author: gianfranco, body: "Waiting reads better for buyers. Most webhooks arrive in under 2 seconds.", createdAt: ago(60 * 4))],
            moreComments: 0)
    }
}
