import SwiftUI

/// Sends the shares that the share extension of the Mac left for the app (ShareInbox). The app sends them as the
/// composers do: each file goes to the Mac first, then the message names them. Then the window shows the chat.
@MainActor
enum MacShareInbox {
    private static var delivering = false

    /// Sends each share that waits, oldest first. Without a connection to the paired Mac the shares stay, and the next
    /// call sends them: at the start of the app, when the connection is back, and when the extension wakes the app.
    static func deliver(store: AppStore, model: AppModel, inbox: URL = ShareFolder.inbox) async {
        guard !delivering, store.connection == .connected else { return }
        delivering = true
        defer { delivering = false }
        for handoff in ShareInbox.pending(in: inbox) {
            guard store.connection == .connected else { return }
            let message = ShareInbox.message(of: handoff, in: inbox)
            // A share leaves the folder before its send, so a share that fails never goes a second time.
            ShareInbox.remove(handoff, in: inbox)
            guard let message else {
                store.error = "Coconut could not read the files of a share. Share them again."
                continue
            }
            if let sent = await send(message, to: handoff.destination, store: store) {
                show(sent, model: model)
            }
        }
    }

    /// The chat that got the message, or nil when it did not go. A share to a project goes to a new chat of the
    /// project, as the first message of the message box does (chatForFirstMessage). A failure leaves its text in the
    /// banner of the window.
    static func send(_ message: ShareMessage, to destination: ShareDestination, store: AppStore) async -> ShareDestination? {
        if let chat = ShareDirectoryBuilder.chatKey(destination) {
            var files: [TeamFile] = []
            for file in message.files {
                guard let draft = await store.uploadTeamFile(chat, PickedFile(data: file.data, name: file.name)) else { return nil }
                files.append(draft)
            }
            return store.sendTeamMessage(chat, text: message.text, replyingTo: nil, files: files) ? destination : nil
        }
        guard var bot = store.bots.first(where: { $0.id == destination.bot }) else {
            store.error = "The agent of this share is gone. Share again and choose an agent."
            return nil
        }
        // Each file fits before anything goes, so a file that cannot go leaves no new chat behind.
        var fitted: [FittedFile] = []
        for file in message.files {
            // The Mac of the person gets an image with its metadata, as from the composer of the agent chat.
            let outcome = await FileFit.fitted(PickedFile(data: file.data, name: file.name), removesPosition: false)
            guard let file = outcome.file else {
                store.error = outcome.text
                return nil
            }
            fitted.append(file)
        }
        var sent = destination
        if destination.kind == .project {
            guard let chat = await store.chatForFirstMessage(in: bot) else { return nil }
            bot = chat
            sent = destination.newChat(chat.id)
        }
        var attachments: [String] = []
        for file in fitted {
            guard let artifact = await store.uploadAttachment(bot: bot, data: file.data, filename: file.name) else { return nil }
            attachments.append(artifact.id)
        }
        return await store.send(bot: bot, text: message.text, attachments: attachments) ? sent : nil
    }

    private static func show(_ destination: ShareDestination, model: AppModel) {
        if let chat = ShareDirectoryBuilder.chatKey(destination) { model.shownChat = chat } else { model.shownBot = destination.bot }
    }
}

/// Sends the waiting shares when the window shows and each time the app reaches the paired Mac.
struct MacShareDelivery: ViewModifier {
    @Environment(AppStore.self) private var store
    @ObservedObject var model: AppModel

    func body(content: Content) -> some View {
        content.task(id: store.connection) {
            guard model.mode == .full, !TestIsolation.active else { return }
            await MacShareInbox.deliver(store: store, model: model)
        }
    }
}
