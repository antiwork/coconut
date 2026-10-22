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
        XCTAssertEqual(String(decoding: body, as: UTF8.self), #"{"logins":true,"mcp_servers":false,"profile_ids":["default"]}"#)
        let servers = try APIJSON.encode(HermesImportRequest(profileIds: [], logins: false, mcpServers: true))
        XCTAssertEqual(String(decoding: servers, as: UTF8.self), #"{"logins":false,"mcp_servers":true,"profile_ids":[]}"#)
    }

    func testOnlyServersThatCoconutDoesNotHaveAreOffered() throws {
        let json = """
        {"found": true, "profiles": [], "logins": [], "sign_ins": [], "model": null,
         "mcp_servers": [{"id": "github", "transport": "stdio", "needs_setup": false},
                         {"id": "notion", "transport": "http", "needs_setup": true}]}
        """
        let setup = try APIJSON.decoder().decode(HermesSetup.self, from: Data(json.utf8))
        let existing = [MCPServer(id: "github", url: "", transport: "stdio", hasToken: false, editable: false)]
        XCTAssertEqual(setup.newMCPServers(existing: existing).map(\.id), ["notion"])
        XCTAssertEqual(setup.newMCPServers(existing: []).last?.needsSetup, true)
        let result = try APIJSON.decoder().decode(HermesImportResult.self, from: Data(
            #"{"bots": [], "logins": [], "mcp_servers": ["notion"], "mcp_needs_setup": ["notion"]}"#.utf8))
        XCTAssertEqual(result.mcpNeedsSetup, ["notion"])
    }

    func testAMacWithoutServerReportingStillDecodes() throws {
        let json = #"{"found": false, "profiles": [], "logins": [], "sign_ins": [], "model": null}"#
        let setup = try APIJSON.decoder().decode(HermesSetup.self, from: Data(json.utf8))
        XCTAssertEqual(setup.newMCPServers(existing: []), [])
    }

    func testTheSetupModelIsOfferedOnlyWhenTheCatalogHasItsRoute() {
        let catalog = ModelCatalog(defaults: ModelDefaults(model: "", provider: ""), models: [
            ModelCatalogEntry(id: "claude-opus-5-5", name: "Claude Opus 5.5", provider: "anthropic", reasoningEfforts: []),
            ModelCatalogEntry(id: "gpt-6", name: "GPT-6", provider: "openai", reasoningEfforts: []),
            ModelCatalogEntry(id: "gpt-6", name: "GPT-6", provider: "openrouter", reasoningEfforts: []),
        ], modelSource: nil, defaultSource: nil)
        var setup = HermesSetup.sample
        XCTAssertEqual(setup.modelChoice(in: catalog), ModelOverride(model: "claude-opus-5-5", provider: "anthropic", reasoningEffort: nil))
        XCTAssertNil(setup.modelChoice(in: nil))
        setup.model = HermesModelChoice(model: "claude-opus-5-5", provider: "bedrock")
        XCTAssertNil(setup.modelChoice(in: catalog))
        // Without a provider, the model must name exactly one route.
        setup.model = HermesModelChoice(model: "claude-opus-5-5", provider: nil)
        XCTAssertEqual(setup.modelChoice(in: catalog)?.provider, "anthropic")
        setup.model = HermesModelChoice(model: "gpt-6", provider: nil)
        XCTAssertNil(setup.modelChoice(in: catalog))
        setup.model = nil
        XCTAssertNil(setup.modelChoice(in: catalog))
    }
}
