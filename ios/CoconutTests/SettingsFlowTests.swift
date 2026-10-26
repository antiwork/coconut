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

    // MARK: Connection banner

    func testLegacyPairingAsksToPairAgainWhileConnected() {
        let notice = ConnectionState.connected.notice(legacyPairing: true)
        XCTAssertEqual(notice?.offersPairing, true)
        XCTAssertEqual(notice?.message, "Pair this iPhone again with a new pairing link from your Hermes.")
    }

    func testLegacyPairingAsksToPairAgainWhileDisconnected() {
        XCTAssertEqual(ConnectionState.disconnected.notice(legacyPairing: true)?.offersPairing, true)
    }

    func testDisconnectedHasABannerWithoutPair() {
        let notice = ConnectionState.disconnected.notice(legacyPairing: false)
        XCTAssertEqual(notice?.message, ConnectionState.disconnected.bannerMessage)
        XCTAssertEqual(notice?.offersPairing, false)
        XCTAssertNotNil(ConnectionState.disconnected.bannerMessage)
    }

    func testRepairOffersPairAndAConnectedRelayPairingShowsNoBanner() {
        XCTAssertEqual(ConnectionState.needsRepair.notice(legacyPairing: false)?.offersPairing, true)
        XCTAssertEqual(ConnectionState.relayUnreachable.notice(legacyPairing: false)?.offersPairing, false)
        XCTAssertNil(ConnectionState.connected.notice(legacyPairing: false))
        XCTAssertNil(ConnectionState.preview.notice(legacyPairing: false))
    }

    func testEveryStateHasATitle() {
        let states: [ConnectionState] = [.unconfigured, .connecting, .connected, .disconnected, .macOffline,
                                         .relayUnreachable, .needsRepair, .preview]
        XCTAssertEqual(Set(states.map(\.title)).count, states.count)
    }

    // MARK: Pairing entry points

    func testEveryPairingEntryPointStartsOnThePairingPage() {
        let steps = OnboardingView.steps(start: .pair, setup: nil, existingServers: [], modelConfigured: true, askNotifications: false)
        XCTAssertEqual(steps, [.pair, .setup, .done])
    }

    func testAfterPairingASetupWithNothingToChooseSkipsItsPage() {
        let empty = HermesSetup(found: false, profiles: [], logins: [], signIns: [], model: nil)
        let steps = OnboardingView.steps(start: .pair, setup: empty, existingServers: [], modelConfigured: false, askNotifications: true)
        XCTAssertEqual(steps, [.pair, .model, .notifications, .done])
    }

    func testHermesSetupFromSettingsAlwaysShowsTheSetupPage() {
        let empty = HermesSetup(found: false, profiles: [], logins: [], signIns: [], model: nil)
        let steps = OnboardingView.steps(start: .setup, setup: empty, existingServers: [], modelConfigured: true, askNotifications: false)
        XCTAssertEqual(steps, [.setup, .done])
    }

    func testASetupWithChoicesKeepsItsPages() {
        let steps = OnboardingView.steps(start: .pair, setup: .sample, existingServers: [], modelConfigured: true, askNotifications: false)
        XCTAssertEqual(steps, [.pair, .setup, .bots, .logins, .mcp, .done])
    }

    func testServersThatCoconutHasAreNotAChoice() {
        var setup = HermesSetup(found: true, profiles: [], logins: [], signIns: [], model: nil)
        setup.mcpServers = [HermesMCPServer(id: "github", transport: "stdio", needsSetup: false)]
        let existing = [MCPServer(id: "github", url: "", transport: "stdio", hasToken: false, editable: false)]
        XCTAssertFalse(setup.offersChoices(existingServers: existing))
        XCTAssertTrue(setup.offersChoices(existingServers: []))
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
        XCTAssertNil(HermesSetup.sample.hermes)
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
