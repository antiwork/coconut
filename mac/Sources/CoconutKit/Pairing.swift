import Foundation

/// The result of `scripts/pairing.py --share`: the `coconut://pair` setup link and the path of its QR image.
public struct PairingLink: Equatable {
    public let link: String
    public let image: String?

    public init(link: String, image: String?) {
        self.link = link
        self.image = image
    }

    /// Read the lines "Setup link: …" and "QR image: …". Return nil when the output has no setup link.
    public init?(output: String) {
        var link: String?
        var image: String?
        for line in output.split(whereSeparator: \.isNewline) {
            if line.hasPrefix("Setup link: ") {
                link = String(line.dropFirst("Setup link: ".count)).trimmingCharacters(in: .whitespaces)
            } else if line.hasPrefix("QR image: ") {
                image = String(line.dropFirst("QR image: ".count)).trimmingCharacters(in: .whitespaces)
            }
        }
        // coconut://pair/<token> holds a relay setup.
        guard let link, link.hasPrefix("coconut://pair/") else { return nil }
        self.link = link
        self.image = image
    }
}
