import SwiftUI
import XCTest
@testable import Coconut

/// Draws the share sheet of Coconut with the preview data, as the share extensions show it. Set COCONUT_WINDOW_SHOTS
/// to a folder to keep the images.
@MainActor
final class ShareSheetSnapshotTests: XCTestCase {
    func testTheShareSheetRenders() async throws {
        let store = AppStore(demo: true)
        store.bots[store.bots.count - 1].coconut = true
        store.bots[store.bots.count - 1].name = "Coconut"
        let rooms = #"""
        {"rooms": [{"room": "aa11", "name": "Gumroad", "state": "active", "unread": 0,
          "members": [{"member": "m1", "name": "Ada", "this_mac": true}, {"member": "m2", "name": "Grace", "direct": true},
                      {"member": "m3", "name": "Lin", "direct": true}],
          "chats": [{"id": "c1", "name": "Payouts"}]}]}
        """#
        store.team = try APIJSON.decoder().decode(TeamRooms.self, from: Data(rooms.utf8))
        let directory = try XCTUnwrap(store.shareDirectory())
        // The app draws each face for the extension.
        XCTAssertTrue(directory.destinations.allSatisfy { ($0.face?.count ?? 0) > 200 })
        XCTAssertEqual(directory.sections().map(\.kind), [.agent, .team, .direct])

        let shot = try ShareSheetTests.png(width: 640, height: 400, red: 0.93, green: 0.90, blue: 0.84)
        let items: [SharedItem] = [
            .file(SharedFile(data: shot, name: "Payout dashboard.png")),
            .file(SharedFile(data: Data(count: 1_260_000), name: "Creator payout report.pdf")),
            .link(URL(string: "https://gumroad.com/features")!),
        ]
        let name = "share-shots-" + UUID().uuidString
        let defaults = try XCTUnwrap(UserDefaults(suiteName: name))
        defer { defaults.removePersistentDomain(forName: name) }
        func model(note: String = "", items: [SharedItem] = items, directory: ShareDirectory? = directory,
                   to destination: String? = nil, fails: Bool = false) async -> ShareModel {
            let model = ShareModel(defaults: defaults, deliver: { _, _, _ in
                if fails { throw ShareFailure(TunnelError.macOffline) }
            }, finish: {}, cancel: {})
            model.show(directory: directory, items: items)
            model.note = note
            if let destination { model.destination = directory?.destinations.first { $0.id == destination } }
            if fails { await model.send() }
            return model
        }
        let filled = await model(note: "Numbers for Friday. Can you check the totals against Stripe?")
        let link = await model(items: [.text("Gumroad features"), .link(URL(string: "https://gumroad.com/features")!)],
                               to: "team-aa11-c1")
        let failed = await model(note: "For the review", fails: true)
        let empty = await model(directory: nil)
        try keep(ShareComposeView(model: filled), as: "share-files")
        try keep(ShareComposeView(model: link), as: "share-link-team")
        try keep(ShareComposeView(model: failed), as: "share-failed")
        try keep(ShareComposeView(model: empty), as: "share-needs-app")
        try keep(ShareComposeView(model: filled).environment(\.colorScheme, .dark), as: "share-files-dark", dark: true)
        #if !os(macOS)
        try keep(NavigationStack { ShareDestinationList(model: filled) {} }.tint(.primary), as: "share-chats")
        #endif
    }

    private func keep(_ view: some View, as name: String, dark: Bool = false) throws {
        let data = try render(view, dark: dark)
        XCTAssertGreaterThan(data.count, 5_000, name)
        if let folder = ProcessInfo.processInfo.environment["COCONUT_WINDOW_SHOTS"] {
            #if os(macOS)
            let file = "mac-\(name).png"
            #else
            let file = "iphone-\(name).png"
            #endif
            try data.write(to: URL(fileURLWithPath: folder).appendingPathComponent(file))
        }
    }

    #if os(macOS)
    private func render(_ view: some View, dark: Bool) throws -> Data {
        let host = NSHostingView(rootView: view)
        host.frame = NSRect(origin: .zero, size: host.fittingSize)
        let window = NSWindow(contentRect: host.frame, styleMask: [.borderless], backing: .buffered, defer: false)
        window.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        window.contentView = host
        window.setFrameOrigin(NSPoint(x: -10_000, y: -10_000))
        window.orderBack(nil)
        for _ in 0..<10 { RunLoop.main.run(until: Date().addingTimeInterval(0.1)) }
        host.layoutSubtreeIfNeeded()
        let rep = try XCTUnwrap(host.bitmapImageRepForCachingDisplay(in: host.bounds))
        host.cacheDisplay(in: host.bounds, to: rep)
        window.orderOut(nil)
        return try XCTUnwrap(rep.representation(using: .png, properties: [:]))
    }
    #else
    private func render(_ view: some View, dark: Bool) throws -> Data {
        let size = CGSize(width: 393, height: 700)
        let host = UIHostingController(rootView: view)
        let window = UIWindow(frame: CGRect(origin: .zero, size: size))
        window.overrideUserInterfaceStyle = dark ? .dark : .light
        window.rootViewController = host
        window.isHidden = false
        for _ in 0..<10 { RunLoop.main.run(until: Date().addingTimeInterval(0.1)) }
        host.view.layoutIfNeeded()
        let image = UIGraphicsImageRenderer(size: size).image { _ in
            window.drawHierarchy(in: CGRect(origin: .zero, size: size), afterScreenUpdates: true)
        }
        window.isHidden = true
        return try XCTUnwrap(image.pngData())
    }
    #endif
}
