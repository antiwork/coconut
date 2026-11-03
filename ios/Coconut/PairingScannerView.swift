import SwiftUI
#if os(iOS)
import AVFoundation
import VisionKit

struct PairingScannerView: View {
    @Environment(\.dismiss) private var dismiss
    @State private var ready = false
    @State private var cameraDenied = false
    @State private var error: String?
    let onPayload: (String) -> Void

    var body: some View {
        NavigationStack {
            Group {
                if ready {
                    SetupQRScanner { payload in
                        onPayload(payload)
                        dismiss()
                    } onFailure: {
                        ready = false
                        error = "The camera cannot scan right now. Paste the pairing link instead."
                    }
                    .ignoresSafeArea(edges: .bottom)
                    .safeAreaInset(edge: .bottom) {
                        Text("Scan the pairing QR code that Hermes sent.")
                            .font(.subheadline).padding().frame(maxWidth: .infinity).background(.regularMaterial)
                    }
                } else if let error {
                    ContentUnavailableView {
                        Label("Camera unavailable", systemImage: "camera")
                    } description: {
                        Text(error)
                    } actions: {
                        if cameraDenied {
                            Button("Open Settings") {
                                guard let url = URL(string: UIApplication.openSettingsURLString) else { return }
                                UIApplication.shared.open(url)
                            }
                        }
                        Button("Close") { dismiss() }
                    }
                } else {
                    ProgressView("Opening camera…")
                }
            }
            .navigationTitle("Scan pairing QR code").inlineNavigationTitle()
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
            }
            .task {
                guard DataScannerViewController.isSupported else {
                    error = "This device cannot scan QR codes. Paste the pairing link instead."
                    return
                }
                let allowed: Bool
                switch AVCaptureDevice.authorizationStatus(for: .video) {
                case .authorized: allowed = true
                case .notDetermined: allowed = await AVCaptureDevice.requestAccess(for: .video)
                default: allowed = false
                }
                guard !Task.isCancelled else { return }
                cameraDenied = !allowed
                if allowed && DataScannerViewController.isAvailable {
                    ready = true
                } else {
                    error = allowed ? "The camera cannot scan right now. Paste the pairing link instead."
                        : "Allow camera access in Settings to scan, or paste the pairing link instead."
                }
            }
        }
    }
}

private struct SetupQRScanner: UIViewControllerRepresentable {
    let onPayload: (String) -> Void
    let onFailure: () -> Void

    func makeCoordinator() -> Coordinator { Coordinator(onPayload: onPayload, onFailure: onFailure) }

    func makeUIViewController(context: Context) -> DataScannerViewController {
        let controller = DataScannerViewController(recognizedDataTypes: [.barcode(symbologies: [.qr])],
            qualityLevel: .balanced, recognizesMultipleItems: false, isHighFrameRateTrackingEnabled: false,
            isPinchToZoomEnabled: true, isGuidanceEnabled: true, isHighlightingEnabled: true)
        controller.delegate = context.coordinator
        let coordinator = context.coordinator
        Task { @MainActor [weak controller] in
            guard let controller, !coordinator.finished else { return }
            do { try controller.startScanning() }
            catch { coordinator.fail(controller) }
        }
        return controller
    }

    func updateUIViewController(_ controller: DataScannerViewController, context: Context) {}

    static func dismantleUIViewController(_ controller: DataScannerViewController, coordinator: Coordinator) {
        coordinator.finished = true
        controller.stopScanning()
    }

    @MainActor final class Coordinator: NSObject, DataScannerViewControllerDelegate {
        var finished = false
        private let onPayload: (String) -> Void
        private let onFailure: () -> Void

        init(onPayload: @escaping (String) -> Void, onFailure: @escaping () -> Void) {
            self.onPayload = onPayload
            self.onFailure = onFailure
        }

        func dataScanner(_ scanner: DataScannerViewController, didAdd addedItems: [RecognizedItem], allItems: [RecognizedItem]) {
            for item in addedItems { receive(item, scanner: scanner) }
        }

        func dataScanner(_ scanner: DataScannerViewController, didTapOn item: RecognizedItem) {
            receive(item, scanner: scanner)
        }

        func dataScanner(_ scanner: DataScannerViewController, becameUnavailableWithError error: DataScannerViewController.ScanningUnavailable) {
            fail(scanner)
        }

        func fail(_ scanner: DataScannerViewController) {
            guard !finished else { return }
            finished = true
            scanner.stopScanning()
            onFailure()
        }

        private func receive(_ item: RecognizedItem, scanner: DataScannerViewController) {
            guard !finished, case .barcode(let barcode) = item, let payload = barcode.payloadStringValue else { return }
            finished = true
            scanner.stopScanning()
            onPayload(payload)
        }
    }
}

#endif

struct PastePairingSetupView: View {
    @Environment(\.dismiss) private var dismiss
    @State private var payload = ""
    @State private var error: String?
    @ScaledMetric(relativeTo: .caption) private var editorHeight = 180.0
    let importPayload: (String) throws -> Void

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextEditor(text: $payload)
                        .font(.caption.monospaced()).frame(minHeight: editorHeight)
                        .noAutocapitalization().autocorrectionDisabled().privacySensitive()
                        .accessibilityLabel("Pairing link").accessibilityIdentifier("pairing-setup-payload")
                } footer: {
                    Text("Paste the pairing link from Coconut on your Mac. Coconut shows the name of the Mac before you pair.")
                }
                if let error { Section { InlineError(error) } }
                Section {
                    Button("Use this link") {
                        do {
                            try importPayload(payload)
                            payload = ""
                            dismiss()
                        } catch { self.error = error.localizedDescription }
                    }
                    .disabled(payload.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .accessibilityIdentifier("use-pairing-setup")
                }
            }
            .settingsBackground()
            .navigationTitle("Paste pairing link").inlineNavigationTitle()
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
            }
        }
    }
}
