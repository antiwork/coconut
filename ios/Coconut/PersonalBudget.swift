import SwiftUI

/// The Personal budget: Antiwork pays for the model use of Personal up to a monthly limit (docs/personal-budget.md).
/// The Mac sends it in the state as `personal_budget`. An older Mac sends nothing. The fields keep the names of the
/// sign-in that the budget once had: `signedIn` means the budget is on.
struct PersonalBudget: Codable, Equatable, Sendable {
    var signedIn: Bool? = nil
    /// The Personal project. Its chats show the budget.
    var botId: String? = nil
    var month: String? = nil
    var spentUsd: Double? = nil
    var limitUsd: Double? = nil
    var paused: Bool? = nil
    var resetsAt: String? = nil
    /// The relay refused the token: the person turns the budget on again.
    var needsSignIn: Bool? = nil

    var isSignedIn: Bool { signedIn == true }
    var isPaused: Bool { isSignedIn && paused == true }
    var wantsTurnOn: Bool { !isSignedIn || needsSignIn == true }

    func shows(in bot: Bot) -> Bool {
        guard let botId else { return false }
        return bot.id == botId || bot.chatOf == botId
    }

    private static func dollars(_ value: Double, cents: Bool) -> String {
        value.formatted(.currency(code: "USD").precision(.fractionLength(cents ? 2 : 0)))
    }

    /// "December 1", the day the budget starts again, in UTC as the ledger of the relay.
    var resetDay: String? {
        guard let resetsAt, let date = resetsAt.apiDate else { return nil }
        var style = Date.FormatStyle(timeZone: TimeZone(identifier: "UTC")!).month(.wide).day()
        style.locale = Locale.current
        return date.formatted(style)
    }

    var line: String {
        guard isSignedIn else { return "Antiwork pays for the model use of Personal each month." }
        if needsSignIn == true { return "The Antiwork budget stopped on this Mac." }
        let limit = Self.dollars(limitUsd ?? 0, cents: false)
        if isPaused {
            return "Paused: \(limit) used this month." + (resetDay.map { " It starts again on \($0)." } ?? "")
        }
        let spent = spentUsd ?? 0
        return "\(Self.dollars(spent, cents: spent > 0 && spent < 10)) of \(limit) this month"
    }
}

/// The line of the budget at the end of a Personal chat, with a button that turns the budget on. Antiwork trusts
/// people, so the button asks for no sign-in, and the iPhone app and the Mac app both show it.
struct PersonalBudgetFooter: View {
    let budget: PersonalBudget
    @Environment(AppStore.self) private var store
    @State private var working = false
    @State private var failure: String?

    var body: some View {
        VStack(spacing: BoardStyle.Space.xs) {
            Text(budget.line)
                .font(.caption2).monospacedDigit()
                .foregroundStyle(budget.isPaused ? .secondary : .tertiary)
                .multilineTextAlignment(.center)
            if budget.wantsTurnOn {
                // A SwiftUI button: the tap of the chat that drops the keyboard cancels the touch of a UIKit control.
                Button("Use the Antiwork budget") { Task { await turn(on: true) } }
                    .buttonStyle(CoconutButtonStyle())
                    .disabled(working)
                    .accessibilityIdentifier("personal-budget-sign-in")
            }
            if let failure {
                Text(failure).font(.caption2).foregroundStyle(.red).multilineTextAlignment(.center)
            }
        }
        .frame(maxWidth: .infinity, minHeight: 28)
        .padding(.vertical, BoardStyle.Space.xs)
        .contentShape(Rectangle())
        .contextMenu {
            if budget.isSignedIn {
                Button("Stop using the Antiwork budget", role: .destructive) { Task { await turn(on: false) } }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("personal-budget")
    }

    private func turn(on: Bool) async {
        failure = nil
        working = true
        defer { working = false }
        do { try await store.personalBudget(on: on) }
        catch { failure = error.localizedDescription }
    }
}
