import XCTest
@testable import Coconut

/// The report screen only prints published figures, so the tests cover decoding and formatting rather than arithmetic.
final class BusinessPeriodsTests: XCTestCase {
    private func periods() throws -> BusinessPeriods {
        BusinessPeriods(report: try BusinessReport.preview("periods"))
    }

    func testDecodesDaysMonthsAndYears() throws {
        let model = try periods()
        XCTAssertEqual(model.days.count, 30)
        XCTAssertEqual(model.months.count, 12)
        XCTAssertEqual(model.years.count, 6)
        XCTAssertNotNil(model.asOf)
        XCTAssertEqual(model.lastDayGmv, 274000)
        XCTAssertEqual(model.vsAverage, 15.1)
        XCTAssertEqual(model.vsWeekday, 3.4)
    }

    func testLastYearIsMarkedPartial() throws {
        let model = try periods()
        XCTAssertEqual(model.years.last?.partial, true)
        XCTAssertEqual(model.years.first?.partial, false)
        XCTAssertEqual(model.years.last?.months, 10)
    }

    func testEveryMonthCarriesThePublishedNetIncome() throws {
        let model = try periods()
        XCTAssertEqual(model.months.last?.netIncome, 483000)
        XCTAssertEqual(model.months.last?.revenue, 2890000)
        XCTAssertEqual(model.months.last?.margin, 16.7)
        XCTAssertEqual(model.revenue3mPercent, 4.3)
    }

    func testFormattingIsTakenFromTheFigure() {
        XCTAssertEqual(BusinessPeriodFormat.money(8500000), "$8.50M")
        XCTAssertEqual(BusinessPeriodFormat.money(268400, compact: false), "$268,400")
        XCTAssertEqual(BusinessPeriodFormat.money(-1930000), "-$1.93M")
        XCTAssertEqual(BusinessPeriodFormat.money(nil), "—")
        XCTAssertEqual(BusinessPeriodFormat.percent(30.3), "30.3%")
        XCTAssertEqual(BusinessPeriodFormat.percent(2.3, signed: true), "+2.3%")
        XCTAssertEqual(BusinessPeriodFormat.percent(-0.7, signed: true), "-0.7%")
        XCTAssertEqual(BusinessPeriodFormat.label("2026-10"), "Oct 2026")
        XCTAssertEqual(BusinessPeriodFormat.label("2026Q4"), "Q4 2026")
        XCTAssertEqual(BusinessPeriodFormat.label("2021"), "2021")
        XCTAssertEqual(BusinessPeriodFormat.day("2026-11-15"), "Nov 15")
    }
}
