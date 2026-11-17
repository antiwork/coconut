import XCTest
@testable import Coconut

/// The unread count of an agent goes when the person reads its chat, also for a reply that comes while it is open.
@MainActor
final class UnreadCountTests: XCTestCase {
    func testReadingAChatClearsItsCountAtOnce() async throws {
        let store = AppStore(demo: true)
        let id = try XCTUnwrap(store.bots.first?.id)
        store.bots[0].unreadCount = 2
        await store.markRead(store.bots[0])
        XCTAssertEqual(store.bots.first { $0.id == id }?.unreadCount, 0)
        // A reply comes while the chat is open: the next read clears it too.
        store.bots[0].unreadCount = 1
        await store.markRead(store.bots[0])
        XCTAssertEqual(store.bots.first { $0.id == id }?.unreadCount, 0)
    }

    /// Slack's "New" line goes above the first row after the message that the person read last.
    func testTheNewLineGoesBelowTheReadMessage() throws {
        let store = AppStore(demo: true)
        let found = store.bots.first { TranscriptEntry.grouped(store.conversationTranscript(for: $0.id, focusDecisionId: nil),
                                                               botId: $0.id, calendar: .current).count >= 2 }
        var bot = try XCTUnwrap(found)
        let entries = TranscriptEntry.grouped(store.conversationTranscript(for: bot.id, focusDecisionId: nil),
                                              botId: bot.id, calendar: .current)
        bot.unreadCount = 1
        bot.lastReadMessageId = entries[0].messages.last?.id
        let mark = try XCTUnwrap(UnreadMark(bot: bot))
        XCTAssertEqual(UnreadMark.firstNewRow(after: mark.messageId, in: entries), entries[1].id)
        // Read through the last row: no line.
        XCTAssertNil(UnreadMark.firstNewRow(after: try XCTUnwrap(entries.last?.messages.last?.id), in: entries))
        // A chat without unread replies has no mark.
        bot.unreadCount = 0
        XCTAssertNil(UnreadMark(bot: bot))
    }
}
