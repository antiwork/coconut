import XCTest
@testable import Coconut

/// The live view of a turn: the feed of the Mac, as the panel reads and shows it.
final class LiveActivityTests: XCTestCase {
    private let feedJSON = #"""
    {"task_id": "00000000-0000-4000-8000-000000000023", "revision": 7, "started_at": "2026-09-08T11:55:42.120Z",
     "ended_at": null,
     "entries": [
       {"id": 1, "tool": "read_file", "phase": "reading", "detail": "/Users/ada/projects/gumroad/app/models/payment.rb",
        "path": "/Users/ada/projects/gumroad/app/models/payment.rb", "status": "ok", "at": "2026-09-08T11:55:43.000Z",
        "seconds": 0.2, "call_id": "c1"},
       {"id": 2, "tool": "terminal", "phase": "testing", "detail": "bundle exec rspec spec/payouts", "status": "error",
        "at": "2026-09-08T11:55:50.000Z", "seconds": 38.4, "error": "1 example, 1 failure"},
       {"id": 3, "tool": "browser_vision", "phase": "browsing", "detail": "", "status": "ok",
        "at": "2026-09-08T11:56:40.000Z", "seconds": 4.8, "images": ["0123456789abcdef0123456789abcdef"]},
       {"id": 4, "tool": "terminal", "phase": "command", "detail": "bin/rails runner script/affected_sellers.rb",
        "status": "running", "at": "2026-09-08T11:57:00.000Z"}
     ],
     "files": [
       {"path": "/Users/ada/projects/gumroad/app/models/payment.rb", "action": "read", "count": 1, "at": "2026-09-08T11:55:43.000Z"},
       {"path": "app/services/payouts/processor.rb", "action": "edit", "count": 3, "at": "2026-09-08T11:56:00.000Z"}
     ],
     "images": [
       {"id": "0123456789abcdef0123456789abcdef", "entry": 3, "tool": "browser_vision", "name": "browser_screenshot_1.png",
        "at": "2026-09-08T11:56:44.000Z"}
     ]}
    """#

    private func feed() throws -> TurnActivity {
        try APIJSON.decoder().decode(TurnActivity.self, from: Data(feedJSON.utf8))
    }

    func testTheAppReadsTheFeedOfTheMac() throws {
        let feed = try feed()
        XCTAssertEqual(feed.taskId, "00000000-0000-4000-8000-000000000023")
        XCTAssertTrue(feed.isRunning)
        XCTAssertFalse(feed.isEmpty)
        XCTAssertEqual(feed.entries.map(\.id), [1, 2, 3, 4])
        // The timeline, the files and the film strip show the newest first.
        XCTAssertEqual(feed.timeline.map(\.id), [4, 3, 2, 1])
        XCTAssertEqual(feed.recentFiles.map(\.name), ["processor.rb", "payment.rb"])
        XCTAssertEqual(feed.running.map(\.id), [4])
        XCTAssertEqual(feed.entries[2].images, ["0123456789abcdef0123456789abcdef"])
        XCTAssertEqual(feed.images.first?.source, "Browser")
        XCTAssertNotNil(feed.startedAt?.apiDate)
        XCTAssertTrue(TurnActivity.empty("t").isEmpty)
        XCTAssertFalse(TurnActivity.empty("t").isRunning)
    }

    func testAnEntrySaysWhatItsCallDoesInOneLine() throws {
        let entries = try feed().entries
        XCTAssertEqual(entries.map(\.title), ["Read", "Test", "Screenshot", "Run"])
        XCTAssertEqual(entries[0].line, "models/payment.rb")
        XCTAssertTrue(entries[0].cutsStart)
        XCTAssertFalse(entries[1].cutsStart)
        XCTAssertEqual(entries[0].file, "/Users/ada/projects/gumroad/app/models/payment.rb")
        XCTAssertEqual(entries[1].line, "bundle exec rspec spec/payouts")
        XCTAssertNil(entries[1].file)
        XCTAssertTrue(entries[1].failed)
        XCTAssertEqual(entries[2].line, "")
        XCTAssertTrue(entries[3].isRunning)
        let page = ActivityEntry(id: 9, tool: "browser_navigate", phase: "browsing", detail: "https://www.example.com/a/",
                                 url: "https://www.example.com/a/", status: "ok", at: "")
        XCTAssertEqual((page.title), "Browse")
        XCTAssertEqual(page.line, "example.com/a")
        // A phase that this app does not know is plain work, not a fault.
        let later = ActivityEntry(id: 10, tool: "connectors__linear_create_issue", phase: "planning", status: "ok", at: "")
        XCTAssertEqual(later.botPhase, .working)
        XCTAssertEqual(later.title, "Linear create issue")
    }

    func testAFinishedTurnHasNoRunningCall() throws {
        var feed = try feed()
        feed.endedAt = "2026-09-08T12:00:00.000Z"
        XCTAssertFalse(feed.isRunning)
        XCTAssertEqual(feed.running, [])
    }

    func testOnlyANewerFeedReplacesTheFeed() throws {
        let feed = try feed()
        var older = feed
        older.revision = 5
        XCTAssertFalse(feed.isReplaced(by: older))
        var newer = feed
        newer.revision = 8
        XCTAssertTrue(feed.isReplaced(by: newer))
        XCTAssertTrue(feed.isReplaced(by: feed))
        // A new turn of the task starts its revisions again.
        var next = TurnActivity.empty(feed.taskId)
        next.startedAt = "2026-09-08T12:30:00.000Z"
        next.revision = 1
        XCTAssertTrue(feed.isReplaced(by: next))
        XCTAssertFalse(feed.isReplaced(by: TurnActivity(taskId: "other", revision: 99, startedAt: feed.startedAt,
                                                        endedAt: nil, entries: [], files: [], images: [])))
    }

    func testAFileShowsItsNameItsFolderAndItsTouches() throws {
        let files = try feed().files
        XCTAssertEqual(files[0].name, "payment.rb")
        XCTAssertEqual(files[0].folder, "~/projects/gumroad/app/models")
        XCTAssertEqual(files[0].note, "Read")
        XCTAssertTrue(files[0].canPreview)
        XCTAssertEqual(files[1].folder, "app/services/payouts")
        XCTAssertEqual(files[1].note, "Edited 3 times")
        // The Mac finds a file only by its whole path.
        XCTAssertFalse(files[1].canPreview)
        XCTAssertEqual(LiveActivityText.folder("/etc/hosts"), "/etc")
        XCTAssertEqual(LiveActivityText.folder("README.md"), "")
        XCTAssertEqual(LiveActivityText.shortPath("/Users/ada/notes.md"), "~/notes.md")
        XCTAssertEqual(LiveActivityText.shortPath("a/b.py"), "a/b.py")
    }

    func testTheTimeOfACallIsShort() {
        XCTAssertNil(LiveActivityText.duration(nil))
        XCTAssertNil(LiveActivityText.duration(0.04))
        XCTAssertEqual(LiveActivityText.duration(0.4), "0.4s")
        XCTAssertEqual(LiveActivityText.duration(9.96), "10.0s")
        XCTAssertEqual(LiveActivityText.duration(38.4), "38s")
        XCTAssertEqual(LiveActivityText.duration(245), "4m 05s")
    }

    func testTheRouteOfAFileKeepsItsPathInOneQueryValue() {
        let path = APIClient.activityFilePath(taskId: "t1", file: "/Users/ada/My Files/a&b+c=d?.swift")
        XCTAssertEqual(path, "tasks/t1/activity/file?path=/Users/ada/My%20Files/a%26b%2Bc%3Dd%3F.swift")
        XCTAssertEqual(URLComponents(string: path)?.queryItems?.first?.value, "/Users/ada/My Files/a&b+c=d?.swift")
    }

    func testTheUpdatesNameTheTasksWhoseActivityChanged() throws {
        let page = try APIJSON.decoder().decode(StateUpdates.self, from: Data(#"""
        {"after": 4, "cursor": 6, "server_time": "2026-09-08T12:00:00Z", "activity_changed": ["t1", "t2"]}
        """#.utf8))
        XCTAssertEqual(page.activityChanged, ["t1", "t2"])
    }

    @MainActor
    func testTheLivePanelOfThePreviewShowsARunningTurn() async throws {
        let store = AppStore(demo: true)
        let bot = try XCTUnwrap(store.bots.first { store.conversationActivity($0.id) != nil })
        let task = try XCTUnwrap(store.liveTask(bot.id))
        XCTAssertTrue(task.isRunning)
        // The working line of the chat names the command, as the live panel does.
        XCTAssertEqual(store.conversationActivity(bot.id)?.phase, .command)
        XCTAssertNil(store.turnActivity[task.id])
        store.watchActivity(task.id)
        await store.loadActivity(task.id)
        let feed = try XCTUnwrap(store.turnActivity[task.id])
        XCTAssertTrue(feed.isRunning)
        XCTAssertEqual(feed.running.count, 1)
        XCTAssertEqual(feed.images.count, 2)
        // Each image of an entry is an image of the feed.
        let images = Set(feed.images.map(\.id))
        XCTAssertTrue(feed.entries.allSatisfy { Set($0.images ?? []).isSubset(of: images) })
        let preview = try await store.activityFilePreview(ActivityFileRoute(taskId: task.id, path: feed.files[0].path))
        XCTAssertEqual(preview.name, "payment.rb")
        // The last panel of the task closed, so the store keeps its feed no longer.
        store.unwatchActivity(task.id)
        XCTAssertNil(store.turnActivity[task.id])
        // An agent without a turn has an empty feed, not the feed of the preview.
        let idle = try XCTUnwrap(store.bots.first { store.conversationActivity($0.id) == nil })
        let earlier = try XCTUnwrap(store.liveTask(idle.id))
        store.watchActivity(earlier.id)
        await store.loadActivity(earlier.id)
        XCTAssertEqual(store.turnActivity[earlier.id]?.isEmpty, true)
    }
}
