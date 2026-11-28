import SwiftUI
import AVFoundation
import Speech

/// Dictation in a message box: the microphone beside Send writes what the person says into the draft, as they say it.
/// Sahil asked for it: "add a transcribe or sort of a micro dictation button to the message box". The keyboard of an
/// iPhone has a dictation key, but only while the keyboard shows, and the box of a Mac has none.
///
/// A tap starts it, and the next tap stops it. The words go after the draft, with a space between them. The
/// recognizer's guess of the words changes as it hears more, so the end of the draft changes with it. The stop takes
/// its last words. The draft stays the person's: typing, a send, another draft, or leaving the box or the app ends the
/// dictation (DictationEnds). A result writes only over the text that the dictation wrote last.
///
/// Recognition runs on the device when the device can, so the words stay on it. A request of server recognition ends
/// after a minute: its words stay, and a new request listens.
@MainActor @Observable
final class Dictation {
    /// The microphone listens.
    private(set) var listening = false
    /// The text that the dictation wrote last into the box. It stays after the end.
    private var wrote: String?
    @ObservationIgnored private var engine: AVAudioEngine?
    @ObservationIgnored private var recognizer: SFSpeechRecognizer?
    @ObservationIgnored private let feed = SpeechFeed()
    @ObservationIgnored private var recognition: SFSpeechRecognitionTask?
    /// Counts the requests of the recognition, so a late result of an earlier one changes nothing.
    @ObservationIgnored private var take = 0
    /// Requests in a row that ended with an error and no words.
    @ObservationIgnored private var misses = 0
    @ObservationIgnored private var draft: Draft?
    @ObservationIgnored private var starting: Task<Void, Never>?
    @ObservationIgnored private var ending: Task<Void, Never>?
    @ObservationIgnored private var demo: Task<Void, Never>?
    @ObservationIgnored private var changes: (any NSObjectProtocol)?
    @ObservationIgnored private var report: (String) -> Void = { _ in }

    /// The box that the words go into.
    private struct Draft {
        let text: Binding<String>
        let selection: Binding<TextSelection?>?
        /// The draft before the dictation, with a space after it when it needs one.
        let before: String
        /// The words of the requests that ended.
        var said = ""
        /// The words of the request that listens now.
        var hearing = ""
        /// The text that the dictation wrote last.
        var written: String

        /// The draft with the words.
        var withWords: String { before + [said, hearing].filter { !$0.isEmpty }.joined(separator: " ") }

        mutating func keepHearing() {
            said = [said, hearing].filter { !$0.isEmpty }.joined(separator: " ")
            hearing = ""
        }
    }

    #if os(macOS)
    private static let settings = "System Settings, under Privacy & Security"
    #else
    private static let settings = "the Settings app"
    #endif

    /// The tap on the microphone. The demo writes a sentence without the microphone.
    func toggle(_ text: Binding<String>, selection: Binding<TextSelection?>?, demo: Bool,
                report: @escaping (String) -> Void) {
        if listening || starting != nil { return stop() }
        cancel()
        wrote = nil
        self.report = report
        starting = Task { [weak self] in await self?.start(text, selection: selection, demo: demo) }
    }

    private func start(_ text: Binding<String>, selection: Binding<TextSelection?>?, demo: Bool) async {
        var refusal: String?
        if !demo {
            if !(await SpeechFeed.allowed()) {
                refusal = "Turn on Speech Recognition for Coconut in \(Self.settings)."
            } else if !(await SpeechFeed.microphoneAllowed()) {
                refusal = "Turn on the Microphone for Coconut in \(Self.settings)."
            }
        }
        // A tap while the person answered the question of the system cancels the start.
        guard !Task.isCancelled else { return }
        starting = nil
        if let refusal { return report(refusal) }
        let old = text.wrappedValue
        let before = old.isEmpty || old.last?.isWhitespace == true ? old : old + " "
        draft = Draft(text: text, selection: selection, before: before, written: old)
        misses = 0
        if demo { return playDemo() }
        let english = Locale(identifier: "en-US")
        guard let recognizer = SFSpeechRecognizer(locale: .current) ?? SFSpeechRecognizer(locale: english),
              recognizer.isAvailable else {
            draft = nil
            return report("Speech recognition is not available right now.")
        }
        self.recognizer = recognizer
        do {
            try startEngine()
        } catch {
            stopEngine()
            draft = nil
            return report(error.localizedDescription)
        }
        listening = true
        listen()
    }

    /// The second tap: the microphone stops at once, and the recognizer gives its last words in a moment.
    func stop() {
        starting?.cancel()
        starting = nil
        guard listening else { return }
        listening = false
        stopEngine()
        guard recognition != nil else { return end() }
        feed.request?.endAudio()
        feed.request = nil
        // A recognizer that keeps its last words longer leaves the words that it gave.
        ending = Task { [weak self] in
            try? await Task.sleep(for: .seconds(2))
            guard !Task.isCancelled else { return }
            self?.end()
        }
    }

    /// Typing, a send, another draft or leaving the box: the dictation ends at once and writes nothing more.
    func cancel() {
        starting?.cancel()
        starting = nil
        if listening {
            listening = false
            stopEngine()
        }
        end()
    }

    /// The box shows the words of the dictation: it listens, or the box holds the text that it wrote last.
    func shows(_ text: String) -> Bool { listening || (wrote != nil && wrote == text) }

    /// The box changed. A text that the dictation did not write ends it.
    func textChanged(to text: String) {
        guard let draft, text != draft.written else { return }
        cancel()
    }

    /// Ends the dictation. The words stay in the box.
    private func end() {
        ending?.cancel()
        ending = nil
        demo?.cancel()
        demo = nil
        feed.request?.endAudio()
        feed.request = nil
        recognition?.cancel()
        recognition = nil
        take += 1
        draft = nil
    }

    private func startEngine() throws {
        #if os(iOS)
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.record, mode: .measurement, options: .duckOthers)
        try session.setActive(true, options: .notifyOthersOnDeactivation)
        #endif
        let engine = AVAudioEngine()
        self.engine = engine
        try tapMicrophone(of: engine)
        changes = Self.watch(engine) { [weak self] in
            Task { @MainActor in self?.restartEngine() }
        }
    }

    private func tapMicrophone(of engine: AVAudioEngine) throws {
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else { throw DictationError.noMicrophone }
        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 1024, format: format, block: SpeechFeed.tap(feed))
        engine.prepare()
        try engine.start()
    }

    /// Another microphone or output, such as AirPods, stops the engine. It starts again with the format of the new
    /// microphone, and a new request listens, as a request takes one format. A tap of another format than the
    /// microphone's stops the app, so the engine stops first.
    private func restartEngine() {
        guard listening, let engine else { return }
        engine.stop()
        do {
            try tapMicrophone(of: engine)
        } catch {
            stop()
            return report(error.localizedDescription)
        }
        draft?.keepHearing()
        listen()
    }

    private func stopEngine() {
        if let changes { NotificationCenter.default.removeObserver(changes) }
        changes = nil
        guard let engine else { return }
        engine.stop()
        engine.inputNode.removeTap(onBus: 0)
        self.engine = nil
        #if os(iOS)
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        #endif
    }

    /// A new request of the recognition takes what the microphone hears from now on.
    private func listen() {
        guard let recognizer else { return }
        feed.request?.endAudio()
        recognition?.cancel()
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        request.addsPunctuation = true
        request.taskHint = .dictation
        if recognizer.supportsOnDeviceRecognition { request.requiresOnDeviceRecognition = true }
        take += 1
        let current = take
        feed.request = request
        recognition = SpeechFeed.recognize(request, with: recognizer) { [weak self] text, final, error in
            Task { @MainActor in self?.heard(text, final: final, error: error, take: current) }
        }
    }

    /// A result of the recognition. The error is its description.
    private func heard(_ text: String?, final: Bool, error: String?, take: Int) {
        guard take == self.take, draft != nil else { return }
        if let text, !text.isEmpty, text != draft?.hearing {
            draft?.hearing = text
            misses = 0
            write()
        }
        guard final || error != nil, var draft else { return }
        // The stop's last words came.
        guard listening else { return end() }
        // The recognizer ended the request while the microphone listens: with an error, or at the minute of server
        // recognition. Its words stay, and a new request listens.
        if let error, draft.hearing.isEmpty {
            misses += 1
            if misses >= 3 {
                cancel()
                return report("Dictation stopped: \(error)")
            }
        }
        draft.keepHearing()
        self.draft = draft
        listen()
    }

    /// Writes the words into the box, unless the box holds another text than the dictation wrote last.
    private func write() {
        guard var draft else { return }
        guard draft.text.wrappedValue == draft.written else { return cancel() }
        let text = draft.withWords
        guard text != draft.written else { return }
        draft.written = text
        self.draft = draft
        wrote = text
        draft.text.wrappedValue = text
        draft.selection?.wrappedValue = TextSelection(insertionPoint: text.endIndex)
    }

    private static let demoSentence = "Check why Mira Chen's payout failed last night, and retry it once the fix is in."

    /// The demo has no microphone. Its words come one at a time, as recognition writes them, and it listens until the
    /// tap.
    private func playDemo() {
        listening = true
        take += 1
        let current = take
        demo = Task { [weak self] in
            var heard = ""
            for word in Self.demoSentence.split(separator: " ") {
                try? await Task.sleep(for: .milliseconds(180))
                guard !Task.isCancelled else { return }
                heard += (heard.isEmpty ? "" : " ") + word
                self?.heard(heard, final: false, error: nil, take: current)
            }
        }
    }

    /// The engine posts a change on a thread of its own, so the handler is made outside the main actor.
    nonisolated private static func watch(_ engine: AVAudioEngine,
                                          then handle: @escaping @Sendable () -> Void) -> any NSObjectProtocol {
        NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine,
                                               queue: nil) { _ in handle() }
    }
}

private enum DictationError: LocalizedError {
    case noMicrophone

    var errorDescription: String? { "Coconut finds no microphone to listen with." }
}

/// Ends the dictation of a message box: typing, a send or another draft in the box, and leaving the box or the app.
/// It sits on the whole box, as the box moves its buttons when it opens.
struct DictationEnds: ViewModifier {
    let dictation: Dictation
    let text: String
    @Environment(\.scenePhase) private var scenePhase

    func body(content: Content) -> some View {
        content
            .onChange(of: text) { _, new in dictation.textChanged(to: new) }
            // Only the background: a question of the system, such as the first one for the microphone, makes the app
            // inactive for a moment.
            .onChange(of: scenePhase) { _, phase in if phase == .background { dictation.stop() } }
            .onDisappear { dictation.cancel() }
    }
}

/// The microphone of a message box (Dictation), beside Send. It shows also in an empty box, as the dictation key of a
/// keyboard does. While it listens, the microphone is filled and pulses in a soft brown circle, as the plus, so a still
/// shows it too. A tap stops it.
struct ComposerDictationButton: View {
    @Environment(AppStore.self) private var store
    let dictation: Dictation
    @Binding var text: String
    /// The caret of the field, which follows the words. Nil for a field that keeps its own.
    var selection: Binding<TextSelection?>? = nil
    /// The circle of Send.
    @ScaledMetric(relativeTo: .body) private var size = 34

    var body: some View {
        let listening = dictation.listening
        Button {
            dictation.toggle($text, selection: selection, demo: store.isDemo) { store.error = $0 }
        } label: {
            Image(systemName: listening ? "mic.fill" : "mic")
                .font(.body)
                .foregroundStyle(listening ? AnyShapeStyle(BoardStyle.control) : AnyShapeStyle(.secondary))
                .symbolEffect(.pulse, isActive: listening)
                .frame(width: size, height: size)
                .background(BoardStyle.control.opacity(listening ? 0.12 : 0), in: Circle())
                .frame(minWidth: 44, minHeight: 44)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help(listening ? "Stop dictation" : "Dictate")
        .accessibilityLabel(listening ? "Stop dictation" : "Dictate")
        .accessibilityIdentifier("dictate")
    }
}

/// The request of the recognition that the microphone feeds. The audio thread reads it, and the main actor swaps it.
/// The dictation of a message box and the brainstorm listen through one.
final class SpeechFeed: @unchecked Sendable {
    private let lock = NSLock()
    private var current: SFSpeechAudioBufferRecognitionRequest?

    var request: SFSpeechAudioBufferRecognitionRequest? {
        get { lock.withLock { current } }
        set { lock.withLock { current = newValue } }
    }

    func append(_ buffer: AVAudioPCMBuffer) { request?.append(buffer) }

    /// The Microphone is on for Coconut. The first call asks the person.
    static func microphoneAllowed() async -> Bool {
        #if os(macOS)
        await AVCaptureDevice.requestAccess(for: .audio)
        #else
        await AVAudioApplication.requestRecordPermission()
        #endif
    }

    /// Speech Recognition is on for Coconut. The first call asks the person.
    static func allowed() async -> Bool {
        let status = await withCheckedContinuation {
            (done: CheckedContinuation<SFSpeechRecognizerAuthorizationStatus, Never>) in
            SFSpeechRecognizer.requestAuthorization { done.resume(returning: $0) }
        }
        return status == .authorized
    }

    /// The tap of the microphone runs on the audio thread, so it is made outside the main actor.
    static func tap(_ feed: SpeechFeed) -> AVAudioNodeTapBlock {
        { buffer, _ in feed.append(buffer) }
    }

    /// The recognizer calls back on a queue of its own, so the handler is made outside the main actor. It gets the
    /// words, whether they are final, and the description of an error.
    static func recognize(_ request: SFSpeechAudioBufferRecognitionRequest, with recognizer: SFSpeechRecognizer,
                          then handle: @escaping @Sendable (String?, Bool, String?) -> Void)
        -> SFSpeechRecognitionTask {
        recognizer.recognitionTask(with: request) { result, error in
            handle(result?.bestTranscription.formattedString, result?.isFinal ?? false, error?.localizedDescription)
        }
    }
}
