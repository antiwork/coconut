// swift-tools-version:5.9
// CoconutKit and the `coconut` tool that run Coconut on this Mac. The app itself is the CoconutMac target of
// ios/project.yml, which compiles the views of the iPhone app too. scripts/mac_app.py builds the bundle.
import PackageDescription

let package = Package(
    name: "CoconutMac",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "CoconutKit", targets: ["CoconutKit"]),
        // The bundle renames this to Contents/Helpers/coconut.
        .executable(name: "CoconutTool", targets: ["CoconutTool"]),
    ],
    targets: [
        .target(name: "CoconutKit"),
        .executableTarget(name: "CoconutTool", dependencies: ["CoconutKit"]),
    ]
)
