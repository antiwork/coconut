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

    func testThinkingChangesEyesAndMouthWithReadableHolds() {
        let lookingUp = CoconutExpression.sample(time: 0, seed: 0, thinking: 1)
        XCTAssertEqual(lookingUp, CoconutExpression.sample(time: 0.8, seed: 0, thinking: 1))
        let lookingLeft = CoconutExpression.sample(time: 1.8, seed: 0, thinking: 1)
        let curious = CoconutExpression.sample(time: 3.6, seed: 0, thinking: 1)
        XCTAssertLessThan(lookingLeft.yaw, 0)
        XCTAssertNotEqual(lookingLeft.leftEye, lookingLeft.rightEye)
        XCTAssertNotEqual(lookingUp.mouthWidth, curious.mouthWidth)
        XCTAssertGreaterThan(curious.mouthOpen, 0)
        XCTAssertEqual(CoconutExpression.sample(time: 12.6, seed: 0, thinking: 1), lookingUp)
        XCTAssertEqual(CoconutExpression.sample(time: 3.6, seed: 0, thinking: 0), .resting)
    }
}
