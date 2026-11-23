import SwiftUI

/// The Live page.
struct LiveRoute: Hashable {}

/// An agent chat that works now: its bot and the turn that runs.
struct LiveAgent: Identifiable, Equatable {
    let bot: Bot
    let task: AgentTask
    var id: String { bot.id }
}

/// A teammate who is active now: the notice that says what they work on, or their newest message in a team chat.
struct LivePerson: Identifiable, Equatable {
    let member: TeamMember
    let room: TeamRoom
    let line: String
    let at: Date?
    /// True for a status notice of the member, false for a message of the team chat.
    let status: Bool
    var id: String { member.member }
}

/// The two lists of the live view. They read only their arguments.
enum LiveFeed {
    /// A teammate who wrote in a team chat this long ago is active now.
    static let activeSeconds: TimeInterval = 10 * 60

    /// The chats that have a running turn, the oldest turn first, so a tile keeps its place while its agent works.
    static func agents(bots: [Bot], tasks: [AgentTask]) -> [LiveAgent] {
        let active = Dictionary(bots.filter { !$0.archived }.map { ($0.id, $0) }) { first, _ in first }
        var seen = Set<String>()
        let running = tasks.reversed().filter { task in
            task.isRunning && active[task.chatBotId] != nil && seen.insert(task.chatBotId).inserted
        }
        return running.compactMap { task in active[task.chatBotId].map { LiveAgent(bot: $0, task: task) } }
            .sorted { ($0.task.turnStartedAt ?? "~", $0.bot.id) < ($1.task.turnStartedAt ?? "~", $1.bot.id) }
    }

    /// The people of the teams who are active now, the newest first. The team log has no presence, so a person is
    /// active while a status notice of theirs is on the board, or when they wrote in the team chat a few minutes ago.
    static func people(rooms: [TeamRoom], now: Date = .now) -> [LivePerson] {
        var found: [String: LivePerson] = [:]
        for room in rooms where room.status == .active {
            for member in room.members ?? [] where member.thisMac != true && !member.isBotOnly {
                var person: LivePerson?
                if let notice = room.noticeList.filter({ $0.active && $0.kind == .status && $0.author == member.member })
                    .max(by: { ($0.updatedAt ?? $0.postedAt) < ($1.updatedAt ?? $1.postedAt) }) {
                    person = LivePerson(member: member, room: room, line: notice.text,
                                        at: (notice.updatedAt ?? notice.postedAt).apiDate, status: true)
                } else if let last = room.lastMessage, last.member == member.member, !last.isBot, last.thisMac != true,
                          let date = last.createdAt?.apiDate, now.timeIntervalSince(date) <= activeSeconds,
                          let text = last.text, !text.isEmpty {
                    person = LivePerson(member: member, room: room, line: text, at: date, status: false)
                }
                if let person, (found[member.member]?.at ?? .distantPast) <= (person.at ?? .distantPast) {
                    found[member.member] = person
                }
            }
        }
        return found.values.sorted { ($0.at ?? .distantPast, $1.id) > ($1.at ?? .distantPast, $0.id) }
    }
}

/// Live: a tile for each agent chat that works now and each teammate who is active now. Each agent tile streams what
/// the agent does: the working line, the tool or command, the file it edits, its newest image, its helpers and its
/// spend. A tap opens the chat.
struct LiveView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.pushRoute) private var pushRoute
    /// The tasks whose feed this page keeps fresh.
    @State private var watched: Set<String> = []

    static let columns = [GridItem(.adaptive(minimum: 320, maximum: 560), spacing: BoardStyle.Space.m, alignment: .top)]

    var body: some View {
        let agents = LiveFeed.agents(bots: store.bots, tasks: store.tasks)
        let people = LiveFeed.people(rooms: store.shownTeamRooms)
        // The page of each other page of tiles.
        BoardPage(width: .wide, spacing: BoardStyle.Space.l) {
            summary(agents)
            if agents.isEmpty && people.isEmpty {
                VStack(spacing: BoardStyle.Space.m) {
                    Image(systemName: "dot.radiowaves.left.and.right").font(.largeTitle.weight(.light)).foregroundStyle(.secondary)
                    Text("Nothing running").font(.title3.weight(.semibold))
                }
                .frame(maxWidth: .infinity).padding(.vertical, BoardStyle.Space.xxl)
            } else {
                LazyVGrid(columns: Self.columns, alignment: .leading, spacing: BoardStyle.Space.m) {
                    ForEach(agents) { LiveAgentTile(agent: $0) }
                    ForEach(people) { LivePersonTile(person: $0) }
                }
            }
        }
        .navigationTitle("Live").inlineNavigationTitle()
        .task(id: store.connection == .connected) { await store.loadUsage() }
        .onChange(of: agents.map(\.task.id), initial: true) { _, ids in watch(Set(ids)) }
        .onDisappear { watch([]) }
        .accessibilityIdentifier("live-page")
    }

    /// The tasks of the tiles are watched, so each tile has its feed. A task that left is not watched any more.
    private func watch(_ ids: Set<String>) {
        for id in ids.subtracting(watched) { store.watchActivity(id) }
        for id in watched.subtracting(ids) { store.unwatchActivity(id) }
        watched = ids
    }

    private func summary(_ agents: [LiveAgent]) -> some View {
        let helpers = agents.reduce(0) { $0 + (store.turnActivity[$1.task.id]?.runningHelpers.count ?? 0) }
        let today = store.usage.map { report in
            report.agents.values.reduce(0.0) { sum, agent in sum + (agent.days.first { $0.day == report.today }?.costUsd ?? 0) }
        }
        return BentoBoard {
            BentoStat(title: "Working", value: "\(agents.count)", size: .medium).bento(4, 1, compact: (1, 1))
            BentoStat(title: "Helpers", value: "\(helpers)", size: .medium).bento(4, 1, compact: (1, 1))
            BentoStat(title: "Spend today", value: today.map(UsageMath.cost) ?? "\u{2014}", size: .medium, chevron: true)
                .contentShape(Rectangle())
                .onTapGesture { pushRoute?(SpendRoute()) }
                .accessibilityAddTraits(.isButton)
                .accessibilityIdentifier("live-spend")
                .bento(4, 1, compact: (2, 1))
        }
    }
}

/// The tile of an agent that works: its face and working line, its newest image, the call it runs, the file it edits,
/// its helpers and its spend.
struct LiveAgentTile: View {
    @Environment(AppStore.self) private var store
    @Environment(\.pushRoute) private var pushRoute
    @Environment(\.openChat) private var openChat
    let agent: LiveAgent

    private var feed: TurnActivity? { store.turnActivity[agent.task.id] }
    private var working: ConversationActivity? { store.conversationActivity(agent.bot.id) }
    private var name: String { agent.bot.isChat ? agent.bot.listName : agent.bot.name }
    private var edited: ActivityFile? { feed?.recentFiles.first(where: \.edited) }
    private var call: ActivityEntry? { feed?.running.last }
    private var image: ActivityImage? { feed?.filmstrip.first }

    var body: some View {
        Button { open() } label: {
            VStack(alignment: .leading, spacing: BoardStyle.Space.m) {
                header
                if let image { picture(image) }
                steps
                if let feed, !feed.helpers.isEmpty { LiveHelpersList(helpers: feed.helpers) }
                spend
            }
            .padding(BoardStyle.Space.l)
            .frame(maxWidth: .infinity, alignment: .topLeading)
            .background {
                ZStack {
                    BentoShape.tile.fill(BoardStyle.surface)
                    BentoShape.tile.strokeBorder(BoardStyle.line.opacity(0.6), lineWidth: 0.5)
                }
            }
            .clipShape(BentoShape.tile)
            .contentShape(BentoShape.tile)
        }
        .buttonStyle(BentoPressStyle())
        .task(id: agent.bot.id) { await keepSpendFresh() }
        .accessibilityIdentifier("live-agent-" + agent.bot.id)
    }

    private var header: some View {
        HStack(alignment: .center, spacing: BoardStyle.Space.m) {
            VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                Text(name).font(.headline).lineLimit(1)
                if let working {
                    TimelineView(.periodic(from: .now, by: 1)) { timeline in
                        // The whole line where it fits. A narrow tile leaves the model out, so no word is cut in two
                        // places.
                        ViewThatFits(in: .horizontal) {
                            workingLine(working, meta: Self.meta(working, at: timeline.date))
                            workingLine(working, meta: Self.meta(working, at: timeline.date, model: false))
                        }
                    }
                }
            }
            Spacer(minLength: BoardStyle.Space.xs)
            Circle().fill(BoardStyle.positive).frame(width: 8, height: 8)
        }
    }

    private func workingLine(_ working: ConversationActivity, meta: String) -> some View {
        HStack(spacing: BoardStyle.Space.xs) {
            ShimmerText(text: working.reconnecting ? "Reconnecting…" : working.phase.label, active: !working.reconnecting)
                .layoutPriority(1)
            Text(meta).monospacedDigit().foregroundStyle(.secondary)
        }
        .font(.subheadline).lineLimit(1)
    }

    /// "· 23s · Claude Opus 5.5": the time of the newest step to the second, and the model of the turn.
    static func meta(_ working: ConversationActivity, at date: Date, model: Bool = true) -> String {
        var parts: [String] = []
        if let start = working.startedAt { parts.append(ConversationActivity.elapsed(date.timeIntervalSince(start))) }
        if model, let name = working.model, !name.isEmpty { parts.append(name) }
        return parts.map { "· " + $0 }.joined(separator: " ")
    }

    private func picture(_ image: ActivityImage) -> some View {
        LiveActivityImageView(taskId: agent.task.id, image: image, fill: true)
            .frame(maxWidth: .infinity).frame(height: 170)
            .background(BoardStyle.surface)
            .clipShape(RoundedRectangle(cornerRadius: BoardStyle.Radius.small, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: BoardStyle.Radius.small, style: .continuous).stroke(BoardStyle.line))
            .overlay(alignment: .bottomLeading) {
                HStack(spacing: BoardStyle.Space.xs) {
                    Image(systemName: "photo").imageScale(.small)
                    Text(image.source)
                    if let date = image.at.apiDate { Text(date, format: .dateTime.hour().minute().second()).monospacedDigit() }
                }
                .font(.caption2.weight(.medium)).foregroundStyle(.white)
                .padding(.horizontal, BoardStyle.Space.s).padding(.vertical, 3)
                .background(.black.opacity(0.55), in: Capsule())
                .padding(BoardStyle.Space.s)
            }
            .accessibilityLabel("\(image.source) image of \(name)")
    }

    /// The call that runs now and the file that the turn edits: one mono line each.
    @ViewBuilder private var steps: some View {
        let detail = call.map { $0.line.isEmpty ? $0.title : $0.title + " " + $0.line }
            ?? agent.task.phaseDetail.flatMap { $0.isEmpty ? nil : $0 }
        if detail != nil || edited != nil {
            VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
                if let detail {
                    stepLine(symbol: call?.symbol ?? "terminal", text: detail, cutsStart: call?.cutsStart ?? false)
                }
                if let edited {
                    stepLine(symbol: "pencil", text: LiveActivityText.shortPath(edited.path, folders: 2), cutsStart: true)
                }
            }
        }
    }

    private func stepLine(symbol: String, text: String, cutsStart: Bool) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: BoardStyle.Space.s) {
            Image(systemName: symbol).font(.caption).foregroundStyle(.secondary).frame(width: 16)
            Text(text).font(.callout.monospaced()).foregroundStyle(.primary.opacity(0.85))
                .lineLimit(1).truncationMode(cutsStart ? .head : .tail)
        }
    }

    @ViewBuilder private var spend: some View {
        if let usage = store.chatUsage[agent.bot.id], usage.total.tokens > 0 {
            Text(usage.line).font(.caption.monospacedDigit()).foregroundStyle(.secondary).lineLimit(1)
        }
    }

    /// The spend of the chat changes while the agent works. The tile reads it again every half minute.
    private func keepSpendFresh() async {
        while !Task.isCancelled {
            await store.loadChatUsage(agent.bot.id)
            try? await Task.sleep(for: .seconds(30))
        }
    }

    private func open() {
        if let openChat { openChat(agent.bot.id) } else { pushRoute?(ConversationRoute(botId: agent.bot.id)) }
    }
}

/// The tile of a teammate who is active now: their face, their name and what they work on or wrote last.
struct LivePersonTile: View {
    @Environment(\.pushRoute) private var pushRoute
    @Environment(\.showTeamChat) private var showTeamChat
    let person: LivePerson

    var body: some View {
        Button {
            let chat = TeamChatKey(room: person.room.room)
            if let showTeamChat { showTeamChat(chat) } else { pushRoute?(TeamChatRoute(chat: chat)) }
        } label: {
            VStack(alignment: .leading, spacing: BoardStyle.Space.m) {
                HStack(spacing: BoardStyle.Space.m) {
                    PersonFace(member: person.member, memberId: person.member.member, name: person.member.name, size: 34)
                    VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                        Text(person.member.name).font(.headline).lineLimit(1)
                        HStack(spacing: BoardStyle.Space.xs) {
                            Text(person.room.name)
                            if let at = person.at {
                                Text("·")
                                TimelineView(.periodic(from: .now, by: 30)) { timeline in
                                    Text(Self.ago(at, now: timeline.date))
                                }
                            }
                        }
                        .font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
                    }
                    Spacer(minLength: BoardStyle.Space.xs)
                    Circle().fill(BoardStyle.positive).frame(width: 8, height: 8)
                }
                Text(person.line).font(.callout).foregroundStyle(.primary.opacity(0.85)).lineLimit(3)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .padding(BoardStyle.Space.l)
            .frame(maxWidth: .infinity, alignment: .topLeading)
            .background {
                ZStack {
                    BentoShape.tile.fill(BoardStyle.surface)
                    BentoShape.tile.strokeBorder(BoardStyle.line.opacity(0.6), lineWidth: 0.5)
                }
            }
            .clipShape(BentoShape.tile)
            .contentShape(BentoShape.tile)
        }
        .buttonStyle(BentoPressStyle())
        .accessibilityIdentifier("live-person-" + person.member.member)
    }

    /// "now", "4 min", "1 h".
    static func ago(_ date: Date, now: Date) -> String {
        let seconds = max(0, now.timeIntervalSince(date))
        if seconds < 60 { return "now" }
        if seconds < 3600 { return "\(Int(seconds / 60)) min" }
        return "\(Int(seconds / 3600)) h"
    }
}

/// The helpers (subagents) of a turn: a line that counts the helpers that run, and, open, the goal, the time and the
/// current call of each helper. The finished helpers follow the running ones.
struct LiveHelpersList: View {
    let helpers: [ActivitySubagent]
    @State private var isOpen = false

    private var running: [ActivitySubagent] { helpers.filter(\.isRunning) }
    private var ordered: [ActivitySubagent] { running + helpers.filter { !$0.isRunning } }

    var body: some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
            Button { withAnimation(.snappy(duration: 0.2)) { isOpen.toggle() } } label: {
                HStack(spacing: BoardStyle.Space.xs) {
                    Image(systemName: "chevron.right").font(.caption2.weight(.bold)).foregroundStyle(.tertiary)
                        .rotationEffect(.degrees(isOpen ? 90 : 0))
                    Image(systemName: "person.2").font(.caption).foregroundStyle(.secondary)
                    Text(title).font(.subheadline.weight(.medium))
                    Spacer(minLength: BoardStyle.Space.xs)
                    if let first = running.first, !isOpen {
                        TimelineView(.periodic(from: .now, by: 1)) { timeline in
                            Text(first.elapsed(at: timeline.date) ?? "").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                        }
                    }
                }
                .frame(minHeight: 28).contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(title)
            .accessibilityHint(isOpen ? "Hides the helpers" : "Shows the helpers")
            .accessibilityIdentifier("live-helpers")
            if isOpen {
                VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
                    ForEach(ordered) { LiveHelperRow(helper: $0) }
                }
                .padding(.leading, BoardStyle.Space.l)
                .transition(.opacity)
            } else if let first = running.first, let now = first.now {
                Text(first.title).font(.caption).foregroundStyle(.secondary).lineLimit(1).padding(.leading, BoardStyle.Space.l)
                Text(now).font(.caption.monospaced()).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                    .padding(.leading, BoardStyle.Space.l)
            }
        }
    }

    /// "2 helpers running", "1 helper", "3 helpers".
    private var title: String {
        let noun = helpers.count == 1 ? "helper" : "helpers"
        return running.isEmpty ? "\(helpers.count) \(noun)" : "\(running.count) \(running.count == 1 ? "helper" : "helpers") running"
    }
}

/// One helper: its goal, its time, and the call that it runs now.
struct LiveHelperRow: View {
    let helper: ActivitySubagent

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: BoardStyle.Space.s) {
            Group {
                if helper.isRunning {
                    ProgressView().controlSize(.mini)
                } else {
                    Image(systemName: helper.failed ? "exclamationmark.circle.fill" : "checkmark.circle.fill")
                        .foregroundStyle(helper.failed ? BoardStyle.destructive : BoardStyle.positive)
                }
            }
            .font(.caption).frame(width: 16)
            VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                Text(helper.title).font(.subheadline).lineLimit(2)
                    .foregroundStyle(helper.isRunning ? .primary : .secondary)
                if let now = helper.now {
                    Text(now).font(.caption.monospaced()).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                }
            }
            Spacer(minLength: BoardStyle.Space.xs)
            TimelineView(.periodic(from: .now, by: helper.isRunning ? 1 : 3600)) { timeline in
                Text(helper.elapsed(at: timeline.date) ?? "").font(.caption.monospacedDigit()).foregroundStyle(.tertiary)
            }
        }
        .accessibilityElement(children: .combine)
    }
}

/// The helpers that run under the working line of a chat. The view keeps the feed of the turn fresh while it shows.
struct ChatHelpersRow: View {
    @Environment(AppStore.self) private var store
    let taskId: String

    var body: some View {
        Group {
            if let helpers = store.turnActivity[taskId]?.helpers, !helpers.isEmpty {
                LiveHelpersList(helpers: helpers)
                    // The text starts at the edge of the text in a bubble, as the working line does.
                    .padding(.leading, BoardStyle.Space.m)
            }
        }
        .onAppear { store.watchActivity(taskId) }
        .onDisappear { store.unwatchActivity(taskId) }
    }
}

/// One image of a feed, loaded through the paired Mac.
struct LiveActivityImageView: View {
    let taskId: String
    let image: ActivityImage
    var fill = false
    @Environment(AppStore.self) private var store
    @State private var loaded: PlatformImage?
    @State private var missing = false

    var body: some View {
        ZStack {
            if let picture = loaded ?? store.activityImages.object(forKey: (taskId + "/" + image.id) as NSString) {
                Image(platformImage: picture).resizable().aspectRatio(contentMode: fill ? .fill : .fit)
            } else if missing {
                Image(systemName: "photo").foregroundStyle(.tertiary)
            } else {
                ProgressView().controlSize(.small)
            }
        }
        .task(id: image.id) {
            loaded = nil
            missing = false
            loaded = await store.activityImage(taskId: taskId, image: image)
            missing = loaded == nil
        }
    }
}


