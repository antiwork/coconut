import XCTest
@testable import Coconut

/// The models that the model menu shows first, the search of the catalog, and the models chosen lately.
final class ModelMenuTests: XCTestCase {
    private func model(_ id: String, _ name: String, provider: String = "anthropic") -> ModelCatalogEntry {
        ModelCatalogEntry(id: id, name: name, provider: provider, baseUrl: nil, reasoningEfforts: [])
    }

    private var catalog: ModelCatalog {
        ModelCatalog(defaults: ModelDefaults(model: "opus", provider: "anthropic", baseUrl: nil, reasoningEffort: nil),
                     models: [model("haiku", "Claude Haiku"), model("sonnet", "Claude Sonnet"), model("opus", "Claude Opus"),
                              model("gpt", "GPT-5", provider: "openai"), model("mini", "GPT-5 mini", provider: "openai"),
                              model("gemini", "Gemini Pro", provider: "google"), model("flash", "Gemini Flash", provider: "google"),
                              model("opus", "Claude Opus", provider: "openrouter")],
                     modelSource: nil, defaultSource: nil)
    }

    private func key(_ provider: String, _ id: String) -> String { ModelOverride(model: id, provider: provider, reasoningEffort: nil).selectionKey }

    func testTheMenuShowsTheModelInUseThenTheDefaultThenRecentThenTheModelsOfOtherAgents() {
        let list = ModelShortlist.make(catalog: catalog,
                                       current: ModelOverride(model: "gpt", provider: "openai", reasoningEffort: "high"),
                                       recent: [key("google", "flash"), key("openai", "gpt"), key("gone", "model")],
                                       inUse: [ModelOverride(model: "haiku", provider: "anthropic", reasoningEffort: nil),
                                               ModelOverride(model: "opus", provider: "anthropic", reasoningEffort: nil)])
        XCTAssertEqual(list.map(\.selectionKey), [key("openai", "gpt"), key("anthropic", "opus"), key("google", "flash"), key("anthropic", "haiku")])
    }

    func testTheMenuHasABoundAndAShortCatalogShowsWhole() {
        let recent = catalog.models.map(\.selectionKey)
        XCTAssertEqual(ModelShortlist.make(catalog: catalog, current: nil, recent: recent, inUse: []).count, ModelShortlist.limit)
        var short = catalog
        short.models = Array(short.models.prefix(ModelShortlist.limit))
        XCTAssertEqual(ModelShortlist.make(catalog: short, current: nil, recent: [], inUse: []), short.models)
    }

    func testTheSearchReadsTheNameTheIDAndTheProvider() {
        let models = catalog.models
        XCTAssertEqual(ModelShortlist.search(models, query: "  ").count, models.count)
        XCTAssertEqual(ModelShortlist.search(models, query: "gem").map(\.id), ["gemini", "flash"])
        XCTAssertEqual(ModelShortlist.search(models, query: "openai mini").map(\.id), ["mini"])
        XCTAssertEqual(ModelShortlist.search(models, query: "OPUS open").map(\.provider), ["openrouter"])
        // A name that starts with the query comes before a name that only has it.
        XCTAssertEqual(ModelShortlist.search(models, query: "g").prefix(4).map(\.id), ["gpt", "mini", "gemini", "flash"])
        XCTAssertEqual(ModelShortlist.search(models, query: "llama"), [])
        XCTAssertEqual(ModelShortlist.providers(models), ["anthropic", "openai", "google", "openrouter"])
    }

    func testAChosenModelMovesToTheFrontOfTheRecentModels() {
        var keys: [String] = []
        for id in ["a", "b", "c", "b", "d", "e", "f"] { keys = RecentModels.adding(id, to: keys) }
        XCTAssertEqual(keys, ["f", "e", "d", "b", "c"])
        let defaults = UserDefaults(suiteName: "model-menu-tests")!
        defaults.removePersistentDomain(forName: "model-menu-tests")
        RecentModels.record("x", in: defaults)
        RecentModels.record("y", in: defaults)
        XCTAssertEqual(RecentModels.read(defaults), ["y", "x"])
    }

    @MainActor
    func testAnAgentWithoutAModelOfItsOwnUsesTheDefault() throws {
        let store = AppStore(demo: true)
        let bot = try XCTUnwrap(store.bots.first)
        let choice = ModelChoice(store: store, botId: bot.id)
        XCTAssertTrue(choice.usesDefault)
        XCTAssertEqual(choice.selection, store.modelCatalog?.defaults.selection)
        let model = try XCTUnwrap(store.modelCatalog?.models.last)
        XCTAssertFalse(choice.isCurrent(model))
        // The default of the agents has no "default" line of its own.
        XCTAssertFalse(ModelChoice(store: store, botId: nil).usesDefault)
        XCTAssertEqual(ModelChoice(store: store, botId: nil).name, "Preview Standard")
    }
}
