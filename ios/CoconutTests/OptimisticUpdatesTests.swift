import XCTest
@testable import Coconut

@MainActor
final class OptimisticUpdatesTests: XCTestCase {
    @MainActor private final class Gate {
        var entered = false
        var continuation: CheckedContinuation<Void, Never>?
        func wait() async { entered = true; await withCheckedContinuation { continuation = $0 } }
        func open() { continuation?.resume(); continuation = nil }
    }
    private func entered(_ gate: Gate) async {
        for _ in 0..<100 where !gate.entered { await Task.yield() }
        XCTAssertTrue(gate.entered)
    }

    func testAnEditIsVisibleBeforeTheRequestAndSurvivesAnOlderSnapshot() async {
        let updates = OptimisticUpdates(), gate = Gate()
        var value = false
        updates.enqueue("pin", change: { id in updates.set("pin", to: true, in: id, read: { value }, write: { value = $0 }) }, save: {
            await gate.wait()
            updates.receive { value = true }
        })
        XCTAssertTrue(value)
        updates.receive { value = false }
        XCTAssertTrue(value)
        await entered(gate); gate.open(); await updates.wait()
        XCTAssertTrue(value)
        updates.receive { value = false }
        XCTAssertFalse(value, "After confirmation a later external edit can take effect.")
    }

    func testAFailureRestoresTheLatestServerValueInsteadOfAnOldCopy() async {
        let updates = OptimisticUpdates(), gate = Gate()
        var name = "Before"
        updates.enqueue("name", change: { id in updates.set("name", to: "My edit", in: id, read: { name }, write: { name = $0 }) }, save: { await gate.wait() })
        updates.receive { name = "Changed on the Mac" }
        XCTAssertEqual(name, "My edit")
        await entered(gate); gate.open(); await updates.wait()
        XCTAssertEqual(name, "Changed on the Mac")
    }

    func testRapidTapsAreSentInOrderAndAnEarlierFailureCannotUndoTheLastChoice() async {
        let updates = OptimisticUpdates(), first = Gate(), second = Gate()
        var pinned = false
        updates.enqueue("chat", change: { id in updates.set("pin", to: true, in: id, read: { pinned }, write: { pinned = $0 }) }, save: { await first.wait() })
        updates.enqueue("chat", change: { id in updates.set("pin", to: false, in: id, read: { pinned }, write: { pinned = $0 }) }, save: {
            await second.wait()
            updates.receive { pinned = false }
        })
        XCTAssertFalse(pinned)
        await entered(first)
        XCTAssertFalse(second.entered)
        first.open(); await entered(second)
        XCTAssertFalse(pinned)
        second.open(); await updates.wait()
        XCTAssertFalse(pinned)
    }

    func testTwoFailedQueuedEditsRestoreTheOriginalValue() async {
        let updates = OptimisticUpdates(), first = Gate(), second = Gate()
        var value = "Original"
        for (name, gate) in [("First", first), ("Second", second)] {
            updates.enqueue("name", change: { id in updates.set("name", to: name, in: id, read: { value }, write: { value = $0 }) }, save: { await gate.wait() })
        }
        await entered(first); first.open(); await entered(second)
        XCTAssertEqual(value, "Second")
        second.open(); await updates.wait()
        XCTAssertEqual(value, "Original")
    }

    func testDifferentResourcesDoNotBlockEachOther() async {
        let updates = OptimisticUpdates(), gate = Gate()
        var first = 0, second = 0
        updates.enqueue("one", change: { id in updates.set("one", to: 1, in: id, read: { first }, write: { first = $0 }) }, save: { await gate.wait() })
        updates.enqueue("two", change: { id in updates.set("two", to: 2, in: id, read: { second }, write: { second = $0 }) }, save: { updates.receive { second = 2 } })
        await entered(gate)
        for _ in 0..<10 { await Task.yield() }
        XCTAssertEqual(second, 2)
        gate.open(); await updates.wait()
        XCTAssertEqual(first, 0)
        XCTAssertEqual(second, 2)
    }

    func testResetDiscardsPendingEditsAndLateReceiptsCannotProjectIntoTheNewSession() async {
        let updates = OptimisticUpdates(), gate = Gate()
        var value = "Old"
        updates.enqueue("name", change: { id in updates.set("name", to: "Pending", in: id, read: { value }, write: { value = $0 }) }, save: { await gate.wait() })
        await entered(gate)
        var waiting = false
        let blocked = Task { waiting = true; return await updates.wait(for: "name") }
        for _ in 0..<100 where !waiting { await Task.yield() }
        XCTAssertTrue(waiting)
        updates.reset(); value = "New pairing"
        gate.open(); await updates.wait()
        let ready = await blocked.value
        XCTAssertFalse(ready, "Work from the old pairing must not continue on the new pairing.")
        XCTAssertEqual(value, "New pairing")
    }

    func testPartialBotEditsKeepLiveStatusAndOtherFields() async throws {
        let store = AppStore(demo: true), gate = Gate()
        let bot = try XCTUnwrap(store.bots.first)
        store.optimistically("bot/\(bot.id)", change: { store.optimisticBot(bot.id, \.pinned, true, in: $0) }, save: { await gate.wait() })
        store.optimisticUpdates.receive {
            if let i = store.bots.firstIndex(where: { $0.id == bot.id }) { store.bots[i].status = "working"; store.bots[i].unreadCount = 7 }
        }
        XCTAssertEqual(store.bots.first { $0.id == bot.id }?.pinned, true)
        await entered(gate); gate.open(); await store.optimisticUpdates.wait()
        let result = try XCTUnwrap(store.bots.first { $0.id == bot.id })
        XCTAssertEqual(result.pinned, bot.pinned)
        XCTAssertEqual(result.status, "working")
        XCTAssertEqual(result.unreadCount, 7)
    }

    func testEachNewIntentHasItsOwnReceiptKeyEvenWhenTheValueRepeats() async {
        let store = AppStore(demo: true)
        var keys: [String?] = []
        for _ in 0..<3 {
            store.optimistically("bot/test", change: { _ in }, save: { keys.append(OptimisticRequest.id) })
        }
        await store.optimisticUpdates.wait()
        XCTAssertEqual(keys.compactMap { $0 }.count, 3)
        XCTAssertEqual(Set(keys.compactMap { $0 }).count, 3, "A later repeated value must not replay an uncertain receipt from before an intervening change.")
    }

    func testWorkWaitsForSettingsAndAFailedPrerequisiteStopsDispatch() async {
        let updates = OptimisticUpdates(), gate = Gate()
        updates.enqueue("native-settings", change: { _ in }, save: {
            await gate.wait()
            updates.markFailed()
        })
        var waiting = false
        let ready = Task { waiting = true; return await updates.wait(for: "native-settings") }
        await entered(gate)
        for _ in 0..<100 where !waiting { await Task.yield() }
        XCTAssertTrue(waiting)
        gate.open()
        let result = await ready.value
        XCTAssertFalse(result)
        let retry = await updates.wait(for: "native-settings")
        XCTAssertTrue(retry, "A later explicit retry uses the now-confirmed settings.")
    }
}
