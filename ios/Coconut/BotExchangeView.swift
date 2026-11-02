import SwiftUI

struct BotExchangeRoute: Hashable {
    let botIds: [String]
    let messageId: String
}

struct TranscriptEntry: Identifiable {
    var messages: [Message]
    var isExchange: Bool = false
    /// The photos of consecutive photo messages from one sender. They show as one carousel.
    var photos: [MessagePhoto] = []
    var id: String { messages[0].id + (isExchange ? "-exchange" : "") }
    var message: Message { messages[0] }

    /// The row that shows a message. Photos that a bot sends one after another share the row of the first photo.
    static func rowId(for id: String, in entries: [TranscriptEntry]) -> String {
        entries.first { $0.photos.count > 1 && $0.messages.dropFirst().contains { $0.id == id } }?.id ?? id
    }

    static func grouped(_ messages: [Message], botId: String? = nil) -> [TranscriptEntry] {
        var entries: [TranscriptEntry] = []
        for message in messages {
            if !message.isBotExchange && (botId == nil || message.botId == botId) {
                if let photo = message.photo {
                    if let last = entries.last, !last.photos.isEmpty, last.message.photoSender == message.photoSender {
                        entries[entries.count - 1].messages.append(message)
                        entries[entries.count - 1].photos.append(photo)
                    } else {
                        entries.append(TranscriptEntry(messages: [message], photos: [photo]))
                    }
                } else {
                    entries.append(TranscriptEntry(messages: [message]))
                }
            }
            guard message.isBotExchange else { continue }
            if let last = entries.last, last.isExchange,
               last.message.exchangeBotIds == message.exchangeBotIds {
                entries[entries.count - 1].messages.append(message)
            } else {
                entries.append(TranscriptEntry(messages: [message], isExchange: true))
            }
        }
        return entries
    }
}

extension Message {
    var isBotExchange: Bool { kind == "bot_exchange" || !(exchangeRecipientBotIds ?? []).isEmpty }
    var exchangeBotIds: [String] {
        Array(Set((exchangeRecipientBotIds ?? recipientBotIds ?? []) + [senderBotId ?? botId])).sorted()
    }
}

struct BotExchangeRow: View {
    @Environment(AppStore.self) private var store
    let messages: [Message]
    let botId: String

    private var message: Message { messages[0] }
    private var peerId: String { message.exchangeBotIds.first { $0 != botId } ?? "" }
    private var peer: Bot? { store.bots.first { $0.id == peerId } }
    private var prefix: String {
        messages.count > 1 ? "\(messages.count) messages with" : message.senderBotId == botId ? "Messaged" : "Message from"
    }

    var body: some View {
        NavigationLink(value: BotExchangeRoute(botIds: message.exchangeBotIds, messageId: message.id)) {
            HStack(spacing: 5) {
                Text(prefix)
                if let peer { BotAvatar(bot: peer, size: 17) }
                Text(peer?.name ?? "Deleted bot").lineLimit(1)
                Image(systemName: "chevron.right").font(.caption2)
            }
            .font(.caption).foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, minHeight: 44)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("bot-exchange-\(message.id)")
    }
}

struct BotExchangeView: View {
    @Environment(AppStore.self) private var store
    let route: BotExchangeRoute
    @State private var nearBottom = true
    @State private var positioned = false

    private var messages: [Message] {
        store.messages.filter { $0.isBotExchange && $0.exchangeBotIds == route.botIds && !TranscriptPresentation.isSilenceControl($0) }
            .sorted { $0.createdAt < $1.createdAt }
    }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if route.botIds.contains(where: store.hasOlderMessages) {
                        Button("Load earlier messages") {
                            let anchor = messages.first?.id
                            nearBottom = false
                            Task {
                                for botId in route.botIds { await store.loadOlderMessages(botId) }
                                await Task.yield()
                                if let anchor { proxy.scrollTo(anchor, anchor: .top) }
                            }
                        }
                        .frame(maxWidth: .infinity, minHeight: 44)
                        .disabled(route.botIds.contains { store.historyLoading.contains($0) })
                    }
                    ForEach(messages) { message in
                        VStack(alignment: .leading, spacing: 5) {
                            HStack(spacing: 6) {
                                if let bot = store.bots.first(where: { $0.id == message.senderBotId }) {
                                    BotAvatar(bot: bot, size: 20)
                                    Text(bot.name)
                                } else { Text("Deleted bot") }
                                Spacer()
                                if let date = message.createdAt.apiDate {
                                    Text(date.formatted(date: .abbreviated, time: .shortened))
                                }
                            }.font(.caption).foregroundStyle(.secondary).padding(.horizontal, 10)
                            MessageBubble(message: message)
                        }.id(message.id)
                    }
                    Color.clear.frame(height: 1).id("exchange-bottom")
                }.padding(16)
            }
            .defaultScrollAnchor(.bottom)
            .background(BoardStyle.paper)
            .onScrollGeometryChange(for: Bool.self) { geometry in
                geometry.visibleRect.maxY - geometry.contentInsets.bottom >= geometry.contentSize.height - 80
            } action: { _, value in nearBottom = value }
            .onChange(of: messages.last?.id) { _, _ in
                if nearBottom { proxy.scrollTo("exchange-bottom", anchor: .bottom) }
            }
            .task(id: messages.isEmpty) {
                guard !positioned, !messages.isEmpty else { return }
                await Task.yield()
                guard !Task.isCancelled else { return }
                proxy.scrollTo("exchange-bottom", anchor: .bottom)
                positioned = true
            }
            .safeAreaInset(edge: .bottom) {
                HStack {
                    Spacer()
                    Label("Read-only", systemImage: "lock").font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    if !nearBottom {
                        Button { proxy.scrollTo("exchange-bottom", anchor: .bottom) } label: {
                            Image(systemName: "arrow.down").frame(minWidth: 44, minHeight: 44)
                        }.accessibilityLabel("Latest messages")
                    }
                }.frame(minHeight: 44).padding(.horizontal, 16).background(.bar)
            }
        }
        .toolbar {
            ToolbarItem(placement: .principal) {
                HStack(spacing: 8) {
                    ForEach(route.botIds, id: \.self) { id in
                        if let bot = store.bots.first(where: { $0.id == id }) {
                            BotAvatar(bot: bot, size: 30)
                        }
                    }
                }.accessibilityLabel(route.botIds.map { id in store.bots.first { $0.id == id }?.name ?? "Deleted bot" }.joined(separator: " and "))
            }
        }
        .navigationBarTitleDisplayMode(.inline)
    }
}
