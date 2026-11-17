import XCTest
@testable import Coconut

@MainActor
final class BusinessTests: XCTestCase {
    func testHomeUsesPublishedNumbersRatherThanRecomputingComparisons() throws {
        let model = try homeFixture()
        XCTAssertEqual(model.attention.map(\.id), ["support", "issues", "high-priority"])
        XCTAssertEqual(model.attention[0].title, "Support · 3 past SLA")
        XCTAssertEqual(model.attention[0].detail, "Synthetic payout timing question · 29.5h")
        XCTAssertEqual(model.attention[2].detail, "Synthetic checkout regression")
        XCTAssertEqual(model.informational.map(\.id), ["risk", "prs"])
        XCTAssertEqual(model.volume.count, 30)
        XCTAssertEqual(model.previousVolume.count, 30)
        // Deliberately unlike the ratio of gmv_30d / gmv_prev_30d in the fixture.
        XCTAssertEqual(model.change, 7.3)
        XCTAssertEqual(model.changeLabel, "vs prior 30 days")
        XCTAssertEqual(model.latest, "Nov 15 · $268.4K")
        XCTAssertEqual(model.weekdayChange, 6.5)
        XCTAssertEqual(model.moneyRows.map(\.value), [12000000, 3200000, 870000])
        XCTAssertEqual(model.moneyRows.map(\.formattedValue), ["$12.0M", "$3.2M", "$870.0K"])
        XCTAssertEqual(model.moneyRows.map(\.months.count), [12, 12, 12])
        XCTAssertEqual(model.moneyRows[1].subtitle, "26.7% margin")
        XCTAssertEqual(model.moneyRows[2].subtitle, "$1.93M non-cash write-off")
        XCTAssertEqual(model.moneyRows[2].months[7].value, -1700000)
        XCTAssertEqual(model.closedThrough, "2026-10")
    }

    func testHomeLegacySnapshotDoesNotInventMoneyOrDeltas() throws {
        let model = try homeFixture("BusinessOverviewLegacy")
        XCTAssertNil(model.change)
        XCTAssertNil(model.changeLabel)
        XCTAssertNil(model.weekdayChange)
        XCTAssertFalse(model.volume.isEmpty)
        XCTAssertNil(model.gmvNotice)
        XCTAssertEqual(model.moneyRows.count, 3)
        XCTAssertTrue(model.moneyRows.allSatisfy { $0.value == nil && $0.subtitle == nil && $0.months.isEmpty })
        XCTAssertEqual(model.moneyRows.map(\.formattedValue), Array(repeating: "Not published", count: 3))
    }

    func testHomeFailedGMVSourceHidesEvenPopulatedChartAndComparison() throws {
        let model = try homeFixture { $0["sections"] = ["gmv": ["ok": false, "as_of": NSNull()]] }
        XCTAssertEqual(model.gmvNotice, "Unavailable")
        XCTAssertTrue(model.volume.isEmpty)
        XCTAssertTrue(model.previousVolume.isEmpty)
        XCTAssertNil(model.latest)
        XCTAssertNil(model.change)
        XCTAssertNil(model.weekdayChange)
        XCTAssertEqual(model.moneyRows.count, 3)
    }

    func testHomeZeroBreachesAreAbsentButFailuresAreNeverAllClear() throws {
        let clear = try homeFixture { data in
            data["support"] = ["gumroad_past_sla": 0]
            data["issues"] = ["past_sla": 0]
            data["high_priority"] = ["count": 0]
        }
        XCTAssertTrue(clear.attention.isEmpty)
        let failed = try homeFixture { data in
            data["support"] = ["gumroad_past_sla": 0]
            data["issues"] = ["past_sla": 0]
            data["high_priority"] = ["count": 0]
            data["sections"] = ["support": ["ok": false]]
        }
        XCTAssertEqual(failed.attention.count, 1)
        XCTAssertEqual(failed.attention.first?.title, "Support · Unavailable")
    }

    func testHomeMissingCountsAndFailedLedgerStayUnknown() throws {
        let model = try homeFixture { data in
            data["support"] = [:]
            data["sections"] = ["ledger": ["ok": false]]
        }
        XCTAssertEqual(model.attention.first?.title, "Support · Unknown")
        XCTAssertEqual(model.sourceNotice("ledger"), "Unavailable")
        XCTAssertTrue(model.moneyRows.isEmpty)
        XCTAssertNil(model.closedThrough)
    }

    func testHomeFreshnessThresholdAndStaleSnapshotRetention() throws {
        var model = try homeFixture()
        model.now = try XCTUnwrap("2026-11-16T12:06:00Z".apiDate)
        XCTAssertEqual(model.freshness, "Updated 6m ago")
        XCTAssertFalse(model.freshnessWarning)
        model.now = try XCTUnwrap("2026-11-16T12:46:00Z".apiDate)
        XCTAssertTrue(model.freshnessWarning)
        XCTAssertFalse(model.currentStatusUnknown)
        model.now = try XCTUnwrap("2026-11-16T13:01:00Z".apiDate)
        XCTAssertEqual(model.freshness, "Updated 61m ago · Stale snapshot")
        XCTAssertTrue(model.currentStatusUnknown)
        XCTAssertTrue(model.attention.allSatisfy(\.unavailable))
        XCTAssertEqual(model.sourceNotice("gmv"), "Stale source")
        XCTAssertEqual(model.attention.first?.title, "Support · Stale source")
        // With no per-source timestamps, keep the old snapshot labelled, not silently discarded.
        var legacy = try homeFixture("BusinessOverviewLegacy")
        legacy.now = model.now
        XCTAssertTrue(legacy.currentStatusUnknown)
        XCTAssertEqual(legacy.volume.count, 30)
    }

    func testHomeUnknownTimestampAndRefreshFailureNeverSayAllClear() throws {
        let full = try homeFixture()
        let report = BusinessReport(key: "overview", data: full.data, source: full.report.source, generatedAt: nil, ledgerUrl: "")
        let unknown = BusinessHomePresentation(report: report, now: full.now)
        XCTAssertEqual(unknown.freshness, "Freshness unknown")
        XCTAssertTrue(unknown.currentStatusUnknown)
        XCTAssertTrue(unknown.attention.allSatisfy(\.unavailable))
        var failed = full
        failed.refreshFailed = true
        XCTAssertTrue(failed.currentStatusUnknown)
        XCTAssertFalse(failed.attention.isEmpty)
        XCTAssertTrue(failed.attention.allSatisfy(\.unavailable))
        // The row says Unknown once; the line under the freshness says why.
        XCTAssertEqual(failed.attention.map(\.title), ["Support · Unknown", "Product issues · Unknown", "High priority · Unknown"])
        XCTAssertTrue(failed.attention.allSatisfy { $0.detail == nil })
        XCTAssertEqual(failed.informational.map(\.detail), ["Unknown", "Unknown"])
    }

    func testHomeNullOptionalFieldsAndZeroImpairmentAreHidden() throws {
        let model = try homeFixture { data in
            var gmv = data["gmv"] as! [String: Any]
            gmv["change_pct"] = NSNull()
            gmv["same_weekday_pct"] = NSNull()
            data["gmv"] = gmv
            var money = data["money"] as! [String: Any]
            money["impairment_total"] = 0
            money["ebitda_margin_pct"] = NSNull()
            data["money"] = money
        }
        XCTAssertNil(model.change)
        XCTAssertNil(model.weekdayChange)
        XCTAssertNil(model.moneyRows[1].subtitle)
        XCTAssertNil(model.moneyRows[2].subtitle)
    }

    func testHomeSelectionReadsTheActualPublishedPoint() throws {
        let model = try homeFixture()
        XCTAssertNil(model.selectedPoint(at: nil))
        XCTAssertEqual(model.selectedPoint(at: 28.8)?.date, "2026-11-15")
        XCTAssertEqual(model.selectedPoint(at: 28.8)?.value, 268400)
        XCTAssertEqual(model.selectedPoint(at: 0)?.date, "2026-10-17")
        XCTAssertEqual(model.selectedPoint(at: 0)?.value, 200000)
    }

    func testSharedFunctionsUseThePrivateGumroadProject() async throws {
        let store = AppStore(demo: true)
        try await store.loadBusinessCatalog()
        XCTAssertEqual(store.businessCatalog?.functions.map(\.key), ["accounting", "marketing", "support"])
        XCTAssertEqual(store.businessCatalog?.projects.first?.fullName, "antiwork/gumroad-private")
        XCTAssertEqual(store.businessCatalog?.projects.first?.displayName, "Gumroad")
        let work = try await store.createBusinessWork(function: "support", project: "gumroad-private", title: "Review a synthetic support request", body: "Test only", requestId: "test")
        let items = try await store.businessWork(function: "support", project: "gumroad-private", state: "open")
        XCTAssertEqual(items.map(\.id), [work.id])
        _ = try await store.setBusinessWork(work, state: "closed")
        let completed = try await store.businessWork(function: "support", project: "gumroad-private", state: "closed")
        XCTAssertEqual(completed.first?.state, "closed")
    }

    func testPublishedReportsKeepMissingValuesDistinctFromZeroAndPreserveAllRecords() throws {
        let data = Data(#"{"missing":null,"zero":0,"count":"12","queueSize":14,"amount_cents":12345,"rows":[["2026-11-01",42],["2026-11-02",43]]}"#.utf8)
        let value = try APIJSON.decoder().decode(BusinessValue.self, from: data)
        XCTAssertNil(value["missing"].number)
        XCTAssertNil(value["not_in_source"].number)
        XCTAssertEqual(value["zero"].number, 0)
        XCTAssertEqual(value["count"].number, 12)
        XCTAssertEqual(value["queueSize"].number, 14)
        XCTAssertEqual(value["amount_cents"].number, 12345)
        XCTAssertEqual(value["rows"].array.count, 2)
        XCTAssertEqual(value["rows"][1][1].number, 43)
        XCTAssertEqual(reportNumber(nil), "—")
        XCTAssertEqual(reportNumber(0, money: true), "$0")
    }

    func testBeancountPreviewHasTwelveClosedMonthsAndValidChartDates() throws {
        let report = try BusinessReport.preview("accounting")
        XCTAssertEqual(report.data["ledger"]["months"].array.count, 12)
        let first = report.data["ledger"]["months"][0].text
        XCTAssertNotNil(report.data["ledger"]["per_month"][first]["ebitda"].number)
        XCTAssertEqual(report.data["ledger"]["closed_through"].text, report.data["ledger"]["months"][11].text)
        let point = ReportPoint(date: "2026-11-01", value: 123, series: "Revenue")
        XCTAssertNotEqual(point.day, .distantPast)
        XCTAssertFalse(report.stale)
    }

    func testResolutionDistributionUsesSnapshotTimeAndIgnoresInvalidOrFutureClosures() throws {
        let values = try APIJSON.decoder().decode(BusinessValue.self, from: Data(#"[{"createdAt":"2026-11-16T10:00:00Z","closedAt":"2026-11-16T10:10:00Z"},{"createdAt":"2026-11-16T10:00:00Z","closedAt":"2026-11-16T11:00:00Z"},{"createdAt":"2026-11-16T10:00:00Z","closedAt":"2026-11-17T11:00:00Z"},{"closedAt":"invalid"}]"#.utf8))
        let stats = SupportResolution(closed: values.array, reference: "2026-11-16T12:00:00Z".apiDate)
        XCTAssertEqual(stats.minutes, [10, 60])
        XCTAssertEqual(stats.buckets, [1, 0, 0, 1, 0, 0, 0, 0])
        XCTAssertEqual(stats.p90, 60)
        XCTAssertNil(SupportResolution(closed: values.array, reference: nil).median)
    }
}

@MainActor
func homeFixture(_ name: String = "BusinessOverviewFull", mutate: ((inout [String: Any]) -> Void)? = nil) throws -> BusinessHomePresentation {
    let url = try XCTUnwrap(Bundle(for: BusinessTests.self).url(forResource: name, withExtension: "json"))
    var envelope = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
    var data = try XCTUnwrap(envelope["data"] as? [String: Any])
    mutate?(&data)
    envelope["data"] = data
    let report = try APIJSON.decoder().decode(BusinessReport.self, from: JSONSerialization.data(withJSONObject: envelope))
    return BusinessHomePresentation(report: report, now: try XCTUnwrap("2026-11-16T12:06:00Z".apiDate))
}
