import Foundation
import ImageIO
import UniformTypeIdentifiers

// The step before an upload: a file fits the limit of the Mac. The app and the share extension of the iPhone compile
// this file, so it uses Foundation and ImageIO only.

/// The limits of a file that goes to the Mac, and the texts for a file that does not fit.
enum FileRules {
    /// The largest file that the Mac accepts: `MAX_FILE` in backend/coconut/team_store.py, and the limit of an
    /// attachment of an agent chat.
    static let largest = 4 * 1024 * 1024
    static let sizeText = "Choose a file between 1 byte and 4 MB."
    /// A file that the fit step cannot make safe to send: a file of several images whose GPS position cannot go, or an
    /// image that the app cannot read. The text is true for both.
    static let cannotSendText = "This file cannot be sent."
}

/// What the step before an upload gives: the file as it goes, or why it cannot go.
enum FitOutcome: Equatable, Sendable {
    case fitted(FittedFile)
    /// The file is empty, or larger than the limit, also after an image became smaller.
    case tooLarge
    /// The app cannot make the file fit the rules: it cannot remove the GPS position of a file with more than one
    /// image, or it cannot read the image.
    case cannotSend

    var file: FittedFile? { if case .fitted(let file) = self { file } else { nil } }

    /// The text for the error banner, or nil for a file that goes.
    var text: String? {
        switch self {
        case .fitted: nil
        case .tooLarge: FileRules.sizeText
        case .cannotSend: FileRules.cannotSendText
        }
    }
}

/// The step before an upload: an image fits the limit of a file, and the chat gets its size in pixels.
struct FittedFile: Equatable, Sendable {
    let data: Data
    let name: String
    let type: String?
    let width: Int?
    let height: Int?
}

enum FileFit {
    /// The longest side of an image that the app makes smaller.
    static let longestSide = 2400
    static let quality = 0.8
    /// The largest side that the Mac takes in `width` and `height` (`MAX_SIDE`).
    static let largestSide = 20_000

    /// The file as it goes to the Mac, or nil when it cannot fit. Each piece of a file is one write to the relay and
    /// one download on each device, and the space of a person in a room holds 320 MiB in 90 days. So a photo goes at
    /// the size that a screen shows:
    /// - An image with a side over `longestSide` pixels becomes a JPEG of `longestSide` pixels, whatever its bytes.
    /// - A HEIC or HEIF image always becomes a JPEG, so each Mac and each app can show it.
    /// - Each other image within `longestSide` pixels stays as it is, unless it is over the limit: then it becomes a
    ///   JPEG of the same size.
    /// - A GIF never changes, because a JPEG loses its frames. A GIF over the limit does not fit.
    /// - Each other file must be 1 byte to the limit as it is.
    /// `width` and `height` are the pixels that go to the Mac.
    ///
    /// With `removesPosition` (the team chats), no image leaves the phone or the Mac with a GPS position. A new JPEG
    /// carries no metadata. An image that stays as it is goes as a copy without the GPS data and without a new encode
    /// of its pixels. When that copy fails or is over the limit, the image becomes a JPEG: never the bytes with the
    /// position. An image without a position keeps its bytes. The agent chat sends to the Mac of the person and keeps
    /// the metadata.
    static func fit(_ data: Data, name: String, limit: Int = FileRules.largest,
                    removesPosition: Bool = true) -> FittedFile? {
        outcome(data, name: name, limit: limit, removesPosition: removesPosition).file
    }

    /// The file as it goes to the Mac, or why it cannot go (`fit`).
    ///
    /// A file with more than one image (a TIFF with pages, an animated GIF, PNG, HEIC or WebP) is never encoded
    /// again: a JPEG would keep only the first image, for example page 1 of a scan of three pages. Without a position
    /// it goes as it is when it fits the limit, whatever its pixels. With a position it goes as the copy without it,
    /// which keeps each image, and else it does not go. ImageIO cannot copy a GIF this way, and its copy of a TIFF
    /// keeps the position, so a GIF or a TIFF of several images with a position does not go. `width` and `height` are
    /// the pixels of the first image.
    static func outcome(_ data: Data, name: String, limit: Int = FileRules.largest,
                        removesPosition: Bool = true) -> FitOutcome {
        guard !data.isEmpty else { return .tooLarge }
        guard let source = CGImageSourceCreateWithData(data as CFData, nil),
              let identifier = CGImageSourceGetType(source) as String?, let kind = UTType(identifier),
              kind.conforms(to: .image), CGImageSourceGetCount(source) > 0,
              let size = pixelSize(source) else {
            guard data.count <= limit else { return .tooLarge }
            return .fitted(FittedFile(data: data, name: name, type: mimeType(name), width: nil, height: nil))
        }
        let (width, height) = size
        let longest = max(width, height)
        let position = removesPosition && hasPosition(source)
        if CGImageSourceGetCount(source) > 1 {
            guard data.count <= limit else { return .tooLarge }
            guard position else { return .fitted(kept(data, name: name, kind: kind, width: width, height: height)) }
            guard let copy = withoutPosition(source, kind: kind), copy.count <= limit else { return .cannotSend }
            return .fitted(kept(copy, name: name, kind: kind, width: width, height: height))
        }
        let gif = kind.conforms(to: .gif)
        let heif = kind.conforms(to: .heic) || kind.conforms(to: .heif)
        if gif || (!heif && longest <= longestSide && data.count <= limit) {
            guard data.count <= limit || !gif else { return .tooLarge }
            guard position else { return .fitted(kept(data, name: name, kind: kind, width: width, height: height)) }
            // A GIF has no EXIF block, but it can carry a position in XMP data, and ImageIO reads it. A GIF of one
            // frame goes as a copy without the position, else as a JPEG of its frame.
            if let copy = withoutPosition(source, kind: kind), copy.count <= limit {
                return .fitted(kept(copy, name: name, kind: kind, width: width, height: height))
            }
        }
        guard let image = downsampledImage(data, largestSide: min(longest, longestSide)) else { return .cannotSend }
        for quality in [quality, 0.6, 0.4] {
            if let jpeg = jpegData(image, quality: quality), jpeg.count <= limit {
                return .fitted(FittedFile(data: jpeg, name: jpegName(name), type: "image/jpeg",
                                          width: image.width, height: image.height))
            }
        }
        return .tooLarge
    }

    /// The file has a GPS position: in the container, or in the EXIF data (the GPS dictionary of ImageIO) or the XMP
    /// data of any of its images. A file with more than one image, for example a TIFF with pages, can carry a
    /// position only on a later image.
    static func hasPosition(_ source: CGImageSource) -> Bool {
        let container = CGImageSourceCopyProperties(source, nil) as? [CFString: Any]
        if container?[kCGImagePropertyGPSDictionary] != nil { return true }
        return (0..<CGImageSourceGetCount(source)).contains { index in
            let image = CGImageSourceCopyPropertiesAtIndex(source, index, nil) as? [CFString: Any]
            if image?[kCGImagePropertyGPSDictionary] != nil { return true }
            guard let metadata = CGImageSourceCopyMetadataAtIndex(source, index, nil) else { return false }
            return [kCGImagePropertyGPSLatitude, kCGImagePropertyGPSLongitude].contains { key in
                CGImageMetadataCopyTagMatchingImageProperty(metadata, kCGImagePropertyGPSDictionary, key) != nil
            }
        }
    }

    /// A copy of the image without its GPS data. ImageIO copies the encoded pixels and leaves out the position. Nil
    /// when the copy fails, or when it still has a position.
    private static func withoutPosition(_ source: CGImageSource, kind: UTType) -> Data? {
        let buffer = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(buffer, kind.identifier as CFString,
                                                                 CGImageSourceGetCount(source), nil),
              CGImageDestinationCopyImageSource(destination, source,
                                                [kCGImageMetadataShouldExcludeGPS: true] as CFDictionary, nil)
        else { return nil }
        let data = buffer as Data
        guard let copy = CGImageSourceCreateWithData(data as CFData, nil), !hasPosition(copy) else { return nil }
        return data
    }

    private static func kept(_ data: Data, name: String, kind: UTType, width: Int, height: Int) -> FittedFile {
        let fits = (1...largestSide).contains(width) && (1...largestSide).contains(height)
        return FittedFile(data: data, name: name, type: kind.preferredMIMEType ?? mimeType(name),
                          width: fits ? width : nil, height: fits ? height : nil)
    }

    /// The size of the first image in pixels, turned upright as the app draws it.
    private static func pixelSize(_ source: CGImageSource) -> (Int, Int)? {
        guard let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
              let width = properties[kCGImagePropertyPixelWidth] as? Int,
              let height = properties[kCGImagePropertyPixelHeight] as? Int, width > 0, height > 0 else { return nil }
        // The orientations 5 to 8 turn the image by a quarter.
        let turned = ((properties[kCGImagePropertyOrientation] as? Int) ?? 1) >= 5
        return turned ? (height, width) : (width, height)
    }

    private static func jpegData(_ image: CGImage, quality: Double) -> Data? {
        let buffer = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(buffer, UTType.jpeg.identifier as CFString, 1, nil)
        else { return nil }
        CGImageDestinationAddImage(destination, image, [kCGImageDestinationLossyCompressionQuality: quality] as CFDictionary)
        return CGImageDestinationFinalize(destination) ? buffer as Data : nil
    }

    static func jpegName(_ name: String) -> String {
        let base = (name as NSString).deletingPathExtension
        return (base.isEmpty ? "Photo" : base) + ".jpg"
    }

    static func mimeType(_ name: String) -> String? {
        let ext = (name as NSString).pathExtension
        return ext.isEmpty ? nil : UTType(filenameExtension: ext)?.preferredMIMEType
    }
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
