import AppKit
import Darwin
import Foundation

@main
@MainActor
private struct MacOSCUAOperator {
    static func main() {
        let statePath = CommandLine.arguments.dropFirst().first
            ?? NSString(string: "~/.cache/macos-cua/operator-state.json").expandingTildeInPath
        let application = NSApplication.shared
        let delegate = OperatorDelegate(stateURL: URL(fileURLWithPath: statePath))
        application.delegate = delegate
        withExtendedLifetime(delegate) {
            application.run()
        }
    }
}
