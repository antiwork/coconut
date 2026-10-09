import XCTest
@testable import Coconut

final class NotificationRoutingTests: XCTestCase {
    private let decision = UUID().uuidString
    private let message = UUID().uuidString
    private let bot = UUID().uuidString

    private func target(decisionId: String? = nil, messageId: String? = nil) throws -> NotificationTarget {
        var data: [String: Any] = ["version": 1, "device_id": UUID().uuidString, "bot_id": bot, "task_id": UUID().uuidString]
        data["decision_id"] = decisionId
        data["message_id"] = messageId
        return try XCTUnwrap(NotificationTarget(userInfo: ["coconut": data]))
    }

    func testPendingDecisionOpensInbox() throws {
        XCTAssertEqual(try target(decisionId: decision).destination(pendingDecisionIds: [decision], snapshotReady: true), .inbox(decision))
    }

    func testCachedPendingDecisionStillWaitsForFreshState() throws {
        XCTAssertNil(try target(decisionId: decision).destination(pendingDecisionIds: [decision], snapshotReady: false))
    }

    func testColdStartWaitsForSnapshotBeforeFallingBackToChat() throws {
        let target = try target(decisionId: decision)
        XCTAssertNil(target.destination(pendingDecisionIds: [], snapshotReady: false))
        XCTAssertEqual(target.destination(pendingDecisionIds: [], snapshotReady: true), .conversation(ConversationRoute(botId: bot, decisionId: decision)))
    }

    func testMessageOpensExactMessageWithoutWaitingForSnapshot() throws {
        XCTAssertEqual(try target(messageId: message).destination(pendingDecisionIds: [], snapshotReady: false), .conversation(ConversationRoute(botId: bot, messageId: message)))
    }

    func testMalformedMessageTargetIsRejected() {
        XCTAssertNil(NotificationTarget(userInfo: ["coconut": ["version": 1, "device_id": UUID().uuidString,
            "bot_id": bot, "task_id": UUID().uuidString, "message_id": "bad-id"]]))
    }

    func testDefaultIsTheConfirmedWorkWindow() {
        let schedule = NotificationSchedule()
        XCTAssertEqual(schedule, NotificationSchedule(enabled: true, start: "09:00", end: "18:00", timezone: "America/New_York"))
        XCTAssertTrue(schedule.isValid)
        XCTAssertEqual(schedule.weekdays, [1, 2, 3, 4, 5, 6, 7])
        XCTAssertFalse(schedule.honorRoutine)
    }

    func testRejectsNoDaysOutOfRangeAndRepeatedDays() {
        XCTAssertFalse(NotificationSchedule(weekdays: []).isValid)
        XCTAssertFalse(NotificationSchedule(weekdays: [0]).isValid)
        XCTAssertFalse(NotificationSchedule(weekdays: [8]).isValid)
        XCTAssertFalse(NotificationSchedule(weekdays: [1, 1]).isValid)
        XCTAssertTrue(NotificationSchedule(weekdays: [1, 2, 3, 4, 5]).isValid)
        XCTAssertTrue(NotificationSchedule(weekdays: [7]).isValid)
        XCTAssertNil(NotificationSchedule(weekdays: []).registrable)
    }

    func testTurningOffWithNoDaysStillSendsAValidImmediateSchedule() {
        let off = NotificationSchedule(enabled: false, weekdays: [], honorRoutine: true)
        let sent = off.registrable
        XCTAssertEqual(sent?.enabled, false)
        XCTAssertEqual(sent?.isValid, true)
    }

    func testDaySummaryReadsCalmly() {
        XCTAssertEqual(NotificationSchedule.summary(of: [1, 2, 3, 4, 5, 6, 7]), "Every day")
        XCTAssertEqual(NotificationSchedule.summary(of: [5, 4, 3, 2, 1]), "Weekdays")
        XCTAssertEqual(NotificationSchedule.summary(of: [6, 7]), "Weekends")
        XCTAssertEqual(NotificationSchedule.summary(of: [1, 3]), "Mon, Wed")
        XCTAssertEqual(NotificationSchedule.summary(of: []), "None")
    }

    func testScheduleEncodesWeekdaysAndHonorRoutineWithSnakeCaseKeys() throws {
        let schedule = NotificationSchedule(weekdays: [1, 3, 5], honorRoutine: true)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: APIJSON.encode(schedule)) as? [String: Any])
        XCTAssertEqual(object["weekdays"] as? [Int], [1, 3, 5])
        XCTAssertEqual(object["honor_routine"] as? Bool, true)
        XCTAssertNil(object["honorRoutine"])
        let decoded = try APIJSON.decoder().decode(NotificationSchedule.self, from: APIJSON.encode(schedule))
        XCTAssertEqual(decoded, schedule)
    }

    func testClockFormatsMinutesAsTwentyFourHourText() {
        XCTAssertEqual(NotificationSchedule.clock(9 * 60), "09:00")
        XCTAssertEqual(NotificationSchedule.clock(17 * 60 + 5), "17:05")
        XCTAssertEqual(NotificationSchedule.clock(0), "00:00")
    }

    func testRejectsEmptyReversedAndUnknownWindows() {
        XCTAssertFalse(NotificationSchedule(start: "18:00", end: "09:00").isValid)
        XCTAssertFalse(NotificationSchedule(start: "09:00", end: "09:00").isValid)
        XCTAssertFalse(NotificationSchedule(timezone: "Mars/Base").isValid)
    }

    func testRegistrationSendsTheEditedScheduleToTheBackend() throws {
        let schedule = NotificationSchedule(enabled: false, start: "08:30", end: "17:15", timezone: "Europe/Paris",
                                            weekdays: [1, 2, 3, 4, 5], honorRoutine: true)
        let registration = PushRegistration(token: "ab", environment: "development", enabled: true,
                                            preferences: NotificationPreferences(decisions: true, messages: false), schedule: schedule)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: APIJSON.encode(registration)) as? [String: Any])
        let sent = try XCTUnwrap(object["schedule"] as? [String: Any])
        XCTAssertEqual(sent["enabled"] as? Bool, false)
        XCTAssertEqual(sent["start"] as? String, "08:30")
        XCTAssertEqual(sent["end"] as? String, "17:15")
        XCTAssertEqual(sent["timezone"] as? String, "Europe/Paris")
        XCTAssertEqual(sent["weekdays"] as? [Int], [1, 2, 3, 4, 5])
        XCTAssertEqual(sent["honor_routine"] as? Bool, true)
        XCTAssertEqual(Set(sent.keys), ["enabled", "start", "end", "timezone", "weekdays", "honor_routine"])
    }

    func testRegistrationOmitsAnInvalidOrMissingSchedule() throws {
        let registration = PushRegistration(token: "ab", environment: "development", enabled: true)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: APIJSON.encode(registration)) as? [String: Any])
        XCTAssertNil(object["schedule"])
    }

    func testTurningTheScheduleOffIsSentEvenWhenTheEditedWindowIsInvalid() {
        let off = NotificationSchedule(enabled: false, start: "18:00", end: "09:00", timezone: "Mars/Base")
        XCTAssertFalse(off.isValid)
        let sent = off.registrable
        XCTAssertEqual(sent?.enabled, false)
        XCTAssertEqual(sent?.isValid, true)
    }

    func testAnInvalidEnabledWindowIsHeldBackAndAValidOneIsSentAsIs() {
        XCTAssertNil(NotificationSchedule(start: "18:00", end: "09:00").registrable)
        let valid = NotificationSchedule(enabled: false, start: "08:30", end: "17:15", timezone: "Europe/Paris")
        XCTAssertEqual(valid.registrable, valid)
    }

    func testPickerMinutesRoundTripAndNeverGoNegative() {
        for minutes in [0, 1, 9 * 60, 17 * 60 + 5, 23 * 60 + 59] {
            XCTAssertEqual(NotificationSchedule.minutes(of: NotificationSchedule.date(minutes: minutes)), minutes)
        }
        // A date before the 1970 epoch, such as the wheel rolling back past midnight.
        let before = Date(timeIntervalSince1970: -90 * 60)
        XCTAssertEqual(NotificationSchedule.minutes(of: before), 22 * 60 + 30)
        XCTAssertEqual(NotificationSchedule.clock(-30), "23:30")
        XCTAssertEqual(NotificationSchedule.date(minutes: -30), NotificationSchedule.date(minutes: 23 * 60 + 30))
    }

    func testTurningTheToggleOffRegistersAnImmediateScheduleAndOnRestoresTheWindow() {
        var schedule = NotificationSchedule(start: "08:00", end: "17:00", timezone: "Europe/Paris")
        XCTAssertEqual(schedule.registrable?.enabled, true)
        schedule.enabled = false
        XCTAssertEqual(schedule.registrable?.enabled, false)
        XCTAssertEqual(schedule.registrable?.start, "08:00")
        schedule.enabled = true
        XCTAssertEqual(schedule.registrable, schedule)
    }

    private func isolatedDefaults() throws -> (defaults: UserDefaults, suiteName: String) {
        let name = "NotificationScheduleTests-" + UUID().uuidString
        return (try XCTUnwrap(UserDefaults(suiteName: name)), name)
    }

    func testNothingSavedLoadsTheDefaultSchedule() throws {
        let isolated = try isolatedDefaults()
        defer { isolated.defaults.removePersistentDomain(forName: isolated.suiteName) }
        XCTAssertEqual(NotificationSchedule.load(from: isolated.defaults), NotificationSchedule())
    }

    func testSavedValuesLoadAndUnreadableOnesTakeDefaults() throws {
        let isolated = try isolatedDefaults()
        let defaults = isolated.defaults
        defer { defaults.removePersistentDomain(forName: isolated.suiteName) }
        defaults.set(false, forKey: "afterHoursImportantOnly")
        defaults.set(8 * 60 + 30, forKey: "workStartMinutes")
        defaults.set(17 * 60 + 15, forKey: "workEndMinutes")
        defaults.set("Europe/Paris", forKey: "workTimeZone")
        defaults.set([1, 3, 5], forKey: "workWeekdays")
        defaults.set(true, forKey: "honorRoutineSchedules")
        XCTAssertEqual(NotificationSchedule.load(from: defaults),
                       NotificationSchedule(enabled: false, start: "08:30", end: "17:15", timezone: "Europe/Paris", weekdays: [1, 3, 5], honorRoutine: true))
        XCTAssertEqual(NotificationSchedule.minutes(fromClock: "08:30"), 8 * 60 + 30)
        XCTAssertNil(NotificationSchedule.minutes(fromClock: "nonsense"))
        defaults.set("not days", forKey: "workWeekdays")
        XCTAssertEqual(NotificationSchedule.load(from: defaults).weekdays, NotificationSchedule.allWeekdays)
    }

    private func signature(_ schedule: NotificationSchedule?, token: String? = "ab") -> String {
        PushRegistration.signature(deviceId: "d", environment: "development", token: token, allowed: true,
                                   preferences: NotificationPreferences(decisions: true, messages: true), schedule: schedule)
    }

    func testEveryScheduleFieldChangesTheRegistrationSignature() {
        let base = NotificationSchedule()
        let variants = [NotificationSchedule(enabled: false), NotificationSchedule(start: "08:00"), NotificationSchedule(end: "19:00"),
                        NotificationSchedule(timezone: "Europe/Paris"), NotificationSchedule(weekdays: [1, 2]), NotificationSchedule(honorRoutine: true)]
        let signatures = Set(variants.map { signature($0) } + [signature(base)])
        XCTAssertEqual(signatures.count, variants.count + 1)
        XCTAssertEqual(signature(base), signature(NotificationSchedule()))
        XCTAssertNotEqual(signature(base), signature(base, token: "cd"))
    }

    func testInvalidEnabledEditsAreWithheldAndLeaveTheSignatureUnchanged() {
        let invalid = [NotificationSchedule(start: "18:00", end: "09:00"), NotificationSchedule(weekdays: []), NotificationSchedule(timezone: "Mars/Base")]
        for draft in invalid {
            XCTAssertNil(draft.registrable)
            XCTAssertEqual(signature(draft.registrable), signature(nil))
            let body = PushRegistration(token: "ab", environment: "development", enabled: true, schedule: draft.registrable)
            XCTAssertNil((try? JSONSerialization.jsonObject(with: APIJSON.encode(body)) as? [String: Any])?["schedule"])
        }
    }

    func testDisabledInvalidDraftIsSentAsAValidOffScheduleAndChangesTheSignature() throws {
        let draft = NotificationSchedule(enabled: false, start: "18:00", end: "09:00", timezone: "Mars/Base", weekdays: [])
        let sent = try XCTUnwrap(draft.registrable)
        XCTAssertTrue(sent.isValid)
        XCTAssertNotEqual(signature(sent), signature(NotificationSchedule()))
        let body = PushRegistration(token: "ab", environment: "development", enabled: true, schedule: sent)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: APIJSON.encode(body)) as? [String: Any])
        let schedule = try XCTUnwrap(object["schedule"] as? [String: Any])
        XCTAssertEqual(schedule["enabled"] as? Bool, false)
        XCTAssertEqual(schedule["weekdays"] as? [Int], NotificationSchedule.allWeekdays)
        XCTAssertEqual(schedule["timezone"] as? String, NotificationSchedule.defaultTimeZone)
    }
}
