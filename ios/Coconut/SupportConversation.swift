import SwiftUI

/// A conversation of the customer queue of Support, read from Gmail through the Google connection of the Mac
/// (backend/coconut/support_threads.py). The support report gives each conversation the ID of its Gmail thread as
/// its slug, so a row of the queue opens the thread here instead of in Gmail.
struct SupportConversation: Decodable, Sendable {
    struct Message: Decodable, Sendable, Identifiable {
        struct Attachment: Decodable, Sendable, Hashable {
            let name: String
            let size: Int
        }
        let id: String
        let fromName: String
        let fromEmail: String
        let to: [String]
        let date: String?
        let text: String
        /// The text stops at the length that the Mac sends. Gmail has the rest.
        let cut: Bool
        let attachments: [Attachment]
        /// The support mailbox sent it.
        let outgoing: Bool
    }
    let id: String
    let subject: String
    let account: String
    let url: String
    let messages: [Message]
    /// Earlier messages that the Mac left out of a long thread.
    let more: Int

    static func gmailURL(_ id: String) -> URL? { URL(string: "https://mail.google.com/mail/u/0/#all/" + id) }
}

extension AppStore {
    func supportConversation(_ id: String, subject: String) async throws -> SupportConversation {
        if isDemo { return .preview(id: id, subject: subject) }
        let path = id.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? id
        return try await businessRequest("support/conversations/" + path)
    }
}

struct SupportConversationView: View {
    @Environment(AppStore.self) private var store
    let id: String
    let subject: String
    @State private var conversation: SupportConversation?
    @State private var failure: (status: Int, message: String)?
    @State private var loads = 0

    private var gmail: URL? { conversation.flatMap { URL(string: $0.url) } ?? SupportConversation.gmailURL(id) }
    private var title: String { conversation.map(\.subject).flatMap { $0.isEmpty ? nil : $0 } ?? subject }

    var body: some View {
        BoardPage(spacing: BoardStyle.Space.m) {
            Text(title.isEmpty ? "No subject" : title).font(.title3.weight(.semibold)).textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true).accessibilityAddTraits(.isHeader)
                .padding(.bottom, BoardStyle.Space.s)
            if let conversation {
                if conversation.more > 0, let gmail {
                    Link("\(conversation.more) earlier messages in Gmail", destination: gmail).font(.subheadline)
                }
                ForEach(conversation.messages) { SupportMessageTile(message: $0, gmail: gmail) }
                if conversation.messages.isEmpty { Text("No messages.").font(.subheadline).foregroundStyle(.secondary) }
            } else if let failure {
                BoardLoadNotice(title: "Gmail unavailable", message: failure.message) { loads += 1 }
                if failure.status == 409 {
                    NavigationLink("Connect Google") { MCPSettingsView() }.accessibilityIdentifier("support-connect-google")
                }
            } else {
                BoardLoading(title: "Loading conversation")
            }
        }
        .navigationTitle("Support")
        .toolbar {
            if let gmail {
                ToolbarItem(placement: .primaryAction) {
                    Link(destination: gmail) { Label("Open in Gmail", systemImage: "arrow.up.right.square") }
                        .accessibilityIdentifier("support-open-gmail")
                }
            }
        }
        .task(id: loads) { await load() }
        .refreshable { await load() }
        // Back from Connections, a page that could not read Gmail tries again.
        .onAppear { if failure != nil { loads += 1 } }
    }

    private func load() async {
        do {
            let loaded = try await store.supportConversation(id, subject: subject)
            conversation = loaded; failure = nil
        } catch is CancellationError {
        } catch ServiceError.response(let status, let message) {
            if conversation == nil { failure = (status, message) }
        } catch {
            if conversation == nil { failure = (0, error.localizedDescription) }
        }
    }
}

private struct SupportMessageTile: View {
    let message: SupportConversation.Message
    let gmail: URL?

    var body: some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
            HStack(alignment: .firstTextBaseline, spacing: BoardStyle.Space.s) {
                Text(message.fromName.isEmpty ? message.fromEmail : message.fromName)
                    .font(.subheadline.weight(.semibold)).lineLimit(1)
                Spacer(minLength: BoardStyle.Space.s)
                if let date = message.date?.apiDate {
                    Text(date.formatted(.dateTime.month(.abbreviated).day().hour().minute()))
                        .font(.caption).foregroundStyle(.secondary).monospacedDigit().lineLimit(1)
                }
            }
            if !message.text.isEmpty {
                Text(message.text).font(.callout).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            }
            if message.cut, let gmail { Link("Rest of this message in Gmail", destination: gmail).font(.caption) }
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
        .accessibilityElement(children: .combine)
        .accessibilityValue(message.outgoing ? "Sent by support" : "From \(message.fromEmail)")
    }
}

extension SupportConversation {
    private typealias DemoThread = (customer: (String, String), messages: [(fromSupport: Bool, text: String, files: [Message.Attachment])])

    /// The conversation of a row of the demo queue (BusinessReport.previewSupport).
    static func preview(id: String, subject: String) -> SupportConversation {
        let support = ("Gumroad Support", "support@gumroad.com")
        let threads: [String: DemoThread] = [
            "Payout timing question": (("Mira Chen", "mira@example.com"), [
                (false, "Hi,\n\nMy payout for last week still shows as pending. It usually arrives on Friday. Is something holding it?\n\nThanks,\nMira", []),
                (true, "Hi Mira,\n\nA large sale put the payout through a routine review. The review is done, so it goes out this Friday with the next one.\n\nBest,\nGumroad Support", []),
                (false, "Great, thank you for checking.", [])]),
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
                           to: [item.fromSupport ? thread.customer.1 : support.1],
                           date: Date().addingTimeInterval(-Double(count - index) * 2700).formatted(.iso8601),
                           text: item.text, cut: false, attachments: item.files, outgoing: item.fromSupport)
        }
        return SupportConversation(id: id, subject: subject, account: support.1,
                                   url: gmailURL(id)?.absoluteString ?? "", messages: messages, more: 0)
    }
}
