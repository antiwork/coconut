import SwiftUI

/// The Team screen: make or join a team, and see its members (backend/coconut/team.py). Profile and the title of the
/// Team chat open it on an iPhone and on a Mac. A team is people who share pages and talk
/// in one chat. A person shares a page on the page itself, so the screen has no share action. The chat is in the Chats
/// list, so the screen shows none.
struct TeamView: View {
    @Environment(AppStore.self) private var store
    /// The title of the screen without a team to name. The People tab shows the screen under its own name.
    var emptyTitle = "Team"

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
        return emptyTitle
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
        case .active: return people.count == 1 ? "1 member" : "\(people.count) members"
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

/// One team: its people, the bots of its Macs for a team only, the invite, and Leave. A team that this Mac cannot use
/// has one sentence and one action instead.
struct TeamRoomView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @Environment(\.showTeamChat) private var showTeamChat
    let roomId: String
    /// True when the list of teams opened this page. The only team shows inside TeamView, which has the title and
    /// the banners.
    let pushed: Bool
    @State private var leaveOpen = false
    @State private var busy = false
    @State private var inviting = false
    @State private var joining = false
    /// The choice of the switch while its request runs. Without it, the switch shows the state of the Mac.
    @State private var notificationChoice: Bool?
    /// The member that the confirmation of a removal asks about.
    @State private var removing: TeamRemoval?
    /// The sheet of a new chat (nil inside) or of a new name for a chat.
    @State private var naming: TeamChatNaming?
    /// The chat that the confirmation of a delete asks about.
    @State private var deletingChat: TeamNamedChat?

    private var room: TeamRoom? { store.team?.rooms.first { $0.room == roomId } }

    var body: some View {
        Group {
            if let room {
                // A choice to mute shows at once, while its request runs.
                Form { sections(TeamChatMute.applying(store.teamChatMuteChoices, to: room)) }
                    .teamForm()
                    .disabled(busy)
                    .refreshable { await store.loadTeam() }
                    .sheet(isPresented: $joining) { TeamJoinView(link: nil) }
                    // An alert, not a popover: it keeps a visible Cancel and fits the largest text, on an iPhone
                    // and on a Mac.
                    .alert(removing?.title ?? "", isPresented: Binding(
                        get: { removing != nil }, set: { if !$0 { removing = nil } }), presenting: removing) { removal in
                        Button("Remove", role: .destructive) {
                            run { await store.removeTeamMember(removal.member.member, from: room) }
                        }
                        .accessibilityIdentifier("team-remove-confirm")
                        Button("Cancel", role: .cancel) {}
                    } message: { removal in
                        Text(removal.message)
                    }
                    .alert(TeamChatManagement.deleteTitle, isPresented: Binding(
                        get: { deletingChat != nil }, set: { if !$0 { deletingChat = nil } }),
                           presenting: deletingChat) { chat in
                        Button(TeamChatManagement.deleteConfirm, role: .destructive) {
                            run { await store.deleteTeamChat(room, chat: chat.id) }
                        }
                        .accessibilityIdentifier("team-chat-delete-confirm")
                        Button("Cancel", role: .cancel) {}
                    } message: { _ in
                        Text(TeamChatManagement.deleteMessage)
                    }
                    .sheet(item: $naming) { naming in TeamChatNameView(room: room, chat: naming.chat) }
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
            if TeamChatManagement.showsSection(room) {
                chatsSection(room)
            }
            // The notice board of the team, apart from its chats.
            if room.notices != nil {
                Section {
                    NavigationLink { NoticeBoardView(roomId: room.room) } label: {
                        Label("Notices", systemImage: "megaphone")
                            .badge(room.unseenNotices)
                    }
                    .accessibilityIdentifier("team-notices")
                }
            }
            Section("Members") {
                ForEach(room.people) { member in memberRow(member, in: room) }
            }
            let botRows = TeamBotSection.rows(of: room)
            if !botRows.isEmpty {
                Section {
                    ForEach(botRows) { botRow($0, in: room) }
                } header: {
                    Text("Agents")
                } footer: {
                    if let line = TeamBotSection.footer(for: botRows) { Text(line) }
                }
            }
            // An older service and a Mac for a team only send no `notifications`, so the screen has no switch.
            if let enabled = room.notifications {
                Section {
                    Toggle("Notifications", isOn: Binding(get: { notificationChoice ?? enabled },
                                                          set: { setNotifications($0, in: room) }))
                        .tint(BoardStyle.green)
                        // One request at a time, so an earlier choice cannot reach the Mac after a later one.
                        .disabled(notificationChoice != nil)
                        .accessibilityIdentifier("team-notifications")
                } footer: {
                    Text(TeamNotificationFooter.text(enabled: notificationChoice ?? enabled, room: room))
                        .accessibilityIdentifier("team-notifications-footer")
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

    /// The named chats of the team, each a row that opens the chat. Each member has Mute or Unmute in the menu of each
    /// row (TeamChatMute). The admin also has "New chat", and Rename and Delete on each row: the swipe actions on an
    /// iPhone and the menu of the row on both (TeamChatManagement).
    private func chatsSection(_ room: TeamRoom) -> some View {
        let manages = TeamChatManagement.allows(room)
        return Section("Chats") {
            ForEach(room.namedChats) { chat in
                chatRow(chat, in: room)
                    .modifier(TeamChatActions(
                        manages: manages, mute: TeamChatMute.action(chat, in: room).map { label in
                            TeamChatActions.Mute(label: label, symbol: TeamChatMute.symbol(chat),
                                                 waits: TeamChatMute.waits(chat.id, store.teamChatMuteChoices)) {
                                setMuted(chat, in: room)
                            }
                        },
                        rename: { naming = TeamChatNaming(chat: chat) }, delete: { deletingChat = chat }))
            }
            if manages {
                Button(TeamChatManagement.newChat) { naming = TeamChatNaming(chat: nil) }
                    .teamRowButton()
                    .accessibilityIdentifier("team-new-chat")
            }
        }
    }

    /// The name of a chat with the arrow of a row that opens something, as the row of a person with a direct chat.
    private func chatRow(_ chat: TeamNamedChat, in room: TeamRoom) -> some View {
        Button { showTeamChat?(TeamChatKey(room: room.room, chat: chat.id)) } label: {
            HStack(spacing: 8) {
                Text(chat.name).frame(maxWidth: .infinity, alignment: .leading)
                if TeamChatMute.showsMark(chat) {
                    // In the font of the name, so the mark is as high as the line of the name.
                    Image(systemName: TeamChatMute.mark)
                        .foregroundStyle(.secondary)
                        .accessibilityLabel(TeamChatMute.spoken)
                        .accessibilityIdentifier("team-chat-muted-\(chat.id)")
                }
                Image(systemName: "chevron.right")
                    .font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
                    .accessibilityHidden(true)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityHint("Opens the chat")
        .accessibilityIdentifier("team-chat-row-\(chat.id)")
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

    /// A row of a member, with the actions of a removal when this person is the admin (TeamRemoval).
    private func memberRow(_ member: TeamMember, in room: TeamRoom) -> some View {
        let message: (() -> Void)? = room.directMember(member.member) != nil
            ? showTeamChat.map { open in { open(TeamChatKey(room: room.room, member: member.member)) } } : nil
        return memberLabel(member, in: room)
            .teamRemovalActions(TeamRemoval(member: member.member, in: room), message: message) { removing = $0 }
    }

    /// The face, the name and the tags of a member. The row of this person opens the name editor. The row of a person
    /// that can get a direct message (TeamRoom.directMember) opens the direct chat with that person. Each other row
    /// opens nothing.
    @ViewBuilder private func memberLabel(_ member: TeamMember, in room: TeamRoom) -> some View {
        let tags = [member.thisMac == true ? "You" : nil, member.admin == true ? "Admin" : nil].compactMap { $0 }
        let label = HStack(spacing: 12) {
            PersonFace(member: member, memberId: member.member, name: member.name, size: avatarSize)
            TeamRowText(name: member.name, note: tags.isEmpty ? nil : tags.joined(separator: " · "))
        }
        if member.thisMac == true {
            NavigationLink { TeamNameView(roomId: room.room, name: member.name) } label: { label }
                .accessibilityIdentifier("team-member-you")
        } else if room.directMember(member.member) != nil, let showTeamChat {
            Button { showTeamChat(TeamChatKey(room: room.room, member: member.member)) } label: {
                HStack(spacing: 8) {
                    label
                    // The arrow of a row that opens something, as a navigation link draws it.
                    Image(systemName: "chevron.right")
                        .font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
                        .accessibilityHidden(true)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityElement(children: .combine)
            .accessibilityHint(TeamDirectText.opens)
            .accessibilityIdentifier("team-member-\(member.member)")
        } else {
            label
        }
    }

    /// A face, the name and the quiet text, as the row of a member has. A Mac without a bot has a quiet symbol of a
    /// Mac in the space of the face, so each name of the section starts at the same edge. The row of a bot that can
    /// get a direct message (TeamBotSection.directBot) opens the direct chat with it, in the form of the row of a
    /// person with a direct chat.
    private func botRow(_ row: TeamBotRow, in room: TeamRoom) -> some View {
        let direct = TeamBotSection.directBot(of: row, in: room)
        let open: (() -> Void)? = direct.flatMap { bot in
            showTeamChat.map { show in { show(TeamChatKey(room: room.room, bot: bot.id)) } }
        }
        let label = HStack(spacing: 12) {
            // A bot has no face. A Mac without a bot has a quiet symbol of a Mac.
            if row.bot == nil {
                Image(systemName: "desktopcomputer")
                    .font(.system(size: avatarSize * 0.6))
                    .foregroundStyle(.secondary)
                    .frame(width: avatarSize * BoardStyle.faceScale, height: avatarSize * BoardStyle.faceScale)
                    .accessibilityHidden(true)
            }
            TeamRowText(name: row.name, note: row.note)
        }
        return Group {
            if let open {
                Button(action: open) {
                    HStack(spacing: 8) {
                        label
                        // The arrow of a row that opens something, as a navigation link draws it.
                        Image(systemName: "chevron.right")
                            .font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
                            .accessibilityHidden(true)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(row.spoken)
                .accessibilityHint(TeamDirectText.opens)
                .accessibilityAddTraits(.isButton)
            } else {
                label
                    // The whole row takes the right click and the long press, also the space after the name.
                    .contentShape(Rectangle())
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(row.spoken)
            }
        }
        .accessibilityIdentifier(row.id)
        // A row of a Mac for a team only removes that Mac, with each of its bots.
        .teamRemovalActions(TeamRemoval(member: row.member, in: room), message: nil) { removing = $0 }
    }

    @ScaledMetric(relativeTo: .body) private var avatarSize = 32.0

    /// Saves the switch at once. It shows the new state while the Mac answers. Then it shows the state of the Mac:
    /// the new one after a success, and the old one after a failure, with the error in the banner of the screen.
    private func setNotifications(_ enabled: Bool, in room: TeamRoom) {
        guard notificationChoice == nil else { return }
        notificationChoice = enabled
        store.error = nil
        Task {
            _ = await store.setTeamNotifications(room.room, enabled: enabled)
            notificationChoice = nil
        }
    }

    /// Mutes or unmutes a named chat, as the switch of the team saves its choice: the row shows the new state at once
    /// (AppStore.teamChatMuteChoices), and the action of the chat waits. Then the row shows the state of the Mac: the
    /// new one after a success, and the old one after a failure, with the error in the banner of the screen.
    private func setMuted(_ chat: TeamNamedChat, in room: TeamRoom) {
        guard let enabled = chat.notifications else { return }
        store.error = nil
        Task { await store.setTeamChatNotifications(room.room, chat: chat.id, enabled: !enabled) }
    }

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

/// The actions of a removal on a row of the Team screen. An iPhone has a swipe to the left and a long press, and a Mac
/// has a right click. Each one asks first (TeamRoomView). VoiceOver offers the swipe action and the menu of the row. A
/// row without a removal keeps its plain form, so a member that is not the admin sees nothing new.
private struct TeamRemovalActions: ViewModifier {
    let removal: TeamRemoval?
    /// Opens the direct chat with the person of the row, or nil when the person cannot get a direct message.
    let message: (() -> Void)?
    let remove: (TeamRemoval) -> Void

    func body(content: Content) -> some View {
        if let removal {
            content
                #if os(iOS)
                .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                    // No destructive role: that role takes the row away at once, before the confirmation.
                    Button("Remove") { remove(removal) }
                        .tint(.red)
                        .accessibilityIdentifier("team-remove-swipe")
                }
                #endif
                .contextMenu {
                    if let message {
                        Button("Message \(removal.member.name)", systemImage: "bubble.left", action: message)
                    }
                    Button("Remove from team", systemImage: "person.crop.circle.badge.minus", role: .destructive) {
                        remove(removal)
                    }
                    .accessibilityIdentifier("team-remove-menu")
                }
        } else {
            content
        }
    }
}

/// The sheet of the Team screen for a chat: a new chat, or a new name for `chat`.
private struct TeamChatNaming: Identifiable {
    let id = UUID()
    let chat: TeamNamedChat?
}

/// The actions on the row of a named chat. Each member has Mute or Unmute in the menu of the row only. The admin also
/// has Rename and Delete: the swipe actions of an iPhone and the menu of the row. Delete asks first (TeamRoomView). A
/// row without an action keeps its plain form.
private struct TeamChatActions: ViewModifier {
    /// Mute or Unmute (TeamChatMute).
    struct Mute {
        let label: String
        let symbol: String
        /// A request of this chat runs: the action waits for it.
        let waits: Bool
        let action: () -> Void
    }

    let manages: Bool
    let mute: Mute?
    let rename: () -> Void
    let delete: () -> Void

    func body(content: Content) -> some View {
        if manages || mute != nil {
            content
                #if os(iOS)
                // Buttons with a symbol only, as in Mail and Messages. VoiceOver reads the title of each label.
                // Rename has the neutral gray of a swipe action: without a tint, the tint of the app draws white on
                // white. A member who is not the admin gets no swipe.
                .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                    if manages {
                        // No destructive role: that role takes the row away at once, before the confirmation.
                        Button(action: delete) { Label("Delete", systemImage: "trash").labelStyle(.iconOnly) }
                            .tint(.red)
                            .accessibilityIdentifier("team-chat-delete-swipe")
                        Button(action: rename) { Label("Rename", systemImage: "pencil").labelStyle(.iconOnly) }
                            .tint(.gray)
                            .accessibilityIdentifier("team-chat-rename-swipe")
                    }
                }
                #endif
                .contextMenu {
                    if let mute {
                        Button(mute.label, systemImage: mute.symbol, action: mute.action)
                            .disabled(mute.waits)
                            .accessibilityIdentifier("team-chat-mute-menu")
                    }
                    if manages {
                        Button("Rename", systemImage: "pencil", action: rename)
                            .accessibilityIdentifier("team-chat-rename-menu")
                        Button("Delete", systemImage: "trash", role: .destructive, action: delete)
                            .accessibilityIdentifier("team-chat-delete-menu")
                    }
                }
        } else {
            content
        }
    }
}

extension View {
    fileprivate func teamRemovalActions(_ removal: TeamRemoval?, message: (() -> Void)?,
                                        remove: @escaping (TeamRemoval) -> Void) -> some View {
        modifier(TeamRemovalActions(removal: removal, message: message, remove: remove))
    }
}

/// The name of a row of the Team screen, and its quiet text at the right end, such as "You · Admin". When both do not
/// fit on one line, the text goes under the name, so neither is cut.
private struct TeamRowText: View {
    let name: String
    let note: String?

    var body: some View {
        if let note {
            ViewThatFits(in: .horizontal) {
                HStack {
                    Text(name).lineLimit(1)
                    Spacer()
                    Text(note).foregroundStyle(.secondary).lineLimit(1)
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text(name)
                    Text(note).foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        } else {
            Text(name).frame(maxWidth: .infinity, alignment: .leading)
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

/// Make a named chat, or give one a new name, in the form of TeamCreateView: one field under "Chat name", Cancel, and
/// Create or Save. An error of the Mac shows in the banner of the sheet.
struct TeamChatNameView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let room: TeamRoom
    /// The chat of a rename. Nil for a new chat.
    let chat: TeamNamedChat?
    @State private var name = ""
    @State private var saving = false
    @FocusState private var focused: Bool

    private var trimmed: String { name.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var canSave: Bool { !saving && !trimmed.isEmpty && trimmed != chat?.name }

    var body: some View {
        NavigationStack {
            Form {
                Section(TeamChatManagement.nameHeader) {
                    TextField(TeamChatManagement.nameHeader, text: $name, prompt: Text(TeamChatManagement.nameHeader))
                        .fieldUnderHeader()
                        .focused($focused)
                        .submitLabel(.done).onSubmit { if canSave { save() } }
                        .accessibilityIdentifier("team-chat-name")
                }
            }
            .teamForm()
            .teamBanners()
            .disabled(saving)
            #if os(macOS)
            .frame(width: 420, height: 220)
            #endif
            .navigationTitle(chat == nil ? TeamChatManagement.newChat : "Rename chat").inlineNavigationTitle()
            .onAppear { name = chat?.name ?? ""; focused = true }
            // The error of the Mac is about this form, so it goes with the sheet and does not stay on the Team screen.
            .onDisappear { store.error = nil }
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button(chat == nil ? (saving ? "Creating…" : "Create") : (saving ? "Saving…" : "Save"), action: save)
                        .disabled(!canSave)
                        .accessibilityIdentifier("team-chat-name-save")
                }
            }
        }
    }

    private func save() {
        saving = true
        store.error = nil
        Task {
            let done = if let chat { await store.renameTeamChat(room, chat: chat.id, name: trimmed) }
                       else { await store.createTeamChat(room, name: trimmed) != nil }
            saving = false
            if done { dismiss() }
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
                    Text("Join \(teamName)?").font(.title3.weight(.semibold))
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

/// The notices of the app on each page of the Team screen: the connection to the Mac, and the error of an action
/// with the text of the Mac.
private struct TeamBanners: ViewModifier {
    @Environment(AppStore.self) private var store
    @ShownError private var shownError
    /// True for a chat: the connection banner stands above the chat, which then starts below it. A chat scrolls its
    /// rows under a top inset, so there an inset would cover a message. The error floats over the chat (ChatErrorNotice).
    let above: Bool
    @State private var pairing = false

    func body(content: Content) -> some View {
        Group {
            if above {
                VStack(spacing: 0) {
                    banners(area: nil)
                    content
                }
            } else {
                content.topBanners { banners(area: $0) }
            }
        }
        .fullScreen(isPresented: $pairing) { OnboardingView() }
    }

    /// The banners. The area is the height of the page, or nil above a chat, where no error shows.
    @ViewBuilder private func banners(area: CGFloat?) -> some View {
        let notice = store.isDemo ? nil : store.connection.notice
        let error = above ? nil : shownError
        if notice != nil || error != nil {
            VStack(spacing: 8) {
                if let notice { ConnectionBanner(notice: notice) { pairing = true } }
                // The page less the padding above and under the column.
                if let error { ErrorBanner(message: error, area: area.map { $0 - 16 }) { store.error = nil } }
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

/// Makes a new invite link of a team and opens the share sheet with it. The team chat and the menu of the team's row
/// in the chat list have it, so an invite needs no visit to the Team screen. Each link lets one person join, for one day.
struct TeamInviteButton<Label: View>: View {
    @Environment(AppStore.self) private var store
    let room: TeamRoom
    @ViewBuilder let label: (_ inviting: Bool) -> Label
    @State private var inviting = false

    var body: some View {
        Button {
            inviting = true
            Task {
                defer { inviting = false }
                if let link = await store.teamInviteLink(room) { SystemShare.present(link) }
            }
        } label: { label(inviting) }
        .disabled(inviting)
        .accessibilityIdentifier("team-invite-\(room.room)")
    }
}
