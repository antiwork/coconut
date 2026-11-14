import XCTest
@testable import Coconut

final class NotificationSentenceTests: XCTestCase {
    private let nineToSix = NotificationSchedule()

    private func text(_ level: NotificationLevel, _ outside: OutsideWorkHours, replies: Bool,
                      schedule: NotificationSchedule? = nil) -> String {
        NotificationSentence.text(level: level, outside: outside, replies: replies, schedule: schedule ?? nineToSix)
    }

    func testEveryCombinationHasItsExactSentence() {
        let urgentOutside = " Outside those hours, only urgent cards notify you."
        let nothingOutside = " Outside those hours, nothing notifies you."
        let expected: [(NotificationLevel, OutsideWorkHours, Bool, String)] = [
            (.every, .urgent, false, "From 9:00 to 18:00, Coconut notifies you about every card." + urgentOutside
                + " Other cards wait in Inbox until 9:00, then one notification tells you about them."),
            (.every, .urgent, true, "From 9:00 to 18:00, Coconut notifies you about every card and every agent reply." + urgentOutside
                + " Other cards and agent replies wait in Inbox until 9:00, then one notification tells you about them."),
            (.every, .nothing, false, "From 9:00 to 18:00, Coconut notifies you about every card." + nothingOutside
                + " Cards wait in Inbox until 9:00, then one notification tells you about them."),
            (.every, .nothing, true, "From 9:00 to 18:00, Coconut notifies you about every card and every agent reply." + nothingOutside
                + " Cards and agent replies wait in Inbox until 9:00, then one notification tells you about them."),
            (.every, .same, false, "At any time, Coconut notifies you about every card."),
            (.every, .same, true, "At any time, Coconut notifies you about every card and every agent reply."),
            (.grouped, .urgent, false, "From 9:00 to 18:00, Coconut sends one summary at a time about new cards. "
                + "Urgent cards notify you at once." + urgentOutside
                + " Other cards wait in Inbox until 9:00, then one notification tells you about them."),
            (.grouped, .urgent, true, "From 9:00 to 18:00, Coconut sends one summary at a time about new cards and agent replies. "
                + "Urgent cards notify you at once." + urgentOutside
                + " Other cards and agent replies wait in Inbox until 9:00, then one notification tells you about them."),
            (.grouped, .nothing, false, "From 9:00 to 18:00, Coconut sends one summary at a time about new cards. "
                + "Urgent cards notify you at once." + nothingOutside
                + " Cards wait in Inbox until 9:00, then one notification tells you about them."),
            (.grouped, .nothing, true, "From 9:00 to 18:00, Coconut sends one summary at a time about new cards and agent replies. "
                + "Urgent cards notify you at once." + nothingOutside
                + " Cards and agent replies wait in Inbox until 9:00, then one notification tells you about them."),
            (.grouped, .same, false, "At any time, Coconut sends one summary at a time about new cards. Urgent cards notify you at once."),
            (.grouped, .same, true, "At any time, Coconut sends one summary at a time about new cards and agent replies. "
                + "Urgent cards notify you at once."),
        ]
        for (level, outside, replies, sentence) in expected {
            XCTAssertEqual(text(level, outside, replies: replies), sentence, "\(level) \(outside) \(replies)")
        }
        // Only urgent turns bot replies off, and outside work hours Only urgent changes nothing.
        let onlyUrgent = "At any time, only urgent cards notify you. Other cards wait in Inbox without a notification."
        for replies in [false, true] {
            XCTAssertEqual(text(.urgentOnly, .urgent, replies: replies), onlyUrgent)
            XCTAssertEqual(text(.urgentOnly, .same, replies: replies), onlyUrgent)
            XCTAssertEqual(text(.urgentOnly, .nothing, replies: replies),
                           "From 9:00 to 18:00, only urgent cards notify you. Other cards wait in Inbox without a notification."
                           + " Outside those hours, nothing notifies you. Urgent cards wait until 9:00, then one notification tells you about them.")
        }
    }

    func testATeamAddsTeamMessagesToEachSentence() {
        func team(_ level: NotificationLevel, _ outside: OutsideWorkHours, replies: Bool) -> String {
            NotificationSentence.text(level: level, outside: outside, replies: replies, team: true, schedule: nineToSix)
        }
        let urgentOutside = " Outside those hours, only urgent cards notify you."
        let nothingOutside = " Outside those hours, nothing notifies you."
        let expected: [(NotificationLevel, OutsideWorkHours, Bool, String)] = [
            (.every, .urgent, true, "From 9:00 to 18:00, Coconut notifies you about every card, every agent reply and every team message."
                + urgentOutside + " Other cards, agent replies and team messages wait until 9:00, then one notification tells you about them."),
            (.every, .urgent, false, "From 9:00 to 18:00, Coconut notifies you about every card and every team message."
                + urgentOutside + " Other cards and team messages wait until 9:00, then one notification tells you about them."),
            (.every, .nothing, true, "From 9:00 to 18:00, Coconut notifies you about every card, every agent reply and every team message."
                + nothingOutside + " Cards, agent replies and team messages wait until 9:00, then one notification tells you about them."),
            (.every, .nothing, false, "From 9:00 to 18:00, Coconut notifies you about every card and every team message."
                + nothingOutside + " Cards and team messages wait until 9:00, then one notification tells you about them."),
            (.every, .same, true, "At any time, Coconut notifies you about every card, every agent reply and every team message."),
            (.every, .same, false, "At any time, Coconut notifies you about every card and every team message."),
            (.grouped, .urgent, true, "From 9:00 to 18:00, Coconut sends one summary at a time about new cards, agent replies and team messages. "
                + "Urgent cards notify you at once." + urgentOutside
                + " Other cards, agent replies and team messages wait until 9:00, then one notification tells you about them."),
            (.grouped, .urgent, false, "From 9:00 to 18:00, Coconut sends one summary at a time about new cards and team messages. "
                + "Urgent cards notify you at once." + urgentOutside
                + " Other cards and team messages wait until 9:00, then one notification tells you about them."),
            (.grouped, .nothing, true, "From 9:00 to 18:00, Coconut sends one summary at a time about new cards, agent replies and team messages. "
                + "Urgent cards notify you at once." + nothingOutside
                + " Cards, agent replies and team messages wait until 9:00, then one notification tells you about them."),
            (.grouped, .nothing, false, "From 9:00 to 18:00, Coconut sends one summary at a time about new cards and team messages. "
                + "Urgent cards notify you at once." + nothingOutside
                + " Cards and team messages wait until 9:00, then one notification tells you about them."),
            (.grouped, .same, true, "At any time, Coconut sends one summary at a time about new cards, agent replies and team messages. "
                + "Urgent cards notify you at once."),
            (.grouped, .same, false, "At any time, Coconut sends one summary at a time about new cards and team messages. "
                + "Urgent cards notify you at once."),
        ]
        for (level, outside, replies, sentence) in expected {
            XCTAssertEqual(team(level, outside, replies: replies), sentence, "\(level) \(outside) \(replies)")
        }
        // Only urgent turns team messages off, as it turns bot replies off. The text stays as without a team.
        for outside in OutsideWorkHours.allCases {
            for replies in [false, true] {
                XCTAssertEqual(team(.urgentOnly, outside, replies: replies), text(.urgentOnly, outside, replies: replies))
            }
        }
        // The next work day stays in the sentence with a team.
        XCTAssertTrue(NotificationSentence.text(level: .grouped, outside: .urgent, replies: true, team: true,
                                                schedule: NotificationSchedule(weekdays: [1, 2, 3, 4, 5]))
            .hasSuffix(" Other cards, agent replies and team messages wait until 9:00 on the next work day, "
                       + "then one notification tells you about them."))
    }

    func testOnlyAnActiveTeamWithItsSwitchOnNotifiesThePhone() throws {
        func room(_ state: String, _ notifications: Bool?) -> TeamRoom {
            TeamRoom(room: "r-\(state)-\(String(describing: notifications))", name: "Example Team", state: state,
                     notifications: notifications)
        }
        XCTAssertTrue(room("active", true).notifiesPhone)
        XCTAssertFalse(room("active", false).notifiesPhone)
        XCTAssertFalse(room("active", nil).notifiesPhone, "A team of an older service has no switch.")
        for state in ["joining", "removed", "gone"] {
            XCTAssertFalse(room(state, true).notifiesPhone, state)
        }
        XCTAssertFalse(TeamRooms(rooms: []).notifiesPhone)
        XCTAssertFalse(TeamRooms(rooms: [room("active", false), room("joining", true), room("active", nil)]).notifiesPhone)
        XCTAssertTrue(TeamRooms(rooms: [room("active", false), room("active", true)]).notifiesPhone)
        // An older service sends no switch.
        let old = try APIJSON.decoder().decode(TeamRooms.self,
                                               from: Data(#"{"rooms": [{"room": "r", "name": "Example Team", "state": "active"}]}"#.utf8))
        XCTAssertFalse(old.notifiesPhone)
    }

    func testTheSentenceNamesTheWorkDaysAndTheNextWorkDay() {
        let weekdays = NotificationSchedule(start: "08:30", end: "17:00", weekdays: [1, 2, 3, 4, 5])
        XCTAssertEqual(text(.every, .urgent, replies: false, schedule: weekdays),
                       "From 8:30 to 17:00 on weekdays, Coconut notifies you about every card. Outside those hours, only urgent cards notify you."
                       + " Other cards wait in Inbox until 8:30 on the next work day, then one notification tells you about them.")
        XCTAssertTrue(text(.every, .urgent, replies: false, schedule: NotificationSchedule(weekdays: [1, 3]))
            .hasPrefix("From 9:00 to 18:00 on Mon, Wed, "))
    }

    func testTheWorkHoursRowSummarizesTheEditor() {
        XCTAssertEqual(NotificationSentence.workHours(nineToSix, currentTimeZone: "America/New_York"), "9:00–18:00, every day")
        XCTAssertEqual(NotificationSentence.workHours(NotificationSchedule(weekdays: [6, 7]), currentTimeZone: "America/New_York"),
                       "9:00–18:00, weekends")
        XCTAssertEqual(NotificationSentence.workHours(NotificationSchedule(timezone: "America/Los_Angeles"), currentTimeZone: "Europe/Paris"),
                       "9:00–18:00, every day, Los Angeles time")
    }

    func testTheLiveSettingsKeepTheirBehavior() throws {
        // The live iPhone saved Grouped, no messages and the work hours switch on, before Outside work hours existed.
        let name = "NotificationSentenceTests-" + UUID().uuidString
        let defaults = try XCTUnwrap(UserDefaults(suiteName: name))
        defer { defaults.removePersistentDomain(forName: name) }
        defaults.set("grouped", forKey: "notificationLevel")
        defaults.set(true, forKey: "afterHoursImportantOnly")
        XCTAssertEqual(NotificationLevel.load(from: defaults), .grouped)
        XCTAssertEqual(OutsideWorkHours.load(from: defaults), .urgent)
        XCTAssertTrue(NotificationSchedule.load(from: defaults).enabled)
        defaults.set(false, forKey: "afterHoursImportantOnly")
        XCTAssertEqual(OutsideWorkHours.load(from: defaults), .same)
        defaults.set("nothing", forKey: OutsideWorkHours.key)
        XCTAssertEqual(OutsideWorkHours.load(from: defaults), .nothing)
        XCTAssertTrue(NotificationSchedule.load(from: defaults).enabled)
        defaults.removeObject(forKey: "afterHoursImportantOnly")
        defaults.removeObject(forKey: OutsideWorkHours.key)
        XCTAssertEqual(OutsideWorkHours.load(from: defaults), .urgent)
    }

    func testTheMacGetsOutsideWorkHoursBesideTheScheduleAndTheSignatureChanges() throws {
        let body = PushRegistration(token: "ab", environment: "development", enabled: true, schedule: NotificationSchedule(),
                                    outsideWorkHours: OutsideWorkHours.nothing.registrable)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: APIJSON.encode(body)) as? [String: Any])
        XCTAssertEqual(object["outside_work_hours"] as? String, "nothing")
        XCTAssertNil((object["schedule"] as? [String: Any])?["outside"])
        XCTAssertEqual(OutsideWorkHours.same.registrable, "urgent")
        let signatures = Set(OutsideWorkHours.allCases.map {
            PushRegistration.signature(deviceId: "d", environment: "development", token: "ab", allowed: true,
                                       preferences: NotificationPreferences(level: .grouped, messages: false),
                                       schedule: NotificationSchedule(enabled: $0 != .same), outside: $0)
        })
        XCTAssertEqual(signatures.count, 3)
    }

    @MainActor
    func testEachOutsideChoiceChangesWhatTheAppObservesForRegistration() {
        let push = PushNotifications.shared
        let saved = push.outsideWorkHours
        defer { push.outsideWorkHours = saved }
        push.outsideWorkHours = .urgent
        let urgent = push.choices
        push.outsideWorkHours = .nothing
        // Only urgent and Nothing send the same schedule. The choice itself must still start a registration.
        XCTAssertEqual(push.choices.schedule, urgent.schedule)
        XCTAssertNotEqual(push.choices, urgent)
    }

    func testTheUrgentSummaryDecodesAndAnOldMacSendsNone() throws {
        let json = #"{"registered": true, "last_delivered_at": null, "last_error": null, "last_error_at": null,"#
            + #" "urgent": {"week_urgent": 4, "week_total": 358, "examples": [{"title": "Approve the release", "bot_name": "Desk", "created_at": "2026-10-29T10:00:00Z"}]}}"#
        let status = try APIJSON.decoder().decode(PushStatus.self, from: Data(json.utf8))
        XCTAssertEqual(status.urgent?.weekText, "Last week: 4 urgent of 358 cards")
        XCTAssertEqual(status.urgent?.examples.first?.botName, "Desk")
        let old = try APIJSON.decoder().decode(PushStatus.self, from: Data(#"{"registered": true}"#.utf8))
        XCTAssertNil(old.urgent)
    }
}
