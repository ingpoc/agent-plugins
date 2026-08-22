// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "CUAService",
    platforms: [.macOS(.v13)],
    dependencies: [
        .package(url: "https://github.com/apple/swift-nio.git", from: "2.65.0"),
        .package(url: "https://github.com/apple/swift-log.git", from: "1.5.0"),
        .package(url: "https://github.com/apple/swift-argument-parser.git", from: "1.3.0"),
        .package(url: "https://github.com/MrKai77/DynamicNotchKit", from: "1.0.0"),
    ],
    targets: [
        .executableTarget(
            name: "CUAService",
            dependencies: [
                .product(name: "NIOCore", package: "swift-nio"),
                .product(name: "NIOPosix", package: "swift-nio"),
                .product(name: "Logging", package: "swift-log"),
                .product(name: "ArgumentParser", package: "swift-argument-parser"),
                .product(name: "DynamicNotchKit", package: "DynamicNotchKit"),
            ],
            linkerSettings: [
                .linkedFramework("AppKit"),
                .linkedFramework("ScreenCaptureKit"),
            ]
        ),
        .testTarget(
            name: "CUAServiceTests",
            dependencies: ["CUAService"],
            exclude: ["test_cua_client.py", "__pycache__"]
        ),
    ]
)
