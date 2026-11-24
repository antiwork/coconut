// The queue of a chat: the messages that the person sent while the agent works. They wait above the message field
// until a turn takes them, as the queued prompts of Codex and Claude Code do.
import SwiftUI
import UniformTypeIdentifiers

/// One message that waits for the next turn of a running task (backend message_queue.py).
struct QueuedMessage: Codable, Identifiable, Equatable, Sendable {
    var id: String
    var text: String
    var createdAt: String
    var attachmentIds: [String] = []
    /// "queued", or "steering" when the message already went to the running turn.
    var state: String = "queued"
    /// The Mac can give this message to the running turn at once.
    var canSteer: Bool = false

    init(id: String, text: String, createdAt: String, attachmentIds: [String] = [], state: String = "queued", canSteer: Bool = false) {
        self.id = id; self.text = text; self.createdAt = createdAt
        self.attachmentIds = attachmentIds; self.state = state; self.canSteer = canSteer
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(String.self, forKey: .id)
        text = try values.decode(String.self, forKey: .text)
        createdAt = try values.decodeIfPresent(String.self, forKey: .createdAt) ?? ""
        attachmentIds = try values.decodeIfPresent([String].self, forKey: .attachmentIds) ?? []
        state = try values.decodeIfPresent(String.self, forKey: .state) ?? "queued"
        canSteer = try values.decodeIfPresent(Bool.self, forKey: .canSteer) ?? false
    }
}

/// One row of the queue of a chat.
struct QueueItem: Identifiable, Equatable {
    enum State: Equatable {
        /// The Mac did not confirm the message yet.
        case sending
        case queued
        /// The message went to the running turn.
        case steering
    }
    let id: String
    /// The task that holds the message. A message on its way has none.
    var taskId: String?
    var text: String
    var attachments: Int
    var state: State
    var canSteer = false

    /// The person can change, move and remove the message.
    var canChange: Bool { state == .queued && taskId != nil }

    /// The row while the message goes to a new agent: Sending, without actions.
    var sending: QueueItem {
        var row = self
        row.state = .sending
        return row
    }

    /// One line of the message for its row.
    var preview: String { MessageQueue.preview(text, attachments: attachments) }
}

enum MessageQueue {
    /// "2 queued" for the working line and the row of the chat. Nil without a queue.
    static func label(_ count: Int) -> String? { count > 0 ? "\(count) queued" : nil }

    /// The first line with text, on one line. A message of attachments alone names them.
    static func preview(_ text: String, attachments: Int) -> String {
        if AttachmentOnly.isMarker(text) || text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return attachments == 1 ? "1 attachment" : "\(attachments) attachments"
        }
        let line = text.split(whereSeparator: \.isNewline).map { $0.trimmingCharacters(in: .whitespaces) }.first { !$0.isEmpty } ?? ""
        return line.split(separator: " ", omittingEmptySubsequences: true).joined(separator: " ")
    }

    /// The rows of a chat: the messages that its tasks hold, then the messages on their way.
    static func items(tasks: [AgentTask], sending: [OutgoingMessage]) -> [QueueItem] {
        let held = tasks.flatMap { task in
            (task.queuedMessages ?? []).map { message in
                QueueItem(id: message.id, taskId: task.id, text: message.text, attachments: message.attachmentIds.count,
                          state: message.state == "steering" ? .steering : .queued, canSteer: message.canSteer)
            }
        }
        return held + sending.map {
            QueueItem(id: "outgoing-" + $0.id, taskId: nil, text: $0.request.text,
                      attachments: $0.request.attachmentIds.count, state: .sending)
        }
    }

    /// The IDs after the row `id` moved to the place of the row `target`. A row that moves down goes after the
    /// target, and a row that moves up goes before it, as a dragged row of a list does.
    static func moved(_ ids: [String], id: String, onto target: String) -> [String] {
        guard id != target, let from = ids.firstIndex(of: id), let to = ids.firstIndex(of: target) else { return ids }
        var result = ids
        result.remove(at: from)
        result.insert(id, at: to)
        return result
    }

    /// The IDs after the row `id` moved by `offset` places: -1 is up. A row at an end stays.
    static func moved(_ ids: [String], id: String, by offset: Int) -> [String] {
        guard let from = ids.firstIndex(of: id), ids.indices.contains(from + offset) else { return ids }
        return moved(ids, id: id, onto: ids[from + offset])
    }

    /// The transcript without the messages of the queue and without their attachments. `ids` are the IDs of the rows
    /// as the transcript shows them, and `attachments` the IDs of the files.
    static func transcript(_ messages: [Message], without ids: Set<String>, attachments: Set<String>) -> [Message] {
        guard !ids.isEmpty || !attachments.isEmpty else { return messages }
        return messages.filter { message in
            if ids.contains(message.id) { return false }
            if message.role == "user", message.kind == "artifact", let file = message.artifactId, attachments.contains(file) { return false }
            return true
        }
    }
}

/// The queue above the message field: one compact row for each message, with its place, the start of its text, the
/// count of its attachments, and its actions.
struct MessageQueueStack: View {
    @Environment(AppStore.self) private var store
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let botId: String
    /// Counts the requests of Up-Arrow in the empty field: each one edits the last message that can change.
    var editLast = 0
    @State private var editing: String?
    @State private var draft = ""
    @State private var dropTarget: String?
    /// The messages on their way to a new agent. Their rows show Sending until the queue lets them go.
    @State private var moving: Set<String> = []
    @FocusState private var editorFocused: Bool

    /// More rows than this scroll, so the queue never covers the chat.
    static let shownRows = 5
    #if os(macOS)
    static let rowHeight: CGFloat = 30
    #else
    static let rowHeight: CGFloat = 44
    #endif

    private var items: [QueueItem] { store.messageQueue(botId) }
    /// A queued message can go to a new agent of the same project, as a draft can (Send to a new agent). The Coconut
    /// chat has one chat.
    private var canSendToNewAgent: Bool { store.bots.first { $0.id == botId }.map { !$0.coconut } ?? false }

    var body: some View {
        let items = items
        if !items.isEmpty {
            VStack(alignment: .leading, spacing: 0) {
                HStack(spacing: BoardStyle.Space.xs) {
                    Image(systemName: "text.line.first.and.arrowtriangle.forward").imageScale(.small)
                    Text(MessageQueue.label(items.count) ?? "")
                    Text("· next turn").foregroundStyle(.tertiary)
                }
                .font(.caption.weight(.medium)).foregroundStyle(.secondary)
                .padding(.horizontal, BoardStyle.Space.m).padding(.top, BoardStyle.Space.s).padding(.bottom, BoardStyle.Space.xs)
                .accessibilityElement(children: .combine)
                .accessibilityIdentifier("queue-count")
                let rows = VStack(spacing: 0) {
                    ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                        if index > 0 { Divider().overlay(BoardStyle.line).padding(.leading, BoardStyle.Space.m + 24) }
                        row(item, position: index + 1, items: items)
                    }
                }
                if items.count > Self.shownRows {
                    ScrollView { rows }.frame(maxHeight: Self.rowHeight * (CGFloat(Self.shownRows) + 0.5))
                } else {
                    rows
                }
            }
            .padding(.bottom, BoardStyle.Space.xs)
            .modifier(ComposerSurface(shape: RoundedRectangle(cornerRadius: BoardStyle.Radius.medium, style: .continuous)))
            .padding(.bottom, BoardStyle.Space.s)
            .animation(BoardStyle.fade(reduceMotion: reduceMotion), value: items.map(\.id))
            .accessibilityIdentifier("message-queue")
            .onChange(of: editLast) { _, _ in
                guard editing == nil, let item = items.last(where: \.canChange) else { return }
                draft = AttachmentOnly.isMarker(item.text) ? "" : item.text
                editing = item.id
                editorFocused = true
            }
            .onChange(of: items.map(\.id)) { _, ids in
                // A turn took the message that the person edits.
                if let editing, !ids.contains(editing) { self.editing = nil }
            }
        }
    }

    @ViewBuilder
    private func row(_ item: QueueItem, position: Int, items: [QueueItem]) -> some View {
        let movable = items.filter(\.canChange).map(\.id)
        // A message on its way to a new agent shows Sending, without actions, and does not move.
        let shown = moving.contains(item.id) ? item.sending : item
        if editing == item.id {
            editor(item, position: position)
        } else {
            QueueRow(item: shown, position: position, targeted: dropTarget == item.id,
                     canMoveUp: shown.canChange && movable.first != item.id,
                     canMoveDown: shown.canChange && movable.last != item.id,
                     canSendToNewAgent: canSendToNewAgent) { action in
                perform(action, on: item, movable: movable)
            }
            .modifier(QueueRowDrag(item: shown, target: $dropTarget) { dragged in
                guard item.canChange, movable.contains(dragged) else { return false }
                order(MessageQueue.moved(movable, id: dragged, onto: item.id), of: item)
                return true
            })
        }
    }

    private func editor(_ item: QueueItem, position: Int) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: BoardStyle.Space.s) {
            QueuePosition(position: position)
            TextField("Message", text: $draft, axis: .vertical)
                .textFieldStyle(.plain).font(.subheadline).lineLimit(1...4)
                .focused($editorFocused)
                .onSubmit { save(item) }
                #if os(macOS)
                .onExitCommand { editing = nil }
                #endif
                .accessibilityIdentifier("queue-edit-field")
            Button("Cancel") { editing = nil }
                .buttonStyle(.plain).foregroundStyle(.secondary)
            Button("Save") { save(item) }
                .buttonStyle(.plain).fontWeight(.semibold)
                .disabled(draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .accessibilityIdentifier("queue-edit-save")
        }
        .font(.subheadline)
        .padding(.horizontal, BoardStyle.Space.m)
        .frame(minHeight: Self.rowHeight)
        .padding(.vertical, BoardStyle.Space.xs)
    }

    private func save(_ item: QueueItem) {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        editing = nil
        guard !text.isEmpty, text != item.text, let task = item.taskId else { return }
        Task { await store.editQueued(item.id, task: task, text: text) }
    }

    private func order(_ ids: [String], of item: QueueItem) {
        guard let task = item.taskId else { return }
        Task { await store.orderQueued(ids, task: task) }
    }

    private func perform(_ action: QueueRow.Action, on item: QueueItem, movable: [String]) {
        guard let task = item.taskId, !moving.contains(item.id) else { return }
        switch action {
        case .newAgent:
            moving.insert(item.id)
            Task {
                await store.sendQueuedToNewAgent(item.id, task: task, chat: botId)
                moving.remove(item.id)
            }
        case .edit:
            draft = AttachmentOnly.isMarker(item.text) ? "" : item.text
            editing = item.id
            editorFocused = true
        case .remove: Task { await store.removeQueued(item.id, task: task) }
        case .steer: Task { await store.steerQueued(item.id, task: task) }
        case .moveUp: order(MessageQueue.moved(movable, id: item.id, by: -1), of: item)
        case .moveDown: order(MessageQueue.moved(movable, id: item.id, by: 1), of: item)
        }
    }
}

/// The place of a message in the queue.
private struct QueuePosition: View {
    let position: Int
    var body: some View {
        Text("\(position)")
            .font(.caption.monospacedDigit().weight(.medium)).foregroundStyle(.secondary)
            .frame(width: 16, alignment: .trailing)
            .accessibilityHidden(true)
    }
}

/// One message of the queue. A Mac shows the actions when the pointer is over the row; an iPhone has them in a menu.
/// Both have them in the menu of a secondary click or a hold.
private struct QueueRow: View {
    enum Action { case edit, remove, steer, newAgent, moveUp, moveDown }
    let item: QueueItem
    let position: Int
    let targeted: Bool
    let canMoveUp: Bool
    let canMoveDown: Bool
    let canSendToNewAgent: Bool
    let perform: (Action) -> Void
    @State private var hovered = false

    var body: some View {
        HStack(spacing: BoardStyle.Space.s) {
            QueuePosition(position: position)
            Text(item.preview)
                .font(.subheadline).lineLimit(1).truncationMode(.tail)
                .foregroundStyle(item.state == .sending ? .secondary : .primary)
                .frame(maxWidth: .infinity, alignment: .leading)
            if item.attachments > 0 {
                Label("\(item.attachments)", systemImage: "paperclip")
                    .labelStyle(.titleAndIcon).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                    .accessibilityLabel(item.attachments == 1 ? "1 attachment" : "\(item.attachments) attachments")
            }
            switch item.state {
            case .sending: Text("Sending").font(.caption).foregroundStyle(.tertiary)
            case .steering: Text("Steering").font(.caption).foregroundStyle(.secondary)
            case .queued: actions
            }
        }
        .padding(.horizontal, BoardStyle.Space.m)
        .frame(minHeight: MessageQueueStack.rowHeight)
        .background(targeted ? BoardStyle.ink.opacity(0.07) : .clear)
        .contentShape(Rectangle())
        #if os(macOS)
        .onHover { hovered = $0 }
        #endif
        .contextMenu { if item.canChange { menuItems } }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Queued \(position): \(item.preview)")
        .accessibilityIdentifier("queue-row-\(position)")
    }

    @ViewBuilder
    private var actions: some View {
        if item.canChange {
            #if os(macOS)
            HStack(spacing: 2) {
                if item.canSteer { icon("arrow.turn.down.right", "Send now", .steer) }
                icon("pencil", "Edit", .edit)
                icon("xmark", "Remove", .remove)
            }
            .opacity(hovered ? 1 : 0)
            // The buttons keep their place, so the text does not move when the pointer comes.
            .accessibilityHidden(!hovered)
            #else
            Menu { menuItems } label: {
                Image(systemName: "ellipsis").foregroundStyle(.secondary)
                    .frame(width: 36, height: MessageQueueStack.rowHeight).contentShape(Rectangle())
            }
            .accessibilityLabel("Actions of queued message \(position)")
            .accessibilityIdentifier("queue-actions-\(position)")
            #endif
        }
    }

    private func icon(_ symbol: String, _ title: String, _ action: Action) -> some View {
        Button { perform(action) } label: {
            Image(systemName: symbol).font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                .frame(width: 24, height: 24).contentShape(Rectangle())
        }
        .buttonStyle(.plain).help(title).accessibilityLabel(title)
    }

    @ViewBuilder
    private var menuItems: some View {
        if item.canSteer { Button("Send now", systemImage: "arrow.turn.down.right") { perform(.steer) } }
        // The message leaves the queue for a new agent of the same project, with its files (sendQueuedToNewAgent).
        if canSendToNewAgent {
            Button("Send to a new agent", systemImage: "square.and.pencil") { perform(.newAgent) }
                .accessibilityIdentifier("queue-new-agent")
        }
        Button("Edit", systemImage: "pencil") { perform(.edit) }
        if canMoveUp { Button("Move up", systemImage: "arrow.up") { perform(.moveUp) } }
        if canMoveDown { Button("Move down", systemImage: "arrow.down") { perform(.moveDown) } }
        Button("Remove", systemImage: "trash", role: .destructive) { perform(.remove) }
    }
}

/// A drag of a row moves the message in the queue. The row under the drag shows a tint.
private struct QueueRowDrag: ViewModifier {
    let item: QueueItem
    @Binding var target: String?
    let drop: (String) -> Bool

    func body(content: Content) -> some View {
        if item.canChange {
            content
                .onDrag { NSItemProvider(object: item.id as NSString) }
                .onDrop(of: [UTType.plainText], isTargeted: Binding(
                    get: { target == item.id },
                    set: { inside in if inside { target = item.id } else if target == item.id { target = nil } })) { providers in
                    guard let provider = providers.first else { return false }
                    _ = provider.loadObject(ofClass: NSString.self) { value, _ in
                        guard let id = value as? String else { return }
                        Task { @MainActor in _ = drop(id) }
                    }
                    return true
                }
        } else {
            content
        }
    }
}
