import Foundation

/// What a row of the Chats list and of the Mac sidebar shows of the messages of one bot.
struct BotChatSummary: Equatable, Sendable {
    /// The message of the line under the name, as `latestConversationMessage` finds it.
    var latestMessage: Message?
    /// The date of the newest message: the age of the row and the mood of the face.
    var newestDate: Date?
    /// The date of the newest message that is no reaction: the place of the row in the list.
    var newestActivity: Date?
    /// A reply streams in.
    var streaming = false
}

/// The summary of each bot that has a message. Each row filtered and parsed all messages three times for each
/// redraw, and a reply that streams in redrew every row for each update. The store keeps the summaries next to the
/// messages. A row reads only its summary, so it redraws only when a summary changes.
struct ChatSummaries: Equatable, Sendable {
    private var bots: [String: BotChatSummary] = [:]
    /// The date of the newest message of each bot, as `ChatList.lastMessageDates` gives it.
    private(set) var newestActivity: [String: Date] = [:]

    init() {}

    init(_ messages: [Message]) {
        self = ChatSummaries().updated(from: [], to: messages)
    }

    subscript(botId: String) -> BotChatSummary { bots[botId] ?? BotChatSummary() }

    /// The summaries of `new`, made from the summaries of `old`. Only the bots with a changed message get a new scan,
    /// so a streamed word costs one comparison for each message and one scan of the messages of one bot.
    func updated(from old: [Message], to new: [Message]) -> ChatSummaries {
        var changed: Set<String> = []
        let shared = min(old.count, new.count)
        for index in 0..<shared where old[index] != new[index] {
            changed.insert(old[index].botId)
            changed.insert(new[index].botId)
        }
        for message in old[shared...] { changed.insert(message.botId) }
        for message in new[shared...] { changed.insert(message.botId) }
        guard !changed.isEmpty else { return self }
        var scans: [String: Scan] = [:]
        // Most updates change one bot. A comparison with its ID is faster than a set.
        let only = changed.count == 1 ? changed.first : nil
        for (index, message) in new.enumerated() where only.map({ $0 == message.botId }) ?? changed.contains(message.botId) {
            scans[message.botId, default: Scan()].add(message, at: index)
        }
        var result = self
        for bot in changed {
            let summary = scans[bot]?.summary(in: new)
            result.bots[bot] = summary
            result.newestActivity[bot] = summary?.newestActivity
        }
        return result
    }

    /// One pass over the messages of one bot.
    private struct Scan {
        var newestDate: Date?
        var newestActivity: Date?
        var streaming = false
        /// The newest text message, and the newest one that is not the old completion label. The first of two messages
        /// with the same date stays, as `max` keeps it.
        var text: (index: Int, date: Date)?
        var content: (index: Int, date: Date)?

        mutating func add(_ message: Message, at index: Int) {
            let date = message.createdAt.apiDate
            if let date {
                if newestDate.map({ date > $0 }) ?? true { newestDate = date }
                if message.reactionEvent == nil, newestActivity.map({ date > $0 }) ?? true { newestActivity = date }
            }
            if message.streaming { streaming = true }
            guard isPreviewText(message) else { return }
            let time = date ?? .distantPast
            if text.map({ $0.date < time }) ?? true { text = (index, time) }
            if !isCompletionLabel(message), content.map({ $0.date < time }) ?? true { content = (index, time) }
        }

        func summary(in messages: [Message]) -> BotChatSummary {
            BotChatSummary(latestMessage: (content ?? text).map { messages[$0.index] }, newestDate: newestDate,
                           newestActivity: newestActivity, streaming: streaming)
        }
    }
}

/// Whether a row can show the message as its line: a finished text of the person or of a bot.
func isPreviewText(_ message: Message) -> Bool {
    message.kind == "text" && !message.streaming && message.reactionEvent == nil && (message.role == "user" || message.role == "assistant")
        && !TranscriptPresentation.isSilenceControl(message)
        && !TranscriptPresentation.trimmed(message.text, equals: "")
}

/// Older hosts stored this completion event as a text message.
func isCompletionLabel(_ message: Message) -> Bool {
    message.role == "assistant" && TranscriptPresentation.trimmed(message.text, equals: "Ready to review.")
}
