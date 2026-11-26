import SwiftUI

// The share sheet of Coconut: what the share holds, an optional note, and the chat that it goes to. The extensions of
// the iPhone and the Mac show this one view.

/// The face of a team or a person as the app drew it, or the first letter of its title on a fill of the faces. A
/// project and a chat with an agent have no face.
struct ShareFace: View {
    let destination: ShareDestination
    let size: CGFloat

    var body: some View {
        if destination.kind == .project || destination.kind == .agent || destination.peerIsBot == true {
            EmptyView()
        } else {
            face
        }
    }

    private var face: some View {
        Group {
            if let face = destination.face, let image = PlatformImage(data: face) {
                Image(platformImage: image).resizable().interpolation(.high).scaledToFit()
            } else {
                ShareMonogram(destination: destination, size: size)
            }
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }
}

/// The first letter of the title of a place on one of the fills of the faces, chosen by the ID of the place. With
/// `husk`, the letter is white on the brown of the app icon (BoardStyle.husk).
struct ShareMonogram: View {
    let destination: ShareDestination
    let size: CGFloat
    var husk = false

    var body: some View {
        let color = BoardStyle.faceColors[Int(destination.id.utf8.reduce(UInt64(5381)) { $0 &* 33 &+ UInt64($1) } % UInt64(BoardStyle.faceColors.count))]
        Circle().fill(husk ? BoardStyle.husk : Color(hex: color.fill)).overlay {
            Text(String(destination.title.first { $0.isLetter || $0.isNumber } ?? "#").uppercased())
                .font(.system(size: size * 0.5, weight: .semibold, design: .rounded))
                .foregroundStyle(husk ? Color.white : Color(hex: color.ink))
        }
        .frame(width: size, height: size)
    }
}

struct ShareComposeView: View {
    @Bindable var model: ShareModel
    @FocusState private var noteFocused: Bool
    #if !os(macOS)
    @State private var picking = false
    #endif

    var body: some View {
        #if os(macOS)
        VStack(alignment: .leading, spacing: 0) {
            if model.needsApp {
                needsApp.padding(BoardStyle.Space.xl)
            } else {
                VStack(alignment: .leading, spacing: BoardStyle.Space.m) {
                    HStack(spacing: BoardStyle.Space.s) {
                        Text("To").foregroundStyle(.secondary)
                        destinationMenu
                        Spacer(minLength: 0)
                    }
                    noteField
                    itemList
                    status
                }
                .padding(BoardStyle.Space.l)
            }
            Divider()
            HStack(spacing: BoardStyle.Space.s) {
                if case .sending(let step, let steps) = model.phase {
                    CoconutLoader(.row, label: nil).foregroundStyle(.secondary)
                    Text(Self.progress(step, steps)).font(.subheadline).foregroundStyle(.secondary)
                }
                Spacer()
                Button("Cancel") { model.cancel() }.keyboardShortcut(.cancelAction)
                if !model.needsApp {
                    Button("Send") { Task { await model.send() } }
                        .buttonStyle(.borderedProminent)
                        .keyboardShortcut(.defaultAction)
                        .disabled(!model.canSend)
                        .accessibilityIdentifier("share-send")
                }
            }
            .padding(BoardStyle.Space.m)
        }
        .frame(width: 440)
        .background(BoardStyle.paper)
        .tint(BoardStyle.control)
        .task { noteFocused = true }
        #else
        NavigationStack {
            Group {
                if model.needsApp {
                    needsApp.padding(BoardStyle.Space.xl).frame(maxHeight: .infinity)
                } else {
                    ScrollView {
                        VStack(alignment: .leading, spacing: BoardStyle.Space.m) {
                            Button { picking = true } label: { destinationRow }
                                .buttonStyle(.plain)
                                .disabled(model.isSending)
                                .accessibilityIdentifier("share-destination")
                            noteField
                            itemList
                            status
                        }
                        .padding(BoardStyle.Space.l)
                    }
                    .scrollDismissesKeyboard(.interactively)
                }
            }
            .background(BoardStyle.paper)
            .navigationTitle("Coconut")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { model.cancel() } }
                ToolbarItem(placement: .confirmationAction) {
                    if model.isSending {
                        CoconutLoader(.button, label: "Sending")
                    } else if !model.needsApp {
                        Button("Send") { Task { await model.send() } }
                            .fontWeight(.semibold)
                            .disabled(!model.canSend)
                            .accessibilityIdentifier("share-send")
                    }
                }
            }
            .navigationDestination(isPresented: $picking) {
                ShareDestinationList(model: model) { picking = false }
            }
        }
        .tint(BoardStyle.control)
        #endif
    }

    static func progress(_ step: Int, _ steps: Int) -> String {
        steps > 1 && step > 0 ? "Sending \(step) of \(steps)" : "Sending"
    }

    private var needsApp: some View {
        VStack(spacing: BoardStyle.Space.s) {
            Text("Open Coconut first").font(.headline)
            Text("Coconut lists your chats here after the app is open and paired with your Mac. Then share again.")
                .font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
    }

    private var noteField: some View {
        TextField("Add a note", text: $model.note, axis: .vertical)
            .lineLimit(2...6)
            .textFieldStyle(.plain)
            .focused($noteFocused)
            .disabled(model.isSending)
            .padding(BoardStyle.Space.m)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.small, style: .continuous))
            .accessibilityIdentifier("share-note")
    }

    @ViewBuilder
    private var itemList: some View {
        if !model.rows.isEmpty {
            VStack(spacing: 0) {
                ForEach(model.rows) { row in
                    if row.id != model.rows.first?.id { Divider().padding(.leading, 36 + 2 * BoardStyle.Space.m) }
                    HStack(spacing: BoardStyle.Space.m) {
                        Group {
                            if let thumbnail = row.thumbnail {
                                Image(decorative: thumbnail, scale: 1).resizable().scaledToFill()
                            } else {
                                Image(systemName: row.symbol).font(.body).foregroundStyle(.secondary)
                            }
                        }
                        .frame(width: 36, height: 36)
                        .background(BoardStyle.paper)
                        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                        VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                            Text(row.title).font(.subheadline).lineLimit(2).truncationMode(.middle)
                            Text(row.detail).font(.footnote).foregroundStyle(.secondary).lineLimit(1)
                        }
                        Spacer(minLength: 0)
                    }
                    .padding(.horizontal, BoardStyle.Space.m)
                    .padding(.vertical, BoardStyle.Space.s)
                    .accessibilityElement(children: .combine)
                }
            }
            .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.small, style: .continuous))
        }
    }

    @ViewBuilder
    private var status: some View {
        if let problem = model.problem {
            Label { Text(problem) } icon: { Image(systemName: "exclamationmark.circle").imageScale(.small) }
                .font(.footnote).foregroundStyle(BoardStyle.accent)
                .accessibilityIdentifier("share-problem")
        }
        if let skipped = model.skippedText {
            Text(skipped).font(.footnote).foregroundStyle(.secondary)
        }
        #if !os(macOS)
        if case .sending(let step, let steps) = model.phase {
            Text(Self.progress(step, steps)).font(.footnote).foregroundStyle(.secondary)
        }
        #endif
    }

    #if os(macOS)
    private var destinationMenu: some View {
        Menu {
            ForEach(model.directory?.sections() ?? [], id: \.kind) { section in
                Section(section.kind.section) {
                    ForEach(section.items) { item in
                        Button(item.detail.map { item.title + " · " + $0 } ?? item.title) { model.destination = item }
                            .disabled(model.hasFiles && !item.takesFiles)
                    }
                }
            }
        } label: {
            HStack(spacing: BoardStyle.Space.s) {
                if let destination = model.destination {
                    ShareFace(destination: destination, size: 22)
                    Text(destination.detail.map { destination.title + " · " + $0 } ?? destination.title)
                        .font(.body.weight(.medium)).lineLimit(1)
                } else {
                    Text("Choose a chat")
                }
                Image(systemName: "chevron.up.chevron.down").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            }
            .padding(.leading, BoardStyle.Space.xs).padding(.trailing, BoardStyle.Space.s)
            .frame(height: 30)
            .background(BoardStyle.surface, in: Capsule())
            .contentShape(Capsule())
        }
        // A Mac draws a menu as a pull-down button with its own image size. The sheet draws the label itself, as the
        // plus menu of the composer does.
        .menuStyle(.button).buttonStyle(.plain).menuIndicator(.hidden)
        .fixedSize()
        .disabled(model.isSending)
        .accessibilityIdentifier("share-destination")
    }
    #else
    private var destinationRow: some View {
        HStack(spacing: BoardStyle.Space.m) {
            Text("To").foregroundStyle(.secondary)
            if let destination = model.destination {
                ShareFace(destination: destination, size: 32)
                VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                    Text(destination.title).font(.body.weight(.medium)).lineLimit(1)
                    if let detail = destination.detail {
                        Text(detail).font(.footnote).foregroundStyle(.secondary).lineLimit(1)
                    }
                }
            } else {
                Text("Choose a chat")
            }
            Spacer(minLength: 0)
            Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
        }
        .padding(.horizontal, BoardStyle.Space.m)
        .frame(minHeight: 52)
        .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.small, style: .continuous))
        .contentShape(Rectangle())
    }
    #endif
}

#if !os(macOS)
/// The chats that take the share, in the sections of the app: agents, teams, direct messages.
struct ShareDestinationList: View {
    let model: ShareModel
    let done: () -> Void
    @State private var query = ""

    var body: some View {
        List {
            ForEach(model.directory?.sections(matching: query) ?? [], id: \.kind) { section in
                Section(section.kind.section) {
                    ForEach(section.items) { item in
                        let refused = model.hasFiles && !item.takesFiles
                        Button {
                            model.destination = item
                            done()
                        } label: {
                            HStack(spacing: BoardStyle.Space.m) {
                                ShareFace(destination: item, size: 36)
                                VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                                    Text(item.title).lineLimit(1)
                                    if let detail = refused ? "Text and links only" : item.detail {
                                        Text(detail).font(.footnote).foregroundStyle(.secondary).lineLimit(1)
                                    }
                                }
                                Spacer(minLength: 0)
                                if item.id == model.destination?.id {
                                    Image(systemName: "checkmark").font(.body.weight(.semibold))
                                }
                            }
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .disabled(refused)
                        .opacity(refused ? 0.5 : 1)
                        .accessibilityIdentifier("share-chat-" + item.id)
                    }
                }
            }
        }
        .listStyle(.plain)
        .searchable(text: $query, prompt: "Search")
        .navigationTitle("Send to")
        .navigationBarTitleDisplayMode(.inline)
    }
}
#endif
