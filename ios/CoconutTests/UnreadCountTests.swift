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
}
