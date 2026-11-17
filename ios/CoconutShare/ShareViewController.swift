import SwiftUI
import UIKit

/// The share extension of the iPhone app. It shows the share sheet of Coconut and sends the share to the paired Mac
/// itself, so the app does not open.
@objc(ShareViewController)
final class ShareViewController: UIViewController {
    private var model: ShareModel?

    override func viewDidLoad() {
        super.viewDidLoad()
        let model = ShareModel(deliver: { message, destination, progress in
            let transport = try SharePairing.transport(for: ShareDirectoryStore.load())
            do {
                try await ShareSender.send(message, to: destination, over: transport, progress: progress)
            } catch {
                await transport.close()
                throw error
            }
            await transport.close()
        }, finish: { [weak self] in
            self?.extensionContext?.completeRequest(returningItems: nil)
        }, cancel: { [weak self] in
            self?.extensionContext?.cancelRequest(withError: CocoaError(.userCancelled))
        })
        self.model = model
        let host = UIHostingController(rootView: ShareComposeView(model: model))
        addChild(host)
        host.view.frame = view.bounds
        host.view.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        view.addSubview(host.view)
        host.didMove(toParent: self)
        let items = (extensionContext?.inputItems as? [NSExtensionItem]) ?? []
        Task {
            let loaded = await ShareItemLoader.load(items)
            model.show(directory: ShareDirectoryStore.load(), items: loaded.items, skipped: loaded.skipped)
        }
    }
}
