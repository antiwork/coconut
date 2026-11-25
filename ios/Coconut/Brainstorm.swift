import SwiftUI
#if os(iOS)
import AVFoundation
import Speech
#endif

/// Brainstorm: the person talks an idea through with the model of a project, by voice, and the talk ends as the next
/// steps in the message box, ready to send to an agent that builds them. The person asked for it on Product, in the
/// place of New to-do: "have brainstorming special button which enables live chat voice mode with agent and then it
/// turns it into next steps (chat box prefill) and then send to build". Support keeps its + (TodoLists), and Finance
/// has none.
///
/// The phone listens and speaks itself. Speech recognition on the phone writes down what the person says, and a pause
/// of a second sends it. The Mac answers with the model of the project and what its profile knows (SOUL.md and memory)
/// in two or three seconds, without the tools of a turn, which take minutes (backend/coconut/brainstorm.py). The phone
/// reads the answer aloud and listens again. Next steps asks the same model for the message that builds what the two
/// worked out. The page closes, and the message waits in the message box of Product with the project of the talk, so
/// the person reads it and sends it to an agent there. The talk and that box share their project (NewChatPlace.product).
/// A Mac has no Product page and no voice yet, so the button shows on the iPhone.

/// One thing said in a brainstorm, by the person or by the model of the project.
struct BrainstormLine: Identifiable, Hashable, Sendable {
    enum Role: String, Sendable { case user, assistant }
    let id = UUID()
    let role: Role
    let text: String
}

/// The next steps of a talk, with the project that they go to.
struct BrainstormSteps: Equatable {
    let project: String
    let text: String
}

private struct BrainstormRequest: Encodable {
    struct Line: Encodable {
        let role: String
        let text: String
    }

    let botId: String
    let messages: [Line]
    let finish: Bool
}

private struct BrainstormReply: Decodable, Sendable {
    let text: String
}

extension AppStore {
    /// The answer of the model of `project` to the talk so far, or with `finish` the message that builds what the talk
    /// worked out. A talk without lines gets the model ready, so the first answer comes sooner. Each call is a new
    /// answer, so each gets a key of its own and no retry.
    func brainstorm(in project: Bot, lines: [BrainstormLine], finish: Bool) async throws -> String {
        if isDemo { return try await BrainstormDemo.answer(lines: lines, finish: finish) }
        let request = BrainstormRequest(botId: project.agentId, messages: lines.map { .init(role: $0.role.rawValue, text: $0.text) },
                                        finish: finish)
        let reply: BrainstormReply = try await businessRequest("brainstorm", method: "POST", body: APIJSON.encode(request),
                                                               requestId: UUID().uuidString)
        return reply.text
    }
}

/// The talk of the demo, for screenshots: Gumroad work, as the rest of the demo.
enum BrainstormDemo {
    static let said = [
        "Sellers keep asking when a refund comes out of their payout. I want a weekly email that shows it.",
        "Just sales and refunds. Monday morning, in their own time zone.",
    ]
    /// What the person is saying when the talk of the demo stops, so a screenshot shows the words as they come.
    static let saying = "And skip anyone who had no sales that week"
    static let answers = [
        "A weekly payout email could answer that before they ask. Should it show only last week's sales and refunds, or the payouts on hold too?",
        "Then it stays short. I'd send it at nine in the morning local time. Should sellers without sales that week get one?",
    ]
    static let steps = """
        Build a weekly email for sellers that shows how refunds change their payout.

        1. Every Monday at 9 a.m. in the seller's time zone, send last week's sales and refunds and the payout they add up to.
        2. Skip sellers who had no sales that week.
        3. Leave payouts on hold out of the email for now.
        """

    static func answer(lines: [BrainstormLine], finish: Bool) async throws -> String {
        try await Task.sleep(for: .seconds(finish ? 1.5 : 1))
        if finish { return steps }
        if lines.isEmpty { return "" }
        let said = lines.filter { $0.role == .user }.count
        return answers[min(max(said - 1, 0), answers.count - 1)]
    }
}

/// A preview launch opens the page through `opening`. With `finishing`, the page runs Next steps after the talk of the
/// demo, so a screenshot shows the message box with the next steps.
@MainActor @Observable
final class PendingBrainstorm {
    static let shared = PendingBrainstorm()
    var opening = false
    var finishing = false
}

extension View {
    /// The Brainstorm button of Product, where its + was. A Mac has no Product page, so there it adds nothing.
    func brainstormButton() -> some View { modifier(BrainstormButton()) }
}

private struct BrainstormButton: ViewModifier {
    @Environment(AppStore.self) private var store
    @State private var shown = false
    /// The next steps of the talk, for the message box once the page is closed.
    @State private var steps: BrainstormSteps?

    func body(content: Content) -> some View {
        #if os(iOS)
        content
            .toolbar {
                // A brown capsule, not one more glyph: the person asked for a special button.
                if #available(iOS 26.0, *) {
                    ToolbarItem(placement: .primaryAction) { button }
                        .sharedBackgroundVisibility(.hidden)
                } else {
                    ToolbarItem(placement: .primaryAction) { button }
                }
            }
            .fullScreenCover(isPresented: $shown, onDismiss: prefill) {
                BrainstormView { steps = $0 }
            }
            .onChange(of: PendingBrainstorm.shared.opening, initial: true) { _, asked in
                guard asked else { return }
                PendingBrainstorm.shared.opening = false
                shown = true
            }
        #else
        content
        #endif
    }

    private var button: some View {
        Button { shown = true } label: {
            Label("Brainstorm", systemImage: "waveform").labelStyle(.titleAndIcon)
        }
        .buttonStyle(CoconutButtonStyle())
        .accessibilityIdentifier("brainstorm")
    }

    /// The message box of Product takes the next steps once the page is gone, with the project of the talk.
    private func prefill() {
        guard let steps else { return }
        self.steps = nil
        store.newChatRequest = NewChatRequest(project: steps.project, text: steps.text, place: .product)
    }
}

#if os(iOS)
/// The page of a brainstorm. It covers the screen: the project on top, the talk in the middle, then the button that
/// listens and Next steps. The screen stays on while it shows.
struct BrainstormView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var scenePhase
    /// The project of the message box of Product, which gets the next steps: a choice here is a choice there.
    @AppStorage(NewChatPlace.product.choiceKey) private var chosen = ""
    @State private var session = BrainstormSession()
    @State private var confirmsEnd = false
    /// Gets the next steps just before the page closes.
    let done: (BrainstormSteps) -> Void

    var body: some View {
        let latest = AgentChats.activity(bots: store.bots, latest: store.chatSummaries.newestActivity)
        let choices = ChatProject.choices(in: store.bots, latest: latest)
        // A project of this Mac answers: an agent of a team has no model here, so the list leaves those out.
        let target: Bot? = switch NewChatPlace.product.target(chosen: chosen, projects: choices, agents: []) {
        case .project(let project): project
        default: nil
        }
        VStack(spacing: 0) {
            header(target, choices: choices)
            talk
            BrainstormOrb(session: session)
                .padding(.vertical, BoardStyle.Space.s)
            Button { finish(target) } label: {
                ZStack {
                    Text("Next steps").opacity(session.phase == .finishing ? 0 : 1)
                    if session.phase == .finishing { ProgressView().tint(BoardStyle.controlLabel) }
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(CoconutButtonStyle(large: true))
            .disabled(target == nil || session.phase == .finishing || !session.lines.contains { $0.role == .user })
            .padding(.horizontal, BoardStyle.Space.l)
            .padding(.bottom, BoardStyle.Space.s)
            .accessibilityIdentifier("brainstorm-next-steps")
        }
        .background(BoardStyle.paper)
        // The next answer goes to the project that the chip shows.
        .onChange(of: target?.id, initial: true) { _, _ in session.project = target }
        .task {
            session.store = store
            session.project = target
            await session.start(demo: store.isDemo)
            if store.isDemo && PendingBrainstorm.shared.finishing {
                PendingBrainstorm.shared.finishing = false
                finish(target)
            }
        }
        .onChange(of: scenePhase) { _, phase in if phase != .active { session.pause() } }
        .onAppear { UIApplication.shared.isIdleTimerDisabled = true }
        .onDisappear {
            UIApplication.shared.isIdleTimerDisabled = false
            session.stop()
        }
        .confirmationDialog("End the brainstorm?", isPresented: $confirmsEnd, titleVisibility: .visible) {
            Button("End", role: .destructive) { dismiss() }
        }
    }

    /// The project chip, as the message box has it, and the button that ends the talk.
    private func header(_ target: Bot?, choices: [Bot]) -> some View {
        HStack(spacing: 0) {
            if let target {
                Menu {
                    Picker("Project", selection: Binding(get: { target.id }, set: { chosen = $0 })) {
                        ForEach(choices) { project in Text(project.name).tag(project.id) }
                    }
                    .pickerStyle(.inline)
                } label: {
                    ComposerChipLabel(title: target.name)
                }
                .modifier(ComposerChipMenuStyle())
                .menuOrder(.fixed)
                .accessibilityLabel("Project, \(target.name)")
                .accessibilityIdentifier("brainstorm-project")
            }
            Spacer(minLength: 0)
            Button {
                if session.lines.isEmpty { dismiss() } else { confirmsEnd = true }
            } label: {
                Image(systemName: "xmark")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(.secondary)
                    .frame(width: 44, height: 44)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("End the brainstorm")
            .accessibilityIdentifier("brainstorm-close")
        }
        .padding(.horizontal, BoardStyle.Space.s)
    }

    /// The talk: what the person said in their bubbles, the answers as plain text, and the words that come now.
    private var talk: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: BoardStyle.Space.m) {
                    ForEach(session.lines) { line in BrainstormBubble(text: line.text, mine: line.role == .user) }
                    if !session.hearing.isEmpty { BrainstormBubble(text: session.hearing, mine: true, live: true) }
                    Color.clear.frame(height: 1).id(Self.end)
                }
                .padding(.horizontal, BoardStyle.Space.l)
                .padding(.vertical, BoardStyle.Space.m)
            }
            .defaultScrollAnchor(.bottom)
            .onChange(of: session.lines.count) { _, _ in proxy.scrollTo(Self.end, anchor: .bottom) }
            .onChange(of: session.hearing) { _, _ in proxy.scrollTo(Self.end, anchor: .bottom) }
        }
        .frame(maxHeight: .infinity)
    }

    private static let end = "brainstorm-end"

    /// Asks for the next steps. They go to the message box when the page has closed (BrainstormButton).
    private func finish(_ target: Bot?) {
        guard let target else { return }
        Task {
            guard let steps = await session.finish() else { return }
            done(BrainstormSteps(project: target.id, text: steps))
            dismiss()
        }
    }
}

/// One line of the talk. The person's words sit in their bubble on the right, as in a chat; the words that come now
/// are lighter until the pause sends them.
private struct BrainstormBubble: View {
    let text: String
    let mine: Bool
    var live = false

    var body: some View {
        HStack(spacing: 0) {
            if mine { Spacer(minLength: BoardStyle.Space.xxl) }
            Text(text)
                .font(.body)
                .foregroundStyle(live ? .secondary : .primary)
                .padding(.horizontal, mine ? BoardStyle.Space.m : 0)
                .padding(.vertical, mine ? BoardStyle.Space.s : 0)
                .background(mine ? BoardStyle.userBubble.opacity(live ? 0.5 : 1) : .clear,
                            in: RoundedRectangle(cornerRadius: BoardStyle.Radius.medium, style: .continuous))
            if !mine { Spacer(minLength: BoardStyle.Space.xxl) }
        }
        .accessibilityElement(children: .combine)
    }
}

/// The button of a brainstorm: a brown circle with a word under it. While it listens, a ring follows the voice of the
/// person. A tap stops the voice of an answer and listens, sends the words before the pause, pauses the microphone or
/// resumes it, and tries again after a failure.
private struct BrainstormOrb: View {
    let session: BrainstormSession
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ScaledMetric(relativeTo: .largeTitle) private var size: CGFloat = 88
    /// How far the ring grows past the circle at the loudest voice.
    private static let ring: CGFloat = 1.4

    var body: some View {
        let phase = session.phase
        let meter = session.meter
        VStack(spacing: BoardStyle.Space.m) {
            Button { session.tap() } label: {
                ZStack {
                    if phase == .listening {
                        TimelineView(.animation(minimumInterval: 1.0 / 30, paused: reduceMotion)) { _ in
                            Circle()
                                .fill(BoardStyle.control.opacity(0.18))
                                .frame(width: size, height: size)
                                .scaleEffect(1 + (Self.ring - 1) * meter.level)
                        }
                    }
                    Circle().fill(BoardStyle.control).frame(width: size, height: size)
                    if [.starting, .thinking, .finishing].contains(phase) {
                        ProgressView().tint(BoardStyle.controlLabel)
                    } else {
                        Image(systemName: symbol(phase))
                            .font(.system(size: size * 0.32, weight: .semibold))
                            .foregroundStyle(BoardStyle.controlLabel)
                    }
                }
                // The ring grows into this frame, so it never reaches the word under it.
                .frame(width: size * Self.ring, height: size * Self.ring)
                .contentShape(Circle())
            }
            .buttonStyle(.plain)
            .disabled([.starting, .thinking, .finishing].contains(phase))
            .accessibilityLabel(action(phase))
            .accessibilityIdentifier("brainstorm-orb")
            Text(label(phase))
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .lineLimit(3)
                .padding(.horizontal, BoardStyle.Space.xl)
                .accessibilityIdentifier("brainstorm-phase")
        }
    }

    private func symbol(_ phase: BrainstormSession.Phase) -> String {
        switch phase {
        case .speaking: "stop.fill"
        case .paused: "mic.slash.fill"
        case .failed: "arrow.clockwise"
        default: "waveform"
        }
    }

    private func label(_ phase: BrainstormSession.Phase) -> String {
        switch phase {
        case .starting: "Starting"
        case .listening: "Listening"
        case .thinking: "Thinking"
        case .speaking: "Speaking"
        case .paused: "Paused"
        case .finishing: "Writing the next steps"
        case .failed(let message): message
        }
    }

    private func action(_ phase: BrainstormSession.Phase) -> String {
        switch phase {
        case .listening: session.hearing.isEmpty ? "Pause the microphone" : "Send what you said"
        case .speaking: "Stop and listen"
        case .paused: "Listen again"
        case .failed: "Try again"
        default: label(phase)
        }
    }
}

/// The microphone, the speech recognition and the voice of one brainstorm. It listens until the person pauses, sends
/// what they said, reads the answer aloud and listens again. The microphone rests while the voice speaks, so the phone
/// never hears itself. The demo plays a talk instead (BrainstormDemo), because a simulator has no one to listen to.
@MainActor @Observable
final class BrainstormSession {
    enum Phase: Equatable {
        case starting, listening, thinking, speaking, paused, finishing
        case failed(String)
    }

    private(set) var phase = Phase.starting
    private(set) var lines: [BrainstormLine] = []
    /// What the person says now, before the pause that sends it.
    private(set) var hearing = ""
    /// The loudness of the microphone. The audio thread writes it, and the ring reads it on each frame.
    let meter = BrainstormMeter()
    @ObservationIgnored weak var store: AppStore?
    @ObservationIgnored var project: Bot?

    /// A pause this long ends what the person says.
    private static let silence = Duration.milliseconds(1200)
    @ObservationIgnored private var demo = false
    @ObservationIgnored private let engine = AVAudioEngine()
    @ObservationIgnored private var running = false
    @ObservationIgnored private let recognizer = SFSpeechRecognizer(locale: .current) ?? SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    @ObservationIgnored private let feed = BrainstormFeed()
    @ObservationIgnored private var recognition: SFSpeechRecognitionTask?
    /// Counts the requests of the recognition, so a late result of an earlier one changes nothing.
    @ObservationIgnored private var take = 0
    /// Recognition requests in a row that ended with an error and nothing heard.
    @ObservationIgnored private var misses = 0
    @ObservationIgnored private var pauseWait: Task<Void, Never>?
    @ObservationIgnored private var turn: Task<Void, Never>?
    @ObservationIgnored private var recover: (() -> Void)?
    @ObservationIgnored private let voice = BrainstormVoice()

    func start(demo: Bool) async {
        self.demo = demo
        if demo { await playDemo(); return }
        guard await Self.speechAllowed() else { return failPermission("Turn on Speech Recognition for Coconut in the Settings app.") }
        guard await AVAudioApplication.requestRecordPermission() else { return failPermission("Turn on the Microphone for Coconut in the Settings app.") }
        warmUp()
        resume()
    }

    /// The Mac starts the model of the project while the person says their first sentence.
    private func warmUp() {
        guard let store, let project else { return }
        Task { _ = try? await store.brainstorm(in: project, lines: [], finish: false) }
    }

    /// Starts the microphone if it does not run, then listens.
    private func resume() {
        guard !demo else { phase = .listening; return }
        guard recognizer?.isAvailable == true else { return fail("Speech recognition is not available right now.") { [weak self] in self?.resume() } }
        if !running {
            do { try startEngine() } catch { return fail(error.localizedDescription) { [weak self] in self?.resume() } }
        }
        listen()
    }

    private func startEngine() throws {
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playAndRecord, mode: .default, options: [.defaultToSpeaker, .allowBluetoothHFP, .duckOthers])
        try session.setActive(true)
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else { throw BrainstormError.noMicrophone }
        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 1024, format: format, block: Self.tap(feed: feed, meter: meter))
        engine.prepare()
        try engine.start()
        running = true
    }

    private func stopEngine() {
        endRecognition()
        guard running else { return }
        engine.stop()
        engine.inputNode.removeTap(onBus: 0)
        running = false
        meter.reset()
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    /// A new request of the recognition takes what the microphone hears from now on.
    private func listen() {
        guard let recognizer else { return }
        endRecognition()
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        request.addsPunctuation = true
        request.taskHint = .dictation
        if recognizer.supportsOnDeviceRecognition { request.requiresOnDeviceRecognition = true }
        take += 1
        let current = take
        feed.request = request
        recognition = Self.recognize(request, with: recognizer) { [weak self] text, final, failed in
            Task { @MainActor in self?.heard(text, final: final, failed: failed, take: current) }
        }
        hearing = ""
        phase = .listening
    }

    private func heard(_ text: String?, final: Bool, failed: Bool, take: Int) {
        guard take == self.take, phase == .listening else { return }
        if let text, !text.isEmpty, text != hearing {
            hearing = text
            misses = 0
            waitForPause()
        }
        guard final || failed else { return }
        // The recognizer ended this request: with the final words after a pause, or with an error after a long
        // silence or a minute of server recognition. What it heard goes, and a new request listens.
        if hearing.isEmpty && failed {
            misses += 1
            if misses >= 5 { return fail("Speech recognition stopped.") { [weak self] in self?.misses = 0; self?.resume() } }
        }
        endUtterance()
    }

    private func waitForPause() {
        pauseWait?.cancel()
        pauseWait = Task { [weak self] in
            try? await Task.sleep(for: Self.silence)
            guard !Task.isCancelled else { return }
            self?.endUtterance()
        }
    }

    /// Sends what the person said, or listens again when they said nothing.
    private func endUtterance() {
        let said = hearing.trimmingCharacters(in: .whitespacesAndNewlines)
        endRecognition()
        hearing = ""
        guard !said.isEmpty else { return listen() }
        lines.append(BrainstormLine(role: .user, text: said))
        answer()
    }

    private func endRecognition() {
        pauseWait?.cancel()
        feed.request?.endAudio()
        feed.request = nil
        recognition?.cancel()
        recognition = nil
        take += 1
    }

    /// Asks the model of the project for its answer and reads it aloud. An answer that comes while the page is paused
    /// joins the talk without the voice.
    private func answer() {
        phase = .thinking
        turn?.cancel()
        turn = Task { [weak self] in
            guard let self else { return }
            do {
                let reply = try await self.ask(finish: false)
                guard !Task.isCancelled else { return }
                self.lines.append(BrainstormLine(role: .assistant, text: reply))
                if self.phase == .thinking { self.speak(reply) }
            } catch is CancellationError {
            } catch {
                guard self.phase == .thinking else { return }
                self.fail(error.localizedDescription) { [weak self] in self?.answer() }
            }
        }
    }

    private func ask(finish: Bool) async throws -> String {
        guard let store, let project else { throw BrainstormError.noProject }
        return try await store.brainstorm(in: project, lines: lines, finish: finish)
    }

    private func speak(_ text: String) {
        phase = .speaking
        if demo { return }
        voice.speak(text) { [weak self] in
            guard let self, self.phase == .speaking else { return }
            self.resume()
        }
    }

    /// The tap on the button.
    func tap() {
        switch phase {
        case .speaking:
            voice.stop()
            resume()
        case .listening:
            if hearing.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { pause() } else { endUtterance() }
        case .paused:
            resume()
        case .failed:
            let again = recover
            recover = nil
            again?()
        default:
            break
        }
    }

    /// Rests the microphone and the voice: on a tap, and when the app leaves the screen. A tap resumes.
    func pause() {
        guard !demo, ![.paused, .finishing].contains(phase) else { return }
        voice.stop()
        stopEngine()
        hearing = ""
        if case .failed = phase { return }
        phase = .paused
    }

    /// The message that builds what the talk worked out. Words that the pause has not sent yet join the talk first.
    /// Returns nil when the Mac does not answer; the reason shows under the button, and Next steps tries again.
    func finish() async -> String? {
        let said = hearing.trimmingCharacters(in: .whitespacesAndNewlines)
        if !said.isEmpty { lines.append(BrainstormLine(role: .user, text: said)) }
        hearing = ""
        voice.stop()
        stopEngine()
        turn?.cancel()
        phase = .finishing
        do {
            return try await ask(finish: true)
        } catch {
            fail(error.localizedDescription) { [weak self] in self?.resume() }
            return nil
        }
    }

    /// Ends the talk when the page closes.
    func stop() {
        turn?.cancel()
        voice.stop()
        stopEngine()
    }

    private func fail(_ message: String, recover: @escaping () -> Void) {
        voice.stop()
        endRecognition()
        hearing = ""
        self.recover = recover
        phase = .failed(message)
    }

    /// Without the permission, the tap opens the page of Coconut in the Settings app.
    private func failPermission(_ message: String) {
        fail(message) {
            if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
        }
    }

    /// The talk of the demo: the person's words come one by one, as the recognition writes them, and each answer
    /// comes from the demo of the store.
    private func playDemo() async {
        for sentence in BrainstormDemo.said {
            await sayInDemo(sentence)
            lines.append(BrainstormLine(role: .user, text: sentence))
            hearing = ""
            phase = .thinking
            guard let reply = try? await ask(finish: false) else { return }
            lines.append(BrainstormLine(role: .assistant, text: reply))
            phase = .speaking
            try? await Task.sleep(for: .seconds(2))
        }
        await sayInDemo(BrainstormDemo.saying)
    }

    private func sayInDemo(_ sentence: String) async {
        phase = .listening
        hearing = ""
        for word in sentence.split(separator: " ") {
            try? await Task.sleep(for: .milliseconds(150))
            guard !Task.isCancelled else { return }
            hearing += (hearing.isEmpty ? "" : " ") + word
            meter.set(Float.random(in: 0.35...0.8))
        }
        try? await Task.sleep(for: .milliseconds(400))
    }

    nonisolated private static func speechAllowed() async -> Bool {
        let status = await withCheckedContinuation { (done: CheckedContinuation<SFSpeechRecognizerAuthorizationStatus, Never>) in
            SFSpeechRecognizer.requestAuthorization { done.resume(returning: $0) }
        }
        return status == .authorized
    }

    /// The tap of the microphone runs on the audio thread, so it is made outside the main actor.
    nonisolated private static func tap(feed: BrainstormFeed, meter: BrainstormMeter) -> AVAudioNodeTapBlock {
        { buffer, _ in
            feed.append(buffer)
            meter.measure(buffer)
        }
    }

    /// The recognizer calls back on a queue of its own, so the handler is made outside the main actor.
    nonisolated private static func recognize(_ request: SFSpeechAudioBufferRecognitionRequest, with recognizer: SFSpeechRecognizer,
                                              then handle: @escaping @Sendable (String?, Bool, Bool) -> Void) -> SFSpeechRecognitionTask {
        recognizer.recognitionTask(with: request) { result, error in
            handle(result?.bestTranscription.formattedString, result?.isFinal ?? false, error != nil)
        }
    }
}

private enum BrainstormError: LocalizedError {
    case noMicrophone, noProject

    var errorDescription: String? {
        switch self {
        case .noMicrophone: "This iPhone has no microphone to listen with."
        case .noProject: "Choose a project to brainstorm with."
        }
    }
}

/// The request of the recognition that the microphone feeds. The audio thread reads it, and the session swaps it.
final class BrainstormFeed: @unchecked Sendable {
    private let lock = NSLock()
    private var current: SFSpeechAudioBufferRecognitionRequest?

    var request: SFSpeechAudioBufferRecognitionRequest? {
        get { lock.withLock { current } }
        set { lock.withLock { current = newValue } }
    }

    func append(_ buffer: AVAudioPCMBuffer) { request?.append(buffer) }
}

/// The loudness of the microphone, 0 to 1, from the audio thread to the ring of the button.
final class BrainstormMeter: @unchecked Sendable {
    private let lock = NSLock()
    private var value: Float = 0

    var level: Double { lock.withLock { Double(value) } }

    func measure(_ buffer: AVAudioPCMBuffer) {
        guard let samples = buffer.floatChannelData?[0] else { return }
        let count = Int(buffer.frameLength)
        guard count > 0 else { return }
        var sum: Float = 0
        for index in 0..<count { sum += samples[index] * samples[index] }
        // -50 dB is silence and -10 dB a loud voice.
        let decibels = 20 * log10(max((sum / Float(count)).squareRoot(), 0.000_01))
        let level = max(0, min(1, (decibels + 50) / 40))
        lock.withLock { value = value * 0.6 + level * 0.4 }
    }

    func set(_ level: Float) { lock.withLock { value = level } }
    func reset() { set(0) }
}

/// The voice that reads the answers: the best voice of the phone's language that the phone has, through the audio
/// session of the brainstorm.
@MainActor
final class BrainstormVoice: NSObject, AVSpeechSynthesizerDelegate {
    private let synthesizer = AVSpeechSynthesizer()
    private var then: (@MainActor () -> Void)?
    private var current: ObjectIdentifier?

    override init() {
        super.init()
        synthesizer.delegate = self
    }

    func speak(_ text: String, then: @escaping @MainActor () -> Void) {
        let utterance = AVSpeechUtterance(string: text)
        utterance.voice = Self.voice
        self.then = then
        current = ObjectIdentifier(utterance)
        synthesizer.speak(utterance)
    }

    func stop() {
        then = nil
        current = nil
        if synthesizer.isSpeaking { synthesizer.stopSpeaking(at: .immediate) }
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        let finished = ObjectIdentifier(utterance)
        Task { @MainActor in self.finished(finished) }
    }

    /// Only the end of the answer that speaks now counts: a stopped one ends with no call.
    private func finished(_ utterance: ObjectIdentifier) {
        guard utterance == current else { return }
        let next = then
        then = nil
        current = nil
        next?()
    }

    private static var voice: AVSpeechSynthesisVoice? {
        let language = AVSpeechSynthesisVoice.currentLanguageCode()
        let voices = AVSpeechSynthesisVoice.speechVoices().filter {
            $0.language == language && $0.voiceTraits.isDisjoint(with: [.isNoveltyVoice, .isPersonalVoice])
        }
        return voices.max { $0.quality.rawValue < $1.quality.rawValue } ?? AVSpeechSynthesisVoice(language: language)
    }
}
#endif
