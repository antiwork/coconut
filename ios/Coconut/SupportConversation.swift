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
}

extension AppStore {
    func supportConversation(_ id: String, subject: String, everything: Bool = false) async throws -> SupportConversation {
        if isDemo { return SupportDemo.conversation(id: id, subject: subject) }
        return try await businessRequest("support/conversations/" + Self.supportPath(id) + (everything ? "?all=1" : ""))
    }

    /// One message with its whole text.
    func supportMessage(_ thread: String, message: String) async throws -> SupportConversation.Message {
        if isDemo, let found = SupportDemo.conversations[thread]?.messages.first(where: { $0.id == message }) { return found }
        return try await businessRequest("support/conversations/\(Self.supportPath(thread))/messages/\(Self.supportPath(message))")
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
        if isDemo { return SupportDemo.setState(thread, state: state) }
        let conversation: SupportConversation = try await businessRequest("support/conversations/" + Self.supportPath(thread),
            method: "PATCH", body: APIJSON.encode(SupportStateRequest(state: state)), requestId: UUID().uuidString)
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
    @State private var conversation: SupportConversation?
    @State private var failure: (status: Int, message: String)?
    @State private var loads = 0
    @State private var everything = false
    /// The message that the answer answers, when the person chose one. Nil is the newest one from a customer.
    @State private var target: String?
    @State private var busy = false
    @State private var actionError: String?
    @State private var asking = false

    private var title: String { conversation.map(\.subject).flatMap { $0.isEmpty ? nil : $0 } ?? subject }
    private var answered: SupportConversation.Message? {
        target.flatMap { id in conversation?.messages.first { $0.id == id } } ?? conversation?.defaultTarget
    }

    var body: some View {
        BoardPage(spacing: BoardStyle.Space.m) {
            VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
                Text(title.isEmpty ? "No subject" : title).font(.title3.weight(.semibold)).textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true).accessibilityAddTraits(.isHeader)
                if let state = conversation?.state { SupportStateChip(state: state) }
            }
            .padding(.bottom, BoardStyle.Space.s)
            if let conversation {
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
                }
                if conversation.messages.isEmpty { Text("No messages.").font(.subheadline).foregroundStyle(.secondary) }
                if conversation.canReply == true, conversation.state == "open", conversation.messages.last?.outgoing == true {
                    Button("Close conversation", systemImage: "checkmark.circle") { Task { await setState("closed") } }
                        .buttonStyle(CoconutButtonStyle(kind: .quiet)).disabled(busy)
                        .accessibilityIdentifier("support-close-after-answer")
                }
                if conversation.canReply == false { SupportMailboxConnect { loads += 1 } }
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
            if let conversation, conversation.canReply == true {
                ReplyBox(draftKey: "support:" + id, placeholder: "Answer", note: answered?.replyTo.map { "To " + $0 },
                         identifier: "support-answer") { text, key in await answer(text, key: key) }
            }
        }
        .navigationTitle("Support")
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Menu {
                    if conversation?.canReply == true, let state = conversation?.state {
                        // Short labels: the narrow menu of an iPhone broke "Reopen conversation" with a hyphen.
                        if state == "open" {
                            Button("Close", systemImage: "checkmark.circle") { Task { await setState("closed") } }
                                .accessibilityIdentifier("support-close")
                        } else {
                            Button("Reopen", systemImage: "arrow.uturn.backward.circle") { Task { await setState("open") } }
                                .accessibilityIdentifier("support-reopen")
                        }
                    }
                    Button("Ask an agent", systemImage: "bubble.left.and.bubble.right") { asking = true }
                        .disabled(store.bots.allSatisfy(\.archived))
                } label: { Label("Actions", systemImage: "ellipsis.circle") }
                    .disabled(conversation == nil || busy)
                    .accessibilityIdentifier("support-menu")
            }
        }
        .task(id: loads) { await load() }
        .refreshable { await load() }
        // Back from Connections, a page that could not read Gmail tries again.
        .onAppear { if failure != nil { loads += 1 } }
        .sheet(isPresented: $asking) {
            AskAgentSheet(summary: "Work on the support conversation \"\(title)\".", link: "Gmail thread " + id,
                          keep: "Keep the answers in this conversation of the support mailbox.")
        }
    }

    private func load() async {
        do {
            let loaded = try await store.supportConversation(id, subject: subject, everything: everything)
            conversation = loaded; failure = nil
        } catch is CancellationError {
        } catch ServiceError.response(let status, let message) {
            if conversation == nil { failure = (status, message) } else { actionError = message }
        } catch {
            if conversation == nil { failure = (0, error.localizedDescription) } else { actionError = error.localizedDescription }
        }
    }

    private func expand(_ message: SupportConversation.Message) async {
        do {
            let whole = try await store.supportMessage(id, message: message.id)
            if let index = conversation?.messages.firstIndex(where: { $0.id == message.id }) {
                conversation?.messages[index].text = whole.text
                conversation?.messages[index].cut = whole.cut
            }
        } catch is CancellationError {
        } catch { actionError = error.localizedDescription }
    }

    /// Sends the answer and shows it at the end. Nil is success; else the words of the failure.
    private func answer(_ text: String, key: String) async -> String? {
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

    private func setState(_ state: String) async {
        busy = true
        defer { busy = false }
        do {
            let changed = try await store.setSupportConversation(id, state: state)
            conversation = changed
            actionError = nil
        } catch is CancellationError {
        } catch { actionError = error.localizedDescription }
    }
}

/// Open or Closed, as the support queue counts the conversation.
private struct SupportStateChip: View {
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

private struct SupportMessageTile: View {
    let message: SupportConversation.Message
    /// The person chose this message to answer.
    let answering: Bool
    /// The menu offers to answer this message instead of the newest one.
    let canAnswer: Bool
    let expand: () async -> Void
    let answer: () -> Void
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
        .background(message.outgoing ? BoardStyle.accent.opacity(0.1) : BoardStyle.surface,
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
private struct SupportMailboxConnect: View {
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
                (true, "Hi Daniel,\n\nThe creator replaced the file last night, which broke the old link. I sent a new link for the updated presets to this address.\n\nBest,\nGumroad Support", [])]),
            "Update a purchase email": (("Priya Nair", "priya@example.com"), [
                (false, "I bought a course with my old work email. Can you move it to priya@example.com?", []),
                (true, "Hi Priya,\n\nDone. The course and its receipts are now under priya@example.com.\n\nBest,\nGumroad Support", [])]),
            "Refund for a double charge": (("Tom Becker", "tom@example.com"), [
                (false, "I was charged twice for the same ebook this morning. A screenshot of my statement is attached.", [.init(name: "statement.png", size: 312_940)]),
                (true, "Hi Tom,\n\nThe second charge was a duplicate. I refunded it, and it shows on your card in 5 to 10 days.\n\nBest,\nGumroad Support", [])]),
        ]
        let fallback: DemoThread = (("Alex Kim", "alex@example.com"), [
            (false, "Hi, I need help with my order.", []),
            (true, "Hi Alex,\n\nHappy to help. Which purchase is it about?\n\nBest,\nGumroad Support", [])])
        let thread = threads[subject] ?? fallback
        let count = thread.messages.count
        let messages = thread.messages.enumerated().map { index, item -> Message in
            let from = item.fromSupport ? support : thread.customer
            return Message(id: "\(id)-\(index)", fromName: from.0, fromEmail: from.1,
                           replyTo: item.fromSupport ? thread.customer.1 : from.1,
                           to: [item.fromSupport ? thread.customer.1 : support.1],
                           date: Date().addingTimeInterval(-Double(count - index) * 2700).formatted(.iso8601),
                           text: item.text, cut: false, attachments: item.files, outgoing: item.fromSupport)
        }
        return SupportConversation(id: id, subject: subject, account: support.1, messages: messages, more: 0,
                                   state: "open", canReply: true)
    }
}
