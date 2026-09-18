import XCTest
@testable import Coconut

final class StateUpdatesTests: XCTestCase {
    func testPreviousCacheKeepsVisibleContentButRequiresOneFullSnapshot() throws {
        var snapshot = try PreviewStateFixture.load()
        snapshot.syncVersion = 1
        let cache = SnapshotCache(snapshot: snapshot, syncedAt: Date())
        let data = try JSONEncoder().encode(cache)
        var old = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        old.removeValue(forKey: "updateProtocol")
        let previous = try JSONDecoder().decode(SnapshotCache.self, from: JSONSerialization.data(withJSONObject: old))
        XCTAssertNil(previous.snapshotForRestore.syncVersion)
        XCTAssertEqual(previous.snapshotForRestore.messages.map(\.id), snapshot.messages.map(\.id))
        XCTAssertEqual(previous.snapshotForRestore.artifacts.map(\.id), snapshot.artifacts.map(\.id))
        let current = try JSONDecoder().decode(SnapshotCache.self, from: data)
        XCTAssertEqual(current.snapshotForRestore.syncVersion, 1)
    }

    func testProtocolThreeCacheRequestsRecentHistorySnapshot() throws {
        var snapshot = try PreviewStateFixture.load()
        snapshot.syncVersion = 1
        let cache = SnapshotCache(snapshot: snapshot, syncedAt: Date())
        var old = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(cache)) as? [String: Any])
        old["updateProtocol"] = 3
        let previous = try JSONDecoder().decode(SnapshotCache.self, from: JSONSerialization.data(withJSONObject: old))
        XCTAssertNil(previous.snapshotForRestore.syncVersion)
        XCTAssertEqual(previous.snapshotForRestore.decisions, snapshot.decisions)
    }

    func testNativeFreshnessKeepsUnchangedWorkVisibleWithoutReplacingArtifacts() throws {
        var state = try PreviewStateFixture.load()
        state.cursor = 10
        var work = state.artifacts[0]
        work.nativeTaskId = "t_native"
        work.workState = "working"
        work.activity = "Running tests"
        work.activityObservedAt = "2026-09-18T12:00:00Z"
        var legacy = work
        legacy.id = "legacy"
        legacy.nativeTaskId = nil
        state.artifacts = [work, legacy]
        let observed = "2026-09-18T12:05:00Z"
        let page = StateUpdates(after: 10, cursor: 10, serverTime: observed,
            collectionDeltas: true, nativeWorkObservedAt: observed)
        let updated = try XCTUnwrap(page.applying(to: state))
        let item = TrackedWorkItem(artifact: updated.artifacts[0], pullRequests: [])
        let current = try XCTUnwrap(observed.apiDate)
        XCTAssertEqual(item.activity(at: current), "Running tests")
        XCTAssertTrue(item.isWorking(at: current))
        XCTAssertEqual(updated.artifacts[1], legacy)
        XCTAssertEqual(updated.artifacts[0].title, work.title)
        XCTAssertFalse(item.isWorking(at: current.addingTimeInterval(61)))
        let stale = StateUpdates(after: 10, cursor: 10, serverTime: "2026-09-18T12:10:00Z",
            collectionDeltas: true, nativeWorkObservedAt: observed)
        let staleState = try XCTUnwrap(stale.applying(to: updated))
        XCTAssertEqual(staleState.artifacts[0].activityObservedAt, observed)
    }

    func testUpdatesAddTasksReplaceCardsAndRemoveHiddenMessages() throws {
        var state = try PreviewStateFixture.load()
        state.cursor = 10
        let oldMessage = state.messages[0]
        var message = oldMessage
        message.id = "new-message"
        message.text = "New output"
        var task = state.tasks[0]
        task.id = "new-task"
        task.status = "working"
        var decision = state.decisions[0]
        decision.revision += 1
        decision.title = "Revised question"
        let page = StateUpdates(after: 10, cursor: 13, serverTime: state.serverTime,
            tasks: [task], messages: [message, message], removedMessages: [oldMessage.id], decisions: [decision])
        let updated = try XCTUnwrap(page.applying(to: state))
        XCTAssertEqual(updated.tasks.last?.id, task.id)
        XCTAssertEqual(updated.messages.filter { $0.id == message.id }.count, 1)
        XCTAssertFalse(updated.messages.contains { $0.id == oldMessage.id })
        XCTAssertEqual(updated.decisions, [decision])
        XCTAssertEqual(updated.cursor, 13)
        XCTAssertNil(page.applying(to: updated), "A repeated response cannot replace newer state.")
    }

    func testCollectionDeltasKeepUnchangedCardsAndRemoveHiddenRecords() throws {
        var state = try PreviewStateFixture.load()
        state.cursor = 10
        var kept = state.decisions[0]
        kept.id = "kept"
        var removed = kept
        removed.id = "removed"
        var revised = kept
        revised.id = "revised"
        state.decisions = [kept, removed, revised]
        var file = state.artifacts[0]
        file.id = "removed-file"
        var keptFile = file
        keptFile.id = "kept-file"
        state.artifacts = [file, keptFile]
        revised.title = "Current question"
        revised.revision += 1
        let page = StateUpdates(after: 10, cursor: 12, serverTime: state.serverTime,
            decisions: [revised], artifacts: [], collectionDeltas: true,
            removedDecisions: [removed.id], removedArtifacts: [file.id])
        let updated = try XCTUnwrap(page.applying(to: state))
        XCTAssertEqual(updated.decisions, [kept, revised])
        XCTAssertEqual(updated.artifacts, [keptFile])
        XCTAssertNil(page.applying(to: updated))
    }

    func testReplayGapRequiresCurrentStateAndResetUsesMatchingCursor() throws {
        var state = try PreviewStateFixture.load()
        state.cursor = 10
        XCTAssertNil(StateUpdates(after: 9, cursor: 11, serverTime: state.serverTime).applying(to: state))
        XCTAssertNil(StateUpdates(after: 10, cursor: 9, serverTime: state.serverTime).applying(to: state))
        var reset = state
        reset.cursor = 30
        reset.messages = []
        let page = StateUpdates(after: 10, cursor: 30, serverTime: state.serverTime, reset: reset)
        XCTAssertEqual(page.applying(to: state)?.messages, [])
        XCTAssertEqual(page.applying(to: state)?.cursor, 30)
        reset.cursor = 3
        XCTAssertEqual(StateUpdates(after: 10, cursor: 3, serverTime: state.serverTime, reset: reset).applying(to: state)?.cursor, 3)
        reset.cursor = 29
        XCTAssertNil(StateUpdates(after: 10, cursor: 30, serverTime: state.serverTime, reset: reset).applying(to: state))
    }

    func testBotReceiptMergesFieldsWithoutReplacingNewerActivity() throws {
        let original = try PreviewStateFixture.load().bots[0]
        var receipt = original
        receipt.name = "Updated name"; receipt.avatar = "banana"
        var current = original
        current.avatar = receipt.avatar; current.status = "working"; current.unreadCount = 7
        let merged = StateUpdates.mergeBotReceipt(receipt, original: original, current: current)
        XCTAssertEqual(merged.name, receipt.name)
        XCTAssertEqual(merged.avatar, receipt.avatar)
        XCTAssertEqual(merged.status, "working")
        XCTAssertEqual(merged.unreadCount, 7)
        current.name = "Newer name"
        XCTAssertEqual(StateUpdates.mergeBotReceipt(receipt, original: original, current: current).name, "Newer name")
    }

    func testNewRepliesKeepTheirChronologicalOrder() throws {
        var state = try PreviewStateFixture.load()
        var early = state.messages[0]
        early.id = "early"; early.createdAt = "2026-09-15T00:00:00Z"
        var late = early
        late.id = "late"; late.createdAt = "2026-09-15T00:00:01Z"
        state.messages = []; state.cursor = 1
        let updated = StateUpdates(after: 1, cursor: 2, serverTime: state.serverTime, messages: [late, early]).applying(to: state)
        XCTAssertEqual(updated?.messages.map(\.id), ["early", "late"])
    }

    func testHistoryCannotOverwriteAnUpdatedMessage() throws {
        var old = try PreviewStateFixture.load().messages[0]
        var current = old
        current.text = "Current text"
        old.text = "Old text"
        XCTAssertEqual(StateUpdates.merge([current], into: [old]).first?.text, "Current text")
    }
}
