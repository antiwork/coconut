import SwiftUI

/// The customer conversations of Support that the person hands to Gumclaw (backend/coconut/support_handoffs.py).
/// Gumclaw answers the support mailbox and leaves open the conversations that wait for a person. The page of one offers
/// how to respond as the options of a poll, beside the box for the person's own words (an answer, or a request to an
/// agent): Let Gumclaw answer, Refund and answer, or Close without answer. The first two go to Gumclaw as a risk review
/// does: the Mac files the conversation as an issue of gumroad-private with the label `support` and an `@gumclaw` line,
/// and Gumclaw answers the customer, reports in the issue and closes it with the conversation.
struct SupportHandoffList: Codable, Equatable, Sendable {
    /// owner/name of the issues: antiwork/gumroad-private.
    var repository: String
    var handoffs: [SupportHandoff]

    func issue(of thread: String) -> RiskReview.Issue? {
        handoffs.first { $0.thread == thread.lowercased() }?.issue
    }
}

struct SupportHandoff: Codable, Equatable, Sendable {
    /// The ID of the Gmail thread.
    var thread: String
    /// The issue of the conversation in the work repository, as a risk review has one.
    var issue: RiskReview.Issue
}

/// How the person can respond. Gumclaw carries out the first two in the issue of the conversation; the Mac closes.
enum SupportChoice: String, CaseIterable, Identifiable, Sendable {
    case gumclaw, refund, close

    var id: String { rawValue }

    var label: String {
        switch self {
        case .gumclaw: "Let Gumclaw answer"
        case .refund: "Refund and answer"
        case .close: "Close without answer"
        }
    }

    var explanation: String {
        switch self {
        case .gumclaw: "From the support mailbox."
        case .refund: "Gumclaw refunds the purchase and tells the customer."
        case .close: "Nothing goes to the customer."
        }
    }
}

private struct SupportHandoffRequest: Encodable {
    let thread: String
    let decision: String
    let subject: String
}

private struct SupportHandoffResult: Decodable, Sendable {
    let thread: String
    let issue: RiskReview.Issue
}

extension AppStore {
    /// Reads the issues of the conversations that went to Gumclaw. A failure keeps the list that shows.
    func loadSupportHandoffs(fresh: Bool = false) async {
        if isDemo {
            if supportHandoffs == nil { supportHandoffs = SupportHandoffList(repository: "antiwork/gumroad-private", handoffs: []) }
            return
        }
        guard connection == .connected else { return }
        do {
            let list: SupportHandoffList = try await businessRequest("support/handoffs" + (fresh ? "?fresh=1" : ""))
            if list != supportHandoffs { supportHandoffs = list }
        } catch {}
    }

    /// Hands the conversation to Gumclaw with the person's choice, and returns its issue. One choice keeps one request
    /// key, so a retry after a lost answer writes nothing twice (support_handoffs.py).
    func handOffSupport(_ thread: String, subject: String, _ choice: SupportChoice, requestId: String) async throws -> RiskReview.Issue {
        let issue: RiskReview.Issue
        if isDemo {
            issue = SupportHandoffDemo.decide(thread, subject: subject, choice)
        } else {
            let result: SupportHandoffResult = try await businessRequest("support/handoffs", method: "POST",
                body: APIJSON.encode(SupportHandoffRequest(thread: thread, decision: choice.rawValue, subject: subject)),
                requestId: requestId)
            issue = result.issue
        }
        var list = supportHandoffs ?? SupportHandoffList(repository: "antiwork/gumroad-private", handoffs: [])
        list.handoffs.removeAll { $0.thread == thread.lowercased() }
        list.handoffs.insert(SupportHandoff(thread: thread.lowercased(), issue: issue), at: 0)
        supportHandoffs = list
        // The issue is a to-do of Support until Gumclaw closes it.
        if !isDemo { businessChanges += 1 }
        return issue
    }
}

/// How the person responds to a conversation, as the options of a poll, and the issue of the conversation once Gumclaw
/// has it, with Gumclaw's newest words. A choice shows as chosen at once and goes out after the Undo time of a card
/// (AppStore.answerUndoSeconds), so a wrong tap costs nothing. Refund asks first. On a card of the support deck
/// (SupportDeckView), `decide` takes the choice instead: the card leaves at once and the deck holds the Undo.
struct SupportChoices: View {
    @Environment(AppStore.self) private var store
    @Environment(\.pushRoute) private var pushRoute
    /// The ID of the Gmail thread.
    let thread: String
    let subject: String
    /// The newest message is the customer's, so the conversation waits for a response.
    let waiting: Bool
    /// The support mailbox of this Mac closes conversations, so Close is a choice.
    let canClose: Bool
    /// Closes the conversation (SupportConversationView), which shows a failure itself.
    let close: () async -> Void
    /// Reads the conversation again, once Gumclaw answered.
    let reload: () -> Void
    /// Takes the choice in place of this view, which then neither waits nor sends (SupportDeckView).
    var decide: ((SupportChoice) -> Void)? = nil
    @State private var report: GitHubItem?
    @State private var chosen: SupportChoice?
    /// The choice that this page sent, which stays marked as a poll's vote does.
    @State private var sent: SupportChoice?
    @State private var sending: SupportChoice?
    @State private var pending: Task<Void, Never>?
    @State private var confirming = false
    @State private var error: String?

    private var issue: RiskReview.Issue? { store.supportHandoffs?.issue(of: thread) }
    private var repository: String { store.supportHandoffs?.repository ?? "antiwork/gumroad-private" }
    private var choices: [SupportChoice] { canClose ? SupportChoice.allCases : [.gumclaw, .refund] }

    var body: some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.m) {
            // Gumclaw's newest words lead once it has the conversation: they say what it sent.
            if let issue { gumclaw(issue) }
            if waiting {
                VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
                    if let chosen, pending != nil {
                        HStack(spacing: BoardStyle.Space.m) {
                            Label(chosen.label, systemImage: "checkmark.circle.fill")
                                .font(.subheadline.weight(.semibold)).lineLimit(2)
                                .frame(maxWidth: .infinity, alignment: .leading)
                            Button("Undo") { undo() }.buttonStyle(.bordered).frame(minHeight: 44)
                                .accessibilityIdentifier("support-choice-undo")
                        }
                        .accessibilityElement(children: .combine)
                        .accessibilityAction(named: "Undo") { undo() }
                    }
                    ForEach(choices) { choice in row(choice) }
                }
            }
            if let error {
                Text(error).font(.subheadline).foregroundStyle(BoardStyle.destructive).fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("support-choice-error")
            }
        }
        .task(id: issue?.number) { await follow() }
        .onDisappear {
            // A choice still in its Undo time goes out when the page closes, as a card's answer does.
            if let chosen, pending != nil { pending?.cancel(); pending = nil; Task { await commit(chosen) } }
        }
    }

    private func row(_ choice: SupportChoice) -> some View {
        Button {
            if choice == .refund { confirming = true } else { choose(choice) }
        } label: {
            DecisionChoiceContent(option: DecisionOption(id: choice.rawValue, label: choice.label,
                                                         explanation: choice.explanation, answer: "approve"),
                                  isSending: sending == choice, isSelected: (chosen ?? sent) == choice)
        }
        .buttonStyle(HomePressStyle())
        .disabled(chosen != nil || sending != nil)
        .accessibilityIdentifier("support-choice-" + choice.rawValue)
        .accessibilityHint(choice.explanation)
        // Refund asks first, from its own row, so the question points at the choice.
        .confirmationDialog("Refund this customer?", isPresented: choice == .refund ? $confirming : .constant(false),
                            titleVisibility: .visible) {
            Button("Refund and answer", role: .destructive) { choose(.refund) }
                .accessibilityIdentifier("support-refund-confirm")
        } message: {
            Text("Gumclaw refunds the purchase of this conversation, tells the customer and reports in its issue.")
        }
    }

    /// The issue of the conversation, and its newest comment: Gumclaw's report once it answered.
    private func gumclaw(_ issue: RiskReview.Issue) -> some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
            Button { pushRoute?(GitHubItemRoute(repository: repository, number: issue.number)) } label: {
                HStack(spacing: BoardStyle.Space.s) {
                    Image(systemName: issue.state == "open" ? AgentTodos.symbol("working") : "checkmark.circle")
                        .foregroundStyle(issue.state == "open" ? BoardStyle.positive : GitHubTint.merged)
                    Text(issue.state == "open" ? "With Gumclaw" : "Done by Gumclaw").font(.subheadline.weight(.semibold))
                    Text(verbatim: "#\(issue.number)").font(.subheadline.monospacedDigit()).foregroundStyle(.secondary)
                    Spacer(minLength: 0)
                    Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
                }
                .frame(minHeight: 44).contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("support-handoff-issue")
            if let entry = report?.comments.last {
                GitHubEntryTile(author: entry.author, verb: "wrote", date: entry.createdAt, text: entry.body,
                                own: entry.author?.login == report?.viewer)
            }
        }
    }

    private func choose(_ choice: SupportChoice) {
        guard chosen == nil, sending == nil else { return }
        if let decide { decide(choice); return }
        chosen = choice
        error = nil
        pending = Task {
            try? await Task.sleep(for: .seconds(AppStore.answerUndoSeconds))
            guard !Task.isCancelled else { return }
            pending = nil
            await commit(choice)
        }
    }

    private func undo() {
        pending?.cancel()
        pending = nil
        chosen = nil
    }

    private func commit(_ choice: SupportChoice) async {
        sending = choice
        defer { sending = nil; chosen = nil }
        if choice == .close {
            await close()
            return
        }
        do {
            _ = try await store.handOffSupport(thread, subject: subject, choice, requestId: UUID().uuidString)
            sent = choice
            error = nil
            await loadReport()
        } catch is CancellationError {
            error = "The choice was not confirmed. Choose it again: it goes out once."
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func loadReport() async {
        guard let issue else { report = nil; return }
        if store.isDemo { report = SupportHandoffDemo.thread(thread, subject: subject, issue: issue, repository: repository); return }
        if let item = try? await store.githubItem(GitHubItemRoute(repository: repository, number: issue.number)) {
            report = item
        }
    }

    /// Reads the issue while Gumclaw has the conversation, so its report and its answer show here without a refresh.
    private func follow() async {
        if store.supportHandoffs == nil { await store.loadSupportHandoffs() }
        await loadReport()
        var reads = 0
        while !Task.isCancelled, issue?.state == "open", reads < 40 {
            try? await Task.sleep(for: .seconds(15))
            guard !Task.isCancelled else { return }
            await loadReport()
            reads += 1
            if report?.state == "closed" {
                await store.loadSupportHandoffs(fresh: true)
                reload()
                return
            }
        }
    }
}

/// The handoffs of the demo and the screenshots, which change in memory when the person chooses.
@MainActor
enum SupportHandoffDemo {
    static var choices: [String: SupportChoice] = [:]

    static func decide(_ thread: String, subject: String, _ choice: SupportChoice) -> RiskReview.Issue {
        choices[thread.lowercased()] = choice
        return RiskReview.Issue(number: 4961, state: "open", title: "Support: " + subject,
                                url: "https://github.com/antiwork/gumroad-private/issues/4961",
                                updatedAt: Date.now.formatted(.iso8601), comments: 1)
    }

    static func thread(_ thread: String, subject: String, issue: RiskReview.Issue, repository: String, now: Date = .now) -> GitHubItem {
        func ago(_ minutes: Double) -> String { now.addingTimeInterval(-minutes * 60).formatted(.iso8601) }
        let gumclaw = GitHubPerson(login: "gumclaw"), person = GitHubPerson(login: "slavingia")
        let conversation = "the Gmail thread `\(thread)` (\"\(subject)\")"
        let line: String = switch choices[thread.lowercased()] ?? .gumclaw {
        case .refund: "refund the purchase that \(conversation) is about and tell the customer from the support mailbox. Report the refund and the answer here"
        default: "answer the customer in \(conversation) from the support mailbox. Decide what to tell them from what you find, and report what you sent and why here"
        }
        return GitHubItem(kind: "issue", repository: repository, number: issue.number, title: issue.title, body: "",
                          state: issue.state, author: person, labels: [GitHubLabel(name: "support")], assignees: [],
                          createdAt: ago(1), viewer: "slavingia", comments: [
            .init(id: "c1", kind: "comment", author: person, body: "@gumclaw, \(line), then mark the conversation done and close this issue.", createdAt: ago(1)),
            .init(id: "c2", kind: "comment", author: gumclaw, body: "On it. Both payouts go out together this Friday as one transfer of $1,912.40, so I'm telling Mira that, with the day her bank shows it.", createdAt: ago(0.5))])
    }
}
