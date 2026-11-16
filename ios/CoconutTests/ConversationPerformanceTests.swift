import XCTest
@testable import Coconut

final class ConversationPerformanceTests: XCTestCase {
    @MainActor
    func testLongHistoryPreviewAvoidsPerMessageFormatterSetup() throws {
        let store = AppStore(demo: true)
        let template = try XCTUnwrap(store.messages.first { $0.kind == "text" })
        let messages = (0..<2_000).map { index in
            var message = template
            message.id = "history-\(index)"
            message.role = "assistant"
            message.text = "History \(index)"
            message.createdAt = index == 1_999 ? "2026-09-13T13:00:00+01:00" : "2026-09-12T12:00:00.123456Z"
            return message
        }
        let start = ContinuousClock.now
        let latest = latestConversationMessage(in: messages)
        let elapsed = start.duration(to: .now)
        XCTAssertEqual(latest?.id, "history-1999")
        // Leave room for a loaded simulator while detecting formatter allocation per comparison.
        XCTAssertLessThan(elapsed, .milliseconds(200))
        XCTAssertEqual("2026-09-13T13:00:00+01:00".apiDate, "2026-09-13T12:00:00Z".apiDate)
        XCTAssertNotNil("2026-09-12T12:00:00.123456Z".apiDate)
        XCTAssertNil("invalid".apiDate)
        print("COCONUT_PREVIEW_DURATION=\(elapsed)")
    }

    /// The sidebar of the Mac sorts its bots in one pass over the messages. The order must stay the order of a scan for
    /// each bot. The sections are shared code, so the iPhone tests run it too.
    func testTheMacSidebarKeepsTheOrderOfAScanForEachBot() {
        // The test makes its own bots and messages, so a change of the demo data cannot change it.
        func bot(_ id: String, created: String, pinned: Bool = false, archived: Bool = false) -> Bot {
            Bot(id: id, sessionId: "session-\(id)", name: id, role: "", avatar: "sparkle", color: "777777",
                instructions: "", status: "idle", lastMessage: "", unreadCount: 0, archived: archived,
                createdAt: created, pinned: pinned)
        }
        func message(_ id: String, bot: String, at date: String) -> Message {
            Message(id: id, botId: bot, role: "user", kind: "text", text: id, createdAt: date, streaming: false)
        }
        let bots = [
            bot("desk", created: "2026-11-01T12:00:00Z", pinned: true),
            bot("forge", created: "2026-11-01T12:00:00Z"),
            bot("scout", created: "2026-11-06T12:00:00Z"),
            bot("old", created: "2026-11-02T12:00:00Z", pinned: true),
            bot("gone", created: "2026-11-01T12:00:00Z", archived: true),
            bot("quiet", created: "invalid"),
            bot("news", created: "2026-11-01T12:00:00Z", pinned: true),
        ]
        let messages = [
            message("d1", bot: "desk", at: "2026-11-05T12:00:00Z"),
            message("f1", bot: "forge", at: "2026-11-07T12:00:00Z"),
            message("d2", bot: "desk", at: "2026-11-03T12:00:00Z"),
            message("f2", bot: "forge", at: "invalid"),
            message("g1", bot: "gone", at: "2026-11-09T12:00:00Z"),
            message("n1", bot: "news", at: "2026-11-08T12:00:00Z"),
        ]

        func scan(_ bot: Bot) -> Date {
            messages.filter { $0.botId == bot.id }.compactMap { $0.createdAt.apiDate }.max()
                ?? bot.createdAt.apiDate ?? .distantPast
        }
        let active = bots.filter { !$0.archived }.sorted { scan($0) > scan($1) }
        let sections = ChatSections.make(bots: bots, messages: messages, rooms: [])
        XCTAssertEqual(sections.pinned.map(\.id), ["news", "desk", "old"])
        XCTAssertEqual(sections.bots.map(\.id), ["bot-forge", "bot-scout", "bot-quiet"])
        XCTAssertEqual(sections.pinned.map(\.id), active.filter(\.pinned).map(\.id))
        XCTAssertEqual(sections.bots.map(\.id), active.filter { !$0.pinned }.map { "bot-" + $0.id })
        XCTAssertTrue(sections.people.isEmpty, "A Mac without a team has no People section.")
    }

    // MARK: Measurements

    /// The median of a few runs, in microseconds.
    private func microseconds(runs: Int = 7, _ work: () -> Void) -> Double {
        let times = (0..<runs).map { _ in
            let duration = ContinuousClock().measure(work)
            return Double(duration.components.seconds) * 1e6 + Double(duration.components.attoseconds) / 1e12
        }
        return times.sorted()[runs / 2]
    }

    private func report(_ name: String, before: Double, after: Double) {
        print(String(format: "COCONUT_PERF %@ before_us=%.1f after_us=%.1f ratio=%.1fx", name, before, after, before / max(after, 0.001)))
    }

    private func bot(_ id: String) -> Bot {
        Bot(id: id, sessionId: "session-\(id)", name: id, role: "", avatar: "sparkle", color: "777777",
            instructions: "", status: "idle", lastMessage: "Last", unreadCount: 0, archived: false,
            createdAt: "2026-09-01T00:00:00Z", pinned: false)
    }

    /// The history of a busy Mac: `count` messages of `bots` bots in the order of their time, with a text of a reply.
    private func history(_ count: Int, bots: Int = 12) -> [Message] {
        let text = "**Summary** The check retries each request once.\n\n- **queue** drains the worker\n- `head` commit\n\n"
            + String(repeating: "The worker reads the saved state and compares the head commit. ", count: 12)
        return (0..<count).map { index in
            let user = index % 2 == 0
            let day: Int = 1 + index / 3600 % 28
            let hour: Int = index / 60 % 24
            let second: Int = index % 60
            let date = String(format: "2026-09-%02dT%02d:00:%02d.%06dZ", day, hour, second, index)
            return Message(id: "message-\(index)", botId: "bot-\(index % bots)", taskId: "task-\(index / 2)",
                           role: user ? "user" : "assistant", kind: "text", text: user ? "Question \(index)" : text,
                           createdAt: date, streaming: false, clientRequestId: user ? "request-\(index)" : nil)
        }
    }

    /// What a row of the Chats list read for each redraw before the summaries: three scans of all messages.
    private func scannedSummary(_ botId: String, _ messages: [Message]) -> BotChatSummary {
        let own = messages.filter { $0.botId == botId }
        return BotChatSummary(latestMessage: latestConversationMessage(in: own),
                              newestDate: own.compactMap { $0.createdAt.apiDate }.max(),
                              newestActivity: ChatList.lastMessageDates(messages)[botId],
                              streaming: own.contains(where: \.streaming))
    }

    func testACachedDateEqualsItsParse() {
        let texts = ["2026-09-13T13:00:00+01:00", "2026-09-12T12:00:00.123456Z", "2026-09-12T12:00:00Z", "invalid", "",
                     "2026-09-13T13:00:00.5+00:00", "2026-02-30T00:00:00Z", "2026-09-13T13:00:00z"]
        for text in texts + texts {
            XCTAssertEqual(text.apiDate, text.parsedAPIDate, text)
        }
        // The cache stays under its limit.
        for index in 0..<(String.apiDates.limit + 50) { _ = "2031-01-01T00:00:00.\(index)Z".apiDate }
        XCTAssertLessThanOrEqual(String.apiDates.count, String.apiDates.limit)
        XCTAssertEqual("2026-09-12T12:00:00.123456Z".apiDate, "2026-09-12T12:00:00.123456Z".parsedAPIDate)

        let dates = history(5_000).map(\.createdAt)
        var sum = 0.0
        let before = microseconds { for text in dates { sum += text.parsedAPIDate?.timeIntervalSince1970 ?? 0 } }
        _ = dates.map(\.apiDate)
        let after = microseconds { for text in dates { sum += text.apiDate?.timeIntervalSince1970 ?? 0 } }
        XCTAssertGreaterThan(sum, 0)
        report("api_date_5000", before: before, after: after)
    }

    /// The summaries that the store keeps equal the scans of the rows, after each kind of change of the messages.
    func testChatSummariesEqualTheScansOfTheRows() {
        func message(_ id: String, _ bot: String, _ date: String, role: String = "assistant", kind: String = "text",
                     text: String? = nil, streaming: Bool = false, reaction: Bool = false) -> Message {
            Message(id: id, botId: bot, role: role, kind: kind, text: text ?? id, createdAt: date, streaming: streaming,
                    reactionEvent: reaction ? MessageReactionEvent(emoji: "👍", action: "added") : nil)
        }
        var messages = [
            message("a1", "a", "2026-11-01T10:00:00Z", role: "user"),
            message("a2", "a", "2026-11-01T11:00:00Z"),
            message("a3", "a", "2026-11-01T11:00:00Z"),
            message("a4", "a", "2026-11-01T12:00:00Z", text: "Ready to review."),
            message("a5", "a", "2026-11-01T13:00:00Z", role: "user", reaction: true),
            message("b1", "b", "2026-11-02T10:00:00Z", text: "Ready to review."),
            message("b2", "b", "invalid"),
            message("c1", "c", "2026-11-03T10:00:00Z", text: "[SILENT]"),
            message("c2", "c", "2026-11-03T11:00:00Z", text: "  "),
            message("c3", "c", "2026-11-03T12:00:00Z", role: "system"),
            message("d1", "d", "2026-11-04T10:00:00Z", kind: "activity"),
            message("e1", "e", "2026-11-05T10:00:00Z"),
            message("e2", "e", "2026-11-05T11:00:00Z", text: "Partial", streaming: true),
        ]
        let bots = ["a", "b", "c", "d", "e", "f", "none"]
        func check(_ summaries: ChatSummaries, _ step: String) {
            XCTAssertEqual(summaries, ChatSummaries(messages), step)
            XCTAssertEqual(summaries.newestActivity, ChatList.lastMessageDates(messages), step)
            for bot in bots { XCTAssertEqual(summaries[bot], scannedSummary(bot, messages), "\(step) \(bot)") }
        }
        var summaries = ChatSummaries(messages)
        check(summaries, "start")
        XCTAssertEqual(summaries["a"].latestMessage?.id, "a2")
        XCTAssertEqual(summaries["b"].latestMessage?.id, "b2", "The old completion label gives way to a reply.")
        XCTAssertNil(summaries["c"].latestMessage)
        XCTAssertTrue(summaries["e"].streaming)

        func change(_ step: String, _ edit: (inout [Message]) -> Void) {
            let old = messages
            edit(&messages)
            summaries = summaries.updated(from: old, to: messages)
            check(summaries, step)
        }
        change("a streamed word") { $0[12].text = "Partial reply" }
        change("the reply ends") { $0[12].streaming = false }
        change("a new message at the end") { $0.append(message("f1", "f", "2026-11-06T10:00:00Z", role: "user")) }
        change("a message in the middle") { $0.insert(message("b3", "b", "2026-11-02T12:00:00Z"), at: 7) }
        change("a removed message") { $0.removeAll { $0.id == "a2" } }
        change("the last message of a bot goes") { $0.removeAll { $0.botId == "d" } }
        change("a message moves to another bot") { $0[0].botId = "e" }
        change("a new order") { $0.reverse() }
        change("no change") { _ in }
        change("no message") { $0.removeAll() }
        XCTAssertEqual(summaries, ChatSummaries())

        // 12 rows of the Chats list with 5,000 messages. Before: each row scans all messages for each update.
        var long = history(5_000)
        long.append(message("stream", "bot-3", "2026-10-30T10:00:00Z", text: "Partial", streaming: true))
        var streamed = long
        streamed[5_000].text = "Partial reply"
        _ = long.map(\.createdAt.apiDate)
        let kept = ChatSummaries(long)
        var rows = 0
        let before = microseconds {
            // One update of the Mac sidebar: the order of the list, then the line, the age and the face of each row.
            rows += ChatList.lastMessageDates(streamed).count
            for index in 0..<12 {
                let id = "bot-\(index)"
                if latestConversationMessage(in: streamed.filter { $0.botId == id }) != nil { rows += 1 }
                if streamed.filter({ $0.botId == id }).compactMap({ $0.createdAt.apiDate }).max() != nil { rows += 1 }
                if streamed.lazy.filter({ $0.botId == id }).compactMap({ $0.createdAt.apiDate }).max() != nil { rows += 1 }
            }
        }
        let after = microseconds { if kept.updated(from: long, to: streamed) == kept { rows += 1 } }
        XCTAssertEqual(kept.updated(from: long, to: streamed), kept, "A streamed word changes no row.")
        XCTAssertGreaterThan(rows, 12)
        report("chat_rows_12_bots_5000_messages_streamed_word", before: before, after: after)
        let full = microseconds { rows += ChatSummaries(long).newestActivity.count }
        print(String(format: "COCONUT_PERF chat_summaries_first_build_5000 us=%.1f", full))
    }

    /// The line of a row is the line of the parse of its Markdown, for the first redraw and for each later one.
    func testAKeptPreviewEqualsItsParse() {
        let reply = history(2)[1].text
        let texts = ["", "Done.", "# Title\n\nA **bold** [link](https://example.com) and `code`.", "- one\n- two\n\n```\ncode **kept**\n```",
                     "> quote\n\n---\n1. first", reply, String(repeating: "Long line with *emphasis*. ", count: 200)]
        for text in texts + texts {
            XCTAssertEqual(TranscriptPresentation.previewText(text), TranscriptPresentation.plainPreview(String(text.prefix(1000))), text)
        }
        XCTAssertEqual(TranscriptPresentation.previewText(texts[2]), "Title A bold link and code.")
        // Two texts with the same first 1,000 characters have one preview. The cache stays under its limit.
        XCTAssertEqual(TranscriptPresentation.previewText(texts[6] + "More"), TranscriptPresentation.previewText(texts[6]))
        for index in 0..<(TranscriptPresentation.previews.limit + 20) { _ = TranscriptPresentation.previewText("Text \(index)") }
        XCTAssertLessThanOrEqual(TranscriptPresentation.previews.count, TranscriptPresentation.previews.limit)

        var count = 0
        _ = TranscriptPresentation.previewText(reply)
        let before = microseconds { for _ in 0..<12 { count += TranscriptPresentation.plainPreview(String(reply.prefix(1000))).count } }
        let after = microseconds { for _ in 0..<12 { count += TranscriptPresentation.previewText(reply).count } }
        XCTAssertGreaterThan(count, 0)
        report("preview_line_of_12_rows", before: before, after: after)
    }

    /// The store keeps the summaries with its messages, and a streamed word tells no reader of the summaries.
    @MainActor
    func testAStreamedWordDoesNotChangeTheSummariesOfTheStore() throws {
        let store = AppStore(demo: true)
        var stream = try XCTUnwrap(store.messages.first { $0.kind == "text" && $0.role == "assistant" })
        stream.id = "stream"
        stream.streaming = true
        stream.text = "Partial"
        stream.createdAt = "2027-01-01T00:00:00Z"
        store.messages.append(stream)
        XCTAssertEqual(store.chatSummaries, ChatSummaries(store.messages))
        XCTAssertTrue(store.chatSummaries[stream.botId].streaming)

        nonisolated(unsafe) var changes = 0
        func observe() {
            withObservationTracking { _ = store.chatSummaries } onChange: { changes += 1 }
        }
        observe()
        store.messages[store.messages.count - 1].text = "Partial reply"
        XCTAssertEqual(changes, 0, "A streamed word redraws no row of the Chats list.")
        XCTAssertEqual(store.savedMessage(id: "stream")?.text, "Partial reply")
        store.messages[store.messages.count - 1].streaming = false
        XCTAssertEqual(changes, 1, "The finished reply is the new line of its row.")
        XCTAssertEqual(store.chatSummaries[stream.botId].latestMessage?.id, "stream")
        XCTAssertEqual(store.chatSummaries, ChatSummaries(store.messages))
    }

    /// The rows of an open chat for one streamed word: 417 of 5,000 messages are of the chat. It is only measured.
    @MainActor
    func testTheTranscriptOfAnOpenChatIsMeasured() {
        let store = AppStore(demo: true)
        store.messages = history(5_000)
        var rows = 0
        let transcript = microseconds { rows += store.conversationTranscript(for: "bot-3").count }
        let messages = store.conversationTranscript(for: "bot-3")
        let grouped = microseconds { rows += TranscriptEntry.grouped(messages, botId: "bot-3", calendar: .current).count }
        XCTAssertEqual(messages.count, 417)
        XCTAssertGreaterThan(rows, 0)
        print(String(format: "COCONUT_PERF open_chat_417_of_5000 transcript_us=%.1f grouped_us=%.1f", transcript, grouped))
    }

    /// The index after an update equals a new index of the same messages.
    func testTheMessageIndexAfterAnUpdateEqualsANewIndex() {
        var messages = history(40)
        messages[7].clientRequestId = "request-4"
        var index = MessageIndex(messages)
        func change(_ step: String, _ edit: (inout [Message]) -> Void) {
            let old = messages
            edit(&messages)
            index.update(from: old, to: messages)
            XCTAssertEqual(index, MessageIndex(messages), step)
        }
        change("a streamed word") { $0[39].text += " more" }
        change("new messages at the end") { $0 += self.history(45).suffix(5) }
        change("a second message with a known request") { var copy = $0[4]; copy.id = "copy"; $0.append(copy) }
        change("a second message with a known ID") { var copy = $0[5]; copy.clientRequestId = "other"; $0.append(copy) }
        change("a message gets its request") { $0[9].clientRequestId = "late" }
        change("a message in the middle") { $0.insert(self.history(60)[55], at: 3) }
        change("a removed message") { $0.remove(at: 10) }
        change("a new order") { $0.reverse() }
        change("no message") { $0.removeAll() }
        XCTAssertEqual(index.first(id: "message-1"), nil)

        let long = history(5_000)
        var streamed = long
        streamed[4_999].text += " more"
        var kept = MessageIndex(long)
        var found = 0
        let before = microseconds { found += MessageIndex(streamed).first(id: "message-4999") ?? 0 }
        let after = microseconds { kept.update(from: long, to: streamed); found += kept.first(id: "message-4999") ?? 0 }
        XCTAssertEqual(kept, MessageIndex(streamed))
        XCTAssertGreaterThan(found, 0)
        report("message_index_5000_streamed_word", before: before, after: after)
    }

    /// A merge of a few values gives what the table of all places gave.
    func testAMergeOfAFewValuesEqualsTheMergeWithATable() {
        func tableMerge(_ updates: [Message], into values: [Message]) -> [Message] {
            var result = values
            var positions = Dictionary(uniqueKeysWithValues: values.enumerated().map { ($0.element.id, $0.offset) })
            for value in updates {
                if let index = positions[value.id] { result[index] = value }
                else { positions[value.id] = result.count; result.append(value) }
            }
            return result
        }
        let values = history(60)
        var edited = values[10]; edited.text = "Edited"
        var again = edited; again.text = "Edited again"
        var new = values[11]; new.id = "new"
        var newAgain = new; newAgain.text = "New again"
        let cases: [[Message]] = [[], [edited], [new], [edited, again], [new, newAgain, edited], [values[59], values[0]],
                                  Array(values.prefix(StateUpdates.scannedUpdates)), Array(values.prefix(StateUpdates.scannedUpdates + 1)),
                                  values.reversed() + [new, newAgain]]
        for updates in cases {
            XCTAssertEqual(StateUpdates.merge(updates, into: values), tableMerge(updates, into: values), "\(updates.map(\.id))")
            XCTAssertEqual(StateUpdates.merge(updates, into: []), tableMerge(updates, into: []), "\(updates.map(\.id))")
        }

        let long = history(5_000)
        var word = long[4_999]; word.text += " more"
        var count = 0
        let before = microseconds { count += tableMerge([word], into: long).count }
        let after = microseconds { count += StateUpdates.merge([word], into: long).count }
        XCTAssertGreaterThan(count, 0)
        report("merge_one_message_into_5000", before: before, after: after)
    }

    /// The new replies of a state, found by their places, equal the ones that a set of all IDs gave.
    func testNewRepliesEqualTheOnesOfASetOfAllMessages() throws {
        func setReplies(in incoming: StateSnapshot, after previous: StateSnapshot?) -> [Message] {
            guard let previous, incoming.cursor > previous.cursor else { return [] }
            let finished = Set(previous.messages.filter { !$0.streaming }.map(\.id))
            return incoming.messages.filter {
                $0.role == "assistant" && $0.kind == "text" && !$0.streaming && !finished.contains($0.id)
            }
        }
        var previous = try PreviewStateFixture.load()
        previous.messages = history(30)
        previous.messages[29].streaming = true
        var states: [StateSnapshot] = []
        func state(_ edit: (inout [Message]) -> Void) {
            var next = previous
            next.cursor += 1
            edit(&next.messages)
            states.append(next)
        }
        state { $0[29].text += " more" }
        state { $0[29].streaming = false }
        state { $0.append(self.history(40)[35]) }
        state { $0.insert(self.history(40)[37], at: 2) }
        state { $0.remove(at: 4) }
        state { $0.reverse() }
        state { $0[27].streaming = true }
        state { $0.removeAll() }
        for (index, next) in states.enumerated() {
            XCTAssertEqual(AppStore.newReplies(in: next, after: previous), setReplies(in: next, after: previous), "state \(index)")
            XCTAssertEqual(AppStore.newReplies(in: previous, after: next), setReplies(in: previous, after: next), "state \(index)")
        }
        XCTAssertEqual(AppStore.newReplies(in: states[1], after: previous).map(\.id), ["message-29"])

        var long = previous
        long.messages = history(5_000)
        long.messages[4_999].streaming = true
        var streamed = long
        streamed.cursor += 1
        streamed.messages[4_999].text += " more"
        var count = 0
        let before = microseconds { count += setReplies(in: streamed, after: long).count + 1 }
        let after = microseconds { count += AppStore.newReplies(in: streamed, after: long).count + 1 }
        XCTAssertGreaterThan(count, 0)
        report("new_replies_5000_streamed_word", before: before, after: after)
    }
}
