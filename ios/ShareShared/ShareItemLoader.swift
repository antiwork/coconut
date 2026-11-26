import Foundation
import ImageIO
import UniformTypeIdentifiers
#if canImport(UIKit)
import UIKit
#else
import AppKit
#endif

/// Reads the items of the share sheet into values of Coconut. An item that Coconut cannot read counts as skipped.
enum ShareItemLoader {
    /// The largest file that the extension reads. An image can be larger than the limit of the Mac, because the fit
    /// step makes it smaller (TeamFileRules.largestRead).
    static let largestRead = 64 * 1024 * 1024

    @MainActor
    static func load(_ extensionItems: [NSExtensionItem]) async -> (items: [SharedItem], skipped: Int) {
        var items: [SharedItem] = []
        var skipped = 0
        var unnamed = 0
        for extensionItem in extensionItems {
            let providers = extensionItem.attachments ?? []
            // A browser gives the title of the page here, next to its link. Only a link gets it: another app can put
            // the name of a file here.
            let hasLink = providers.contains { ShareRepresentation.choose(from: $0.registeredTypeIdentifiers) == .link }
            if hasLink, let title = extensionItem.attributedContentText?.string, !title.isEmpty {
                items.append(.text(title))
            }
            for provider in providers {
                let identifiers = provider.registeredTypeIdentifiers
                var loaded: SharedItem?
                switch ShareRepresentation.choose(from: identifiers) {
                case .fileURL:
                    if let url = await url(provider), url.isFileURL { loaded = read(url).map(SharedItem.file) }
                    // An app can name a file that the extension may not open. Its bytes come as data then.
                    if loaded == nil, let id = identifiers.first(where: Self.isContent), let data = await data(provider, id) {
                        loaded = file(data, provider: provider, type: id, unnamed: &unnamed)
                    }
                case .data(let id):
                    let data = await data(provider, id)
                    if UTType(id)?.conforms(to: .image) == true, !(data.map(isImage) ?? false),
                       let png = await imageData(provider, id) {
                        // An app can share an image object. Its provider gives an archive of the object, or nothing,
                        // in place of the bytes of an image, and such a file did not open on the Mac.
                        loaded = file(png, provider: provider, type: UTType.png.identifier, unnamed: &unnamed)
                    } else if let data {
                        loaded = file(data, provider: provider, type: id, unnamed: &unnamed)
                    }
                case .link:
                    if let url = await url(provider) {
                        loaded = url.isFileURL ? read(url).map(SharedItem.file) : .link(url)
                    }
                case .text:
                    loaded = await text(provider).map(SharedItem.text)
                case nil:
                    break
                }
                if let loaded { items.append(loaded) } else { skipped += 1 }
            }
        }
        return (items, skipped)
    }

    /// A type of bytes of a file: data that is no address.
    private static func isContent(_ identifier: String) -> Bool {
        guard let type = UTType(identifier) else { return false }
        return type.conforms(to: .data) && !type.conforms(to: .url)
    }

    /// The bytes are an image file that ImageIO reads.
    private static func isImage(_ data: Data) -> Bool {
        CGImageSourceCreateWithData(data as CFData, nil).flatMap(CGImageSourceGetType) != nil
    }

    private static func file(_ data: Data, provider: NSItemProvider, type: String, unnamed: inout Int) -> SharedItem? {
        guard !data.isEmpty, data.count <= largestRead else { return nil }
        let suggested = provider.suggestedName
        let name = ShareFileName.name(suggested: suggested, typeIdentifier: type, index: unnamed)
        if (suggested ?? "").isEmpty { unnamed += 1 }
        return .file(SharedFile(data: data, name: name))
    }

    /// The bytes and the name of a file of the device. Nil for a folder, an empty file, and a file that is too large
    /// to read.
    static func read(_ url: URL) -> SharedFile? {
        let accessed = url.startAccessingSecurityScopedResource()
        defer { if accessed { url.stopAccessingSecurityScopedResource() } }
        guard let values = try? url.resourceValues(forKeys: [.fileSizeKey, .isRegularFileKey]),
              values.isRegularFile == true, let size = values.fileSize, size > 0, size <= largestRead,
              let data = try? Data(contentsOf: url) else { return nil }
        return SharedFile(data: data, name: url.lastPathComponent)
    }

    @MainActor
    private static func data(_ provider: NSItemProvider, _ identifier: String) async -> Data? {
        await withCheckedContinuation { continuation in
            _ = provider.loadDataRepresentation(forTypeIdentifier: identifier) { data, _ in
                continuation.resume(returning: data)
            }
        }
    }

    @MainActor
    private static func url(_ provider: NSItemProvider) async -> URL? {
        await withCheckedContinuation { continuation in
            _ = provider.loadObject(ofClass: URL.self) { url, _ in continuation.resume(returning: url) }
        }
    }

    /// The image object of an item, as a PNG, or nil when the item has none. Only the old way to load an item gives
    /// the object: a load as an image class reads the archive as image bytes and fails.
    @MainActor
    private static func imageData(_ provider: NSItemProvider, _ identifier: String) async -> Data? {
        await withCheckedContinuation { continuation in
            provider.loadItem(forTypeIdentifier: identifier, options: nil) { item, _ in
                #if canImport(UIKit)
                continuation.resume(returning: (item as? UIImage)?.pngData())
                #else
                let bitmap = (item as? NSImage)?.tiffRepresentation.flatMap(NSBitmapImageRep.init(data:))
                continuation.resume(returning: bitmap?.representation(using: .png, properties: [:]))
                #endif
            }
        }
    }

    @MainActor
    private static func text(_ provider: NSItemProvider) async -> String? {
        await withCheckedContinuation { continuation in
            _ = provider.loadObject(ofClass: String.self) { text, _ in continuation.resume(returning: text) }
        }
    }
}
