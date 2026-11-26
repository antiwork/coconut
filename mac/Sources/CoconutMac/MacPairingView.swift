import SwiftUI

/// The pairing page of the Mac client. When this Mac runs the Coconut service, one click pairs the client with it
/// through the relay, as an iPhone pairs. Any other Mac pairs with a code that the person sends to Hermes, or with a
/// pairing link, as on the iPhone (OnboardingView).
struct MacPairingView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var model: AppModel
    @State private var pairingHere = false
    @State private var failure: String?

    private var servesHere: Bool { model.status.service == .running }

    var body: some View {
        VStack(spacing: 0) {
            if servesHere {
                VStack(alignment: .leading, spacing: BoardStyle.Space.m) {
                    Text("This Mac runs Coconut").font(.body.weight(.semibold))
                    Text("Use the agents of this Mac here. Coconut pairs this window with the service on this Mac.")
                        .foregroundStyle(.secondary)
                    HStack {
                        Button {
                            Task { await pairHere() }
                        } label: {
                            // The button keeps its width while it pairs.
                            Text("Use this Mac")
                                .opacity(pairingHere ? 0 : 1)
                                .overlay { if pairingHere { CoconutLoader(.row, label: "Pairing") } }
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(pairingHere)
                        .accessibilityIdentifier("pair-this-mac")
                        if let failure { Text(failure).font(.subheadline).foregroundStyle(BoardStyle.destructive) }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(BoardStyle.Space.l)
                .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.medium))
                .padding([.horizontal, .top], BoardStyle.Space.l)
            }
            OnboardingView(firstRun: !store.hasPairing)
        }
        // As a sheet, the page hides the notice of the window (sheetShowsError), so it shows the error of another
        // action itself. A failure of the pairing shows only on the page: the store keeps no second copy.
        .pageErrorBanner()
        .onChange(of: store.hasPairing) { _, paired in if paired { dismiss() } }
    }

    /// Makes a one-time pairing link with the service of this Mac, and pairs with it at once.
    private func pairHere() async {
        pairingHere = true
        failure = nil
        defer { pairingHere = false }
        await model.pair()
        guard let link = model.pairing?.link else {
            failure = model.pairingError ?? "This Mac could not make a pairing link."
            return
        }
        do {
            let setup = try PairingSetup.parse(link)
            if !(await store.pair(setup: setup)) {
                failure = store.error ?? "This Mac could not pair."
                store.error = nil
            }
        } catch {
            failure = "This Mac made a pairing link that Coconut cannot read."
        }
    }
}
