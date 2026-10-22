import SwiftUI
import UserNotifications

enum OnboardingStep: Int, Hashable, Comparable {
    case welcome, pair, setup, bots, logins, mcp, model, notifications, done

    static func < (a: Self, b: Self) -> Bool { a.rawValue < b.rawValue }
}

// First run and Settings → Hermes setup share these pages. Each page has one title, one live
// illustration, and one primary action. The Mac copies only what the person chooses.
struct OnboardingView: View {
    static let completedKey = "coconut.onboarding.completed"

    private enum SetupLoad: Equatable {
        case loading
        case loaded(HermesSetup)
        // The Mac did not answer. This is not the same as a Mac without Hermes.
        case failed

        var setup: HermesSetup? { if case .loaded(let setup) = self { setup } else { nil } }
        var finished: Bool { if case .loaded = self { true } else { false } }
    }
    // The Mac accepts at most this many profiles in one import request.
    private static let importBatch = 20

    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let start: OnboardingStep
    @State private var step: OnboardingStep
    @State private var history: [OnboardingStep] = []
    @State private var load = SetupLoad.loading
    @State private var selected: Set<String> = []
    @State private var action: Task<Void, Never>?
    @State private var modelOpen = false
    @State private var askNotifications = false
    @State private var notificationsOn = false
    @State private var importError: String?
    @State private var skippedSkills: [String] = []
    @State private var serversNeedingSetup: [String] = []

    init(start: OnboardingStep = .welcome) {
        self.start = start
        _step = State(initialValue: start)
    }

    static func showsOnLaunch(_ store: AppStore) -> Bool {
        #if DEBUG && targetEnvironment(simulator)
        // Test servers pair the simulator on launch.
        if ProcessInfo.processInfo.environment["COCONUT_SERVER_URL"] != nil { return false }
        #endif
        return !store.isDemo && !store.hasPairing && !UserDefaults.standard.bool(forKey: completedKey)
    }

    private var setup: HermesSetup? { load.setup }
    private var newServers: [HermesMCPServer] { setup?.newMCPServers(existing: store.mcpServers) ?? [] }
    // The setup's default model, when Coconut can use the same route.
    private var setupModel: ModelOverride? { setup?.modelChoice(in: store.modelCatalog) }
    private var working: Bool { action != nil }

    private var steps: [OnboardingStep] {
        var steps: [OnboardingStep] = start == .welcome ? [.welcome, .pair, .setup] : [.setup]
        if setup?.importable.isEmpty == false { steps.append(.bots) }
        if setup.map({ !$0.logins.isEmpty || !$0.signIns.isEmpty }) == true { steps.append(.logins) }
        if !newServers.isEmpty { steps.append(.mcp) }
        if !store.modelConfigured { steps.append(.model) }
        if askNotifications { steps.append(.notifications) }
        steps.append(.done)
        return steps
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            page
                .id(step)
                .transition(.opacity)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
            footer
        }
        .padding(.horizontal, 24)
        .padding(.bottom, 12)
        .background(BoardStyle.paper.ignoresSafeArea())
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.3), value: step)
        .sheet(isPresented: $modelOpen) {
            NavigationStack {
                ModelSettingsView()
                    .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { modelOpen = false } } }
            }
        }
        .task {
            await PushNotifications.shared.refresh()
            askNotifications = PushNotifications.shared.authorization == .notDetermined
        }
        .onChange(of: store.hasPairing) { _, paired in
            if paired && step == .pair { advance() }
        }
        .onDisappear { action?.cancel() }
        .accessibilityIdentifier("onboarding")
    }

    private var header: some View {
        let steps = steps
        // A step can leave the list while it shows. The bar then stays on the step before it.
        let index = steps.firstIndex(of: step) ?? max(0, (steps.lastIndex { $0 < step } ?? 0))
        return HStack(spacing: 12) {
            Button { back() } label: {
                Image(systemName: "chevron.left").font(.system(size: 17, weight: .medium)).frame(width: 44, height: 44)
            }
            .opacity(history.isEmpty || step == .done ? 0 : 1)
            .disabled(history.isEmpty || step == .done || working)
            .accessibilityLabel("Back")
            ProgressSegments(count: steps.count, index: index)
            Button { finish() } label: {
                Image(systemName: "xmark").font(.system(size: 15, weight: .medium)).frame(width: 44, height: 44)
            }
            .opacity(start == .welcome || step == .done ? 0 : 1)
            .disabled(start == .welcome || step == .done)
            .accessibilityLabel("Close")
        }
        .foregroundStyle(.primary)
        .padding(.top, 4)
    }

    @ViewBuilder private var page: some View {
        switch step {
        case .welcome:
            OnboardingPage(title: "Bots that keep work moving",
                           subtitle: "Coconut runs your Hermes bots on your Mac. They work in the background and reach you when they need you.") {
                WelcomeIllustration()
            }
        case .pair:
            // The pairing fields are form rows with their own scrolling and expanding details.
            OnboardingPage(title: "Connect your Mac",
                           subtitle: "On your Mac, open Pair iPhone. Then scan the code that it shows.", scrolls: false) {
                List { MacPairingFields() }
                    .scrollContentBackground(.hidden)
            }
        case .setup:
            OnboardingPage(title: setupTitle, subtitle: setupSubtitle) {
                SetupIllustration(setup: setup, loaded: load.finished)
            }
            .id(load == .failed ? "setup-failed" : "setup")
            .task(id: load == .loading) { await loadSetup() }
        case .bots:
            OnboardingPage(title: "Choose your bots",
                           subtitle: "Each profile becomes a bot with its personality, memory, and skills.") {
                VStack(spacing: 0) {
                    ForEach(setup?.profiles ?? []) { profile in
                        ProfileRow(profile: profile, selected: binding(for: profile))
                        Divider().opacity(0.6)
                    }
                }
            }
        case .logins:
            OnboardingPage(title: setup?.logins.isEmpty == false ? "Use your model keys?" : "Sign in again",
                           subtitle: loginsSubtitle) {
                VStack(alignment: .leading, spacing: 14) {
                    ForEach(Array((setup?.logins ?? []).enumerated()), id: \.element.id) { index, login in
                        StaggeredRow(index: index) { Label(login.label, systemImage: "key").font(.body) }
                    }
                    if let signIns = setup?.signIns, !signIns.isEmpty {
                        Text("Sign in again in Coconut's Hermes Terminal on your Mac for \(signIns.map(\.label).formatted()). A sign-in cannot be shared with your own Hermes.")
                            .font(.footnote).foregroundStyle(.secondary).padding(.top, 6)
                    }
                    if setup?.logins.isEmpty == false {
                        Text("The keys stay on your Mac.").font(.footnote).foregroundStyle(.secondary)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        case .mcp:
            OnboardingPage(title: "Add your MCP servers?",
                           subtitle: "Your Hermes setup uses these MCP servers. Coconut can add them for your bots. It keeps any server that it already has.") {
                VStack(alignment: .leading, spacing: 14) {
                    ForEach(Array(newServers.enumerated()), id: \.element.id) { index, server in
                        StaggeredRow(index: index) { MCPServerRow(server: server) }
                    }
                    Text("Coconut copies the keys that these servers use. A server that needs setup stays off until you finish it in Hermes on your Mac.")
                        .font(.footnote).foregroundStyle(.secondary).padding(.top, 6)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        case .model:
            OnboardingPage(title: setupModel.map { "Use \(modelName($0))?" } ?? "Choose a model", subtitle: modelSubtitle) {
                WelcomeIllustration(phases: [.thinking, .writing, .reading])
            }
            // Copied keys can add providers, so the page reads the Mac's catalog again.
            .task { await store.refreshSettings() }
        case .notifications:
            OnboardingPage(title: "Get notified",
                           subtitle: "Coconut tells you when a bot finishes or needs your answer.") {
                NotificationIllustration()
            }
        case .done:
            OnboardingPage(title: "You are all set", subtitle: doneSubtitle) {
                CompletionBurst()
            }
            .task {
                // A note about skipped skills or servers that need setup stays until the person reads it.
                guard skippedSkills.isEmpty && serversNeedingSetup.isEmpty else { return }
                try? await Task.sleep(for: .seconds(1.6))
                if !Task.isCancelled { finish() }
            }
        }
    }

    @ViewBuilder private var footer: some View {
        VStack(spacing: 4) {
            if let importError, [.bots, .logins, .mcp, .model].contains(step) {
                Text(importError).font(.footnote).foregroundStyle(BoardStyle.accent)
                    .multilineTextAlignment(.center).padding(.bottom, 6)
                    .accessibilityIdentifier("onboarding-error")
            }
            switch step {
            case .welcome:
                PrimaryButton(title: "Connect your Mac") { advance() }
                SecondaryButton(title: "Set up later", identifier: "onboarding-later") { finish() }
            case .pair:
                if store.hasPairing {
                    PrimaryButton(title: "Continue") { advance() }
                } else {
                    SecondaryButton(title: "Set up later", identifier: "onboarding-later") { finish() }
                }
            case .setup:
                if load == .failed {
                    PrimaryButton(title: "Try again") { load = .loading }
                    SecondaryButton(title: "Skip for now") { advance() }
                } else {
                    PrimaryButton(title: "Continue", enabled: load.finished) { advance() }
                }
            case .bots:
                PrimaryButton(title: selected.isEmpty ? "Skip" : selected.count == 1 ? "Add 1 bot" : "Add \(selected.count) bots",
                              enabled: !working) {
                    if setup?.logins.isEmpty == false { advance() } else { perform { await runImport(logins: false) } }
                }
            case .logins:
                if setup?.logins.isEmpty == false {
                    PrimaryButton(title: "Use them", enabled: !working) { perform { await runImport(logins: true) } }
                    SecondaryButton(title: "Not now", enabled: !working) { perform { await runImport(logins: false) } }
                } else {
                    PrimaryButton(title: "Continue", enabled: !working) { perform { await runImport(logins: false) } }
                }
            case .mcp:
                PrimaryButton(title: "Add them", enabled: !working) { perform { await importServers() } }
                SecondaryButton(title: "Not now", enabled: !working) { advance() }
            case .model:
                if let setupModel, !store.modelConfigured {
                    PrimaryButton(title: "Use \(modelName(setupModel))", enabled: !working) { perform { await useSetupModel(setupModel) } }
                    SecondaryButton(title: "Choose another model", enabled: !working) { modelOpen = true }
                } else {
                    PrimaryButton(title: "Choose a model") { modelOpen = true }
                }
                SecondaryButton(title: store.modelConfigured ? "Continue" : "Later", enabled: !working) { advance() }
            case .notifications:
                if notificationsOn {
                    Label("Notifications on", systemImage: "checkmark")
                        .font(.body.weight(.medium)).foregroundStyle(Color.green)
                        .frame(maxWidth: .infinity, minHeight: 52)
                        .background(Color.green.opacity(0.14), in: Capsule())
                        .transition(.opacity.combined(with: .scale(scale: 0.96)))
                } else {
                    PrimaryButton(title: "Turn on notifications", enabled: !working) { perform { await enableNotifications() } }
                    SecondaryButton(title: "Not now", enabled: !working) { advance() }
                }
            case .done:
                PrimaryButton(title: "Start") { finish() }
            }
        }
        .animation(reduceMotion ? nil : .spring(response: 0.35, dampingFraction: 0.8), value: notificationsOn)
    }

    private var setupTitle: String {
        if load == .failed { return "Coconut could not check your Mac" }
        guard load.finished else { return "Looking for Hermes" }
        return setup?.found == true ? "Already knows your setup" : "Coconut set up Hermes"
    }

    private var setupSubtitle: String {
        if load == .failed { return store.error ?? "Check that your Mac is on and connected, then try again." }
        guard load.finished else { return "Coconut checks your Mac for an existing Hermes setup." }
        if setup?.found == true {
            return "Coconut found Hermes on your Mac. It copies only what you choose and leaves the original unchanged."
        }
        return "This Mac has no other Hermes setup. Coconut runs its own Hermes for your bots."
    }

    private var loginsSubtitle: String {
        setup?.logins.isEmpty == false
            ? "Your Hermes setup has these model keys. Coconut can copy them for your bots. It keeps any key that it already has."
            : "Your Hermes setup uses sign-ins that Coconut cannot copy."
    }

    private var doneSubtitle: String {
        var notes: [String] = []
        if !skippedSkills.isEmpty {
            notes.append("Coconut left out \(skippedSkills.formatted()), because a skill was too large or had too many files.")
        }
        if !serversNeedingSetup.isEmpty {
            notes.append("Finish \(serversNeedingSetup.formatted()) in Hermes on your Mac. Until then, Coconut keeps these MCP servers off.")
        }
        return notes.isEmpty ? "Your bots are ready on your Mac." : (["Your bots are ready."] + notes).joined(separator: " ")
    }

    private var modelSubtitle: String {
        if setupModel != nil {
            return "Your Hermes setup uses this model. Coconut can use it as the default for your bots. You can change it later in Settings."
        }
        if let model = setup?.model {
            return "Your Hermes setup uses \(model.model), but Coconut cannot use it yet. Choose the default model for your Coconut bots."
        }
        return "Your bots need a default model. You can change it later in Settings."
    }

    private func modelName(_ selection: ModelOverride) -> String {
        store.modelCatalog?.name(for: selection) ?? selection.model
    }

    private func binding(for profile: HermesSetupProfile) -> Binding<Bool> {
        Binding(get: { selected.contains(profile.id) },
                set: { if $0 { selected.insert(profile.id) } else { selected.remove(profile.id) } })
    }

    // One action at a time. Closing the flow cancels it, so it cannot move a page that is gone.
    private func perform(_ work: @escaping @MainActor () async -> Void) {
        guard action == nil else { return }
        action = Task { @MainActor in
            await work()
            action = nil
        }
    }

    private func loadSetup() async {
        guard load == .loading else { return }
        let started = Date()
        let result = await store.loadHermesSetup()
        // Give the search a moment on screen, so the result does not flash in.
        let wait = 0.8 - Date().timeIntervalSince(started)
        if wait > 0 { try? await Task.sleep(for: .seconds(wait)) }
        guard !Task.isCancelled else { return }
        selected = Set(result?.importable.map(\.id) ?? [])
        withAnimation(reduceMotion ? nil : .easeInOut(duration: 0.3)) { load = result.map(SetupLoad.loaded) ?? .failed }
    }

    private func runImport(logins: Bool) async {
        importError = nil
        let profiles = Array(selected).sorted()
        if !profiles.isEmpty || logins {
            // The first batch also copies the keys. Later batches add only profiles.
            let batches = stride(from: 0, to: max(profiles.count, 1), by: Self.importBatch)
                .map { Array(profiles[$0..<min($0 + Self.importBatch, profiles.count)]) }
            for (index, batch) in batches.enumerated() {
                guard let result = await store.importHermesSetup(profileIds: batch, logins: logins && index == 0) else {
                    importError = store.error ?? "Coconut could not copy the setup. Try again."
                    return
                }
                guard !Task.isCancelled else { return }
                skippedSkills += result.skippedSkills ?? []
                selected.subtract(batch)
            }
            // Imported profiles now have bots. Back and Add again must not offer them twice.
            if let fresh = await store.loadHermesSetup() { load = .loaded(fresh) }
        }
        guard !Task.isCancelled else { return }
        advance()
    }

    private func importServers() async {
        importError = nil
        guard let result = await store.importHermesSetup(profileIds: [], logins: false, mcpServers: true) else {
            importError = store.error ?? "Coconut could not add the MCP servers. Try again."
            return
        }
        guard !Task.isCancelled else { return }
        serversNeedingSetup = result.mcpNeedsSetup ?? []
        advance()
    }

    private func useSetupModel(_ selection: ModelOverride) async {
        importError = nil
        guard await store.setModel(selection) else {
            importError = store.error ?? "Coconut could not set the model. Choose another model."
            return
        }
        guard !Task.isCancelled else { return }
        advance()
    }

    private func enableNotifications() async {
        await PushNotifications.shared.refresh(requestPermission: true)
        guard !Task.isCancelled else { return }
        guard PushNotifications.shared.allowed else { return advance() }
        notificationsOn = true
        try? await Task.sleep(for: .seconds(1))
        if !Task.isCancelled { advance() }
    }

    // A step can leave the list while it shows, for example when the person chooses a model.
    // The next step therefore follows the fixed order, not the position in the list.
    private func advance() {
        guard let next = steps.first(where: { $0 > step }) else { return finish() }
        // An error belongs to the page that showed it.
        importError = nil
        history.append(step)
        step = next
    }

    private func back() {
        guard let previous = history.popLast() else { return }
        step = previous
    }

    private func finish() {
        action?.cancel()
        UserDefaults.standard.set(true, forKey: Self.completedKey)
        dismiss()
    }
}

// Title, then subtitle, then illustration, as each page enters.
private struct OnboardingPage<Illustration: View>: View {
    let title: String
    let subtitle: String
    var scrolls = true
    @ViewBuilder let illustration: Illustration
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var shown = 0

    init(title: String, subtitle: String, scrolls: Bool = true, @ViewBuilder illustration: () -> Illustration) {
        self.title = title
        self.subtitle = subtitle
        self.scrolls = scrolls
        self.illustration = illustration()
    }

    var body: some View {
        Group {
            if scrolls {
                ScrollView { content }.scrollBounceBehavior(.basedOnSize)
            } else {
                content
            }
        }
        .task {
            for value in 1...3 {
                withAnimation(reduceMotion ? nil : .easeOut(duration: 0.35)) { shown = value }
                try? await Task.sleep(for: .milliseconds(reduceMotion ? 0 : 90))
            }
        }
    }

    private var content: some View {
        VStack(spacing: 14) {
            Text(title)
                .font(.system(.title, design: .serif).weight(.medium))
                .multilineTextAlignment(.center)
                .opacity(shown > 0 ? 1 : 0)
                .contentTransition(.opacity)
                .accessibilityAddTraits(.isHeader)
            Text(subtitle)
                .font(.subheadline).foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .opacity(shown > 1 ? 1 : 0)
                .contentTransition(.opacity)
            illustration
                .padding(.top, 28)
                .opacity(shown > 2 ? 1 : 0)
                .offset(y: shown > 2 || reduceMotion ? 0 : 10)
        }
        .padding(.top, 28)
        .frame(maxWidth: .infinity)
    }
}

private struct ProgressSegments: View {
    let count: Int
    let index: Int
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        HStack(spacing: 5) {
            ForEach(0..<count, id: \.self) { item in
                Capsule()
                    .fill(item == index ? Color.primary : Color.primary.opacity(0.15))
                    .frame(width: item == index ? 22 : 12, height: 4)
            }
        }
        .frame(maxWidth: .infinity)
        .animation(reduceMotion ? nil : .spring(response: 0.35, dampingFraction: 0.85), value: index)
        .accessibilityElement()
        .accessibilityLabel("Step \(index + 1) of \(count)")
    }
}

private struct PrimaryButton: View {
    let title: String
    var enabled = true
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(title).font(.body.weight(.semibold))
                .frame(maxWidth: .infinity, minHeight: 52)
                .foregroundStyle(BoardStyle.controlLabel)
                .background(BoardStyle.control.opacity(enabled ? 1 : 0.35), in: Capsule())
                .contentTransition(.opacity)
        }
        .buttonStyle(HomePressStyle())
        .disabled(!enabled)
    }
}

private struct SecondaryButton: View {
    let title: String
    var identifier: String? = nil
    var enabled = true
    let action: () -> Void

    var body: some View {
        Button(title, action: action)
            .font(.body.weight(.medium))
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, minHeight: 44)
            .disabled(!enabled)
            .accessibilityIdentifier(identifier ?? title)
    }
}

private struct StaggeredRow<Content: View>: View {
    let index: Int
    @ViewBuilder let content: Content
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var shown = false

    var body: some View {
        content
            .opacity(shown ? 1 : 0)
            .offset(y: shown || reduceMotion ? 0 : 8)
            .task {
                try? await Task.sleep(for: .milliseconds(reduceMotion ? 0 : 300 + 110 * index))
                withAnimation(reduceMotion ? nil : .spring(response: 0.4, dampingFraction: 0.8)) { shown = true }
            }
    }
}

private struct ProfileRow: View {
    let profile: HermesSetupProfile
    @Binding var selected: Bool

    var body: some View {
        HStack(spacing: 14) {
            BotAvatar(symbol: "coconut", color: "A85F32", size: 36, identity: profile.id)
            VStack(alignment: .leading, spacing: 3) {
                Text(profile.name).font(.body.weight(.medium))
                Text(profile.botId == nil ? profile.contents : "Already added").font(.caption).foregroundStyle(.secondary)
            }
            Spacer(minLength: 8)
            if profile.botId == nil {
                Toggle(isOn: $selected) { Text(profile.name) }
                    .labelsHidden()
                    .accessibilityLabel("\(profile.name), \(profile.contents)")
            } else {
                Image(systemName: "checkmark").foregroundStyle(.secondary)
                    .accessibilityLabel("\(profile.name), already added")
            }
        }
        .padding(.vertical, 12)
    }
}

private struct MCPServerRow: View {
    let server: HermesMCPServer

    var body: some View {
        HStack(spacing: 12) {
            Label(server.id, systemImage: server.transport == "http" ? "globe" : "terminal").font(.body)
            Spacer(minLength: 8)
            if server.needsSetup {
                Text("Needs setup").font(.caption).foregroundStyle(.secondary)
            }
        }
        .accessibilityElement(children: .combine)
    }
}

// Three bots at work. Every few seconds each one moves on to another phase.
private struct WelcomeIllustration: View {
    var phases: [BotPhase] = [.github, .reading, .testing, .memory, .browsing, .editing]
    private let fruits = ["fruit.coconut", "fruit.apple", "fruit.blueberry"]
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        TimelineView(.periodic(from: .now, by: 2.6)) { timeline in
            // Reduce Motion keeps one set of phases. The characters stay still.
            let turn = reduceMotion ? 0 : Int(timeline.date.timeIntervalSinceReferenceDate / 2.6)
            HStack(alignment: .top, spacing: 18) {
                ForEach(0..<3, id: \.self) { index in
                    let phase = phases[(turn + index * 2) % phases.count]
                    VStack(spacing: 10) {
                        BotAvatar(symbol: fruits[index], color: "", size: 64, identity: fruits[index], phase: phase)
                        ShimmerText(text: phase.label)
                            .font(.caption)
                            .id(phase)
                            .transition(.opacity)
                    }
                    .frame(width: 96)
                    .offset(y: index == 1 ? -14 : 0)
                }
            }
            .animation(reduceMotion ? nil : .easeInOut(duration: 0.3), value: turn)
        }
        .accessibilityHidden(true)
    }
}

// Items travel from Hermes to the coconut, then land in a list below, as in an import.
private struct SetupIllustration: View {
    let setup: HermesSetup?
    let loaded: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var landed = 0
    @State private var flying: Int?
    @State private var travel = 0.0

    private static func items(_ setup: HermesSetup?) -> [(icon: String, text: String)] {
        guard let setup, setup.found else { return [("checkmark.seal", "Coconut's own Hermes")] }
        var items: [(String, String)] = setup.profiles.map { ("person.crop.circle", $0.name) }
        let skills = setup.profiles.reduce(0) { $0 + $1.skills }
        let memories = setup.profiles.reduce(0) { $0 + $1.memories }
        if memories > 0 { items.append(("brain", memories == 1 ? "1 memory file" : "\(memories) memory files")) }
        if skills > 0 { items.append(("wand.and.stars", skills == 1 ? "1 skill" : "\(skills) skills")) }
        if !setup.logins.isEmpty { items.append(("key", setup.logins.count == 1 ? "1 model key" : "\(setup.logins.count) model keys")) }
        return items
    }

    var body: some View {
        let items = Self.items(setup)
        VStack(spacing: 28) {
            GeometryReader { geometry in
                let width = geometry.size.width
                ZStack {
                    Path { path in
                        path.move(to: CGPoint(x: 56, y: 28))
                        path.addLine(to: CGPoint(x: width - 56, y: 28))
                    }
                    .stroke(Color.primary.opacity(0.18), style: StrokeStyle(lineWidth: 1.5, dash: [3, 5]))
                    HStack {
                        Text("H").font(.system(size: 22, weight: .semibold, design: .serif))
                            .frame(width: 52, height: 52)
                            .background(Color.primary.opacity(0.06), in: Circle())
                        Spacer()
                        BotAvatar(symbol: "fruit.coconut", color: "", size: 52, identity: "setup",
                                  phase: loaded ? nil : .searching)
                            .overlay(alignment: .topTrailing) {
                                if loaded && landed >= items.count {
                                    Image(systemName: "checkmark.circle.fill").foregroundStyle(Color.green)
                                        .background(Circle().fill(BoardStyle.paper))
                                        .transition(.scale(scale: 0.2).combined(with: .opacity))
                                }
                            }
                    }
                    if let flying, flying < items.count {
                        Image(systemName: items[flying].icon)
                            .font(.system(size: 13, weight: .semibold))
                            .frame(width: 28, height: 28)
                            .background(BoardStyle.paper, in: Circle())
                            .overlay(Circle().stroke(Color.primary.opacity(0.15)))
                            .position(x: 56 + (width - 112) * travel, y: 28)
                    }
                }
            }
            .frame(height: 56)
            VStack(alignment: .leading, spacing: 12) {
                ForEach(Array(items.enumerated()), id: \.offset) { index, item in
                    Label(item.text, systemImage: item.icon)
                        .font(.subheadline)
                        .opacity(index < landed ? 1 : 0)
                        .offset(y: index < landed || reduceMotion ? 0 : 6)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 8)
        }
        .task(id: loaded) {
            guard loaded else { return }
            let items = Self.items(setup)
            for index in items.indices {
                if reduceMotion {
                    landed = index + 1
                    continue
                }
                flying = index
                travel = 0
                withAnimation(.easeInOut(duration: 0.45)) { travel = 1 }
                try? await Task.sleep(for: .milliseconds(450))
                withAnimation(.spring(response: 0.35, dampingFraction: 0.75)) { landed = index + 1 }
                try? await Task.sleep(for: .milliseconds(120))
            }
            flying = nil
            withAnimation(.spring(response: 0.4, dampingFraction: 0.6)) { landed = items.count }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(loaded ? items.map(\.text).joined(separator: ", ") : "Looking for Hermes")
    }
}

// A notification drops onto a phone, with a small overshoot.
private struct NotificationIllustration: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var shown = false

    var body: some View {
        ZStack(alignment: .top) {
            RoundedRectangle(cornerRadius: 34, style: .continuous)
                .fill(Color.primary.opacity(0.05))
                .frame(width: 210, height: 250)
                .overlay(alignment: .bottom) {
                    HStack(spacing: 12) {
                        ForEach(0..<4, id: \.self) { _ in
                            RoundedRectangle(cornerRadius: 9).fill(Color.primary.opacity(0.07)).frame(width: 34, height: 34)
                        }
                    }
                    .padding(.bottom, 26)
                }
            HStack(spacing: 10) {
                BotAvatar(symbol: "fruit.coconut", color: "", size: 30, identity: "notification")
                VStack(alignment: .leading, spacing: 2) {
                    Text("Coconut").font(.caption.weight(.semibold))
                    Text("Your release notes are ready.").font(.caption).foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
            }
            .padding(10)
            .frame(width: 236)
            .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            .shadow(color: .black.opacity(0.08), radius: 10, y: 4)
            .offset(y: shown ? 30 : -24)
            .opacity(shown ? 1 : 0)
        }
        .task {
            try? await Task.sleep(for: .milliseconds(reduceMotion ? 0 : 450))
            withAnimation(reduceMotion ? nil : .spring(response: 0.45, dampingFraction: 0.62)) { shown = true }
        }
        .accessibilityHidden(true)
    }
}

// The coconut gets a check mark, and short ticks burst outward once.
private struct CompletionBurst: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var checked = false
    @State private var burst = false

    var body: some View {
        ZStack {
            ForEach(0..<8, id: \.self) { index in
                Capsule()
                    .fill(Color.primary)
                    .frame(width: 3, height: 9)
                    .offset(y: burst ? -62 : -44)
                    .rotationEffect(.degrees(Double(index) * 45 + 22.5))
                    .opacity(burst ? 0 : checked && !reduceMotion ? 1 : 0)
            }
            BotAvatar(symbol: "fruit.coconut", color: "", size: 72, identity: "complete")
                .overlay(alignment: .bottomTrailing) {
                    Image(systemName: "checkmark.circle.fill")
                        .font(.system(size: 24))
                        .foregroundStyle(Color.green)
                        .background(Circle().fill(BoardStyle.paper))
                        .scaleEffect(checked ? 1 : 0.2)
                        .opacity(checked ? 1 : 0)
                }
        }
        .frame(height: 160)
        .task {
            try? await Task.sleep(for: .milliseconds(250))
            withAnimation(reduceMotion ? nil : .spring(response: 0.35, dampingFraction: 0.55)) { checked = true }
            try? await Task.sleep(for: .milliseconds(120))
            withAnimation(reduceMotion ? nil : .easeOut(duration: 0.45)) { burst = true }
        }
        .accessibilityHidden(true)
    }
}
