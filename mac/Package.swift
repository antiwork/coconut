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
    targets: [
        .target(name: "CoconutKit"),
        .executableTarget(name: "CoconutApp", dependencies: ["CoconutKit"]),
        .executableTarget(name: "CoconutTool", dependencies: ["CoconutKit"]),
        .testTarget(name: "CoconutKitTests", dependencies: ["CoconutKit"]),
    ]
)
