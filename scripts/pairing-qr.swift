import AppKit
import CoreImage

// The code arrives through stdin so process arguments do not expose it.
let payload = FileHandle.standardInput.readDataToEndOfFile()
guard CommandLine.arguments.count == 2,
      !payload.isEmpty,
      let filter = CIFilter(name: "CIQRCodeGenerator") else { exit(1) }
filter.setValue(payload, forKey: "inputMessage")
filter.setValue("M", forKey: "inputCorrectionLevel")
guard let source = filter.outputImage else { exit(1) }
let margin: CGFloat = 4
let canvas = source.extent.insetBy(dx: -margin, dy: -margin)
let white = CIImage(color: .white).cropped(to: canvas)
let qr = source.composited(over: white)
    .transformed(by: CGAffineTransform(scaleX: 8, y: 8))
guard let image = CIContext().createCGImage(qr, from: qr.extent),
      let png = NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:]) else { exit(1) }
do {
    try png.write(to: URL(fileURLWithPath: CommandLine.arguments[1]), options: .atomic)
} catch { exit(1) }
