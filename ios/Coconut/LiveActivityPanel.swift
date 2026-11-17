import SwiftUI

/// The live view of a turn: what the agent does now, a film strip of the newest images, a timeline of its tool calls
/// and the files it touched. A Mac shows it in the details column next to the chat, an iPhone in a sheet over the chat.
struct LiveActivityPanel: View {
    let botId: String
    @Environment(AppStore.self) private var store
    @State private var watched: String?
    @State private var tab: Tab
    @State private var selectedImage: String?
    @State private var file: ActivityFileRoute?

    enum Tab: Hashable { case activity, files }

    init(botId: String, tab: Tab = .activity) {
        self.botId = botId
        _tab = State(initialValue: tab)
    }

    private var task: AgentTask? { store.liveTask(botId) }
    private var feed: TurnActivity? { task.flatMap { store.turnActivity[$0.id] } }
    /// The line of the chat about the turn that runs: its phase, its time and its model.
    private var working: ConversationActivity? { store.conversationActivity(botId) }

    var body: some View {
        VStack(spacing: 0) {
            LiveActivityHeader(feed: feed, working: working)
            Rectangle().fill(BoardStyle.line).frame(height: 1)
            if let feed, !feed.isEmpty, let task {
                ScrollView {
                    VStack(alignment: .leading, spacing: 0) {
                        if !feed.images.isEmpty {
                            LiveActivityFilmstrip(taskId: task.id, images: feed.filmstrip, selected: $selectedImage)
                                .padding(.horizontal, BoardStyle.Space.l).padding(.top, BoardStyle.Space.m)
                        }
                        HStack(spacing: BoardStyle.Space.xs) {
                            ChipButton(title: "Activity", selected: tab == .activity) { tab = .activity }
                            ChipButton(title: "Files", selected: tab == .files,
                                       count: feed.files.isEmpty ? nil : String(feed.files.count)) { tab = .files }
                                .accessibilityIdentifier("live-activity-files")
                        }
                        .padding(.horizontal, BoardStyle.Space.s).padding(.top, BoardStyle.Space.s)
                        switch tab {
                        case .activity:
                            ForEach(feed.timeline) { entry in
                                LiveActivityRow(entry: entry, turnRuns: feed.isRunning,
                                                open: entry.file.flatMap { path in open(path, taskId: task.id) },
                                                showImage: showImage(of: entry, in: feed))
                            }
                        case .files:
                            if feed.files.isEmpty {
                                Text("This turn has not opened a file yet.")
                                    .font(.subheadline).foregroundStyle(.secondary)
                                    .padding(BoardStyle.Space.l)
                            }
                            ForEach(feed.recentFiles) { row in
                                LiveActivityFileRow(file: row, open: open(row.path, taskId: task.id))
                            }
                        }
                    }
                    .padding(.bottom, BoardStyle.Space.l)
                }
            } else {
                VStack(spacing: BoardStyle.Space.xs) {
                    Text(working == nil ? "Nothing runs now" : "No steps yet").font(.callout.weight(.semibold))
                    Text("The commands, files and images of a turn show here while it runs.")
                        .font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
                }
                .padding(BoardStyle.Space.xl)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .background(BoardStyle.paper)
        .accessibilityIdentifier("live-activity-panel")
        .onAppear { watch(task?.id) }
        .onDisappear { watch(nil) }
        .onChange(of: task?.id) { _, id in watch(id) }
        .sheet(item: $file) { ActivityFileView(route: $0) }
    }

    private func watch(_ id: String?) {
        guard id != watched else { return }
        if let watched { store.unwatchActivity(watched) }
        if let id { store.watchActivity(id) }
        watched = id
        selectedImage = nil
    }

    /// The action that opens a file of the feed, or nil for a path that the Mac cannot find.
    private func open(_ path: String, taskId: String) -> (() -> Void)? {
        guard path.hasPrefix("/") || path.hasPrefix("~") else { return nil }
        return { file = ActivityFileRoute(taskId: taskId, path: path) }
    }

    /// The action that shows the image of a tool call in the film strip.
    private func showImage(of entry: ActivityEntry, in feed: TurnActivity) -> (() -> Void)? {
        guard let id = entry.images?.last, feed.images.contains(where: { $0.id == id }) else { return nil }
        return { selectedImage = id }
    }
}

/// The top of the live panel: the phase of the turn with its time and its model, and the calls that run now.
private struct LiveActivityHeader: View {
    let feed: TurnActivity?
    let working: ConversationActivity?

    var body: some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
            HStack(spacing: BoardStyle.Space.s) {
                Circle().fill(working != nil ? BoardStyle.positive : Color.secondary.opacity(0.4))
                    .frame(width: 8, height: 8)
                if let working {
                    ShimmerText(text: working.reconnecting ? "Reconnecting…" : working.phase.label,
                                active: !working.reconnecting)
                        .font(.callout.weight(.semibold))
                    Spacer(minLength: BoardStyle.Space.s)
                    TimelineView(.periodic(from: .now, by: 1)) { timeline in
                        Text(meta(working, at: timeline.date))
                            .font(.subheadline).monospacedDigit().foregroundStyle(.secondary).lineLimit(1)
                    }
                } else {
                    Text(feed?.endedAt != nil ? "Finished" : "Idle").font(.callout.weight(.semibold))
                    Spacer(minLength: BoardStyle.Space.s)
                    if let ran {
                        Text(ran).font(.subheadline).monospacedDigit().foregroundStyle(.secondary)
                    }
                }
            }
            ForEach((feed?.running ?? []).suffix(2)) { entry in
                Text(entry.line.isEmpty ? entry.title : entry.line)
                    .font(.subheadline.monospaced()).foregroundStyle(.secondary)
                    .lineLimit(1).truncationMode(entry.cutsStart ? .head : .tail).textSelection(.enabled)
                    .padding(.leading, BoardStyle.Space.l)
            }
        }
        .padding(.horizontal, BoardStyle.Space.l).padding(.vertical, BoardStyle.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func meta(_ working: ConversationActivity, at date: Date) -> String {
        var parts: [String] = []
        if let startedAt = working.startedAt { parts.append(ConversationActivity.elapsed(date.timeIntervalSince(startedAt))) }
        if let model = working.model, !model.isEmpty { parts.append(model) }
        return parts.joined(separator: " · ")
    }

    /// The time that a finished turn ran.
    private var ran: String? {
        guard let start = feed?.startedAt?.apiDate, let end = feed?.endedAt?.apiDate else { return nil }
        return ConversationActivity.elapsed(end.timeIntervalSince(start))
    }
}

/// One tool call of the timeline: its symbol, what it does in one line, and its time.
private struct LiveActivityRow: View {
    let entry: ActivityEntry
    /// A call runs only while its turn runs.
    let turnRuns: Bool
    let open: (() -> Void)?
    let showImage: (() -> Void)?

    private var running: Bool { entry.isRunning && turnRuns }

    var body: some View {
        if let action = open ?? showImage {
            Button(action: action) { content }
                .buttonStyle(HomePressStyle())
                .accessibilityHint(open != nil ? "Opens the file" : "Shows the image")
        } else {
            content
        }
    }

    private var content: some View {
        HStack(alignment: .firstTextBaseline, spacing: BoardStyle.Space.s) {
            Group {
                if running {
                    ProgressView().controlSize(.mini)
                } else {
                    Image(systemName: entry.failed ? "exclamationmark.circle.fill" : entry.symbol)
                        .foregroundStyle(entry.failed ? BoardStyle.destructive : Color.secondary)
                }
            }
            .font(.subheadline)
            .frame(width: 18, alignment: .center)
            VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                HStack(alignment: .firstTextBaseline, spacing: BoardStyle.Space.s) {
                    Text(entry.title).font(.callout.weight(.medium)).lineLimit(1).fixedSize()
                    Text(entry.line).font(.subheadline.monospaced()).foregroundStyle(.secondary)
                        .lineLimit(1).truncationMode(entry.cutsStart ? .head : .tail)
                }
                if entry.failed, let error = entry.error, !error.isEmpty {
                    Text(error).font(.subheadline).foregroundStyle(BoardStyle.destructive).lineLimit(2)
                }
            }
            Spacer(minLength: BoardStyle.Space.xs)
            if showImage != nil {
                Image(systemName: "photo").font(.subheadline).foregroundStyle(.secondary)
            }
            Group {
                if running, let start = entry.at.apiDate {
                    TimelineView(.periodic(from: .now, by: 1)) { timeline in
                        Text(ConversationActivity.elapsed(timeline.date.timeIntervalSince(start)))
                    }
                } else if let time = LiveActivityText.duration(entry.seconds) {
                    Text(time)
                }
            }
            .font(.subheadline).monospacedDigit().foregroundStyle(.tertiary)
        }
        .padding(.horizontal, BoardStyle.Space.l).padding(.vertical, 5)
        .frame(maxWidth: .infinity, alignment: .leading)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }
}

/// One file that the turn touched: its name, its folder, and whether the turn read or edited it.
private struct LiveActivityFileRow: View {
    let file: ActivityFile
    let open: (() -> Void)?

    var body: some View {
        if let open {
            Button(action: open) { content }.buttonStyle(HomePressStyle())
                .accessibilityIdentifier("live-activity-file")
        } else {
            content
        }
    }

    private var content: some View {
        HStack(alignment: .firstTextBaseline, spacing: BoardStyle.Space.s) {
            Image(systemName: file.edited ? "pencil" : "doc.text")
                .font(.subheadline).foregroundStyle(.secondary).frame(width: 18)
            VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                Text(file.name).font(.callout.weight(.medium)).lineLimit(1).truncationMode(.middle)
                if !file.folder.isEmpty {
                    Text(file.folder).font(.subheadline.monospaced()).foregroundStyle(.secondary)
                        .lineLimit(1).truncationMode(.head)
                }
            }
            Spacer(minLength: BoardStyle.Space.xs)
            Text(file.note).font(.subheadline).foregroundStyle(file.edited ? AnyShapeStyle(.primary) : AnyShapeStyle(.tertiary))
                .lineLimit(1).fixedSize()
            if open != nil {
                Image(systemName: "chevron.right").font(.caption.weight(.semibold)).foregroundStyle(.tertiary)
            }
        }
        .padding(.horizontal, BoardStyle.Space.l).padding(.vertical, 6)
        .frame(maxWidth: .infinity, alignment: .leading)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }
}

/// The newest images of the turn: one large, and a strip of the others under it. A new image shows at once, unless
/// the person chose an earlier one.
private struct LiveActivityFilmstrip: View {
    let taskId: String
    /// Newest first.
    let images: [ActivityImage]
    @Binding var selected: String?

    private var shown: ActivityImage? { images.first { $0.id == selected } ?? images.first }

    var body: some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
            if let shown {
                // The image takes the width of the panel and its own height, so no bars stand next to it.
                LiveActivityImageView(taskId: taskId, image: shown)
                    .frame(maxWidth: .infinity, minHeight: 120, maxHeight: 260)
                    .background(BoardStyle.surface)
                    .clipShape(RoundedRectangle(cornerRadius: BoardStyle.Radius.small))
                    .overlay(RoundedRectangle(cornerRadius: BoardStyle.Radius.small).stroke(BoardStyle.line))
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityLabel("\(shown.source) image, \(shown.name)")
                HStack(spacing: BoardStyle.Space.xs) {
                    Text(shown.source).font(.subheadline.weight(.medium))
                    Text(shown.name).font(.subheadline.monospaced()).foregroundStyle(.secondary)
                        .lineLimit(1).truncationMode(.middle)
                    Spacer(minLength: BoardStyle.Space.xs)
                    if let date = shown.at.apiDate {
                        Text(date, format: .dateTime.hour().minute().second())
                            .font(.subheadline).monospacedDigit().foregroundStyle(.tertiary)
                    }
                }
            }
            if images.count > 1 {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: BoardStyle.Space.s) {
                        ForEach(images) { image in
                            Button { selected = image.id } label: {
                                LiveActivityImageView(taskId: taskId, image: image, fill: true)
                                    .frame(width: 72, height: 46)
                                    .background(BoardStyle.surface)
                                    .clipShape(RoundedRectangle(cornerRadius: 6))
                                    .overlay(RoundedRectangle(cornerRadius: 6)
                                        .stroke(image.id == shown?.id ? BoardStyle.ink : BoardStyle.line,
                                                lineWidth: image.id == shown?.id ? 2 : 1))
                                    // The image that shows large stands out, also a white page in dark mode.
                                    .opacity(image.id == shown?.id ? 1 : 0.55)
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel("\(image.source) image, \(image.name)")
                        }
                    }
                    .padding(1)
                }
            }
        }
        .accessibilityIdentifier("live-activity-images")
    }
}

/// One image of a feed, loaded through the paired Mac.
private struct LiveActivityImageView: View {
    let taskId: String
    let image: ActivityImage
    var fill = false
    @Environment(AppStore.self) private var store
    @State private var loaded: PlatformImage?
    @State private var missing = false

    var body: some View {
        ZStack {
            if let picture = loaded ?? store.activityImages.object(forKey: (taskId + "/" + image.id) as NSString) {
                Image(platformImage: picture).resizable().aspectRatio(contentMode: fill ? .fill : .fit)
            } else if missing {
                Image(systemName: "photo").foregroundStyle(.tertiary)
            } else {
                ProgressView().controlSize(.small)
            }
        }
        .task(id: image.id) {
            loaded = nil
            missing = false
            loaded = await store.activityImage(taskId: taskId, image: image)
            missing = loaded == nil
        }
    }
}

/// The bar at the top of a sheet of the live view: a title, one line under it, and Done.
private struct LiveSheetBar: View {
    let title: String
    var detail: String? = nil
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        HStack(spacing: BoardStyle.Space.m) {
            VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                Text(title).font(.headline).lineLimit(1).truncationMode(.middle)
                if let detail, !detail.isEmpty {
                    Text(detail).font(.subheadline.monospaced()).foregroundStyle(.secondary)
                        .lineLimit(1).truncationMode(.head)
                }
            }
            Spacer(minLength: BoardStyle.Space.s)
            Button("Done") { dismiss() }
                .keyboardShortcut(.cancelAction)
                .accessibilityIdentifier("live-activity-done")
        }
        .padding(.horizontal, BoardStyle.Space.l).padding(.vertical, BoardStyle.Space.m)
        .overlay(alignment: .bottom) { Rectangle().fill(BoardStyle.line).frame(height: 1) }
    }
}

/// The start of a text file that the turn touched, in a monospaced page.
struct ActivityFileView: View {
    let route: ActivityFileRoute
    @Environment(AppStore.self) private var store
    @State private var preview: ActivityFilePreview?
    @State private var failure: String?

    var body: some View {
        VStack(spacing: 0) {
            LiveSheetBar(title: LiveActivityText.fileName(route.path), detail: LiveActivityText.folder(route.path))
            if let preview {
                GeometryReader { geometry in
                    ScrollView([.vertical, .horizontal]) {
                        Text(preview.text)
                            .font(.callout.monospaced()).textSelection(.enabled)
                            .padding(BoardStyle.Space.l)
                            // A short file starts at the top left of the page.
                            .frame(minWidth: geometry.size.width, minHeight: geometry.size.height, alignment: .topLeading)
                    }
                }
                HStack {
                    Text(preview.truncated ? "The first 64 KB of \(LiveActivityText.size(preview.size))"
                         : LiveActivityText.size(preview.size))
                    Spacer(minLength: BoardStyle.Space.s)
                    Text("As it is on the Mac now")
                }
                .font(.subheadline).foregroundStyle(.secondary)
                .padding(.horizontal, BoardStyle.Space.l).padding(.vertical, BoardStyle.Space.s)
                .overlay(alignment: .top) { Rectangle().fill(BoardStyle.line).frame(height: 1) }
            } else if let failure {
                InlineError(failure).padding(BoardStyle.Space.xl)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .background(BoardStyle.paper)
        #if os(macOS)
        .frame(minWidth: 620, idealWidth: 760, minHeight: 440, idealHeight: 560)
        #endif
        .accessibilityIdentifier("live-activity-file-preview")
        .task(id: route) {
            do { preview = try await store.activityFilePreview(route) }
            catch let error as ServiceError { failure = error.localizedDescription }
            catch { failure = "Couldn't open this file. Try again." }
        }
    }
}

/// The live panel as a sheet over the chat: on an iPhone, and on a Mac for a chat without a details column.
struct LiveActivitySheet: View {
    let botId: String
    @Environment(AppStore.self) private var store

    var body: some View {
        VStack(spacing: 0) {
            LiveSheetBar(title: store.botName(botId))
            LiveActivityPanel(botId: botId)
        }
        .background(BoardStyle.paper)
        #if os(macOS)
        .frame(width: 420, height: 620)
        #else
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
        #endif
    }
}

/// The images of the preview data: two pages of a payout dashboard, drawn here, as a browser screenshot shows them.
enum LiveActivityPreviewImage {
    @MainActor
    static func image(_ id: String) -> PlatformImage? {
        let renderer = ImageRenderer(content: Page(second: id.hasSuffix("2")).frame(width: 960, height: 600))
        renderer.scale = 1
        return renderer.cgImage.map { PlatformImage($0) }
    }

    private struct Page: View {
        let second: Bool

        private var rows: [(String, String, String)] {
            second ? [("Mira Chen", "$1,284.00", "Paid twice"), ("Otto Weiss", "$412.50", "Paid twice"),
                      ("Studio Kaya", "$3,090.75", "Paid twice"), ("Lena Park", "$96.00", "Paid twice")]
                   : [("po_1Qx7", "$1,284.00", "Paid"), ("po_1Qx8", "$1,284.00", "Paid"), ("po_1Qx9", "$412.50", "Paid"),
                      ("po_1QxA", "$412.50", "Paid"), ("po_1QxB", "$3,090.75", "Paid")]
        }

        var body: some View {
            VStack(alignment: .leading, spacing: 0) {
                HStack(spacing: 12) {
                    Circle().fill(Color(hex: second ? "FF90E8" : "635BFF")).frame(width: 28, height: 28)
                    Text(second ? "Gumroad Admin" : "Stripe Dashboard").font(.system(size: 20, weight: .semibold))
                    Spacer()
                    Text(second ? "Sellers" : "Payouts").font(.system(size: 16)).foregroundStyle(.gray)
                }
                .padding(.horizontal, 32).frame(height: 64)
                Rectangle().fill(Color.black.opacity(0.08)).frame(height: 1)
                Text(second ? "Sellers paid twice on Friday" : "Payouts · Friday").font(.system(size: 30, weight: .bold))
                    .padding(.horizontal, 32).padding(.top, 28).padding(.bottom, 16)
                ForEach(rows, id: \.0) { row in
                    HStack {
                        Text(row.0).font(.system(size: 18, design: second ? .default : .monospaced))
                        Spacer()
                        Text(row.1).font(.system(size: 18)).monospacedDigit()
                        Text(row.2).font(.system(size: 15, weight: .medium))
                            .padding(.horizontal, 12).padding(.vertical, 5)
                            .background(Color(hex: second ? "FDE2E2" : "D7F7C2"), in: Capsule())
                            .frame(width: 140, alignment: .trailing)
                    }
                    .padding(.horizontal, 32).frame(height: 58)
                    Rectangle().fill(Color.black.opacity(0.06)).frame(height: 1).padding(.horizontal, 32)
                }
                Spacer()
            }
            .foregroundStyle(.black)
            .background(Color.white)
            .environment(\.colorScheme, .light)
        }
    }
}
