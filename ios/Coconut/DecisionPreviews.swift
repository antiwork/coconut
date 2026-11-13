import SwiftUI
import ImageIO
import QuickLook

private struct PreparedDecisionPreview: Sendable {
    let file: URL
    let thumbnail: CGImage

    static func prepare(_ data: Data, id: String) throws -> Self {
        guard let thumbnail = downsampledImage(data, largestSide: 1000) else { throw ServiceError.invalidResponse }
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("CoconutPreviews", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let isPNG = data.starts(with: [0x89, 0x50, 0x4e, 0x47])
        let file = directory.appendingPathComponent(id).appendingPathExtension(isPNG ? "png" : "jpg")
        try data.write(to: file, options: [.atomic, .completeFileProtection])
        return Self(file: file, thumbnail: thumbnail)
    }
}

struct DecisionPreviewStrip: View {
    @Environment(AppStore.self) private var store
    let previews: [DecisionPreview]
    @Binding var loadedIds: Set<String>
    var onFrameChange: (CGRect) -> Void = { _ in }
    @State private var loaded: [String: PreparedDecisionPreview] = [:]
    @State private var failed = false
    @State private var attempt = 0
    @State private var selection: DecisionPreview?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            GeometryReader { geometry in
                ScrollView(.horizontal) {
                    HStack(alignment: .top, spacing: 8) {
                        ForEach(previews) { preview in
                            Button { selection = preview } label: {
                                VStack(alignment: .leading, spacing: BoardStyle.Space.s) {
                                    ZStack {
                                        RoundedRectangle(cornerRadius: BoardStyle.Radius.small).fill(BoardStyle.paper)
                                        if let image = loaded[preview.id]?.thumbnail {
                                            Image(decorative: image, scale: 1).resizable().scaledToFit()
                                                .clipShape(RoundedRectangle(cornerRadius: BoardStyle.Radius.small))
                                        } else if failed {
                                            Image(systemName: "photo").foregroundStyle(.secondary)
                                        } else { ProgressView() }
                                    }
                                    .frame(height: 200)
                                    Text(preview.caption).font(.footnote).foregroundStyle(.secondary)
                                        .lineLimit(2).frame(maxWidth: .infinity, alignment: .leading)
                                }
                                .frame(width: previews.count == 1 ? geometry.size.width : min(geometry.size.width * 0.86, max(140, 200 * CGFloat(preview.width) / CGFloat(max(1, preview.height)))))
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            .disabled(!previews.allSatisfy { loaded[$0.id] != nil })
                            .accessibilityLabel("Open preview: \(preview.caption)")
                            .accessibilityIdentifier("decision-preview-\(preview.id)")
                        }
                    }
                    .scrollTargetLayout()
                }
                .scrollIndicators(.hidden)
                .scrollTargetBehavior(.viewAligned)
            }
            .frame(height: 240)
            if failed {
                Button("Could not load the previews. Retry") { attempt += 1 }
                    .font(.footnote).frame(minHeight: 44)
            }
        }
        .onGeometryChange(for: CGRect.self) { $0.frame(in: .global) } action: { onFrameChange($0) }
        .task(id: PreviewLoadKey(previews: previews, attempt: attempt)) {
            loadedIds = []
            failed = false
            loaded = loaded.filter { id, _ in previews.contains { $0.id == id } }
            for preview in previews where loaded[preview.id] == nil {
                do {
                    let data = try await store.decisionPreviewData(id: preview.id)
                    let result = try await Task.detached(priority: .userInitiated) {
                        try PreparedDecisionPreview.prepare(data, id: preview.id)
                    }.value
                    try Task.checkCancellation()
                    loaded[preview.id] = result
                } catch is CancellationError where Task.isCancelled {
                    // The view went away. Its next appearance loads again.
                    return
                } catch {
                    // A read that the tunnel cut, when the app went to the background, is a failure too: Retry
                    // loads it again.
                    failed = true
                }
            }
            loadedIds = Set(loaded.keys)
        }
        #if os(iOS)
        .fullScreenCover(item: $selection) { selected in
            DecisionPreviewViewer(previews: previews.compactMap { preview in
                loaded[preview.id].map { PreviewFile(url: $0.file, title: preview.caption, id: preview.id) }
            }, selectedId: selected.id)
                .ignoresSafeArea()
        }
        #else
        // The Quick Look panel of the Mac shows the files, and the arrow keys move between them.
        .quickLookPreview(Binding(get: { selection.flatMap { loaded[$0.id]?.file } },
                                  set: { if $0 == nil { selection = nil } }),
                          in: previews.compactMap { loaded[$0.id]?.file })
        #endif
    }
}

private struct PreviewLoadKey: Equatable {
    let previews: [DecisionPreview]
    let attempt: Int
}

#if os(iOS)
private final class PreviewFile: NSObject, QLPreviewItem {
    let previewItemURL: URL?
    let previewItemTitle: String?
    let id: String
    init(url: URL, title: String, id: String) {
        previewItemURL = url; previewItemTitle = title; self.id = id
    }
}

private struct DecisionPreviewViewer: UIViewControllerRepresentable {
    @Environment(\.dismiss) private var dismiss
    let previews: [PreviewFile]
    let selectedId: String

    func makeCoordinator() -> Coordinator { Coordinator(previews: previews, dismiss: dismiss) }
    func makeUIViewController(context: Context) -> UINavigationController {
        let controller = QLPreviewController()
        controller.dataSource = context.coordinator
        controller.delegate = context.coordinator
        controller.currentPreviewItemIndex = previews.firstIndex { $0.id == selectedId } ?? 0
        controller.navigationItem.leftBarButtonItem = UIBarButtonItem(systemItem: .done,
            primaryAction: UIAction { _ in context.coordinator.dismiss() })
        return UINavigationController(rootViewController: controller)
    }
    func updateUIViewController(_ controller: UINavigationController, context: Context) {}

    final class Coordinator: NSObject, QLPreviewControllerDataSource, QLPreviewControllerDelegate {
        let previews: [PreviewFile]
        let dismiss: DismissAction
        init(previews: [PreviewFile], dismiss: DismissAction) { self.previews = previews; self.dismiss = dismiss }
        nonisolated func previewController(_ controller: QLPreviewController, editingModeFor previewItem: any QLPreviewItem) -> QLPreviewItemEditingMode { .disabled }
        func numberOfPreviewItems(in controller: QLPreviewController) -> Int { previews.count }
        func previewController(_ controller: QLPreviewController, previewItemAt index: Int) -> any QLPreviewItem { previews[index] }
    }
}
#endif
