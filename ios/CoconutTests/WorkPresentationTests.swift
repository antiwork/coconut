import XCTest
@testable import Coconut

final class WorkPresentationTests: XCTestCase {
    func testStatusLabelNamesQueuedAndPausedWork() {
        XCTAssertEqual(StatusLabel.label(for: "queued"), "Queued")
        XCTAssertEqual(StatusLabel.label(for: "paused"), "Paused")
    }

    func testStopAsksFirstAndForgetsTheTaskWhenCancelled() {
        var stop = StopConfirmation<String>()
        XCTAssertFalse(stop.isPresented)
        stop.request("task")
        XCTAssertTrue(stop.isPresented, "A Stop control opens the confirmation instead of stopping.")
        XCTAssertEqual(stop.item, "task")
        stop.isPresented = false
        XCTAssertNil(stop.item, "Cancel keeps no task to stop later.")
        XCTAssertEqual(StopCopy.title(executor: "Forge"), "Stop Forge's task?")
    }
}
