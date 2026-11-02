// Make the icon set of Coconut.app from the square iPhone icon.
//
// Usage: swift mac-icon.swift <icon.png> <Coconut.iconset>
//
// A macOS icon is not full-bleed. Each image draws the iPhone icon in a rounded square of 824 of 1024 points with a
// corner radius of 185, as in Apple's macOS icon template, over a soft shadow. Then `iconutil` turns the folder into
// an .icns file (scripts/mac_app.py).
import AppKit

let arguments = CommandLine.arguments
guard arguments.count == 3, let source = NSImage(contentsOfFile: arguments[1]),
      let artwork = source.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    FileHandle.standardError.write("Usage: swift mac-icon.swift <icon.png> <Coconut.iconset>\n".data(using: .utf8)!)
    exit(2)
}
let folder = URL(fileURLWithPath: arguments[2])
try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)

func image(pixels: Int) throws -> Data {
    let scale = CGFloat(pixels) / 1024
    let context = CGContext(data: nil, width: pixels, height: pixels, bitsPerComponent: 8, bytesPerRow: 0,
                            space: CGColorSpace(name: CGColorSpace.sRGB)!,
                            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
    context.interpolationQuality = .high
    context.scaleBy(x: scale, y: scale)
    let tile = CGRect(x: 100, y: 100, width: 824, height: 824)
    let shape = CGPath(roundedRect: tile, cornerWidth: 185, cornerHeight: 185, transform: nil)
    context.saveGState()
    context.setShadow(offset: CGSize(width: 0, height: -10), blur: 28, color: CGColor(gray: 0, alpha: 0.3))
    context.addPath(shape)
    context.setFillColor(CGColor(gray: 1, alpha: 1))
    context.fillPath()
    context.restoreGState()
    context.addPath(shape)
    context.clip()
    context.draw(artwork, in: tile)
    let bitmap = NSBitmapImageRep(cgImage: context.makeImage()!)
    return bitmap.representation(using: .png, properties: [:])!
}

for points in [16, 32, 128, 256, 512] {
    for factor in [1, 2] {
        let name = factor == 1 ? "icon_\(points)x\(points).png" : "icon_\(points)x\(points)@2x.png"
        try image(pixels: points * factor).write(to: folder.appendingPathComponent(name))
    }
}
