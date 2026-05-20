// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "MagiVisionHelper",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "magi-vision-helper", targets: ["MagiVisionHelper"]),
    ],
    targets: [
        .executableTarget(
            name: "MagiVisionHelper",
            path: "Sources/MagiVisionHelper"
        ),
        .testTarget(
            name: "MagiVisionHelperTests",
            dependencies: ["MagiVisionHelper"],
            path: "Tests/MagiVisionHelperTests"
        ),
    ]
)
