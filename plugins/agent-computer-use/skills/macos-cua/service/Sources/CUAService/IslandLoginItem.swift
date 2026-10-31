import AppKit
import ServiceManagement

@MainActor
protocol IslandLoginItem: AnyObject {
    var status: SMAppService.Status { get }
    func register() throws
    func unregister() throws
    func openSettings()
}

extension IslandLoginItem {
    var isOn: Bool { status == .enabled || status == .requiresApproval }
}

@MainActor
final class SystemIslandLoginItem: IslandLoginItem {
    var status: SMAppService.Status { SMAppService.mainApp.status }
    func register() throws { try SMAppService.mainApp.register() }
    func unregister() throws { try SMAppService.mainApp.unregister() }
    func openSettings() { SMAppService.openSystemSettingsLoginItems() }
}

@MainActor
final class MemoryIslandLoginItem: IslandLoginItem {
    var status: SMAppService.Status = .notRegistered
    var statusAfterRegister: SMAppService.Status = .enabled
    private(set) var registrations = 0
    private(set) var unregistrations = 0
    private(set) var settingsOpened = 0
    func register() throws { registrations += 1; status = statusAfterRegister }
    func unregister() throws { unregistrations += 1; status = .notRegistered }
    func openSettings() { settingsOpened += 1 }
}

struct IslandSettingsScreen {
    var uuid: String
    var name: String
    var builtIn: Bool

    static func connected() -> [Self] {
        NSScreen.screens.map { screen in
            Self(uuid: screen.displayUUID, name: screen.localizedName,
                 builtIn: screen.displayID.map { CGDisplayIsBuiltin($0) != 0 } ?? false)
        }
    }
}
