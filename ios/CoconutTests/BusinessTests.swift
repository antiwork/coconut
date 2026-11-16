import XCTest
@testable import Coconut

@MainActor
final class BusinessTests: XCTestCase {
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
        XCTAssertNotNil(report.data["ledger"]["per_month"]["2026-01"]["ebitda"].number)
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
