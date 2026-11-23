import SwiftUI

/// The tokens and the cost of one chat since its start (`GET /v1/bots/<id>/usage`).
struct ChatUsageTotal: Codable, Equatable, Sendable {
    var inputTokens = 0
    var outputTokens = 0
    var cacheReadTokens = 0
    var cacheWriteTokens = 0
    var reasoningTokens = 0
    var costUsd = 0.0
    var models: [UsageModel] = []
    var costKnown = false
    var costEstimated = false

    var tokens: Int { inputTokens + outputTokens + cacheReadTokens + cacheWriteTokens }
}

/// One turn of a chat: the model that ran it and its share of the tokens and the cost of its session. Hermes stores
/// the cost for each session, so the Mac gives a turn the share of the model calls that it made.
struct ChatTurnSpend: Codable, Equatable, Sendable {
    /// The time of the first and of the last message of the turn, in seconds since 1970.
    var startedAt: Double
    var endedAt: Double
    var model: String
    var provider: String = ""
    var modelCalls = 0
    var tokens = 0
    var costUsd = 0.0
    var costKnown = false
    var costEstimated = false
}

/// The usage of one chat: the total since its start, and its last days.
struct ChatUsage: Codable, Equatable, Sendable {
    var botId: String
    var today: String
    var days: [UsageDay]
    var total: ChatUsageTotal
    /// The newest turns of the chat, oldest first. A Mac before this field sends none.
    var turns: [ChatTurnSpend]? = nil

    /// "$2.41 · 184k tokens": the cost first and the tokens second, as on the Usage page. The cost is left out when
    /// Hermes has no price for the model.
    var line: String {
        let tokens = UsageMath.count(total.tokens) + " tokens"
        guard total.costKnown else { return tokens }
        return (total.costEstimated ? "~" : "") + UsageMath.cost(total.costUsd) + " · " + tokens
    }

    func day(_ day: String) -> UsageDay { days.first { $0.day == day } ?? UsageDay(day: day) }

    /// The last seven days of the report, together.
    var week: UsageDay { UsageMath.sum(Array(days.suffix(7))) }

    static func preview(botId: String) -> ChatUsage {
        let today = UsageMath.day(Date())
        let day = UsageDay(day: today, turns: 6, modelCalls: 41, toolCalls: 63, workingSeconds: 1140, inputTokens: 21_400,
                           outputTokens: 8_900, cacheReadTokens: 151_000, cacheWriteTokens: 3_100, costUsd: 2.41)
        let model = UsageModel(model: "claude-opus-5-5", provider: "anthropic", inputTokens: 21_400, outputTokens: 8_900,
                               cacheReadTokens: 151_000, cacheWriteTokens: 3_100, costUsd: 2.41)
        return ChatUsage(botId: botId, today: today, days: [day],
                         total: ChatUsageTotal(inputTokens: 21_400, outputTokens: 8_900, cacheReadTokens: 151_000,
                                               cacheWriteTokens: 3_100, costUsd: 2.41, models: [model], costKnown: true,
                                               costEstimated: true))
    }
}

/// The cost of a turn on its last reply, next to its time and its model: "Opus 5.5 · $0.42".
enum ReplySpend {
    /// A reply that lands this long after the last message of its turn still belongs to it.
    static let slack = 120.0

    /// The note of each reply that ends a turn, by message ID. A turn of the chat with several replies shows its
    /// note on the newest one, and a reply outside every turn shows none.
    static func notes(messages: [Message], usage: ChatUsage?, modelName: (String) -> String) -> [String: String] {
        guard let turns = usage?.turns, !turns.isEmpty else { return [:] }
        var lastReply: [Int: (id: String, at: Double)] = [:]
        for message in messages where message.role == "assistant" && message.kind == "text" && !message.streaming {
            guard let at = message.createdAt.apiDate?.timeIntervalSince1970 else { continue }
            // The newest turn that began before the reply, if the reply is within its slack.
            guard let index = turns.lastIndex(where: { $0.startedAt <= at + 1 }), at <= turns[index].endedAt + slack else { continue }
            if lastReply[index].map({ at >= $0.at }) ?? true { lastReply[index] = (message.id, at) }
        }
        var result: [String: String] = [:]
        for (index, reply) in lastReply {
            if let note = note(turns[index], modelName: modelName) { result[reply.id] = note }
        }
        return result
    }

    static func note(_ turn: ChatTurnSpend, modelName: (String) -> String) -> String? {
        var parts: [String] = []
        if !turn.model.isEmpty { parts.append(modelName(turn.model)) }
        if turn.costKnown { parts.append((turn.costEstimated ? "~" : "") + UsageMath.cost(turn.costUsd)) }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }
}

private struct ReplySpendKey: EnvironmentKey {
    static let defaultValue: [String: String] = [:]
}

extension EnvironmentValues {
    /// The cost note of the replies of the open chat, by message ID.
    var replySpend: [String: String] {
        get { self[ReplySpendKey.self] }
        set { self[ReplySpendKey.self] = newValue }
    }
}

/// The last row of a chat: its cost and its tokens. A tap opens where they went.
struct ChatSpendFooter: View {
    let usage: ChatUsage
    @State private var open = false

    var body: some View {
        Button { open = true } label: {
            Text(usage.line)
                .font(.caption2).monospacedDigit()
                .foregroundStyle(.tertiary)
                .frame(maxWidth: .infinity, minHeight: 28)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("This agent used " + usage.line.replacingOccurrences(of: " · ", with: ", "))
        .accessibilityHint("Shows the cost and the tokens by model and by day")
        .accessibilityIdentifier("chat-spend")
        .sheet(isPresented: $open) { ChatSpendSheet(usage: usage) }
    }
}

/// Where the cost and the tokens of a chat went: by period, by kind of token, and by model. Each value has the cost
/// first and the tokens second.
struct ChatSpendSheet: View {
    let usage: ChatUsage
    @Environment(\.dismiss) private var dismiss

    private var total: ChatUsageTotal { usage.total }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    LabeledContent("Today", value: period(usage.day(usage.today)))
                    LabeledContent("7 days", value: period(usage.week))
                    LabeledContent("All", value: spend(total.costUsd, total.tokens))
                }
                Section("Tokens") {
                    row("Input", total.inputTokens)
                    row("Cache read", total.cacheReadTokens)
                    row("Cache write", total.cacheWriteTokens)
                    row("Output", total.outputTokens)
                    if total.reasoningTokens > 0 { row("Reasoning", total.reasoningTokens) }
                }
                if !total.models.isEmpty {
                    Section("Models") {
                        ForEach(total.models) { model in
                            LabeledContent(model.model) {
                                Text(spend(model.costUsd, model.tokens))
                                    .monospacedDigit()
                            }
                        }
                    }
                }
            }
            .formStyle(.grouped)
            .navigationTitle(cost(total.costUsd) ?? UsageMath.count(total.tokens) + " tokens")
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
        }
        #if os(macOS)
        .frame(minWidth: 360, minHeight: 420)
        #endif
        .presentationDetents([.medium, .large])
        .accessibilityIdentifier("chat-spend-sheet")
    }

    private func cost(_ value: Double) -> String? {
        total.costKnown ? (total.costEstimated ? "~" : "") + UsageMath.cost(value) : nil
    }

    private func period(_ day: UsageDay) -> String { spend(day.costUsd, day.tokens) }

    /// "$2.41 · 184k tokens", or the tokens alone without a price.
    private func spend(_ value: Double, _ tokens: Int) -> String {
        (cost(value).map { $0 + " · " } ?? "") + UsageMath.count(tokens) + " tokens"
    }

    private func row(_ label: String, _ value: Int) -> some View {
        LabeledContent(label) { Text(UsageMath.count(value)).monospacedDigit() }
    }
}
