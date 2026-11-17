import SwiftUI

/// The notice of Hermes for a background process that ended, as the Mac reads it (backend process_notices.py).
/// The message keeps the text of Hermes, which an older app shows as a bubble.
struct ProcessNotice: Codable, Equatable, Sendable {
    var ok: Bool
    /// The command as Hermes shows it: one line of at most 80 characters.
    var command: String? = nil
    /// The command without a leading `cd <path> &&`, or the repository of that path.
    var label: String? = nil
    /// The repository or folder of a leading `cd`.
    var place: String? = nil
    /// The run time in the words of Hermes, such as "5m 26s".
    var duration: String? = nil
    var exitCode: Int? = nil
    /// The last lines of output of a failure.
    var output: String? = nil
}

/// One compact row for a background process that ended: a check or a cross, a short name, the run time, and the
/// command on a tap. A failure shows its exit code and the last lines of output at once.
struct ProcessNoticeRow: View {
    let message: Message
    let notice: ProcessNotice
    @State private var expanded = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var tint: Color { notice.ok ? BoardStyle.positive : BoardStyle.destructive }
    private var title: String { notice.label ?? notice.command ?? "Background task" }
    private var status: String { notice.ok ? "Finished" : "Failed" }

    var body: some View {
        VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
            Button {
                withAnimation(BoardStyle.fade(reduceMotion: reduceMotion)) { expanded.toggle() }
            } label: {
                HStack(spacing: BoardStyle.Space.s) {
                    Image(systemName: notice.ok ? "checkmark.circle.fill" : "xmark.circle.fill")
                        .font(.body).foregroundStyle(tint)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(title).font(.subheadline.weight(.medium)).foregroundStyle(BoardStyle.ink)
                            .lineLimit(1).truncationMode(.middle)
                        Text(caption).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                    }
                    Spacer(minLength: BoardStyle.Space.s)
                    if let duration = notice.duration {
                        Text(duration).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                    }
                    if notice.command != nil {
                        Image(systemName: "chevron.right").font(.caption2.weight(.semibold)).foregroundStyle(.tertiary)
                            .rotationEffect(.degrees(expanded ? 90 : 0))
                    }
                }
                .frame(minHeight: 44)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(notice.command == nil)
            .accessibilityLabel("Background task \(status.lowercased()): \(title)")
            .accessibilityValue([notice.duration, notice.exitCode.map { "exit code \($0)" }].compactMap { $0 }.joined(separator: ", "))
            .accessibilityHint(notice.command == nil ? "" : (expanded ? "Hides the command" : "Shows the command"))
            .accessibilityIdentifier("process-notice-\(message.id)")

            if expanded, let command = notice.command {
                code(command, label: "Command")
                    .accessibilityIdentifier("process-notice-command-\(message.id)")
            }
            if !notice.ok, let output = notice.output, !output.isEmpty {
                code(output, label: "Last output")
                    .accessibilityIdentifier("process-notice-output-\(message.id)")
            }
        }
        .padding(.horizontal, BoardStyle.Space.m)
        .padding(.vertical, BoardStyle.Space.xs)
        .background(BoardStyle.surface.opacity(0.6), in: RoundedRectangle(cornerRadius: BoardStyle.Radius.small))
        .overlay(alignment: .leading) {
            // A thin edge in the tint of the outcome sets the row apart from a reply.
            UnevenRoundedRectangle(topLeadingRadius: BoardStyle.Radius.small, bottomLeadingRadius: BoardStyle.Radius.small)
                .fill(tint).frame(width: 3)
        }
        .contextMenu {
            if let command = notice.command {
                Button("Copy command", systemImage: "doc.on.doc") { Pasteboard.copy(command) }
            }
            if let output = notice.output {
                Button("Copy output", systemImage: "doc.on.doc") { Pasteboard.copy(output) }
            }
        }
        .padding(.horizontal, BoardStyle.Space.m)
        .accessibilityElement(children: .contain)
    }

    /// "Finished in coconut" or "Failed · exit 2".
    private var caption: String {
        var parts = [status + (notice.place.map { " in \($0)" } ?? "")]
        if !notice.ok, let code = notice.exitCode { parts.append("exit \(code)") }
        return parts.joined(separator: " · ")
    }

    private func code(_ text: String, label: String) -> some View {
        Text(text)
            .font(.caption.monospaced())
            .foregroundStyle(BoardStyle.ink)
            .textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(BoardStyle.Space.s)
            .background(BoardStyle.paper, in: RoundedRectangle(cornerRadius: BoardStyle.Space.s))
            .padding(.bottom, BoardStyle.Space.xs)
            .accessibilityLabel("\(label): \(text)")
    }
}
