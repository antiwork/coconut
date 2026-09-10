import SwiftUI
import QuickLook

private enum DecisionSheet: Identifiable {
    case context, modify, details, option(String)
    var id: String {
        switch self {
        case .context: "context"
        case .modify: "modify"
        case .details: "details"
        case .option(let id): "option-" + id
        }
    }
}

struct DecisionCard: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    let decision: Decision
    @State private var sheet: DecisionSheet?
    @State private var submitting = false
    @State private var submittingOptionId: String?
    @State private var responseError: String?
    private var canRespond: Bool { store.connection == .connected || store.isDemo }
    private var recommendation: String? {
        guard let index = decision.options.firstIndex(where: { $0.id == decision.recommendedOptionId }) else { return nil }
        return "Recommended: " + optionLetter(index)
    }
    private var recommendationText: String {
        recommendation.map { $0 + ". " + decision.explanation } ?? decision.explanation
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 4) {
                VStack(alignment: .leading, spacing: 5) {
                    Text(decision.title).font(.body.weight(.medium))
                        .fixedSize(horizontal: false, vertical: true)
                    if decision.isPending {
                        Text(recommendationText).font(.subheadline).foregroundStyle(.secondary)
                            .lineLimit(3).textSelection(.enabled)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                cardMenu
            }
            if decision.isPending {
                if !decision.options.isEmpty { choices }
                if !canRespond {
                    Text("Reconnect to respond.").font(.caption).foregroundStyle(.secondary)
                }
                if let responseError {
                    Text(responseError).font(.caption).foregroundStyle(BoardStyle.accent)
                        .textSelection(.enabled)
                }
            } else {
                decisionReceipt
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: 22))
        .sheet(item: $sheet) { destination in
            switch destination {
            case .context: DecisionInputSheet(decision: decision, mode: "context")
            case .modify: DecisionInputSheet(decision: decision, mode: "modify")
            case .details: DecisionDetailsSheet(decision: decision)
            case .option(let id): DecisionDetailsSheet(decision: decision, optionId: id)
            }
        }
    }

    private var choices: some View {
        VStack(spacing: 0) {
            ForEach(Array(decision.options.enumerated()), id: \.element.id) { index, option in
                Button { answer(option.answer, optionId: option.id) } label: {
                    HStack(alignment: .firstTextBaseline, spacing: 10) {
                        Text(optionLetter(index)).font(.caption.monospaced())
                            .foregroundStyle(.secondary)
                            .frame(minWidth: 23, minHeight: 25)
                            .background(BoardStyle.ink.opacity(0.07), in: RoundedRectangle(cornerRadius: 5))
                            .accessibilityHidden(true)
                        Text(option.label).font(.subheadline).foregroundStyle(.primary)
                            .fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: 0)
                        if submittingOptionId == option.id {
                            ProgressView().controlSize(.small).accessibilityLabel("Sending choice")
                        }
                    }
                    .padding(.horizontal, 12).padding(.vertical, 9)
                    .frame(maxWidth: .infinity, minHeight: 44)
                    .contentShape(Rectangle())
                }
                .buttonStyle(HomePressStyle())
                .disabled(!canRespond || submitting)
                .accessibilityIdentifier("decision-option-\(option.id)")
                .accessibilityLabel("\(optionLetter(index)). \(option.label)")
                .accessibilityHint("Submits this choice. " + option.explanation)
                .contextMenu {
                    Button("Review option", systemImage: "doc.text.magnifyingglass") { sheet = .option(option.id) }
                }
                if option.id != decision.options.last?.id { Divider().padding(.leading, 12) }
            }
        }
        .background(BoardStyle.paper, in: RoundedRectangle(cornerRadius: 12))
        .clipShape(RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(BoardStyle.line))
    }

    private var cardMenu: some View {
        Menu {
            Button("Details", systemImage: "doc.text.magnifyingglass") { sheet = .details }
                .accessibilityIdentifier("decision-details")
            if decision.isPending {
                Button("Ask a question", systemImage: "bubble.left") { sheet = .context }
                    .disabled(!canRespond || submitting)
                    .accessibilityIdentifier("ask-decision-context")
                Button("Modify", systemImage: "pencil") { sheet = .modify }
                    .disabled(!canRespond || submitting)
                    .accessibilityIdentifier("modify-decision")
                Button("Reject", systemImage: "xmark", role: .destructive) { answer("reject") }
                    .disabled(!canRespond || submitting)
                    .accessibilityIdentifier("reject-decision")
                if decision.options.isEmpty {
                    Button("Approve", systemImage: "checkmark") { answer("approve") }
                        .disabled(!canRespond || submitting)
                        .accessibilityIdentifier("approve-decision")
                }
            }
        } label: {
            Image(systemName: "ellipsis").font(.system(size: 15))
                .foregroundStyle(.secondary).frame(width: 44, height: 44)
        }
        .disabled(submitting)
        .accessibilityLabel("Decision options")
        .accessibilityIdentifier("decision-menu-\(decision.id)")
    }

    private var decisionReceipt: some View {
        VStack(alignment: .leading, spacing: 6) {
            if dynamicTypeSize.isAccessibilitySize {
                VStack(alignment: .leading, spacing: 3) {
                    StatusLabel(status: decision.status)
                    Text(store.decisionActor(decision)).font(.caption).foregroundStyle(.secondary)
                }
            } else {
                HStack {
                    StatusLabel(status: decision.status)
                    Spacer(minLength: 8)
                    Text(store.decisionActor(decision)).font(.caption).foregroundStyle(.secondary)
                }
            }
            if let selected = decision.selectedOptionLabel {
                Text("Selected: \(selected)").font(.caption).foregroundStyle(.secondary)
            }
            if let correction = decision.correction, !correction.isEmpty {
                Text("Changed: \(correction)").font(.caption).foregroundStyle(.secondary)
            }
            if let outcome = decision.outcome, !outcome.isEmpty {
                Text(outcome).font(.subheadline)
            } else {
                Text(decision.status == "approved" ? "Approval recorded. The Mac will recheck the action before it runs." : "The Mac keeps this decision in the task history.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if let replacement = decision.replacedByDecisionId {
                NavigationLink(value: TaskThreadRoute(taskId: decision.taskId, decisionId: replacement)) {
                    Label("View revised proposal", systemImage: "arrow.turn.down.right")
                        .font(.caption.weight(.medium)).frame(minHeight: 44)
                }
            }
        }
    }

    private func answer(_ choice: String, optionId: String? = nil) {
        guard decision.isPending, canRespond, !submitting else { return }
        submitting = true
        submittingOptionId = optionId
        responseError = nil
        store.error = nil
        Task {
            await store.answer(decision, choice: choice, optionId: optionId)
            responseError = store.error
            submitting = false
            submittingOptionId = nil
        }
    }

    private func optionLetter(_ index: Int) -> String {
        let letters = Array("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        return index < letters.count ? String(letters[index]) : String(index + 1)
    }
}

private struct DecisionDetailsSheet: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let decision: Decision
    var optionId: String? = nil
    private var current: Decision { store.decisions.first { $0.id == decision.id } ?? decision }
    private var options: [DecisionOption] {
        optionId.map { id in current.options.filter { $0.id == id } } ?? current.options
    }

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Text(current.title).font(.headline)
                    Text(current.explanation).font(.subheadline).textSelection(.enabled)
                }
                if !current.resource.isEmpty {
                    Section("Affected resources") {
                        Text(current.resource).font(.subheadline).textSelection(.enabled)
                    }
                }
                if !current.consequences.isEmpty {
                    Section("Consequences") {
                        Text(current.consequences).font(.subheadline).textSelection(.enabled)
                    }
                }
                if current.options.isEmpty || current.options.allSatisfy({ $0.action == nil }) {
                    if let proposal = current.proposalPreview, !proposal.isEmpty {
                        Section("Proposed file") { proposalText(proposal) }
                    }
                }
                ForEach(options) { option in
                    Section(option.label) {
                        Text(option.explanation).font(.subheadline).textSelection(.enabled)
                        if let action = option.action {
                            Text(action.filename).font(.subheadline.monospaced()).textSelection(.enabled)
                            proposalText(action.content)
                        }
                        if let modification = option.modification, !modification.isEmpty {
                            Text(modification).font(.subheadline).textSelection(.enabled)
                        }
                    }
                }
                Section("Record") {
                    StatusLabel(status: current.status)
                    if let selected = current.selectedOptionLabel { LabeledContent("Selected", value: selected) }
                    if current.decidedBy != nil { LabeledContent("Decided by", value: store.decisionActor(current)) }
                    if let correction = current.correction, !correction.isEmpty { Text("Changed: \(correction)") }
                    if let outcome = current.outcome, !outcome.isEmpty { Text(outcome) }
                    if let date = current.createdAt.apiDate {
                        LabeledContent("Created", value: date.formatted(date: .abbreviated, time: .shortened))
                    }
                    Text("Action: \(current.actionType) · Revision \(current.revision)")
                        .font(.caption.monospaced()).foregroundStyle(.secondary)
                    if let actor = current.decidedBy {
                        Text("Recorded by: \(actor)").font(.caption.monospaced()).textSelection(.enabled)
                    }
                }
            }
            .scrollContentBackground(.hidden).background(BoardStyle.paper)
            .navigationTitle(optionId == nil ? "Decision details" : "Option details")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }.accessibilityIdentifier("close-decision-details")
                }
            }
        }
    }

    private func proposalText(_ value: String) -> some View {
        ScrollView(.horizontal) {
            Text(value).font(.caption.monospaced()).textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

struct DecisionInputSheet: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let decision: Decision
    let mode: String
    @State private var text = ""
    @State private var sending = false
    @FocusState private var focused: Bool
    private var isContext: Bool { mode == "context" }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField(isContext ? "What do you need to know?" : "Describe the change…", text: $text, axis: .vertical)
                        .lineLimit(3...10).focused($focused)
                        .accessibilityIdentifier("decision-context-input")
                } header: {
                    Text(isContext ? "Your question" : "What should change?")
                } footer: {
                    Text(isContext ? "This decision stays pending." : "The bot prepares a new proposal. This change does not approve it.")
                }
                Section("Decision") {
                    Text(decision.title).font(.subheadline)
                }
                if let error = store.error {
                    Section { Text(error).font(.caption).foregroundStyle(BoardStyle.accent) }
                }
            }
            .scrollContentBackground(.hidden).background(BoardStyle.paper)
            .navigationTitle(isContext ? "Ask for context" : "Modify proposal")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button(isContext ? "Ask" : "Send") {
                        sending = true
                        store.error = nil
                        Task {
                            if isContext { await store.requestContext(decision, question: text) }
                            else { await store.answer(decision, choice: "modify", correction: text) }
                            sending = false
                            if store.error == nil { dismiss() }
                        }
                    }
                    .disabled(sending || text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .accessibilityIdentifier("submit-decision-context")
                }
            }
            .onAppear { focused = true }
        }
        .presentationDetents([.large])
    }
}

struct ArtifactCard: View {
    @Environment(AppStore.self) private var store
    let artifact: Artifact
    @State private var previewURL: URL?
    @State private var downloading = false
    @State private var expanded = false
    private var shortResult: Bool {
        artifact.preview.count <= 160 && artifact.preview.components(separatedBy: "\n").count <= 3
    }

    private var externalURL: URL? {
        guard let raw = artifact.url, let url = URL(string: raw), let scheme = url.scheme,
              ["https", "http"].contains(scheme) else { return nil }
        return url
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: artifact.kind == "pr" ? "arrow.triangle.pull" : artifact.kind == "task" ? "checkmark.circle" : "doc.text")
                    .font(.title3).foregroundStyle(.secondary)
                    .frame(width: 30, height: 36)
                VStack(alignment: .leading, spacing: 6) {
                    Text(artifact.title).font(.subheadline.weight(.semibold))
                    StatusLabel(status: artifact.status)
                }
                Spacer(minLength: 0)
            }
            if !artifact.preview.isEmpty {
                Text(.init(artifact.preview)).font(.subheadline).foregroundStyle(.secondary)
                    .lineLimit(expanded || (artifact.kind == "task" && shortResult) ? nil : 3).textSelection(.enabled)
            }
            if artifact.kind == "task" && !shortResult {
                Button(expanded ? "Show less" : "Show full result") { expanded.toggle() }
                    .font(.subheadline).frame(minHeight: 44)
            }
            if artifact.kind == "pr" {
                HStack(spacing: 5) {
                    if let error = artifact.refreshError, !error.isEmpty {
                        Image(systemName: "exclamationmark.circle")
                        Text("Status could not refresh")
                    } else if let checked = artifact.checkedAt?.apiDate {
                        Text("Checked")
                        Text(checked, style: .relative)
                    }
                }.font(.caption2).foregroundStyle(.secondary)
            }
            HStack {
                if let externalURL {
                    Link(destination: externalURL) {
                        Label(artifact.kind == "pr" ? "Open pull request" : "Open result", systemImage: "arrow.up.right")
                            .font(.subheadline.weight(.medium)).frame(minHeight: 44)
                    }
                    Spacer()
                    ShareLink(item: externalURL) { Image(systemName: "square.and.arrow.up").frame(width: 44, height: 44) }
                } else if artifact.kind != "task" {
                    Button {
                        downloading = true
                        Task {
                            previewURL = await store.downloadArtifact(artifact)
                            downloading = false
                        }
                    } label: {
                        Label(downloading ? "Opening…" : "Preview file", systemImage: "doc.text.magnifyingglass")
                            .font(.subheadline.weight(.medium)).frame(minHeight: 44)
                    }
                    .disabled(downloading)
                }
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: 20))
        .quickLookPreview($previewURL)
    }
}
