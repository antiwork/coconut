import SwiftUI
#if os(iOS)
import AuthenticationServices
#endif

/// The Personal budget: Antiwork pays for the model use of Personal up to a monthly limit (docs/personal-budget.md).
/// The Mac sends it in the state as `personal_budget`. An older Mac sends nothing.
struct PersonalBudget: Codable, Equatable, Sendable {
    var signedIn: Bool? = nil
    /// The Personal project. Its chats show the budget.
    var botId: String? = nil
    var month: String? = nil
    var spentUsd: Double? = nil
    var limitUsd: Double? = nil
    var paused: Bool? = nil
    var resetsAt: String? = nil
    /// The relay refused the token: the person signs in again.
    var needsSignIn: Bool? = nil

    var isSignedIn: Bool { signedIn == true }
    var isPaused: Bool { isSignedIn && paused == true }
    var wantsSignIn: Bool { !isSignedIn || needsSignIn == true }

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
        if needsSignIn == true { return "Sign in again to keep using the Antiwork budget." }
        let limit = Self.dollars(limitUsd ?? 0, cents: false)
        if isPaused {
            return "Paused: \(limit) used this month." + (resetDay.map { " It starts again on \($0)." } ?? "")
        }
        let spent = spentUsd ?? 0
        return "\(Self.dollars(spent, cents: spent > 0 && spent < 10)) of \(limit) this month"
    }
}

/// The line of the budget at the end of a Personal chat, with Sign in with Apple when the person has not signed in.
/// The Mac app shows the line only: Sign in with Apple belongs to the iPhone app.
struct PersonalBudgetFooter: View {
    let budget: PersonalBudget
    @Environment(AppStore.self) private var store
    @Environment(\.colorScheme) private var colorScheme
    @State private var working = false
    @State private var failure: String?

    var body: some View {
        VStack(spacing: BoardStyle.Space.xs) {
            Text(budget.line)
                .font(.caption2).monospacedDigit()
                .foregroundStyle(budget.isPaused ? .secondary : .tertiary)
                .multilineTextAlignment(.center)
            if budget.wantsSignIn {
                #if os(iOS)
                SignInWithAppleButton(.signIn) { request in
                    request.requestedScopes = []
                } onCompletion: { result in
                    Task { await signIn(result) }
                }
                .signInWithAppleButtonStyle(colorScheme == .dark ? .white : .black)
                .frame(maxWidth: 240, minHeight: 36, maxHeight: 36)
                .disabled(working)
                .accessibilityIdentifier("personal-budget-sign-in")
                #else
                Text("Sign in with Apple in Coconut on your iPhone.")
                    .font(.caption2).foregroundStyle(.tertiary)
                #endif
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
                Button("Sign Out of the Antiwork Budget", role: .destructive) { Task { await signOut() } }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("personal-budget")
    }

    #if os(iOS)
    private func signIn(_ result: Result<ASAuthorization, Error>) async {
        failure = nil
        switch result {
        case .success(let authorization):
            guard let credential = authorization.credential as? ASAuthorizationAppleIDCredential,
                  let data = credential.identityToken, let token = String(data: data, encoding: .utf8) else {
                failure = "Apple did not confirm the sign-in. Try again."
                return
            }
            working = true
            defer { working = false }
            do { try await store.personalBudget(signIn: token) }
            catch { failure = error.localizedDescription }
        case .failure(let error):
            // Closing the Apple sheet is no failure.
            if (error as? ASAuthorizationError)?.code != .canceled { failure = error.localizedDescription }
        }
    }
    #endif

    private func signOut() async {
        failure = nil
        working = true
        defer { working = false }
        do { try await store.personalBudget(signIn: nil) }
        catch { failure = error.localizedDescription }
    }
}
