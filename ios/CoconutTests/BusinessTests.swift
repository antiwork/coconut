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
        XCTAssertEqual(model.weekdayComparison, "+6.5% vs the same weekday")
        XCTAssertEqual(model.moneyRows.map(\.value), [12000000, 3200000, 870000])
        XCTAssertEqual(model.moneyRows.map(\.formattedValue), ["$12.0M", "$3.2M", "$870.0K"])
        XCTAssertEqual(model.moneyRows.map(\.months.count), [12, 12, 12])
        XCTAssertEqual(model.moneyRows[1].subtitle, "26.7% margin")
        XCTAssertEqual(model.moneyRows[2].subtitle, "includes $1.93M non-cash write-off")
        XCTAssertEqual(model.moneyRows[2].months[7].value, -1700000)
        XCTAssertEqual(model.closedThrough, "2026-10")
    }

    func testHomeLegacySnapshotDoesNotInventMoneyOrDeltas() throws {
        let model = try homeFixture("BusinessOverviewLegacy")
        XCTAssertNil(model.change)
        XCTAssertNil(model.changeLabel)
        XCTAssertNil(model.weekdayComparison)
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
        XCTAssertNil(model.weekdayComparison)
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
        XCTAssertEqual(model.sourceNotice("gmv"), "Stale source · current status unknown")
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
        XCTAssertNil(model.weekdayComparison)
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
final class BusinessBentoTests: XCTestCase {
    private func value(_ json: String) throws -> BusinessValue { try APIJSON.decoder().decode(BusinessValue.self, from: Data(json.utf8)) }
    private func place(_ spans: [BentoSpan], columns: Int, width: CGFloat = 1200, heights: [Int: CGFloat] = [:]) -> (frames: [CGRect], height: CGFloat) {
        BentoGrid.place(spans, columns: columns, width: width, row: 100, gap: 12) { index, _ in heights[index] ?? 0 }
    }

    func testGridHasTwoColumnsOnAnIPhoneAndTwelveOnAMac() {
        XCTAssertEqual(BentoGrid.columns(for: 358), 2)
        XCTAssertEqual(BentoGrid.columns(for: 619), 2)
        XCTAssertEqual(BentoGrid.columns(for: 620), 12)
        XCTAssertEqual(BentoGrid.columns(for: 1112), 12)
    }

    func testGridPutsSmallTilesNextToATallTile() {
        // A hero of 8 by 3 and three tiles of 4 by 1: the small tiles stack at its side, then a new line starts.
        let result = place([BentoSpan(columns: 8, rows: 3), BentoSpan(columns: 4, rows: 1), BentoSpan(columns: 4, rows: 1),
                            BentoSpan(columns: 4, rows: 1), BentoSpan(columns: 12, rows: 1)], columns: 12)
        let column = (1200.0 - 11 * 12) / 12
        XCTAssertEqual(result.frames[0], CGRect(x: 0, y: 0, width: 8 * column + 7 * 12, height: 324))
        XCTAssertEqual(result.frames[1].origin.x, 8 * (column + 12), accuracy: 0.01)
        XCTAssertEqual(result.frames[1].size.height, 100)
        XCTAssertEqual(result.frames.map(\.minY), [0, 0, 112, 224, 336])
        XCTAssertEqual(result.frames[3].maxX, 1200, accuracy: 0.01)
        XCTAssertEqual(result.height, 436)
    }

    func testGridFillsAFreePlaceBeforeItStartsANewLine() {
        // 4 + 4 leave four columns of the first line free. A tile of 6 does not fit there; the next tile of 4 does.
        let result = place([BentoSpan(columns: 4, rows: 1), BentoSpan(columns: 4, rows: 1), BentoSpan(columns: 6, rows: 1),
                            BentoSpan(columns: 4, rows: 1)], columns: 12)
        XCTAssertEqual(result.frames.map(\.minY), [0, 0, 112, 0])
        XCTAssertFalse(zip(result.frames, result.frames.dropFirst()).contains { $0.intersects($1) })
        XCTAssertEqual(result.height, 212)
    }

    func testAListOverTheWholeWidthTakesTheHeightOfItsContent() {
        let result = place([BentoSpan(columns: 12, rows: 1), BentoSpan(columns: 12, rows: nil), BentoSpan(columns: 6, rows: 1)],
                           columns: 12, heights: [1: 333])
        XCTAssertEqual(result.frames[1], CGRect(x: 0, y: 112, width: 1200, height: 333))
        XCTAssertEqual(result.frames[2].minY, 112 + 333 + 12)
        XCTAssertEqual(result.height, 112 + 333 + 12 + 100)
    }

    func testATileOfPartOfTheWidthTakesWholeRowsForItsContent() {
        // 150 points of content need two rows of 100 with their gap.
        let result = place([BentoSpan(columns: 6, rows: nil), BentoSpan(columns: 6, rows: 2)], columns: 12, heights: [0: 150])
        XCTAssertEqual(result.frames[0].height, 212)
        XCTAssertEqual(result.frames[1].height, 212)
        XCTAssertEqual(result.height, 212)
    }

    func testANarrowGridKeepsATileInsideItsTwoColumns() {
        let result = place([BentoSpan(columns: 2, rows: 3), BentoSpan(columns: 1, rows: 2), BentoSpan(columns: 1, rows: 2),
                            BentoSpan(columns: 8, rows: 1)], columns: 2, width: 358)
        XCTAssertEqual(result.frames[0].width, 358)
        XCTAssertEqual(result.frames[1].width, 173)
        XCTAssertEqual(result.frames[2].minX, 185)
        XCTAssertEqual(result.frames[1].minY, result.frames[2].minY)
        // A span wider than the grid is the whole width.
        XCTAssertEqual(result.frames[3].width, 358)
        XCTAssertEqual(place([], columns: 2).height, 0)
    }

    func testHomeReadsThePublishedShapeOfPullRequestsAndHighPriority() throws {
        // The shape of the published snapshot: one count per repository, and the titles by issue number.
        let model = try homeFixture { data in
            data["prs"] = ["data": ["gumroad": ["pullRequests": ["totalCount": 6]], "flexile": ["pullRequests": ["totalCount": 3]]]]
            data["high_priority"] = ["ok": true, "count": 2, "numbers": [4712, 4721], "titles": ["4712": "Synthetic first", "4721": "Synthetic second"]]
        }
        XCTAssertEqual(model.pullRequests, 9)
        XCTAssertEqual(model.informational.last?.value, "9")
        XCTAssertEqual(model.informational.last?.detail, "9 open")
        XCTAssertEqual(model.informational.last?.unavailable, false)
        XCTAssertEqual(model.attention.last?.title, "High priority · 2 open")
        XCTAssertEqual(model.attention.last?.detail, "Synthetic first")
        // The older shape still reads, and a snapshot without counts stays unknown.
        XCTAssertEqual(try homeFixture().pullRequests, 9)
        let missing = try homeFixture { $0["prs"] = ["data": [:]] }
        XCTAssertNil(missing.pullRequests)
        XCTAssertNil(missing.informational.last?.value)
        XCTAssertEqual(missing.informational.last?.detail, "Not published")
    }

    func testHomeShowsPublishedGMVTotalsOnlyFromACurrentSource() throws {
        let model = try homeFixture()
        XCTAssertEqual(model.total, 7200000)
        XCTAssertEqual(model.runRate, 84570500)
        XCTAssertEqual(model.dailyAverage, 231700)
        XCTAssertEqual(model.informational.first?.value, "24")
        let failed = try homeFixture { $0["sections"] = ["gmv": ["ok": false], "risk": ["ok": false]] }
        XCTAssertNil(failed.total)
        XCTAssertNil(failed.runRate)
        XCTAssertNil(failed.dailyAverage)
        XCTAssertNil(failed.informational.first?.value)
        XCTAssertEqual(failed.informational.first?.detail, "Unavailable")
    }

    func testSourceDaysAndMonthsReadAsDates() throws {
        // The snapshot publishes the day of a source as a day, not as a time. It is shown and never makes a source stale.
        let model = try homeFixture { $0["sections"] = ["gmv": ["ok": true, "as_of": "2026-11-15"], "ledger": ["ok": true, "as_of": "soon"]] }
        XCTAssertEqual(model.asOf("gmv"), "as of Nov 15")
        XCTAssertNil(model.sourceNotice("gmv"))
        XCTAssertNil(model.asOf("ledger"))
        XCTAssertNil(model.asOf("support"))
        XCTAssertNil(try homeFixture().asOf("gmv"))
        XCTAssertEqual(BusinessHomePresentation.month("2026-10"), "Oct 2026")
        XCTAssertEqual(BusinessHomePresentation.month("later"), "later")
    }

    func testAWatchedRiskAccountShowsItsEmailRevenueAndNote() throws {
        let watched = RiskAccount(try value(#"{"name":null,"username":null,"email":"creator@example.com","revenue_cents":1840000,"unpaid_balance_cents":212000,"notes":"Sales grew fast."}"#))
        XCTAssertEqual(watched.title, "creator@example.com")
        XCTAssertEqual(watched.detail, "Sales grew fast.")
        XCTAssertEqual(watched.amount, "$18.4K")
        XCTAssertEqual(watched.amountLabel, "$2,120 unpaid")
        let reviewed = RiskAccount(try value(#"{"name":"Mira Chen","username":"mira","email":"mira@example.com","balance_cents":48200}"#))
        XCTAssertEqual(reviewed.title, "Mira Chen")
        XCTAssertEqual(reviewed.detail, "mira@example.com")
        XCTAssertEqual(reviewed.amount, "$482")
        XCTAssertEqual(reviewed.amountLabel, "Balance")
        let empty = RiskAccount(try value("{}"))
        XCTAssertEqual(empty.title, "Unknown account")
        XCTAssertNil(empty.detail)
        XCTAssertEqual(empty.amount, "—")
    }

    func testAPullRequestRowReadsItsAuthorLinesReviewAndChecks() throws {
        let pull = PullRequestRow(try value(#"{"number":4728,"title":"Upgrade the Stripe SDK","url":"https://github.com/example/gumroad/pull/4728","author":{"login":"grace"},"additions":96,"deletions":71,"isDraft":false,"reviewDecision":"CHANGES_REQUESTED","commits":{"nodes":[{"commit":{"statusCheckRollup":{"state":"FAILURE"}}}]}}"#))
        XCTAssertEqual(pull.detail, "#4728 · grace")
        XCTAssertEqual(pull.additions, "+96")
        XCTAssertEqual(pull.deletions, "−71")
        XCTAssertEqual(pull.state, "Changes requested")
        XCTAssertEqual(pull.checkLabel, "Checks fail")
        let draft = PullRequestRow(try value(#"{"title":"Draft","isDraft":true,"reviewDecision":"APPROVED"}"#))
        XCTAssertEqual(draft.state, "Draft")
        XCTAssertEqual(draft.checkLabel, "No checks")
        XCTAssertEqual(draft.detail, "")
        XCTAssertEqual(draft.deletions, "−—")
        XCTAssertNil(PullRequestRow(try value(#"{"reviewDecision":null}"#)).state)
    }

    func testSupportLinksUseThePublishedIssueLinkAndDurationsAreShort() throws {
        XCTAssertEqual(SupportFormat.issueURL(try value(#"{"slug":"4712","url":"https://github.com/example/flexile/issues/4712"}"#))?.absoluteString,
                       "https://github.com/example/flexile/issues/4712")
        XCTAssertEqual(SupportFormat.issueURL(try value(#"{"slug":"4712"}"#))?.absoluteString, "https://github.com/antiwork/gumroad-private/issues/4712")
        XCTAssertNil(SupportFormat.issueURL(try value(#"{"slug":"sample-1","url":"javascript:alert(1)"}"#)))
        XCTAssertEqual(SupportFormat.duration(nil), "—")
        XCTAssertEqual(SupportFormat.duration(42), "42m")
        XCTAssertEqual(SupportFormat.duration(108), "1.8h")
    }

    func testThePreviewHasEachReportInThePublishedShape() throws {
        let home = try BusinessReport.preview("overview")
        let model = BusinessHomePresentation(report: home)
        XCTAssertEqual(model.moneyRows.map(\.months.count), [12, 12, 12])
        XCTAssertEqual(model.volume.count, 30)
        XCTAssertEqual(model.previousVolume.count, 30)
        XCTAssertEqual(model.pullRequests, 9)
        XCTAssertFalse(model.currentStatusUnknown)
        XCTAssertNotNil(model.asOf("gmv"))
        let risk = try BusinessReport.preview("risk")
        XCTAssertEqual(risk.data["watching"]["users"].array.count, 3)
        XCTAssertEqual(risk.data["chargebacks"]["daily"].array.count, 28)
        let engineering = try BusinessReport.preview("engineering")
        XCTAssertEqual(engineering.data["data"]["gumroad"]["pullRequests"]["nodes"].array.count, 4)
        let support = try BusinessReport.preview("support")
        XCTAssertEqual(SupportResolution(closed: support.data["support"]["closed"].array, reference: support.data["generatedAt"].text.apiDate).minutes.count, 36)
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
