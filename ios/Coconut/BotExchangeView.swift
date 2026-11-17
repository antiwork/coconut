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
    /// Whether the row is the first of its calendar day. The chat shows the day separator above such a row.
    var startsDay = false
    var id: String { messages[0].id + (isExchange ? "-exchange" : "") }
    var message: Message { messages[0] }

    /// The row that shows a message. Photos that a bot sends one after another share the row of the first photo.
    static func rowId(for id: String, in entries: [TranscriptEntry]) -> String {
        entries.first { $0.photos.count > 1 && $0.messages.dropFirst().contains { $0.id == id } }?.id ?? id
    }

    /// A carousel or an exchange never crosses midnight: a row belongs to one calendar day, so the day separator
    /// above it is the only one that day needs. The first row of each day has `startsDay`.
    static func grouped(_ messages: [Message], botId: String? = nil,
                        calendar: Calendar = .current) -> [TranscriptEntry] {
        var entries: [TranscriptEntry] = []
        for message in messages {
            if !message.isBotExchange && (botId == nil || message.botId == botId) {
                if let photo = message.photo {
                    if let last = entries.last, !last.photos.isEmpty, last.message.photoSender == message.photoSender,
                       DaySeparator.sameDay(last.message.createdAt, message.createdAt, calendar: calendar) {
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
               last.message.exchangeBotIds == message.exchangeBotIds,
               DaySeparator.sameDay(last.message.createdAt, message.createdAt, calendar: calendar) {
                entries[entries.count - 1].messages.append(message)
            } else {
                entries.append(TranscriptEntry(messages: [message], isExchange: true))
            }
        }
        // A row without a date starts no day. The next row compares its day with the last row that has a date.
        var previous: Date?
        for index in entries.indices {
            guard let date = entries[index].message.createdAt.apiDate else { continue }
            entries[index].startsDay = previous.map { !calendar.isDate(date, inSameDayAs: $0) } ?? true
            previous = date
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
    @Environment(\.pushRoute) private var pushRoute
    let messages: [Message]
    let botId: String

    private var message: Message { messages[0] }
    private var peerId: String { message.exchangeBotIds.first { $0 != botId } ?? "" }
    private var peer: Bot? { store.bots.first { $0.id == peerId } }
    private var prefix: String {
        messages.count > 1 ? "\(messages.count) messages with" : message.senderBotId == botId ? "Messaged" : "Message from"
    }

    var body: some View {
        // The bot is a chip that opens the exchange, with no chevron. On an iPhone the chip pushes the page itself: a
        // list row with a navigation link gets a chevron of its own and did not open on a tap.
        let route = BotExchangeRoute(botIds: message.exchangeBotIds, messageId: message.id)
        HStack(spacing: 4) {
            Text(prefix).foregroundStyle(.secondary)
            Group {
                if let pushRoute {
                    Button { pushRoute(route) } label: { chip }
                } else {
                    NavigationLink(value: route) { chip }
                }
            }
            .buttonStyle(ExchangeChipStyle())
            .accessibilityLabel("\(prefix) \(peer?.name ?? "Deleted agent")")
            .accessibilityIdentifier("bot-exchange-\(message.id)")
        }
        .font(.footnote)
        .frame(maxWidth: .infinity, minHeight: 44)
    }

    private var chip: some View {
        HStack(spacing: BoardStyle.Space.xs) {
            Text(peer?.name ?? "Deleted agent").lineLimit(1)
        }
    }
}

/// A bot chip: the name in the secondary color. A pointer over it, or a press, gives it a soft capsule.
private struct ExchangeChipStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View { ExchangeChip(configuration: configuration) }
}

private struct ExchangeChip: View {
    let configuration: ButtonStyleConfiguration
    @State private var hovering = false

    var body: some View {
        let lit = hovering || configuration.isPressed
        configuration.label
            .foregroundStyle(lit ? .primary : .secondary)
            .padding(.leading, 4).padding(.trailing, 8).padding(.vertical, 4)
            .background(Capsule().fill(Color.primary.opacity(lit ? 0.1 : 0)))
            // The tap area is at least 44 points tall, more than the chip.
            .frame(minHeight: 44)
            .contentShape(Rectangle())
            .onHover { hovering = $0 }
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
                        VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
                            HStack(spacing: BoardStyle.Space.s) {
                                if let bot = store.bots.first(where: { $0.id == message.senderBotId }) {
                                    Text(bot.name)
                                } else { Text("Deleted agent") }
                                Spacer()
                                if let date = message.createdAt.apiDate {
                                    Text(date.formatted(date: .abbreviated, time: .shortened))
                                }
                            }.font(.footnote).foregroundStyle(.secondary).padding(.horizontal, BoardStyle.Space.m)
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
                    Label("Read-only", systemImage: "lock").font(.footnote).foregroundStyle(.secondary)
                    Spacer()
                    if !nearBottom {
                        Button { proxy.scrollTo("exchange-bottom", anchor: .bottom) } label: {
                            Image(systemName: "arrow.down").frame(minWidth: 44, minHeight: 44)
                        }.accessibilityLabel("Latest messages")
                    }
                }.frame(minHeight: 44).padding(.horizontal, 16).background(.bar)
            }
        }
        // "Load earlier messages" can fail.
        .pageErrorBanner()
        .toolbar {
            ToolbarItem(placement: .principal) {
                Text(route.botIds.map { id in store.bots.first { $0.id == id }?.name ?? "Deleted agent" }.joined(separator: " and "))
                    .font(.headline).lineLimit(1)
            }
        }
        .inlineNavigationTitle()
    }
}
