import SwiftUI

/// The Team screen: make or join a team, and see its members (backend/coconut/team.py). Settings and the title of the
/// Team chat open it on an iPhone, and the Settings window opens it on a Mac. A team is people who share pages and talk
/// in one chat. A person shares a page on the page itself, so the screen has no share action. The chat is in the Chats
/// list, so the screen shows none.
struct TeamView: View {
    @Environment(AppStore.self) private var store

    private var screen: TeamScreen {
        TeamScreen(paired: store.hasPairing || store.isDemo, connection: store.connection, rooms: store.team,
                   error: store.teamLoadError)
    }

    var body: some View {
        let screen = screen
        content(screen)
            .teamBanners()
            .navigationTitle(title(screen)).inlineNavigationTitle()
            // A load without a connection does nothing, so the screen loads again when the Mac connects.
            .task(id: store.connection) { await store.loadTeam() }
    }

    private func title(_ screen: TeamScreen) -> String {
        if case .team(let room) = screen { return room.name }
        return "Team"
    }

    @ViewBuilder private func content(_ screen: TeamScreen) -> some View {
        switch screen {
        case .unpaired:
            TeamNotice(symbol: "link", text: "Pair with your Mac first. A team needs a Mac.") { PairMacButton() }
        case .loading:
            ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity).settingsBackground()
        case .offline:
            TeamNotice(symbol: "person.2", text: "Coconut shows your team when it reaches your Mac.") { EmptyView() }
        case .failed(let text):
            TeamNotice(symbol: "exclamationmark.circle", text: text) {
                Button("Try again") { Task { await store.loadTeam() } }
            }
        case .updateMac:
            TeamNotice(symbol: "arrow.down.circle", text: "Update Coconut on your Mac to use a team.") { EmptyView() }
        case .empty:
            TeamEmptyView()
        case .team(let room):
            TeamRoomView(roomId: room.room, pushed: false)
        case .teams(let rooms):
            Form {
                Section {
                    ForEach(rooms) { room in
                        NavigationLink { TeamRoomView(roomId: room.room, pushed: true) } label: {
                            SettingsNavigationLabel(title: room.name, subtitle: room.summary)
                        }
                        .accessibilityIdentifier("team-\(room.room)")
                    }
                } footer: {
                    Text("This Mac is in more than one team.")
                }
            }
            .teamForm()
            .refreshable { await store.loadTeam() }
        }
    }
}

extension TeamRoom {
    /// Why a team that is not active cannot be used, for its Team screen and its chat. Nil for an active team.
    var inactiveReason: String? {
        switch status {
        case .joining: "This Mac did not finish joining the team. Open the invite link again, or leave the team."
        case .removed: "You are no longer in this team."
        case .gone: "This team no longer exists."
        case .active: nil
        }
    }

    /// The line under the name of a team in a list.
    var summary: String {
        switch status {
        case .joining: return "Not joined yet"
        case .removed: return "You are no longer in this team"
        case .gone: return "This team no longer exists"
        case .active: return memberList.count == 1 ? "1 member" : "\(memberList.count) members"
        }
    }
}

/// One state of the screen without a team to show: a sentence, and at most one action.
private struct TeamNotice<Action: View>: View {
    let symbol: String
    let text: String
    @ViewBuilder let action: () -> Action

    var body: some View {
        ContentUnavailableView {
            Image(systemName: symbol).font(.largeTitle.weight(.light)).foregroundStyle(.secondary)
        } description: {
            Text(text)
        } actions: {
            action()
        }
        .settingsBackground()
    }
}

/// One quiet sentence over the actions of a form, in the style of an empty screen. It is no row, so nobody taps it.
private struct TeamFormNotice: View {
    let symbol: String
    let text: String

    var body: some View {
        Section {
            VStack(spacing: 8) {
                Image(systemName: symbol).font(.largeTitle.weight(.light)).foregroundStyle(.secondary)
                    .accessibilityHidden(true)
                Text(text).font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
            }
            .frame(maxWidth: .infinity).padding(.vertical, 12)
        }
        .listRowBackground(Color.clear)
    }
}

/// No team yet: what a team is for, and the two ways to have one.
private struct TeamEmptyView: View {
    @State private var creating = false
    @State private var joining = false

    var body: some View {
        Form {
            TeamFormNotice(symbol: "person.2", text: "People in a team share pages and talk in one chat.")
            Section {
                Button("Create a team") { creating = true }
                    .teamRowButton()
                    .accessibilityIdentifier("team-create")
                Button("Join with a link") { joining = true }
                    .teamRowButton()
                    .accessibilityIdentifier("team-join")
            }
        }
        .teamForm()
        .sheet(isPresented: $creating) { TeamCreateView() }
        .sheet(isPresented: $joining) { TeamJoinView(link: nil) }
    }
}

/// One team: its members, the bots that a teammate can ask, the invite, and Leave. A team that this Mac cannot use has
/// one sentence and one action instead.
struct TeamRoomView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let roomId: String
    /// True when the list of teams opened this page. The only team shows inside TeamView, which has the title and
    /// the banners.
    let pushed: Bool
    @State private var leaveOpen = false
    @State private var busy = false
    @State private var inviting = false
    @State private var joining = false

    private var room: TeamRoom? { store.team?.rooms.first { $0.room == roomId } }

    var body: some View {
        Group {
            if let room {
                Form { sections(room) }
                    .teamForm()
                    .disabled(busy)
                    .refreshable { await store.loadTeam() }
                    .sheet(isPresented: $joining) { TeamJoinView(link: nil) }
            }
        }
        .modifier(PushedTeamPage(pushed: pushed, title: room?.name ?? "Team"))
        // A team that left the list closes its page. When it is the only team, TeamView shows the empty screen.
        .onChange(of: room == nil) { _, gone in if gone && pushed { dismiss() } }
    }

    @ViewBuilder private func sections(_ room: TeamRoom) -> some View {
        switch room.status {
        case .removed, .gone:
            TeamFormNotice(symbol: "person.2.slash", text: room.inactiveReason ?? "")
            Section {
                Button("Remove from this Mac") { run { await store.leaveTeam(room) } }
                    .teamRowButton()
                    .accessibilityIdentifier("team-remove")
            }
        case .joining:
            TeamFormNotice(symbol: "person.2", text: room.inactiveReason ?? "")
            Section {
                Button("Join with the link again") { joining = true }
                    .teamRowButton()
                    .accessibilityIdentifier("team-join-again")
            }
            leaveSection(room)
        case .active:
            if let error = room.sync?.error {
                Section { InlineError(error) }
            }
            Section("Members") {
                ForEach(room.memberList) { member in memberRow(member, in: room) }
            }
            // The section shows only while the team has a bot that a teammate can ask.
            let bots = room.askableBots
            if !bots.isEmpty {
                Section("Bots") {
                    ForEach(bots) { botRow($0) }
                }
            }
            if room.showsInvite {
                Section {
                    Button {
                        inviting = true
                        run {
                            defer { inviting = false }
                            guard let link = await store.teamInviteLink(room) else { return false }
                            SystemShare.present(link)
                            return true
                        }
                    } label: {
                        HStack {
                            Text("Invite someone")
                            Spacer()
                            if inviting { ProgressView() }
                        }
                    }
                    .teamRowButton()
                    .accessibilityIdentifier("team-invite")
                } footer: {
                    Text("A link lets one person join, for one day.")
                }
            }
            leaveSection(room)
        }
    }

    @ViewBuilder private func leaveSection(_ room: TeamRoom) -> some View {
        Section {
            Button("Leave team", role: .destructive) { leaveOpen = true }
                .teamRowButton(destructive: true)
                .accessibilityIdentifier("team-leave")
                // On the button, so the dialog points at it where the system shows it as a popover.
                .confirmationDialog("Leave \(room.name)?", isPresented: $leaveOpen, titleVisibility: .visible) {
                    Button("Leave team", role: .destructive) { run { await store.leaveTeam(room) } }
                        .accessibilityIdentifier("team-leave-confirm")
                } message: {
                    Text("This Mac leaves the team. The pages of the team leave your Library, and the team no longer sees the pages that you share.")
                }
        }
    }

    @ViewBuilder private func memberRow(_ member: TeamMember, in room: TeamRoom) -> some View {
        let tags = [member.thisMac == true ? "You" : nil, member.admin == true ? "Admin" : nil].compactMap { $0 }
        let label = HStack {
            Text(member.name)
            Spacer()
            if !tags.isEmpty { Text(tags.joined(separator: " · ")).foregroundStyle(.secondary) }
        }
        if member.thisMac == true {
            NavigationLink { TeamNameView(roomId: room.room, name: member.name) } label: { label }
                .accessibilityIdentifier("team-member-you")
        } else {
            label
        }
    }

    private func botRow(_ bot: TeamRoomBot) -> some View {
        HStack(spacing: 12) {
            BotAvatar(symbol: bot.avatar ?? "", color: bot.color ?? "", size: avatarSize, identity: bot.id,
                      animated: false)
            VStack(alignment: .leading, spacing: 2) {
                Text(bot.name)
                if let owner = bot.memberName {
                    Text(owner).font(.subheadline).foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 0)
        }
        .accessibilityElement(children: .combine)
    }

    @ScaledMetric(relativeTo: .body) private var avatarSize = 32.0

    /// One action at a time. The form stays disabled until the Mac answers.
    private func run(_ action: @escaping () async -> Bool) {
        busy = true
        store.error = nil
        Task {
            _ = await action()
            busy = false
        }
    }
}

/// The name that the team sees for this person.
struct TeamNameView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let roomId: String
    let name: String
    @State private var edited = ""
    @State private var saving = false
    @FocusState private var focused: Bool

    private var trimmed: String { edited.trimmingCharacters(in: .whitespacesAndNewlines) }

    var body: some View {
        Form {
            Section {
                TextField("Your name", text: $edited, prompt: Text("Your name")).textContentType(.name)
                    .fieldUnderHeader()
                    .submitLabel(.done).onSubmit(save)
                    .focused($focused)
                    .accessibilityIdentifier("team-member-name")
            } footer: {
                Text("The team sees this name.")
            }
        }
        .teamForm()
        .teamBanners()
        .disabled(saving)
        .navigationTitle("Your name").inlineNavigationTitle()
        .toolbar {
            ToolbarItem(placement: .confirmationAction) {
                Button(saving ? "Saving…" : "Save", action: save)
                    .disabled(saving || trimmed.isEmpty || trimmed == name)
                    .accessibilityIdentifier("team-member-name-save")
            }
        }
        .onAppear { edited = name; focused = true }
    }

    private func save() {
        guard let room = store.team?.rooms.first(where: { $0.room == roomId }), !trimmed.isEmpty, trimmed != name else { return }
        saving = true
        store.error = nil
        Task {
            if await store.renameTeamMember(room, to: trimmed) { dismiss() }
            saving = false
        }
    }
}

/// Make a team: its name, and the name that the team sees for this person.
struct TeamCreateView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var memberName = TeamMemberName.remembered()
    @State private var saving = false
    @FocusState private var nameFocused: Bool

    private var canCreate: Bool {
        !saving && !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !memberName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Team name") {
                    TextField("Team name", text: $name, prompt: Text("Team name"))
                        .fieldUnderHeader()
                        .focused($nameFocused)
                        .accessibilityIdentifier("team-name")
                }
                Section {
                    TextField("Your name", text: $memberName, prompt: Text("Your name")).textContentType(.name)
                        .fieldUnderHeader()
                        .submitLabel(.done).onSubmit { if canCreate { create() } }
                        .accessibilityIdentifier("team-member-name")
                } header: {
                    Text("Your name")
                } footer: {
                    Text("The team sees this name.")
                }
            }
            .teamForm()
            .teamBanners()
            .disabled(saving)
            #if os(macOS)
            .frame(width: 420, height: 320)
            #endif
            .navigationTitle("New team").inlineNavigationTitle()
            // The first field takes the keyboard, so the person types at once.
            .onAppear { nameFocused = true }
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button(saving ? "Creating…" : "Create", action: create)
                        .disabled(!canCreate)
                        .accessibilityIdentifier("team-create-save")
                }
            }
        }
    }

    private func create() {
        saving = true
        store.error = nil
        Task {
            let made = await store.createTeam(name: name.trimmingCharacters(in: .whitespacesAndNewlines),
                                              memberName: memberName.trimmingCharacters(in: .whitespacesAndNewlines))
            saving = false
            if made { dismiss() }
        }
    }
}

/// Join a team with an invite link: a tapped link, or one that the person pastes. After a join from a tapped link,
/// the sheet shows the team, so the person sees what the join did.
struct TeamJoinView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    /// A tapped link. Nil when the person pastes one.
    let link: String?
    @State private var pasted: String?
    @State private var memberName = TeamMemberName.remembered()
    @State private var joining = false
    @State private var joined = false

    private var current: String? { link ?? pasted }
    private var check: TeamLinkCheck? { current.map { TeamLinkCheck(link: $0) } }
    private var teamName: String? { if case .team(let name) = check { name } else { nil } }
    private var canJoin: Bool {
        !joining && teamName != nil && !memberName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var body: some View {
        NavigationStack {
            Group {
                if joined {
                    TeamView()
                } else {
                    form
                }
            }
            #if os(macOS)
            .frame(width: 440, height: joined ? 520 : 300)
            #endif
            .toolbar {
                if joined {
                    ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
                } else {
                    ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                    if store.hasPairing && teamName != nil {
                        ToolbarItem(placement: .confirmationAction) {
                            Button(joining ? "Joining…" : "Join", action: join)
                                .disabled(!canJoin)
                                .accessibilityIdentifier("team-join-save")
                        }
                    }
                }
            }
        }
    }

    private var form: some View {
        Form {
            if !store.hasPairing {
                Section {
                    Text("Pair with your Mac first, then open the link again.")
                    PairMacButton()
                }
            } else if let teamName {
                Section {
                    Text("Join \(teamName)?").font(.title2.weight(.semibold))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .accessibilityAddTraits(.isHeader)
                        .accessibilityIdentifier("team-join-title")
                }
                .listRowBackground(Color.clear)
                Section {
                    TextField("Your name", text: $memberName, prompt: Text("Your name")).textContentType(.name)
                        .fieldUnderHeader()
                        .submitLabel(.join).onSubmit { if canJoin { join() } }
                        .accessibilityIdentifier("team-member-name")
                } header: {
                    Text("Your name")
                } footer: {
                    Text("The team sees this name.")
                }
            } else {
                Section {
                    PasteButton(payloadType: String.self) { values in
                        // The paste control gives the text only after the tap of the person.
                        Task { @MainActor in pasted = values.first }
                    }
                    .accessibilityIdentifier("team-paste")
                } footer: {
                    if case .invalid(let reason) = check {
                        Text(reason)
                    } else {
                        Text("Paste the invite link that you got.")
                    }
                }
            }
        }
        .teamForm()
        .teamBanners()
        .disabled(joining)
        .navigationTitle(teamName == nil ? "Join a team" : "").inlineNavigationTitle()
    }

    private func join() {
        guard let current else { return }
        joining = true
        store.error = nil
        Task {
            let room = await store.joinTeam(link: current, memberName: memberName.trimmingCharacters(in: .whitespacesAndNewlines))
            joining = false
            guard room != nil else { return }
            if link == nil { dismiss() } else { joined = true }
        }
    }
}

/// The row of the Team screen in Settings.
struct TeamSettingsRow: View {
    @Environment(AppStore.self) private var store

    var body: some View {
        NavigationLink { TeamView() } label: {
            SettingsNavigationLabel(title: "Team", subtitle: summary)
        }
        .accessibilityIdentifier("team-settings")
    }

    private var summary: String {
        guard let rooms = store.team?.rooms, !rooms.isEmpty else { return "Share pages with people" }
        return rooms.map(\.name).joined(separator: ", ")
    }
}

/// The notices of the app on each page of the Team screen: the connection to the Mac, and the error of an action
/// with the text of the Mac.
private struct TeamBanners: ViewModifier {
    @Environment(AppStore.self) private var store
    /// True: the banners stand above the page, which then starts below them. A chat scrolls its rows under a top
    /// inset, so there an inset would cover a message.
    let above: Bool
    @State private var pairing = false

    func body(content: Content) -> some View {
        Group {
            if above {
                VStack(spacing: 0) {
                    banners
                    content
                }
            } else {
                content.safeAreaInset(edge: .top, spacing: 0) { banners }
            }
        }
        .fullScreen(isPresented: $pairing) { OnboardingView() }
    }

    @ViewBuilder private var banners: some View {
        let notice = store.isDemo ? nil : store.connection.notice
        if notice != nil || store.error != nil {
            VStack(spacing: 8) {
                if let notice { ConnectionBanner(notice: notice) { pairing = true } }
                if let error = store.error { ErrorBanner(message: error) { store.error = nil } }
            }
            .padding(.horizontal, 16).padding(.vertical, 8)
        }
    }
}

/// The title and the banners of a team page that the list of teams opened.
private struct PushedTeamPage: ViewModifier {
    let pushed: Bool
    let title: String

    func body(content: Content) -> some View {
        if pushed {
            content.teamBanners().navigationTitle(title).inlineNavigationTitle()
        } else {
            content
        }
    }
}

extension View {
    /// The banners of the connection and of a failed action, for a page of a team: the Team screen and the chat.
    func teamBanners(above: Bool = false) -> some View { modifier(TeamBanners(above: above)) }

    /// A button that is a row of a form. A Mac draws a bezel around a button in a form, and its rows have none.
    /// A Mac draws a borderless button in the tint of the app, so a destructive one names its red itself.
    @ViewBuilder fileprivate func teamRowButton(destructive: Bool = false) -> some View {
        #if os(macOS)
        if destructive { buttonStyle(.borderless).foregroundStyle(.red) } else { buttonStyle(.borderless) }
        #else
        self
        #endif
    }

    /// A form of the Team screen in the style of Settings. A Mac form keeps each section in a box.
    fileprivate func teamForm() -> some View {
        #if os(macOS)
        formStyle(.grouped).settingsBackground()
        #else
        settingsBackground()
        #endif
    }
}
