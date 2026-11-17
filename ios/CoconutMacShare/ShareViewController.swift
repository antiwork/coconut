import AppKit
import SwiftUI

/// The share extension of the Mac app. It shows the share sheet of Coconut and hands the share to the app, which
/// sends it (ShareInbox). The link then starts the app or brings it to the front.
@objc(ShareViewController)
final class ShareViewController: NSViewController {
    private var model: ShareModel?

    override func loadView() {
        let model = ShareModel(deliver: { message, destination, _ in
            try ShareInbox.write(message, to: destination)
            NSWorkspace.shared.open(ShareInbox.wake)
        }, finish: { [weak self] in
            self?.extensionContext?.completeRequest(returningItems: nil)
        }, cancel: { [weak self] in
            self?.extensionContext?.cancelRequest(withError: CocoaError(.userCancelled))
        })
        self.model = model
        let host = NSHostingView(rootView: ShareComposeView(model: model))
        view = host
        preferredContentSize = host.fittingSize
        let items = (extensionContext?.inputItems as? [NSExtensionItem]) ?? []
        Task {
            let loaded = await ShareItemLoader.load(items)
            model.show(directory: ShareDirectoryStore.load(), items: loaded.items, skipped: loaded.skipped)
            // The sheet grows to the rows of the share.
            host.layoutSubtreeIfNeeded()
            self.preferredContentSize = host.fittingSize
        }
    }
}
