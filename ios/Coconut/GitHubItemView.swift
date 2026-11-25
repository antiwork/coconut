import SwiftUI

/// An issue or a pull request in Coconut (GitHubItem): the state and the title, the people and the labels, for a pull
/// request its checks, review, changes and merge, then the text and the comments, with a box under them that comments
/// or starts an agent on the item (ReplyBox). The menu closes and reopens it and copies the link. Nothing here opens
/// github.com: the person reads and answers the work of the agents where the rest of the work is.
struct GitHubItemView: View {
    @Environment(AppStore.self) private var store
    let route: GitHubItemRoute
    @State private var item: GitHubItem?
    @State private var failure: String?
    @State private var busy = false
    @State private var actionError: String?
    @State private var confirmingMerge = false
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
            if let item {
                ReplyBox(draftKey: "github:\(route.repository)#\(route.number)", placeholder: "Comment",
                         identifier: "github-comment", handoff: AgentHandoff(item)) { text, key in await comment(text, key: key) }
            }
        }
        // A number of GitHub is a name, so it shows without the separator of an amount: #4731, not #4,731.
        .navigationTitle(Text(verbatim: "#\(route.number)"))
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
    }

    // MARK: Parts

    private func header(_ item: GitHubItem) -> some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
            Text(verbatim: "\(item.repository) #\(item.number)").font(.footnote.monospacedDigit()).foregroundStyle(.secondary)
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

/// The lines of a unified patch in a monospaced font, each on the wash of its kind. A long line scrolls sideways, as
/// code does: a wrapped line of code broke words with hyphens.
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
                        // A plain color took rounded corners here, so each line drew as a pill.
                        .background { Rectangle().fill(wash(line)) }
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

/// The box under an issue, a pull request, a support conversation or a risk review: one field and Send, as the box of
/// a chat. Send first gives the field all that the keyboard holds (KeyboardSettle), and a text that comes back from
/// dictation after the send clears again (SentEcho). A failed send puts the text back with the reason above it. One
/// text keeps one request key, so a Send again after a lost answer sends nothing twice. The draft stays when the page
/// closes.
///
/// Under an issue, a pull request and a support conversation the box is the message box too (the person: "comment box
/// can merge w this new agent chat box. Then can do all work from coco"), so a comment, an answer and the work of an
/// agent start in one place. A chip under the field names where the text goes: the box's own reply first, then the
/// projects and the team agents of the message box (NewChatBar). To a project the text starts a new chat with the item
/// on top (AgentHandoff), and to a team agent such as Gumclaw it goes to the direct chat. As a Send of the message box,
/// it opens no chat: the chat shows in Chats, and the empty box says for a moment where the text went. With an agent
/// chosen, the empty box shows the item as its suggestion, and Send hands the item over as it is. The box starts on its
/// own reply, and a draft keeps where it goes, so a text written for an agent never comes back as a comment on GitHub
/// or an answer to a customer.
struct ReplyBox: View {
    @Environment(AppStore.self) private var store
    let draftKey: String
    /// The word of the box's own reply: its placeholder, and the first row of its chip.
    let placeholder: String
    /// A line above the field, such as the address that an answer goes to. It shows while the box replies.
    var note: String? = nil
    var identifier = "reply-box"
    /// The item that an agent gets with the text. Nil keeps the box a reply only, as under a risk review.
    var handoff: AgentHandoff? = nil
    /// The page sends its own reply. Without it the box sends only to agents, as under a support conversation while the
    /// support mailbox is not connected.
    var canReply = true
    /// Sends the text with its request key. Nil is success; else the words of the failure.
    let send: @MainActor (String, String) async -> String?
    @State private var text = ""
    @State private var sending = false
    @State private var failure: String?
    @State private var echo = SentEcho()
    @State private var attempt: (key: String, text: String)?
    /// Where the text goes, as ReplyDestination.key: empty for the box's own reply.
    @State private var chosen = ""
    /// Where the last text went, which the empty box says for a moment.
    @State private var handed: Handed?
    /// The agent of the business as the phone last saw it (NewChatBar), until the list of the teams is here.
    @AppStorage(ChatProject.businessAgentKey) private var businessAgent = ""
    @FocusState private var focused: Bool

    private struct Handed: Equatable {
        let id = UUID()
        let name: String
    }

    private var trimmed: String { text.trimmingCharacters(in: .whitespacesAndNewlines) }
    /// Where the draft goes, kept beside the draft.
    private var chosenKey: String { draftKey + "/to" }

    var body: some View {
        let agents = handoff == nil ? nil : agentList()
        let target = destination(in: agents)
        VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
            // The line floats over the page as it scrolls under the box, so it sits in a capsule of its own.
            if let failure {
                Text(failure).font(.caption).foregroundStyle(BoardStyle.destructive).fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, BoardStyle.Space.s).padding(.vertical, 3)
                    .modifier(FloatingCapsule())
            } else if let note, target == .reply {
                Text(note).font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                    .padding(.horizontal, BoardStyle.Space.s).padding(.vertical, 3)
                    .modifier(FloatingCapsule())
            }
            if let handoff, let agents {
                stacked(target, handoff: handoff, agents: agents)
            } else {
                row
            }
        }
        .padding(.horizontal, BoardStyle.Space.l)
        .padding(.top, BoardStyle.Space.xs)
        .padding(.bottom, BoardStyle.Space.s)
        .modifier(ComposerBackdrop())
        .onAppear {
            guard text.isEmpty else { return }
            text = ComposerDrafts.read(draftKey)
            // A draft comes back with where it goes. Without one the box starts on its own reply.
            chosen = text.isEmpty ? "" : ComposerDrafts.read(chosenKey)
        }
        .onChange(of: text) { old, new in
            if echo.takesBack(old: old, new: new) { text = ""; return }
            if !sending { keepDraft(new) }
            if failure != nil && !new.isEmpty && new != old { failure = nil }
        }
        .onChange(of: chosen) { _, _ in if !sending { keepDraft(text) } }
        .task(id: handed?.id) {
            guard handed != nil else { return }
            try? await Task.sleep(for: .seconds(3))
            guard !Task.isCancelled else { return }
            handed = nil
        }
    }

    /// The field and Send on one line: the box of a page that only replies.
    private var row: some View {
        HStack(alignment: .bottom, spacing: 0) {
            TextField(placeholder, text: $text, axis: .vertical)
                .focused($focused)
                .modifier(ComposerFieldStyle(send: submit))
                .padding(.horizontal, BoardStyle.Space.m)
                .accessibilityIdentifier(identifier + "-input")
            sendButton(enabled: canReply && !trimmed.isEmpty)
        }
        .padding(.trailing, BoardStyle.Space.xs)
        .modifier(ComposerSurface(shape: RoundedRectangle(cornerRadius: BoardStyle.Radius.large, style: .continuous)))
        .modifier(ComposerGlassGroup())
    }

    /// The text on top, and under it the chip that names where it goes and Send, as the message box (NewChatBar).
    private func stacked(_ target: ReplyDestination?, handoff: AgentHandoff, agents: ReplyAgents) -> some View {
        let shape = RoundedRectangle(cornerRadius: BoardStyle.Radius.large, style: .continuous)
        let enabled: Bool = switch target {
        case .reply?: !trimmed.isEmpty
        // While the box says where the last text went, an empty box sends nothing.
        case .agent?: !trimmed.isEmpty || handed == nil
        case nil: false
        }
        return VStack(alignment: .leading, spacing: 0) {
            TextField(prompt(target, handoff: handoff), text: $text, axis: .vertical)
                .focused($focused)
                .modifier(ComposerFieldStyle(send: submit, stacked: true))
                .padding(.horizontal, BoardStyle.Space.m)
                .disabled(target == nil)
                .accessibilityIdentifier(identifier + "-input")
            HStack(spacing: 0) {
                chip(target, agents: agents)
                Spacer(minLength: 0)
                sendButton(enabled: enabled)
            }
            .padding(.horizontal, BoardStyle.Space.xs)
        }
        .modifier(ComposerClickArea(shape: shape) { focused = true })
        .modifier(ComposerSurface(shape: shape))
        .modifier(ComposerGlassGroup())
    }

    @ViewBuilder private func sendButton(enabled: Bool) -> some View {
        if sending {
            ProgressView().frame(minWidth: 44, minHeight: 44)
        } else {
            ComposerSendButton(symbol: "arrow.up", enabled: enabled, label: "Send", action: submit,
                               identifier: identifier + "-send")
        }
    }

    /// The gray text of the empty box: where the last text went, for a moment; else the item that an agent gets; else
    /// the box's own reply.
    private func prompt(_ target: ReplyDestination?, handoff: AgentHandoff) -> String {
        if let handed { return "Sent to " + handed.name }
        if case .agent? = target { return handoff.summary }
        return placeholder
    }

    /// Where the text goes, as a chip that opens the box's own reply, the projects and the team agents. Before the Mac
    /// sends its projects, a page that cannot reply says so.
    @ViewBuilder
    private func chip(_ target: ReplyDestination?, agents: ReplyAgents) -> some View {
        if let target {
            let choice = Binding(get: { target.key }, set: { chosen = $0 })
            let name = target.name(reply: placeholder)
            Menu {
                if canReply {
                    Picker(placeholder, selection: choice) { Text(placeholder).tag("") }
                        .pickerStyle(.inline)
                }
                Picker("New agent", selection: choice) {
                    ForEach(agents.projects) { project in Text(project.name).tag(project.id) }
                }
                .pickerStyle(.inline)
                if !agents.agents.isEmpty {
                    Picker("Team agents", selection: choice) {
                        ForEach(agents.agents) { agent in Text(agent.name).tag(agent.memory) }
                    }
                    .pickerStyle(.inline)
                }
            } label: {
                ComposerChipLabel(title: name)
            }
            .modifier(ComposerChipMenuStyle())
            // The first item stays first, also when the menu opens above the chip.
            .menuOrder(.fixed)
            .accessibilityLabel("Send to, " + name)
            .accessibilityIdentifier(identifier + "-to")
        } else {
            Text("No project yet")
                .font(.subheadline.weight(.medium))
                .foregroundStyle(.secondary)
                .padding(.horizontal, BoardStyle.Space.s)
                .frame(minHeight: 44)
                .accessibilityIdentifier(identifier + "-to")
        }
    }

    /// Where a Send goes now. A box without an item only replies.
    private func destination(in agents: ReplyAgents?) -> ReplyDestination? {
        guard let agents else { return canReply ? .reply : nil }
        return ReplyDestination.resolve(chosen, canReply: canReply, in: agents)
    }

    /// The projects and the team agents of the message box (NewChatBar), the project with the newest chat first.
    private func agentList() -> ReplyAgents {
        let latest = AgentChats.activity(bots: store.bots, latest: store.chatSummaries.newestActivity)
        let agents = ChatProject.teamAgents(in: store.shownTeamRooms)
        // Until the list of the teams is here, the agent of the business is the one that the phone kept.
        let business = store.team == nil ? TeamAgentChoice(memory: businessAgent) : ChatProject.businessAgent(in: agents)
        return ReplyAgents(projects: ChatProject.choices(in: store.bots, latest: latest), agents: agents,
                           teamLoaded: store.team != nil, business: business)
    }

    /// Keeps the draft and where it goes. An empty draft keeps neither.
    private func keepDraft(_ draft: String) {
        ComposerDrafts.write(draft, chat: draftKey)
        ComposerDrafts.write(draft.isEmpty ? "" : chosen, chat: chosenKey)
    }

    private func submit() {
        let target = destination(in: handoff == nil ? nil : agentList())
        KeyboardSettle.run(if: focused) {
            switch target {
            case .reply?: reply()
            case .agent(let agent)?: handOff(to: agent)
            case nil: break
            }
        }
    }

    /// Sends the text as the box's own reply: a comment, an answer, a question in the issue of a risk review.
    private func reply() {
        let draft = trimmed
        guard canReply, !draft.isEmpty, !sending else { return }
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
                keepDraft(text)
            }
        }
    }

    /// Sends the item with the text to an agent: the first message of a new chat of a project, which stays closed
    /// (AppStore.sendNewChat), or a message of the direct chat with a team agent. An empty box sends the item alone.
    /// When the Mac does not take it, its notice says why and the text comes back.
    private func handOff(to agent: NewChatTarget) {
        guard let handoff, !sending else { return }
        let words = trimmed
        guard !words.isEmpty || handed == nil else { return }
        let message = handoff.message(words)
        failure = nil
        switch agent {
        case .project(let project):
            sending = true
            if !words.isEmpty {
                echo.sent(text)
                text = ""
            }
            Task {
                let chat = await store.sendNewChat(in: project, text: message)
                sending = false
                guard chat != nil else {
                    echo.cancel()
                    if !words.isEmpty, text.isEmpty { text = words }
                    return
                }
                keepDraft(text)
                handed = Handed(name: agent.name)
            }
        case .teamAgent(let team):
            // The store keeps the message and sends it. A message that does not go waits in the chat with Retry.
            guard store.sendTeamMessage(team.chat, text: message, replyingTo: nil) else { return }
            if !words.isEmpty {
                echo.sent(text)
                text = ""
            }
            handed = Handed(name: agent.name)
        }
    }
}

/// The item that an agent gets from the box under it (ReplyBox): a first line that names it as the suggestions of the
/// message box do ("Work on #4872: …"), which names the new chat too, and the address that opens it in Coconut
/// (InAppLink), then the person's words.
struct AgentHandoff: Equatable {
    let summary: String
    let link: String

    /// The message of the agent. An empty text hands the item over as it is.
    func message(_ words: String) -> String {
        [summary + "\n" + link, words].filter { !$0.isEmpty }.joined(separator: "\n\n")
    }
}

extension AgentHandoff {
    /// An issue or a pull request.
    init(_ item: GitHubItem) {
        // A number of GitHub is a name, so it has no separator of thousands.
        summary = "Work on #" + String(item.number) + ": " + item.title
        link = item.url ?? "https://github.com/" + item.repository + (item.isPull ? "/pull/" : "/issues/") + String(item.number)
    }

    /// A conversation of the support mailbox. The address of its Gmail thread opens it in Coconut, and an agent finds
    /// the thread by the ID in it.
    init(supportThread id: String, subject: String) {
        summary = "Work on the support conversation \"" + (subject.isEmpty ? "No subject" : subject) + "\""
        link = "https://mail.google.com/mail/u/0/#all/" + id
    }
}

/// The agents that the box under an item sends to, as the message box lists them (NewChatBar).
struct ReplyAgents {
    /// The projects, the one with the newest chat first, then the Coconut chat (ChatProject.choices).
    let projects: [Bot]
    /// The agents of the teams that the person can write to, such as Gumclaw (ChatProject.teamAgents).
    let agents: [TeamAgentChoice]
    /// The list of the teams is here, so a team agent that it lacks is gone.
    let teamLoaded: Bool
    /// The agent of the business (ChatProject.businessAgent).
    let business: TeamAgentChoice?
}

/// Where the text of the box under an item goes: the box's own reply (a comment, an answer) or an agent.
enum ReplyDestination: Equatable {
    case reply
    case agent(NewChatTarget)

    /// The value of the choice: empty for the reply, the ID of a project, or the memory of a team agent
    /// (TeamAgentChoice.memory), which names it also before the list of the teams is here.
    var key: String {
        switch self {
        case .reply: ""
        case .agent(.project(let bot)): bot.id
        case .agent(.teamAgent(let agent)): agent.memory
        }
    }

    func name(reply: String) -> String {
        switch self {
        case .reply: reply
        case .agent(let agent): agent.name
        }
    }

    /// The place of a choice. An empty choice is the reply while the page can send one. An agent that left the list
    /// gives way to the agent that the tabs of the business send to (NewChatPlace), and a page without its own reply
    /// starts there too: a choice of an agent never turns into the reply, so a text written for an agent does not go
    /// to a customer or to GitHub. Nil before the Mac sends its projects.
    static func resolve(_ chosen: String, canReply: Bool, in list: ReplyAgents) -> ReplyDestination? {
        if chosen.isEmpty, canReply { return .reply }
        if let project = list.projects.first(where: { $0.id == chosen }) { return .agent(.project(project)) }
        if let kept = TeamAgentChoice(memory: chosen) {
            if !list.teamLoaded { return .agent(.teamAgent(kept)) }
            if let agent = list.agents.first(where: { $0.id == kept.id }) { return .agent(.teamAgent(agent)) }
        }
        return NewChatPlace.support.target(chosen: "", projects: list.projects, agents: list.agents, business: list.business)
            .map(ReplyDestination.agent)
    }
}
