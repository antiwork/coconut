#if os(macOS)
import AppKit
import SwiftUI
import XCTest
@testable import Coconut

/// Renders the Mac sidebar with the preview data, in light and dark mode, for review. Set COCONUT_SIDEBAR_SHOTS to a
/// folder to keep the images.
@MainActor
final class MacSidebarSnapshotTests: XCTestCase {
    func testTheSidebarRenders() throws {
        let store = AppStore(demo: true)
        // Unread chats and open work, so each section shows.
        for (index, unread) in [3, 0, 1].enumerated() where index < store.bots.count {
            store.bots[index].unreadCount = unread
        }
        if store.bots.count > 3 { store.bots[3].pinned = true }
        let owner = store.bots.first?.id ?? ""
        let states = [("Rebase the Pages PR on main", "working"), ("Review the model picker fix", "ready"),
                      ("Wait for the TestFlight build", "waiting"), ("Fix click-drag on the Mac window", "blocked")]
        store.artifacts += states.enumerated().map { index, item in
            Artifact(id: "work-\(index)", botId: owner, taskId: nil, title: item.0, kind: "work", status: item.1,
                     preview: "", url: nil, createdAt: "2026-11-16T0\(index):00:00Z")
        }
        for scheme in [ColorScheme.light, .dark] {
            let view = MacSidebar(selection: .constant(.inbox))
                .environment(store)
                .frame(width: 280, height: 760)
                .background(.background)
                .environment(\.colorScheme, scheme)
            // A list is an AppKit table, which ImageRenderer does not draw. A window off the screen draws it.
            let host = NSHostingView(rootView: view)
            host.frame = NSRect(x: 0, y: 0, width: 280, height: 760)
            let window = NSWindow(contentRect: host.frame, styleMask: [.borderless], backing: .buffered, defer: false)
            window.appearance = NSAppearance(named: scheme == .dark ? .darkAqua : .aqua)
            window.contentView = host
            window.setFrameOrigin(NSPoint(x: -10_000, y: -10_000))
            window.orderBack(nil)
            for _ in 0..<10 { RunLoop.main.run(until: Date().addingTimeInterval(0.1)) }
            host.layoutSubtreeIfNeeded()
            let rep = try XCTUnwrap(host.bitmapImageRepForCachingDisplay(in: host.bounds))
            host.cacheDisplay(in: host.bounds, to: rep)
            window.orderOut(nil)
            let data = try XCTUnwrap(rep.representation(using: .png, properties: [:]))
            XCTAssertGreaterThan(data.count, 10_000)
            if let folder = ProcessInfo.processInfo.environment["COCONUT_SIDEBAR_SHOTS"] {
                try data.write(to: URL(fileURLWithPath: folder).appendingPathComponent("sidebar-\(scheme).png"))
            }
        }
    }
}
#endif
