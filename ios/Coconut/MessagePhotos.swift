import ImageIO
import SwiftUI
#if canImport(UIKit)
import UIKit
#endif

/// A row of photos that a bot sent one after another. A tap opens the photos full screen.
struct PhotoCarousel: View {
    @Environment(AppStore.self) private var store
    /// The message of each photo, in the same order. Reply and reactions act on it.
    let messages: [Message]
    let photos: [MessagePhoto]
    var onReply: ((Message) -> Void)? = nil
    @State private var viewerOpen = false
    @State private var viewing = 0
    @State private var scrolled: Int?
    @State private var held: Int?
    @State private var pressing: Int?
    @Namespace private var zoom
    private var width: CGFloat { photos.count == 1 ? 280 : 236 }
    private let height: CGFloat = 180

    var body: some View {
        ScrollView(.horizontal) {
            LazyHStack(alignment: .top, spacing: 8) {
                ForEach(photos.indices, id: \.self) { index in
                    tile(photos[index], message: messages[index], index: index)
                }
            }
            .scrollTargetLayout()
        }
        .scrollPosition(id: $scrolled, anchor: .leading)
        .scrollTargetBehavior(.viewAligned)
        .scrollIndicators(.hidden)
        // The row reaches the screen edges, so the next photo shows that the row scrolls.
        .contentMargins(.horizontal, 16, for: .scrollContent)
        .padding(.horizontal, -16)
        .accessibilityIdentifier("photo-carousel")
        #if os(iOS)
        .fullScreenCover(isPresented: $viewerOpen) {
            // The viewer closes into the photo it shows, so the row keeps that photo in view.
            PhotoViewer(photos: photos, index: $viewing)
                .navigationTransition(.zoom(sourceID: viewing, in: zoom))
        }
        .onChange(of: viewing) { _, index in scrolled = index }
        .fullScreenCover(isPresented: Binding(get: { held != nil }, set: { if !$0 { held = nil } })) {
            if let held, held < photos.count {
                HeldPhoto(photo: photos[held], message: messages[held], onReply: onReply)
            }
        }
        #endif
    }

    private func tile(_ photo: MessagePhoto, message: Message, index: Int) -> some View {
        let canReply = onReply != nil && store.messageReference(for: message) != nil
        let reactions = store.reactions(for: message)
        return VStack(alignment: .leading, spacing: 4) {
            VStack(alignment: .leading, spacing: 4) {
                RemotePhoto(url: photo.url)
                    .frame(width: width, height: height)
                    .clipShape(RoundedRectangle(cornerRadius: BoardStyle.Radius.small))
                    .matchedTransitionSource(id: index, in: zoom)
                    .scaleEffect(pressing == index ? 0.96 : 1)
                    .animation(.spring(duration: 0.25), value: pressing)
                if !photo.caption.isEmpty {
                    Text(photo.caption).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                        .frame(width: width, alignment: .leading)
                }
            }
            .contentShape(Rectangle())
            .onTapGesture { viewing = index; viewerOpen = true }
            #if os(iOS)
            // A held photo shows its reactions and actions, as a held text message does.
            .onLongPressGesture(minimumDuration: 0.4) {
                UIImpactFeedbackGenerator(style: .medium).impactOccurred()
                pressing = nil
                // The actions fade in over the chat, without the slide of a full-screen cover.
                var transaction = Transaction()
                transaction.disablesAnimations = true
                withTransaction(transaction) { held = index }
            } onPressingChanged: { pressing = $0 ? index : nil }
            #endif
            .accessibilityElement(children: .ignore)
            .accessibilityAddTraits(.isButton)
            .accessibilityLabel(photo.label)
            .accessibilityHint("Opens the photo")
            .accessibilityIdentifier("photo-\(index)")
            .accessibilityAction { viewing = index; viewerOpen = true }
            .accessibilityActions {
                if canReply {
                    Button("Reply") { onReply?(message) }
                    ForEach(MessageReactions.selectable, id: \.self) { emoji in
                        Button("\(reactions.contains(emoji) ? "Remove" : "React with") \(MessageReactions.name(emoji))") {
                            Task { await store.react(to: message, emoji: emoji) }
                        }
                    }
                }
                if !photo.isMacFile { Button("Copy link") { Pasteboard.copy(photo.url) } }
            }
            MessageReactionList(message: message, enabled: canReply)
        }
    }
}

#if os(iOS)
/// A held photo. A photo from the Mac has no public address, so Copy and Share take its image, and they wait until it
/// has loaded.
private struct HeldPhoto: View {
    let photo: MessagePhoto
    let message: Message
    let onReply: ((Message) -> Void)?
    @State private var image: PlatformImage?

    var body: some View {
        let loaded = image
        HeldMessageView(message: message, onReply: onReply, copyTitle: photo.isMacFile ? "Copy" : "Copy link",
                        copy: photo.isMacFile ? loaded.map { image in { Pasteboard.copy(image) } }
                            : { Pasteboard.copy(photo.url) },
                        share: photo.isMacFile ? loaded.map { .image($0, photo.label) } : .url(photo.url)) { maxHeight in
            PhotoMenuPreview(url: photo.url, maxHeight: maxHeight)
                .clipShape(RoundedRectangle(cornerRadius: BoardStyle.Radius.small))
                .accessibilityLabel(photo.label)
        }
        .task(id: photo.url) {
            guard photo.isMacFile else { return }
            image = PhotoLoader.shared.cached(photo.url, size: .full) ?? PhotoLoader.shared.cached(photo.url, size: .tile)
            if image == nil { image = await PhotoLoader.shared.image(photo.url, size: .full) }
        }
    }
}

/// The whole photo at its own shape, between the reactions and the actions of a held photo.
private struct PhotoMenuPreview: View {
    let url: URL
    let maxHeight: CGFloat

    var body: some View {
        if let image = PhotoLoader.shared.cached(url, size: .full) ?? PhotoLoader.shared.cached(url, size: .tile),
           image.size.width > 0, image.size.height > 0 {
            let scale = min(340 / image.size.width, maxHeight / image.size.height)
            Image(platformImage: image).resizable()
                .frame(width: image.size.width * scale, height: image.size.height * scale)
        } else {
            RemotePhoto(url: url).frame(width: 340, height: min(230, maxHeight))
        }
    }
}

/// The photos of one carousel, full screen. Swipe sideways to move between them. The zoom transition of the
/// carousel closes the viewer on a swipe down.
struct PhotoViewer: View {
    let photos: [MessagePhoto]
    @Binding var index: Int
    @State private var zoomed = false
    @State private var loaded: Set<URL> = []
    @Environment(\.dismiss) private var dismiss
    // A new transcript can shorten the row while the viewer is open.
    private var photo: MessagePhoto { photos[min(index, photos.count - 1)] }

    var body: some View {
        TabView(selection: $index) {
            ForEach(photos.indices, id: \.self) { page in
                PhotoPage(url: photos[page].url, zoomed: page == index ? $zoomed : .constant(false)) {
                    loaded.insert(photos[page].url)
                }
                .accessibilityLabel(photos[page].label)
                .tag(page)
            }
        }
        .tabViewStyle(.page(indexDisplayMode: .never))
        .background(Color.black.ignoresSafeArea())
        .overlay(alignment: .top) { topBar }
        .overlay(alignment: .bottom) {
            if !photo.caption.isEmpty {
                Text(photo.caption).font(.subheadline).foregroundStyle(.white)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 20).padding(.vertical, 12)
                    .background(.black.opacity(0.5))
            }
        }
        .onChange(of: index) { _, _ in zoomed = false }
        .accessibilityAction(.escape) { dismiss() }
    }

    private var topBar: some View {
        HStack {
            Button { dismiss() } label: {
                Image(systemName: "xmark").font(.body.weight(.semibold)).frame(width: 44, height: 44)
            }
            .accessibilityLabel("Close")
            .accessibilityIdentifier("photo-viewer-close")
            Spacer()
            if photos.count > 1 {
                Text("\(index + 1) of \(photos.count)").font(.subheadline.weight(.medium)).monospacedDigit()
            }
            Spacer()
            Group {
                // A shared image offers Save Image. Before the image loads, the link is shared.
                if loaded.contains(photo.url), let image = PhotoLoader.shared.cached(photo.url, size: .full) {
                    ShareLink(item: Image(platformImage: image),
                              preview: SharePreview(photo.label, image: Image(platformImage: image)))
                } else if !photo.isMacFile {
                    ShareLink(item: photo.url)
                }
            }
            .labelStyle(.iconOnly).font(.body.weight(.semibold)).frame(width: 44, height: 44)
            .accessibilityIdentifier("photo-viewer-share")
        }
        .foregroundStyle(.white)
        .padding(.horizontal, 8)
        .background(.black.opacity(0.5), in: Capsule())
        .padding(.horizontal, 12)
    }
}

private struct PhotoPage: View {
    let url: URL
    @Binding var zoomed: Bool
    let loaded: () -> Void
    @State private var image: PlatformImage?
    @State private var failed = false
    @State private var attempt = 0

    var body: some View {
        ZStack {
            if let image {
                ZoomableImage(image: image, zoomed: $zoomed)
            } else if failed {
                Label("The photo did not load.", systemImage: "photo").foregroundStyle(.white.opacity(0.7))
            } else {
                ProgressView().tint(.white)
            }
        }
        .ignoresSafeArea()
        .onReceive(NotificationCenter.default.publisher(for: .coconutMacConnected)) { _ in
            if failed && url.scheme == "coconut-file" { attempt += 1 }
        }
        .task(id: "\(url.absoluteString) \(attempt)") {
            failed = false
            image = await PhotoLoader.shared.image(url, size: .full)
            failed = image == nil
            if image != nil { loaded() }
        }
    }
}

/// UIKit zooms and pans a photo with the feel of Photos. A double tap zooms in or out.
private struct ZoomableImage: UIViewRepresentable {
    let image: UIImage
    @Binding var zoomed: Bool

    func makeCoordinator() -> Coordinator { Coordinator(zoomed: $zoomed) }

    func makeUIView(context: Context) -> UIScrollView {
        let scroll = UIScrollView()
        scroll.delegate = context.coordinator
        scroll.maximumZoomScale = 4
        scroll.showsHorizontalScrollIndicator = false
        scroll.showsVerticalScrollIndicator = false
        scroll.contentInsetAdjustmentBehavior = .never
        // The pages move sideways and a drag down closes the viewer until the photo is zoomed.
        scroll.isScrollEnabled = false
        let view = UIImageView(image: image)
        view.contentMode = .scaleAspectFit
        view.translatesAutoresizingMaskIntoConstraints = false
        scroll.addSubview(view)
        NSLayoutConstraint.activate([
            view.leadingAnchor.constraint(equalTo: scroll.contentLayoutGuide.leadingAnchor),
            view.trailingAnchor.constraint(equalTo: scroll.contentLayoutGuide.trailingAnchor),
            view.topAnchor.constraint(equalTo: scroll.contentLayoutGuide.topAnchor),
            view.bottomAnchor.constraint(equalTo: scroll.contentLayoutGuide.bottomAnchor),
            view.widthAnchor.constraint(equalTo: scroll.frameLayoutGuide.widthAnchor),
            view.heightAnchor.constraint(equalTo: scroll.frameLayoutGuide.heightAnchor),
        ])
        context.coordinator.imageView = view
        let doubleTap = UITapGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.doubleTapped))
        doubleTap.numberOfTapsRequired = 2
        scroll.addGestureRecognizer(doubleTap)
        return scroll
    }

    func updateUIView(_ scroll: UIScrollView, context: Context) {
        context.coordinator.zoomed = $zoomed
        if context.coordinator.imageView?.image !== image { context.coordinator.imageView?.image = image }
        if !zoomed && scroll.zoomScale > 1 { scroll.setZoomScale(1, animated: false) }
    }

    @MainActor
    final class Coordinator: NSObject, UIScrollViewDelegate {
        var zoomed: Binding<Bool>
        weak var imageView: UIImageView?

        init(zoomed: Binding<Bool>) { self.zoomed = zoomed }

        func viewForZooming(in scrollView: UIScrollView) -> UIView? { imageView }

        func scrollViewDidZoom(_ scrollView: UIScrollView) {
            let isZoomed = scrollView.zoomScale > 1.01
            scrollView.isScrollEnabled = isZoomed
            if zoomed.wrappedValue != isZoomed { zoomed.wrappedValue = isZoomed }
        }

        @objc func doubleTapped(_ gesture: UITapGestureRecognizer) {
            guard let scroll = gesture.view as? UIScrollView, let imageView else { return }
            if scroll.zoomScale > 1 {
                scroll.setZoomScale(1, animated: true)
            } else {
                let point = gesture.location(in: imageView)
                let size = CGSize(width: scroll.bounds.width / 2.5, height: scroll.bounds.height / 2.5)
                scroll.zoom(to: CGRect(x: point.x - size.width / 2, y: point.y - size.height / 2,
                                       width: size.width, height: size.height), animated: true)
            }
        }
    }
}

#endif

/// A photo from an address. It shows a placeholder while it loads and an icon when it fails.
struct RemotePhoto: View {
    let url: URL
    // A photo in the cache shows at once, without a frame of the placeholder.
    @State private var image: PlatformImage?
    @State private var failed = false
    @State private var attempt = 0

    init(url: URL) {
        self.url = url
        _image = State(initialValue: PhotoLoader.shared.cached(url, size: .tile))
    }

    var body: some View {
        Rectangle().fill(BoardStyle.surface)
            .overlay {
                if let image {
                    Image(platformImage: image).resizable().scaledToFill()
                } else if failed {
                    Image(systemName: "photo").font(.title2).foregroundStyle(.secondary)
                } else {
                    ProgressView()
                }
            }
            .clipped()
            .onReceive(NotificationCenter.default.publisher(for: .coconutMacConnected)) { _ in
                if failed && url.scheme == "coconut-file" { attempt += 1 }
            }
            .task(id: "\(url.absoluteString) \(attempt)") {
                failed = false
                image = PhotoLoader.shared.cached(url, size: .tile)
                guard image == nil else { return }
                image = await PhotoLoader.shared.image(url, size: .tile)
                failed = image == nil
            }
    }
}

/// The longest side in pixels of a decoded photo. A tile in the chat needs far fewer pixels than the viewer.
enum PhotoSize: Int {
    case tile = 900
    case full = 2400
}

/// Loads photos and keeps them in memory and on disk, so a scroll back or the viewer shows them at once.
final class PhotoLoader: @unchecked Sendable {
    static let shared = PhotoLoader()
    // NSCache is safe to use from any thread.
    private let images = NSCache<NSString, PlatformImage>()
    private let session: URLSession
    /// Loads a photo that a bot saved on the Mac, `coconut-file://<artifact id>`, through the paired Mac. The store
    /// sets it.
    var macFile: (@Sendable (String) async -> Data?)?

    private init() {
        images.totalCostLimit = 96 << 20
        let configuration = URLSessionConfiguration.default
        configuration.urlCache = URLCache(memoryCapacity: 8 << 20, diskCapacity: 200 << 20)
        configuration.requestCachePolicy = .returnCacheDataElseLoad
        session = URLSession(configuration: configuration)
    }

    func cached(_ url: URL, size: PhotoSize) -> PlatformImage? { images.object(forKey: Self.key(url, size)) }

    /// The disk cache keeps the downloaded data, so the second size of a photo does not download it again.
    func image(_ url: URL, size: PhotoSize) async -> PlatformImage? {
        if let image = cached(url, size: size) { return image }
        guard let data = await download(url), let decoded = downsampledImage(data, largestSide: size.rawValue)
        else { return nil }
        let image = PlatformImage(decoded)
        images.setObject(image, forKey: Self.key(url, size), cost: decoded.bytesPerRow * decoded.height)
        return image
    }

    /// The bytes of a photo that a bot saved on the Mac, for a Library page (DocumentPage). Nil when the Mac cannot send them.
    func macFileData(_ url: URL) async -> Data? {
        guard url.scheme == "coconut-file" else { return nil }
        return await download(url)
    }

    /// The data of a photo, or nil for a failed request or a file larger than `largestDownload`. The limit applies
    /// while the data arrives, so a huge file never fills the memory.
    private func download(_ url: URL) async -> Data? {
        if url.scheme == "coconut-file" {
            // The bytes of an attachment never change, so both sizes and later launches reuse one transfer.
            guard let id = url.host(), UUID(uuidString: id) != nil else { return nil }
            let file = Self.macFiles.appendingPathComponent(id)
            if let data = try? Data(contentsOf: file) {
                // A read marks the photo as recently used, so the trim removes the photos nobody opens.
                try? FileManager.default.setAttributes([.modificationDate: Date()], ofItemAtPath: file.path)
                return data
            }
            guard let macFile, let data = await macFile(id) else { return nil }
            try? FileManager.default.createDirectory(at: Self.macFiles, withIntermediateDirectories: true,
                                                     attributes: [.protectionKey: FileProtectionType.complete])
            try? data.write(to: file, options: [.atomic, .completeFileProtection])
            Self.trimMacFiles()
            return data
        }
        // A redirect may go only to a public page, so an address cannot send the phone to a private service.
        guard let (bytes, response) = try? await session.bytes(from: url, delegate: PublicRedirects.shared),
              (response as? HTTPURLResponse).map({ (200..<300).contains($0.statusCode) }) ?? false,
              response.expectedContentLength <= Int64(Self.largestDownload) else { return nil }
        var data = Data()
        data.reserveCapacity(Int(max(response.expectedContentLength, 0)))
        do {
            for try await byte in bytes {
                guard data.count < Self.largestDownload else { return nil }
                data.append(byte)
            }
        } catch { return nil }
        return data
    }

    private static let largestDownload = 20 << 20

    // The caches of an iPhone app are its own. A Mac app that has no sandbox shares the folder, so it uses a folder of
    // its bundle there.
    private static let macFiles = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
        #if os(macOS)
        .appendingPathComponent(Bundle.main.bundleIdentifier ?? "com.gumroad.coconut.mac", isDirectory: true)
        #endif
        .appendingPathComponent(TestIsolation.name("MacPhotos"), isDirectory: true)

    /// Above 200 MB of Mac photos, the oldest used ones go until 150 MB are left, as a URL cache keeps its size.
    static func trimMacFiles(in folder: URL = macFiles, limit: Int = 200 << 20, target: Int = 150 << 20) {
        let keys: [URLResourceKey] = [.fileSizeKey, .contentModificationDateKey]
        guard let files = try? FileManager.default.contentsOfDirectory(at: folder, includingPropertiesForKeys: keys)
        else { return }
        var entries = files.compactMap { file -> (url: URL, size: Int, used: Date)? in
            guard let values = try? file.resourceValues(forKeys: Set(keys)), let size = values.fileSize else { return nil }
            return (file, size, values.contentModificationDate ?? .distantPast)
        }
        var total = entries.reduce(0) { $0 + $1.size }
        guard total > limit else { return }
        entries.sort { $0.used < $1.used }
        for entry in entries where total > target {
            try? FileManager.default.removeItem(at: entry.url)
            total -= entry.size
        }
    }

    private static func key(_ url: URL, _ size: PhotoSize) -> NSString { "\(size.rawValue) \(url.absoluteString)" as NSString }
}

/// Decodes image data at no more than the given number of pixels on its longest side, turned upright.
/// A full-size camera photo would use too much memory.
func downsampledImage(_ data: Data, largestSide: Int) -> CGImage? {
    guard let source = CGImageSourceCreateWithData(data as CFData, nil) else { return nil }
    return CGImageSourceCreateThumbnailAtIndex(source, 0, [
        kCGImageSourceCreateThumbnailFromImageAlways: true,
        kCGImageSourceCreateThumbnailWithTransform: true,
        kCGImageSourceShouldCacheImmediately: true,
        kCGImageSourceThumbnailMaxPixelSize: largestSide,
    ] as CFDictionary)
}

extension Notification.Name {
    /// The phone reached the paired Mac again.
    static let coconutMacConnected = Notification.Name("CoconutMacConnected")
}
