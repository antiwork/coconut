// swift-tools-version:5.9
// Coconut.app: the menu bar app and the `coconut` tool that run Coconut on this Mac. scripts/mac_app.py builds the bundle.
import PackageDescription

let package = Package(
    name: "CoconutMac",
    platforms: [.macOS(.v14)],
    products: [
        // The bundle renames these to Contents/MacOS/Coconut and Contents/Helpers/coconut. The names differ here, because
        // the file system of a Mac ignores case and both binaries share one build folder.
        .executable(name: "CoconutApp", targets: ["CoconutApp"]),
        .executable(name: "CoconutTool", targets: ["CoconutTool"]),
    ],
    dependencies: [
        // The owner made the EdDSA key pair of the feed with this version. Update both together.
        .package(url: "https://github.com/sparkle-project/Sparkle", exact: "2.10.0"),
    ],
    targets: [
        .target(name: "CoconutKit"),
        .executableTarget(
            name: "CoconutApp",
            dependencies: ["CoconutKit", .product(name: "Sparkle", package: "Sparkle")],
            // The bundle keeps Sparkle.framework in Contents/Frameworks.
            linkerSettings: [.unsafeFlags(["-Xlinker", "-rpath", "-Xlinker", "@executable_path/../Frameworks"])]
        ),
        .executableTarget(name: "CoconutTool", dependencies: ["CoconutKit"]),
        .testTarget(name: "CoconutKitTests", dependencies: ["CoconutKit"]),
    ]
)
