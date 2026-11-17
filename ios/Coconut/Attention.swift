import SwiftUI

/// Where a row of Home leads. The iPhone tabs and the Mac window each map it to their own navigation.
enum HomeTarget: Equatable {
    /// The chat of an agent, with a card or a To-do when the route names one.
    case conversation(ConversationRoute)
    /// A chat of a team or a direct chat.
    case teamChat(TeamChatKey)
    /// The pending cards, with this card first.
    case cards(String?)
    /// The page of one agent.
    case agent(String)
    /// The list of the agents.
    case agents
    /// The connection to the Mac, on Profile.
    case connection
    /// The notice board of a team, by its room ID.
    case notices(String)
}

/// One row of "Needs attention": a rule that breaches now, how many things breach it, and the place to fix it.
struct AttentionItem: Identifiable, Equatable {
    enum Kind: String, CaseIterable {
        case waiting, agents, work, stale, notices
    }

    let kind: Kind
    /// How many things breach the rule. The pill of the header adds them up.
    let count: Int
    /// The large number of the row. It is the count, or the age of the last sync for stale data.
    let value: String
    let label: String
    let detail: String
    let symbol: String
    let target: HomeTarget

    var id: String { kind.rawValue }
}

/// The rules of "Needs attention" on Home. A rule shows only while something breaches it, so a clear day shows no
/// banner. Each threshold has a name here and nowhere else.
enum AttentionRules {
    /// A pending card waits on the person too long after this time.
    static let cardWait: TimeInterval = 4 * 3600
    /// Work that got blocked or failed counts for this long.
    static let workWindow: TimeInterval = 24 * 3600
    /// The data is stale when the last sync is older than this.
    static let staleSync: TimeInterval = 10 * 60
    /// A Deadline counts when it ends within this time.
    static let deadlineWindow: TimeInterval = 24 * 3600
    /// The states of a To-do and of a task that count as blocked or failed.
    static let stuckStates: Set<String> = ["blocked", "failed", "needs_recovery"]

    /// What the rules read. A view fills it from the store, and a test fills it by hand.
    struct Input {
        var decisions: [Decision] = []
        /// The cards that the person answered already while the answer travels to the Mac.
        var answering: Set<String> = []
        var bots: [Bot] = []
        var artifacts: [Artifact] = []
        var tasks: [AgentTask] = []
        var connection: ConnectionState = .connected
        var lastSync: Date? = nil
        var rooms: [TeamRoom] = []
    }

    /// The rows in a fixed order: the person first, then the agents, their work, the data, and the team.
    static func items(_ input: Input, now: Date = Date()) -> [AttentionItem] {
        [waiting(input, now), agents(input), work(input, now), stale(input, now), notices(input, now)].compactMap { $0 }
    }

    /// The sum for the pill of the header.
    static func total(_ items: [AttentionItem]) -> Int { items.reduce(0) { $0 + $1.count } }

    /// The pending cards that are older than `cardWait`, oldest first.
    static func overdueCards(_ input: Input, now: Date) -> [Decision] {
        input.decisions
            .filter { $0.isPending && !input.answering.contains($0.id) }
            .filter { decision in decision.createdAt.apiDate.map { now.timeIntervalSince($0) > cardWait } ?? false }
            .sorted { ($0.createdAt, $0.id) < ($1.createdAt, $1.id) }
    }

    /// The agents that cannot run: a provider limit or a provider error. An archived agent does not count.
    static func haltedAgents(_ input: Input) -> [Bot] {
        input.bots.filter { $0.health != nil && !$0.archived }
    }

    /// The To-dos and the tasks that got blocked or failed within `workWindow`, newest first. A To-do and its task
    /// count one time. The work of an archived agent does not count.
    static func stuckWork(_ input: Input, now: Date) -> [ConversationRoute] {
        let active = Set(input.bots.filter { !$0.archived }.map(\.id))
        func recent(_ time: String?) -> Bool {
            time?.apiDate.map { now.timeIntervalSince($0) <= workWindow } ?? false
        }
        let todos = input.artifacts.filter {
            $0.kind == "work" && stuckStates.contains($0.status) && active.contains($0.botId)
                && recent($0.observedAt ?? $0.createdAt)
        }
        let covered = Set(todos.compactMap(\.taskId))
        let tasks = input.tasks.filter {
            stuckStates.contains($0.status) && active.contains($0.chatBotId) && recent($0.updatedAt)
                && !covered.contains($0.id)
        }
        let rows = todos.map { (time: $0.observedAt ?? $0.createdAt, id: $0.id,
                                route: ConversationRoute(botId: $0.botId, workContext: WorkReference(id: $0.id, title: $0.title))) }
            + tasks.map { (time: $0.updatedAt, id: $0.id, route: ConversationRoute(botId: $0.chatBotId)) }
        return rows.sorted { ($0.time, $0.id) > ($1.time, $1.id) }.map(\.route)
    }

    /// True when the app cannot show current data: the Mac is not connected, or the last sync is older than
    /// `staleSync`. An app without a pairing and the preview have no data to be stale. A connection that starts is
    /// not a breach yet.
    static func isStale(connection: ConnectionState, lastSync: Date?, now: Date) -> Bool {
        switch connection {
        case .unconfigured, .preview, .connecting: false
        case .connected: lastSync.map { now.timeIntervalSince($0) > staleSync } ?? false
        case .disconnected, .macOffline, .relayUnreachable, .needsRepair: true
        }
    }

    /// The notices that hold the work of a team: an active Status or Deadline that the person has not seen, and an
    /// active Deadline that ends within `deadlineWindow`. Each notice counts one time.
    static func urgentNotices(_ input: Input, now: Date) -> [(room: TeamRoom, notice: WorkspaceNotice)] {
        input.rooms.filter { $0.status == .active }.flatMap { room in
            room.noticeList.filter { notice in
                guard notice.active, notice.kind.isBanner else { return false }
                if !notice.seen { return true }
                guard notice.kind == .deadline, let end = notice.endsAt?.apiDate else { return false }
                return end > now && end.timeIntervalSince(now) <= deadlineWindow
            }.map { (room, $0) }
        }
    }

    private static func waiting(_ input: Input, _ now: Date) -> AttentionItem? {
        let cards = overdueCards(input, now: now)
        guard let oldest = cards.first else { return nil }
        let age = oldest.createdAt.apiDate.map { self.age(now.timeIntervalSince($0)) } ?? ""
        return AttentionItem(kind: .waiting, count: cards.count, value: "\(cards.count)",
                             label: cards.count == 1 ? "Card waiting on you" : "Cards waiting on you",
                             detail: "Past 4h · oldest \(age)", symbol: "hourglass", target: .cards(oldest.id))
    }

    private static func agents(_ input: Input) -> AttentionItem? {
        let halted = haltedAgents(input)
        guard let first = halted.first else { return nil }
        let names = halted.prefix(2).map(\.name).joined(separator: ", ") + (halted.count > 2 ? " and \(halted.count - 2) more" : "")
        return AttentionItem(kind: .agents, count: halted.count, value: "\(halted.count)",
                             label: halted.count == 1 ? "Agent can't run" : "Agents can't run",
                             detail: halted.count == 1 ? "\(first.name) · \(first.health?.label ?? "")" : names,
                             symbol: "bolt.slash.fill", target: halted.count == 1 ? .agent(first.id) : .agents)
    }

    private static func work(_ input: Input, _ now: Date) -> AttentionItem? {
        let stuck = stuckWork(input, now: now)
        guard let first = stuck.first else { return nil }
        let title = first.workContext?.title ?? input.bots.first { $0.id == first.botId }?.name ?? "Agent"
        return AttentionItem(kind: .work, count: stuck.count, value: "\(stuck.count)",
                             label: "Blocked or failed",
                             detail: stuck.count == 1 ? title : "Last 24h · newest: \(title)",
                             symbol: "exclamationmark.octagon.fill", target: .conversation(first))
    }

    private static func stale(_ input: Input, _ now: Date) -> AttentionItem? {
        guard isStale(connection: input.connection, lastSync: input.lastSync, now: now) else { return nil }
        let value = input.lastSync.map { age(now.timeIntervalSince($0)) } ?? "–"
        let detail = input.connection == .connected ? "Connected, but no sync since then" : input.connection.title
        return AttentionItem(kind: .stale, count: 1, value: value, label: "Data is stale",
                             detail: input.lastSync == nil ? detail + " · never synced" : detail + " · last sync \(value) ago",
                             symbol: "wifi.exclamationmark", target: .connection)
    }

    private static func notices(_ input: Input, _ now: Date) -> AttentionItem? {
        let urgent = urgentNotices(input, now: now)
        guard let first = urgent.first else { return nil }
        // The Deadline that ends first names the row, because it is the one with a clock.
        let deadline = urgent.filter { $0.notice.kind == .deadline }
            .compactMap { item in item.notice.endsAt?.apiDate.map { (end: $0, item: item) } }
            .min { $0.end < $1.end }
        let named = deadline?.item ?? first
        let text = named.notice.text.trimmingCharacters(in: .whitespacesAndNewlines)
        let detail = deadline.map { "Ends in \(NoticeCountdown.text(until: $0.end, now: now)) · \(text)" }
            ?? "\(named.notice.kind.label) · \(text)"
        return AttentionItem(kind: .notices, count: urgent.count, value: "\(urgent.count)",
                             label: urgent.count == 1 ? "Team notice" : "Team notices",
                             detail: detail, symbol: "megaphone.fill", target: .notices(named.room.room))
    }

    /// A short age: "12m", "5h", "3d".
    static func age(_ seconds: TimeInterval) -> String {
        let minutes = max(0, Int(seconds / 60))
        if minutes < 60 { return "\(minutes)m" }
        if minutes < 48 * 60 { return "\(minutes / 60)h" }
        return "\(minutes / (24 * 60))d"
    }
}

extension AppStore {
    /// The rows of "Needs attention" from the state of the store.
    func attentionItems(now: Date = Date()) -> [AttentionItem] {
        AttentionRules.items(AttentionRules.Input(
            decisions: decisions, answering: answeringDecisionIds, bots: bots, artifacts: artifacts, tasks: tasks,
            connection: connection, lastSync: lastSync, rooms: shownTeamRooms), now: now)
    }
}

#if DEBUG && targetEnvironment(simulator)
extension AppStore {
    /// Makes the preview data breach three rules, for the UI test of Home: a card that waits for 5 hours, an agent
    /// with a provider limit, and a blocked To-do.
    func applyAttentionPreview(now: Date = Date()) {
        let formatter = ISO8601DateFormatter()
        if let index = decisions.firstIndex(where: \.isPending) {
            decisions[index].createdAt = formatter.string(from: now.addingTimeInterval(-5 * 3600))
        }
        guard let index = bots.firstIndex(where: { !$0.coconut && !$0.archived }) else { return }
        bots[index].health = BotHealth(state: "rate_limited", errorCode: nil, until: nil, uncovered: 2)
        artifacts.append(Artifact(id: "attention-preview-work", botId: bots[index].id, taskId: nil,
                                  title: "Ship the retry fix", kind: "work", status: "blocked",
                                  preview: "Waits for a review of the pull request.", url: nil,
                                  createdAt: formatter.string(from: now.addingTimeInterval(-40 * 60))))
    }
}
#endif

/// "Needs attention" at the top of Home: a red header with the count, then a red card with one row for each rule
/// that breaches. It draws nothing when no rule breaches.
struct AttentionCard: View {
    let items: [AttentionItem]
    let open: (HomeTarget) -> Void

    static let tint = BoardStyle.destructive

    var body: some View {
        if !items.isEmpty {
            VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
                HStack(spacing: BoardStyle.Space.s) {
                    Text("Needs attention").font(.subheadline.weight(.semibold)).foregroundStyle(Self.tint)
                    Text("\(AttentionRules.total(items))")
                        .font(.caption.weight(.semibold).monospacedDigit())
                        .foregroundStyle(.white)
                        .padding(.horizontal, 6).frame(minWidth: 18, minHeight: 18)
                        .background(Self.tint, in: Capsule())
                }
                .accessibilityElement(children: .combine)
                .accessibilityAddTraits(.isHeader)
                // On the header only: an identifier on the whole card would replace the identifier of each row.
                .accessibilityIdentifier("attention-card")
                VStack(spacing: 0) {
                    ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                        if index > 0 { Divider().overlay(Self.tint.opacity(0.18)).padding(.leading, 44) }
                        row(item)
                    }
                }
                .background(Self.tint.opacity(0.08), in: RoundedRectangle(cornerRadius: BoardStyle.Radius.small))
                .overlay(RoundedRectangle(cornerRadius: BoardStyle.Radius.small).strokeBorder(Self.tint.opacity(0.22)))
            }
        }
    }

    private func row(_ item: AttentionItem) -> some View {
        Button { open(item.target) } label: {
            HStack(spacing: BoardStyle.Space.m) {
                Image(systemName: item.symbol)
                    .font(.body.weight(.semibold)).foregroundStyle(Self.tint)
                    .frame(width: 20)
                VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                    Text(item.label).font(.body.weight(.semibold)).foregroundStyle(Self.tint)
                    Text(item.detail).font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer(minLength: BoardStyle.Space.s)
                Text(item.value).font(.title2.weight(.semibold).monospacedDigit()).foregroundStyle(Self.tint)
                Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(Self.tint.opacity(0.6))
            }
            .padding(.horizontal, BoardStyle.Space.m).padding(.vertical, 10)
            .contentShape(Rectangle())
        }
        .buttonStyle(HomePressStyle())
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(item.label), \(item.value), \(item.detail)")
        .accessibilityIdentifier("attention-\(item.kind.rawValue)")
    }
}
