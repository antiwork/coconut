import XCTest
@testable import Coconut

final class BotExpressionTests: XCTestCase {
    func testIdlePoseDoesNotDependOnTimeOrBotIdentity() {
        for seed: UInt64 in [0, 1, 100, .max] {
            for time in [0.0, 2.3, 60, 1_000_000] {
                XCTAssertEqual(CoconutExpression.sample(time: time, seed: seed, thinking: 0), .resting)
            }
        }
        XCTAssertGreaterThan(CoconutExpression.resting.yaw, 0)
        XCTAssertLessThan(CoconutExpression.resting.pitch, 0)
        XCTAssertLessThan(CoconutExpression.resting.eyeTilt, 0)
    }

    func testPonderingHoldsEachGazeLongEnoughToRead() {
        let lookingLeft = CoconutExpression.sample(time: 0.5, seed: 0, thinking: 1)
        XCTAssertEqual(lookingLeft, CoconutExpression.sample(time: 1.2, seed: 0, thinking: 1))
        XCTAssertLessThan(lookingLeft.yaw, -0.3)
        XCTAssertEqual(lookingLeft.squash, 0)
        let lookingRight = CoconutExpression.sample(time: 2.0, seed: 0, thinking: 1)
        XCTAssertGreaterThan(lookingRight.yaw, 0.3)
        let moving = CoconutExpression.sample(time: 1.55, seed: 0, thinking: 1)
        XCTAssertGreaterThan(moving.squash, 0)
    }

    func testEveryPhaseHasAGestureThatDiffersFromRest() {
        for phase in BotPhase.allCases {
            let poses = stride(from: 0.0, to: 4, by: 0.1).map {
                CoconutExpression.sample(time: $0, seed: 0, thinking: 1, gesture: phase.gesture)
            }
            XCTAssertTrue(poses.contains { $0 != .resting }, "\(phase) does not move.")
        }
        let reading = CoconutExpression.sample(time: 0.4, seed: 0, thinking: 1, gesture: .scan)
        let recalling = CoconutExpression.sample(time: 0.9, seed: 0, thinking: 1, gesture: .recall)
        XCTAssertGreaterThan(reading.pitch, 0, "Reading looks down at the text.")
        XCTAssertLessThan(recalling.pitch, -0.4, "Recall looks up.")
    }

    func testSpinTurnsTheFaceAwayAndBackToTheFront() {
        let away = CoconutExpression.sample(time: 0.625, seed: 0, thinking: 1, gesture: .spin)
        XCTAssertLessThan(cos(away.yaw), 0)
        let front = CoconutExpression.sample(time: 1.4, seed: 0, thinking: 1, gesture: .spin)
        XCTAssertGreaterThan(cos(front.yaw), 0.9)
        XCTAssertGreaterThan(front.eyeHeight, 1.2)
    }

    func testANewPhasePassesThroughTheRestingFace() {
        let midpoint = CoconutExpression.sample(time: 10.25, seed: 0, thinking: 1, gesture: .scan,
                                                previous: .dart, changedAt: 10)
        XCTAssertEqual(midpoint.yaw, CoconutExpression.resting.yaw, accuracy: 0.001)
        XCTAssertEqual(midpoint.pitch, CoconutExpression.resting.pitch, accuracy: 0.001)
        let settled = CoconutExpression.sample(time: 10.6, seed: 0, thinking: 1, gesture: .scan, previous: .dart, changedAt: 10)
        XCTAssertEqual(settled, CoconutExpression.sample(time: 10.6, seed: 0, thinking: 1, gesture: .scan))
    }

    func testEyesLandWithASmallOvershoot() {
        XCTAssertEqual(CoconutExpression.settle(0), 0, accuracy: 0.0001)
        XCTAssertEqual(CoconutExpression.settle(1), 1, accuracy: 0.0001)
        let peak = stride(from: 0.0, through: 1, by: 0.01).map(CoconutExpression.settle).max() ?? 0
        XCTAssertGreaterThan(peak, 1)
        XCTAssertLessThan(peak, 1.1)
    }

    func testShimmerRestsOffTheLabelForItsRestTime() {
        // The band moves at one speed. The distance past the label equals the rest time at that speed.
        let speed = (1.3 - ShimmerText.start) / ShimmerText.sweep
        XCTAssertEqual((ShimmerText.end - 1.3) / speed, ShimmerText.rest, accuracy: 0.0001)
    }

    func testTypingDotsWaveFromLeftToRight() {
        XCTAssertGreaterThan(TypingDots.level(index: 0, time: 0.2), TypingDots.level(index: 2, time: 0.2))
        XCTAssertGreaterThan(TypingDots.level(index: 2, time: 0.6), TypingDots.level(index: 0, time: 0.6))
        XCTAssertEqual(TypingDots.level(index: 1, time: 0.9), 0)
    }
}
