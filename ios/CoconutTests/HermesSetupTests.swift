import XCTest
@testable import Coconut

final class HermesSetupTests: XCTestCase {
    func testSetupDecodesTheMacResponseAndOffersOnlyNewProfiles() throws {
        let json = """
        {"found": true, "model": {"model": "claude-opus-5-5", "provider": "anthropic"},
         "logins": [{"id": "ANTHROPIC_API_KEY", "label": "Anthropic API key"}],
         "sign_ins": [{"id": "nous", "label": "Nous"}],
         "profiles": [
           {"id": "default", "name": "Hermes", "soul": true, "memories": 2, "skills": 1, "bot_id": null},
           {"id": "profile:writer", "name": "Writer", "soul": false, "memories": 0, "skills": 0, "bot_id": "bot-1"}]}
        """
        let setup = try APIJSON.decoder().decode(HermesSetup.self, from: Data(json.utf8))
        XCTAssertEqual(setup.importable.map(\.id), ["default"])
        XCTAssertEqual(setup.signIns.map(\.label), ["Nous"])
        XCTAssertEqual(setup.profiles[0].contents, "Personality · 2 memory files · 1 skill")
        XCTAssertEqual(setup.profiles[1].contents, "Empty profile")
    }

    func testImportRequestUsesTheMacKeys() throws {
        let body = try APIJSON.encode(HermesImportRequest(profileIds: ["default"], logins: true))
        XCTAssertEqual(String(decoding: body, as: UTF8.self), #"{"logins":true,"profile_ids":["default"]}"#)
    }
}
