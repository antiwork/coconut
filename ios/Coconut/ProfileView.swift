import SwiftUI

/// A page that Profile opens: a thing of this device, or a setting that every chat shares.
enum ProfileRoute: Hashable {
    case mac, notifications, connections, team
}

/// What Profile shows of the person, from the state of the store.
struct ProfileSummary: Equatable {
    /// The name that a team sees for this person, else the name that the person typed for a team before. Nil when the
    /// person never gave one.
    let name: String?
    /// The member of this Mac in its first team, for the face.
    let member: TeamMember?

    init(rooms: [TeamRoom], remembered: String) {
        member = rooms.lazy.filter { $0.status == .active }.compactMap(\.ownMember).first
        let name = (member?.name ?? remembered).trimmingCharacters(in: .whitespacesAndNewlines)
        self.name = name.isEmpty ? nil : name
    }

    /// The line under "Your Mac": the state of the connection and the time of the last sync.
    static func connection(_ state: ConnectionState, lastSync: Date?, demo: Bool, now: Date = Date()) -> String {
        if demo { return "Preview data" }
        guard let lastSync else { return state.title }
        return state.title + " · synced " + AttentionRules.age(now.timeIntervalSince(lastSync)) + " ago"
    }
}

/// You, the place of the person: the person, this device, the settings of every chat, and Coconut itself. Coconut
/// has no Settings screen: the few things that need this device are here, with the connections and the repositories
/// that Coconut follows, and the Coconut chat changes the rest.
struct ProfileView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.pushRoute) private var pushRoute
    @Environment(\.openChat) private var openChat
    @Bindable private var push = PushNotifications.shared

    private var paired: Bool { store.hasPairing || store.isDemo }

    var body: some View {
        let summary = ProfileSummary(rooms: store.shownTeamRooms, remembered: TeamMemberName.remembered())
        BoardPage {
            header(summary)
            BoardSection("This device") {
                if paired {
                    BoardRow(symbol: "desktopcomputer", title: "Your Mac",
                             detail: ProfileSummary.connection(store.connection, lastSync: store.lastSync, demo: store.isDemo),
                             value: store.isDemo ? nil : store.pairedMacDescription)
                        .boardButton("mac-settings") { pushRoute?(ProfileRoute.mac) }
                } else {
                    PairMacButton().padding(.horizontal, BoardStyle.Space.m).padding(.vertical, 10)
                }
                BoardRow(symbol: "bell", title: "Notifications",
                         detail: push.outsideWorkHours == .same ? "At any time"
                             : "Work hours " + NotificationSentence.workHours(push.schedule))
                    .boardButton("notification-settings") { pushRoute?(ProfileRoute.notifications) }
                // The row has the name of the page that it opens.
                BoardRow(symbol: "link", title: "Connections", detail: "Google, GitHub and other tools")
                    .boardButton("mcp-settings") { pushRoute?(ProfileRoute.connections) }
                // The repositories of GitHub that Coconut follows for the work of the business: a setting, so it is
                // here and not on Business.
                BoardRow(symbol: "shippingbox", title: "Repositories", value: repositorySummary)
                    .boardButton("profile-repositories") { pushRoute?(BusinessRoute.projects) }
            }
            // What the agents do now, what they cost and what they use. The Mac has Live and Spend in its sidebar.
            BoardSection("Agents") {
                #if os(iOS)
                BoardRow(symbol: "dot.radiowaves.left.and.right", title: "Live",
                         value: "\(LiveFeed.agents(bots: store.bots, tasks: store.tasks).count) working")
                    .boardButton("profile-live") { pushRoute?(LiveRoute()) }
                BoardRow(symbol: "dollarsign.circle", title: "Spend", value: store.spendTodayTotal)
                    .boardButton("profile-spend") { pushRoute?(SpendRoute()) }
                #endif
                // The place of this person on the leaderboard of the first team, so the row leads to it.
                BoardRow(symbol: "chart.bar", title: "Usage",
                         value: store.usage.flatMap { UsageLeaderboard.myPlace($0) }.map { "#\($0.rank) of \($0.of)" })
                    .boardButton("profile-usage") { pushRoute?(UsageRoute()) }
                // A Mac older than the setting has no route to change it, so the row waits for a Mac that has one.
                if let hours = store.autoArchiveHours { AutoArchiveMenu(hours: hours) }
            }
            BoardSection("Coconut") {
                if let coconut = store.bots.coconutChat {
                    // The line says where the settings went, so it reads whole on a narrow screen.
                    BoardRow(symbol: "bubble.left.and.bubble.right", title: "Ask Coconut",
                             detail: "Ask it to change the model, the access or a limit. Send /help to see all.", detailLines: 2)
                        .boardButton("profile-coconut-chat") {
                            if let openChat { openChat(coconut.id) } else { pushRoute?(ConversationRoute(botId: coconut.id)) }
                        }
                }
                BoardRow(symbol: "person.2", title: "Team", value: teamSummary)
                    .boardButton("profile-team") { pushRoute?(ProfileRoute.team) }
                // One row has the version and where Coconut runs.
                BoardRow(symbol: "info.circle", title: "Version", detail: "Runs on your Mac",
                         value: Self.appVersion, chevron: false)
                    .accessibilityIdentifier("profile-version")
            }
        }
        // An iPhone tab root has the name of its tab in the bar.
        #if os(iOS)
        .navigationTitle("You").inlineNavigationTitle()
        #endif
        // The Spend row of the iPhone has the cost of today, and the Usage row has the place of the person on the
        // leaderboard, so You reads the usage. The Team row names the team, so You reads the teams of a connected Mac.
        .task(id: store.connection) {
            await store.loadUsage()
            if store.connection == .connected { await store.loadTeam() }
            // The Repositories row names them.
            if store.connection == .connected || store.isDemo { try? await store.loadBusinessCatalog() }
        }
    }

    private func header(_ summary: ProfileSummary) -> some View {
        HStack(spacing: BoardStyle.Space.l) {
            if let member = summary.member {
                PersonFace(member: member, memberId: member.member, name: member.name, size: 56)
            } else {
                LetterFaceView(letters: summary.name.flatMap { LetterFace.letters($0) }, id: "profile", symbol: "person.fill", size: 56)
            }
            VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                #if os(iOS)
                // The bar and the tab say "You", so a person without a name has the name of the device here.
                // The person is the heading of this tab, large and bold.
                Text(summary.name ?? Device.name).font(.largeTitle.weight(.bold)).lineLimit(1).minimumScaleFactor(0.6)
                if summary.name != nil {
                    Text(Device.name).font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
                }
                #else
                Text(summary.name ?? "You").font(.title2.weight(.bold)).lineLimit(1)
                Text(Device.name).font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
                #endif
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
        .accessibilityIdentifier("profile-header")
    }

    /// The one repository by its name, else how many there are. Nothing before the Mac answers.
    private var repositorySummary: String? {
        guard let projects = store.businessCatalog?.projects else { return nil }
        return projects.count == 1 ? projects[0].name : "\(projects.count)"
    }

    private var teamSummary: String {
        let rooms = store.shownTeamRooms.filter { $0.status == .active }
        if rooms.isEmpty { return "None" }
        return rooms.count == 1 ? rooms[0].name : "\(rooms.count) teams"
    }

    static var appVersion: String {
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "Unknown"
        let build = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "Unknown"
        return "\(version) (\(build))"
    }
}

/// The pages of Profile, for a navigation stack of the iPhone tabs and of the Mac window.
struct ProfileDestinations: ViewModifier {
    func body(content: Content) -> some View {
        content.navigationDestination(for: ProfileRoute.self) { route in
            switch route {
            case .mac: MacConnectionSettingsView()
            case .notifications: NotificationSettingsView()
            case .connections: MCPSettingsView()
            case .team: TeamView()
            }
        }
    }
}

/// When a chat without activity archives itself on the Mac (AgentChats.archive_idle_chats), as a row of You. The person
/// asked for a place that changes it in one tap, so a tap opens the choices in place, with no page in between. The
/// Coconut chat changes it too ("archive chats after a week"), also to a time that the menu does not list.
struct AutoArchiveMenu: View {
    @Environment(AppStore.self) private var store
    let hours: Int

    var body: some View {
        Menu {
            Picker("Auto-archive", selection: Binding(get: { hours }, set: { store.setAutoArchive(hours: $0) })) {
                ForEach(AutoArchive.choices(with: hours), id: \.self) { Text(AutoArchive.label($0)).tag($0) }
            }
            .pickerStyle(.inline).labelsHidden()
        } label: {
            BoardRow(symbol: "archivebox", title: "Auto-archive", value: AutoArchive.label(hours), chevron: false, menu: true)
        }
        .menuStyle(.button).buttonStyle(.plain).menuIndicator(.hidden)
        .accessibilityLabel("Auto-archive, " + AutoArchive.label(hours))
        .accessibilityIdentifier("profile-auto-archive")
    }
}

/// The times of the auto-archive menu, in hours. 0 means never.
enum AutoArchive {
    static let choices = [24, 48, 168, 720, 0]

    /// The choices, with a time that the Coconut chat set in its place among them.
    static func choices(with current: Int) -> [Int] {
        guard !choices.contains(current) else { return choices }
        return (choices.filter { $0 != 0 } + [current]).sorted() + [0]
    }

    static func label(_ hours: Int) -> String {
        switch hours {
        case 0: "Never"
        case 1: "After 1 hour"
        case 24: "After 1 day"
        case 168: "After 1 week"
        case let value where value % 168 == 0: "After \(value / 168) weeks"
        case let value where value % 24 == 0: "After \(value / 24) days"
        default: "After \(hours) hours"
        }
    }
}
