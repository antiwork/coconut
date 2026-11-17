#if os(macOS)
import AppKit
import SwiftUI
import XCTest
@testable import Coconut

/// Renders every fruit face into one sprite sheet. The icon of a new agent takes it as a style reference
/// (backend/coconut/assets/agent-sprites.png). Set COCONUT_SPRITE_SHEET to a file path to write it again.
@MainActor
final class AgentSpriteSheetTests: XCTestCase {
    func testTheSpriteSheetRenders() throws {
        let count = FruitArtwork.all.count
        let columns = 6, cell: CGFloat = 160
        let rows = (count + columns - 1) / columns
        let sheet = LazyVGrid(columns: Array(repeating: GridItem(.fixed(cell), spacing: 0), count: columns), spacing: 0) {
            ForEach(0..<count, id: \.self) { index in
                FruitFigure(index: index, size: cell * 0.82, seed: UInt64(index * 7919), time: 0, thinking: 0,
                            eyelid: 1, gesture: .ponder, previousGesture: nil, gestureChangedAt: 0, mood: .neutral)
                    .frame(width: cell, height: cell)
            }
        }
        .frame(width: cell * CGFloat(columns), height: cell * CGFloat(rows))
        .background(Color.white)
        let renderer = ImageRenderer(content: sheet)
        renderer.scale = 1
        let image = try XCTUnwrap(renderer.nsImage)
        let tiff = try XCTUnwrap(image.tiffRepresentation)
        let png = try XCTUnwrap(NSBitmapImageRep(data: tiff)?.representation(using: .png, properties: [:]))
        XCTAssertGreaterThan(png.count, 20_000)
        if let path = ProcessInfo.processInfo.environment["COCONUT_SPRITE_SHEET"] {
            try png.write(to: URL(fileURLWithPath: path))
        }
    }
}
#endif
