import XCTest
@testable import Coconut

final class ModelSettingsTests: XCTestCase {
    func testFallbackChoiceReplacesOnlyTheFirstModel() {
        let a = FallbackModel(model: "model-a", provider: "provider-a")
        let b = FallbackModel(model: "model-b", provider: "provider-b")
        let c = FallbackModel(model: "model-c", provider: "provider-c")
        let d = FallbackModel(model: "model-d", provider: "provider-d")
        func chosen(_ chain: [FallbackModel], _ choice: FallbackModel?) -> [FallbackModel] {
            FallbackSettings(models: chain).replacingFirst(with: choice)
        }

        XCTAssertEqual(chosen([], b), [b])
        XCTAssertEqual(chosen([a], b), [b])
        XCTAssertEqual(chosen([a, b, c], b), [b, c])
        XCTAssertEqual(chosen([a, b, c], d), [d, b, c])
        // Each other copy of the choice keeps its entry, and its endpoint, on the Mac.
        XCTAssertEqual(chosen([a, b, b], b), [b, b])
        XCTAssertEqual(chosen([a, b], nil), [b])
        XCTAssertEqual(chosen([b, b], nil), [b])
        XCTAssertEqual(chosen([], nil), [])
        let upper = FallbackModel(model: "MODEL-B", provider: "Provider-B")
        XCTAssertEqual(chosen([a, upper, c], b), [b, c])
    }

    func testLegacyCachedBotHasNoModelOverride() throws {
        let bot = try JSONDecoder().decode(Bot.self, from: Data(legacyBotJSON.utf8))

        XCTAssertNil(bot.modelOverride)
        XCTAssertNil(bot.effectiveModel)
        XCTAssertNil(bot.modelError)
    }

    func testLegacyCachedSnapshotKeepsHostModelWithoutCatalog() throws {
        let snapshot = try JSONDecoder().decode(StateSnapshot.self, from: Data(legacySnapshotJSON.utf8))

        XCTAssertNil(snapshot.modelCatalog)
        XCTAssertEqual(snapshot.model, "model-old")
        XCTAssertEqual(snapshot.provider, "provider-old")
        XCTAssertNil(snapshot.bots.first?.modelOverride)
        XCTAssertNil(snapshot.bots.first?.effectiveModel)
    }

    func testCatalogDecodesServerDefaultsAndExactEffortValues() throws {
        let catalog = try APIJSON.decoder().decode(ModelCatalog.self, from: Data(catalogJSON.utf8))

        XCTAssertEqual(catalog.defaults.model, "model-shared")
        XCTAssertEqual(catalog.defaults.provider, "provider-a")
        XCTAssertEqual(catalog.defaults.baseUrl, "https://models.example.test/v1")
        XCTAssertEqual(catalog.defaults.reasoningEffort, "adaptive-v2")
        XCTAssertNil(catalog.defaults.supported)
        XCTAssertNil(catalog.defaults.unavailableReason)
        XCTAssertEqual(catalog.modelSource, "hermes-catalog")
        XCTAssertEqual(catalog.defaultSource, "host-settings")
        XCTAssertNil(catalog.loading)
        XCTAssertEqual(catalog.models.map(\.id), ["model-shared", "model-shared"])
        XCTAssertEqual(catalog.models.map(\.provider), ["provider-a", "provider-b"])
        XCTAssertEqual(catalog.models[0].name, "Model A")
        XCTAssertEqual(catalog.models[0].baseUrl, "https://models.example.test/v1")
        XCTAssertEqual(catalog.models[0].reasoningEfforts, ["low", "high", "adaptive-v2"])
        XCTAssertEqual(catalog.models[0].defaultReasoningEffort, "high")
        XCTAssertNil(catalog.models[1].baseUrl)
        XCTAssertNil(catalog.models[1].defaultReasoningEffort)
        XCTAssertTrue(catalog.models[1].reasoningEfforts.isEmpty)
    }

    func testLoadingCatalogKeepsTheCurrentDefaultWithoutModelOptions() throws {
        let json = """
        {
          "default": {
            "model": "model-current", "provider": "provider-current", "reasoning_effort": "high"
          },
          "models": [], "loading": true
        }
        """

        let catalog = try APIJSON.decoder().decode(ModelCatalog.self, from: Data(json.utf8))

        XCTAssertEqual(catalog.loading, true)
        XCTAssertTrue(catalog.models.isEmpty)
        XCTAssertEqual(catalog.defaults.model, "model-current")
        XCTAssertEqual(catalog.defaults.provider, "provider-current")
        XCTAssertEqual(catalog.defaults.reasoningEffort, "high")
    }

    func testUnsupportedNativeDefaultKeepsItsActualSettingsAndReason() throws {
        let json = """
        {
          "default": {
            "model": "model-removed", "provider": "provider-current", "reasoning_effort": "high",
            "supported": false, "unavailable_reason": "The provider no longer offers this model."
          },
          "models": [
            {"id": "model-listed", "name": "Listed model", "provider": "provider-current", "reasoning_efforts": []}
          ]
        }
        """

        let catalog = try APIJSON.decoder().decode(ModelCatalog.self, from: Data(json.utf8))

        XCTAssertFalse(catalog.models.isEmpty)
        XCTAssertEqual(catalog.defaults.model, "model-removed")
        XCTAssertEqual(catalog.defaults.provider, "provider-current")
        XCTAssertEqual(catalog.defaults.reasoningEffort, "high")
        XCTAssertEqual(catalog.defaults.supported, false)
        XCTAssertEqual(catalog.defaults.unavailableReason, "The provider no longer offers this model.")
        XCTAssertFalse(catalog.contains(catalog.defaults.selection))
    }

    func testAPIBotKeepsOverrideAndEffectiveModelSeparate() throws {
        let json = """
        {
          "id": "bot", "session_id": "session", "name": "Research", "role": "Research",
          "avatar": "sparkle", "color": "777777", "instructions": "", "status": "idle",
          "last_message": "", "unread_count": 0, "archived": false,
          "created_at": "2026-09-08T12:00:00Z",
          "model_override": {"model": "model-shared", "provider": "provider-b", "reasoning_effort": null},
          "effective_model": {"model": "model-shared", "provider": "provider-b", "reasoning_effort": "balanced-v3"}
        }
        """

        let bot = try APIJSON.decoder().decode(Bot.self, from: Data(json.utf8))

        XCTAssertEqual(bot.modelOverride?.model, "model-shared")
        XCTAssertEqual(bot.modelOverride?.provider, "provider-b")
        XCTAssertNil(bot.modelOverride?.reasoningEffort)
        XCTAssertEqual(bot.effectiveModel?.reasoningEffort, "balanced-v3")
    }

    func testBotModelErrorDoesNotCreateAnOverrideOrEffectiveModel() throws {
        let json = """
        {
          "id": "bot", "session_id": "session", "name": "Research", "role": "Research",
          "avatar": "sparkle", "color": "777777", "instructions": "", "status": "idle",
          "last_message": "", "unread_count": 0, "archived": false,
          "created_at": "2026-09-08T12:00:00Z", "effective_model": null,
          "model_error": "The configured model is unavailable."
        }
        """

        let bot = try APIJSON.decoder().decode(Bot.self, from: Data(json.utf8))

        XCTAssertEqual(bot.modelError, "The configured model is unavailable.")
        XCTAssertNil(bot.modelOverride)
        XCTAssertNil(bot.effectiveModel)
    }

    func testAPISnapshotIncludesModelCatalog() throws {
        let json = """
        {
          "bots": [], "tasks": [], "messages": [], "decisions": [], "artifacts": [], "policies": [],
          "cursor": 4, "server_time": "2026-09-08T12:00:00Z", "provider": "provider-a",
          "model": "model-shared", "model_configured": true, "model_catalog": \(catalogJSON)
        }
        """

        let snapshot = try APIJSON.decoder().decode(StateSnapshot.self, from: Data(json.utf8))

        XCTAssertEqual(snapshot.modelCatalog?.defaults.reasoningEffort, "adaptive-v2")
        XCTAssertEqual(snapshot.modelCatalog?.models.count, 2)
    }

    func testSameModelIDKeepsDistinctProviderSelections() throws {
        let catalog = try decodedCatalog()
        let first = ModelOverride(model: "model-shared", provider: "provider-a", reasoningEffort: nil)
        let second = ModelOverride(model: "model-shared", provider: "provider-b", reasoningEffort: nil)

        XCTAssertNotEqual(catalog.models[0].selectionKey, catalog.models[1].selectionKey)
        XCTAssertNotEqual(first.selectionKey, second.selectionKey)
        XCTAssertEqual(first.selectionKey, catalog.models[0].selectionKey)
        XCTAssertEqual(second.selectionKey, catalog.models[1].selectionKey)
        XCTAssertEqual(catalog.entry(for: first)?.name, "Model A")
        XCTAssertEqual(catalog.entry(for: second)?.name, "Model B")
    }

    func testCatalogRejectsUnknownModelProviderAndEffort() throws {
        let catalog = try decodedCatalog()
        let unsupported = [
            ModelOverride(model: "model-missing", provider: "provider-a", reasoningEffort: nil),
            ModelOverride(model: "model-shared", provider: "provider-missing", reasoningEffort: nil),
            ModelOverride(model: "model-shared", provider: "provider-a", reasoningEffort: "unsupported"),
            ModelOverride(model: "model-shared", provider: "provider-a", reasoningEffort: "HIGH"),
            ModelOverride(model: "model-shared", provider: "provider-b", reasoningEffort: "high")
        ]

        for selection in unsupported {
            XCTAssertFalse(catalog.contains(selection), "Accepted unsupported selection: \(selection)")
        }
    }

    func testCatalogAcceptsModelDefaultAndOnlyListedEfforts() throws {
        let catalog = try decodedCatalog()
        let firstDefault = ModelOverride(model: "model-shared", provider: "provider-a", reasoningEffort: nil)
        let secondDefault = ModelOverride(model: "model-shared", provider: "provider-b", reasoningEffort: nil)

        XCTAssertTrue(catalog.contains(firstDefault))
        XCTAssertTrue(catalog.contains(secondDefault))
        for effort in ["low", "high", "adaptive-v2"] {
            XCTAssertTrue(catalog.contains(ModelOverride(
                model: "model-shared", provider: "provider-a", reasoningEffort: effort
            )))
        }
    }

    func testResolvedEffortUsesTheSelectedProvidersDefault() throws {
        let catalog = try decodedCatalog()
        let first = ModelOverride(model: "model-shared", provider: "provider-a", reasoningEffort: nil)
        let second = ModelOverride(model: "model-shared", provider: "provider-b", reasoningEffort: nil)

        XCTAssertEqual(catalog.resolved(first).reasoningEffort, "high")
        XCTAssertNil(catalog.resolved(second).reasoningEffort)
        XCTAssertEqual(ModelPresentation.effortName(catalog.resolved(second).reasoningEffort), "Auto")
        XCTAssertNil(first.reasoningEffort, "Resolving the display must preserve the default choice in the draft.")
    }

    func testExplicitEffortOverridesTheModelDefault() throws {
        let catalog = try decodedCatalog()
        let selection = ModelOverride(model: "model-shared", provider: "provider-a", reasoningEffort: "low")

        XCTAssertEqual(catalog.resolved(selection), selection)
    }

    func testReturningToHostDefaultEncodesExplicitNullOverride() throws {
        let json = try encodedObject(BotModelRequest(modelOverride: nil))

        XCTAssertEqual(Set(json.keys), ["model_override"])
        XCTAssertTrue(json["model_override"] is NSNull)
    }

    func testUsingModelDefaultEncodesExplicitNullEffort() throws {
        let selection = ModelOverride(model: "model-shared", provider: "provider-b", reasoningEffort: nil)
        let request = try encodedObject(BotModelRequest(modelOverride: selection))
        let override = try XCTUnwrap(request["model_override"] as? [String: Any])

        XCTAssertEqual(Set(request.keys), ["model_override"])
        XCTAssertEqual(Set(override.keys), ["model", "provider", "reasoning_effort"])
        XCTAssertEqual(override["model"] as? String, "model-shared")
        XCTAssertEqual(override["provider"] as? String, "provider-b")
        XCTAssertTrue(override["reasoning_effort"] is NSNull)
    }

    func testOverrideRequestPreservesExactEffort() throws {
        let selection = ModelOverride(model: "model-shared", provider: "provider-a", reasoningEffort: "adaptive-v2")
        let request = try encodedObject(BotModelRequest(modelOverride: selection))
        let override = try XCTUnwrap(request["model_override"] as? [String: Any])

        XCTAssertEqual(override["model"] as? String, "model-shared")
        XCTAssertEqual(override["provider"] as? String, "provider-a")
        XCTAssertEqual(override["reasoning_effort"] as? String, "adaptive-v2")
    }

    func testAppearancePatchSendsOnlyChangedAvatar() throws {
        let original = try botWithNativeSettings()
        var updated = original
        updated.avatar = "circle"

        let patch = BotPatch(updated: updated, original: original)
        let json = try encodedObject(patch)

        XCTAssertFalse(patch.isEmpty)
        XCTAssertEqual(Set(json.keys), ["avatar"])
        XCTAssertEqual(json["avatar"] as? String, "circle")
    }

    func testNamePatchExcludesUnchangedInstructionsMemoryAndModel() throws {
        let original = try botWithNativeSettings()
        var updated = original
        updated.name = "Reviewer"

        let json = try encodedObject(BotPatch(updated: updated, original: original))

        XCTAssertEqual(Set(json.keys), ["name"])
        XCTAssertEqual(json["name"] as? String, "Reviewer")
    }

    func testApplyingAppearanceAndNamePatchPreservesNewerNativeSettings() throws {
        let original = try botWithNativeSettings()
        var updated = original
        updated.name = "Reviewer"
        updated.avatar = "circle"
        let patch = BotPatch(updated: updated, original: original)
        var current = original
        current.instructions = "Instructions changed in Hermes after the editor opened."
        current.memory = "New notes from the Mac."
        current.modelOverride = ModelOverride(model: "model-new", provider: "provider-b", reasoningEffort: "high")
        current.effectiveModel = current.modelOverride
        current.status = "working"
        current.lastMessage = "I am checking the files."
        var expected = current
        expected.name = "Reviewer"
        expected.avatar = "circle"

        patch.apply(to: &current)

        XCTAssertEqual(current, expected, "The patch must preserve all fields outside the editor's changes.")
    }

    func testNotificationChangesDoNotWriteNativeMemory() throws {
        let original = try botWithNativeSettings()
        var updated = original
        updated.notifications = false

        let patch = BotPatch(updated: updated, original: original)
        let json = try encodedObject(patch)

        XCTAssertFalse(patch.isEmpty)
        XCTAssertEqual(Set(json.keys), ["notifications"])
        XCTAssertEqual(json["notifications"] as? Bool, false)
    }

    func testUnchangedBotProducesAnEmptyPatch() throws {
        let original = try botWithNativeSettings()
        let patch = BotPatch(updated: original, original: original)

        XCTAssertTrue(patch.isEmpty)
        XCTAssertTrue(try encodedObject(patch).isEmpty)
    }

    func testCachedBotTextIsVisibleBeforeRefresh() {
        var draft = BotTextDraft()

        draft.loadCached("Cached instructions")

        XCTAssertTrue(draft.isLoaded)
        XCTAssertEqual(draft.text, "Cached instructions")
        XCTAssertEqual(draft.baseline, "Cached instructions")
    }

    func testUnchangedCachedBotTextAcceptsFreshText() {
        var draft = BotTextDraft()
        draft.loadCached("Cached instructions")

        draft.applyFresh("Current Hermes instructions", requestedBaseline: "Cached instructions")

        XCTAssertEqual(draft.text, "Current Hermes instructions")
        XCTAssertEqual(draft.baseline, "Current Hermes instructions")
        XCTAssertTrue(draft.isLoaded)
    }

    func testTypedBotTextSurvivesARefresh() {
        var draft = BotTextDraft()
        draft.loadCached("Cached instructions")
        draft.text = "My unfinished instructions"

        draft.applyFresh("Current Hermes instructions", requestedBaseline: "Cached instructions")

        XCTAssertEqual(draft.text, "My unfinished instructions")
        XCTAssertEqual(draft.baseline, "Cached instructions")
    }

    func testTypingDuringBotTextSaveKeepsTheNewerDraft() {
        var draft = BotTextDraft()
        draft.loadCached("Cached instructions")
        draft.text = "Submitted instructions"
        let submitted = draft.text
        draft.text = "Submitted instructions with another edit"

        draft.markSaved(submitted)

        XCTAssertEqual(draft.text, "Submitted instructions with another edit")
        XCTAssertEqual(draft.baseline, "Submitted instructions")
    }

    func testRefreshRequestedBeforeSaveCannotOverwriteSavedBotText() {
        var draft = BotTextDraft()
        draft.loadCached("Cached instructions")
        let requestedBaseline = draft.baseline
        draft.text = "Saved instructions"
        draft.markSaved("Saved instructions")

        draft.applyFresh("Instructions from before the save", requestedBaseline: requestedBaseline)

        XCTAssertEqual(draft.text, "Saved instructions")
        XCTAssertEqual(draft.baseline, "Saved instructions")
    }

    private func botWithNativeSettings() throws -> Bot {
        var bot = try JSONDecoder().decode(Bot.self, from: Data(legacyBotJSON.utf8))
        bot.instructions = "Use the instructions from Hermes."
        bot.memory = "Notes from the Mac."
        bot.modelOverride = ModelOverride(model: "model-shared", provider: "provider-a", reasoningEffort: "low")
        bot.effectiveModel = bot.modelOverride
        return bot
    }

    private func decodedCatalog() throws -> ModelCatalog {
        try APIJSON.decoder().decode(ModelCatalog.self, from: Data(catalogJSON.utf8))
    }

    private func encodedObject<T: Encodable>(_ value: T) throws -> [String: Any] {
        let data = try APIJSON.encode(value)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    private var legacyBotJSON: String {
        """
        {
          "id": "bot", "sessionId": "session", "name": "Research", "role": "Research",
          "avatar": "sparkle", "color": "777777", "instructions": "", "status": "idle",
          "lastMessage": "", "unreadCount": 0, "archived": false, "createdAt": "2026-09-08T12:00:00Z"
        }
        """
    }

    private var legacySnapshotJSON: String {
        """
        {
          "bots": [\(legacyBotJSON)], "tasks": [], "messages": [], "decisions": [], "artifacts": [],
          "policies": [], "cursor": 3, "serverTime": "2026-09-08T12:00:00Z", "provider": "provider-old",
          "model": "model-old", "modelConfigured": true
        }
        """
    }

    private var catalogJSON: String {
        """
        {
          "default": {
            "model": "model-shared", "provider": "provider-a",
            "base_url": "https://models.example.test/v1", "reasoning_effort": "adaptive-v2"
          },
          "models": [
            {
              "id": "model-shared", "name": "Model A", "provider": "provider-a",
              "base_url": "https://models.example.test/v1", "reasoning_efforts": ["low", "high", "adaptive-v2"],
              "default_reasoning_effort": "high"
            },
            {
              "id": "model-shared", "name": "Model B", "provider": "provider-b",
              "base_url": null, "reasoning_efforts": []
            }
          ],
          "model_source": "hermes-catalog", "default_source": "host-settings"
        }
        """
    }
}
