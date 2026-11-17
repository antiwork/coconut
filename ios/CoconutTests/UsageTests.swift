import XCTest
@testable import Coconut

/// The usage of the agents: the answer of the Mac, the sums, the bars by day and by week, and the words of a number.
final class UsageTests: XCTestCase {
    private var calendar: Calendar {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "UTC")!
        calendar.firstWeekday = 2
        return calendar
    }

    private func day(_ day: String, turns: Int = 0, input: Int = 0, output: Int = 0, cached: Int = 0, cost: Double = 0,
                     seconds: Int = 0, tools: Int = 0) -> UsageDay {
        UsageDay(day: day, turns: turns, modelCalls: turns * 2, toolCalls: tools, workingSeconds: seconds, inputTokens: input,
                 outputTokens: output, cacheReadTokens: cached, costUsd: cost)
    }

    func testTheAnswerOfTheMacDecodes() throws {
        let json = #"""
            {"days": 2, "today": "2026-11-16", "agents": {"00000000-0000-4000-8000-000000000001": {
              "days": [{"day": "2026-11-15", "turns": 0, "model_calls": 0, "tool_calls": 0, "working_seconds": 0, "input_tokens": 0,
                        "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0, "cost_usd": 0},
                       {"day": "2026-11-16", "turns": 3, "model_calls": 40, "tool_calls": 31, "working_seconds": 912, "input_tokens": 1200,
                        "output_tokens": 5400, "cache_read_tokens": 880000, "cache_write_tokens": 21000, "reasoning_tokens": 0, "cost_usd": 1.2345}],
              "models": [{"model": "claude-opus-5-5", "provider": "anthropic", "input_tokens": 1200, "output_tokens": 5400,
                          "cache_read_tokens": 880000, "cache_write_tokens": 21000, "reasoning_tokens": 0, "cost_usd": 1.2345}],
              "cost_known": true, "cost_estimated": true}}}
            """#
        let report = try APIJSON.decoder().decode(UsageReport.self, from: Data(json.utf8))
        let usage = try XCTUnwrap(report.agents["00000000-0000-4000-8000-000000000001"])
        XCTAssertEqual(usage.days.map(\.turns), [0, 3])
        XCTAssertEqual(usage.days[1].tokensIn, 902_200)
        XCTAssertEqual(usage.days[1].tokens, 907_600)
        XCTAssertEqual(usage.models.first?.costUsd, 1.2345)
        XCTAssertTrue(usage.costKnown && usage.costEstimated)
        XCTAssertEqual(UsageMath.cached(usage.days[1]), "98% cached")
        XCTAssertNil(UsageMath.cached(usage.days[0]))
    }

    func testTheAgentsAddUpDayByDayAndModelByModel() {
        let first = AgentUsage(days: [day("2026-11-15", turns: 2, input: 10, cost: 1), day("2026-11-16", turns: 1, output: 5)],
                               models: [UsageModel(model: "opus", provider: "anthropic", inputTokens: 10, costUsd: 1)], costKnown: true)
        let second = AgentUsage(days: [day("2026-11-16", turns: 4, input: 7, cost: 0.5, seconds: 60)],
                                models: [UsageModel(model: "opus", provider: "anthropic", outputTokens: 3, costUsd: 0.5),
                                         UsageModel(model: "haiku", provider: "anthropic", inputTokens: 900)])
        let days = UsageMath.combined([first, second])
        XCTAssertEqual(days.map(\.day), ["2026-11-15", "2026-11-16"])
        XCTAssertEqual(days.map(\.turns), [2, 5])
        XCTAssertEqual(UsageMath.sum(days).costUsd, 1.5)
        XCTAssertEqual(UsageMath.sum(days).workingSeconds, 60)
        // One row for a model of two agents, the largest cost first.
        let models = UsageMath.models([first, second])
        XCTAssertEqual(models.map(\.model), ["opus", "haiku"])
        XCTAssertEqual(models[0].tokens, 13)
        XCTAssertEqual(models[0].costUsd, 1.5)
    }

    func testARangeHasEachOfItsDaysAndTheLongRangeHasWeeks() {
        let days = [day("2026-11-02", turns: 9), day("2026-11-09", turns: 1), day("2026-11-10", turns: 2), day("2026-11-16", turns: 4)]
        let week = UsageMath.buckets(days, range: .week, today: "2026-11-16", calendar: calendar)
        XCTAssertEqual(week.count, 7)
        XCTAssertEqual(week.map(\.total.turns), [2, 0, 0, 0, 0, 0, 4])
        XCTAssertEqual(week.first?.total.day, "2026-11-10")
        // The 16th is a Monday, so it starts a week of its own.
        let quarter = UsageMath.buckets(days, range: .quarter, today: "2026-11-16", calendar: calendar)
        XCTAssertEqual(quarter.suffix(3).map(\.total.turns), [9, 3, 4])
        XCTAssertEqual(quarter.suffix(3).map(\.total.day), ["2026-11-02", "2026-11-09", "2026-11-16"])
        XCTAssertEqual(UsageMath.sum(quarter.map(\.total)).turns, 16)
        XCTAssertEqual(UsageMath.buckets(days, range: .week, today: "soon", calendar: calendar), [])
    }

    func testASummaryKnowsItsCostOnlyWhenHermesHasAPrice() {
        let priced = AgentUsage(days: [day("2026-11-16", turns: 1, cost: 12.4)], costKnown: true, costEstimated: true)
        let free = AgentUsage(days: [day("2026-11-16", turns: 2)])
        let summary = UsageSummary(agents: [priced, free], range: .week, today: "2026-11-16", calendar: calendar)
        XCTAssertEqual(summary.total.turns, 3)
        XCTAssertEqual([summary.cost, summary.costDetail], ["$12.40", "Estimate at list prices"])
        let unknown = UsageSummary(agents: [free], range: .week, today: "2026-11-16", calendar: calendar)
        XCTAssertEqual([unknown.cost, unknown.costDetail], ["\u{2014}", "No price known"])
    }

    func testTheWordsOfANumber() {
        XCTAssertEqual([0, 950, 1_000, 12_440, 456_179, 3_200_000, 226_587_623, 1_100_000_000].map(UsageMath.count),
                       ["0", "950", "1.0k", "12.4k", "456k", "3.2M", "227M", "1.1B"])
        XCTAssertEqual([0, 0.004, 0.42, 12.4, 1284.2].map(UsageMath.cost), ["$0.00", "<$0.01", "$0.42", "$12.40", "$1,284"])
        XCTAssertEqual([0, 45, 60, 725, 3_600, 11_100].map(UsageMath.duration), ["0s", "45s", "1m", "12m", "1h 00m", "3h 05m"])
        XCTAssertEqual(UsageMetric.time.text(3_660), "1h 01m")
        XCTAssertEqual(UsageMetric.cost.value(day("x", cost: 2)), 2)
    }

    @MainActor
    func testThePreviewHasUsageForEachAgent() async {
        let store = AppStore(demo: true)
        await store.loadUsage()
        XCTAssertEqual(store.usageOf(nil).count, store.bots.count)
        XCTAssertEqual(store.usageOf(store.bots[0].id).first?.days.count, UsageRange.quarter.days)
        XCTAssertEqual(store.usageOf("gone"), [])
    }
}
