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

/// The usage of one chat: the total since its start, and its last days.
struct ChatUsage: Codable, Equatable, Sendable {
    var botId: String
    var today: String
    var days: [UsageDay]
    var total: ChatUsageTotal

    /// "184k tokens · $2.41". The cost is left out when Hermes has no price for the model.
    var line: String {
        var text = UsageMath.count(total.tokens) + " tokens"
        if total.costKnown { text += " · " + (total.costEstimated ? "~" : "") + UsageMath.cost(total.costUsd) }
        return text
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

/// The last row of a chat: its tokens and its cost. A tap opens where they went.
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
        .accessibilityLabel("This chat used " + usage.line.replacingOccurrences(of: " · ", with: ", "))
        .accessibilityHint("Shows the tokens and the cost by model and by day")
        .accessibilityIdentifier("chat-spend")
        .sheet(isPresented: $open) { ChatSpendSheet(usage: usage) }
    }
}

/// Where the tokens and the cost of a chat went: by period, by kind of token, and by model.
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
                    LabeledContent("All", value: UsageMath.count(total.tokens) + (cost(total.costUsd).map { " · " + $0 } ?? ""))
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
                                Text(UsageMath.count(model.tokens) + (cost(model.costUsd).map { " · " + $0 } ?? ""))
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

    private func period(_ day: UsageDay) -> String {
        UsageMath.count(day.tokens) + (cost(day.costUsd).map { " · " + $0 } ?? "")
    }

    private func row(_ label: String, _ value: Int) -> some View {
        LabeledContent(label) { Text(UsageMath.count(value)).monospacedDigit() }
    }
}
