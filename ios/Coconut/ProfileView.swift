import SwiftUI

/// A page that Profile opens: a thing of this device, or a setting that every chat shares.
enum ProfileRoute: Hashable {
    case mac, notifications, connections, model, access, parallelWork, team
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

/// The Profile tab: the person, this device, the settings of every chat, and Coconut itself. Coconut has no Settings
/// screen: the few things that need this device are here, and the Coconut chat changes the rest.
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
                BoardRow(symbol: "link", title: "Sign-ins and connections", detail: "Google, GitHub and other tools")
                    .boardButton("mcp-settings") { pushRoute?(ProfileRoute.connections) }
            }
            BoardSection("All chats") {
                // The row is a menu of the models. Its last line opens the page with the effort and the fallback.
                ModelMenu(settings: { pushRoute?(ProfileRoute.model) }) { _ in
                    BoardRow(symbol: "cpu", title: "Model", value: modelSummary, chevron: false, menu: true)
                }
                .accessibilityIdentifier("profile-model")
                BoardRow(symbol: "lock.shield", title: "Access",
                         value: store.approvals?.label(for: store.approvals?.defaults) ?? "Hermes default")
                    .boardButton("profile-access") { pushRoute?(ProfileRoute.access) }
                BoardRow(symbol: "arrow.triangle.branch", title: "Parallel work", value: parallelWorkSummary)
                    .boardButton("parallel-work-settings") { pushRoute?(ProfileRoute.parallelWork) }
            }
            BoardSection("Coconut") {
                if let coconut = store.bots.coconutChat {
                    BoardRow(symbol: "bubble.left.and.bubble.right", title: "Ask Coconut",
                             detail: "Change anything else in the Coconut chat")
                        .boardButton("profile-coconut-chat") {
                            if let openChat { openChat(coconut.id) } else { pushRoute?(ConversationRoute(botId: coconut.id)) }
                        }
                }
                BoardRow(symbol: "server.rack", title: "Runs on", detail: "\(CoconutMode.local.label). \(CoconutMode.cloud.label) comes later.",
                         value: "Your Mac", chevron: false)
                    .accessibilityIdentifier("profile-mode")
                BoardRow(symbol: "person.2", title: "Team", value: teamSummary)
                    .boardButton("profile-team") { pushRoute?(ProfileRoute.team) }
                BoardRow(symbol: "chart.bar", title: "Usage", detail: "Turns, tokens, cost and time of the agents")
                    .boardButton("profile-usage") { pushRoute?(UsageRoute()) }
                BoardRow(symbol: "dollarsign.circle", title: "Spend", value: store.spendTodayTotal)
                    .boardButton("profile-spend") { pushRoute?(SpendRoute()) }
            }
            BoardSection("About") {
                BoardRow(symbol: "info.circle", title: "Version", detail: "Execution stays on your Mac",
                         value: Self.appVersion, chevron: false)
                    .accessibilityIdentifier("profile-version")
            }
        }
        // The Team row names the team, so Profile reads the teams of a connected Mac.
        .task(id: store.connection) { if store.connection == .connected { await store.loadTeam() } }
    }

    private func header(_ summary: ProfileSummary) -> some View {
        HStack(spacing: BoardStyle.Space.l) {
            if let member = summary.member {
                PersonFace(member: member, memberId: member.member, name: member.name, size: 56)
            } else {
                LetterFaceView(letters: summary.name.flatMap { LetterFace.letters($0) }, id: "profile", symbol: "person.fill", size: 56)
            }
            VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                Text(summary.name ?? "You").font(.title2.weight(.bold)).lineLimit(1)
                Text(Device.name).font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
        .accessibilityIdentifier("profile-header")
    }

    private var teamSummary: String {
        let rooms = store.shownTeamRooms.filter { $0.status == .active }
        if rooms.isEmpty { return "None" }
        return rooms.count == 1 ? rooms[0].name : "\(rooms.count) teams"
    }

    private var parallelWorkSummary: String {
        guard let capacity = store.workCapacity else { return "Automatic" }
        let total = capacity.limit == nil ? "Automatic · up to \(capacity.effectiveLimit)" : "Up to \(capacity.effectiveLimit)"
        guard let agent = store.agentLimit else { return total }
        return total + " · \(agent.effectiveLimit) per agent"
    }

    private var modelSummary: String {
        guard let catalog = store.modelCatalog else { return store.model.isEmpty ? "Set on your Mac" : store.model }
        return catalog.name(for: catalog.defaults.selection)
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
            case .model: ModelSettingsView()
            case .access: ApprovalSettingsView()
            case .parallelWork: ParallelWorkSettingsView()
            case .team: TeamView()
            }
        }
    }
}
