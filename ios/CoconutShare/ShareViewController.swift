import Intents
import SwiftUI
import UIKit

/// The share extension of the iPhone app. It shows the share sheet of Coconut and sends the share to the paired Mac
/// itself, so the app does not open.
@objc(ShareViewController)
final class ShareViewController: UIViewController {
    private var model: ShareModel?
    /// The key of the new chat of each project that this share tried, so a second try gets the same chat back
    /// (ShareSender.send).
    private var newChatKeys: [String: String] = [:]

    override func viewDidLoad() {
        super.viewDidLoad()
        let model = ShareModel(deliver: { [weak self] message, destination, progress in
            let directory = ShareDirectoryStore.load()
            let transport = try SharePairing.transport(for: directory)
            let key = self?.newChatKey(for: destination) ?? UUID().uuidString
            do {
                try await ShareSender.send(message, to: destination, over: transport, newChatKey: key, progress: progress)
            } catch {
                await transport.close()
                throw error
            }
            await transport.close()
            // The place shows in the row of people of the next share sheet, as after a message in the app.
            if let directory { ShareSuggestions.donate(directory.suggestion(for: destination)) }
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
        // A tap on a place of Coconut in the row of people of the share sheet opens the sheet with that place chosen.
        let suggested = (extensionContext?.intent as? INSendMessageIntent)?.conversationIdentifier
        Task {
            let loaded = await ShareItemLoader.load(items)
            model.show(directory: ShareDirectoryStore.load(), items: loaded.items, skipped: loaded.skipped,
                       suggested: suggested)
        }
    }

    private func newChatKey(for destination: ShareDestination) -> String {
        if let key = newChatKeys[destination.id] { return key }
        let key = UUID().uuidString
        newChatKeys[destination.id] = key
        return key
    }
}
