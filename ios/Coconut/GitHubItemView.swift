import SwiftUI

/// An issue or a pull request in Coconut (GitHubItem): the state and the title, the people and the labels, for a pull
/// request its checks, review, changes and merge, then the text and the comments, with a box to comment under them.
/// The menu closes and reopens it, asks an agent and copies the link. Nothing here opens github.com: the person reads
/// and answers the work of the agents where the rest of the work is.
struct GitHubItemView: View {
    @Environment(AppStore.self) private var store
    let route: GitHubItemRoute
    @State private var item: GitHubItem?
    @State private var failure: String?
    @State private var busy = false
    @State private var actionError: String?
    @State private var confirmingMerge = false
    @State private var asking = false
    @State private var loads = 0

    var body: some View {
        BoardPage(spacing: BoardStyle.Space.l) {
            if let item {
                header(item)
                if item.pull != nil { GitHubPullSummary(item: item, route: route, busy: busy) { confirmingMerge = true } }
                if let actionError {
                    Text(actionError).font(.subheadline).foregroundStyle(BoardStyle.destructive)
                        .fixedSize(horizontal: false, vertical: true).accessibilityIdentifier("github-item-error")
                }
                conversation(item)
            } else if let failure {
                BoardLoadNotice(title: "GitHub unavailable", message: failure) { loads += 1 }
                NavigationLink("GitHub connection") { GitHubSettingsView() }.font(.callout)
            } else {
                BoardLoading(title: "Loading \(route.repository) #\(route.number)")
            }
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            if item != nil {
                ReplyBox(draftKey: "github:\(route.repository)#\(route.number)", placeholder: "Comment",
                         identifier: "github-comment") { text, key in await comment(text, key: key) }
            }
        }
        .navigationTitle("#\(route.number)")
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Menu { menu } label: { Label("Actions", systemImage: "ellipsis.circle") }
                    .disabled(item == nil || busy)
                    .accessibilityIdentifier("github-item-menu")
            }
        }
        .task(id: loads) { await load() }
        .refreshable { await load() }
        .confirmationDialog(mergeTitle, isPresented: $confirmingMerge, titleVisibility: .visible) {
            Button(mergeLabel) { Task { await merge() } }.accessibilityIdentifier("github-merge-confirm")
        } message: {
            Text("GitHub merges the commit that you see now. A newer push stops the merge.")
        }
        .sheet(isPresented: $asking) {
            if let item {
                AskAgentSheet(summary: "Work on \(item.repository)#\(item.number): \(item.title)", link: item.url ?? "",
                              keep: "Keep the work and its results in this \(item.isPull ? "pull request" : "issue"), so the person sees them in Coconut.")
            }
        }
    }

    // MARK: Parts

    private func header(_ item: GitHubItem) -> some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
            Text("\(item.repository) #\(item.number)").font(.footnote.monospacedDigit()).foregroundStyle(.secondary)
                .lineLimit(1).truncationMode(.middle)
            Text(item.title).font(.title3.weight(.semibold)).textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true).accessibilityAddTraits(.isHeader)
            GitHubFlow(spacing: BoardStyle.Space.s, lineSpacing: BoardStyle.Space.xs) {
                GitHubStateChip(item: item)
                if let author = item.author {
                    HStack(spacing: BoardStyle.Space.xs) {
                        GitHubAvatar(person: author)
                        Text([author.login, GitHubPreview.age(of: item.createdAt)].compactMap { $0 }.joined(separator: " · "))
                            .lineLimit(1)
                    }
                }
                ForEach(item.labels, id: \.self) { GitHubLabelChip(label: $0) }
                if !item.assignees.isEmpty {
                    HStack(spacing: BoardStyle.Space.xs) {
                        Image(systemName: "person").imageScale(.small)
                        Text(item.assignees.map(\.login).joined(separator: ", ")).lineLimit(1)
                    }
                    .accessibilityLabel("Assigned to \(item.assignees.map(\.login).joined(separator: ", "))")
                }
            }
            .font(.footnote).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder private func conversation(_ item: GitHubItem) -> some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.m) {
            if !item.body.isEmpty {
                GitHubEntryTile(author: item.author, verb: "wrote", date: item.createdAt, text: item.body,
                                own: item.author?.login == item.viewer)
            }
            if item.moreComments > 0 {
                Text(item.moreComments == 1 ? "1 earlier comment is not shown" : "\(item.moreComments) earlier comments are not shown")
                    .font(.footnote).foregroundStyle(.secondary)
            }
            ForEach(item.comments) { entry in
                GitHubEntryTile(author: entry.author, verb: Self.verb(entry), date: entry.createdAt, text: entry.body,
                                own: entry.author?.login == item.viewer, review: entry.state)
            }
            if item.body.isEmpty && item.comments.isEmpty {
                Text("No description or comments yet.").font(.subheadline).foregroundStyle(.secondary)
            }
        }
    }

    private static func verb(_ entry: GitHubItem.Entry) -> String {
        switch entry.state {
        case "approved": "approved"
        case "changes_requested": "requested changes"
        case "dismissed": "reviewed (dismissed)"
        case "commented": "reviewed"
        default: "commented"
        }
    }

    @ViewBuilder private var menu: some View {
        if let item {
            if item.isOpen && !item.isPull {
                Button("Close as completed", systemImage: "checkmark.circle") { Task { await setState("closed", reason: "completed") } }
                    .accessibilityIdentifier("github-close")
                Button("Close as not planned", systemImage: "slash.circle") { Task { await setState("closed", reason: "not_planned") } }
            } else if item.isOpen {
                Button("Close pull request", systemImage: "xmark.circle") { Task { await setState("closed") } }
                    .accessibilityIdentifier("github-close")
            } else if item.state != "merged" {
                Button(item.isPull ? "Reopen pull request" : "Reopen issue", systemImage: "arrow.uturn.backward.circle") {
                    Task { await setState("open") }
                }
                .accessibilityIdentifier("github-reopen")
            }
            Button("Ask an agent", systemImage: "bubble.left.and.bubble.right") { asking = true }
                .disabled(store.bots.allSatisfy(\.archived))
            if let link = item.link {
                Button("Copy link", systemImage: "link") { Pasteboard.copy(link) }
            }
        }
    }

    private var mergeLabel: String {
        switch item?.pull?.mergeMethod {
        case "squash": "Squash and merge"
        case "rebase": "Rebase and merge"
        default: "Merge"
        }
    }

    private var mergeTitle: String {
        guard let item, let pull = item.pull else { return "Merge?" }
        return "Merge #\(item.number) into \(pull.baseRef)?"
    }

    // MARK: Actions

    private func load() async {
        do {
            let loaded = try await store.githubItem(route)
            guard !Task.isCancelled else { return }
            item = loaded; failure = nil
        } catch is CancellationError {
        } catch {
            if item == nil { failure = error.localizedDescription } else { actionError = error.localizedDescription }
        }
    }

    /// Posts a comment and shows it at the end. Nil is success; else the words of the failure.
    private func comment(_ text: String, key: String) async -> String? {
        do {
            let entry = try await store.commentOnGitHubItem(route, body: text, requestId: key)
            if item?.comments.contains(where: { $0.id == entry.id }) == false { item?.comments.append(entry) }
            actionError = nil
            return nil
        } catch is CancellationError {
            return "The comment was not confirmed. Send it again: it posts once."
        } catch {
            return error.localizedDescription
        }
    }

    private func setState(_ state: String, reason: String? = nil) async {
        busy = true
        defer { busy = false }
        do {
            item = try await store.setGitHubItemState(route, state: state, reason: reason)
            actionError = nil
        } catch is CancellationError {
        } catch { actionError = error.localizedDescription }
    }

    private func merge() async {
        guard let sha = item?.pull?.headSha else { return }
        busy = true
        defer { busy = false }
        do {
            item = try await store.mergeGitHubPull(route, sha: sha)
            actionError = nil
        } catch is CancellationError {
        } catch { actionError = error.localizedDescription }
    }
}

/// The state of an issue or a pull request in its color, as GitHub shows it.
struct GitHubStateChip: View {
    let item: GitHubItem

    var body: some View {
        HStack(spacing: BoardStyle.Space.xs) {
            Image(systemName: item.stateSymbol).imageScale(.small)
            Text(item.stateLabel)
        }
        .font(.caption.weight(.semibold)).foregroundStyle(item.stateTint)
        .padding(.horizontal, 7).padding(.vertical, 2)
        .background(item.stateTint.opacity(0.14), in: Capsule())
        .accessibilityIdentifier("github-state")
    }
}

/// The text of the issue or one comment: who wrote it and when, a review's verdict, then the Markdown. The person's own
/// comments have the wash of the person's side, as the answers of the support mailbox do.
struct GitHubEntryTile: View {
    let author: GitHubPerson?
    let verb: String
    let date: String?
    let text: String
    var own = false
    var review: String? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
            HStack(alignment: .firstTextBaseline, spacing: BoardStyle.Space.xs) {
                if let author { GitHubAvatar(person: author).alignmentGuide(.firstTextBaseline) { $0[.bottom] - 3 } }
                Text(author?.login ?? "Someone").font(.subheadline.weight(.semibold)).lineLimit(1)
                Text(verb).font(.subheadline).foregroundStyle(reviewTint).lineLimit(1)
                Spacer(minLength: BoardStyle.Space.s)
                if let age = GitHubPreview.age(of: date) {
                    Text(age).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                }
            }
            if !text.isEmpty { MessageText(text: text, linkColor: BoardStyle.control) }
        }
        .padding(BoardStyle.Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(own ? BoardStyle.accent.opacity(0.1) : BoardStyle.surface,
                    in: RoundedRectangle(cornerRadius: BoardStyle.Radius.small, style: .continuous))
    }

    private var reviewTint: Color {
        switch review {
        case "approved": BoardStyle.positive
        case "changes_requested": BoardStyle.destructive
        default: .secondary
        }
    }
}

/// The box of a pull request: its checks and review, its changes with the way to read them, its branches, and whether
/// it merges, with the Merge button when the account may merge.
struct GitHubPullSummary: View {
    let item: GitHubItem
    let route: GitHubItemRoute
    let busy: Bool
    let merge: () -> Void

    var body: some View {
        if let pull = item.pull {
            VStack(alignment: .leading, spacing: 0) {
                if let checks = pull.checks {
                    row(symbol: checks.state == "passing" ? "checkmark.circle.fill" : checks.state == "failing" ? "xmark.circle.fill" : "clock.fill",
                        tint: GitHubTint.checks(checks.state), text: checks.label)
                    Divider().padding(.leading, BoardStyle.Space.m)
                }
                if let review = pull.review, let label = GitHubPreview.reviewLabel(review) {
                    row(symbol: review == "approved" ? "checkmark.seal" : review == "changes_requested" ? "arrow.uturn.backward.circle" : "eye",
                        tint: GitHubTint.review(review), text: label)
                    Divider().padding(.leading, BoardStyle.Space.m)
                }
                NavigationLink { GitHubFilesView(route: route, title: item.title) } label: {
                    HStack(spacing: BoardStyle.Space.m) {
                        Image(systemName: "plusminus").font(.callout).foregroundStyle(.secondary).frame(width: 20)
                        HStack(spacing: BoardStyle.Space.s) {
                            if let additions = pull.additions { Text("+\(additions)").foregroundStyle(BoardStyle.positive) }
                            if let deletions = pull.deletions { Text("−\(deletions)").foregroundStyle(BoardStyle.destructive) }
                            Text(changes(pull)).foregroundStyle(.secondary)
                        }
                        .font(.subheadline.monospacedDigit()).lineLimit(1)
                        Spacer(minLength: BoardStyle.Space.s)
                        Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
                    }
                    .padding(BoardStyle.Space.m).contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("github-files")
                Divider().padding(.leading, BoardStyle.Space.m)
                row(symbol: "arrow.triangle.branch", tint: .secondary, text: "\(pull.headRef) → \(pull.baseRef)", mono: true)
                if item.state == "merged" {
                    Divider().padding(.leading, BoardStyle.Space.m)
                    row(symbol: "arrow.triangle.merge", tint: GitHubTint.merged,
                        text: ["Merged", pull.mergedBy.map { "by \($0)" }, GitHubPreview.age(of: pull.mergedAt)].compactMap { $0 }.joined(separator: " "))
                } else if let summary = item.mergeSummary {
                    Divider().padding(.leading, BoardStyle.Space.m)
                    HStack(spacing: BoardStyle.Space.m) {
                        Image(systemName: summary.ready ? "checkmark.circle" : "exclamationmark.circle").font(.callout)
                            .foregroundStyle(summary.ready ? BoardStyle.positive : BoardStyle.attention).frame(width: 20)
                        Text(summary.text).font(.subheadline).fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: BoardStyle.Space.s)
                        if pull.mergeMethod != nil, pull.headSha != nil, !pull.draft {
                            Button(busy ? "Merging…" : "Merge", action: merge)
                                .buttonStyle(CoconutButtonStyle())
                                .disabled(busy || !summary.ready)
                                .accessibilityIdentifier("github-merge")
                        }
                    }
                    .padding(BoardStyle.Space.m)
                }
            }
            .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.small, style: .continuous))
        }
    }

    private func changes(_ pull: GitHubItem.Pull) -> String {
        [pull.changedFiles.map { $0 == 1 ? "1 file" : "\($0) files" }, pull.commits.map { $0 == 1 ? "1 commit" : "\($0) commits" }]
            .compactMap { $0 }.joined(separator: " · ")
    }

    private func row(symbol: String, tint: Color, text: String, mono: Bool = false) -> some View {
        HStack(spacing: BoardStyle.Space.m) {
            Image(systemName: symbol).font(.callout).foregroundStyle(tint).frame(width: 20)
            Text(text).font(mono ? .subheadline.monospaced() : .subheadline).lineLimit(2).truncationMode(.middle)
            Spacer(minLength: 0)
        }
        .padding(BoardStyle.Space.m)
        .accessibilityElement(children: .combine)
    }
}

/// The changed files of a pull request, each with its patch: added lines green, removed lines red, the hunk headers
/// quiet. A long line scrolls sideways, as code does.
struct GitHubFilesView: View {
    @Environment(AppStore.self) private var store
    let route: GitHubItemRoute
    let title: String
    @State private var files: [GitHubChangedFile]?
    @State private var failure: String?
    @State private var shown: Set<String> = []

    var body: some View {
        BoardPage(spacing: BoardStyle.Space.m) {
            if let files {
                ForEach(files) { file in fileTile(file) }
                if files.isEmpty { Text("No changed files.").font(.subheadline).foregroundStyle(.secondary) }
            } else if let failure {
                BoardLoadNotice(title: "GitHub unavailable", message: failure) { Task { await load() } }
            } else {
                BoardLoading(title: "Loading changes")
            }
        }
        .navigationTitle(files.map { $0.count == 1 ? "1 file" : "\($0.count) files" } ?? "Files")
        .inlineNavigationTitle()
        .task { await load() }
    }

    private func load() async {
        do {
            let loaded = try await store.githubItemFiles(route)
            files = loaded
            // The first files show their patch at once. The rest open with a tap, so a large pull request stays quick.
            shown = Set(loaded.prefix(3).map(\.id))
            failure = nil
        } catch is CancellationError {
        } catch { failure = error.localizedDescription }
    }

    private func fileTile(_ file: GitHubChangedFile) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                if shown.contains(file.id) { shown.remove(file.id) } else { shown.insert(file.id) }
            } label: {
                HStack(spacing: BoardStyle.Space.s) {
                    Image(systemName: shown.contains(file.id) ? "chevron.down" : "chevron.right")
                        .font(.caption.weight(.semibold)).foregroundStyle(.tertiary).frame(width: 14)
                    Text(file.path).font(.footnote.monospaced().weight(.medium)).lineLimit(2).truncationMode(.head)
                    Spacer(minLength: BoardStyle.Space.s)
                    Text("+\(file.additions)").foregroundStyle(BoardStyle.positive)
                    Text("−\(file.deletions)").foregroundStyle(BoardStyle.destructive)
                }
                .font(.caption.monospacedDigit())
                .padding(BoardStyle.Space.m).contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("\(file.path), \(file.status), \(file.additions) added, \(file.deletions) removed")
            if shown.contains(file.id) {
                Divider()
                if let patch = file.patch {
                    GitHubPatchLines(patch: patch)
                } else {
                    Text(file.tooLarge ? "This change is too large to show here." : file.status == "renamed" ? "Renamed from \(file.previousPath ?? "another path")." : "No text change to show.")
                        .font(.footnote).foregroundStyle(.secondary).padding(BoardStyle.Space.m)
                }
            }
        }
        .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.small, style: .continuous))
        .clipShape(RoundedRectangle(cornerRadius: BoardStyle.Radius.small, style: .continuous))
    }
}

/// The lines of a unified patch in a monospaced font, each on the wash of its kind.
struct GitHubPatchLines: View {
    let patch: String

    var body: some View {
        ScrollView(.horizontal) {
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(patch.split(separator: "\n", omittingEmptySubsequences: false).enumerated()), id: \.offset) { _, line in
                    Text(line.isEmpty ? " " : String(line))
                        .font(.caption.monospaced())
                        .foregroundStyle(line.hasPrefix("@@") ? Color.secondary : Color.primary)
                        .lineLimit(1).fixedSize()
                        .padding(.horizontal, BoardStyle.Space.m).padding(.vertical, 1)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(wash(line))
                }
            }
            .padding(.vertical, BoardStyle.Space.xs)
        }
        .textSelection(.enabled)
    }

    private func wash(_ line: Substring) -> Color {
        if line.hasPrefix("+") { return BoardStyle.positive.opacity(0.14) }
        if line.hasPrefix("-") { return BoardStyle.destructive.opacity(0.12) }
        if line.hasPrefix("@@") { return BoardStyle.line.opacity(0.5) }
        return .clear
    }
}

/// The box under an issue, a pull request or a support conversation: one field and Send, as the box of a chat. Send
/// first gives the field all that the keyboard holds (KeyboardSettle), and a text that comes back from dictation after
/// the send clears again (SentEcho). A failed send puts the text back with the reason above it. One text keeps one
/// request key, so a Send again after a lost answer sends nothing twice. The draft stays when the page closes.
struct ReplyBox: View {
    let draftKey: String
    let placeholder: String
    /// A line above the field, such as the address that an answer goes to.
    var note: String? = nil
    var identifier = "reply-box"
    /// Sends the text with its request key. Nil is success; else the words of the failure.
    let send: @MainActor (String, String) async -> String?
    @State private var text = ""
    @State private var sending = false
    @State private var failure: String?
    @State private var echo = SentEcho()
    @State private var attempt: (key: String, text: String)?
    @FocusState private var focused: Bool

    private var trimmed: String { text.trimmingCharacters(in: .whitespacesAndNewlines) }

    var body: some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
            if let failure {
                Text(failure).font(.caption).foregroundStyle(BoardStyle.destructive).fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, BoardStyle.Space.s)
            } else if let note {
                Text(note).font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                    .padding(.horizontal, BoardStyle.Space.s)
            }
            HStack(alignment: .bottom, spacing: 0) {
                TextField(placeholder, text: $text, axis: .vertical)
                    .focused($focused)
                    .modifier(ComposerFieldStyle(send: submit))
                    .padding(.horizontal, BoardStyle.Space.m)
                    .accessibilityIdentifier(identifier + "-input")
                if sending {
                    ProgressView().frame(minWidth: 44, minHeight: 44)
                } else {
                    ComposerSendButton(symbol: "arrow.up", enabled: !trimmed.isEmpty, label: "Send", action: submit,
                                       identifier: identifier + "-send")
                }
            }
            .padding(.trailing, BoardStyle.Space.xs)
            .modifier(ComposerSurface(shape: RoundedRectangle(cornerRadius: BoardStyle.Radius.large, style: .continuous)))
            .modifier(ComposerGlassGroup())
        }
        .padding(.horizontal, BoardStyle.Space.l)
        .padding(.top, BoardStyle.Space.xs)
        .padding(.bottom, BoardStyle.Space.s)
        .modifier(ComposerBackdrop())
        .onAppear { if text.isEmpty { text = ComposerDrafts.read(draftKey) } }
        .onChange(of: text) { old, new in
            if echo.takesBack(old: old, new: new) { text = ""; return }
            if !sending { ComposerDrafts.write(new, chat: draftKey) }
            if failure != nil && !new.isEmpty && new != old { failure = nil }
        }
    }

    private func submit() {
        KeyboardSettle.run(if: focused) {
            let draft = trimmed
            guard !draft.isEmpty, !sending else { return }
            let key = attempt?.text == draft ? attempt?.key ?? UUID().uuidString : UUID().uuidString
            attempt = (key, draft)
            sending = true
            failure = nil
            echo.sent(text)
            text = ""
            Task {
                let problem = await send(draft, key)
                sending = false
                if let problem {
                    echo.cancel()
                    failure = problem
                    if text.isEmpty { text = draft }
                } else {
                    attempt = nil
                    ComposerDrafts.write(text, chat: draftKey)
                }
            }
        }
    }
}

/// Hands an issue, a pull request or a conversation to an agent: the agent, a line of instructions, Send. The message
/// goes to the agent's chat with the link, and that chat opens.
struct AskAgentSheet: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openChat) private var openChat
    @Environment(\.pushRoute) private var pushRoute
    /// The first line of the message: "Work on antiwork/gumroad-private#4563: <title>".
    let summary: String
    /// Where the work is, for the agent: its address, or the ID of its thread.
    let link: String
    /// The last line: where the agent keeps the work.
    let keep: String
    @State private var selected = ""
    @State private var instruction = ""
    @State private var busy = false

    var body: some View {
        NavigationStack {
            Form {
                Picker("Agent", selection: $selected) {
                    Text("Choose an agent").tag("")
                    ForEach(store.bots.withoutChats.filter { !$0.archived }) { Text($0.name).tag($0.id) }
                }
                TextField("Instructions (optional)", text: $instruction, axis: .vertical).lineLimit(3...6)
            }
            .navigationTitle("Ask an agent").inlineNavigationTitle().settingsBackground()
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button(busy ? "Sending…" : "Send") { send() }.disabled(selected.isEmpty || busy)
                }
            }
        }
        .frame(minWidth: 360, minHeight: 300)
    }

    private func send() {
        guard let bot = store.bots.first(where: { $0.id == selected }) else { return }
        busy = true
        Task {
            defer { busy = false }
            let request = instruction.trimmingCharacters(in: .whitespacesAndNewlines)
            let text = [summary, link, request, keep].filter { !$0.isEmpty }.joined(separator: "\n\n")
            if await store.send(bot: bot, text: text, mode: "queue") {
                dismiss()
                if let openChat { openChat(bot.id) } else { pushRoute?(ConversationRoute(botId: bot.id)) }
            }
        }
    }
}
