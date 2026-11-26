import SwiftUI

/// The People page of the Mac as data: each team with its chats, its board and its members, and the direct chats with a person.
struct PeopleSections: Equatable {
    struct Team: Identifiable, Equatable {
        let room: TeamRoom
        /// The Team chat, then the named chats in the order of the Mac.
        let chats: [ChatListItem]
        /// The people of the team: this person first, then the others by name.
        let members: [TeamMember]

        var id: String { room.room }
        /// The team has a notice board. An older service has none.
        var hasBoard: Bool { room.status == .active && room.notices != nil }
        /// "4 members · 2 agents".
        var detail: String {
            guard room.status == .active else { return room.summary }
            let agents = room.botList.count
            return agents == 0 ? room.summary : room.summary + " · \(agents) \(agents == 1 ? "agent" : "agents")"
        }
    }

    let teams: [Team]
    /// The direct chats with a person of each team, newest activity first.
    let directs: [ChatListItem]

    /// "2 teams · 7 people". The same person in two teams counts one time for each team.
    var detail: String {
        let people = teams.reduce(0) { $0 + $1.members.count }
        return "\(teams.count) \(teams.count == 1 ? "team" : "teams") · \(people) \(people == 1 ? "person" : "people")"
    }

    init(rooms: [TeamRoom], local: [TeamChatKey: TeamLastMessage] = [:]) {
        teams = rooms.map { room in
            let active = room.status == .active
            let chats = active && room.hasChat ? [ChatListItem.team(room)] + room.namedChatRooms.map(ChatListItem.team) : []
            let members = room.people.sorted {
                ($0.thisMac == true ? 0 : 1, $0.name.lowercased(), $0.member) < ($1.thisMac == true ? 0 : 1, $1.name.lowercased(), $1.member)
            }
            return Team(room: room, chats: chats, members: active ? members : [])
        }
        directs = ChatList.people(rooms, local: local).filter { if case .direct = $0 { true } else { false } }
    }
}

/// The People page: the direct chats, then each team with its chats, its notice board, its members and its invite. A
/// Mac without a team shows the Team screen, which makes or joins one.
struct PeopleView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.pushRoute) private var pushRoute
    /// Opens a chat of a team. The Mac window selects it; without it the chat goes onto the stack of the page, so Back
    /// leads to People.
    var openTeamChat: ((TeamChatKey) -> Void)? = nil

    private var screen: TeamScreen {
        TeamScreen(paired: store.hasPairing || store.isDemo, connection: store.connection, rooms: store.team,
                   error: store.teamLoadError)
    }

    var body: some View {
        Group {
            switch screen {
            case .team, .teams: list(PeopleSections(rooms: store.shownTeamRooms, local: store.newestLocalMessages))
            // Without a team the Team screen says why and offers the one action: pair, make a team, or join one.
            default: TeamView(emptyTitle: "People")
            }
        }
        .task(id: store.connection) { await store.loadTeam() }
    }

    private func list(_ sections: PeopleSections) -> some View {
        BoardPage {
            BoardPageTitle(title: "People", detail: sections.detail)
            if !sections.directs.isEmpty {
                BoardSection("Direct messages", count: sections.directs.reduce(0) { $0 + $1.unreadCount }) {
                    ForEach(sections.directs) { item in chatRow(item) }
                }
            }
            ForEach(sections.teams) { team in
                BoardSection(team.room.name) {
                    if team.room.showsInvite {
                        TeamInviteButton(room: team.room) { _ in Label("Invite", systemImage: "person.badge.plus") }
                    }
                } rows: {
                    ForEach(team.chats) { item in chatRow(item) }
                    if team.hasBoard {
                        BoardRow(symbol: team.room.bannerNotice?.kind.symbol ?? "megaphone",
                                 tint: team.room.bannerNotice?.kind.tint ?? .secondary, title: "Notice board",
                                 detail: team.room.bannerNotice?.text,
                                 value: team.room.unseenNotices > 0 ? "\(team.room.unseenNotices) new" : nil)
                            .boardButton("people-notices-" + team.id) { pushRoute?(NoticeBoardRoute(roomId: team.id)) }
                    }
                    ForEach(team.members) { member in memberRow(member, in: team.room) }
                    BoardRow(symbol: "gearshape", title: "Team settings", detail: team.detail)
                        .boardButton("people-team-" + team.id) { pushRoute?(TeamScreenRoute(roomId: team.id)) }
                }
            }
        }
        .refreshable { await store.loadTeam() }
    }

    private func show(_ chat: TeamChatKey) {
        if let openTeamChat { openTeamChat(chat) } else { pushRoute?(TeamChatRoute(chat: chat)) }
    }

    private func chatRow(_ item: ChatListItem) -> some View {
        PeopleRow(item: item, avatarSize: 32)
            .padding(.horizontal, BoardStyle.Space.m).padding(.vertical, BoardStyle.Space.xs)
            .contentShape(Rectangle())
            .modifier(TeamChatMuteMenu(item: item))
            .boardButton("people-" + item.id) { if let chat = item.chat { show(chat) } }
    }

    /// A member: the face, the name, and "You" or "Admin". A member that can get a direct message opens that chat.
    @ViewBuilder private func memberRow(_ member: TeamMember, in room: TeamRoom) -> some View {
        let tags = [member.thisMac == true ? "You" : nil, member.admin == true ? "Admin" : nil].compactMap { $0 }
        let direct = room.directMember(member.member) != nil
        let row = HStack(spacing: BoardStyle.Space.m) {
            PersonFace(member: member, memberId: member.member, name: member.name, size: 28)
            Text(member.name).lineLimit(1)
            if !tags.isEmpty {
                Text(tags.joined(separator: " · ")).font(.subheadline).foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
            if direct {
                Image(systemName: "bubble.left").font(.footnote).foregroundStyle(.tertiary)
            }
        }
        .padding(.horizontal, BoardStyle.Space.m).padding(.vertical, BoardStyle.Space.s)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
        if direct {
            row.boardButton("people-member-" + member.member) {
                show(TeamChatKey(room: room.room, member: member.member))
            }
            .accessibilityHint(TeamDirectText.opens)
        } else {
            row
        }
    }
}

#if DEBUG && targetEnvironment(simulator)
extension AppStore {
    /// Gives the preview data a team, for a screenshot of People and of the notice board: Gumroad with three people,
    /// a direct chat with an unread message, and four notices.
    func applyTeamPreview(now: Date = Date()) {
        let formatter = ISO8601DateFormatter()
        func ago(_ minutes: Double) -> String { formatter.string(from: now.addingTimeInterval(-minutes * 60)) }
        func notice(_ id: String, _ type: String, _ text: String, posted: Double, ends: Double?, active: Bool,
                    author: String, name: String, seen: Bool) -> String {
            let end = ends.map { "\"" + formatter.string(from: now.addingTimeInterval($0 * 60)) + "\"" } ?? "null"
            let head = "{\"id\": \"\(id)\", \"type\": \"\(type)\", \"text\": \"\(text)\", \"posted_at\": \"\(ago(posted))\""
            let state = ", \"ends_at\": \(end), \"closed\": false, \"active\": \(active), \"author\": \"\(author)\""
            let tail = ", \"author_name\": \"\(name)\", \"own\": \(author == "m1"), \"seen\": \(seen), \"seen_by\": [\"Lin\"]}"
            return head + state + tail
        }
        let notices = [
            notice("n1", "status", "Deploys are frozen until the payout fix ships. Don't merge to main.", posted: 35,
                   ends: nil, active: true, author: "m2", name: "Grace", seen: false),
            notice("n2", "deadline", "Creator payout report is due Friday. One page per team in the Library.",
                   posted: 6 * 60, ends: 4 * 1440, active: true, author: "m1", name: "Ada", seen: true),
            notice("n3", "rule", "Every PR needs a real screenshot of the app.", posted: 4 * 1440, ends: nil,
                   active: true, author: "m1", name: "Ada", seen: true),
            notice("n4", "announcement", "Coconut 0.2 ships Thursday.", posted: 6 * 1440, ends: -3 * 1440,
                   active: false, author: "m2", name: "Grace", seen: true),
        ]
        // `mentioned` as a current Mac sends it, so the `@` list and a name without `@` name the people too.
        let json = "{\"rooms\": [{\"room\": \"gumroad\", \"name\": \"Gumroad\", \"state\": \"active\", \"unread\": 0, \"mentioned\": false, \"notices\": ["
            + notices.joined(separator: ", ") + "]}]}"
        guard var rooms = try? APIJSON.decoder().decode(TeamRooms.self, from: Data(json.utf8)) else { return }
        rooms.rooms[0].canInvite = true
        // Ada is a member and Grace is the admin, as for most people of a team. Each member makes a chat.
        rooms.rooms[0].canMakeChats = true
        rooms.rooms[0].lastMessage = TeamLastMessage(member: "m3", memberName: "Lin", text: "The payout fix is in review.",
                                                     createdAt: ago(42))
        rooms.rooms[0].members = [
            TeamMember(member: "m1", name: "Ada", thisMac: true),
            TeamMember(member: "m2", name: "Grace", admin: true, direct: true),
            TeamMember(member: "m3", name: "Lin", direct: true),
        ]
        rooms.rooms[0].directs = [
            TeamDirect(member: "m2", memberName: "Grace", unread: 1,
                       lastMessage: TeamLastMessage(text: "Can you check the refund for the Friday double payouts?",
                                                    createdAt: ago(9))),
        ]
        // The agent of the team on a Mac of its own, which each person can write to, as Gumclaw is for Gumroad.
        rooms.rooms[0].bots = [TeamRoomBot(id: "t-gumroad-gumclaw", name: "Gumclaw", member: "m4", memberName: "Gumclaw",
                                           open: true, direct: true)]
        rooms.rooms[0].botDirects = [
            TeamBotDirect(bot: "t-gumroad-gumclaw", botName: "Gumclaw", unread: 0,
                          lastMessage: TeamLastMessage(bot: "t-gumroad-gumclaw", botName: "Gumclaw",
                                                       text: "The ledger through October balances; November waits on two Stripe payouts.",
                                                       createdAt: ago(75))),
        ]
        team = rooms
        applyInboxPreview(room: "gumroad", now: now)
    }
}
#endif
