import ImageIO
import SwiftUI
import UIKit

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
        .fullScreenCover(isPresented: $viewerOpen) {
            // The viewer closes into the photo it shows, so the row keeps that photo in view.
            PhotoViewer(photos: photos, index: $viewing)
                .navigationTransition(.zoom(sourceID: viewing, in: zoom))
        }
        .onChange(of: viewing) { _, index in scrolled = index }
    }

    private func tile(_ photo: MessagePhoto, message: Message, index: Int) -> some View {
        // A photo message takes Reply and reactions like a text message, from the long-press menu.
        let canReply = onReply != nil && store.messageReference(for: message) != nil
        let reactions = store.reactions(for: message)
        return VStack(alignment: .leading, spacing: 4) {
            Button { viewing = index; viewerOpen = true } label: {
                VStack(alignment: .leading, spacing: 4) {
                    RemotePhoto(url: photo.url)
                        .frame(width: width, height: height)
                        .clipShape(RoundedRectangle(cornerRadius: BoardStyle.Radius.small))
                        // One flat layer, so the press highlight keeps the crop of the photo.
                        .compositingGroup()
                        .matchedTransitionSource(id: index, in: zoom)
                    if !photo.caption.isEmpty {
                        Text(photo.caption).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                            .frame(width: width, alignment: .leading)
                    }
                }
            }
            .buttonStyle(.plain)
            .contentShape(.contextMenuPreview, RoundedRectangle(cornerRadius: BoardStyle.Radius.small))
            .accessibilityLabel(photo.label)
            .accessibilityHint("Opens the photo")
            .accessibilityIdentifier("photo-\(index)")
            .contextMenu {
                if canReply {
                    Button("Reply", systemImage: "arrowshape.turn.up.left") { onReply?(message) }
                    ForEach(MessageReactions.selectable, id: \.self) { emoji in
                        Button("\(emoji) \(reactions.contains(emoji) ? "Remove" : "React with") \(MessageReactions.name(emoji))") {
                            Task { await store.react(to: message, emoji: emoji) }
                        }
                    }
                }
                Button("Copy link", systemImage: "link") { UIPasteboard.general.url = photo.url }
                ShareLink(item: photo.url)
            } preview: {
                // The lifted photo shows whole, as in Photos. A snapshot of the tile would lose its crop.
                PhotoMenuPreview(url: photo.url)
            }
            .accessibilityActions {
                if canReply {
                    Button("Reply") { onReply?(message) }
                    ForEach(MessageReactions.selectable, id: \.self) { emoji in
                        Button("\(reactions.contains(emoji) ? "Remove" : "React with") \(MessageReactions.name(emoji))") {
                            Task { await store.react(to: message, emoji: emoji) }
                        }
                    }
                }
            }
            MessageReactionList(message: message, enabled: canReply)
        }
    }
}

/// The whole photo, at its own shape, above the long-press menu.
private struct PhotoMenuPreview: View {
    let url: URL

    var body: some View {
        if let image = PhotoLoader.shared.cached(url, size: .full) ?? PhotoLoader.shared.cached(url, size: .tile),
           image.size.width > 0, image.size.height > 0 {
            // A tall photo stays short enough to leave room for the menu.
            let scale = min(360 / image.size.width, 420 / image.size.height)
            Image(uiImage: image).resizable()
                .frame(width: image.size.width * scale, height: image.size.height * scale)
        } else {
            RemotePhoto(url: url).frame(width: 360, height: 240)
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
                    ShareLink(item: Image(uiImage: image),
                              preview: SharePreview(photo.label, image: Image(uiImage: image)))
                } else {
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
    @State private var image: UIImage?
    @State private var failed = false

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
        .task(id: url) {
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

/// A photo from an address. It shows a placeholder while it loads and an icon when it fails.
struct RemotePhoto: View {
    let url: URL
    // A photo in the cache shows at once, without a frame of the placeholder.
    @State private var image: UIImage?
    @State private var failed = false

    init(url: URL) {
        self.url = url
        _image = State(initialValue: PhotoLoader.shared.cached(url, size: .tile))
    }

    var body: some View {
        Rectangle().fill(BoardStyle.surface)
            .overlay {
                if let image {
                    Image(uiImage: image).resizable().scaledToFill()
                } else if failed {
                    Image(systemName: "photo").font(.title2).foregroundStyle(.secondary)
                } else {
                    ProgressView()
                }
            }
            .clipped()
            .task(id: url) {
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
    private let images = NSCache<NSString, UIImage>()
    private let session: URLSession

    private init() {
        images.totalCostLimit = 96 << 20
        let configuration = URLSessionConfiguration.default
        configuration.urlCache = URLCache(memoryCapacity: 8 << 20, diskCapacity: 200 << 20)
        configuration.requestCachePolicy = .returnCacheDataElseLoad
        session = URLSession(configuration: configuration)
    }

    func cached(_ url: URL, size: PhotoSize) -> UIImage? { images.object(forKey: Self.key(url, size)) }

    /// The disk cache keeps the downloaded data, so the second size of a photo does not download it again.
    func image(_ url: URL, size: PhotoSize) async -> UIImage? {
        if let image = cached(url, size: size) { return image }
        guard let data = await download(url),
              let image = downsampledImage(data, largestSide: size.rawValue).map(UIImage.init(cgImage:)) else { return nil }
        images.setObject(image, forKey: Self.key(url, size), cost: image.cgImage.map { $0.bytesPerRow * $0.height } ?? data.count)
        return image
    }

    /// The data of a photo, or nil for a failed request or a file larger than `largestDownload`. The limit applies
    /// while the data arrives, so a huge file never fills the memory.
    private func download(_ url: URL) async -> Data? {
        guard let (bytes, response) = try? await session.bytes(from: url),
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
