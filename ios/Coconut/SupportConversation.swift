import SwiftUI

/// A conversation of the customer queue of Support, read from Gmail through the Mac (backend/coconut/support_threads.py).
/// The support report gives each conversation the ID of its Gmail thread as its slug, so a row of the queue opens the
/// thread here instead of in Gmail, and the person answers and closes it here too.
struct SupportConversation: Decodable, Sendable {
    struct Message: Decodable, Sendable, Identifiable {
        struct Attachment: Decodable, Sendable, Hashable {
            let name: String
            let size: Int
        }
        let id: String
        let fromName: String
        let fromEmail: String
        /// The address that an answer to this message goes to. A Mac before answers had none.
        var replyTo: String? = nil
        let to: [String]
        let date: String?
        var text: String
        /// The text stops at the length that the Mac sends. "Show the whole message" reads the rest.
        var cut: Bool
        let attachments: [Attachment]
        /// The support mailbox sent it.
        let outgoing: Bool
    }
    let id: String
    let subject: String
    let account: String
    var messages: [Message]
    /// Earlier messages that the Mac left out of a long thread.
    var more: Int
    /// open or closed by the labels of the support pipeline, or nil for a thread that is no ticket.
    var state: String? = nil
    /// Whether this Mac answers and closes conversations: the support mailbox is connected. Nil from a Mac that cannot.
    var canReply: Bool? = nil

    /// The message that an answer answers by default: the newest one from a customer.
    var defaultTarget: Message? { messages.last { !$0.outgoing } ?? messages.last }

    /// The thread in Gmail on the web, in the account that holds it.
    var gmailURL: URL? {
        let user = account.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed.subtracting(CharacterSet(charactersIn: "+&=#"))) ?? ""
        return URL(string: "https://mail.google.com/mail/" + (user.isEmpty ? "u/0/" : "?authuser=" + user) + "#all/" + id)
    }
}

extension AppStore {
    /// `mailbox` "personal" reads the person's own Gmail, for a mail that notified.
    func supportConversation(_ id: String, subject: String, everything: Bool = false, mailbox: String = "") async throws -> SupportConversation {
        if isDemo { return mailbox.isEmpty ? SupportDemo.conversation(id: id, subject: subject) : SupportDemo.mail(id: id) }
        let query = [everything ? "all=1" : "", mailbox.isEmpty ? "" : "mailbox=" + mailbox].filter { !$0.isEmpty }
        return try await businessRequest("support/conversations/" + Self.supportPath(id)
                                         + (query.isEmpty ? "" : "?" + query.joined(separator: "&")))
    }

    /// One message with its whole text.
    func supportMessage(_ thread: String, message: String, mailbox: String = "") async throws -> SupportConversation.Message {
        if isDemo, let found = SupportDemo.conversations[thread]?.messages.first(where: { $0.id == message }) { return found }
        return try await businessRequest("support/conversations/\(Self.supportPath(thread))/messages/\(Self.supportPath(message))"
                                         + (mailbox.isEmpty ? "" : "?mailbox=" + mailbox))
    }

    /// Sends the person's answer from the support mailbox. `message` is the answered message, or nil for the newest one
    /// from a customer.
    func answerSupport(_ thread: String, text: String, message: String?, requestId: String) async throws -> SupportConversation.Message {
        if isDemo { return SupportDemo.answer(thread, text: text) }
        let answer: SupportConversation.Message = try await businessRequest("support/conversations/\(Self.supportPath(thread))/reply",
            method: "POST", body: APIJSON.encode(SupportAnswerRequest(text: text, message: message)), requestId: requestId)
        businessChanges += 1
        return answer
    }

    /// Closes or reopens a conversation, as the support pipeline does with its labels.
    func setSupportConversation(_ thread: String, state: String) async throws -> SupportConversation {
        if isDemo {
            let conversation = SupportDemo.setState(thread, state: state)
            supportClosedAt[thread] = state == "closed" ? .now : nil
            return conversation
        }
        let conversation: SupportConversation = try await businessRequest("support/conversations/" + Self.supportPath(thread),
            method: "PATCH", body: APIJSON.encode(SupportStateRequest(state: state)), requestId: UUID().uuidString)
        // The rows of Support leave the conversation out until a report published after the close (supportTickets).
        supportClosedAt[thread] = state == "closed" ? .now : nil
        businessChanges += 1
        return conversation
    }

    private static func supportPath(_ id: String) -> String { id.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? id }
}

private struct SupportAnswerRequest: Encodable { let text: String; let message: String? }
private struct SupportStateRequest: Encodable { let state: String }

struct SupportConversationView: View {
    @Environment(AppStore.self) private var store
    let id: String
    let subject: String
    /// "personal" for a mail of the person's own Gmail (SupportThreadRoute), which this page only shows.
    var mailbox = ""
    @State private var conversation: SupportConversation?
    @State private var failure: (status: Int, message: String)?
    @State private var loads = 0
    /// The person's answers that Gmail has not confirmed yet. They show at the end at once, and a read of the page in the
    /// meantime keeps them.
    @State private var pendingAnswers: [SupportConversation.Message] = []
    /// A close or a reopen that the Mac has not confirmed yet. The page shows the new state at once.
    @State private var pendingState: String?
    /// Counts the starts and ends of the person's writes. A read that ran across one can be older than the write, so it
    /// is dropped.
    @State private var writes = 0
    @State private var everything = false
    /// The message that the answer answers, when the person chose one. Nil is the newest one from a customer.
    @State private var target: String?
    @State private var busy = false
    @State private var actionError: String?

    private var title: String { conversation.map(\.subject).flatMap { $0.isEmpty ? nil : $0 } ?? subject }
    private var personal: Bool { mailbox == "personal" }
    private var answered: SupportConversation.Message? {
        target.flatMap { id in conversation?.messages.first { $0.id == id } } ?? conversation?.defaultTarget
    }

    var body: some View {
        BoardPage(spacing: BoardStyle.Space.m) {
            VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
                Text(title.isEmpty ? "No subject" : title).font(.title3.weight(.semibold)).textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true).accessibilityAddTraits(.isHeader)
                // A mail of the person's own Gmail is not a conversation of the queue, so it has no state.
                if !personal, let state = shown?.state { SupportStateChip(state: state) }
            }
            .padding(.bottom, BoardStyle.Space.s)
            if let conversation = shown {
                if conversation.more > 0 {
                    Button(conversation.more == 1 ? "Show 1 earlier message" : "Show \(conversation.more) earlier messages") {
                        everything = true; loads += 1
                    }
                    .font(.subheadline).accessibilityIdentifier("support-earlier")
                }
                ForEach(conversation.messages) { message in
                    SupportMessageTile(message: message, answering: conversation.canReply == true && target == message.id,
                                       canAnswer: conversation.canReply == true && !message.outgoing && conversation.messages.count > 1,
                                       expand: { await expand(message) }, answer: { target = message.id })
                        // An answer on its way shows lighter until Gmail confirms it.
                        .opacity(message.id.hasPrefix(Self.pendingPrefix) ? 0.55 : 1)
                }
                if conversation.messages.isEmpty { Text("No messages.").font(.subheadline).foregroundStyle(.secondary) }
                // How to respond, as the options of a poll, while the newest message is the customer's; Gumclaw does the
                // work of most (SupportHandoffs.swift). The box at the bottom stays for the person's own words: an
                // answer, or a request to an agent.
                if !personal {
                    SupportChoices(thread: id, subject: title, waiting: conversation.state == "open" && conversation.messages.last?.outgoing == false,
                                   canClose: conversation.canReply == true, close: { await setState("closed") }, reload: { loads += 1 })
                }
                if conversation.canReply == true, conversation.state == "open", conversation.messages.last?.outgoing == true {
                    Button("Close conversation", systemImage: "checkmark.circle") { Task { await setState("closed") } }
                        .buttonStyle(CoconutButtonStyle(kind: .quiet)).disabled(busy)
                        .accessibilityIdentifier("support-close-after-answer")
                }
                if conversation.canReply == false, !personal { SupportMailboxConnect { loads += 1 } }
                // Coconut only shows a mail of the person's own Gmail, so Gmail answers it. The system browser holds the
                // person's Google session, and `openURL` would open the thread here again (InAppLink).
                if personal, let url = conversation.gmailURL {
                    Button("Open in Gmail", systemImage: "arrow.up.right.square") { SystemBrowser.open(url) }
                        .buttonStyle(CoconutButtonStyle(kind: .quiet))
                        .accessibilityIdentifier("mail-open-gmail")
                }
                if let actionError {
                    Text(actionError).font(.subheadline).foregroundStyle(BoardStyle.destructive).fixedSize(horizontal: false, vertical: true)
                }
            } else if let failure {
                BoardLoadNotice(title: "Gmail unavailable", message: failure.message) { loads += 1 }
                if failure.status == 409 {
                    NavigationLink("Connect Google") { MCPSettingsView() }.accessibilityIdentifier("support-connect-google")
                }
            } else {
                BoardLoading(title: "Loading conversation")
            }
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            // The box answers the customer, or hands the conversation to an agent with the person's words (ReplyBox).
            // Without the support mailbox it only hands over. A mail of the person's own Gmail has no box.
            if let conversation, !personal {
                ReplyBox(draftKey: "support:" + id, placeholder: "Answer", note: answered?.replyTo.map { "To " + $0 },
                         identifier: "support-answer", handoff: AgentHandoff(supportThread: id, subject: title),
                         canReply: conversation.canReply == true) { text, key in await answer(text, key: key) }
            }
        }
        .navigationTitle(personal ? "Mail" : "Support")
        .toolbar {
            if conversation?.canReply == true, let state = conversation?.state {
                ToolbarItem(placement: .primaryAction) {
                    Menu {
                        // Short labels: the narrow menu of an iPhone broke "Reopen conversation" with a hyphen.
                        if state == "open" {
                            Button("Close", systemImage: "checkmark.circle") { Task { await setState("closed") } }
                                .accessibilityIdentifier("support-close")
                        } else {
                            Button("Reopen", systemImage: "arrow.uturn.backward.circle") { Task { await setState("open") } }
                                .accessibilityIdentifier("support-reopen")
                        }
                    } label: { Label("Actions", systemImage: "ellipsis.circle") }
                        .disabled(busy)
                        .accessibilityIdentifier("support-menu")
                }
            }
        }
        .task(id: loads) { await load() }
        .refreshable {
            await load()
            await store.loadSupportHandoffs(fresh: true)
        }
        // A message of the customer, an answer of Gumclaw or a close from elsewhere shows without a pull (LiveReload).
        .liveReload { await load(quiet: true) }
        // Back from Connections, a page that could not read Gmail tries again.
        .onAppear { if failure != nil { loads += 1 } }
    }

    private static let pendingPrefix = "pending-"

    /// The conversation as the person sees it: a close or a reopen on its way in its new state, and the answers on their
    /// way at the end.
    private var shown: SupportConversation? {
        guard var shown = conversation else { return nil }
        if let pendingState { shown.state = pendingState }
        shown.messages += pendingAnswers
        return shown
    }

    /// Reads the conversation. A quiet read, while the page shows (LiveReload), keeps the page as it is when it fails.
    private func load(quiet: Bool = false) async {
        let before = writes
        do {
            let loaded = try await store.supportConversation(id, subject: subject, everything: everything, mailbox: mailbox)
            guard !Task.isCancelled, before == writes else { return }
            conversation = loaded; failure = nil
        } catch is CancellationError {
        } catch ServiceError.response(let status, let message) {
            if conversation == nil { failure = (status, message) } else if !quiet { actionError = message }
        } catch {
            if conversation == nil { failure = (0, error.localizedDescription) } else if !quiet { actionError = error.localizedDescription }
        }
    }

    private func expand(_ message: SupportConversation.Message) async {
        do {
            let whole = try await store.supportMessage(id, message: message.id, mailbox: mailbox)
            if let index = conversation?.messages.firstIndex(where: { $0.id == message.id }) {
                conversation?.messages[index].text = whole.text
                conversation?.messages[index].cut = whole.cut
            }
        } catch is CancellationError {
        } catch { actionError = error.localizedDescription }
    }

    /// Sends the answer. It shows at the end at once, lighter until Gmail confirms it, and a failure takes it away and
    /// puts the text back in the box. Nil is success; else the words of the failure.
    private func answer(_ text: String, key: String) async -> String? {
        let mailbox = conversation?.messages.last { $0.outgoing }
        let pending = SupportConversation.Message(id: Self.pendingPrefix + key, fromName: mailbox?.fromName ?? "",
            fromEmail: mailbox?.fromEmail ?? conversation?.account ?? "", to: [answered?.replyTo ?? answered?.fromEmail].compactMap { $0 },
            date: Date.now.formatted(.iso8601), text: text, cut: false, attachments: [], outgoing: true)
        pendingAnswers.append(pending)
        writes += 1
        defer {
            pendingAnswers.removeAll { $0.id == pending.id }
            writes += 1
        }
        do {
            let sent = try await store.answerSupport(id, text: text, message: target, requestId: key)
            if conversation?.messages.contains(where: { $0.id == sent.id }) == false { conversation?.messages.append(sent) }
            target = nil
            actionError = nil
            return nil
        } catch is CancellationError {
            return "The answer was not confirmed. Send it again: it goes out once."
        } catch {
            return error.localizedDescription
        }
    }

    /// Closes or reopens. The page shows the new state at once and the Mac's state when it answers; a refusal shows the
    /// state before with the reason.
    private func setState(_ state: String) async {
        busy = true
        pendingState = state
        writes += 1
        defer {
            busy = false
            pendingState = nil
            writes += 1
        }
        do {
            let changed = try await store.setSupportConversation(id, state: state)
            conversation = changed
            actionError = nil
        } catch is CancellationError {
        } catch { actionError = error.localizedDescription }
    }
}

/// Open or Closed, as the support queue counts the conversation.
struct SupportStateChip: View {
    let state: String

    var body: some View {
        let open = state == "open"
        HStack(spacing: BoardStyle.Space.xs) {
            Image(systemName: open ? "tray.full" : "checkmark.circle").imageScale(.small)
            Text(open ? "Open" : "Closed")
        }
        .font(.caption.weight(.semibold)).foregroundStyle(open ? BoardStyle.attention : BoardStyle.positive)
        .padding(.horizontal, 7).padding(.vertical, 2)
        .background((open ? BoardStyle.attention : BoardStyle.positive).opacity(0.14), in: Capsule())
        .accessibilityIdentifier("support-state")
    }
}

/// One message of a conversation, on its page and on a card of the deck (SupportDeckView).
struct SupportMessageTile: View {
    let message: SupportConversation.Message
    /// The person chose this message to answer.
    let answering: Bool
    /// The menu offers to answer this message instead of the newest one.
    let canAnswer: Bool
    let expand: () async -> Void
    let answer: () -> Void
    /// The color of a customer's message. A card of the deck is a surface, so its messages take the color of the page.
    var fill: Color = BoardStyle.surface
    @State private var expanding = false

    var body: some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
            HStack(alignment: .firstTextBaseline, spacing: BoardStyle.Space.s) {
                Text(message.fromName.isEmpty ? message.fromEmail : message.fromName)
                    .font(.subheadline.weight(.semibold)).lineLimit(1)
                if answering {
                    Text("Answering").font(.caption.weight(.semibold)).foregroundStyle(BoardStyle.control)
                }
                Spacer(minLength: BoardStyle.Space.s)
                if let date = message.date?.apiDate {
                    Text(date.formatted(.dateTime.month(.abbreviated).day().hour().minute()))
                        .font(.caption).foregroundStyle(.secondary).monospacedDigit().lineLimit(1)
                }
            }
            if !message.text.isEmpty {
                Text(message.text).font(.callout).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            }
            if message.cut {
                Button(expanding ? "Loading…" : "Show the whole message") {
                    expanding = true
                    Task { await expand(); expanding = false }
                }
                .font(.caption).disabled(expanding).accessibilityIdentifier("support-whole-message")
            }
            ForEach(message.attachments, id: \.self) { file in
                HStack(spacing: BoardStyle.Space.s) {
                    Image(systemName: "paperclip").accessibilityHidden(true)
                    Text(file.name).lineLimit(1).truncationMode(.middle)
                    if file.size > 0 {
                        Text(ByteCountFormatter.string(fromByteCount: Int64(file.size), countStyle: .file)).foregroundStyle(.secondary)
                    }
                }
                .font(.caption).accessibilityElement(children: .combine).accessibilityLabel("Attachment \(file.name)")
            }
        }
        .padding(BoardStyle.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(message.outgoing ? BoardStyle.accent.opacity(0.1) : fill,
                    in: RoundedRectangle(cornerRadius: BoardStyle.Radius.large, style: .continuous))
        .overlay {
            if answering {
                RoundedRectangle(cornerRadius: BoardStyle.Radius.large, style: .continuous).stroke(BoardStyle.control.opacity(0.5))
            }
        }
        .contextMenu {
            if canAnswer { Button("Answer this message", systemImage: "arrowshape.turn.up.left") { answer() } }
            Button("Copy text", systemImage: "doc.on.doc") { Pasteboard.copy(message.text) }
        }
        .accessibilityElement(children: .combine)
        .accessibilityValue(message.outgoing ? "Sent by support" : "From \(message.fromEmail)")
        .accessibilityAction(named: "Answer this message") { if canAnswer { answer() } }
    }
}

/// Answers need the support mailbox: the Google sign-in of the account that holds support mail, with the right to send
/// and label it. Only Coconut uses it, so the agents keep their read-only Gmail.
struct SupportMailboxConnect: View {
    @Environment(AppStore.self) private var store
    let connected: () -> Void
    @State private var signIn = GoogleSignIn()
    @State private var busy = false
    @State private var error: String?

    var body: some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
            Text("Connect the support mailbox to answer and close conversations here. Agents keep read-only Gmail.")
                .font(.subheadline).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            Button(busy ? "Connecting…" : "Connect support mailbox") { connect() }
                .buttonStyle(CoconutButtonStyle()).disabled(busy)
                .accessibilityIdentifier("support-connect-mailbox")
            if let error { InlineError(error) }
        }
        .padding(BoardStyle.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.small, style: .continuous))
        .onOpenURL { signIn.receive($0) }
        .onDisappear { signIn.cancel() }
    }

    private func connect() {
        busy = true; error = nil
        Task {
            defer { busy = false }
            do {
                _ = try await store.connectGoogle(signIn, mailbox: GoogleMailbox.support)
                connected()
            } catch is CancellationError {
            } catch { self.error = error.localizedDescription }
        }
    }
}

/// The conversations of the demo queue (BusinessReport.previewSupport), which change in memory when the person answers
/// or closes one.
@MainActor
enum SupportDemo {
    static var conversations: [String: SupportConversation] = [:]

    static func conversation(id: String, subject: String) -> SupportConversation {
        if let shown = conversations[id] { return shown }
        let shown = SupportConversation.preview(id: id, subject: subject)
        conversations[id] = shown
        return shown
    }

    /// A mail of the person's own Gmail, which a notification opens (`--open mail-tap`). The page only shows it.
    static func mail(id: String) -> SupportConversation {
        let text = "Hi,\n\nAre you still up for recording on Tuesday? Thirty minutes on running a company with agents. I can send the questions ahead.\n\nJordan"
        let message = SupportConversation.Message(id: id + "-0", fromName: "Jordan Lee", fromEmail: "jordan@example.com",
                                                  replyTo: "jordan@example.com", to: ["you@example.com"],
                                                  date: Date.now.addingTimeInterval(-300).formatted(.iso8601), text: text, cut: false,
                                                  attachments: [], outgoing: false)
        return SupportConversation(id: id, subject: "Podcast on Tuesday", account: "you@example.com", messages: [message],
                                   more: 0, state: "open", canReply: false)
    }

    static func answer(_ thread: String, text: String) -> SupportConversation.Message {
        let shown = conversations[thread]
        let customer = shown?.defaultTarget
        let message = SupportConversation.Message(id: UUID().uuidString, fromName: "Gumroad Support", fromEmail: "support@gumroad.com",
                                                  replyTo: customer?.fromEmail, to: [customer?.fromEmail ?? ""],
                                                  date: Date.now.formatted(.iso8601), text: text, cut: false, attachments: [], outgoing: true)
        conversations[thread]?.messages.append(message)
        return message
    }

    static func setState(_ thread: String, state: String) -> SupportConversation {
        conversations[thread]?.state = state
        return conversations[thread] ?? SupportConversation.preview(id: thread, subject: "")
    }
}

extension SupportConversation {
    /// How long the customer of each open conversation of the demo queue has waited, in hours since their newest
    /// message, and their address. The report of the demo (BusinessReport.previewSupport) and the conversations share
    /// them, so a card says the wait that its row says. Three are past the service level, as the demo overview counts.
    static let demoWaits: [String: (hours: Double, email: String)] = [
        "Payout timing question": (29, "mira@example.com"),
        "Help with a product download": (14, "daniel@example.com"),
        "Update a purchase email": (9, "priya@example.com"),
        "Refund for a double charge": (2, "tom@example.com"),
    ]

    private typealias DemoThread = (customer: (String, String), messages: [(fromSupport: Bool, text: String, files: [Message.Attachment])])

    /// The conversation of a row of the demo queue (BusinessReport.previewSupport). The support mailbox is connected in
    /// the demo, so the page answers and closes.
    static func preview(id: String, subject: String) -> SupportConversation {
        let support = ("Gumroad Support", "support@gumroad.com")
        let threads: [String: DemoThread] = [
            "Payout timing question": (("Mira Chen", "mira@example.com"), [
                (false, "Hi,\n\nMy payout for last week still shows as pending. It usually arrives on Friday. Is something holding it?\n\nThanks,\nMira", []),
                (true, "Hi Mira,\n\nA large sale put the payout through a routine review. The review is done, so it goes out this Friday with the next one.\n\nBest,\nGumroad Support", []),
                (false, "Great, thank you for checking. Will both payouts arrive together, or as two transfers?", [])]),
            "Help with a product download": (("Daniel Ortiz", "daniel@example.com"), [
                (false, "I bought the Lightroom presets yesterday, but the download link says the file is missing. Receipt attached.", [.init(name: "receipt.pdf", size: 48_211)]),
                (true, "Hi Daniel,\n\nThe creator replaced the file last night, which broke the old link. I sent a new link for the updated presets to this address.\n\nBest,\nGumroad Support", []),
                (false, "Thanks, the new link works, but the bonus PDF from the product page isn't in the download.", [])]),
            "Update a purchase email": (("Priya Nair", "priya@example.com"), [
                (false, "I bought a course with my old work email. Can you move it to priya@example.com?", [])]),
            "Refund for a double charge": (("Tom Becker", "tom@example.com"), [
                (false, "I was charged twice for the same ebook this morning. A screenshot of my statement is attached.", [.init(name: "statement.png", size: 312_940)])]),
        ]
        let fallback: DemoThread = (("Alex Kim", "alex@example.com"), [
            (false, "Hi, I need help with my order.", []),
            (true, "Hi Alex,\n\nHappy to help. Which purchase is it about?\n\nBest,\nGumroad Support", [])])
        let thread = threads[subject] ?? fallback
        let count = thread.messages.count
        // The newest message of a conversation of the queue is the customer's, as old as the wait of its row.
        let newest = (demoWaits[subject]?.hours ?? 0) * 3600
        let messages = thread.messages.enumerated().map { index, item -> Message in
            let from = item.fromSupport ? support : thread.customer
            let age = demoWaits[subject] == nil ? Double(count - index) * 2700 : newest + Double(count - 1 - index) * 7200
            return Message(id: "\(id)-\(index)", fromName: from.0, fromEmail: from.1,
                           replyTo: item.fromSupport ? thread.customer.1 : from.1,
                           to: [item.fromSupport ? thread.customer.1 : support.1],
                           date: Date().addingTimeInterval(-age).formatted(.iso8601),
                           text: item.text, cut: false, attachments: item.files, outgoing: item.fromSupport)
        }
        return SupportConversation(id: id, subject: subject, account: support.1, messages: messages, more: 0,
                                   state: "open", canReply: true)
    }
}
