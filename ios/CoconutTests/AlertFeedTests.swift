import XCTest
@testable import Coconut

final class AlertFeedTests: XCTestCase {
    private let device = "00000000-0000-4000-8000-000000000001"

    private func alert(_ id: String, level: String = "info", created: String = "2026-11-16T12:00:00Z",
                       read: String? = nil, resolved: String? = nil) -> FeedAlert {
        FeedAlert(id: id, level: level, source: "cron:test", title: "Alert \(id)", body: "", createdAt: created,
                  readAt: read, resolvedAt: resolved)
    }

    func testAnAlertOfTheMacDecodesWithEachField() throws {
        let json = """
        {"items": [{"id": "a1", "level": "critical", "source": "cron:daily-ledger", "title": "daily-ledger failed",
          "body": "Request 20261116T1200 status=error", "url": "https://example.com/run/7",
          "created_at": "2026-11-16T12:00:00.123456Z", "read_at": null, "resolved_at": null, "dedupe_key": "ledger"},
          {"id": "a2", "level": "fatal", "source": "watchdog:x", "title": "A level of a newer Mac", "body": "",
          "url": "javascript:alert(1)", "created_at": "2026-11-16T11:00:00Z", "read_at": "2026-11-16T11:05:00Z",
          "resolved_at": "2026-11-16T11:06:00Z", "dedupe_key": null}], "unread": 1}
        """
        let feed = try APIJSON.decoder().decode(AlertFeed.self, from: Data(json.utf8))
        XCTAssertEqual(feed.unread, 1)
        let first = feed.items[0]
        XCTAssertEqual(first.severity, .critical)
        XCTAssertEqual(first.source, "cron:daily-ledger")
        XCTAssertEqual(first.link, URL(string: "https://example.com/run/7"))
        XCTAssertNotNil(first.createdAt.apiDate)
        XCTAssertTrue(first.isUnread)
        XCTAssertFalse(first.isResolved)
        // A level that this app does not know shows as a notice, and only an http address is a link.
        let second = feed.items[1]
        XCTAssertEqual(second.severity, .notice)
        XCTAssertNil(second.link)
        XCTAssertFalse(second.isUnread)
        XCTAssertTrue(second.isResolved)
    }

    func testAStateWithoutAlertsDecodesAndAnUpdateReplacesTheFeed() throws {
        var snapshot = try PreviewStateFixture.load()
        var fields = try XCTUnwrap(JSONSerialization.jsonObject(with: APIJSON.encoder().encode(snapshot)) as? [String: Any])
        fields.removeValue(forKey: "alerts")
        let older = try APIJSON.decoder().decode(StateSnapshot.self, from: JSONSerialization.data(withJSONObject: fields))
        XCTAssertNil(older.alerts)

        snapshot.syncVersion = 1
        let cursor = snapshot.cursor
        let page = """
        {"after": \(cursor), "cursor": \(cursor + 1), "server_time": "2026-11-16T12:00:01Z", "collection_deltas": true,
         "alerts": {"items": [{"id": "new", "level": "notice", "source": "watchdog:x", "title": "New", "body": "",
         "url": null, "created_at": "2026-11-16T12:00:00Z", "read_at": null, "resolved_at": null}], "unread": 4}}
        """
        let updates = try APIJSON.decoder().decode(StateUpdates.self, from: Data(page.utf8))
        let next = try XCTUnwrap(updates.applying(to: snapshot))
        XCTAssertEqual(next.alerts?.items.map(\.id), ["new"])
        XCTAssertEqual(next.alerts?.unread, 4)
        // An update without alerts keeps the feed.
        let quiet = """
        {"after": \(cursor + 1), "cursor": \(cursor + 2), "server_time": "2026-11-16T12:00:02Z", "collection_deltas": true}
        """
        let later = try XCTUnwrap(APIJSON.decoder().decode(StateUpdates.self, from: Data(quiet.utf8)).applying(to: next))
        XCTAssertEqual(later.alerts, next.alerts)
    }

    @MainActor
    func testTheDemoHasFiveAlertsAndTheBellCountsTheUnreadOnes() async {
        let store = AppStore(demo: true)
        XCTAssertEqual(store.alertFeed.items.count, 5)
        XCTAssertEqual(store.alertFeed.items.filter(\.isUnread).map(\.level), ["critical", "notice", "info"])
        XCTAssertEqual(store.unreadAlertCount, 3)
        // The badge of the Inbox tab stays about decisions.
        let inbox = store.inboxBadgeCount

        await store.markAlertRead(store.alertFeed.items[0])
        XCTAssertEqual(store.unreadAlertCount, 2)
        await store.markAlertRead(store.alertFeed.items[0])
        XCTAssertEqual(store.unreadAlertCount, 2)

        await store.resolveAlert(store.alertFeed.items[1])
        XCTAssertEqual(store.unreadAlertCount, 1)
        XCTAssertTrue(store.alertFeed.items[1].isResolved)
        XCTAssertFalse(store.alertFeed.items[1].isUnread)

        await store.markAllAlertsRead()
        XCTAssertEqual(store.unreadAlertCount, 0)
        XCTAssertTrue(store.alertFeed.items.allSatisfy { !$0.isUnread })
        XCTAssertEqual(store.inboxBadgeCount, inbox)
    }

    func testTheFeedGroupsItsAlertsByDayWithTheOpenOnesFirst() {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "UTC")!
        let now = "2026-11-16T15:00:00Z".apiDate!
        let days = AlertDay.days(of: [
            alert("morning", created: "2026-11-16T08:00:00Z"),
            alert("noon-resolved", created: "2026-11-16T12:00:00Z", read: "2026-11-16T12:01:00Z", resolved: "2026-11-16T12:01:00Z"),
            alert("afternoon", created: "2026-11-16T14:00:00Z"),
            alert("yesterday", created: "2026-11-15T23:59:00Z"),
            alert("last-week", created: "2026-11-05T10:00:00Z"),
        ], now: now, calendar: calendar)
        XCTAssertEqual(days.map(\.alerts).map { $0.map(\.id) },
                       [["afternoon", "morning", "noon-resolved"], ["yesterday"], ["last-week"]])
        XCTAssertEqual(days.map(\.label).prefix(2), ["Today", "Yesterday"])
    }

    func testVoiceOverReadsTheLevelTheTitleTheSourceAndTheAge() {
        let now = "2026-11-16T12:05:00Z".apiDate!
        let spoken = AlertSpeech.row(alert("a1", level: "critical"), now: now)
        XCTAssertTrue(spoken.hasPrefix("critical, Alert a1, cron:test, "), spoken)
        XCTAssertEqual(spoken, "critical, Alert a1, cron:test, " + AlertSpeech.relative("2026-11-16T12:00:00Z".apiDate!, now: now))
        // An alert from a Mac whose clock is ahead never reads as a time in the future.
        XCTAssertEqual(AlertSpeech.relative(now.addingTimeInterval(30), now: now), AlertSpeech.relative(now, now: now))
    }

    func testTheNotificationOfACriticalAlertOpensTheFeed() throws {
        let payload: [AnyHashable: Any] = ["coconut": ["version": 1, "device_id": device, "kind": "alert",
                                                         "alert_id": "a1e5c0de-0000-4000-8000-000000000001"]]
        let target = try XCTUnwrap(NotificationTarget(userInfo: payload))
        XCTAssertTrue(target.alert)
        XCTAssertEqual(target.deviceId, device)
        XCTAssertEqual(target.destination(pendingDecisionIds: [], snapshotReady: false), .alerts)
    }
}
