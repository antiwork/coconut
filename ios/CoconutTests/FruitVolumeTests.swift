import SwiftUI
import XCTest
@testable import Coconut

final class FruitVolumeTests: XCTestCase {
    private let front = FruitTurn(yaw: 0, pitch: 0)

    func testEveryFruitHasABodyAroundItsAxis() {
        for (index, volume) in FruitVolume.all.enumerated() {
            let name = FruitArtwork.all[index].name
            XCTAssertGreaterThan(volume.rows.filter { !$0.isEmpty }.count, 40, name)
            XCTAssertTrue(FruitArtwork.paths[index][0].contains(volume.axis), name)
        }
    }

    func testTheFrontViewDrawsTheFlatArtwork() {
        for (index, volume) in FruitVolume.all.enumerated() {
            let name = FruitArtwork.all[index].name
            let drawn = volume.body(turn: front)[0].boundingRect, artwork = FruitArtwork.paths[index][0].boundingRect
            XCTAssertEqual(drawn.minX, artwork.minX, accuracy: 1.5, name)
            XCTAssertEqual(drawn.maxX, artwork.maxX, accuracy: 1.5, name)
            let face = CGPoint(x: FruitArtwork.all[index].face[0], y: FruitArtwork.all[index].face[1])
            let placed = volume.surface(face, turn: front)
            XCTAssertEqual(placed.point.x, face.x, accuracy: 0.6, name)
            XCTAssertEqual(placed.point.y, face.y, accuracy: 0.6, name)
            XCTAssertGreaterThan(placed.facing, 0.5, name)
        }
    }

    func testAFaceTurnedAwayStaysOnTheEdgeOfTheFruit() {
        for (index, volume) in FruitVolume.all.enumerated() {
            let name = FruitArtwork.all[index].name
            let face = CGPoint(x: FruitArtwork.all[index].face[0], y: FruitArtwork.all[index].face[1])
            let turn = FruitTurn(yaw: 2, pitch: 0)
            let placed = volume.surface(face, turn: turn)
            let outline = volume.body(turn: turn)[0].boundingRect
            XCTAssertLessThan(placed.facing, 0, name)
            XCTAssertLessThanOrEqual(placed.point.x, outline.maxX + 1, name)
            XCTAssertGreaterThanOrEqual(placed.point.x, outline.minX - 1, name)
            XCTAssertLessThan(volume.surface(face, turn: FruitTurn(yaw: .pi, pitch: 0)).facing, -0.5, name)
        }
    }

    func testStemsTurnAsCardsAndInnerLayersSitOnTheSurface() {
        let apple = FruitVolume.all[FruitArtwork.all.firstIndex { $0.id == "apple" }!]
        XCTAssertEqual(apple.layers.map(\.onSurface), [false, false])
        let coconut = FruitVolume.all[FruitArtwork.all.firstIndex { $0.id == "coconut" }!]
        XCTAssertEqual(coconut.layers.map(\.onSurface), [true])
    }

    func testMoodsChangeTheFaceButNotTheGaze() {
        let rest = CoconutExpression.resting
        for mood in [CoconutMood.neutral, .happy, .surprised, .sad, .sleepy] {
            let face = mood.applied(to: rest)
            XCTAssertEqual(face.yaw, rest.yaw)
            XCTAssertEqual(face.pitch, rest.pitch)
        }
        XCTAssertGreaterThan(CoconutMood.happy.applied(to: rest).smile, rest.smile)
        XCTAssertLessThan(CoconutMood.sad.applied(to: rest).smile, 0)
        XCTAssertGreaterThan(CoconutMood.surprised.applied(to: rest).mouthOpen, 0.4)
        XCTAssertNotNil(CoconutMood.happy.eyeArc)
        XCTAssertNil(CoconutMood.neutral.eyeArc)
    }
}
