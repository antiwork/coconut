import XCTest
@testable import Coconut

@MainActor
final class SettingsFlowTests: XCTestCase {
    // MARK: Save on change

    func testUnchangedBotHasNothingToSave() throws {
        let bot = try decodedBot()
        XCTAssertNil(BotAutosave.changes(bot, comparedTo: bot))
    }

    func testAToggleChangeSavesByItself() throws {
        let original = try decodedBot()
        var draft = original
        draft.notifications = false

        let pending = try XCTUnwrap(BotAutosave.changes(draft, comparedTo: original))
        XCTAssertEqual(pending.notifications, false)
        XCTAssertEqual(pending.name, "Research")
    }

    func testAnEmptyNameKeepsTheSavedNameAndTheOtherEditsStillSave() throws {
        let original = try decodedBot()
        var draft = original
        draft.name = "   "
        draft.instructions = "Read the release notes."

        let pending = try XCTUnwrap(BotAutosave.changes(draft, comparedTo: original))
        XCTAssertEqual(pending.name, "Research")
        XCTAssertEqual(pending.instructions, "Read the release notes.")
        XCTAssertEqual(Set(try encodedObject(BotPatch(updated: pending, original: original)).keys), ["instructions"])
    }

    func testOnlyAnEmptyNameHasNothingToSave() throws {
        let original = try decodedBot()
        var draft = original
        draft.name = ""
        XCTAssertNil(BotAutosave.changes(draft, comparedTo: original))
    }

    func testAnEditDuringASaveSurvivesTheSaveReply() throws {
        let original = try decodedBot()
        var submitted = original
        submitted.notifications = false
        // The person turns notifications on again while the first save runs.
        var draft = submitted
        draft.notifications = true
        draft.instructions = "Read the release notes."

        let merged = BotAutosave.merge(draft: draft, baseline: submitted, into: submitted)
        XCTAssertTrue(merged.notifications)
        XCTAssertEqual(merged.instructions, "Read the release notes.")
        let pending = try XCTUnwrap(BotAutosave.changes(merged, comparedTo: submitted))
        XCTAssertEqual(Set(try encodedObject(BotPatch(updated: pending, original: submitted)).keys), ["instructions", "notifications"])
    }

    func testTheMacValueWinsForFieldsThatThePersonDidNotEdit() throws {
        let original = try decodedBot()
        var current = original
        current.name = "Researcher"
        var draft = original
        draft.color = "112233"

        let merged = BotAutosave.merge(draft: draft, baseline: original, into: current)
        XCTAssertEqual(merged.name, "Researcher")
        XCTAssertEqual(merged.color, "112233")
    }

    func testAStatusUpdateDuringASaveKeepsThePendingEditAndNoSaveWritesTheOldValue() throws {
        let original = try decodedBot()
        var edits = BotDraft(original)
        edits.bot.avatar = "fruit.apple"
        edits.bot.notifications = false
        let request = try XCTUnwrap(edits.beginSave())
        XCTAssertEqual(request.pending.avatar, "fruit.apple")

        // The Mac sends a status change that still has the old settings, before the save returns.
        var update = original
        update.status = "working"
        edits.receive(update)
        XCTAssertEqual(edits.bot.avatar, "fruit.apple")
        XCTAssertFalse(edits.bot.notifications)
        XCTAssertEqual(edits.bot.status, "working")
        // A save request during the save sends nothing now.
        XCTAssertNil(edits.beginSave())

        var confirmed = update
        confirmed.avatar = "fruit.apple"
        confirmed.notifications = false
        XCTAssertTrue(edits.finishSave(confirmed))
        XCTAssertEqual(edits.bot, confirmed)
        // The queued save has nothing to write, so the old values never go back to the Mac.
        XCTAssertNil(edits.beginSave())
    }

    func testAnEditDuringASaveSavesAfterTheConfirmedResult() throws {
        let original = try decodedBot()
        var edits = BotDraft(original)
        edits.bot.notifications = false
        let first = try XCTUnwrap(edits.beginSave())
        edits.bot.notifications = true
        XCTAssertNil(edits.beginSave())

        XCTAssertTrue(edits.finishSave(first.pending))
        XCTAssertTrue(edits.bot.notifications)
        let second = try XCTUnwrap(edits.beginSave())
        XCTAssertTrue(second.pending.notifications)
        XCTAssertFalse(second.original.notifications)
    }

    func testAFailedSaveKeepsTheEditForTheNextSave() throws {
        let original = try decodedBot()
        var edits = BotDraft(original)
        edits.bot.color = "112233"
        _ = try XCTUnwrap(edits.beginSave())
        XCTAssertFalse(edits.finishSave(nil))
        XCTAssertEqual(edits.bot.color, "112233")
        XCTAssertEqual(try XCTUnwrap(edits.beginSave()).pending.color, "112233")
    }

    // MARK: Connection banner

    func testDisconnectedHasABannerWithoutPair() {
        let notice = ConnectionState.disconnected.notice
        XCTAssertEqual(notice?.message, ConnectionState.disconnected.bannerMessage)
        XCTAssertEqual(notice?.offersPairing, false)
        XCTAssertNotNil(ConnectionState.disconnected.bannerMessage)
    }

    func testRepairOffersPairAndAConnectedRelayPairingShowsNoBanner() {
        XCTAssertEqual(ConnectionState.needsRepair.notice?.offersPairing, true)
        XCTAssertEqual(ConnectionState.relayUnreachable.notice?.offersPairing, false)
        XCTAssertNil(ConnectionState.connected.notice)
        XCTAssertNil(ConnectionState.preview.notice)
    }

    func testEveryStateHasATitle() {
        let states: [ConnectionState] = [.unconfigured, .connecting, .connected, .disconnected, .macOffline,
                                         .relayUnreachable, .needsRepair, .preview]
        XCTAssertEqual(Set(states.map(\.title)).count, states.count)
    }

    // MARK: Hermes version

    func testUntestedHermesShowsAWarning() throws {
        let json = """
        {"found": false, "profiles": [], "logins": [], "sign_ins": [], "model": null,
         "hermes": {"status": "ok", "version": "0.12.0", "reason": null, "tested": false}}
        """
        let setup = try APIJSON.decoder().decode(HermesSetup.self, from: Data(json.utf8))
        XCTAssertEqual(setup.hermes?.untestedWarning, "Hermes 0.12.0 is not tested with Coconut. Some features can fail.")
    }

    func testTestedOrUnknownHermesShowsNoWarning() {
        XCTAssertNil(HermesRuntimeStatus(version: "0.11.0", tested: true).untestedWarning)
        XCTAssertNil(HermesRuntimeStatus(version: nil, tested: nil).untestedWarning)
    }

    // MARK: Routine groups

    func testOneTimeJobsThatStillRunAreScheduledChecksInTimeOrder() throws {
        let now = try XCTUnwrap("2026-10-29T12:00:00Z".apiDate)
        let routines = [
            routine("daily", cron: "0 8 * * *", state: "scheduled"),
            routine("old-server", cron: "every 1h"),
            routine("later", oneTime: true, state: "scheduled", nextRunAt: "2026-10-29T14:00:00Z"),
            routine("sooner", oneTime: true, state: "scheduled", nextRunAt: "2026-10-29T12:15:00Z"),
            routine("paused-future", oneTime: true, state: "paused", enabled: false, runAt: "2026-10-30T09:00:00Z"),
            routine("done", oneTime: true, state: "completed", enabled: false),
            routine("failed", oneTime: true, state: "error", enabled: false),
            routine("paused-past", oneTime: true, state: "paused", enabled: false, nextRunAt: "2026-10-01T09:00:00Z"),
            routine("paused-no-time", oneTime: true, state: "paused", enabled: false),
        ]
        let groups = RoutineGroups(routines, now: now)
        XCTAssertEqual(groups.recurring.map(\.id), ["daily", "old-server"])
        XCTAssertEqual(groups.scheduledChecks.map(\.id), ["sooner", "later", "paused-future"])
    }

    func testARoutineFromAnOlderMacDecodesAsRecurring() throws {
        let json = """
        {"id": "r", "bot_id": "bot", "name": "Daily", "prompt": "", "cron": "0 8 * * *", "timezone": "UTC",
         "enabled": true, "next_run_at": null, "last_run_at": null, "last_task_id": null,
         "created_at": "2026-10-29T12:00:00Z", "updated_at": "2026-10-29T12:00:00Z"}
        """
        let decoded = try APIJSON.decoder().decode(Routine.self, from: Data(json.utf8))
        XCTAssertNil(decoded.oneTime)
        XCTAssertEqual(RoutineGroups([decoded]).recurring.map(\.id), ["r"])
    }

    func testARoutineWithAScriptCheckDecodesItsScriptName() throws {
        let json = """
        {"id": "r", "bot_id": "bot", "name": "Intake", "prompt": "Triage new events.", "cron": "*/5 * * * *",
         "timezone": "UTC", "enabled": true, "next_run_at": null, "last_run_at": null, "last_task_id": null,
         "created_at": "2026-10-29T12:00:00Z", "updated_at": "2026-10-29T12:00:00Z", "script": null,
         "script_check": "intake_gate.py", "error": null}
        """
        let decoded = try APIJSON.decoder().decode(Routine.self, from: Data(json.utf8))
        XCTAssertEqual(decoded.scriptCheck, "intake_gate.py")
        XCTAssertNil(decoded.script)
        XCTAssertNil(decoded.error)
        XCTAssertEqual(RoutineGroups([decoded]).recurring.map(\.id), ["r"])
    }

    private func routine(_ id: String, cron: String = "once in 15m", oneTime: Bool? = nil, state: String? = nil,
                         enabled: Bool = true, nextRunAt: String? = nil, runAt: String? = nil) -> Routine {
        Routine(id: id, botId: "bot", name: id, prompt: "", cron: cron, timezone: "UTC", enabled: enabled,
                nextRunAt: nextRunAt, lastRunAt: nil, lastTaskId: nil, createdAt: "", updatedAt: "",
                oneTime: oneTime, state: state, runAt: runAt)
    }

    private func decodedBot() throws -> Bot {
        let json = """
        {
          "id": "bot", "sessionId": "session", "name": "Research", "role": "Research",
          "avatar": "sparkle", "color": "777777", "instructions": "", "status": "idle",
          "lastMessage": "", "unreadCount": 0, "archived": false, "createdAt": "2026-09-08T12:00:00Z"
        }
        """
        return try JSONDecoder().decode(Bot.self, from: Data(json.utf8))
    }

    private func encodedObject<T: Encodable>(_ value: T) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: APIJSON.encode(value)) as? [String: Any])
    }
}
