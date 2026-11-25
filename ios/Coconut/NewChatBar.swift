import SwiftUI

/// The project of the message box. The box sends to one project and always names it.
enum ChatProject {
    static let projectKey = "chatProject"
    /// The key of the project of the business (bot_profiles.PROJECTS). Finance, Support and Product start their box
    /// with it.
    static let business = "gumroad"

    /// The places that a chat can start in: the projects with the newest chat first, then the Coconut chat. An archived
    /// project and an agent that is no project are not among them.
    static func choices(in bots: [Bot], latest: [String: Date]) -> [Bot] {
        let open = bots.filter { !$0.archived && ($0.isProject || ($0.coconut && !$0.isChat)) }
        let agents = open.filter { !$0.coconut }.sorted {
            (latest[$0.id] ?? $0.createdAt.apiDate ?? .distantPast) > (latest[$1.id] ?? $1.createdAt.apiDate ?? .distantPast)
        }
        return agents + open.filter(\.coconut)
    }

    /// The agents of the teams that this person can write to now (TeamRoom.directBot), in the order of the teams, such
    /// as Gumclaw, the agent of the Gumroad team on its own Mac (the person asked for it in this list). A team agent has
    /// one direct chat with each person, so a message of the box goes to that chat and starts no new one. An agent of
    /// this Mac is a project here, so it is not among them.
    static func teamAgents(in rooms: [TeamRoom]) -> [TeamAgentChoice] {
        var seen = Set<String>()
        return rooms.filter { $0.status == .active }.flatMap { room in
            room.botList.compactMap { bot -> TeamAgentChoice? in
                guard room.directBot(bot.id) != nil, seen.insert(bot.id).inserted else { return nil }
                return TeamAgentChoice(chat: TeamChatKey(room: room.room, bot: bot.id), name: bot.name)
            }
        }
    }
}

/// An agent of a team in the list of the message box: its direct chat with this person, and its name.
struct TeamAgentChoice: Identifiable, Equatable {
    let chat: TeamChatKey
    let name: String
    /// The team bot ID (`t-<room>-<entry>`), which no project has.
    var id: String { chat.bot ?? chat.room }
}

/// Where the message box sends: a project, where the message starts a new chat, or an agent of a team, whose direct
/// chat takes it.
enum NewChatTarget: Equatable {
    case project(Bot)
    case teamAgent(TeamAgentChoice)

    var id: String {
        switch self {
        case .project(let bot): bot.id
        case .teamAgent(let agent): agent.id
        }
    }

    var name: String {
        switch self {
        case .project(let bot): bot.name
        case .teamAgent(let agent): agent.name
        }
    }
}

/// The page that the message box sits under: a tab of the iPhone (the person: "new chat should show up every tab
/// you're on"), or Home of the Mac, which is the place of Chats. Each place keeps its own project and its own draft,
/// and the project follows where the person is: Finance, Support and Product start with the project of the business,
/// and You with Coconut, where Coconut's settings change. Chats keeps the project that each New agent chooses
/// (AppStore.startChat), and only its box takes those starts. The box of Product takes the next steps of a brainstorm
/// (NewChatRequest.place, Brainstorm.swift), which shares its project.
enum NewChatPlace: String {
    case chats, product, support, finance, you

    /// The key of the project or agent that the person chose here. Chats keeps the key of the one box of earlier
    /// releases.
    var choiceKey: String { self == .chats ? ChatProject.projectKey : ChatProject.projectKey + "-" + rawValue }
    /// The key of the text that the person did not send yet (ComposerDrafts).
    var draftKey: String { self == .chats ? "new-chat" : "new-chat-" + rawValue }
    /// The key of the suggestions that the box of the place sent (NewChatSuggestion).
    var sentSuggestionsKey: String { "newChatSuggestionsSent-" + rawValue }
    /// The list of to-dos that the empty box suggests from (NewChatSuggestion).
    var area: TodoArea? { TodoArea(rawValue: rawValue) }

    /// The project that the box of the place starts with until the person chooses one there. Nil for Chats, which
    /// starts with the newest project.
    func startingProject(in projects: [Bot]) -> Bot? {
        switch self {
        case .chats: nil
        case .you: projects.first(where: \.coconut)
        case .product, .support, .finance: projects.first { $0.projectKey == ChatProject.business }
        }
    }

    /// What the box sends to: the choice of the person while it is in the list, else the project that the place starts
    /// with, else the newest project. Nil before the Mac sends its projects.
    func target(chosen: String, projects: [Bot], agents: [TeamAgentChoice]) -> NewChatTarget? {
        if let project = projects.first(where: { $0.id == chosen }) { return .project(project) }
        if let agent = agents.first(where: { $0.id == chosen }) { return .teamAgent(agent) }
        return (startingProject(in: projects) ?? projects.first).map(NewChatTarget.project)
    }
}

/// The requests that the empty box of a tab of the business suggests from what the tab shows (the person: "the
/// placeholder changes to start with suggestion for response per thing"): the breach of the tab first, the support
/// conversations or the risk reviews past SLA, then each to-do of its list that nobody works on or waits for, high
/// priority first. The box shows the first one that it did not send yet in place of its placeholder, and Send on the
/// empty box sends it, so one tap hands the top thing of the tab to the agent of the box. The count and the to-dos are
/// those that the list of the tab shows (TodoCounts): while Finance lists each risk review, the reviews that nobody
/// decided or gave to Gumclaw count, and the issue of a review is no to-do.
enum NewChatSuggestion {
    /// The sent suggestions that a box keeps, newest last, one on each line. A suggestion that the person sent does not
    /// come back after a restart, also while the agent has not started on it yet.
    static let kept = 30

    static func list(_ area: TodoArea, todos: TodoIssues?, attention: [AttentionItem],
                     reviews: [RiskReview]? = nil) -> [String] {
        let reviews = area == .finance ? reviews : nil
        let count = reviews.map { $0.filter { !$0.withGumclaw && !$0.decided }.count }
            ?? TodoCounts.breaches(area, attention).reduce(0) { $0 + $1.count }
        var breach: String?
        switch area {
        case .support where count > 0:
            breach = count == 1 ? "Answer the support conversation past SLA"
                : "Answer the " + String(count) + " support conversations past SLA"
        case .finance where count > 0:
            breach = count == 1 ? "Clear the risk review past SLA" : "Clear the " + String(count) + " risk reviews past SLA"
        default: break
        }
        let hidden = TodoCounts.reviewIssues(reviews)
        let open = (todos?.items(in: area) ?? []).filter { !$0.working && $0.awaiting == nil && !hidden.contains($0.number) }
        // A number of GitHub is a name, so it has no separator of thousands.
        return [breach].compactMap { $0 } + open.map { "Work on #" + String($0.number) + ": " + $0.title }
    }
}

/// The message box of Home, in the iPhone and in the Mac window, as the box of the Codex app: always there, with the
/// cursor ready. The chip under the text names where the message goes and lists the projects, newest first, then the
/// Coconut chat, then the agents of the teams that this person can write to. It ends with the list of all projects (on
/// the iPhone). The first message starts a new chat in the project, and a message to a team agent goes to the direct
/// chat with it. The box of Chats is where every New chat leads (AppStore.startChat).
///
/// A Send never opens the chat that it makes (the person's request): the chat shows at the top of the Chats list,
/// under Working while it works, and under its project in the Mac sidebar. The box keeps the keyboard and the cursor,
/// so the person can queue up many chats in a row and open each one later. For a moment the empty box says where the
/// message went, because on a tab other than Chats nothing else on the screen changes.
struct NewChatBar: View {
    @Environment(AppStore.self) private var store
    @Environment(\.pushRoute) private var pushRoute
    let place: NewChatPlace
    @AppStorage private var chosen: String
    @State private var text: String
    @State private var sending = false
    /// The text of the last send, for the moment after it (SentEcho).
    @State private var echo = SentEcho()
    /// A new request puts the cursor in the field when the page is settled: a pop of the stack takes a moment.
    @State private var focusRequest: UUID?
    /// Where the last send went, which the empty box says for a moment.
    @State private var sent: SentNote?
    /// The suggestions that this box sent, one on each line, so the next one takes their place (NewChatSuggestion.kept).
    @AppStorage private var sentSuggestions: String
    @FocusState private var focused: Bool

    private struct SentNote: Equatable {
        let id = UUID()
        let name: String
    }

    init(place: NewChatPlace = .chats) {
        self.place = place
        _chosen = AppStorage(wrappedValue: "", place.choiceKey)
        _sentSuggestions = AppStorage(wrappedValue: "", place.sentSuggestionsKey)
        // The unsent text of the box stays when the app closes, as the text of each chat.
        _text = State(initialValue: ComposerDrafts.read(place.draftKey))
    }

    var body: some View {
        let latest = AgentChats.activity(bots: store.bots, latest: store.chatSummaries.newestActivity)
        let projects = ChatProject.choices(in: store.bots, latest: latest)
        let agents = ChatProject.teamAgents(in: store.shownTeamRooms)
        let target = place.target(chosen: chosen, projects: projects, agents: agents)
        let suggestion = place.area.flatMap { area in
            let done = Set(sentSuggestions.split(separator: "\n").map(String.init))
            return NewChatSuggestion.list(area, todos: store.todoIssues, attention: store.attentionItems(),
                                          reviews: store.shownRiskReviews)
                .first { !done.contains($0) }
        }
        VStack(spacing: 0) {
            box(target, projects: projects, agents: agents, suggestion: suggestion)
        }
        .padding(.horizontal, BoardStyle.Space.l).padding(.top, BoardStyle.Space.xs).padding(.bottom, BoardStyle.Space.s)
        .frame(maxWidth: 760)
        .frame(maxWidth: .infinity)
        .modifier(ComposerBackdrop())
        // The choice of Chats stays what the chip shows: a project that left the list, or a first start, gives the
        // newest project, and the newest project moving on does not move the box under the person who types. A team
        // agent that the list of the teams does not have yet stays chosen. The other places start with their own
        // project, which does not move.
        .onChange(of: target?.id, initial: true) { _, id in
            guard place == .chats, let id, id != chosen, chosen.isEmpty || store.team != nil else { return }
            chosen = id
        }
        .onChange(of: store.newChatRequest, initial: true) { _, request in
            if let request, request.place == place { apply(request) }
        }
        // The text of a send that the keyboard gives back right after it goes again (SentEcho).
        .onChange(of: text) { old, draft in
            if echo.takesBack(old: old, new: draft) { text = ""; return }
            ComposerDrafts.write(draft, chat: place.draftKey)
        }
        .task(id: focusRequest) {
            guard focusRequest != nil else { return }
            try? await Task.sleep(for: Self.focusDelay)
            guard !Task.isCancelled else { return }
            focused = true
        }
        .task(id: sent?.id) {
            guard sent != nil else { return }
            try? await Task.sleep(for: .seconds(3))
            guard !Task.isCancelled else { return }
            sent = nil
        }
        #if os(macOS)
        // A click on Home puts the cursor in the box, as a click on a chat puts it in the field of the chat.
        .task {
            try? await Task.sleep(for: .milliseconds(150))
            guard !Task.isCancelled else { return }
            focused = true
        }
        #endif
    }

    #if os(macOS)
    private static let focusDelay = Duration.milliseconds(150)
    #else
    private static let focusDelay = Duration.milliseconds(450)
    #endif

    /// The text on top, and under it the chip with the project and Send, as the box of a chat that opens.
    private func box(_ target: NewChatTarget?, projects: [Bot], agents: [TeamAgentChoice], suggestion: String?) -> some View {
        let shape = RoundedRectangle(cornerRadius: BoardStyle.Radius.large, style: .continuous)
        let typed = !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        // While the box says where the last message went, an empty box sends nothing.
        let offered = sent == nil ? suggestion : nil
        let canSend = target != nil && !sending && (typed || offered != nil)
        return VStack(alignment: .leading, spacing: 0) {
            TextField(placeholder(target, suggestion: offered), text: $text, axis: .vertical)
                .focused($focused)
                .modifier(ComposerFieldStyle(send: { if let target { send(to: target, suggestion: offered) } }, stacked: true))
                .padding(.horizontal, BoardStyle.Space.m)
                .disabled(target == nil)
                .accessibilityIdentifier("new-chat-input")
            HStack(spacing: 0) {
                projectChip(target, projects: projects, agents: agents)
                Spacer(minLength: 0)
                ComposerSendButton(symbol: "arrow.up", enabled: canSend, label: typed || offered == nil ? "Send" : "Send the suggestion",
                                   action: { if let target { send(to: target, suggestion: offered) } }, identifier: "new-chat-send")
            }
            .padding(.horizontal, BoardStyle.Space.xs)
        }
        .modifier(ComposerClickArea(shape: shape) { focused = true })
        .modifier(ComposerSurface(shape: shape))
        .modifier(ComposerGlassGroup())
    }

    /// The gray text of the empty box: where the last message went, for a moment; else the suggestion of the place;
    /// else what a Send does.
    private func placeholder(_ target: NewChatTarget?, suggestion: String?) -> String {
        if let sent { return "Sent to " + sent.name }
        if let suggestion { return suggestion }
        if case .teamAgent(let agent) = target { return TeamDirectText.message(agent.name) }
        return "New agent"
    }

    /// The project of the box, as a chip that opens the list of projects and team agents. Before the Mac sends its
    /// projects, the chip says so.
    @ViewBuilder
    private func projectChip(_ target: NewChatTarget?, projects: [Bot], agents: [TeamAgentChoice]) -> some View {
        if let target {
            let choice = Binding(get: { target.id }, set: { chosen = $0 })
            Menu {
                Picker("Project", selection: choice) {
                    ForEach(projects) { project in Text(project.name).tag(project.id) }
                }
                .pickerStyle(.inline)
                if !agents.isEmpty {
                    Picker("Team agents", selection: choice) {
                        ForEach(agents) { agent in Text(agent.name).tag(agent.id) }
                    }
                    .pickerStyle(.inline)
                    .accessibilityIdentifier("new-chat-team-agents")
                }
                Divider()
                #if os(iOS)
                // The page of the projects: the live work, the pages, the spend and the archive. The Mac sidebar has it.
                Button("All projects", systemImage: "list.bullet") { pushRoute?(AgentListRoute()) }
                    .accessibilityIdentifier("new-chat-all-projects")
                #endif
            } label: {
                ComposerChipLabel(title: target.name)
            }
            .modifier(ComposerChipMenuStyle())
            // The first item stays first, also when the menu opens above the chip.
            .menuOrder(.fixed)
            .accessibilityLabel("Project, \(target.name)")
            .accessibilityIdentifier("new-chat-project")
        } else {
            Text("No project yet")
                .font(.subheadline.weight(.medium))
                .foregroundStyle(.secondary)
                .padding(.horizontal, BoardStyle.Space.s)
                .frame(minHeight: 44)
                .accessibilityIdentifier("new-chat-project")
        }
    }

    /// A start of a chat names the project and brings text, for example a forwarded message. The text joins what the
    /// person typed already, so nothing typed goes away.
    private func apply(_ request: NewChatRequest?) {
        guard let request else { return }
        store.newChatRequest = nil
        if let project = request.project { chosen = project }
        if !request.text.isEmpty { text += (text.isEmpty ? "" : "\n\n") + request.text }
        focusRequest = UUID()
    }

    /// Sends the text, or the suggestion while the box is empty: as the first message of a new chat in a project, which
    /// stays closed (sendNewChat), or as a message of the direct chat with a team agent. When the Mac does not take it,
    /// the notice says why and the text comes back. The keyboard first gives the field all that it holds and stays up
    /// for the next chat, so the message is the text that the person sees, and the box stays empty (KeyboardSettle).
    private func send(to target: NewChatTarget, suggestion: String?) {
        KeyboardSettle.run(if: focused) { start(in: target, suggestion: suggestion) }
    }

    private func start(in target: NewChatTarget, suggestion: String?) {
        let typed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let message = typed.isEmpty ? suggestion ?? "" : typed
        guard !message.isEmpty, !sending else { return }
        if typed.isEmpty { markSent(message, true) }
        switch target {
        case .project(let project):
            sending = true
            if !typed.isEmpty {
                text = ""
                echo.sent(message)
            }
            Task {
                defer { sending = false }
                guard await store.sendNewChat(in: project, text: message) != nil else {
                    echo.cancel()
                    if typed.isEmpty { markSent(message, false) } else if text.isEmpty { text = message }
                    return
                }
                sent = SentNote(name: target.name)
            }
        case .teamAgent(let agent):
            // The store keeps the message and sends it. A message that does not go waits in the chat with Retry.
            guard store.sendTeamMessage(agent.chat, text: message, replyingTo: nil) else {
                if typed.isEmpty { markSent(message, false) }
                return
            }
            if !typed.isEmpty {
                text = ""
                echo.sent(message)
            }
            sent = SentNote(name: target.name)
        }
    }

    /// Keeps a sent suggestion, or lets it come back after a send that failed.
    private func markSent(_ suggestion: String, _ keeps: Bool) {
        var list = sentSuggestions.split(separator: "\n").map(String.init).filter { $0 != suggestion }
        if keeps { list.append(suggestion) }
        sentSuggestions = list.suffix(NewChatSuggestion.kept).joined(separator: "\n")
    }
}
